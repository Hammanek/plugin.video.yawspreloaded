import io
import json
import os
import tempfile
import unittest
from unittest import mock
import zipfile

from requests import HTTPError

from yawsp_app import backup_db
from yawsp_app.backup_db import BackupDBError, BackupDBStore


VALID_PAYLOAD = b'{"data": [{"id": 1, "title": "Film"}]}'


def archive_bytes(files=None):
    files = {"A.txt": VALID_PAYLOAD} if files is None else files
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for filename, payload in files.items():
            archive.writestr(filename, payload)
    return output.getvalue()


def archive_with_bad_crc():
    archive = bytearray(archive_bytes())
    payload_offset = archive.index(VALID_PAYLOAD)
    archive[payload_offset] = ord("X") if archive[payload_offset] != ord("X") else ord("Y")
    return bytes(archive)


class BackupDBTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.profile = os.path.join(self.tempdir.name, "profile")
        os.mkdir(self.profile)
        self.store = BackupDBStore(self.profile)
        self.dbdir = os.path.join(self.profile, "db")

    def assert_failed_attempt_leaves_no_installation(self, failure):
        calls = []

        def load_archive():
            calls.append(1)
            if len(calls) == 1:
                if isinstance(failure, BaseException):
                    raise failure
                return failure
            return archive_bytes()

        with self.assertRaises(BackupDBError):
            self.store.ensure_installed(load_archive)
        self.assertFalse(os.path.exists(self.dbdir))
        self.assertEqual(os.listdir(self.profile), [])

        self.assertTrue(self.store.ensure_installed(load_archive))
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.store.is_valid())
        self.assertEqual(
            sorted(os.listdir(self.profile)), [".backup-db-validation.json", "db"]
        )

    def data_reads_during(self, action):
        with mock.patch(
            "yawsp_app.backup_db._read_json", wraps=backup_db._read_json
        ) as read_json:
            action()
        return [os.path.basename(call.args[0]) for call in read_json.call_args_list]

    def test_minimal_valid_archive_installs_without_complete_alphabet(self):
        calls = []

        def load_archive():
            calls.append(1)
            return archive_bytes({"Z.txt": VALID_PAYLOAD})

        self.assertTrue(self.store.ensure_installed(load_archive))
        self.assertTrue(self.store.is_valid())
        self.assertEqual(
            self.store.load("Z.txt"), [{"id": 1, "title": "Film"}]
        )
        self.assertEqual(sorted(os.listdir(self.dbdir)), [".manifest.json", "Z.txt"])

        self.assertTrue(self.store.ensure_installed(self.fail_if_called))
        self.assertEqual(len(calls), 1)

    def test_legacy_archive_with_db_directory_is_supported(self):
        self.assertTrue(
            self.store.ensure_installed(
                lambda: archive_bytes({"db/A.txt": VALID_PAYLOAD})
            )
        )

        self.assertEqual(self.store.load("A.txt")[0]["title"], "Film")

    def test_crc_failure_allows_successful_retry(self):
        self.assert_failed_attempt_leaves_no_installation(archive_with_bad_crc())

    def test_unsafe_member_allows_successful_retry_without_escaping(self):
        unsafe = archive_bytes(
            {"A.txt": VALID_PAYLOAD, "../escape.txt": VALID_PAYLOAD}
        )

        self.assert_failed_attempt_leaves_no_installation(unsafe)
        self.assertFalse(os.path.exists(os.path.join(self.tempdir.name, "escape.txt")))

    def test_empty_archive_allows_successful_retry(self):
        self.assert_failed_attempt_leaves_no_installation(archive_bytes({}))

    def test_missing_data_allows_successful_retry(self):
        self.assert_failed_attempt_leaves_no_installation(
            archive_bytes({"A.txt": b'{"items": []}'})
        )

    def test_non_object_json_allows_successful_retry(self):
        self.assert_failed_attempt_leaves_no_installation(
            archive_bytes({"A.txt": b'[{"data": []}]'})
        )

    def test_malformed_json_allows_successful_retry(self):
        self.assert_failed_attempt_leaves_no_installation(
            archive_bytes({"A.txt": b'{"data": ['})
        )

    def test_http_failure_allows_successful_retry(self):
        self.assert_failed_attempt_leaves_no_installation(
            HTTPError("synthetic offline HTTP failure")
        )

    def test_every_txt_file_must_be_valid(self):
        invalid = archive_bytes(
            {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": {}}'}
        )

        self.assert_failed_attempt_leaves_no_installation(invalid)

    def test_valid_unmarked_legacy_installation_remains_untouched(self):
        os.mkdir(self.dbdir)
        original = b'{"data": [{"id": "legacy"}]}'
        with open(os.path.join(self.dbdir, "A.txt"), "wb") as legacy:
            legacy.write(original)

        self.assertTrue(self.store.is_valid())
        self.assertTrue(self.store.ensure_installed(self.fail_if_called))
        with open(os.path.join(self.dbdir, "A.txt"), "rb") as legacy:
            self.assertEqual(legacy.read(), original)
        self.assertEqual(os.listdir(self.dbdir), ["A.txt"])

    def test_empty_legacy_installation_is_replaced(self):
        os.mkdir(self.dbdir)

        self.assertFalse(self.store.is_valid())
        self.assertTrue(self.store.ensure_installed(lambda: archive_bytes()))

        self.assertEqual(self.store.load("A.txt")[0]["title"], "Film")

    def test_partial_legacy_installation_survives_failed_replacement_then_retries(self):
        os.mkdir(self.dbdir)
        partial_path = os.path.join(self.dbdir, "A.txt")
        with open(partial_path, "wb") as partial:
            partial.write(b'{"data": [')
        archives = iter([archive_bytes({"A.txt": b"not json"}), archive_bytes()])

        self.assertFalse(self.store.is_valid())
        with self.assertRaises(BackupDBError):
            self.store.ensure_installed(lambda: next(archives))
        with open(partial_path, "rb") as partial:
            self.assertEqual(partial.read(), b'{"data": [')

        self.assertTrue(self.store.ensure_installed(lambda: next(archives)))
        self.assertEqual(self.store.load("A.txt")[0]["title"], "Film")

    def test_double_replacement_failure_preserves_and_recovers_previous_database(self):
        os.mkdir(self.dbdir)
        original = b'{"data": ['
        original_path = os.path.join(self.dbdir, "A.txt")
        with open(original_path, "wb") as legacy:
            legacy.write(original)
        real_replace = os.replace

        def fail_promotion_and_rollback(source, destination):
            if destination == self.dbdir and source.endswith("staged-db"):
                raise OSError("forced promotion failure")
            if destination == self.dbdir and source.endswith("previous-db"):
                raise OSError("forced rollback failure")
            return real_replace(source, destination)

        with mock.patch(
            "yawsp_app.backup_db.os.replace", side_effect=fail_promotion_and_rollback
        ):
            with self.assertRaises(BackupDBError) as raised:
                self.store.ensure_installed(lambda: archive_bytes())

        message = str(raised.exception)
        self.assertIn("promotion failed", message)
        self.assertIn("rollback failed", message)
        recovery_path = os.path.join(
            self.profile, ".backup-db-recovery", "previous-db", "A.txt"
        )
        with open(recovery_path, "rb") as recovered:
            self.assertEqual(recovered.read(), original)
        self.assertFalse(os.path.exists(self.dbdir))

        bytes_seen_by_retry = []

        def recover_then_load_archive():
            with open(original_path, "rb") as recovered:
                bytes_seen_by_retry.append(recovered.read())
            return archive_bytes()

        recovered_store = BackupDBStore(self.profile)
        self.assertTrue(recovered_store.ensure_installed(recover_then_load_archive))
        self.assertEqual(bytes_seen_by_retry, [original])
        self.assertEqual(recovered_store.load("A.txt"), [{"id": 1, "title": "Film"}])
        self.assertEqual(
            sorted(os.listdir(self.profile)), [".backup-db-validation.json", "db"]
        )

    def test_new_manifest_detects_a_missing_data_file(self):
        self.store.ensure_installed(
            lambda: archive_bytes(
                {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": []}'}
            )
        )
        os.remove(os.path.join(self.dbdir, "A.txt"))

        self.assertFalse(self.store.is_valid())
        self.assertTrue(self.store.ensure_installed(lambda: archive_bytes()))
        self.assertEqual(self.store.load("A.txt")[0]["title"], "Film")

    def test_new_manifest_detects_valid_json_with_changed_size(self):
        self.store.ensure_installed(lambda: archive_bytes())
        with open(os.path.join(self.dbdir, "A.txt"), "wb") as changed:
            changed.write(b'{"data": []}')

        self.assertFalse(self.store.is_valid())

    def test_matching_signature_avoids_warm_validation_and_parses_only_target(self):
        self.store.ensure_installed(
            lambda: archive_bytes(
                {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": []}'}
            )
        )

        self.assertEqual(os.path.dirname(self.store.signature_path), self.profile)
        with open(self.store.signature_path, "r", encoding="utf-8") as source:
            signature = json.load(source)
        self.assertEqual(signature["schema_version"], 1)
        self.assertEqual(set(signature["files"]), {"A.txt", "B.txt"})
        for filename, recorded in signature["files"].items():
            actual = os.stat(os.path.join(self.dbdir, filename))
            self.assertEqual(recorded["size"], actual.st_size)
            self.assertEqual(recorded["mtime_ns"], actual.st_mtime_ns)

        warm = BackupDBStore(self.profile)
        reads = self.data_reads_during(
            lambda: warm.ensure_installed(self.fail_if_called)
        )
        self.assertEqual(reads, [])

        reads = self.data_reads_during(lambda: warm.load("A.txt"))
        self.assertEqual(reads, ["A.txt"])

    def test_is_valid_regenerates_signature_after_mtime_only_drift(self):
        self.store.ensure_installed(
            lambda: archive_bytes(
                {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": []}'}
            )
        )
        target = os.path.join(self.dbdir, "A.txt")
        original_bytes = VALID_PAYLOAD
        with open(target, "rb") as source:
            self.assertEqual(source.read(), original_bytes)
        modified = os.stat(target).st_mtime_ns + 1000000
        os.utime(target, ns=(modified, modified))

        first_reads = self.data_reads_during(
            lambda: self.assertTrue(self.store.is_valid())
        )
        second_reads = self.data_reads_during(
            lambda: self.assertTrue(self.store.is_valid())
        )

        self.assertEqual(sorted(first_reads), ["A.txt", "B.txt"])
        self.assertEqual(second_reads, [])
        with open(self.store.signature_path, "r", encoding="utf-8") as source:
            signature = json.load(source)
        self.assertEqual(signature["files"]["A.txt"]["mtime_ns"], modified)
        with open(target, "rb") as source:
            self.assertEqual(source.read(), original_bytes)

    def test_is_valid_recreates_corrupt_sidecar_then_uses_warm_metadata(self):
        self.store.ensure_installed(
            lambda: archive_bytes(
                {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": []}'}
            )
        )
        with open(self.store.signature_path, "w", encoding="utf-8") as marker:
            marker.write("{not json")

        first_reads = self.data_reads_during(
            lambda: self.assertTrue(self.store.is_valid())
        )
        second_reads = self.data_reads_during(
            lambda: self.assertTrue(self.store.is_valid())
        )

        self.assertEqual(sorted(first_reads), ["A.txt", "B.txt"])
        self.assertEqual(second_reads, [])
        with open(self.store.signature_path, "r", encoding="utf-8") as source:
            self.assertEqual(json.load(source)["schema_version"], 1)

    def test_failed_promotion_preserves_previous_validation_marker(self):
        self.store.ensure_installed(lambda: archive_bytes())
        with open(self.store.signature_path, "rb") as source:
            previous_marker = source.read()
        with open(os.path.join(self.dbdir, "A.txt"), "wb") as damaged:
            damaged.write(b'{"data": [')
        real_replace = os.replace

        def fail_promotion(source, destination):
            if destination == self.dbdir and source.endswith("staged-db"):
                raise OSError("forced promotion failure")
            return real_replace(source, destination)

        with mock.patch("yawsp_app.backup_db.os.replace", side_effect=fail_promotion):
            with self.assertRaises(BackupDBError):
                self.store.ensure_installed(
                    lambda: archive_bytes({"B.txt": b'{"data": []}'})
                )

        with open(self.store.signature_path, "rb") as source:
            self.assertEqual(source.read(), previous_marker)
        with open(os.path.join(self.dbdir, "A.txt"), "rb") as source:
            self.assertEqual(source.read(), b'{"data": [')

    def test_valid_legacy_installation_migrates_once_without_changing_data_bytes(self):
        os.mkdir(self.dbdir)
        originals = {
            "A.txt": b'{"data": [{"id": "legacy-a"}]}',
            "B.txt": b'{"data": [{"id": "legacy-b"}]}',
        }
        for filename, contents in originals.items():
            with open(os.path.join(self.dbdir, filename), "wb") as legacy:
                legacy.write(contents)

        reads = self.data_reads_during(
            lambda: self.store.ensure_installed(self.fail_if_called)
        )
        self.assertEqual(sorted(reads), ["A.txt", "B.txt"])
        self.assertTrue(os.path.isfile(self.store.signature_path))
        self.assertEqual(sorted(os.listdir(self.dbdir)), ["A.txt", "B.txt"])
        for filename, contents in originals.items():
            with open(os.path.join(self.dbdir, filename), "rb") as legacy:
                self.assertEqual(legacy.read(), contents)

        warm = BackupDBStore(self.profile)
        reads = self.data_reads_during(
            lambda: warm.ensure_installed(self.fail_if_called)
        )
        self.assertEqual(reads, [])

    def test_corrupt_or_unsafe_signature_revalidates_and_is_recreated(self):
        invalid_signatures = (
            "{not json",
            json.dumps({"schema_version": 1, "files": {"../A.txt": {}}}),
            json.dumps({"schema_version": 999, "files": {}}),
        )
        for invalid_signature in invalid_signatures:
            with self.subTest(signature=invalid_signature):
                with tempfile.TemporaryDirectory() as root:
                    store = BackupDBStore(os.path.join(root, "profile"))
                    store.ensure_installed(
                        lambda: archive_bytes(
                            {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": []}'}
                        )
                    )
                    with open(store.signature_path, "w", encoding="utf-8") as marker:
                        marker.write(invalid_signature)

                    reads = self.data_reads_during(
                        lambda: store.ensure_installed(self.fail_if_called)
                    )
                    self.assertEqual(sorted(reads), ["A.txt", "B.txt"])
                    self.assertTrue(store.is_valid())

    def test_external_size_and_mtime_changes_trigger_full_metadata_revalidation(self):
        for kind in ("size", "mtime"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as root:
                    store = BackupDBStore(os.path.join(root, "profile"))
                    store.ensure_installed(
                        lambda: archive_bytes(
                            {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": []}'}
                        )
                    )
                    target = os.path.join(store.dbdir, "A.txt")
                    if kind == "size":
                        with open(target, "w", encoding="utf-8") as changed:
                            changed.write('{"data": [{"id": 2}]}')
                    else:
                        modified = os.stat(target).st_mtime_ns + 1000000
                        os.utime(target, ns=(modified, modified))

                    loader = (
                        (lambda: archive_bytes(
                            {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": []}'}
                        ))
                        if kind == "size"
                        else self.fail_if_called
                    )
                    reads = self.data_reads_during(
                        lambda: store.ensure_installed(loader)
                    )
                    self.assertGreaterEqual(reads.count("A.txt"), 2 if kind == "size" else 1)
                    self.assertIn("B.txt", reads)
                    self.assertTrue(store.is_valid())

    def test_missing_or_extra_file_rechecks_existing_data_before_repair(self):
        for kind in ("missing", "extra"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as root:
                    store = BackupDBStore(os.path.join(root, "profile"))
                    files = {"A.txt": VALID_PAYLOAD, "B.txt": b'{"data": []}'}
                    store.ensure_installed(lambda: archive_bytes(files))
                    if kind == "missing":
                        os.remove(os.path.join(store.dbdir, "A.txt"))
                        expected_existing_reads = 1
                    else:
                        with open(os.path.join(store.dbdir, "C.txt"), "wb") as extra:
                            extra.write(VALID_PAYLOAD)
                        expected_existing_reads = 2

                    reads = self.data_reads_during(
                        lambda: store.ensure_installed(lambda: archive_bytes(files))
                    )
                    self.assertGreaterEqual(len(reads), expected_existing_reads + 2)
                    self.assertEqual(reads.count("A.txt"), 1 if kind == "missing" else 2)
                    self.assertTrue(store.is_valid())
                    self.assertNotIn("C.txt", os.listdir(store.dbdir))

    def test_signature_is_replaced_atomically_without_temporary_residue(self):
        real_replace = os.replace
        signature_replacements = []

        def record_replace(source, destination):
            if destination == self.store.signature_path:
                signature_replacements.append((source, destination))
            return real_replace(source, destination)

        with mock.patch("yawsp_app.backup_db.os.replace", side_effect=record_replace):
            self.store.ensure_installed(lambda: archive_bytes())

        self.assertEqual(len(signature_replacements), 1)
        source, destination = signature_replacements[0]
        self.assertNotEqual(source, destination)
        self.assertFalse(os.path.exists(source))
        self.assertEqual(
            sorted(os.listdir(self.profile)), [".backup-db-validation.json", "db"]
        )

    def test_seekable_temporary_archive_path_is_supported_and_always_removed(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                with tempfile.TemporaryDirectory() as root:
                    store = BackupDBStore(os.path.join(root, "profile"))
                    os.mkdir(store.profile)
                    archive_path = os.path.join(store.profile, "source.zip")
                    with open(archive_path, "wb") as source:
                        source.write(archive_with_bad_crc() if corrupt else archive_bytes())

                    if corrupt:
                        with self.assertRaises(BackupDBError):
                            store.ensure_installed(lambda: archive_path)
                    else:
                        self.assertTrue(store.ensure_installed(lambda: archive_path))
                        self.assertEqual(store.load("A.txt")[0]["title"], "Film")
                    self.assertFalse(os.path.exists(archive_path))
                    self.assertFalse(
                        any(name.startswith(".backup-db-") and name != ".backup-db-validation.json"
                            for name in os.listdir(store.profile))
                    )

    def test_crc_failure_is_detected_by_extraction_without_testzip_pass(self):
        with mock.patch(
            "yawsp_app.backup_db.zipfile.ZipFile.testzip",
            side_effect=AssertionError("redundant full testzip pass"),
        ) as testzip:
            with self.assertRaises(BackupDBError):
                self.store.ensure_installed(lambda: archive_with_bad_crc())
        testzip.assert_not_called()

    def test_group_selection_trims_and_only_routes_ascii_initials_to_alphabet(self):
        cases = {
            "  alice  ": "A.txt",
            "Zebra": "Z.txt",
            "  želva": "0.txt",
            "Éclair": "0.txt",
            "123": "0.txt",
            "   ": "0.txt",
            "Ωmega": "0.txt",
            "ßeta": "0.txt",
            "ıcarus": "0.txt",
        }

        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.store.group_for_query(query), expected)

    def test_load_rejects_traversal_and_non_dictionary_items(self):
        self.store.ensure_installed(lambda: archive_bytes())

        with self.assertRaises(BackupDBError):
            self.store.load("../A.txt")

        other = BackupDBStore(os.path.join(self.tempdir.name, "other"))
        other.ensure_installed(lambda: archive_bytes({"A.txt": b'{"data": [1]}'}))
        with self.assertRaises(BackupDBError):
            other.load("A.txt")

    def fail_if_called(self):
        self.fail("archive loader must not be called for a valid installation")


if __name__ == "__main__":
    unittest.main()
