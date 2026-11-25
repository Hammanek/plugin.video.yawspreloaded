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

    def test_deepcopy_failure_returns_false_and_preserves_original_bytes_and_load(self):
        class Uncopyable:
            def __deepcopy__(self, memo):
                raise TypeError("defensive copy failed")

        self.write_raw('{"valid": "original"}')
        cache = self.cache()
        with open(self.path, "rb") as stream:
            original_bytes = stream.read()

        with self.assertLogs(self.logger, level="ERROR") as logs:
            self.assertFalse(cache.save({"new": Uncopyable()}))

        self.assertEqual(len(logs.output), 1)
        with open(self.path, "rb") as stream:
            self.assertEqual(stream.read(), original_bytes)
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

    def test_save_merged_reload_merge_and_save_all_hold_the_cache_lock(self):
        cache = self.cache()
        self.assertTrue(cache.save({"current": "on-demand", "shared": "newer"}))
        real_load = cache.load
        real_save = cache.save
        lock_observations = []

        def checked_load():
            lock_observations.append(("load", cache._lock._is_owned()))
            return real_load()

        def checked_save(mapping):
            lock_observations.append(("save", cache._lock._is_owned()))
            return real_save(mapping)

        def prefer_update(candidate, current):
            lock_observations.append(("merge", cache._lock._is_owned()))
            self.assertEqual(candidate, "worker")
            self.assertEqual(current, "newer")
            return False

        with mock.patch.object(cache, "load", side_effect=checked_load), mock.patch.object(
            cache, "save", side_effect=checked_save
        ):
            self.assertTrue(
                cache.save_merged(
                    {"added": "worker", "shared": "worker"},
                    prefer_update=prefer_update,
                )
            )

        self.assertEqual(
            cache.load(),
            {"current": "on-demand", "added": "worker", "shared": "newer"},
        )
        self.assertEqual(
            lock_observations,
            [("load", True), ("merge", True), ("save", True)],
        )

    def test_returned_mappings_do_not_alias_internal_state(self):
        cache = self.cache()
        loaded = cache.load()
        loaded["unexpected"] = True

        self.assertEqual(cache.load(), {})


if __name__ == "__main__":
    unittest.main()
