import hashlib
import json
import logging
import os
import stat
import tempfile
import unittest
from unittest import mock

from yawsp_app.genre_file_cache import GenreFileCache, MISSING, UNSUPPORTED, VALID
from yawsp_app.json_cache import AtomicJsonCache


READ_LIMIT = 128 * 1024
SUPPORTED_GROUPS = (
    "movie/en-US",
    "movie/cs-CZ",
    "movie/sk-SK",
    "tv/en-US",
    "tv/cs-CZ",
    "tv/sk-SK",
)


class GenreFileCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.profile = os.path.join(self.temporary_directory.name, "profile")
        self.path = os.path.join(self.profile, "tmdb_genres.json")
        self.logger = logging.getLogger("tests.genre_file_cache")

    def cache(self):
        return GenreFileCache(self.path, logger=self.logger)

    def write_bytes(self, contents):
        os.makedirs(self.profile, exist_ok=True)
        with open(self.path, "wb") as stream:
            stream.write(contents)

    def read_bytes(self):
        with open(self.path, "rb") as stream:
            return stream.read()

    def json_bytes(self, document):
        return json.dumps(document, ensure_ascii=False).encode("utf-8")

    def assert_unsupported_and_unchanged(self, contents):
        self.write_bytes(contents)
        original_hash = hashlib.sha256(contents).digest()

        state, document = self.cache().read()
        self.assertEqual(state, UNSUPPORTED)
        self.assertIsNone(document)
        self.assertIsNone(self.cache().get("movie", "en-US"))
        with self.assertLogs(self.logger, level="ERROR"):
            self.assertFalse(self.cache().store("movie", "en-US", {"1": "Replacement"}))

        self.assertEqual(self.read_bytes(), contents)
        self.assertEqual(hashlib.sha256(self.read_bytes()).digest(), original_hash)

    def test_atomic_primitive_baseline_writes_loads_and_replaces_exact_document(self):
        primitive = AtomicJsonCache(self.path, logger=self.logger)
        first = {"schema": 1, "genres": {"movie/en-US": {"28": "Akční"}}}
        self.assertTrue(primitive.save(first))
        self.assertEqual(primitive.load(), first)

        second = {"schema": 1, "genres": {"tv/cs-CZ": {"10759": "Žánr"}}}
        self.assertTrue(primitive.save(second))
        self.assertEqual(AtomicJsonCache(self.path, logger=self.logger).load(), second)

    def test_missing_file_and_missing_parent_are_missing_without_creation(self):
        cache = self.cache()

        self.assertEqual(cache.read(), (MISSING, None))
        self.assertIsNone(cache.get("movie", "en-US"))
        self.assertFalse(os.path.exists(self.profile))

    def test_empty_malformed_and_unknown_documents_are_not_missing_and_stay_unchanged(self):
        cases = (
            b"",
            b"{broken",
            b"null",
            b"[]",
            self.json_bytes({"error": "unavailable"}),
        )
        for contents in cases:
            with self.subTest(contents=contents):
                self.assert_unsupported_and_unchanged(contents)

    def test_valid_document_read_is_detached_and_has_exact_public_shape(self):
        source = {
            "schema": 1,
            "genres": {
                "movie/en-US": {"28": "Action", "12": " Adventure "},
                "tv/cs-CZ": {},
            },
        }
        self.write_bytes(self.json_bytes(source))

        state, document = self.cache().read()
        self.assertEqual(state, VALID)
        self.assertEqual(document, source)
        self.assertEqual(set(document), {"schema", "genres"})
        self.assertEqual(set(document["genres"]), {"movie/en-US", "tv/cs-CZ"})
        self.assertTrue(all(type(key) is str for key in document["genres"]["movie/en-US"]))
        self.assertTrue(
            all(type(value) is str for value in document["genres"]["movie/en-US"].values())
        )

        document["genres"]["movie/en-US"]["28"] = "Changed"
        document["genres"]["new-group"] = {"1": "Unknown"}
        self.assertEqual(self.cache().read(), (VALID, source))

    def test_get_distinguishes_absent_supported_group_from_present_empty_group(self):
        self.write_bytes(self.json_bytes({"schema": 1, "genres": {"movie/en-US": {}}}))
        cache = self.cache()

        empty = cache.get("movie", "en-US")
        self.assertEqual(empty, {})
        self.assertIsNotNone(empty)
        self.assertIsNone(cache.get("tv", "en-US"))

        empty["1"] = "local mutation"
        self.assertEqual(cache.get("movie", "en-US"), {})
        self.assertIsNone(cache.get("documentary", "en-US"))
        self.assertIsNone(cache.get("movie", "de-DE"))

    def test_all_six_supported_media_language_groups_round_trip(self):
        groups = {
            group: {str(index + 1): "Genre {0}".format(index + 1)}
            for index, group in enumerate(SUPPORTED_GROUPS)
        }
        self.write_bytes(self.json_bytes({"schema": 1, "genres": groups}))

        state, document = self.cache().read()
        self.assertEqual(state, VALID)
        self.assertEqual(document["genres"], groups)
        for group in SUPPORTED_GROUPS:
            media, language = group.split("/")
            with self.subTest(group=group):
                value = self.cache().get(media, language)
                value["999"] = "Detached"
                self.assertNotIn("999", self.cache().get(media, language))

    def test_current_corruption_never_trusts_previously_valid_atomic_memory(self):
        source = {"schema": 1, "genres": {"movie/en-US": {"28": "Action"}}}
        self.write_bytes(self.json_bytes(source))
        old_atomic = AtomicJsonCache(self.path, logger=self.logger)
        self.assertEqual(old_atomic.load(), source)
        self.assertEqual(self.cache().read(), (VALID, source))

        self.write_bytes(b"{current-is-corrupt")
        with self.assertLogs(self.logger, level="ERROR"):
            self.assertEqual(old_atomic.load(), source)
        self.assertEqual(self.cache().read(), (UNSUPPORTED, None))
        self.assertIsNone(self.cache().get("movie", "en-US"))

    def test_duplicate_keys_at_every_object_scope_are_rejected_without_overwrite(self):
        cases = (
            b'{"schema":1,"schema":1,"genres":{}}',
            b'{"schema":1,"genres":{},"genres":{}}',
            b'{"schema":1,"genres":{"movie/en-US":{},"movie/en-US":{}}}',
            b'{"schema":1,"genres":{"movie/en-US":{"28":"Action","28":"Drama"}}}',
            b'{"schema":1,"genres":{"movie/en-US":{"28":{"name":"Action","name":"Drama"}}}}',
        )
        for contents in cases:
            with self.subTest(contents=contents):
                self.assert_unsupported_and_unchanged(contents)

    def test_root_and_group_shape_violations_are_strictly_rejected(self):
        valid_genres = {"movie/en-US": {"28": "Action"}}
        cases = {
            "missing-schema": {"genres": valid_genres},
            "missing-genres": {"schema": 1},
            "extra-root": {"schema": 1, "genres": valid_genres, "request_url": "x"},
            "bool-schema": {"schema": True, "genres": valid_genres},
            "float-schema": {"schema": 1.0, "genres": valid_genres},
            "string-schema": {"schema": "1", "genres": valid_genres},
            "future-schema": {"schema": 2, "genres": valid_genres},
            "genres-list": {"schema": 1, "genres": []},
            "genres-null": {"schema": 1, "genres": None},
            "member-list": {"schema": 1, "genres": {"movie/en-US": []}},
            "member-extra-field": {
                "schema": 1,
                "genres": {"movie/en-US": {"28": {"name": "Action", "session": "x"}}},
            },
        }
        for label, document in cases.items():
            with self.subTest(label=label):
                self.assert_unsupported_and_unchanged(self.json_bytes(document))

    def test_unknown_group_names_aliases_and_private_scopes_are_rejected(self):
        groups = (
            "movie/de-DE",
            "documentary/en-US",
            "movie:en-US",
            "Movie/en-US",
            "movie/en-us",
            "movie/en-US ",
            "movie/en-US/account",
            "account/session",
            "request_url",
            "watchlist/en-US",
        )
        for group in groups:
            with self.subTest(group=group):
                contents = self.json_bytes({"schema": 1, "genres": {group: {"1": "Name"}}})
                self.assert_unsupported_and_unchanged(contents)

    def test_only_canonical_ascii_positive_decimal_member_keys_are_accepted(self):
        invalid_ids = (
            "0",
            "-1",
            "+1",
            "01",
            "001",
            "1.0",
            "1e2",
            " 1",
            "1 ",
            "1\n",
            "١",
            "１",
            "²",
            "True",
            "",
        )
        for identifier in invalid_ids:
            with self.subTest(identifier=identifier):
                contents = self.json_bytes(
                    {"schema": 1, "genres": {"movie/en-US": {identifier: "Action"}}}
                )
                self.assert_unsupported_and_unchanged(contents)

    def test_names_must_be_nonblank_actual_strings_within_the_bound(self):
        invalid_names = ("", " ", "\t\n", None, True, 1, 1.5, [], {}, "x" * 201)
        for name in invalid_names:
            with self.subTest(name=repr(name)[:40]):
                contents = self.json_bytes(
                    {"schema": 1, "genres": {"movie/en-US": {"28": name}}}
                )
                self.assert_unsupported_and_unchanged(contents)

    def test_valid_name_content_and_boundary_length_are_preserved(self):
        names = {"1": " Action ", "2": "Žluťoučký žánr", "3": "x" * 200}
        self.write_bytes(self.json_bytes({"schema": 1, "genres": {"movie/sk-SK": names}}))

        self.assertEqual(self.cache().get("movie", "sk-SK"), names)

    def test_json_special_numbers_and_excessive_nesting_are_unsupported(self):
        cases = (
            b'{"schema":NaN,"genres":{}}',
            b'{"schema":Infinity,"genres":{}}',
            b'{"schema":1,"genres":{"movie/en-US":{"1":NaN}}}',
            b'{"schema":1,"genres":{"movie/en-US":{"1":Infinity}}}',
            b'{"schema":1,"genres":{"movie/en-US":{"1":-Infinity}}}',
            b"[" * 2000 + b"0" + b"]" * 2000,
        )
        for contents in cases:
            with self.subTest(prefix=contents[:35]):
                self.assert_unsupported_and_unchanged(contents)

    def test_read_limit_is_128_kibibytes_with_one_guard_byte(self):
        document = self.json_bytes({"schema": 1, "genres": {"movie/en-US": {"1": "Action"}}})
        exact_limit = document + b" " * (READ_LIMIT - len(document))
        over_limit = exact_limit + b" "
        self.assertEqual(len(exact_limit), READ_LIMIT)

        self.write_bytes(exact_limit)
        self.assertEqual(self.cache().get("movie", "en-US"), {"1": "Action"})
        self.assert_unsupported_and_unchanged(over_limit)

    def test_invalid_utf8_and_read_errors_are_unsupported(self):
        self.assert_unsupported_and_unchanged(b'{"schema":1,"genres":{}}\xff')

        self.write_bytes(self.json_bytes({"schema": 1, "genres": {}}))
        original = self.read_bytes()
        with mock.patch("yawsp_app.genre_file_cache.os.open", side_effect=OSError("denied")):
            self.assertEqual(self.cache().read(), (UNSUPPORTED, None))
        self.assertEqual(self.read_bytes(), original)

    def test_directory_and_other_nonregular_targets_are_not_read_or_replaced(self):
        os.makedirs(self.path)
        with self.assertLogs(self.logger, level="ERROR"):
            self.assertFalse(self.cache().store("movie", "en-US", {"1": "Action"}))
        self.assertEqual(self.cache().read(), (UNSUPPORTED, None))
        self.assertTrue(stat.S_ISDIR(os.lstat(self.path).st_mode))

    def test_symlink_target_bytes_remain_unchanged_when_symlinks_are_supported(self):
        target = os.path.join(self.temporary_directory.name, "target.json")
        original = b'{"schema":1,"genres":{"movie/en-US":{"1":"Original"}}}'
        with open(target, "wb") as stream:
            stream.write(original)
        os.makedirs(self.profile, exist_ok=True)
        try:
            os.symlink(target, self.path)
        except (NotImplementedError, OSError) as exc:
            self.skipTest("symlink creation is unavailable: {0}".format(exc))

        with self.assertLogs(self.logger, level="ERROR"):
            self.assertFalse(self.cache().store("movie", "en-US", {"2": "Replacement"}))
        self.assertEqual(self.cache().read(), (UNSUPPORTED, None))
        with open(target, "rb") as stream:
            self.assertEqual(stream.read(), original)
        self.assertTrue(os.path.islink(self.path))

    def test_symlink_shaped_lstat_rejects_before_open_or_save_and_preserves_bytes(self):
        original = self.json_bytes(
            {"schema": 1, "genres": {"movie/en-US": {"1": "Original"}}}
        )
        self.write_bytes(original)
        link_metadata = mock.Mock(st_mode=stat.S_IFLNK)

        with mock.patch(
            "yawsp_app.genre_file_cache.os.lstat", return_value=link_metadata
        ), mock.patch("yawsp_app.genre_file_cache.os.open") as safe_open, mock.patch.object(
            AtomicJsonCache, "save"
        ) as save:
            cache = self.cache()
            self.assertEqual(cache.read(), (UNSUPPORTED, None))
            with self.assertLogs(self.logger, level="ERROR"):
                self.assertFalse(cache.store("movie", "en-US", {"2": "Replacement"}))

        safe_open.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.read_bytes(), original)

    def test_read_admission_uses_lstat_and_nontruncating_nofollow_open(self):
        self.write_bytes(self.json_bytes({"schema": 1, "genres": {}}))
        real_open = os.open
        real_lstat = os.lstat

        with mock.patch("yawsp_app.genre_file_cache.os.open", wraps=real_open) as safe_open:
            with mock.patch("yawsp_app.genre_file_cache.os.lstat", wraps=real_lstat) as lstat:
                self.assertEqual(self.cache().read()[0], VALID)

        lstat.assert_called_once_with(self.path)
        flags = safe_open.call_args.args[1]
        self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if hasattr(os, "O_NOFOLLOW"):
            self.assertTrue(flags & os.O_NOFOLLOW)

    def test_store_from_missing_parent_commits_before_independent_offline_reader(self):
        events = []

        class ClosedResponse:
            def close(self):
                events.append("response.close")

        class OfflineClient:
            def get(self, *args, **kwargs):
                raise AssertionError("an independent cache read must not perform a client request")

        response = ClosedResponse()
        response.close()
        real_replace = os.replace

        def observed_replace(source, destination):
            result = real_replace(source, destination)
            events.append("os.replace")
            return result

        with mock.patch("yawsp_app.json_cache.os.replace", side_effect=observed_replace):
            committed = self.cache().store("movie", "en-US", {"28": "Action"})

        self.assertTrue(committed)
        events.append("reader.created")
        reader = self.cache()
        client = OfflineClient()
        self.assertEqual(reader.get("movie", "en-US"), {"28": "Action"})
        self.assertEqual(reader.read()[0], VALID)
        self.assertEqual(events, ["response.close", "os.replace", "reader.created"])
        self.assertFalse(hasattr(client, "requests"))

    def test_replace_event_follows_actual_replacement_and_validated_disk_read(self):
        events = []
        real_replace = os.replace

        def observed_replace(source, destination):
            result = real_replace(source, destination)
            self.assertEqual(self.cache().get("movie", "en-US"), {"28": "Action"})
            events.append("os.replace")
            return result

        with mock.patch("yawsp_app.json_cache.os.replace", side_effect=observed_replace):
            committed = self.cache().store("movie", "en-US", {"28": "Action"})

        self.assertTrue(committed)
        self.assertEqual(events, ["os.replace"])

    def test_store_preserves_other_allowed_groups_and_returns_detached_values(self):
        self.assertTrue(self.cache().store("movie", "en-US", {"28": "Action"}))
        self.assertTrue(self.cache().store("tv", "cs-CZ", {"10759": "Akční a dobrodružný"}))
        self.assertTrue(self.cache().store("movie", "sk-SK", {}))

        reader = self.cache()
        self.assertEqual(reader.get("movie", "en-US"), {"28": "Action"})
        self.assertEqual(reader.get("tv", "cs-CZ"), {"10759": "Akční a dobrodružný"})
        self.assertEqual(reader.get("movie", "sk-SK"), {})
        state, document = reader.read()
        self.assertEqual(state, VALID)
        self.assertEqual(set(document["genres"]), {"movie/en-US", "tv/cs-CZ", "movie/sk-SK"})

    def test_failed_atomic_replacement_returns_false_and_does_not_fabricate_a_hit(self):
        self.assertTrue(self.cache().store("movie", "en-US", {"28": "Action"}))
        original = self.read_bytes()

        with mock.patch("yawsp_app.json_cache.os.replace", side_effect=OSError("locked")):
            with self.assertLogs(self.logger, level="ERROR"):
                self.assertFalse(self.cache().store("tv", "en-US", {"18": "Drama"}))

        self.assertEqual(self.read_bytes(), original)
        reader = self.cache()
        self.assertEqual(reader.get("movie", "en-US"), {"28": "Action"})
        self.assertIsNone(reader.get("tv", "en-US"))

    def test_store_rejects_invalid_input_without_creating_or_touching_the_file(self):
        invalid_calls = (
            ("movie", "de-DE", {"1": "Name"}),
            ("documentary", "en-US", {"1": "Name"}),
            ("movie", "en-US", None),
            ("movie", "en-US", {"01": "Name"}),
            ("movie", "en-US", {"1": " "}),
            ("movie", "en-US", {"1": "x" * 201}),
        )
        for media, language, mapping in invalid_calls:
            with self.subTest(media=media, language=language, mapping=repr(mapping)[:40]):
                with self.assertLogs(self.logger, level="ERROR"):
                    self.assertFalse(self.cache().store(media, language, mapping))
                self.assertFalse(os.path.exists(self.path))
                self.assertFalse(os.path.exists(self.profile))

    def test_store_rejects_a_merged_document_over_the_read_limit(self):
        oversized = {str(number): "Name" for number in range(1, 24000)}

        with self.assertLogs(self.logger, level="ERROR"):
            self.assertFalse(self.cache().store("movie", "en-US", oversized))
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.profile))

    def test_store_never_uses_atomic_load_as_current_disk_proof(self):
        self.write_bytes(self.json_bytes({"schema": 1, "genres": {"movie/en-US": {"1": "One"}}}))

        with mock.patch.object(
            AtomicJsonCache, "load", side_effect=AssertionError("generic load is not strict proof")
        ):
            self.assertTrue(self.cache().store("tv", "en-US", {"2": "Two"}))

        reader = self.cache()
        self.assertEqual(reader.get("movie", "en-US"), {"1": "One"})
        self.assertEqual(reader.get("tv", "en-US"), {"2": "Two"})


if __name__ == "__main__":
    unittest.main()
