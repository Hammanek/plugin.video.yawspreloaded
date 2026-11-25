import os
import tempfile
import threading
import time
import unittest

from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.service_queue import (
    QueueValidationError,
    ServiceConflictError,
    ServiceJobQueue,
    ServiceJobWorker,
    ServiceRunner,
    build_metadata_document,
    build_shared_document,
    build_watchlist_document,
)
from yawsp_app.tmdb import (
    BackgroundPrefetch,
    TMDBAbortError,
    TMDBError,
    TrailerLookup,
)
from yawsp_app.watchlist_snapshot import SnapshotScope, WatchlistSnapshotStore
from tests.test_service_queue import JOB_ID, metadata_item
from tests.test_runtime import RecordingLogger
from tests.test_tmdb import ManualWorker, MemorySettings, MetadataSession, json_response


def internal_item(family="trailer", media_id=42, attempts=0):
    if family == "trailer":
        return (
            ("trailer", "movie", media_id),
            "movie:{0}".format(media_id),
            "trailer",
            "movie",
            media_id,
            attempts,
            0.0,
        )
    return (
        ("show", media_id),
        str(media_id),
        "show",
        None,
        media_id,
        attempts,
        0.0,
    )


def trailer_result(youtube_id="video-42"):
    return {
        "trailer/movie/42": {
            "kind": "trailer",
            "status": "hit" if youtube_id else "missing",
            "youtube_id": youtube_id,
        }
    }


class ServiceJobWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.queue = ServiceJobQueue(os.path.join(self.temporary.name, "queue"))
        self.lease = self.queue.acquire_service("0123456789abcdef0123456789abcdef")
        self.addCleanup(self.lease.close)
        self.worker = ServiceJobWorker(self.queue, poll_interval=0.005, wait_timeout=1.0)

    def pending_documents(self):
        root = os.path.join(self.queue.root, "pending")
        return os.listdir(root) if os.path.isdir(root) else []

    def serve_once(self, results=None, state="completed"):
        deadline = time.monotonic() + 2
        while True:
            claim = self.queue.claim_one()
            if claim is not None:
                break
            if time.monotonic() > deadline:
                self.fail("service did not receive a job")
            time.sleep(0.002)
        if state == "completed":
            self.queue.complete(claim, results or trailer_result())
        elif state == "aborted":
            self.queue.abort(claim)
        else:
            self.queue.reject(claim, "failed")
        return claim.job_id

    def test_only_reserved_named_background_functions_can_be_serialized(self):
        for function, args in (
            (lambda: None, ()),
            (self.worker._run_metadata, ()),
            (self.worker._run_watchlist, ()),
        ):
            with self.subTest(function=getattr(function, "__name__", "lambda")):
                with self.assertRaises(TypeError):
                    self.worker.submit(function, *args)
        self.assertEqual(self.pending_documents(), [])

    def test_metadata_batch_returns_after_publish_without_parent_http(self):
        settings = MemorySettings()
        session_calls = []
        background = BackgroundPrefetch(
            settings,
            AtomicJsonCache(os.path.join(self.temporary.name, "trailers.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "shows.json")),
            lambda: "en-US",
            session_factory=lambda: session_calls.append("forbidden"),
            worker=self.worker,
            snapshot_store=WatchlistSnapshotStore(
                AtomicJsonCache(os.path.join(self.temporary.name, "watchlist.json"))
            ),
        )
        self.worker.bind_background(background)

        accepted = background.submit_trailers("movie", [42], missing_known=True)

        self.assertEqual(accepted, 1)
        self.assertEqual(len(self.pending_documents()), 1)
        self.assertEqual(session_calls, [])
        self.assertFalse(hasattr(self.worker, "_executor"))

    def test_large_background_drains_all_batches_and_deduplicates_without_parent_http(self):
        session_calls = []
        background = BackgroundPrefetch(
            MemorySettings(),
            AtomicJsonCache(os.path.join(self.temporary.name, "trailers-large.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "shows-large.json")),
            lambda: "en-US",
            session_factory=lambda: session_calls.append("forbidden"),
            worker=self.worker,
        )
        self.worker.bind_background(background)

        accepted = background.submit_trailers("movie", range(1, 41), missing_known=True)

        self.assertEqual(accepted, 32)
        documents = []
        for name in self.pending_documents():
            with open(os.path.join(self.queue.root, "pending", name), encoding="utf-8") as stream:
                import json

                documents.append(json.load(stream))
        self.assertEqual(len(documents), 2)
        self.assertEqual(sum(len(document["payload"]["items"]) for document in documents), 40)
        self.assertEqual(session_calls, [])
        self.assertEqual(len(background._metadata_queued), 40)
        self.assertEqual(background.submit_trailers("movie", range(1, 41), missing_known=True), 0)
        self.assertEqual(len(self.pending_documents()), 2)

    def test_two_producers_cannot_publish_duplicate_key_manifests(self):
        self.worker.bind_background(None)
        first = BackgroundPrefetch(
            MemorySettings(),
            AtomicJsonCache(os.path.join(self.temporary.name, "first-trailers.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "first-shows.json")),
            lambda: "en-US",
            worker=self.worker,
        )
        second_worker = ServiceJobWorker(self.queue, poll_interval=0.005, wait_timeout=20.0)
        second = BackgroundPrefetch(
            MemorySettings(),
            AtomicJsonCache(os.path.join(self.temporary.name, "second-trailers.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "second-shows.json")),
            lambda: "en-US",
            worker=second_worker,
        )
        self.worker.bind_background(first)
        second_worker.bind_background(second)

        first.submit_trailers("movie", [42], missing_known=True)
        second.submit_trailers("movie", [42], missing_known=True)

        self.assertEqual(len(self.pending_documents()), 1)

    def test_reap_clears_completed_reservations_for_later_freshness_checks(self):
        background = BackgroundPrefetch(
            MemorySettings(),
            AtomicJsonCache(os.path.join(self.temporary.name, "reap-trailers.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "reap-shows.json")),
            lambda: "en-US",
            worker=self.worker,
        )
        self.worker.bind_background(background)
        background.submit_trailers("movie", [42], missing_known=True)
        claim = self.queue.claim_one()
        self.queue.complete(claim, trailer_result())

        self.worker.reap()

        self.assertEqual(len(background._metadata_queued), 0)
        self.assertEqual(background._pending, {})
        self.assertEqual(len(self.pending_documents()), 0)

    def test_concurrent_reaping_applies_and_deletes_each_job_once(self):
        background = BackgroundPrefetch(
            MemorySettings(),
            AtomicJsonCache(os.path.join(self.temporary.name, "reap-race-trailers.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "reap-race-shows.json")),
            lambda: "en-US",
            worker=self.worker,
        )
        self.worker.bind_background(background)
        background.submit_trailers("movie", [42], missing_known=True)
        claim = self.queue.claim_one()
        self.queue.complete(claim, trailer_result())
        errors = []

        def reap():
            try:
                self.worker.reap()
            except Exception as error:
                errors.append(error)

        callers = [threading.Thread(target=reap) for _ in range(2)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(self.worker._metadata_jobs, {})

    def test_watchlist_serializes_scope_digest_generation_not_raw_session(self):
        background = BackgroundPrefetch(
            MemorySettings(tmdb_access_token="very-private-session"),
            AtomicJsonCache(os.path.join(self.temporary.name, "trailers.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "shows.json")),
            lambda: "en-US",
            worker=self.worker,
            snapshot_store=WatchlistSnapshotStore(
                AtomicJsonCache(os.path.join(self.temporary.name, "watchlist.json"))
            ),
        )
        self.worker.bind_background(background)
        scope = SnapshotScope("old-account", "movies", "en-US")

        accepted = background.submit_watchlist(
            scope, force=True, generation=8, allow_missing_account=False
        )

        self.assertEqual(accepted, 1)
        name = self.pending_documents()[0]
        with open(os.path.join(self.queue.root, "pending", name), encoding="utf-8") as stream:
            raw = stream.read()
        self.assertNotIn("very-private-session", raw)
        self.assertNotIn("session_id", raw)
        self.assertIn('"generation":8', raw)
        self.assertIn('"session_digest"', raw)

    def test_typed_trailer_result_never_invokes_raw_parent_closure(self):
        def forbidden_operation():
            raise AssertionError("raw closure crossed service adapter")

        results = []
        caller = threading.Thread(
            target=lambda: results.append(
                self.worker.run_shared(("trailer", "movie", 42), forbidden_operation)
            )
        )
        caller.start()
        self.serve_once()
        caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(results, [TrailerLookup("hit", "video-42")])

    def test_show_result_uses_existing_repository_internal_tuple(self):
        results = []
        value = {"season": 2, "episode": 3, "air_date": "2026-11-25"}
        caller = threading.Thread(
            target=lambda: results.append(
                self.worker.run_shared(("show", 42), lambda: self.fail("inline fallback"))
            )
        )
        caller.start()
        claim = self.queue.claim_one()
        while claim is None:
            claim = self.queue.claim_one()
        self.queue.complete(
            claim,
            {"show/42": {"kind": "show", "value": value, "success": True}},
        )
        caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(results, [(value, True)])

    def test_cross_producer_same_key_reservation_loser_shares_winner(self):
        other_queue = ServiceJobQueue(self.queue.root)
        other_worker = ServiceJobWorker(other_queue, poll_interval=0.005, wait_timeout=5.0)
        barrier = threading.Barrier(2)
        first_publish = self.queue.publish
        second_publish = other_queue.publish

        def synchronized_publish(publish):
            def call(document):
                barrier.wait(2)
                return publish(document)

            return call

        self.queue.publish = synchronized_publish(first_publish)
        other_queue.publish = synchronized_publish(second_publish)
        results = []
        errors = []

        def call(worker):
            try:
                results.append(
                    worker.run_shared(("trailer", "movie", 42), lambda: self.fail("inline"))
                )
            except Exception as error:
                errors.append(error)

        callers = [threading.Thread(target=call, args=(worker,)) for worker in (self.worker, other_worker)]
        for caller in callers:
            caller.start()
        claim = self.queue.claim_one()
        deadline = time.monotonic() + 2
        while claim is None and time.monotonic() < deadline:
            time.sleep(0.002)
            claim = self.queue.claim_one()
        self.queue.complete(claim, trailer_result())
        for caller in callers:
            caller.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(results, [TrailerLookup("hit", "video-42")] * 2)
        self.assertEqual(len(os.listdir(os.path.join(self.queue.root, "completed"))), 1)

    def test_same_key_callers_share_original_background_manifest_and_result(self):
        self.queue.publish(build_metadata_document([metadata_item()]))
        results = []
        callers = [
            threading.Thread(
                target=lambda: results.append(
                    self.worker.run_shared(("trailer", "movie", 42), lambda: self.fail("inline"))
                )
            )
            for _ in range(2)
        ]
        for caller in callers:
            caller.start()
        claim = self.queue.claim_one()
        self.queue.complete(claim, trailer_result())
        for caller in callers:
            caller.join(2)
        self.assertFalse(any(caller.is_alive() for caller in callers))
        self.assertEqual(results, [TrailerLookup("hit", "video-42")] * 2)
        self.assertEqual(len(os.listdir(os.path.join(self.queue.root, "completed"))), 1)

    def test_missing_service_fails_before_job_publication_or_inline_fallback(self):
        self.lease.close()
        with self.assertRaises(TMDBError):
            self.worker.run_shared(("trailer", "movie", 42), lambda: self.fail("inline"))
        self.assertEqual(self.pending_documents(), [])

    def test_wait_timeout_and_abort_use_only_typed_domain_errors(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        worker = ServiceJobWorker(
            self.queue,
            clock=lambda: now[0],
            sleeper=sleep,
            poll_interval=0.1,
            wait_timeout=0.25,
        )
        with self.assertRaises(TMDBError):
            worker.run_shared(("trailer", "movie", 42), lambda: self.fail("inline"))
        aborted = ServiceJobWorker(
            self.queue,
            abort=lambda: True,
            clock=lambda: now[0],
            sleeper=sleep,
            wait_timeout=5,
        )
        with self.assertRaises(TMDBAbortError):
            aborted.run_shared(("show", 7), lambda: self.fail("inline"))

    def test_malformed_terminal_becomes_domain_error(self):
        worker = ServiceJobWorker(self.queue, poll_interval=0.005, wait_timeout=1.0)
        errors = []

        def call():
            try:
                worker.run_shared(("trailer", "movie", 43), lambda: self.fail("inline"))
            except Exception as error:
                errors.append(error)

        caller = threading.Thread(target=call)
        caller.start()
        deadline = time.monotonic() + 2
        claim = self.queue.claim_one()
        while claim is None and time.monotonic() < deadline:
            time.sleep(0.002)
            claim = self.queue.claim_one()
        self.assertIsNotNone(claim)
        document = claim.document
        import json

        document["terminal"] = {
            "state": "completed",
            "finished_at": 100.0,
            "results": {"trailer/movie/43": {"kind": "script"}},
        }
        with open(os.path.join(self.queue.root, "completed", claim.job_id + ".json"), "w", encoding="utf-8") as stream:
            json.dump(document, stream)
        os.unlink(claim.path)
        self.queue._release_reservations(claim.document)
        caller.join(2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TMDBError)

    def test_transport_io_becomes_domain_error(self):
        worker = ServiceJobWorker(self.queue, poll_interval=0.005, wait_timeout=0.2)
        worker.queue.publish = lambda document: (_ for _ in ()).throw(OSError("synthetic I/O"))
        with self.assertRaises(TMDBError):
            worker.run_shared(("trailer", "movie", 44), lambda: self.fail("inline"))

    def test_rejected_remote_job_is_domain_error_without_fallback(self):
        results = []

        def call():
            try:
                self.worker.run_shared(("trailer", "movie", 42), lambda: self.fail("inline"))
            except Exception as error:
                results.append(error)

        caller = threading.Thread(target=call)
        caller.start()
        self.serve_once(state="rejected")
        caller.join(2)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], TMDBError)

    def test_fourty_distinct_interactive_callers_reserve_at_most_background_plus_one(self):
        maximum = [0]
        lock = threading.Lock()
        errors = []
        results = []
        worker = ServiceJobWorker(self.queue, poll_interval=0.005, wait_timeout=20.0)

        def call(media_id):
            try:
                result = worker.run_shared(
                    ("trailer", "movie", media_id), lambda: self.fail("inline")
                )
                with lock:
                    results.append(result)
            except Exception as error:
                errors.append(error)

        callers = [threading.Thread(target=call, args=(media_id,)) for media_id in range(1, 41)]
        for caller in callers:
            caller.start()
        deadline = time.monotonic() + 2
        while len(self.pending_documents()) < 33 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(self.pending_documents()), 33)
        self.assertLessEqual(worker.interactive_count, 33)

        while any(caller.is_alive() for caller in callers):
            with lock:
                maximum[0] = max(maximum[0], worker.interactive_count)
            claim = self.queue.claim_one()
            if claim is None:
                time.sleep(0.002)
                continue
            media_id = claim.document["payload"]["key"][2]
            youtube_id = "video-{0}".format(media_id)
            self.queue.complete(
                claim,
                {
                    "trailer/movie/{0}".format(media_id): {
                        "kind": "trailer",
                        "status": "hit",
                        "youtube_id": youtube_id,
                    }
                },
            )
        for caller in callers:
            caller.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 40)
        self.assertLessEqual(maximum[0], 33)
        self.assertEqual(worker.interactive_count, 0)

    def test_real_parent_repositories_receive_trailer_and_show_from_service_only(self):
        from yawsp_app.tmdb import ShowRepository, TrailerRepository

        parent_session_calls = []
        parent_background = BackgroundPrefetch(
            MemorySettings(),
            AtomicJsonCache(os.path.join(self.temporary.name, "typed-trailers.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "typed-shows.json")),
            lambda: "en-US",
            session_factory=lambda: parent_session_calls.append("forbidden"),
            worker=self.worker,
        )
        trailers = TrailerRepository(
            None, parent_background.trailer_cache, clock=lambda: 1000.0
        )
        shows = ShowRepository(None, parent_background.show_cache, clock=lambda: 1000.0)
        parent_background.attach_repositories(trailers, shows)
        calls = []
        attempts = {}
        service_sessions = []

        def factory():
            session = MetadataSession(calls, attempts)
            service_sessions.append(session)
            return session

        runner = ServiceRunner(
            self.queue,
            MemorySettings(),
            parent_background.trailer_cache,
            parent_background.show_cache,
            WatchlistSnapshotStore(
                AtomicJsonCache(os.path.join(self.temporary.name, "typed-watchlist.json"))
            ),
            lambda: "en-US",
            session_factory=factory,
            clock=lambda: 1000.0,
            sleeper=lambda seconds: None,
            logger=RecordingLogger(),
        )
        def serve_two():
            completed = 0
            deadline = time.monotonic() + 2
            while completed < 2 and time.monotonic() < deadline:
                if runner.process_one():
                    completed += 1
                else:
                    time.sleep(0.002)

        service_thread = threading.Thread(target=serve_two)
        service_thread.start()

        trailer = trailers.lookup("movie", 42)
        show = shows.next_episode(42)

        service_thread.join(2)
        self.assertFalse(service_thread.is_alive())
        self.assertEqual(trailer, TrailerLookup("hit", "video-42"))
        self.assertIsNone(show)
        self.assertEqual(parent_session_calls, [])
        self.assertEqual(attempts, {("trailer", 42): 1, ("show", 42): 1})
        self.assertEqual(len(service_sessions), 2)
        self.assertTrue(all(session.closed for session in service_sessions))

    def test_shutdown_is_nonblocking_and_does_not_spawn_executor(self):
        self.worker.shutdown(wait=False)
        with self.assertRaises(TMDBError):
            self.worker.run_shared(("show", 42), lambda: self.fail("inline"))

    def test_background_delegates_only_to_specific_shared_worker_capability(self):
        class DedicatedWorker:
            def __init__(self):
                self.calls = []
                self.expected = TrailerLookup("hit", "service-result")

            def run_shared(self, key, operation):
                self.calls.append((key, operation))
                return self.expected

        worker = DedicatedWorker()
        background = BackgroundPrefetch(
            MemorySettings(),
            AtomicJsonCache(os.path.join(self.temporary.name, "trailers-dedicated.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "shows-dedicated.json")),
            lambda: "en-US",
            worker=worker,
        )
        operation = lambda: self.fail("parent inline operation must not run")
        result = background.run_shared(("trailer", "movie", 42), operation)
        self.assertEqual(result, TrailerLookup("hit", "service-result"))
        self.assertEqual(worker.calls, [(("trailer", "movie", 42), operation)])

    def test_legacy_manual_worker_has_no_new_delegation_requirement(self):
        worker = ManualWorker()
        background = BackgroundPrefetch(
            MemorySettings(),
            AtomicJsonCache(os.path.join(self.temporary.name, "trailers-legacy.json")),
            AtomicJsonCache(os.path.join(self.temporary.name, "shows-legacy.json")),
            lambda: "en-US",
            worker=worker,
        )
        operation = lambda: TrailerLookup("missing", None)
        self.assertEqual(background.run_shared(("trailer", "movie", 8), operation), operation())


class ServiceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.queue = ServiceJobQueue(os.path.join(self.temporary.name, "queue"))
        self.settings = MemorySettings(
            tmdb_access_token="captured-session", tmdb_account_id="old-account"
        )
        self.trailer_cache = AtomicJsonCache(os.path.join(self.temporary.name, "trailers.json"))
        self.show_cache = AtomicJsonCache(os.path.join(self.temporary.name, "shows.json"))
        self.watchlist_cache = AtomicJsonCache(os.path.join(self.temporary.name, "watchlist.json"))
        self.snapshots = WatchlistSnapshotStore(self.watchlist_cache)
        self.sessions = []

    def runner(self, session_factory=None, abort=None):
        def default_factory():
            session = MetadataSession([], {}, failures={"trailer": 0})
            self.sessions.append(session)
            return session

        return ServiceRunner(
            self.queue,
            self.settings,
            self.trailer_cache,
            self.show_cache,
            self.snapshots,
            lambda: "en-US",
            session_factory=session_factory or default_factory,
            abort=abort,
            clock=lambda: 1000.0,
            sleeper=lambda seconds: None,
            logger=RecordingLogger(),
        )

    def test_mixed_metadata_uses_real_retry_cache_results_and_serial_sessions(self):
        calls = []
        attempts = {}
        active = [0]
        maximum = [0]

        def factory():
            outer = self

            class ObservedSession(MetadataSession):
                def request(self, method, url, **kwargs):
                    active[0] += 1
                    maximum[0] = max(maximum[0], active[0])
                    try:
                        return super().request(method, url, **kwargs)
                    finally:
                        active[0] -= 1

            session = ObservedSession(calls, attempts, failures={("trailer", 7): 1})
            self.sessions.append(session)
            return session

        items = [metadata_item(), metadata_item("show", 9), metadata_item(media_id=7)]
        job_id = self.queue.publish(build_metadata_document(items))
        self.assertTrue(self.runner(factory).process_one())

        terminal = self.queue.read_terminal(job_id, require_completed=True)
        self.assertEqual(terminal["terminal"]["results"]["trailer/movie/42"]["youtube_id"], "video-42")
        self.assertEqual(terminal["terminal"]["results"]["show/9"]["value"], None)
        self.assertEqual(terminal["terminal"]["results"]["trailer/movie/7"]["youtube_id"], "video-7")
        self.assertEqual(attempts[("trailer", 7)], 2)
        self.assertEqual(maximum[0], 1)
        self.assertEqual(len(self.sessions), 2)
        self.assertTrue(all(session.closed for session in self.sessions))
        self.assertNotEqual(id(self.sessions[0]), id(self.sessions[1]))
        self.assertEqual(self.trailer_cache.load()["movie:7"]["youtube_id"], "video-7")
        self.assertIn("9", self.show_cache.load())

    def test_shared_show_deserializes_real_next_episode(self):
        calls = []
        attempts = {}

        def factory():
            session = MetadataSession(calls, attempts)
            self.sessions.append(session)
            return session

        job_id = self.queue.publish(build_shared_document(("show", 42)))
        self.runner(factory).process_one()
        result = self.queue.read_terminal(job_id)["terminal"]["results"]["show/42"]
        self.assertEqual(result, {"kind": "show", "value": None, "success": True})
        self.assertEqual(attempts, {("show", 42): 1})

    def test_cache_merge_preserves_unrelated_existing_bytes(self):
        self.trailer_cache.save({"movie:2": {"ts": 1.0, "youtube_id": "keep"}})
        self.queue.publish(build_metadata_document([metadata_item()]))
        self.runner().process_one()
        self.assertEqual(self.trailer_cache.load()["movie:2"]["youtube_id"], "keep")

    def test_watchlist_uses_captured_scope_and_closes_session(self):
        from tests.support.http_stubs import FakeSession

        scope = SnapshotScope("old-account", "movies", "en-US")
        session = FakeSession([json_response({"total_pages": 1, "results": [{"id": 42, "title": "Current"}]})])
        self.sessions.append(session)
        job_id = self.queue.publish(
            build_watchlist_document(
                scope, "captured-session", 0, True, False
            )
        )

        self.runner(lambda: session).process_one()

        self.queue.read_terminal(job_id, require_completed=True)
        self.assertEqual(self.snapshots.load(scope).results, [{"id": 42, "title": "Current"}])
        self.assertTrue(session.closed)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["kwargs"]["params"]["session_id"], "captured-session")

    def test_wrong_session_account_or_language_aborts_without_http_or_save(self):
        scope = SnapshotScope("old-account", "movies", "en-US")
        original = self.snapshots.save_complete(scope, [{"id": 1, "title": "Old"}], 1.0)
        for switching in ("session", "account", "language"):
            with self.subTest(switching=switching):
                job_id = self.queue.publish(
                    build_watchlist_document(scope, "captured-session", original.generation, True, False)
                )
                if switching == "session":
                    self.settings.set("tmdb_access_token", "different-session")
                elif switching == "account":
                    self.settings.set("tmdb_account_id", "different-account")
                runner = self.runner()
                if switching == "language":
                    runner.locale_mapper = lambda: "cs-CZ"
                runner.process_one()
                self.assertEqual(self.queue.read_terminal(job_id)["terminal"]["state"], "aborted")
                self.assertEqual(self.sessions, [])
                self.assertEqual(self.snapshots.load(scope), original)
                self.queue.recover_stale()

    def test_mid_response_identity_switch_aborts_and_preserves_both_scopes(self):
        from tests.support.http_stubs import FakeSession

        scope = SnapshotScope("old-account", "movies", "en-US")
        old = self.snapshots.save_complete(scope, [{"id": 1, "title": "Old"}], 1.0)
        other_scope = SnapshotScope("other-account", "movies", "en-US")
        other = self.snapshots.save_complete(other_scope, [{"id": 2, "title": "Other"}], 1.0)

        def switch():
            self.settings.set("tmdb_access_token", "new-session")
            self.settings.set("tmdb_account_id", "other-account")

        response = json_response({"total_pages": 1, "results": [{"id": 3, "title": "Wrong"}]})
        response.json = lambda: switch() or {"total_pages": 1, "results": [{"id": 3, "title": "Wrong"}]}
        session = FakeSession([response])
        job_id = self.queue.publish(
            build_watchlist_document(scope, "captured-session", old.generation, True, False)
        )

        self.runner(lambda: session).process_one()

        self.assertEqual(self.queue.read_terminal(job_id)["terminal"]["state"], "aborted")
        self.assertTrue(session.closed)
        self.assertEqual(self.snapshots.load(scope), old)
        self.assertEqual(self.snapshots.load(other_scope), other)

    def test_stale_generation_followup_is_rebuilt_once_with_current_settings(self):
        from tests.support.http_stubs import FakeSession

        scope = SnapshotScope("old-account", "movies", "en-US")
        self.snapshots.save_complete(scope, [{"id": 1, "title": "Old"}], 1.0)
        first = FakeSession()
        second = FakeSession()
        self.sessions.extend((first, second))

        def mutate_generation():
            self.snapshots.invalidate(scope)
            return {"total_pages": 1, "results": [{"id": 2, "title": "Stale"}]}

        first.queue_response(json_response({}))
        first.responses[0].json = mutate_generation
        second.queue_response(json_response({"total_pages": 1, "results": [{"id": 3, "title": "Fresh"}]}))
        job_id = self.queue.publish(
            build_watchlist_document(scope, "captured-session", 0, True, False)
        )
        session_iter = iter((first, second))
        runner = self.runner(lambda: next(session_iter))

        runner.process_one()
        self.queue.read_terminal(job_id, require_completed=True)
        pending = self.queue.claim_one()
        self.assertIsNotNone(pending)
        self.assertTrue(pending.document["payload"]["followup"])
        self.assertEqual(pending.document["payload"]["generation"], 1)
        # Put the valid claimed document back through its normal journey for the
        # second real service invocation boundary without inventing raw execution.
        restored = os.path.join(self.queue.root, "pending", pending.job_id + ".json")
        os.link(pending.path, restored)
        os.unlink(pending.path)
        runner.process_one()

        self.assertEqual(self.snapshots.load(scope).results, [{"id": 3, "title": "Fresh"}])
        self.assertEqual(len(first.calls) + len(second.calls), 2)
        self.assertEqual(self.pending_names(), [])

    def pending_names(self):
        path = os.path.join(self.queue.root, "pending")
        return os.listdir(path) if os.path.isdir(path) else []

    def test_abort_during_delayed_request_closes_session_and_marks_aborted(self):
        from tests.support.http_stubs import FakeSession

        scope = SnapshotScope("old-account", "movies", "en-US")
        entered = threading.Event()
        release = threading.Event()
        aborted = threading.Event()
        session = FakeSession()

        def request(method, url, **kwargs):
            session.calls.append({"method": method, "url": url, "kwargs": kwargs})
            entered.set()
            self.assertTrue(release.wait(2))
            return json_response({"total_pages": 1, "results": []})

        session.request = request
        job_id = self.queue.publish(
            build_watchlist_document(scope, "captured-session", 0, True, False)
        )
        runner = self.runner(lambda: session, abort=aborted.is_set)
        service_thread = threading.Thread(target=runner.process_one)
        service_thread.start()
        self.assertTrue(entered.wait(2))
        aborted.set()
        release.set()
        service_thread.join(2)

        self.assertFalse(service_thread.is_alive())
        self.assertTrue(session.closed)
        self.assertEqual(self.queue.read_terminal(job_id)["terminal"]["state"], "aborted")

    def test_only_one_service_lease_can_consume_and_conflict_does_no_http(self):
        first = self.queue.acquire_service()
        try:
            with self.assertRaises(ServiceConflictError):
                self.queue.acquire_service()
            self.assertEqual(self.sessions, [])
        finally:
            first.close()
        second = self.queue.acquire_service()
        second.close()


if __name__ == "__main__":
    unittest.main()
