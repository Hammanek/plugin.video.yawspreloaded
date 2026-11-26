"""Read-only parent adapter, waiter lifecycle and typed service outcomes."""

import copy
import itertools
import json
import os
import tempfile
import threading
import time
import unittest

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.tmdb import (
    ShowRepository,
    TMDBAbortError,
    TMDBError,
    TMDBServiceBackpressuredError,
    TMDBServiceResultExpiredError,
    TMDBServiceShuttingDownError,
    TMDBServiceUnavailableError,
    TrailerLookup,
    TrailerRepository,
)
from yawsp_app.tmdb_service_client import TMDBServiceClient
from yawsp_app.tmdb_service_store import ServiceStateStore


JOB_ID = "a" * 32
WAITER_ID = "b" * 32
EPOCH = "c" * 32
CLAIM = "d" * 32
SESSION_BINDING = "v1:" + "e" * 64
SCOPE = {
    "account_id": "42",
    "media": "movies",
    "language": "cs-CZ",
    "schema": 1,
}


class Settings:
    def __init__(self, **values):
        self.values = {
            "tmdb_access_token": "synthetic-secret-session",
            "tmdb_account_id": "42",
        }
        self.values.update(values)

    def get(self, key):
        return self.values.get(key, "")


class ScriptedStore:
    def __init__(self, request, row=None, result=None):
        self.request = request
        self.row = row or {
            "status": "accepted",
            "job_id": JOB_ID,
            "request": copy.deepcopy(request),
            "reservation": True,
        }
        self.result = result
        self.calls = []
        self.submit_rows = None
        self.acknowledge_error = None
        self.join_error = None

    def join(self, request, waiter_id):
        self.calls.append(("join", request, waiter_id))
        if self.join_error is not None:
            raise self.join_error
        return copy.deepcopy(self.row)

    def submit(self, requests, interactive=False, waiter_id=None):
        self.calls.append(("submit", requests, interactive, waiter_id))
        if self.submit_rows is not None:
            return copy.deepcopy(self.submit_rows)
        return [copy.deepcopy(self.row) for _request in requests]

    def read_wait_result(self, job_id, waiter_id):
        self.calls.append(("read", job_id, waiter_id))
        return copy.deepcopy(self.result)

    def acknowledge(self, job_id, waiter_id):
        self.calls.append(("acknowledge", job_id, waiter_id))
        if self.acknowledge_error is not None:
            raise self.acknowledge_error


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.trailer_cache = AtomicJsonCache(
            os.path.join(self.temporary.name, "trailers.json")
        )
        self.show_cache = AtomicJsonCache(
            os.path.join(self.temporary.name, "shows.json")
        )

    def make_client(self, store, **kwargs):
        return TMDBServiceClient(
            store,
            Settings(),
            self.trailer_cache,
            self.show_cache,
            locale_mapper=lambda: "cs-CZ",
            id_factory=lambda _size: WAITER_ID,
            **kwargs,
        )

    @staticmethod
    def forbidden_operation():
        raise AssertionError("parent operation fallback was invoked")

    def test_interactive_success_is_validated_detached_and_acknowledged(self):
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        value = {"rows": [{"title": "Praha 🙂"}]}
        store = ScriptedStore(
            request, result=protocol.make_result(JOB_ID, "succeeded", value)
        )
        client = self.make_client(store)

        decoded = client.run_operation(request)
        decoded["rows"][0]["title"] = "changed"

        self.assertEqual(value, {"rows": [{"title": "Praha 🙂"}]})
        self.assertEqual(
            store.calls,
            [
                ("join", request, WAITER_ID),
                ("read", JOB_ID, WAITER_ID),
                ("acknowledge", JOB_ID, WAITER_ID),
            ],
        )

    def test_terminal_error_codes_map_to_fixed_typed_domain_errors(self):
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        cases = (
            ("FAILED", "failed", TMDBError, "TMDB operation failed"),
            (
                "UNAVAILABLE",
                "failed",
                TMDBServiceUnavailableError,
                "TMDB service unavailable",
            ),
            (
                "BACKPRESSURED",
                "failed",
                TMDBServiceBackpressuredError,
                "TMDB service backpressured",
            ),
            (
                "SHUTTING_DOWN",
                "failed",
                TMDBServiceShuttingDownError,
                "TMDB service shutting down",
            ),
            (
                "RESULT_EXPIRED",
                "failed",
                TMDBServiceResultExpiredError,
                "TMDB service result expired",
            ),
            ("ABORTED", "aborted", TMDBAbortError, "TMDB operation aborted"),
        )
        for code, outcome, error_type, message in cases:
            with self.subTest(code=code):
                store = ScriptedStore(
                    request,
                    result=protocol.make_result(JOB_ID, outcome, code),
                )
                client = self.make_client(store)
                with self.assertRaises(error_type) as caught:
                    client.run_operation(request)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(store.calls[-1], ("acknowledge", JOB_ID, WAITER_ID))

    def test_admission_errors_map_to_typed_errors_without_read_or_fallback(self):
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        cases = (
            ("UNAVAILABLE", TMDBServiceUnavailableError),
            ("BACKPRESSURED", TMDBServiceBackpressuredError),
            ("SHUTTING_DOWN", TMDBServiceShuttingDownError),
            ("FAILED", TMDBError),
        )
        for code, error_type in cases:
            with self.subTest(code=code):
                store = ScriptedStore(
                    request,
                    row={
                        "status": "backpressured",
                        "request": request,
                        "code": code,
                    },
                )
                client = self.make_client(store)
                with self.assertRaises(error_type):
                    client.run_operation(request)
                self.assertEqual(
                    [call[0] for call in store.calls], ["join"]
                )

    def test_abort_is_registered_before_first_wait_slice_and_acknowledged(self):
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        store = ScriptedStore(request)
        events = []

        def wait_for_abort(seconds):
            events.append(seconds)
            return True

        client = self.make_client(store, wait_for_abort=wait_for_abort)
        with self.assertRaises(TMDBAbortError):
            client.run_operation(request)

        self.assertEqual(events, [0.05])
        self.assertEqual(
            [call[0] for call in store.calls], ["join", "read", "acknowledge"]
        )
        self.assertEqual(store.calls[-1], ("acknowledge", JOB_ID, WAITER_ID))

    def test_ack_failure_preserves_valid_success_without_another_request(self):
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        store = ScriptedStore(
            request,
            result=protocol.make_result(JOB_ID, "succeeded", {"handled": True}),
        )
        store.acknowledge_error = OSError("synthetic ACK failure")
        client = self.make_client(store)

        with self.assertLogs("yawsp_app.tmdb_service_client", level="WARNING"):
            self.assertEqual(client.run_operation(request), {"handled": True})
        self.assertEqual(
            [call[0] for call in store.calls], ["join", "read", "acknowledge"]
        )

    def test_unknown_shared_key_and_store_failures_never_execute_closure(self):
        trailer = protocol.trailer_request("movie", 1)
        for failure in (protocol.ProtocolError("bad handoff"), OSError("offline")):
            with self.subTest(failure=type(failure).__name__):
                store = ScriptedStore(trailer)
                store.join_error = failure
                client = self.make_client(store)
                with self.assertRaises(protocol.ProtocolError):
                    client.run_shared(("unknown", 1), self.forbidden_operation)
                self.assertEqual([call[0] for call in store.calls], [])

        store = ScriptedStore(trailer)
        store.join_error = protocol.ProtocolError("bad handoff")
        client = self.make_client(store)
        with self.assertRaises(protocol.ProtocolError):
            client.run_shared(
                ("trailer", "movie", 1), self.forbidden_operation
            )
        self.assertEqual([call[0] for call in store.calls], ["join"])

        interactive = protocol.catalog_request("popular_movies", "cs-CZ")
        store = ScriptedStore(interactive)
        store.join_error = OSError("offline")
        with self.assertRaises(TMDBServiceUnavailableError):
            self.make_client(store).run_operation(interactive)
        self.assertEqual([call[0] for call in store.calls], ["join"])

    def test_trailer_failure_keeps_legacy_error_result_without_cache_write(self):
        request = protocol.trailer_request("movie", 7)
        store = ScriptedStore(
            request, result=protocol.make_result(JOB_ID, "failed", "UNAVAILABLE")
        )
        client = self.make_client(store)
        repository = TrailerRepository(None, self.trailer_cache)
        client.attach_repositories(repository, ShowRepository(None, self.show_cache))

        with self.assertLogs("yawsp_app.tmdb_service_client", level="ERROR"):
            result = client.run_shared(
                ("trailer", "movie", 7), self.forbidden_operation
            )

        self.assertEqual(result, TrailerLookup("error", None))
        self.assertFalse(os.path.exists(self.trailer_cache.path))

    def test_show_failure_returns_stale_value_and_false_success_flag(self):
        request = protocol.show_request(13)
        store = ScriptedStore(
            request, result=protocol.make_result(JOB_ID, "failed", "FAILED")
        )
        client = self.make_client(store)
        stale = {"season": 1, "episode": 2, "air_date": "2026-01-01"}
        self.show_cache.save({"13": {"ts": 1.0, "next": stale}})
        repository = ShowRepository(None, self.show_cache, clock=lambda: 100000.0)
        client.attach_repositories(TrailerRepository(None, self.trailer_cache), repository)

        with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
            result = client.run_shared(("show", 13), self.forbidden_operation)

        self.assertEqual(result, (stale, False))
        self.assertIsNot(result[0], stale)
        self.assertEqual(
            self.show_cache.load(), {"13": {"ts": 1.0, "next": stale}}
        )

    def test_show_false_success_flag_is_returned_exactly(self):
        request = protocol.show_request(22)
        store = ScriptedStore(
            request,
            result=protocol.make_result(
                JOB_ID, "succeeded", {"value": None, "succeeded": False}
            ),
        )
        self.assertEqual(
            self.make_client(store).run_shared(
                ("show", 22), self.forbidden_operation
            ),
            (None, False),
        )

    def test_shared_metadata_results_require_exact_domain_shapes(self):
        malformed = (
            (("trailer", "movie", 7), None),
            (("trailer", "movie", 7), {}),
            (("trailer", "movie", 7), {"status": "hit", "youtube_id": None}),
            (
                ("trailer", "movie", 7),
                {"status": "missing", "youtube_id": "unexpected"},
            ),
            (("trailer", "movie", 7), {"status": "other", "youtube_id": None}),
            (("trailer", "movie", 7), {"status": "hit"}),
            (
                ("trailer", "movie", 7),
                {"status": "hit", "youtube_id": "video000000", "extra": True},
            ),
            (("show", 7), None),
            (("show", 7), {}),
            (("show", 7), {"unrelated": True}),
            (("show", 7), {"value": None}),
            (("show", 7), {"succeeded": True}),
            (("show", 7), {"value": None, "succeeded": "yes"}),
            (("show", 7), {"value": None, "succeeded": 1}),
            (("show", 7), {"value": [], "succeeded": True}),
            (("show", 7), {"value": "episode", "succeeded": False}),
            (
                ("show", 7),
                {"value": None, "succeeded": False, "extra": True},
            ),
        )
        for key, value in malformed:
            with self.subTest(key=key, value=value):
                request = (
                    protocol.trailer_request(key[1], key[2])
                    if key[0] == "trailer"
                    else protocol.show_request(key[1])
                )
                store = ScriptedStore(
                    request,
                    result={
                        "job_id": JOB_ID,
                        "outcome": "succeeded",
                        "value": value,
                    },
                )
                client = self.make_client(store)
                with self.assertRaises(protocol.ProtocolError):
                    client.run_shared(key, self.forbidden_operation)
                self.assertEqual(
                    [call[0] for call in store.calls],
                    ["join", "read", "acknowledge"],
                )

        legitimate = (
            (("trailer", "movie", 7), {"status": "hit", "youtube_id": "video000000"}),
            (("trailer", "movie", 7), {"status": "missing", "youtube_id": None}),
            (("trailer", "movie", 7), {"status": "error", "youtube_id": None}),
            (("show", 7), {"value": None, "succeeded": True}),
            (("show", 7), {"value": None, "succeeded": False}),
        )
        for key, value in legitimate:
            with self.subTest(key=key, value=value):
                request = (
                    protocol.trailer_request(key[1], key[2])
                    if key[0] == "trailer"
                    else protocol.show_request(key[1])
                )
                store = ScriptedStore(
                    request,
                    result=protocol.make_result(JOB_ID, "succeeded", value),
                )
                expected = (
                    TrailerLookup(value["status"], value["youtube_id"])
                    if key[0] == "trailer"
                    else (None, value["succeeded"])
                )
                self.assertEqual(
                    self.make_client(store).run_shared(
                        key, self.forbidden_operation
                    ),
                    expected,
                )

    def test_background_submission_counts_accepted_collisions_without_waiting(self):
        request = protocol.trailer_request("movie", 1)
        store = ScriptedStore(request)
        store.submit_rows = [
            {
                "status": "accepted",
                "job_id": JOB_ID,
                "request": request,
                "reservation": True,
            },
            {"status": "joined", "job_id": JOB_ID, "request": request},
            {"status": "backpressured", "request": request, "code": "BACKPRESSURED"},
        ]
        client = self.make_client(store)

        accepted = client.submit_metadata(
            "movie", trailer_ids=(1, 2), show_ids=(3,), missing_known=True
        )

        self.assertEqual(accepted, 2)
        submit = store.calls[0]
        self.assertEqual(submit[0], "submit")
        self.assertEqual(len(submit[1]), 3)
        self.assertEqual(
            [item["operation"] for item in submit[1]],
            ["metadata.trailer", "metadata.trailer", "metadata.show"],
        )
        self.assertFalse(submit[2])
        self.assertIsNone(submit[3])
        self.assertEqual(len(store.calls), 1)

    def test_watchlist_submission_uses_only_nonsecret_session_binding(self):
        class SnapshotReads:
            def refresh_due(self, scope, now):
                return True

            def generation(self, scope):
                return 4

        request = protocol.watchlist_request(SCOPE, 4, False, False, SESSION_BINDING)
        store = ScriptedStore(request)
        client = self.make_client(store, snapshot_store=SnapshotReads())

        self.assertEqual(client.submit_watchlist(SCOPE), 1)

        submitted = store.calls[0][1][0]
        self.assertEqual(submitted["generation"], 4)
        self.assertRegex(submitted["session_binding"], r"^v1:[0-9a-f]{64}$")
        self.assertNotIn("synthetic-secret-session", repr(submitted))

    def test_close_during_join_is_nonjoining_and_acknowledges_late_handoff_once(self):
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        entered = threading.Event()
        release = threading.Event()

        class BlockingJoinStore(ScriptedStore):
            def join(self, request, waiter_id):
                entered.set()
                self.assert_release = release.wait(5)
                return super().join(request, waiter_id)

        store = BlockingJoinStore(request)
        client = self.make_client(store)
        errors = []

        def invoke():
            try:
                client.run_operation(request)
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        invocation = threading.Thread(target=invoke)
        invocation.start()
        self.assertTrue(entered.wait(5))
        client.close(wait=True)
        client.close(wait=False)
        self.assertTrue(invocation.is_alive(), "close joined a blocked store handoff")
        release.set()
        invocation.join(5)
        self.assertFalse(invocation.is_alive())
        self.assertTrue(store.assert_release)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TMDBAbortError)
        self.assertEqual([call[0] for call in store.calls], ["join", "acknowledge"])
        self.assertEqual(store.calls[-1], ("acknowledge", JOB_ID, WAITER_ID))

    def test_close_does_not_translate_unrelated_read_protocol_failure(self):
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        failure = protocol.ProtocolError("unrelated terminal schema failure")

        class BlockingFailureStore(ScriptedStore):
            def read_wait_result(self, job_id, waiter_id):
                self.calls.append(("read", job_id, waiter_id))
                entered.set()
                release.wait(5)
                raise failure

        store = BlockingFailureStore(request)
        client = self.make_client(store)
        errors = []

        def invoke():
            try:
                client.run_operation(request)
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        invocation = threading.Thread(target=invoke)
        invocation.start()
        self.assertTrue(entered.wait(5))
        client.close()
        release.set()
        invocation.join(5)
        self.assertFalse(invocation.is_alive())
        self.assertEqual(errors, [failure])
        self.assertIs(errors[0], failure)
        self.assertEqual(
            store.calls,
            [
                ("join", request, WAITER_ID),
                ("read", JOB_ID, WAITER_ID),
                ("acknowledge", JOB_ID, WAITER_ID),
            ],
        )

    def test_run_watchlist_returns_only_scope_and_revision(self):
        request = protocol.watchlist_request(SCOPE, 4, True, False, SESSION_BINDING)
        value = {"scope": SCOPE, "revision": 9, "generation": 5}
        store = ScriptedStore(
            request, result=protocol.make_result(JOB_ID, "succeeded", value)
        )

        self.assertEqual(
            self.make_client(store).run_watchlist(request),
            {"scope": SCOPE, "revision": 9},
        )

    def test_attach_discards_pending_immediate_and_deferred_writes(self):
        class PendingCache:
            def __init__(self):
                self.saves = []
                self.merges = []

            def load(self):
                return {}

            def save(self, mapping):
                self.saves.append(copy.deepcopy(mapping))
                return False

            def save_merged(self, updates, prefer_update=None):
                self.merges.append(copy.deepcopy(updates))
                return False

        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                cache = PendingCache()
                repository = TrailerRepository(
                    None, cache, _defer_save=deferred
                )
                key = "movie:1"
                entry = {"ts": 1.0, "youtube_id": "pending"}
                repository._entries = {key: entry}
                repository._save_success(key)
                self.assertTrue(repository._save_pending)
                self.assertEqual(len(cache.saves), 0 if deferred else 1)
                self.assertEqual(cache.merges, [])
                calls_before_attach = (len(cache.saves), len(cache.merges))
                client = self.make_client(
                    ScriptedStore(protocol.trailer_request("movie", 2))
                )

                client.attach_repositories(
                    repository, ShowRepository(None, self.show_cache)
                )
                repository._save_success(key)
                repository._flush()

                self.assertEqual(
                    (len(cache.saves), len(cache.merges)), calls_before_attach
                )
                self.assertFalse(repository._save_pending)
                self.assertEqual(repository._dirty_entries, {})
                self.assertEqual(repository._changes_since_flush, 0)
                self.assertEqual(repository._entries, {key: entry})

                repository._save_pending = True
                repository._dirty_entries[key] = entry
                repository._flush()
                self.assertEqual(
                    (len(cache.saves), len(cache.merges)), calls_before_attach
                )

    def test_read_only_legacy_cache_conversion_performs_zero_writes(self):
        original = {"movie:1": "legacy00001", "movie:2": None}
        self.trailer_cache.save(original)
        with open(self.trailer_cache.path, "rb") as handle:
            before = handle.read()
        repository = TrailerRepository(None, self.trailer_cache)
        client = self.make_client(ScriptedStore(protocol.trailer_request("movie", 3)))
        client.attach_repositories(
            repository, ShowRepository(None, self.show_cache)
        )
        self.trailer_cache.save = lambda *_args, **_kwargs: self.fail(
            "read-only legacy migration attempted a cache save"
        )
        self.trailer_cache.save_merged = lambda *_args, **_kwargs: self.fail(
            "read-only lookup attempted a merged cache save"
        )

        self.assertTrue(repository.needs_lookup("movie", 1))
        self.assertTrue(repository.needs_lookup("movie", 2))
        self.assertEqual(repository._entries["movie:1"]["youtube_id"], "legacy00001")
        self.assertIsNone(repository._entries["movie:1"]["ts"])
        self.assertNotIn("movie:2", repository._entries)

        accepted = client.submit_metadata("movie", trailer_ids=(1, 2))

        self.assertEqual(accepted, 2)
        submitted = [call for call in client.store.calls if call[0] == "submit"]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(
            [request["params"]["media_id"] for request in submitted[0][1]], [1, 2]
        )
        with open(self.trailer_cache.path, "rb") as handle:
            self.assertEqual(handle.read(), before)


class RealStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name
        self.service = ServiceStateStore(self.root)
        self.assertTrue(self.service.lease.acquire())
        self.service.recover(EPOCH)
        self.service.set_ready(EPOCH)
        self.clients = []
        self.identifiers = itertools.count(1)

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.service.lease.release()
        self.temporary.cleanup()

    def make_client(self, wait_for_abort=None, caller=None):
        if caller is None:
            caller = ServiceStateStore(self.root)
        cache = AtomicJsonCache(os.path.join(self.root, "cache.json"))
        client = TMDBServiceClient(
            caller,
            Settings(),
            cache,
            cache,
            locale_mapper=lambda: "cs-CZ",
            wait_for_abort=wait_for_abort,
            id_factory=self.next_identifier,
        )
        self.clients.append(client)
        return client

    def next_identifier(self, _size):
        return "%032x" % next(self.identifiers)

    def manifest(self):
        with open(
            os.path.join(self.root, "tmdb_service_state", "manifest.json"),
            encoding="utf-8",
        ) as handle:
            return json.load(handle)

    def wait_for_waiters(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jobs = self.manifest()["jobs"]
            if len(jobs) == 1 and len(jobs[0]["waiters"]) == count:
                return jobs[0]
            time.sleep(0.01)
        self.fail("timed out waiting for registered waiters")

    def complete_value(self, value):
        claim = self.service.claim_next(EPOCH, CLAIM)
        self.assertIsNotNone(claim)
        result = protocol.make_result(claim["job_id"], "succeeded", value)
        self.assertEqual(self.service.complete(claim, result), result)
        self.assertEqual(self.service.complete(claim, result), result)
        self.assertIsNone(self.service.claim_next(EPOCH, CLAIM))
        return claim["job_id"]

    def run_fanout(self, key, value):
        clients = [self.make_client(), self.make_client()]
        outputs = [[], []]
        errors = []

        def invoke(index):
            try:
                outputs[index] = clients[index].run_shared(
                    key, lambda: (_ for _ in ()).throw(
                        AssertionError("parent closure executed")
                    )
                )
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        job = self.wait_for_waiters(2)
        job_id = self.complete_value(value)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(len(self.manifest()["jobs"]), 1)
        self.assertEqual(self.manifest()["jobs"][0]["waiters"], [])
        return outputs

    def test_same_key_trailer_waiters_share_one_job_and_typed_value(self):
        outputs = self.run_fanout(
            ("trailer", "movie", 21),
            {"status": "hit", "youtube_id": "sharevideo0"},
        )
        self.assertEqual(
            outputs,
            [
                TrailerLookup("hit", "sharevideo0"),
                TrailerLookup("hit", "sharevideo0"),
            ],
        )

    def test_same_key_show_waiters_share_exact_value_and_real_success_flag(self):
        episode = {"season": 2, "episode": 4, "air_date": "2026-12-01"}
        outputs = self.run_fanout(
            ("show", 21), {"value": episode, "succeeded": True}
        )
        self.assertEqual(outputs, [(episode, True), (episode, True)])
        self.assertIsNot(outputs[0][0], outputs[1][0])

    def test_interactive_collision_reuses_detached_terminal_without_new_job(self):
        client = self.make_client()
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        value = {"rows": [{"title": "one00000000"}]}
        # Complete only after the first interactive admission creates the job.
        first_output = []
        first = threading.Thread(target=lambda: first_output.append(client.run_operation(request)))
        first.start()
        self.wait_for_waiters(1)
        job_id = self.complete_value(value)
        first.join(5)
        self.assertFalse(first.is_alive())

        first_output[0]["rows"][0]["title"] = "changed"
        self.assertEqual(client.run_operation(request), value)
        self.assertEqual(len(self.manifest()["jobs"]), 1)
        self.assertEqual(self.manifest()["jobs"][0]["job_id"], job_id)

    def test_close_during_blocked_terminal_read_maps_detach_race_to_abort(self):
        caller = ServiceStateStore(self.root)
        waiting_for_result = threading.Event()
        terminal_read = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        original_read = caller.read_wait_result
        read_calls = []

        def blocked_read(job_id, waiter_id):
            read_calls.append((job_id, waiter_id))
            if len(read_calls) == 1:
                return original_read(job_id, waiter_id)
            terminal_read.set()
            release.wait(5)
            return original_read(job_id, waiter_id)

        caller.read_wait_result = blocked_read
        acknowledgements = []
        original_acknowledge = caller.acknowledge

        def record_acknowledgement(job_id, waiter_id):
            acknowledgements.append((job_id, waiter_id))
            return original_acknowledge(job_id, waiter_id)

        caller.acknowledge = record_acknowledgement

        def wait_slice(seconds):
            self.assertEqual(seconds, 0.05)
            waiting_for_result.set()
            return False

        client = self.make_client(caller=caller, wait_for_abort=wait_slice)
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        errors = []
        invocation = threading.Thread(
            target=lambda: self.capture_error(client, request, errors)
        )
        invocation.start()
        job = self.wait_for_waiters(1)
        self.assertTrue(waiting_for_result.wait(5))
        job_id = self.complete_value({"handled": True})
        self.assertTrue(terminal_read.wait(5))

        client.close(wait=True)

        current = self.manifest()["jobs"][0]
        self.assertEqual(current["job_id"], job_id)
        self.assertEqual(current["waiters"], [])
        self.assertTrue(invocation.is_alive(), "close joined a blocked terminal read")
        release.set()
        invocation.join(5)
        self.assertFalse(invocation.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TMDBAbortError)
        self.assertEqual(acknowledgements, [(job_id, job["waiters"][0])])

    def test_unknown_job_returns_typed_expiry_without_an_absent_path_poll(self):
        unexpected_waits = []

        def unexpected_wait(seconds):
            unexpected_waits.append(seconds)
            raise AssertionError("unknown job would wait indefinitely")

        client = self.make_client(wait_for_abort=unexpected_wait)
        unknown = "f" * 32
        result = client._read_result(unknown, WAITER_ID)
        self.assertEqual(result["job_id"], unknown)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["error"]["code"], "RESULT_EXPIRED")
        self.assertEqual(unexpected_waits, [])

    def test_real_tombstone_returns_typed_expiry_without_an_absent_path_poll(self):
        unexpected_waits = []

        def unexpected_wait(seconds):
            unexpected_waits.append(seconds)
            raise AssertionError("tombstone would wait indefinitely")

        client = self.make_client(wait_for_abort=unexpected_wait)
        caller = client.store
        waiter_id = self.next_identifier(16)
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        row = caller.join(request, waiter_id)
        claim = self.service.claim_next(EPOCH, CLAIM)
        first_result = protocol.make_result(
            row["job_id"], "succeeded", {"handled": True}
        )
        self.service.complete(claim, first_result)
        caller.acknowledge(row["job_id"], waiter_id)
        for index in range(32):
            other = caller.submit(
                [protocol.trailer_request("movie", index + 2)]
            )[0]
            next_claim = self.service.claim_next(EPOCH, "%032x" % (index + 100))
            self.service.complete(
                next_claim,
                protocol.make_result(
                    other["job_id"], "succeeded", {"status": "error", "youtube_id": None}
                ),
            )
        result = client._read_result(row["job_id"], waiter_id)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["error"]["code"], "RESULT_EXPIRED")
        self.assertEqual(unexpected_waits, [])

    def test_absent_result_wait_slices_use_path_probes_not_manifest_reads(self):
        class TrackedStore(ServiceStateStore):
            def __init__(self, root):
                super().__init__(root)
                self.loads = 0
                self.read_transactions = 0
                self.probes = 0

            def _load(self):
                self.loads += 1
                return super()._load()

            def _read_transaction(self, reader):
                self.read_transactions += 1
                return super()._read_transaction(reader)

            def result_candidate(self, job_id):
                self.probes += 1
                return super().result_candidate(job_id)

        callers = [TrackedStore(self.root), TrackedStore(self.root)]
        ready = [threading.Event(), threading.Event()]
        proceed = threading.Event()
        self.addCleanup(proceed.set)
        waits = [[], []]

        def wait_for_abort(index, seconds):
            waits[index].append(seconds)
            if len(waits[index]) == 5:
                ready[index].set()
            if len(waits[index]) >= 5:
                self.assertTrue(proceed.wait(5), "result publication barrier")
            return False

        clients = [
            self.make_client(
                caller=callers[index],
                wait_for_abort=lambda seconds, index=index: wait_for_abort(index, seconds),
            )
            for index in range(2)
        ]
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        outputs = [[], []]
        errors = []

        def invoke(index):
            try:
                outputs[index] = clients[index].run_operation(request)
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for event in ready:
            self.assertTrue(event.wait(5))

        self.assertEqual([caller.loads for caller in callers], [2, 2])
        self.assertEqual([caller.read_transactions for caller in callers], [1, 1])
        self.assertEqual([len(items) for items in waits], [5, 5])
        self.assertTrue(all(seconds == 0.05 for items in waits for seconds in items))

        self.complete_value({"handled": True})
        proceed.set()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(outputs, [{"handled": True}, {"handled": True}])
        self.assertEqual(
            [caller.loads for caller in callers],
            [4, 4],
            "admission, initial nonterminal read, terminal read and ACK per caller",
        )
        self.assertEqual([caller.read_transactions for caller in callers], [2, 2])
        self.assertEqual([caller.probes for caller in callers], [5, 5])

    def test_close_detaches_only_its_waiter_and_never_joins_or_stops_service(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_wait(seconds):
            self.assertEqual(seconds, 0.05)
            entered.set()
            release.wait(5)
            return False

        closing_client = self.make_client(wait_for_abort=blocked_wait)
        remaining_client = self.make_client()
        request = protocol.catalog_request("popular_movies", "cs-CZ")
        closing_errors = []
        remaining_results = []

        closing = threading.Thread(
            target=lambda: self.capture_error(closing_client, request, closing_errors)
        )
        remaining = threading.Thread(
            target=lambda: remaining_results.append(
                remaining_client.run_operation(request)
            )
        )
        closing.start()
        remaining.start()
        job = self.wait_for_waiters(2)
        self.assertTrue(entered.wait(5))

        closing_client.close(wait=True)
        closing_client.close(wait=False)

        self.assertTrue(closing.is_alive(), "close joined its invocation waiter")
        current = self.manifest()["jobs"][0]
        self.assertEqual(current["job_id"], job["job_id"])
        self.assertEqual(len(current["waiters"]), 1)
        foreign_waiter = current["waiters"][0]
        self.assertTrue(self.service.service_ready())
        self.assertTrue(self.service.lease.acquired)
        release.set()
        closing.join(5)
        self.assertFalse(closing.is_alive())
        self.assertEqual(len(closing_errors), 1)
        self.assertIsInstance(closing_errors[0], TMDBAbortError)

        self.complete_value({"handled": True})
        remaining.join(5)
        self.assertFalse(remaining.is_alive())
        self.assertEqual(remaining_results, [{"handled": True}])
        self.assertNotIn(foreign_waiter, self.manifest()["jobs"][0]["waiters"])

    @staticmethod
    def capture_error(client, request, errors):
        try:
            client.run_operation(request)
        except Exception as error:  # pragma: no cover - asserted by caller
            errors.append(error)


if __name__ == "__main__":
    unittest.main()
