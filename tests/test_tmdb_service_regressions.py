"""Whole-branch regressions for historical TMDB ownership and lifecycle faults.

The older focused suites own the individual protocol/store/client contracts.  This
module covers only the cross-module schedules and direct result assertions that
were still missing from those suites.  All state, HTTP and identity data are
synthetic and local.
"""

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.test_tmdb_service_worker import Clock, DynamicMetadataSession
from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.tmdb_service_store import ServiceStateStore
from yawsp_app.tmdb_service_worker import (
    ServiceMetadataEngine,
    ServiceRunner,
    ServiceWatchlistEngine,
)
from yawsp_app.watchlist_snapshot import SnapshotScope, WatchlistSnapshotStore


WAITER = "0123456789abcdef0123456789abcdef"
OTHER_WAITER = "1123456789abcdef0123456789abcdef"
EPOCH = "a" * 32
CLAIM = "b" * 32


class Settings:
    def __init__(self):
        self.values = {
            "tmdb_api_key": "synthetic-regression-api-key",
            "tmdb_access_token": "synthetic-regression-session",
            "tmdb_account_id": "42",
        }

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


def binding(session_id):
    return "v1:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def page(rows):
    return FakeResponse(
        json_data={"page": 1, "total_pages": 1, "results": rows}
    )


def movie(media_id, title):
    return {
        "id": media_id,
        "title": title,
        "original_title": title,
        "release_date": "2026-01-02",
        "overview": "Synthetic plot",
        "poster_path": None,
        "backdrop_path": None,
        "genre_ids": [],
        "vote_average": 7.0,
    }


class BlockingSession(FakeSession):
    """Hold one real service HTTP call without holding an identity lock."""

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
                raise AssertionError("HTTP barrier was not released")
        return super().request(method, url, **kwargs)


class HistoricalServiceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.logger = logging.getLogger("test.service.regressions")
        self.settings = Settings()
        self.cache = AtomicJsonCache(os.path.join(self.root, "watchlist.json"))
        self.snapshots = WatchlistSnapshotStore(self.cache)
        self.scope = SnapshotScope("42", "movies", "cs-CZ")
        self.identity = ServiceStateStore(self.root)
        self.addCleanup(self.identity.lease.release)
        self.threads = []
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "regression thread did not finish")

    def barrier_event(self):
        event = threading.Event()
        self.release_events.append(event)
        return event

    def run_async(self, operation):
        outcome = []

        def invoke():
            try:
                outcome.append(("value", operation()))
            except BaseException as error:  # asserted by the calling regression
                outcome.append(("error", error))

        thread = threading.Thread(target=invoke)
        self.threads.append(thread)
        thread.start()
        return outcome

    def manifest(self):
        with open(
            os.path.join(self.root, "tmdb_service_state", "manifest.json"),
            encoding="utf-8",
        ) as handle:
            return json.load(handle)

    def result_document(self, job_id):
        path = os.path.join(
            self.root, "tmdb_service_state", "results", job_id + ".json"
        )
        return protocol.validate_result(Path(path).read_bytes())

    def nonterminal(self):
        return [
            job
            for job in self.manifest()["jobs"]
            if job["state"] in ("queued", "backlog", "active")
        ]

    def refresh_request(self, generation=0, force=True):
        return protocol.watchlist_request(
            self.scope,
            generation,
            force,
            False,
            binding(self.settings.get("tmdb_access_token")),
        )

    def watched_request(self, media_id):
        return protocol.watched_request(
            self.scope,
            media_id,
            binding(self.settings.get("tmdb_access_token")),
        )

    def watchlist_engine(self, session, identity=None, snapshots=None):
        return ServiceWatchlistEngine(
            self.settings,
            snapshots or self.snapshots,
            lambda: "cs-CZ",
            identity or self.identity,
            session,
            lambda: False,
            self.logger,
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

    def make_metadata_runner(self, sessions, wait_for_abort=None):
        caller = self.start_service()
        session_factory = mock.patch(
            "yawsp_app.tmdb_service_worker.requests.Session",
            side_effect=sessions,
        )
        session_factory.start()
        self.addCleanup(session_factory.stop)
        clock = Clock()

        def metadata_factory(session):
            return ServiceMetadataEngine(
                self.settings,
                AtomicJsonCache(os.path.join(self.root, "trailers.json")),
                AtomicJsonCache(os.path.join(self.root, "shows.json")),
                lambda: "en-US",
                session,
                lambda: False,
                self.logger,
                clock=clock,
                sleeper=clock.sleep,
            )

        def unsupported_factory(_session):
            raise AssertionError("metadata regression used another operation family")

        runner = ServiceRunner(
            self.identity,
            metadata_factory,
            unsupported_factory,
            unsupported_factory,
            lambda: False,
            wait_for_abort or (lambda _seconds: False),
            self.logger,
        )
        self.addCleanup(runner.close)
        self.assertFalse(runner.run_once())
        return runner, caller

    def make_watchlist_runner(self, session):
        caller = self.start_service()
        session_factory = mock.patch(
            "yawsp_app.tmdb_service_worker.requests.Session", return_value=session
        )
        session_factory.start()
        self.addCleanup(session_factory.stop)

        def unsupported_factory(_session):
            raise AssertionError("watchlist regression used another operation family")

        runner = ServiceRunner(
            self.identity,
            unsupported_factory,
            lambda current: self.watchlist_engine(current),
            unsupported_factory,
            lambda: False,
            lambda _seconds: False,
            self.logger,
        )
        self.addCleanup(runner.close)
        self.assertFalse(runner.run_once())
        return runner, caller

    def test_actual_system_exit_closes_response_session_and_preserves_next_job(self):
        class ExitResponse(FakeResponse):
            def iter_content(self, *args, **kwargs):
                raise SystemExit(0)

        first_response = ExitResponse()
        first_session = FakeSession([first_response])
        second_response = FakeResponse(json_data={"next_episode_to_air": None})
        second_session = FakeSession([second_response])
        runner, caller = self.make_metadata_runner([first_session, second_session])
        rows = caller.submit([protocol.show_request(1), protocol.show_request(2)])
        self.assertTrue(all(row["status"] == "accepted" for row in rows))

        with self.assertLogs(self.logger, level="ERROR"):
            with self.assertRaises(SystemExit):
                runner.run_once()

        first = self.result_document(rows[0]["job_id"])
        self.assertEqual(first["outcome"], "failed")
        self.assertEqual(first["error"]["code"], "FAILED")
        self.assertTrue(first_response.closed)
        self.assertTrue(first_session.closed)
        self.assertIsNone(caller.read_wait_result(rows[1]["job_id"], "unregistered"))

        self.assertTrue(runner.run_once())
        second = self.result_document(rows[1]["job_id"])
        self.assertEqual(second["outcome"], "succeeded")
        self.assertEqual(second["value"], {"value": None, "succeeded": True})
        self.assertTrue(second_response.closed)
        self.assertTrue(second_session.closed)
        self.assertIsNot(first_session, second_session)
        self.assertFalse(self.nonterminal())

    def test_background_retry_shared_join_precedes_independent_interactive(self):
        shared_request = protocol.show_request(42)
        independent_request = protocol.show_request(43)
        shared_url = "https://api.themoviedb.org/3/tv/42"
        session = DynamicMetadataSession(failures={shared_url: 1})
        joined = {}
        waits = []

        def during_retry(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                joined["shared00000"] = caller.join(shared_request, WAITER)
                joined["independent"] = caller.join(
                    independent_request, OTHER_WAITER
                )
                active = [job for job in self.manifest()["jobs"] if job["state"] == "active"]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["job_id"], joined["shared00000"]["job_id"])
                self.assertEqual(active[0]["waiters"], [WAITER])
                self.assertTrue(active[0]["priority"])
            return False

        runner, caller = self.make_metadata_runner([session], during_retry)
        background = caller.submit([shared_request])[0]
        self.assertEqual(background["status"], "accepted")

        with self.assertLogs(self.logger, level="ERROR"):
            self.assertTrue(runner.run_once())

        self.assertEqual(joined["shared00000"]["status"], "joined")
        self.assertEqual(joined["shared00000"]["job_id"], background["job_id"])
        self.assertEqual(joined["independent"]["status"], "accepted")
        self.assertNotEqual(joined["independent"]["job_id"], background["job_id"])
        self.assertEqual(waits, [0.05])
        self.assertEqual(session.attempts[shared_url], 2)
        self.assertEqual(
            [call["url"] for call in session.calls],
            [shared_url, shared_url, "https://api.themoviedb.org/3/tv/43"],
        )
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(len({call["url"] for call in session.calls}), 2)
        self.assertTrue(all(response.closed for response in session.responses))
        for row, waiter in (
            (joined["shared00000"], WAITER),
            (joined["independent"], OTHER_WAITER),
        ):
            result = caller.read_wait_result(row["job_id"], waiter)
            self.assertEqual(result["outcome"], "succeeded")
            self.assertEqual(result["value"], {"value": None, "succeeded": True})
        self.assertFalse(self.nonterminal())

    def test_two_watched_race_keeps_each_coherent_commit_triple(self):
        rows = [movie(1, "One"), movie(2, "Two"), movie(3, "Three")]
        self.snapshots.save_complete(self.scope, rows, 100.0)
        other_identity = ServiceStateStore(self.root)
        other_snapshots = WatchlistSnapshotStore(
            AtomicJsonCache(self.cache.path)
        )
        remove_entered = threading.Event()
        release_remove = self.barrier_event()
        other_attempted = threading.Event()
        original_remove = self.snapshots.remove
        first_remove = True

        def remove_with_barrier(scope, media_id):
            nonlocal first_remove
            if first_remove:
                first_remove = False
                remove_entered.set()
                self.assertTrue(release_remove.wait(5), "first watched barrier")
            return original_remove(scope, media_id)

        self.snapshots.remove = remove_with_barrier
        first = self.run_async(
            lambda: self.watchlist_engine(
                FakeSession([FakeResponse(json_data={"success": True})])
            ).execute(self.watched_request(1))
        )
        self.assertTrue(remove_entered.wait(5))

        original_transaction = other_identity.identity_transaction

        @contextlib.contextmanager
        def attempted_transaction():
            other_attempted.set()
            with original_transaction():
                yield

        other_identity.identity_transaction = attempted_transaction
        second = self.run_async(
            lambda: self.watchlist_engine(
                FakeSession([FakeResponse(json_data={"success": True})]),
                identity=other_identity,
                snapshots=other_snapshots,
            ).execute(self.watched_request(2))
        )
        self.assertTrue(other_attempted.wait(5))
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        release_remove.set()
        self.threads[0].join(5)
        self.threads[1].join(5)

        expected_scope = dict(self.scope.__dict__)
        self.assertEqual(
            first,
            [("value", {"scope": expected_scope, "revision": 2, "generation": 1})],
        )
        self.assertEqual(
            second,
            [("value", {"scope": expected_scope, "revision": 3, "generation": 2})],
        )
        current = other_snapshots.load(self.scope)
        self.assertEqual([row["id"] for row in current.results], [3])
        self.assertEqual((current.revision, current.generation), (3, 2))

    def test_late_state_read_serializes_before_newer_generation_continuation(self):
        rows = [movie(1, "One"), movie(2, "Two"), movie(3, "Three")]
        self.snapshots.save_complete(self.scope, rows, 100.0)
        caller = self.start_service()
        old_row, old_claim = self.claim(self.refresh_request())
        session = BlockingSession([page([movie(1, "Old response")])])
        engine = self.watchlist_engine(session)
        engine.bind_claim(old_claim)
        original_refresh_state = self.snapshots.refresh_state
        state_read = threading.Event()
        release_state = self.barrier_event()
        first_state_read = True

        def state_with_barrier(scope, now):
            nonlocal first_state_read
            if first_state_read:
                first_state_read = False
                state_read.set()
                self.assertTrue(release_state.wait(5), "late state barrier")
            return original_refresh_state(scope, now)

        self.snapshots.refresh_state = state_with_barrier
        old = self.run_async(lambda: engine.execute(old_claim["request"]))
        self.assertTrue(session.entered.wait(5))

        other_identity = ServiceStateStore(self.root)
        with other_identity.identity_transaction():
            removed = self.snapshots.remove(self.scope, 1)
            self.assertEqual((removed.revision, removed.generation), (2, 1))
        explicit = caller.submit([self.refresh_request(generation=1)])[0]
        self.assertEqual(explicit["status"], "accepted")
        self.assertNotEqual(explicit["job_id"], old_row["job_id"])
        session.release.set()
        self.assertTrue(state_read.wait(5))

        other_snapshots = WatchlistSnapshotStore(
            AtomicJsonCache(self.cache.path)
        )
        newer_attempted = threading.Event()

        def newer_mutation():
            newer_attempted.set()
            with other_identity.identity_transaction():
                return other_snapshots.remove(self.scope, 2)

        newer = self.run_async(newer_mutation)
        self.assertTrue(newer_attempted.wait(5))
        self.assertEqual(old, [])
        self.assertEqual(newer, [])
        release_state.set()
        self.threads[0].join(5)
        self.threads[1].join(5)

        self.assertEqual(old[0][0], "error")
        self.assertEqual(newer[0][0], "value")
        self.assertEqual(
            (newer[0][1].revision, newer[0][1].generation), (3, 2)
        )
        upgraded = self.identity.ensure_followup(
            old_claim, self.refresh_request(generation=2)
        )
        self.assertEqual(upgraded["job_id"], explicit["job_id"])
        joined = caller.submit([self.refresh_request(generation=2)])[0]
        self.assertEqual(joined["status"], "joined")
        self.assertEqual(joined["job_id"], explicit["job_id"])
        continuations = [
            job
            for job in self.nonterminal()
            if job["followup_of"] == old_row["job_id"]
        ]
        self.assertEqual(len(continuations), 1)
        self.assertEqual(continuations[0]["request"]["generation"], 2)
        self.assertTrue(continuations[0]["request"]["force"])
        self.identity.abort_claim(old_claim, "FAILED")
        self.assertEqual(self.nonterminal(), continuations)

    def test_already_absent_watched_id_returns_new_commit_and_one_forced_refresh(self):
        self.snapshots.save_complete(self.scope, [movie(2, "Keep")], 100.0)
        watched_response = FakeResponse(status_code=201, json_data={"success": True})
        refresh_response = page([movie(2, "Keep")])
        session = FakeSession([watched_response, refresh_response])
        runner, caller = self.make_watchlist_runner(session)
        row = caller.join(self.watched_request(1), WAITER)
        self.assertEqual(row["status"], "accepted")
        original_complete = self.identity.complete
        checked = []

        def check_durable_before_watched_terminal(claim, document):
            if claim["job_id"] == row["job_id"] and not checked:
                continuations = [
                    job
                    for job in self.manifest()["jobs"]
                    if job["followup_of"] == row["job_id"]
                ]
                self.assertEqual(len(continuations), 1)
                self.assertEqual(continuations[0]["state"], "queued")
                self.assertEqual(continuations[0]["request"]["generation"], 1)
                self.assertTrue(continuations[0]["request"]["force"])
                self.assertEqual(
                    document["value"],
                    {"scope": dict(self.scope.__dict__), "revision": 2, "generation": 1},
                )
                checked.append(True)
            return original_complete(claim, document)

        self.identity.complete = check_durable_before_watched_terminal
        self.assertTrue(runner.run_once())

        self.assertEqual(checked, [True])
        result = caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(
            result["value"],
            {"scope": dict(self.scope.__dict__), "revision": 2, "generation": 1},
        )
        self.assertNotEqual(result["value"]["revision"], 1)
        self.assertNotEqual(result["value"]["generation"], 0)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[1]["method"], "GET")
        self.assertTrue(watched_response.closed)
        self.assertTrue(refresh_response.closed)
        current = self.snapshots.load(self.scope)
        self.assertEqual([item["id"] for item in current.results], [2])
        self.assertEqual((current.revision, current.generation), (3, 1))
        self.assertFalse(self.nonterminal())


if __name__ == "__main__":
    unittest.main()
