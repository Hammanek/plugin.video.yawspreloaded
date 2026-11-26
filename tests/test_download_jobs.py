import os
import tempfile
import threading
import unittest
from unittest import mock

from tests.support.http_stubs import FakeSession
from tests.test_runtime import RecordingLogger
from yawsp_app.download_jobs import (
    DownloadJobCancelled,
    DownloadJobKey,
    DownloadJobManager,
    DownloadOperation,
    media_job_key,
)


class ControlledOperation(DownloadOperation):
    def __init__(self, events, run=None, result=None):
        self.events = events
        self.run_callback = run
        self.result = result
        self.succeeded_event = threading.Event()
        self.failed_event = threading.Event()
        self.cancelled_event = threading.Event()
        self.error = None

    def run(self, context):
        self.events.append("run")
        if self.run_callback is not None:
            return self.run_callback(context)
        return self.result

    def succeeded(self, result):
        self.events.append("succeeded")
        self.succeeded_event.set()

    def failed(self, error):
        self.error = error
        self.events.append("failed")
        self.failed_event.set()

    def cancelled(self):
        self.events.append("cancelled")
        self.cancelled_event.set()


class DownloadJobManagerTests(unittest.TestCase):
    def setUp(self):
        self.abort = False
        self.sessions = []
        self.session_lock = threading.Lock()
        self.logger = RecordingLogger()

        def session_factory():
            session = FakeSession()
            with self.session_lock:
                self.sessions.append(session)
            return session

        self.session_factory = session_factory
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close(wait=False)

    def manager(self, max_pending=4):
        manager = DownloadJobManager(
            session_factory=self.session_factory,
            abort=lambda: self.abort,
            logger=self.logger,
            max_pending=max_pending,
        )
        self.managers.append(manager)
        return manager

    def test_submit_accepts_before_blocked_operation_and_notifies_success_only_after_release(self):
        manager = self.manager()
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        events = []

        def blocked(context):
            events.append("worker-entered")
            entered.set()
            self.assertTrue(release.wait(2))
            events.append("worker-released")
            return "receipt"

        operation = ControlledOperation(events, blocked)

        def submit():
            events.append("submit-entered")
            self.assertTrue(manager.submit(media_job_key("Movie.mkv"), operation))
            events.append("submit-returned")
            returned.set()

        thread = threading.Thread(target=submit)
        thread.start()
        self.assertTrue(returned.wait(2))
        thread.join(2)
        self.assertTrue(entered.wait(2))
        self.assertFalse(operation.succeeded_event.is_set())
        self.assertEqual(manager.status(media_job_key("Movie.mkv")), "running")
        self.assertEqual(events[-1], "worker-entered")

        release.set()

        self.assertTrue(operation.succeeded_event.wait(2))
        self.assertEqual(manager.status(media_job_key("Movie.mkv")), "succeeded")
        self.assertLess(events.index("submit-returned"), events.index("worker-released"))
        self.assertLess(events.index("worker-released"), events.index("succeeded"))

    def test_duplicate_and_quota_reject_without_reserving_unsubmitted_work(self):
        manager = self.manager(max_pending=1)
        entered = threading.Event()
        release = threading.Event()

        def blocked(context):
            entered.set()
            self.assertTrue(release.wait(2))

        first = ControlledOperation([], blocked)
        first_key = media_job_key(os.path.join("downloads", "Movie.mkv"))
        equivalent_key = media_job_key(os.path.join("downloads", ".", "Movie.mkv"))
        self.assertTrue(manager.submit(first_key, first))
        self.assertTrue(entered.wait(2))
        self.assertFalse(manager.submit(equivalent_key, ControlledOperation([])))
        self.assertFalse(manager.submit(media_job_key("Other.mkv"), ControlledOperation([])))

        release.set()
        self.assertTrue(first.succeeded_event.wait(2))
        retry = ControlledOperation([])
        self.assertTrue(manager.submit(media_job_key("Other.mkv"), retry))
        self.assertTrue(retry.succeeded_event.wait(2))
        self.assertEqual(manager.status(media_job_key("Other.mkv")), "succeeded")

    def test_worker_start_failure_rolls_back_reservation_and_retry_succeeds(self):
        manager = self.manager(max_pending=1)
        key = media_job_key("retry-after-start-failure")
        rejected = ControlledOperation([])

        with mock.patch(
            "yawsp_app.download_jobs.threading.Thread.start",
            side_effect=RuntimeError("synthetic worker start failure"),
        ):
            self.assertFalse(manager.submit(key, rejected))

        self.assertIsNone(manager.status(key))
        self.assertIsNone(manager.job_status(key))
        self.assertNotIn(key, manager._active)
        self.assertFalse(any(job.operation is rejected for job in manager._pending))
        self.assertEqual(rejected.events, [])

        retry = ControlledOperation([], result="receipt")
        self.assertTrue(manager.submit(key, retry))
        self.assertTrue(retry.succeeded_event.wait(2))
        self.assertEqual(manager.status(key), "succeeded")
        self.assertEqual(retry.events, ["run", "succeeded"])
        manager.close(wait=True)

    def test_failure_releases_quota_and_does_not_poison_next_job(self):
        manager = self.manager(max_pending=1)
        failed = ControlledOperation(
            [], lambda context: (_ for _ in ()).throw(RuntimeError("broken job"))
        )
        following = ControlledOperation([])
        self.assertTrue(manager.submit(media_job_key("first"), failed))
        self.assertTrue(failed.failed_event.wait(2))
        self.assertEqual(manager.status(media_job_key("first")), "failed")
        self.assertIsInstance(failed.error, RuntimeError)

        self.assertTrue(manager.submit(media_job_key("second"), following))
        self.assertTrue(following.succeeded_event.wait(2))
        self.assertEqual(manager.status(media_job_key("second")), "succeeded")

    def test_abort_before_queued_job_cancels_without_running_it(self):
        manager = self.manager(max_pending=2)
        entered = threading.Event()
        release = threading.Event()

        def block_first(context):
            entered.set()
            self.assertTrue(release.wait(2))

        first = ControlledOperation([], block_first)
        operation = ControlledOperation([])
        key = media_job_key("pending")
        self.assertTrue(manager.submit(media_job_key("blocker"), first))
        self.assertTrue(entered.wait(2))
        self.assertTrue(manager.submit(key, operation))
        self.abort = True
        release.set()
        self.assertTrue(operation.cancelled_event.wait(2))
        self.assertEqual(manager.status(key), "cancelled")
        self.assertEqual(operation.events, ["cancelled"])
        self.assertEqual(len(self.sessions), 1)

    def test_abort_during_job_cancels_and_skips_stale_success_callback(self):
        manager = self.manager()
        entered = threading.Event()
        release = threading.Event()
        events = []

        def blocked(context):
            entered.set()
            self.assertTrue(release.wait(2))
            context.raise_if_cancelled()

        operation = ControlledOperation(events, blocked)
        key = media_job_key("movie")
        self.assertTrue(manager.submit(key, operation))
        self.assertTrue(entered.wait(2))
        self.abort = True
        release.set()
        self.assertTrue(operation.cancelled_event.wait(2))
        self.assertFalse(operation.succeeded_event.is_set())
        self.assertEqual(manager.status(key), "cancelled")
        self.assertEqual(events, ["run", "cancelled"])

    def test_context_can_turn_monitor_abort_into_cancellation(self):
        manager = self.manager()
        operation = ControlledOperation(
            [],
            lambda context: (_ for _ in ()).throw(DownloadJobCancelled()),
        )
        key = media_job_key("movie")
        self.assertTrue(manager.submit(key, operation))
        self.assertTrue(operation.cancelled_event.wait(2))
        self.assertEqual(manager.status(key), "cancelled")

    def test_idempotent_close_cancels_pending_jobs_and_rejects_new_work(self):
        manager = self.manager(max_pending=2)
        release = threading.Event()
        first = ControlledOperation([], lambda context: release.wait(2))
        pending = ControlledOperation([])
        first_key = media_job_key("first")
        pending_key = media_job_key("pending")
        self.assertTrue(manager.submit(first_key, first))
        self.assertTrue(manager.submit(pending_key, pending))
        manager.close(wait=False)
        manager.close(wait=False)
        self.assertTrue(pending.cancelled_event.wait(2))
        self.assertEqual(manager.status(pending_key), "cancelled")
        self.assertFalse(manager.submit(media_job_key("late"), ControlledOperation([])))
        release.set()
        self.assertTrue(first.cancelled_event.wait(2))
        self.assertFalse(first.succeeded_event.is_set())
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_each_job_owns_and_closes_a_distinct_worker_session(self):
        manager = self.manager()
        ui_session = FakeSession()
        seen_sessions = []
        for name in ("first", "second"):
            operation = ControlledOperation(
                [], lambda context: seen_sessions.append(context.session)
            )
            self.assertTrue(manager.submit(media_job_key(name), operation))
            self.assertTrue(operation.succeeded_event.wait(2))
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(seen_sessions, self.sessions)
        self.assertIsNot(seen_sessions[0], seen_sessions[1])
        self.assertNotIn(ui_session, seen_sessions)
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_rekey_rejects_an_already_reserved_media_destination(self):
        manager = self.manager(max_pending=2)
        entered = threading.Event()
        allow_rekey = threading.Event()
        target_key = media_job_key("same-final-name")
        target = ControlledOperation([])

        def collide(context):
            entered.set()
            self.assertTrue(allow_rekey.wait(2))
            context.rekey(target_key)

        colliding = ControlledOperation([], collide)
        colliding_key = media_job_key("initial-name")
        self.assertTrue(manager.submit(colliding_key, colliding))
        self.assertTrue(entered.wait(2))
        self.assertTrue(manager.submit(target_key, target))
        allow_rekey.set()
        self.assertTrue(colliding.failed_event.wait(2))
        self.assertEqual(manager.status(colliding_key), "failed")
        self.assertIn(manager.status(target_key), ("queued", "running", "succeeded"))
        self.assertTrue(target.succeeded_event.wait(2))

    def test_typed_keys_reject_unknown_job_families(self):
        with self.assertRaises(ValueError):
            DownloadJobKey("metadata", "movie")
        manager = self.manager()
        with self.assertRaises(ValueError):
            manager.submit(("media", "movie"), ControlledOperation([]))


if __name__ == "__main__":
    unittest.main()
