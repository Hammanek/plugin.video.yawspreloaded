"""Cross-invoker watchlist snapshots, continuations, and identity fencing."""

import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import unittest
from unittest import mock

import requests

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.tmdb import TMDBClient, TMDBError, public_tmdb_client
from yawsp_app.tmdb_service_store import ServiceStateStore
from yawsp_app.tmdb_service_worker import ServiceRunner, ServiceWatchlistEngine
from yawsp_app.watchlist_snapshot import SnapshotScope, WatchlistSnapshotStore
from tests.support.http_stubs import FakeResponse, FakeSession


EPOCH = "a" * 32
CLAIM = "b" * 32
WAITER = "0123456789abcdef0123456789abcdef"
MOVIE = {
    "id": 1,
    "title": "One",
    "original_title": "One",
    "release_date": "2026-01-02",
    "overview": "Plot",
    "poster_path": None,
    "backdrop_path": None,
    "genre_ids": [],
    "vote_average": 7.0,
}
KEEP = dict(MOVIE, id=2, title="Keep")


def binding(session_id):
    return "v1:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def page(results, number=1, total=1):
    return FakeResponse(
        json_data={"page": number, "total_pages": total, "results": results}
    )


class Settings:
    def __init__(self):
        self.values = {
            "tmdb_api_key": "synthetic-api-key",
            "tmdb_access_token": "old-session",
            "tmdb_account_id": "42",
        }

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


class BlockingSession(FakeSession):
    """Hold the first request while another service domain command executes."""

    def __init__(self, responses):
        super().__init__(responses)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._blocked = False

    def request(self, method, url, **kwargs):
        if not self._blocked:
            self._blocked = True
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("blocking HTTP barrier was not released")
        return super().request(method, url, **kwargs)


class WatchlistServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.cache = AtomicJsonCache(os.path.join(self.root, "watchlist.json"))
        self.snapshots = WatchlistSnapshotStore(self.cache)
        self.settings = Settings()
        self.language = {"value": "cs-CZ"}
        self.scope = SnapshotScope("42", "movies", "cs-CZ")
        self.identity = ServiceStateStore(self.root)
        self.addCleanup(self.identity.lease.release)
        self.logger = logging.getLogger("test.service.watchlist")
        self.threads = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "watchlist test thread did not finish")

    def engine(self, session, identity=None):
        return ServiceWatchlistEngine(
            self.settings,
            self.snapshots,
            lambda: self.language["value"],
            identity or self.identity,
            session,
            lambda: False,
            self.logger,
        )

    def refresh_request(self, generation=0, force=True, allow_missing=False):
        return protocol.watchlist_request(
            self.scope,
            generation,
            force,
            allow_missing,
            binding(self.settings.get("tmdb_access_token")),
        )

    def watched_request(self):
        return protocol.watched_request(
            self.scope, 1, binding(self.settings.get("tmdb_access_token"))
        )

    def start_service(self):
        self.assertTrue(self.identity.lease.acquire())
        self.identity.recover(EPOCH)
        self.identity.set_ready(EPOCH)
        return ServiceStateStore(self.root)

    def claim(self, request):
        row = self.identity.submit([request])[0]
        self.assertEqual(row["status"], "accepted")
        claim = self.identity.claim_next(EPOCH, CLAIM)
        self.assertEqual(claim["job_id"], row["job_id"])
        return row, claim

    def manifest(self):
        with open(
            os.path.join(self.root, "tmdb_service_state", "manifest.json"),
            encoding="utf-8",
        ) as handle:
            return json.load(handle)

    def nonterminal(self):
        return [
            job
            for job in self.manifest()["jobs"]
            if job["state"] in ("queued", "backlog", "active")
        ]

    def result_document(self, job_id):
        path = os.path.join(
            self.root, "tmdb_service_state", "results", job_id + ".json"
        )
        return protocol.validate_result(Path(path).read_bytes())

    def run_async(self, operation):
        outcome = []

        def invoke():
            try:
                outcome.append(("value", operation()))
            except Exception as error:  # asserted by the calling test
                outcome.append(("error", error))

        thread = threading.Thread(target=invoke)
        self.threads.append(thread)
        thread.start()
        return outcome

    def make_runner(self, session):
        caller = self.start_service()
        session_factory = mock.patch(
            "yawsp_app.tmdb_service_worker.requests.Session", return_value=session
        )
        session_factory.start()
        self.addCleanup(session_factory.stop)

        def metadata_factory(_session):
            raise AssertionError("metadata engine cannot handle watchlist work")

        def operation_factory(_session):
            raise AssertionError("identity.invalidate belongs only to watchlist engine")

        runner = ServiceRunner(
            self.identity,
            metadata_factory,
            lambda current: self.engine(current),
            operation_factory,
            lambda: False,
            lambda _seconds: False,
            self.logger,
        )
        self.addCleanup(runner.close)
        self.assertFalse(runner.run_once())
        return runner, caller

    def test_old_refresh_cannot_overwrite_watched_generation_or_peer_scope(self):
        peer = SnapshotScope("43", "movies", "cs-CZ")
        self.snapshots.save_complete(self.scope, [MOVIE, KEEP], 100.0)
        original_peer = self.snapshots.save_complete(peer, [dict(MOVIE, id=9)], 100.0)
        self.start_service()
        row, claim = self.claim(self.refresh_request())
        old_session = BlockingSession([page([MOVIE, KEEP])])
        old_engine = self.engine(old_session)
        old_engine.bind_claim(claim)
        outcome = self.run_async(lambda: old_engine.execute(claim["request"]))
        self.assertTrue(old_session.entered.wait(5))

        watched = self.engine(FakeSession([FakeResponse(json_data={"success": True})]))
        watched_value = watched.execute(self.watched_request())
        self.assertEqual(watched_value["revision"], 2)
        self.assertEqual(watched_value["generation"], 1)
        old_session.release.set()
        self.threads[0].join(5)

        self.assertEqual(outcome[0][0], "error")
        self.assertIsInstance(outcome[0][1], TMDBError)
        current = self.snapshots.load(self.scope)
        self.assertEqual(current.results, [KEEP])
        self.assertEqual((current.revision, current.generation), (2, 1))
        self.assertEqual(self.snapshots.load(peer), original_peer)
        followups = [
            job for job in self.manifest()["jobs"]
            if job["followup_of"] == row["job_id"]
        ]
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0]["state"], "queued")
        self.assertEqual(followups[0]["request"]["generation"], 1)
        self.assertTrue(followups[0]["request"]["force"])
        self.assertEqual(self.manifest()["jobs"][0]["state"], "active")
        self.identity.abort_claim(claim, "FAILED")
        self.assertEqual(len(self.nonterminal()), 1)

    def test_successful_refresh_preserves_peer_scope_saved_during_http(self):
        peer = SnapshotScope("43", "movies", "cs-CZ")
        self.start_service()
        _row, claim = self.claim(self.refresh_request())
        session = BlockingSession([page([KEEP])])
        engine = self.engine(session)
        engine.bind_claim(claim)
        outcome = self.run_async(lambda: engine.execute(claim["request"]))
        self.assertTrue(session.entered.wait(5))

        independent_peer_store = WatchlistSnapshotStore(
            AtomicJsonCache(self.cache.path)
        )
        peer_snapshot = independent_peer_store.save_complete(peer, [MOVIE], 200.0)
        session.release.set()
        self.threads[0].join(5)

        self.assertEqual(
            outcome[0],
            (
                "value",
                {
                    "scope": dict(self.scope.__dict__),
                    "revision": 1,
                    "generation": 0,
                },
            ),
        )
        self.assertEqual(self.snapshots.load(peer), peer_snapshot)
        self.assertEqual(self.snapshots.load(self.scope).results, [KEEP])
        active = next(job for job in self.manifest()["jobs"] if job["state"] == "active")
        self.identity.abort_claim(
            {
                "job_id": active["job_id"],
                "service_epoch": active["service_epoch"],
                "claim_token": active["claim_token"],
            },
            "FAILED",
        )
        self.assertFalse(self.nonterminal())

    def test_new_force_coalesces_once_before_faulty_predecessor_terminal(self):
        self.snapshots.save_complete(self.scope, [MOVIE, KEEP], 100.0)
        session = BlockingSession([page([MOVIE, KEEP]), page([KEEP])])
        runner, caller = self.make_runner(session)
        old_row = caller.submit([self.refresh_request()])[0]
        original_complete = self.identity.complete
        checked = []
        outcome = self.run_async(runner.run_once)
        self.assertTrue(session.entered.wait(5))

        self.snapshots.remove(self.scope, 1)
        newer = caller.submit([self.refresh_request(generation=1, force=True)])[0]
        self.assertEqual(newer["status"], "accepted")
        self.assertNotEqual(newer["job_id"], old_row["job_id"])

        def faulty_complete(claim, document):
            if claim["job_id"] == old_row["job_id"] and not checked:
                continuation = next(
                    job for job in self.manifest()["jobs"]
                    if job["job_id"] == newer["job_id"]
                )
                self.assertEqual(continuation["followup_of"], old_row["job_id"])
                self.assertEqual(continuation["state"], "queued")
                checked.append(True)
                raise OSError("injected pre-terminal publication fault")
            return original_complete(claim, document)

        self.identity.complete = faulty_complete
        session.release.set()
        with self.assertLogs(self.logger, level="ERROR"):
            self.threads[0].join(5)

        self.assertEqual(outcome, [("value", True)])
        self.assertEqual(checked, [True])
        continuation = next(
            job for job in self.manifest()["jobs"] if job["job_id"] == newer["job_id"]
        )
        self.assertEqual(continuation["state"], "queued")
        self.assertEqual(continuation["request"]["generation"], 1)
        self.assertTrue(continuation["request"]["force"])
        self.assertEqual(
            len([job for job in self.manifest()["jobs"] if job["followup_of"] == old_row["job_id"]]),
            1,
        )
        self.assertEqual(len(session.calls), 1)

        self.identity.complete = original_complete
        active = next(job for job in self.manifest()["jobs"] if job["state"] == "active")
        self.identity.abort_claim(
            {
                "job_id": active["job_id"],
                "service_epoch": active["service_epoch"],
                "claim_token": active["claim_token"],
            },
            "FAILED",
        )
        self.assertTrue(runner.run_once())
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(self.nonterminal())
        self.assertEqual(self.snapshots.load(self.scope).results, [KEEP])

    def test_successful_watched_removes_latest_rows_returns_revision_and_one_followup(self):
        self.snapshots.save_complete(self.scope, [MOVIE, KEEP], 100.0)
        watched_response = FakeResponse(status_code=201, json_data={"success": True})
        refresh_response = page([KEEP])
        session = FakeSession([watched_response, refresh_response])
        runner, caller = self.make_runner(session)
        row = caller.join(self.watched_request(), WAITER)
        self.assertEqual(row["status"], "accepted")

        runner.run_once()

        result = caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(
            result["value"],
            {"scope": dict(self.scope.__dict__), "revision": 2, "generation": 1},
        )
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(
            session.calls[0]["kwargs"]["json"],
            {"media_type": "movie", "media_id": 1, "watchlist": False},
        )
        current = self.snapshots.load(self.scope)
        self.assertEqual(current.results, [KEEP])
        self.assertEqual((current.revision, current.generation), (3, 1))
        self.assertFalse(self.nonterminal())
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(watched_response.closed)
        self.assertTrue(refresh_response.closed)
        self.assertTrue(all(call["kwargs"]["timeout"] == 15 for call in session.calls))

    def test_successful_manual_refresh_publishes_without_automatic_duplicate(self):
        session = FakeSession([page([KEEP])])
        runner, caller = self.make_runner(session)
        row = caller.join(self.refresh_request(force=True), WAITER)

        runner.run_once()

        result = caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["value"]["revision"], 1)
        self.assertEqual(result["value"]["generation"], 0)
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(self.nonterminal())
        self.assertFalse(runner.run_once())
        self.assertEqual(len(session.calls), 1)

    def test_identity_and_language_changes_prevent_wrong_scope_save(self):
        original = self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        new_scope = SnapshotScope("43", "movies", "cs-CZ")
        for change in ("account", "session", "language"):
            with self.subTest(change=change, phase="before"):
                settings_before = dict(self.settings.values)
                language_before = self.language["value"]
                request = self.refresh_request()
                if change == "account":
                    self.settings.set("tmdb_account_id", "43")
                elif change == "session":
                    self.settings.set("tmdb_access_token", "new-session")
                else:
                    self.language["value"] = "en-US"
                session = FakeSession([page([KEEP])])
                with self.assertRaises(TMDBError):
                    self.engine(session).execute(request)
                self.assertEqual(session.calls, [])
                self.assertEqual(self.snapshots.load(self.scope), original)
                self.assertIsNone(self.snapshots.load(new_scope))
                self.settings.values = settings_before
                self.language["value"] = language_before

        class ChangeDuringResponse(FakeResponse):
            def __init__(self, change):
                super().__init__(json_data={})
                self.change = change

            def json(inner):
                if inner.change == "account":
                    self.settings.set("tmdb_account_id", "43")
                elif inner.change == "session":
                    self.settings.set("tmdb_access_token", "new-session")
                else:
                    self.language["value"] = "en-US"
                return {"page": 1, "total_pages": 1, "results": [KEEP]}

        for change in ("account", "session", "language"):
            with self.subTest(change=change, phase="during"):
                self.settings.set("tmdb_account_id", "42")
                self.settings.set("tmdb_access_token", "old-session")
                self.language["value"] = "cs-CZ"
                session = FakeSession([ChangeDuringResponse(change)])
                with self.assertRaises(TMDBError):
                    self.engine(session).execute(self.refresh_request())
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(self.snapshots.load(self.scope), original)
                self.assertIsNone(self.snapshots.load(new_scope))

    def test_missing_account_resolution_is_bound_to_explicit_scope(self):
        self.settings.set("tmdb_account_id", "")
        session = FakeSession(
            [FakeResponse(json_data={"id": 42}), page([MOVIE])]
        )

        value = self.engine(session).execute(
            self.refresh_request(allow_missing=True)
        )

        self.assertEqual(value["scope"]["account_id"], "42")
        self.assertEqual(self.settings.get("tmdb_account_id"), "42")
        self.assertEqual(session.calls[0]["url"].rsplit("/", 1)[-1], "account")
        self.assertEqual(
            session.calls[1]["url"].split("?")[0],
            "https://api.themoviedb.org/3/account/42/watchlist/movies",
        )
        self.assertEqual(session.calls[1]["kwargs"]["params"]["session_id"], "old-session")
        self.assertEqual(self.snapshots.load(self.scope).results, [MOVIE])

        self.settings.set("tmdb_account_id", "")
        mismatch = FakeSession([FakeResponse(json_data={"id": 43}), page([KEEP])])
        self.snapshots.invalidate(self.scope)
        with self.assertRaises(TMDBError):
            self.engine(mismatch).execute(self.refresh_request(allow_missing=True))
        self.assertEqual(self.settings.get("tmdb_account_id"), "")
        self.assertEqual(len(mismatch.calls), 1)

    def test_delayed_old_401_cannot_clear_qr_replacement_or_new_scope(self):
        old = self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        new_scope = SnapshotScope("43", "movies", "cs-CZ")
        new = self.snapshots.save_complete(new_scope, [KEEP], 100.0)
        self.start_service()
        request = self.refresh_request()
        _row, claim = self.claim(request)
        unauthorized = FakeResponse(json_data={}, status_code=401)
        session = BlockingSession([unauthorized])
        engine = self.engine(session)
        engine.bind_claim(claim)
        outcome = self.run_async(lambda: engine.execute(request))
        self.assertTrue(session.entered.wait(5))

        with ServiceStateStore(self.root).identity_transaction():
            self.settings.set("tmdb_access_token", "qr-session")
            self.settings.set("tmdb_account_id", "43")
        session.release.set()
        self.threads[0].join(5)

        self.assertEqual(outcome[0][0], "error")
        self.assertEqual(self.settings.get("tmdb_access_token"), "qr-session")
        self.assertEqual(self.settings.get("tmdb_account_id"), "43")
        self.assertEqual(self.snapshots.load(self.scope), old)
        self.assertEqual(self.snapshots.load(new_scope), new)
        self.assertTrue(unauthorized.closed)

    def test_public_401_preserves_personal_identity(self):
        self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        previous = Path(self.cache.path).read_bytes()
        session = FakeSession([FakeResponse(json_data={}, status_code=401)])
        client = public_tmdb_client(
            TMDBClient(
                session,
                self.settings,
                lambda: self.language["value"],
                identity_lock=self.identity.identity_transaction,
            )
        )

        with self.assertRaises(TMDBError):
            client.get("/3/discover/movie")

        self.assertEqual(self.settings.get("tmdb_access_token"), "old-session")
        self.assertEqual(self.settings.get("tmdb_account_id"), "42")
        self.assertEqual(Path(self.cache.path).read_bytes(), previous)

    def test_identity_invalidate_tombstones_only_matching_bound_account(self):
        scopes = [
            self.scope,
            SnapshotScope("42", "tv", "cs-CZ"),
            SnapshotScope("42", "movies", "en-US"),
        ]
        peer = SnapshotScope("43", "movies", "cs-CZ")
        for scope in scopes + [peer]:
            self.snapshots.save_complete(scope, [MOVIE], 100.0)
        request = protocol.invalidate_identity_request("42", binding("old-session"))

        value = self.engine(FakeSession()).execute(request)

        self.assertEqual(value, {"invalidated": True})
        self.assertTrue(all(self.snapshots.load(scope) is None for scope in scopes))
        self.assertIsNotNone(self.snapshots.load(peer))
        self.settings.set("tmdb_access_token", "new-session")
        self.settings.set("tmdb_account_id", "43")
        self.assertEqual(
            self.engine(FakeSession()).execute(request), {"invalidated": False}
        )
        self.assertIsNotNone(self.snapshots.load(peer))
        self.assertEqual(self.settings.get("tmdb_access_token"), "new-session")
        self.assertEqual(self.settings.get("tmdb_account_id"), "43")

    def test_failed_persistence_preserves_exact_bytes_and_publishes_no_followup(self):
        self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        previous = Path(self.cache.path).read_bytes()
        session = FakeSession([page([KEEP])])
        runner, caller = self.make_runner(session)
        row = caller.join(self.refresh_request(), WAITER)

        with mock.patch.object(self.cache, "save", return_value=False):
            with self.assertLogs(self.logger, level="ERROR"):
                runner.run_once()

        result = caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(Path(self.cache.path).read_bytes(), previous)
        self.assertEqual(self.snapshots.load(self.scope).results, [MOVIE])
        self.assertFalse(self.nonterminal())
        self.assertEqual(len(session.calls), 1)

    def assert_failed_operation_preserves_snapshot(self, session, request):
        original = self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        previous = Path(self.cache.path).read_bytes()
        runner, caller = self.make_runner(session)
        row = caller.join(request, WAITER)

        with self.assertLogs(self.logger, level="ERROR"):
            runner.run_once()

        result = caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(self.snapshots.load(self.scope), original)
        self.assertEqual(Path(self.cache.path).read_bytes(), previous)
        self.assertFalse(self.nonterminal())
        self.assertEqual(len(session.calls), 1)

    def test_failed_watched_creates_no_snapshot_or_refresh_success(self):
        self.assert_failed_operation_preserves_snapshot(
            FakeSession([FakeResponse(json_data={"success": False})]),
            self.watched_request(),
        )

    def test_failed_refresh_creates_no_snapshot_or_refresh_success(self):
        self.assert_failed_operation_preserves_snapshot(
            FakeSession([requests.Timeout("offline")]), self.refresh_request()
        )


    def start_qr_replacement_save(self, attempted, finished, results):
        def replace():
            attempted.set()
            with ServiceStateStore(self.root).identity_transaction():
                generation = self.snapshots.generation(self.scope)
                self.settings.set("tmdb_access_token", "qr-session")
                self.settings.set("tmdb_account_id", "42")
                saved = self.snapshots.save_complete(
                    self.scope,
                    results,
                    300.0,
                    expected_generation=generation,
                )
                self.assertIsNotNone(saved)
            finished.set()
            return saved

        return self.run_async(replace)

    def test_identity_tombstone_generation_conflict_has_no_automatic_refresh(self):
        self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        self.start_service()
        row, claim = self.claim(self.refresh_request())
        session = BlockingSession([page([KEEP])])
        engine = self.engine(session)
        engine.bind_claim(claim)
        outcome = self.run_async(lambda: engine.execute(claim["request"]))
        self.assertTrue(session.entered.wait(5))

        invalidated = self.engine(FakeSession()).execute(
            protocol.invalidate_identity_request("42", binding("old-session"))
        )
        self.assertEqual(invalidated, {"invalidated": True})
        session.release.set()
        self.threads[0].join(5)

        self.assertEqual(outcome[0][0], "error")
        self.assertIsNone(self.snapshots.load(self.scope))
        self.assertEqual(
            [job for job in self.manifest()["jobs"] if job["followup_of"] == row["job_id"]],
            [],
        )
        self.identity.abort_claim(claim, "FAILED")
        self.assertFalse(self.nonterminal())
        with ServiceStateStore(self.root).identity_transaction():
            self.settings.set("identity_probe", "released")
        self.assertEqual(self.settings.get("identity_probe"), "released")

    def test_qr_same_account_cannot_be_overwritten_by_validated_old_refresh(self):
        self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        session = FakeSession([page([KEEP])])
        engine = self.engine(session)
        original_save = self.snapshots.save_complete
        save_entered = threading.Event()
        release_save = threading.Event()
        replacement_attempted = threading.Event()
        replacement_finished = threading.Event()
        racing = False

        def save_with_barrier(scope, results, created_at, **kwargs):
            nonlocal racing
            if racing:
                return original_save(scope, results, created_at, **kwargs)
            racing = True
            save_entered.set()
            self.assertTrue(release_save.wait(5), "old save barrier")
            return original_save(scope, results, created_at, **kwargs)

        self.snapshots.save_complete = save_with_barrier
        outcome = self.run_async(lambda: engine.execute(self.refresh_request()))
        self.assertTrue(save_entered.wait(5))
        replacement = self.start_qr_replacement_save(
            replacement_attempted,
            replacement_finished,
            [dict(MOVIE, id=7, title="QR refresh")],
        )
        self.assertTrue(replacement_attempted.wait(5))
        replacement_finished.wait(1)
        release_save.set()
        self.threads[0].join(5)
        self.threads[1].join(5)

        self.assertEqual(outcome[0][0], "value")
        self.assertEqual(replacement[0][0], "value")
        current = self.snapshots.load(self.scope)
        self.assertEqual(current.results, [dict(MOVIE, id=7, title="QR refresh")])
        self.assertEqual(self.settings.get("tmdb_access_token"), "qr-session")

    def test_qr_same_account_cannot_precede_old_watched_removal(self):
        self.snapshots.save_complete(self.scope, [MOVIE, KEEP], 100.0)
        original_remove = self.snapshots.remove
        remove_entered = threading.Event()
        release_remove = threading.Event()
        replacement_attempted = threading.Event()
        replacement_finished = threading.Event()
        racing = False
        qr_rows = [
            dict(MOVIE, id=1, title="QR one"),
            dict(KEEP, id=2, title="QR two"),
        ]

        def remove_with_barrier(scope, media_id):
            nonlocal racing
            if racing:
                return original_remove(scope, media_id)
            racing = True
            remove_entered.set()
            self.assertTrue(release_remove.wait(5), "watched remove barrier")
            return original_remove(scope, media_id)

        self.snapshots.remove = remove_with_barrier
        session = FakeSession([FakeResponse(json_data={"success": True})])
        outcome = self.run_async(
            lambda: self.engine(session).execute(self.watched_request())
        )
        self.assertTrue(remove_entered.wait(5))
        replacement = self.start_qr_replacement_save(
            replacement_attempted, replacement_finished, qr_rows
        )
        self.assertTrue(replacement_attempted.wait(5))
        replacement_finished.wait(1)
        release_remove.set()
        self.threads[0].join(5)
        self.threads[1].join(5)

        self.assertEqual(outcome[0][0], "value")
        self.assertEqual(replacement[0][0], "value")
        self.assertEqual(self.snapshots.load(self.scope).results, qr_rows)
        self.assertEqual(
            outcome[0][1],
            {"scope": dict(self.scope.__dict__), "revision": 2, "generation": 1},
        )

    def test_delayed_invalidate_cannot_tombstone_same_account_qr_snapshot(self):
        self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        original_invalidate = self.snapshots.invalidate_identity
        invalidate_entered = threading.Event()
        release_invalidate = threading.Event()
        replacement_attempted = threading.Event()
        replacement_finished = threading.Event()
        racing = False

        def invalidate_with_barrier(account_id):
            nonlocal racing
            if racing:
                return original_invalidate(account_id)
            racing = True
            invalidate_entered.set()
            self.assertTrue(release_invalidate.wait(5), "identity invalidation barrier")
            return original_invalidate(account_id)

        self.snapshots.invalidate_identity = invalidate_with_barrier
        request = protocol.invalidate_identity_request("42", binding("old-session"))
        outcome = self.run_async(lambda: self.engine(FakeSession()).execute(request))
        self.assertTrue(invalidate_entered.wait(5))
        replacement = self.start_qr_replacement_save(
            replacement_attempted,
            replacement_finished,
            [dict(MOVIE, id=7, title="QR invalidation")],
        )
        self.assertTrue(replacement_attempted.wait(5))
        replacement_finished.wait(1)
        release_invalidate.set()
        self.threads[0].join(5)
        self.threads[1].join(5)

        self.assertEqual(outcome, [("value", {"invalidated": True})])
        self.assertEqual(replacement[0][0], "value")
        current = self.snapshots.load(self.scope)
        self.assertEqual(current.results, [dict(MOVIE, id=7, title="QR invalidation")])
        self.assertEqual((current.revision, current.generation), (3, 1))

    def test_delayed_old_401_cannot_invalidate_same_account_qr_snapshot(self):
        self.snapshots.save_complete(self.scope, [MOVIE], 100.0)
        original_invalidate = self.snapshots.invalidate_identity
        invalidate_entered = threading.Event()
        release_invalidate = threading.Event()
        replacement_attempted = threading.Event()
        replacement_finished = threading.Event()
        racing = False

        def invalidate_with_barrier(account_id):
            nonlocal racing
            if racing:
                return original_invalidate(account_id)
            racing = True
            invalidate_entered.set()
            self.assertTrue(release_invalidate.wait(5), "401 invalidation barrier")
            return original_invalidate(account_id)

        self.snapshots.invalidate_identity = invalidate_with_barrier
        session = FakeSession([FakeResponse(json_data={}, status_code=401)])
        outcome = self.run_async(
            lambda: self.engine(session).execute(self.refresh_request())
        )
        self.assertTrue(invalidate_entered.wait(5))
        replacement = self.start_qr_replacement_save(
            replacement_attempted,
            replacement_finished,
            [dict(MOVIE, id=7, title="QR after 401")],
        )
        self.assertTrue(replacement_attempted.wait(5))
        replacement_finished.wait(1)
        release_invalidate.set()
        self.threads[0].join(5)
        self.threads[1].join(5)

        self.assertEqual(outcome[0][0], "error")
        self.assertEqual(replacement[0][0], "value")
        current = self.snapshots.load(self.scope)
        self.assertEqual(current.results, [dict(MOVIE, id=7, title="QR after 401")])
        self.assertEqual((current.revision, current.generation), (3, 1))
        self.assertEqual(self.settings.get("tmdb_access_token"), "qr-session")

    def test_two_watched_results_keep_revision_and_generation_from_one_commit(self):
        self.snapshots.save_complete(
            self.scope, [MOVIE, KEEP, dict(MOVIE, id=3)], 100.0
        )
        original_generation = self.snapshots.generation
        generation_read = threading.Event()
        release_generation = threading.Event()
        first_read = True

        def generation_with_barrier(scope):
            nonlocal first_read
            value = original_generation(scope)
            if first_read:
                first_read = False
                generation_read.set()
                self.assertTrue(release_generation.wait(5), "generation/load gap")
            return value

        self.snapshots.generation = generation_with_barrier
        first = self.run_async(
            lambda: self.engine(
                FakeSession([FakeResponse(json_data={"success": True})])
            ).execute(self.watched_request())
        )
        interleaved = generation_read.wait(1)
        if not interleaved:
            self.threads[0].join(5)
        second = self.engine(
            FakeSession([FakeResponse(json_data={"success": True})])
        ).execute(protocol.watched_request(self.scope, 2, binding("old-session")))
        if interleaved:
            release_generation.set()
            self.threads[0].join(5)

        self.assertEqual(first[0][0], "value")
        self.assertEqual(
            first[0][1],
            {"scope": dict(self.scope.__dict__), "revision": 2, "generation": 1},
        )
        self.assertEqual(
            second,
            {"scope": dict(self.scope.__dict__), "revision": 3, "generation": 2},
        )
        self.assertEqual(
            [row["id"] for row in self.snapshots.load(self.scope).results], [3]
        )

    def test_late_generation_read_coalesces_with_newer_explicit_refresh(self):
        self.snapshots.save_complete(self.scope, [MOVIE, KEEP], 100.0)
        caller = self.start_service()
        row, claim = self.claim(self.refresh_request())
        session = BlockingSession([page([MOVIE])])
        engine = self.engine(session)
        engine.bind_claim(claim)
        original_generation = self.snapshots.generation
        generation_read = threading.Event()
        release_generation = threading.Event()
        first_read = True

        def generation_with_barrier(scope):
            nonlocal first_read
            value = original_generation(scope)
            if first_read:
                first_read = False
                generation_read.set()
                self.assertTrue(release_generation.wait(5), "late generation barrier")
            return value

        self.snapshots.generation = generation_with_barrier
        outcome = self.run_async(lambda: engine.execute(claim["request"]))
        self.assertTrue(session.entered.wait(5))
        self.snapshots.remove(self.scope, 1)
        explicit = caller.submit([self.refresh_request(generation=1)])[0]
        self.assertEqual(explicit["status"], "accepted")
        session.release.set()

        interleaved = generation_read.wait(1)
        if interleaved:
            with ServiceStateStore(self.root).identity_transaction():
                self.snapshots.remove(self.scope, 2)
                newer = caller.submit([self.refresh_request(generation=2)])[0]
                self.assertEqual(newer["status"], "accepted")
            release_generation.set()
        self.threads[0].join(5)

        self.assertEqual(outcome[0][0], "error")
        expected_generation = 2 if interleaved else 1
        refresh_jobs = [
            job for job in self.manifest()["jobs"]
            if job["state"] == "queued"
            and job["request"]["operation"] == "watchlist.refresh"
        ]
        self.assertEqual(len(refresh_jobs), 1)
        self.assertEqual(refresh_jobs[0]["request"]["generation"], expected_generation)
        self.assertEqual(refresh_jobs[0]["followup_of"], row["job_id"])
        self.assertTrue(refresh_jobs[0]["request"]["force"])
        self.identity.abort_claim(claim, "FAILED")

    def test_watched_business_failure_without_complete_snapshot_has_no_followup(self):
        self.start_service()
        row, claim = self.claim(self.watched_request())
        engine = self.engine(FakeSession([FakeResponse(json_data={"success": True})]))
        engine.bind_claim(claim)

        with self.assertRaises(TMDBError):
            engine.execute(claim["request"])

        self.assertIsNone(self.snapshots.load(self.scope))
        self.assertEqual(
            [job for job in self.manifest()["jobs"]
             if job["followup_of"] == row["job_id"]],
            [],
        )
        self.identity.abort_claim(claim, "FAILED")
        self.assertFalse(self.nonterminal())


if __name__ == "__main__":
    unittest.main()
