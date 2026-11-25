import importlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yawsp_app.json_cache import AtomicJsonCache


MOVIE = {
    "id": 1,
    "title": "Film",
    "original_title": "Film",
    "release_date": "2026-01-02",
    "overview": "Plot",
    "poster_path": "/poster.jpg",
    "backdrop_path": "/backdrop.jpg",
    "genre_ids": [12, 18],
    "vote_average": 7.5,
}


class WatchlistSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.cache = AtomicJsonCache(
            os.path.join(self.temporary_directory.name, "watchlist.json")
        )
        try:
            self.module = importlib.import_module("yawsp_app.watchlist_snapshot")
        except ModuleNotFoundError:
            self.module = None

    def require_module(self):
        if self.module is None:
            self.fail("watchlist snapshot service is not implemented")
        return self.module

    def store(self, **kwargs):
        return self.require_module().WatchlistSnapshotStore(self.cache, **kwargs)

    def scope(self, account_id="account-1", media="movies", language="cs-CZ"):
        return self.require_module().SnapshotScope(account_id, media, language)

    def raw_disk(self):
        with open(self.cache.path, encoding="utf-8") as stream:
            return json.load(stream)

    def test_complete_round_trip_is_scoped_and_persists_no_auth_secrets(self):
        scope = self.scope()
        store = self.store()

        snapshot = store.save_complete(scope, [MOVIE], 1_700_000_000)

        self.assertEqual(snapshot.results, [MOVIE])
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(store.load(scope).results, [MOVIE])
        self.assertIsNone(store.load(self.scope(account_id="account-2")))
        self.assertIsNone(store.load(self.scope(language="en-US")))
        self.assertIsNone(store.load(self.scope(media="tv")))
        raw = json.dumps(self.raw_disk())
        self.assertIn("account-1", raw)
        self.assertIn("cs-CZ", raw)
        self.assertIn("movies", raw)
        self.assertNotIn("api-key", raw)
        self.assertNotIn("session-token", raw)
        self.assertNotIn("access_token", raw)

    def test_empty_complete_snapshot_is_valid_and_subject_to_the_same_throttle(self):
        scope = self.scope()
        store = self.store()

        snapshot = store.save_complete(scope, [], 100.0)

        self.assertEqual(snapshot.results, [])
        self.assertEqual(store.load(scope).results, [])
        self.assertFalse(store.refresh_due(scope, 399.999))
        self.assertTrue(store.refresh_due(scope, 400.0))

    def test_refresh_due_defaults_to_five_minutes_and_missing_is_due(self):
        scope = self.scope()
        store = self.store()
        store.save_complete(scope, [MOVIE], 1_000.0)

        self.assertFalse(store.refresh_due(scope, 1_299.999))
        self.assertTrue(store.refresh_due(scope, 1_300.0))
        self.assertTrue(store.refresh_due(self.scope(account_id="other"), 1_000.0))

    def test_revisions_are_monotonic_and_remove_does_not_reset_refresh_age(self):
        scope = self.scope()
        store = self.store()
        first = store.save_complete(scope, [MOVIE, dict(MOVIE, id=2)], 100.0)
        second = store.save_complete(
            scope, [MOVIE, dict(MOVIE, id=2)], 200.0
        )
        removed = store.remove(scope, 1)

        self.assertEqual((first.revision, second.revision, removed.revision), (1, 2, 3))
        self.assertEqual(removed.results, [dict(MOVIE, id=2)])
        self.assertEqual(removed.created_at, 200.0)
        self.assertTrue(store.refresh_due(scope, 500.0))
        self.assertIsNone(store.remove(scope, 999))

    def test_competing_scope_updates_invalidate_and_remove_preserve_each_other(self):
        first = self.scope(account_id="account-1")
        second = self.scope(account_id="account-2")
        store = self.store()
        store.save_complete(first, [MOVIE], 100.0)
        store.save_complete(second, [dict(MOVIE, id=2)], 100.0)

        store.remove(first, 1)
        self.assertEqual(store.load(second).results, [dict(MOVIE, id=2)])
        store.invalidate(first)

        self.assertIsNone(store.load(first))
        self.assertEqual(store.load(second).results, [dict(MOVIE, id=2)])
        persisted = self.raw_disk()
        self.assertEqual(len(persisted), 2)
        self.assertEqual(
            sorted(record["account_id"] for record in persisted.values()),
            ["account-1", "account-2"],
        )

    def test_malformed_incompatible_or_incomplete_records_are_never_loaded(self):
        scope = self.scope()
        store = self.store()
        store.save_complete(scope, [MOVIE], 100.0)
        key = next(iter(self.raw_disk()))
        mutations = (
            lambda record: record.update(complete=False),
            lambda record: record.update(schema=999),
            lambda record: record.update(created_at="now"),
            lambda record: record.update(revision=True),
            lambda record: record.update(results={}),
            lambda record: record.update(results=[dict(MOVIE, id="1")]),
            lambda record: record.update(results=[dict(MOVIE, vote_average="high")]),
            lambda record: record.update(results=[dict(MOVIE, genre_ids=[18, "12"])]),
            lambda record: record.update(results=[dict(MOVIE, release_date=2026)]),
            lambda record: record.update(results=[dict(MOVIE, vote_average=math.inf)]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = self.raw_disk()
                mutate(data[key])
                with open(self.cache.path, "w", encoding="utf-8") as stream:
                    json.dump(data, stream)
                self.assertIsNone(store.load(scope))

    def test_identity_invalidation_covers_media_and_language_without_touching_peers(self):
        store = self.store()
        scopes = [
            self.scope(media="movies", language="en-US"),
            self.scope(media="movies", language="cs-CZ"),
            self.scope(media="tv", language="en-US"),
        ]
        peer = self.scope(account_id="account-2")
        for scope in scopes + [peer]:
            store.save_complete(scope, [MOVIE], 100.0)
        self.assertTrue(
            hasattr(store, "invalidate_identity"),
            "scoped identity invalidation is not implemented",
        )

        store.invalidate_identity("account-1")

        self.assertTrue(all(store.load(scope) is None for scope in scopes))
        self.assertEqual(store.load(peer).results, [MOVIE])
        restored = store.save_complete(scopes[0], [dict(MOVIE, id=2)], 200.0)
        self.assertGreater(restored.revision, 1)

    def test_persistence_failure_does_not_replace_the_previous_complete_snapshot(self):
        scope = self.scope()
        store = self.store()
        original = store.save_complete(scope, [MOVIE], 100.0)
        previous_bytes = Path(self.cache.path).read_bytes()

        with patch.object(self.cache, "save_merged", return_value=False):
            with self.assertRaises(RuntimeError):
                store.save_complete(scope, [dict(MOVIE, id=2)], 200.0)

        self.assertEqual(Path(self.cache.path).read_bytes(), previous_bytes)
        self.assertEqual(store.load(scope).results, original.results)
        self.assertEqual(store.load(scope).revision, original.revision)

    def test_snapshot_inputs_cannot_contain_secret_scope_fields(self):
        with self.assertRaises(ValueError):
            self.scope(account_id="")
        with self.assertRaises(ValueError):
            self.scope(media="movie")
        with self.assertRaises(ValueError):
            self.scope(language="")


if __name__ == "__main__":
    unittest.main()
