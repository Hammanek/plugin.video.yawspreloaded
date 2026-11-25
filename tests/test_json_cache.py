import json
import logging
import os
import tempfile
import unittest
from unittest import mock

from yawsp_app.json_cache import AtomicJsonCache


class AtomicJsonCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = os.path.join(self.temporary_directory.name, "cache.json")
        self.logger = logging.getLogger("tests.json_cache")

    def cache(self):
        return AtomicJsonCache(self.path, logger=self.logger)

    def write_raw(self, contents):
        with open(self.path, "w", encoding="utf-8") as stream:
            stream.write(contents)

    def test_missing_file_loads_as_an_empty_mapping(self):
        self.assertEqual(self.cache().load(), {})

    def test_valid_legacy_mapping_is_preserved_without_discarding_values(self):
        legacy = {"movie:1": "legacy-id", "movie:2": None, "7": {"ts": 4}}
        self.write_raw(json.dumps(legacy))

        self.assertEqual(self.cache().load(), legacy)

    def test_malformed_json_load_is_reported_and_returns_empty_mapping(self):
        self.write_raw("{not-json")

        with self.assertLogs(self.logger, level="ERROR") as logs:
            self.assertEqual(self.cache().load(), {})

        self.assertEqual(len(logs.output), 1)

    def test_non_mapping_json_is_rejected(self):
        self.write_raw(json.dumps(["not", "a", "mapping"]))

        with self.assertLogs(self.logger, level="ERROR"):
            self.assertEqual(self.cache().load(), {})

    def test_failed_reload_preserves_previously_loaded_valid_data(self):
        self.write_raw(json.dumps({"valid": "value"}))
        cache = self.cache()
        self.assertEqual(cache.load(), {"valid": "value"})
        self.write_raw("{broken")

        with self.assertLogs(self.logger, level="ERROR"):
            self.assertEqual(cache.load(), {"valid": "value"})

    def test_save_writes_in_the_same_directory_and_atomically_replaces_target(self):
        self.write_raw(json.dumps({"old": True}))
        cache = self.cache()
        real_replace = os.replace

        with mock.patch("yawsp_app.json_cache.os.replace", wraps=real_replace) as replace:
            self.assertTrue(cache.save({"new": "žluťoučký"}))

        source, destination = replace.call_args.args
        self.assertEqual(os.path.dirname(source), self.temporary_directory.name)
        self.assertEqual(destination, self.path)
        with open(self.path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"new": "žluťoučký"})
        self.assertEqual(cache.load(), {"new": "žluťoučký"})

    def test_serialization_failure_leaves_original_file_and_cached_data_intact(self):
        self.write_raw(json.dumps({"valid": "original"}))
        cache = self.cache()

        with self.assertLogs(self.logger, level="ERROR"):
            self.assertFalse(cache.save({"not_serializable": object()}))

        with open(self.path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"valid": "original"})
        self.assertEqual(cache.load(), {"valid": "original"})

    def test_replace_failure_leaves_original_file_and_cached_data_intact(self):
        self.write_raw(json.dumps({"valid": "original"}))
        cache = self.cache()

        with mock.patch("yawsp_app.json_cache.os.replace", side_effect=OSError("locked")):
            with self.assertLogs(self.logger, level="ERROR"):
                self.assertFalse(cache.save({"new": "discarded"}))

        with open(self.path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"valid": "original"})
        self.assertEqual(cache.load(), {"valid": "original"})
        self.assertEqual(
            [name for name in os.listdir(self.temporary_directory.name) if name != "cache.json"],
            [],
        )

    def test_returned_mappings_do_not_alias_internal_state(self):
        cache = self.cache()
        loaded = cache.load()
        loaded["unexpected"] = True

        self.assertEqual(cache.load(), {})


if __name__ == "__main__":
    unittest.main()
