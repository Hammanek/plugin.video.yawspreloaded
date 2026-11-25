"""Dedicated bounded jobs for media downloads and profile database installs."""

from collections import deque
from dataclasses import dataclass
import logging
import os
import threading

from yawsp_app.runtime import is_vfs_path


_LOGGER = logging.getLogger(__name__)
_JOB_STATES = frozenset(("queued", "running", "succeeded", "failed", "cancelled"))


class DownloadJobCancelled(RuntimeError):
    """An operation observed manager shutdown or monitor cancellation."""


class DuplicateJobError(RuntimeError):
    """A media destination is already reserved by another running job."""


@dataclass(frozen=True)
class DownloadJobKey:
    """A typed, normalized reservation key shared by UI and worker threads."""

    kind: str
    identifier: str

    def __post_init__(self):
        if self.kind not in ("media", "backup_db"):
            raise ValueError("Unknown download job family: {0}".format(self.kind))
        if not isinstance(self.identifier, str) or not self.identifier:
            raise ValueError("Download job keys require a destination identifier")


def media_job_key(destination):
    destination = os.fspath(destination)
    if not is_vfs_path(destination):
        destination = os.path.normcase(os.path.realpath(destination))
    return DownloadJobKey("media", destination)


def backup_db_job_key(profile):
    profile = os.fspath(profile)
    if not is_vfs_path(profile):
        profile = os.path.normcase(os.path.realpath(profile))
    else:
        profile = profile.rstrip("/")
    return DownloadJobKey("backup_db", profile)


@dataclass(frozen=True)
class DownloadJobStatus:
    key: DownloadJobKey
    state: str

    def __post_init__(self):
        if self.state not in _JOB_STATES:
            raise ValueError("Unknown download job state: {0}".format(self.state))


class DownloadOperation:
    """Optional lifecycle callbacks for an operation accepted by the manager."""

    def run(self, context):  # pragma: no cover - interface definition
        raise NotImplementedError

    def succeeded(self, result):
        pass

    def failed(self, error):
        pass

    def cancelled(self):
        pass


@dataclass
class _Job:
    key: DownloadJobKey
    operation: object
    state: str = "queued"


class DownloadJobContext:
    """Worker-scoped session and cancellation/destination protocol."""

    def __init__(self, manager, job, session):
        self._manager = manager
        self._job = job
        self.session = session

    @property
    def key(self):
        return self._job.key

    @property
    def cancelled(self):
        return self._manager._is_cancelled(self._job)

    def raise_if_cancelled(self):
        if self.cancelled:
            raise DownloadJobCancelled()

    def rekey(self, key):
        """Atomically reserve the real media name obtained from remote info."""
        self._manager._rekey(self._job, key)


