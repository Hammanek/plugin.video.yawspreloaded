import io
import json
import os
import tempfile
import unittest
import zipfile

from requests import HTTPError

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
        self.assertEqual(os.listdir(self.profile), ["db"])

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