class DownloadJobManager:
    """Run each accepted operation on one dedicated, bounded worker thread."""

    def __init__(
        self,
        session_factory,
        abort=None,
        logger=None,
        max_pending=4,
    ):
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self._session_factory = session_factory
        self._abort = abort or (lambda: False)
        self._logger = logger or _LOGGER
        self._max_pending = max_pending
        self._condition = threading.Condition(threading.RLock())
        self._pending = deque()
        self._active = {}
        self._statuses = {}
        self._worker = None
        self._closed = False

    def _abort_requested(self):
        try:
            return bool(self._abort())
        except Exception:
            self._logger.exception("Download job abort monitor failed")
            return True

    def submit(self, key, operation):
        """Reserve and enqueue without executing any operation on the caller."""
        if not isinstance(key, DownloadJobKey):
            raise ValueError("Download jobs require a DownloadJobKey")
        runner = getattr(operation, "run", operation)
        if not callable(runner):
            raise ValueError("Download job operation must be callable")
        with self._condition:
            if self._closed or self._abort_requested():
                return False
            if key in self._active or len(self._active) >= self._max_pending:
                return False
            job = _Job(key=key, operation=operation)
            self._active[key] = job
            self._statuses[key] = "queued"
            self._pending.append(job)
            if self._worker is None or not self._worker.is_alive():
                previous_worker = self._worker
                self._worker = threading.Thread(
                    target=self._drain,
                    name="yawsp-download-jobs",
                    daemon=True,
                )
                try:
                    self._worker.start()
                except Exception:
                    self._pending.remove(job)
                    if self._active.get(key) is job:
                        del self._active[key]
                    self._statuses.pop(key, None)
                    self._worker = previous_worker
                    self._condition.notify_all()
                    self._logger.exception("Unable to start download job worker")
                    return False
            self._condition.notify_all()
            return True

    def status(self, key):
        if not isinstance(key, DownloadJobKey):
            raise ValueError("Download jobs require a DownloadJobKey")
        with self._condition:
            return self._statuses.get(key)

    def job_status(self, key):
        state = self.status(key)
        return DownloadJobStatus(key, state) if state is not None else None

    def _is_cancelled(self, job):
        with self._condition:
            return self._closed or job.state == "cancelled" or self._abort_requested()

    def _rekey(self, job, key):
        if not isinstance(key, DownloadJobKey) or key.kind != job.key.kind:
            raise ValueError("A job can only be rekeyed within its original family")
        with self._condition:
            if self._closed or job.state == "cancelled" or self._abort_requested():
                raise DownloadJobCancelled()
            if job.state != "running" or self._active.get(job.key) is not job:
                raise RuntimeError("Only the active operation can rekey its reservation")
            other = self._active.get(key)
            if other is not None and other is not job:
                raise DuplicateJobError(
                    "Download destination is already reserved: {0}".format(key.identifier)
                )
            old_key = job.key
            if key == old_key:
                return
            del self._active[old_key]
            self._statuses.pop(old_key, None)
            job.key = key
            self._active[key] = job
            self._statuses[key] = "running"

    @staticmethod
    def _succeeded(operation, result):
        callback = getattr(operation, "succeeded", None)
        if callback is not None:
            callback(result)

    @staticmethod
    def _failed(operation, error):
        callback = getattr(operation, "failed", None)
        if callback is not None:
            callback(error)

    @staticmethod
    def _cancelled(operation):
        callback = getattr(operation, "cancelled", None)
        if callback is not None:
            callback()

    def _terminal(self, job, state, result=None, error=None):
        with self._condition:
            if state == "succeeded" and (
                self._closed or job.state == "cancelled" or self._abort_requested()
            ):
                state = "cancelled"
            job.state = state
            self._statuses[job.key] = state
            self._active.pop(job.key, None)
            try:
                if state == "succeeded":
                    self._succeeded(job.operation, result)
                elif state == "failed":
                    self._failed(job.operation, error)
                else:
                    self._cancelled(job.operation)
            except Exception:
                self._logger.exception("Download job %s callback failed", state)
            self._condition.notify_all()

    def _run(self, job):
        session = None
        result = None
        error = None
        try:
            session = self._session_factory()
            context = DownloadJobContext(self, job, session)
            context.raise_if_cancelled()
            runner = getattr(job.operation, "run", job.operation)
            result = runner(context)
        except DownloadJobCancelled as cancellation:
            error = cancellation
        except Exception as exception:
            error = exception
            self._logger.exception("Download job failed for %s", job.key.identifier)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    self._logger.exception("Unable to close download job HTTP session")

        if error is None:
            self._terminal(job, "succeeded", result=result)
        elif isinstance(error, DownloadJobCancelled) or self._is_cancelled(job):
            self._terminal(job, "cancelled")
        else:
            self._terminal(job, "failed", error=error)

    def _drain(self):
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if not self._pending:
                    return
                job = self._pending.popleft()
                if self._closed or job.state == "cancelled" or self._abort_requested():
                    job.state = "cancelled"
                    self._statuses[job.key] = "cancelled"
                    self._active.pop(job.key, None)
                    try:
                        self._cancelled(job.operation)
                    except Exception:
                        self._logger.exception("Download job cancelled callback failed")
                    self._condition.notify_all()
                    if self._closed:
                        continue
                    continue
                job.state = "running"
                self._statuses[job.key] = "running"
            self._run(job)

    def close(self, wait=True):
        """Reject new work, cancel queued work, and optionally await running jobs."""
        with self._condition:
            self._closed = True
            while self._pending:
                job = self._pending.popleft()
                job.state = "cancelled"
                self._statuses[job.key] = "cancelled"
                self._active.pop(job.key, None)
                try:
                    self._cancelled(job.operation)
                except Exception:
                    self._logger.exception("Download job cancelled callback failed")
            self._condition.notify_all()
            if wait:
                while self._active:
                    self._condition.wait()
            worker = self._worker
        if wait and worker is not None and worker is not threading.current_thread():
            worker.join()
