"""OS-locked, epoch-fenced ownership for the bounded native TMDB service."""

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading

from yawsp_app import tmdb_service_protocol as protocol


SCHEMA_VERSION = 1
BACKGROUND_RESERVATIONS = 32
INTERACTIVE_RESERVATIONS = 1
BACKGROUND_BACKLOG = 64
INTERACTIVE_BACKLOG = 8
TOTAL_JOBS = 128
TOTAL_WAITERS = 128
UNWAITED_TERMINALS = 32
TOMBSTONES = 32
MAX_MANIFEST_BYTES = 262144
MAX_RESULT_BYTES = protocol.MAX_RESULT_CANONICAL_BYTES
MAX_AGGREGATE_RESULT_BYTES = 16777216
MAX_BATCH = 50

_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL = frozenset(("succeeded", "failed", "aborted"))
_NONTERMINAL = frozenset(("queued", "backlog", "active"))
_SERVICE_STATES = frozenset(("absent", "starting", "ready", "stopping"))
_MANIFEST_FIELDS = frozenset(
    ("schema", "service_state", "service_epoch", "next_ordinal", "last_metadata_family", "jobs", "tombstones")
)
_JOB_FIELDS = frozenset(
    (
        "job_id",
        "dedup_key",
        "request",
        "ordinal",
        "state",
        "service_epoch",
        "claim_token",
        "reservation",
        "interactive",
        "priority",
        "waiters",
        "result_ref",
        "attempts",
        "retry_at",
        "followup_of",
        "terminal_ordinal",
    )
)


def _json_encode(document, limit):
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as error:
        raise protocol.ProtocolError("document is not bounded canonical JSON") from error
    if len(encoded) > limit:
        raise protocol.ProtocolError("canonical document exceeds its byte bound")
    return encoded


ABORT_RESULT_BYTES = len(
    _json_encode(protocol.make_result("0" * 32, "aborted", "ABORTED"), MAX_RESULT_BYTES)
)
ABORT_RESERVE_BYTES = TOTAL_JOBS * ABORT_RESULT_BYTES


def _reject_duplicates(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise protocol.ProtocolError("duplicate JSON object field")
        document[key] = value
    return document


def _reject_constant(_value):
    raise protocol.ProtocolError("nonfinite JSON number")


def _decode(raw, limit):
    try:
        text = raw.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except protocol.ProtocolError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise protocol.ProtocolError("malformed JSON document") from error
    if _json_encode(document, limit) != raw:
        raise protocol.ProtocolError("document is not canonical or has trailing input")
    return document


def _safe_read(path, limit, allow_missing=False):
    try:
        information = os.lstat(path)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise protocol.ProtocolError("required state file is absent")
    if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
        raise protocol.ProtocolError("state path is not a regular nofollow file")
    if information.st_size > limit:
        raise protocol.ProtocolError("state file exceeds its byte bound")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise protocol.ProtocolError("state path is not a regular file")
        if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
            raise protocol.ProtocolError("state file changed during safe open")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 131072))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > limit:
        raise protocol.ProtocolError("state file exceeds its byte bound")
    return raw


def _unlink(path):
    try:
        information = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
        raise protocol.ProtocolError("refusing to unlink a nonregular state reference")
    os.unlink(path)


def _temp_file(path):
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    return os.open(path, flags, 0o600)


def _best_effort_unlink(path):
    try:
        _unlink(path)
    except (OSError, protocol.ProtocolError):
        pass


def _atomic_write(path, temporary, document, limit):
    encoded = _json_encode(document, limit)
    if os.path.lexists(temporary):
        _unlink(temporary)
    if os.path.lexists(path):
        information = os.lstat(path)
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
            raise protocol.ProtocolError("refusing to replace a nonregular state path")
    descriptor = _temp_file(temporary)
    closed = False
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short state-file write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        closed = True
        os.replace(temporary, path)
    finally:
        if not closed:
            os.close(descriptor)
        if os.path.lexists(temporary):
            _best_effort_unlink(temporary)
    raw = _safe_read(path, limit)
    if _decode(raw, limit) != document:
        raise protocol.ProtocolError("published document failed validated reread")
    return raw


class _WindowsFileLock:
    """A one-byte msvcrt lock. Only this adapter is exercised live on Windows."""

    def __init__(self, path):
        self.path = path
        self._descriptor = None
        self.acquired = False

    def _open(self):
        import msvcrt

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode):
                raise protocol.ProtocolError("lock path is not a regular file")
            if stat.S_ISLNK(os.lstat(self.path).st_mode):
                raise protocol.ProtocolError("lock path must not be a symlink")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if information.st_size == 0:
                os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, msvcrt

    def acquire(self, blocking=True):
        if self.acquired:
            return True
        import msvcrt

        descriptor, module = self._open()
        operation = module.LK_LOCK if blocking else module.LK_NBLCK
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            module.locking(descriptor, operation, 1)
        except OSError:
            os.close(descriptor)
            if blocking:
                raise
            return False
        self._descriptor = descriptor
        self.acquired = True
        return True

    def release(self):
        if not self.acquired:
            return
        descriptor = self._descriptor
        self._descriptor = None
        self.acquired = False
        try:
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)


class _PosixFileLock:
    """Statically testable fcntl adapter; live POSIX behavior is not claimed."""

    def __init__(self, path, fcntl_module=None):
        self.path = path
        self._descriptor = None
        self.acquired = False
        if fcntl_module is None:  # pragma: no cover - only selected on a POSIX host
            import fcntl as fcntl_module
        self._fcntl = fcntl_module

    def acquire(self, blocking=True):
        if self.acquired:
            return True
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode):
                raise protocol.ProtocolError("lock path is not a regular file")
            operation = self._fcntl.LOCK_EX
            if not blocking:
                operation |= self._fcntl.LOCK_NB
            self._fcntl.lockf(descriptor, operation)
        except OSError:
            os.close(descriptor)
            if blocking:
                raise
            return False
        except Exception:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self.acquired = True
        return True

    def release(self):
        if not self.acquired:
            return
        descriptor = self._descriptor
        self._descriptor = None
        self.acquired = False
        try:
            self._fcntl.lockf(descriptor, self._fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _lock_class():
    return _WindowsFileLock if os.name == "nt" else _PosixFileLock


def _ensure_directory(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except FileExistsError:
        pass
    information = os.lstat(path)
    if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
        raise protocol.ProtocolError("state directory is not a real directory")


_PROCESS_LOCKS = {}


def _new_lock(path):
    return _lock_class()(path)


def _process_lock(path):
    # This only avoids same-process msvcrt contention. The OS lock remains mandatory.
    return _PROCESS_LOCKS.setdefault(os.path.abspath(path), threading.RLock())


class ServiceLease:
    """The native consumer's lifetime lease; release can only release this handle."""

    def __init__(self, profile_root):
        self.root = profile_root
        self.state_dir = os.path.join(profile_root, "tmdb_service_state")
        self.path = os.path.join(self.state_dir, "lease.lock")
        self._lock = None
        self.acquired = False

    def acquire(self):
        if self.acquired:
            return True
        _ensure_directory(self.state_dir)
        lock = _new_lock(self.path)
        if not lock.acquire(blocking=False):
            return False
        self._lock = lock
        self.acquired = True
        return True

    def release(self):
        lock = self._lock
        self._lock = None
        self.acquired = False
        if lock is not None:
            lock.release()


def _empty_manifest():
    return {
        "schema": SCHEMA_VERSION,
        "service_state": "absent",
        "service_epoch": None,
        "next_ordinal": 1,
        "last_metadata_family": None,
        "jobs": [],
        "tombstones": [],
    }


def _exact(document, fields, name):
    if type(document) is not dict or set(document) != set(fields):
        raise protocol.ProtocolError(name + " has unknown or missing fields")


def _hex(value, expression, name):
    if type(value) is not str or expression.fullmatch(value) is None:
        raise protocol.ProtocolError(name + " is not a canonical identifier")
    return value


def _integer(value, minimum, name):
    if type(value) is not int or value < minimum:
        raise protocol.ProtocolError(name + " is not a valid integer")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise protocol.ProtocolError(name + " must be boolean")
    return value


def _job_id(value):
    return _hex(value, _HEX_32, "job_id")


def _epoch(value):
    return _hex(value, _HEX_32, "service_epoch")


def _claim_token(value):
    return _hex(value, _HEX_32, "claim_token")


def _result_reference(job_id):
    return {
        "path": "results/%s.json" % job_id,
        "temp_path": "results/%s.tmp" % job_id,
        "digest": None,
        "bytes": 0,
    }


def _validate_result_reference(reference, job_id, terminal):
    _exact(reference, ("path", "temp_path", "digest", "bytes"), "result_ref")
    if reference["path"] != "results/%s.json" % job_id:
        raise protocol.ProtocolError("result path is not deterministic")
    if reference["temp_path"] != "results/%s.tmp" % job_id:
        raise protocol.ProtocolError("result temp path is not deterministic")
    size = _integer(reference["bytes"], 0, "result bytes")
    if size > protocol.MAX_RESULT_CANONICAL_BYTES:
        raise protocol.ProtocolError("result reference exceeds its byte bound")
    digest = reference["digest"]
    if terminal:
        _hex(digest, _HEX_64, "result digest")
        if size == 0:
            raise protocol.ProtocolError("terminal result is empty")
    elif digest is not None or size != 0:
        raise protocol.ProtocolError("nonterminal result reference must be unpublished")


def _validate_job(job):
    _exact(job, _JOB_FIELDS, "job")
    _job_id(job["job_id"])
    _hex(job["dedup_key"], _HEX_64, "dedup_key")
    request = protocol.validate_job(job["request"])
    if request != job["request"] or protocol.dedup_key(request) != job["dedup_key"]:
        raise protocol.ProtocolError("job request/key is not canonical")
    _integer(job["ordinal"], 1, "ordinal")
    _epoch(job["service_epoch"])
    state = job["state"]
    if type(state) is not str or state not in _TERMINAL | _NONTERMINAL:
        raise protocol.ProtocolError("unknown job state")
    token = job["claim_token"]
    if token is not None:
        _claim_token(token)
    if state == "active" and token is None:
        raise protocol.ProtocolError("active job lacks a fencing token")
    if state in ("queued", "backlog") and token is not None:
        raise protocol.ProtocolError("unclaimed job has a fencing token")
    _boolean(job["reservation"], "reservation")
    _boolean(job["interactive"], "interactive")
    _boolean(job["priority"], "priority")
    if (state in ("queued", "active")) != job["reservation"]:
        raise protocol.ProtocolError("reservation ledger disagrees with job state")
    if type(job["waiters"]) is not list:
        raise protocol.ProtocolError("job waiters must be a list")
    if len(set(job["waiters"])) != len(job["waiters"]):
        raise protocol.ProtocolError("duplicate waiter on job")
    for waiter_id in job["waiters"]:
        _job_id(waiter_id)
    _validate_result_reference(job["result_ref"], job["job_id"], state in _TERMINAL)
    if job["attempts"] not in (0, 1) or type(job["attempts"]) is not int:
        raise protocol.ProtocolError("attempts must be exactly zero or one")
    retry_at = job["retry_at"]
    if isinstance(retry_at, bool) or not isinstance(retry_at, (int, float)):
        raise protocol.ProtocolError("retry_at must be finite")
    if not math.isfinite(retry_at) or retry_at < 0:
        raise protocol.ProtocolError("retry_at must be finite and nonnegative")
    followup = job["followup_of"]
    if followup is not None:
        _job_id(followup)
    terminal_ordinal = job["terminal_ordinal"]
    if state in _TERMINAL:
        _integer(terminal_ordinal, 1, "terminal_ordinal")
    elif terminal_ordinal is not None:
        raise protocol.ProtocolError("nonterminal job has terminal ordering")
    return job


def _validate_tombstone(entry):
    _exact(entry, ("job_id", "dedup_key", "ordinal", "result"), "tombstone")
    _job_id(entry["job_id"])
    _hex(entry["dedup_key"], _HEX_64, "tombstone dedup_key")
    _integer(entry["ordinal"], 1, "tombstone ordinal")
    decoded = protocol.validate_result(entry["result"])
    if decoded != entry["result"] or decoded["outcome"] != "failed":
        raise protocol.ProtocolError("invalid tombstone result")
    if decoded["error"]["code"] != "RESULT_EXPIRED" or decoded["job_id"] != entry["job_id"]:
        raise protocol.ProtocolError("tombstone must be typed RESULT_EXPIRED")
    return entry


def _validate_manifest(document, quarantine=False):
    _exact(document, _MANIFEST_FIELDS, "manifest")
    if type(document["schema"]) is not int or document["schema"] != SCHEMA_VERSION:
        raise protocol.ProtocolError("unsupported manifest schema")
    if document["service_state"] not in _SERVICE_STATES or type(document["service_state"]) is not str:
        raise protocol.ProtocolError("unknown service state")
    epoch = document["service_epoch"]
    if epoch is not None:
        _epoch(epoch)
    _integer(document["next_ordinal"], 1, "next_ordinal")
    family = document["last_metadata_family"]
    if family not in (None, "trailer", "show"):
        raise protocol.ProtocolError("unknown metadata family")
    jobs = document["jobs"]
    tombstones = document["tombstones"]
    if type(jobs) is not list or type(tombstones) is not list:
        raise protocol.ProtocolError("manifest collections must be lists")
    if len(jobs) > TOTAL_JOBS or len(tombstones) > TOMBSTONES:
        raise protocol.ProtocolError("manifest collection exceeds its bound")
    valid_jobs = []
    identifiers = set()
    nonterminal_keys = set()
    for job in jobs:
        try:
            _validate_job(job)
            if job["job_id"] in identifiers:
                raise protocol.ProtocolError("duplicate job identifier")
            if job["state"] in _NONTERMINAL and job["dedup_key"] in nonterminal_keys:
                raise protocol.ProtocolError("duplicate nonterminal key owner")
        except (protocol.ProtocolError, KeyError, TypeError, ValueError, OverflowError):
            if not quarantine:
                raise
            continue
        identifiers.add(job["job_id"])
        if job["state"] in _NONTERMINAL:
            nonterminal_keys.add(job["dedup_key"])
        valid_jobs.append(job)
    if quarantine:
        document["jobs"] = valid_jobs
    valid_tombstones = []
    for entry in tombstones:
        try:
            _validate_tombstone(entry)
        except (protocol.ProtocolError, KeyError, TypeError, ValueError, OverflowError):
            if not quarantine:
                raise
            continue
        valid_tombstones.append(entry)
    if quarantine:
        document["tombstones"] = valid_tombstones
    if document["next_ordinal"] <= max([job["ordinal"] for job in valid_jobs] + [0]):
        raise protocol.ProtocolError("job ordinal ledger is not monotonic")
    terminal_order = [job["terminal_ordinal"] for job in valid_jobs if job["state"] in _TERMINAL]
    terminal_order.extend(entry["ordinal"] for entry in valid_tombstones)
    if len(terminal_order) != len(set(terminal_order)):
        raise protocol.ProtocolError("terminal ordinal ledger is not monotonic")
    reservations = [job for job in valid_jobs if job["reservation"]]
    if sum(not job["interactive"] for job in reservations) > BACKGROUND_RESERVATIONS:
        raise protocol.ProtocolError("too many background reservations")
    if sum(job["interactive"] for job in reservations) > INTERACTIVE_RESERVATIONS:
        raise protocol.ProtocolError("too many interactive reservations")
    if sum(job["state"] == "backlog" and not job["interactive"] for job in valid_jobs) > BACKGROUND_BACKLOG:
        raise protocol.ProtocolError("background backlog exceeds its bound")
    if sum(job["state"] == "backlog" and job["interactive"] for job in valid_jobs) > INTERACTIVE_BACKLOG:
        raise protocol.ProtocolError("interactive backlog exceeds its bound")
    if sum(len(job["waiters"]) for job in valid_jobs) > TOTAL_WAITERS:
        raise protocol.ProtocolError("waiter bound exceeded")
    if sum(job["state"] in _TERMINAL and not job["waiters"] for job in valid_jobs) > UNWAITED_TERMINALS:
        raise protocol.ProtocolError("unwaited terminal bound exceeded")
    if sum(job["result_ref"]["bytes"] for job in valid_jobs if job["state"] in _TERMINAL) > MAX_AGGREGATE_RESULT_BYTES:
        raise protocol.ProtocolError("aggregate terminal result bound exceeded")
    return document


class IdentityTransactionAdapter:
    """Enter a fresh real OS identity transaction for one invocation."""

    def __init__(self, owner):
        self.owner = owner
        self._local = threading.local()

    def __enter__(self):
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            return self._local.entered
        if self.owner is None:
            manager = contextlib.nullcontext()
        elif hasattr(self.owner, "identity_transaction"):
            manager = self.owner.identity_transaction()
        elif hasattr(self.owner, "__enter__"):
            manager = self.owner
        else:
            manager = self.owner()
        self._local.manager = manager
        self._local.entered = manager.__enter__()
        self._local.depth = 1
        return self._local.entered

    def __exit__(self, exc_type, exc_value, traceback):
        depth = self._local.depth - 1
        self._local.depth = depth
        if depth:
            return False
        manager = self._local.manager
        self._local.manager = None
        self._local.entered = None
        return manager.__exit__(exc_type, exc_value, traceback)


class ServiceStateStore:
    """All persistent operations are serialized by the profile transaction lock."""

    def __init__(self, profile_root, id_factory=None):
        self.root = profile_root
        self.state_dir = os.path.join(profile_root, "tmdb_service_state")
        self.results_dir = os.path.join(self.state_dir, "results")
        self.manifest_path = os.path.join(self.state_dir, "manifest.json")
        self.manifest_temp = os.path.join(self.state_dir, "manifest.json.tmp")
        self.transaction_path = os.path.join(self.state_dir, "transaction.lock")
        self.identity_path = os.path.join(self.state_dir, "identity.lock")
        self.lease = ServiceLease(profile_root)
        self._local = threading.RLock()
        self._id_factory = id_factory or secrets.token_hex

    def _layout(self):
        _ensure_directory(self.state_dir)
        _ensure_directory(self.results_dir)

    @contextlib.contextmanager
    def _locked(self):
        self._layout()
        with _process_lock(self.transaction_path):
            lock = _new_lock(self.transaction_path)
            lock.acquire(blocking=True)
            try:
                yield
            finally:
                lock.release()

    def _load(self):
        raw = _safe_read(self.manifest_path, MAX_MANIFEST_BYTES, allow_missing=True)
        if raw is None:
            return _empty_manifest()
        document = _decode(raw, MAX_MANIFEST_BYTES)
        return _validate_manifest(document, quarantine=True)

    def _commit_hook(self, _document):
        """Test-controlled scheduling hook; production commits have no callback."""

    def _write_manifest(self, document):
        _validate_manifest(document)
        self._commit_hook(document)
        raw = _atomic_write(self.manifest_path, self.manifest_temp, document, MAX_MANIFEST_BYTES)
        reread = _validate_manifest(_decode(raw, MAX_MANIFEST_BYTES))
        if reread != document:
            raise protocol.ProtocolError("manifest reread differs from committed successor")

    def _reconcile_result_cleanup(self, previous, committed):
        remaining = {job["job_id"] for job in committed["jobs"]}
        for job in previous["jobs"]:
            if job["job_id"] in remaining:
                continue
            # Commit/reread is authoritative before any old result byte is removed.
            _best_effort_unlink(self._temp_result_path(job["result_ref"]))
            _best_effort_unlink(self._result_path(job["result_ref"]))

    def _transaction(self, mutation):
        with self._local:
            with self._locked():
                document = self._load()
                candidate = copy.deepcopy(document)
                value = mutation(candidate)
                if candidate != document:
                    self._write_manifest(candidate)
                    self._reconcile_result_cleanup(document, candidate)
                return copy.deepcopy(value)

    def _read_transaction(self, reader):
        with self._local:
            with self._locked():
                document = self._load()
                return copy.deepcopy(reader(document))

    @contextlib.contextmanager
    def identity_transaction(self):
        """Serialize only local identity compare-and-set, never network waits."""

        self._layout()
        with _process_lock(self.identity_path):
            lock = _new_lock(self.identity_path)
            lock.acquire(blocking=True)
            try:
                yield self
            finally:
                lock.release()

    def _lease_is_live(self):
        contender = ServiceLease(self.root)
        if contender.acquire():
            contender.release()
            return False
        return True

    @staticmethod
    def _unavailable_code(document):
        if document["service_state"] == "stopping":
            return "SHUTTING_DOWN"
        return "UNAVAILABLE"

    def service_ready(self):
        def read(document):
            return (
                document["service_state"] == "ready"
                and document["service_epoch"] is not None
                and self._lease_is_live()
            )

        try:
            return bool(self._read_transaction(read))
        except (FileNotFoundError, protocol.ProtocolError):
            return False

    @staticmethod
    def _find_job(document, job_id):
        for job in document["jobs"]:
            if job["job_id"] == job_id:
                return job
        return None

    @staticmethod
    def _matches_claim(job, claim):
        try:
            job_id = _job_id(claim["job_id"])
            epoch = _epoch(claim["service_epoch"])
            token = _claim_token(claim["claim_token"])
        except (KeyError, TypeError, protocol.ProtocolError) as error:
            raise protocol.ProtocolError("invalid fenced claim") from error
        return (
            job is not None
            and job["job_id"] == job_id
            and job["service_epoch"] == epoch
            and job["claim_token"] == token
        )

    def _new_identifier(self, document):
        used = {job["job_id"] for job in document["jobs"]}
        used.update(entry["job_id"] for entry in document["tombstones"])
        for _attempt in range(128):
            value = _job_id(self._id_factory(16))
            if value not in used:
                return value
        raise protocol.ProtocolError("could not allocate a unique job identifier")

    def _append_job(
        self,
        document,
        request,
        interactive,
        waiter_id,
        followup_of=None,
        ledger=None,
    ):
        key = protocol.dedup_key(request)
        ordinal = document["next_ordinal"]
        document["next_ordinal"] += 1
        job_id = self._new_identifier(document)
        job = {
            "job_id": job_id,
            "dedup_key": key,
            "request": copy.deepcopy(request),
            "ordinal": ordinal,
            "state": "queued",
            "service_epoch": document["service_epoch"],
            "claim_token": None,
            "reservation": True,
            "interactive": interactive,
            "priority": False,
            "waiters": [],
            "result_ref": _result_reference(job_id),
            "attempts": 0,
            "retry_at": 0.0,
            "followup_of": followup_of,
            "terminal_ordinal": None,
        }
        cap = INTERACTIVE_RESERVATIONS if interactive else BACKGROUND_RESERVATIONS
        backlog_cap = INTERACTIVE_BACKLOG if interactive else BACKGROUND_BACKLOG
        reservations = (
            sum(item["reservation"] and item["interactive"] == interactive for item in document["jobs"])
            if ledger is None
            else ledger["reservations"][interactive]
        )
        if reservations >= cap:
            backlog = (
                sum(item["state"] == "backlog" and item["interactive"] == interactive for item in document["jobs"])
                if ledger is None
                else ledger["backlog"][interactive]
            )
            if backlog >= backlog_cap:
                document["next_ordinal"] -= 1
                return None, "BACKPRESSURED"
            job["state"] = "backlog"
            job["reservation"] = False
            if ledger is not None:
                ledger["backlog"][interactive] += 1
        elif ledger is not None:
            ledger["reservations"][interactive] += 1
        if waiter_id is not None:
            job["waiters"].append(waiter_id)
        document["jobs"].append(job)
        return job, None

    @staticmethod
    def _waiter_count(document):
        return sum(len(job["waiters"]) for job in document["jobs"])

    @staticmethod
    def _collision(document, key):
        candidates = [
            job
            for job in document["jobs"]
            if job["dedup_key"] == key and (job["state"] in _NONTERMINAL or job["state"] in ("succeeded", "failed"))
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda job: job["terminal_ordinal"] or job["ordinal"])

    @staticmethod
    def _row_backpressured(request, code):
        return {"status": "backpressured", "request": copy.deepcopy(request), "code": code}

    @staticmethod
    def _row_joined(job):
        return {"status": "joined", "job_id": job["job_id"], "request": copy.deepcopy(job["request"])}

    @staticmethod
    def _row_accepted(job):
        return {
            "status": "accepted",
            "job_id": job["job_id"],
            "request": copy.deepcopy(job["request"]),
            "reservation": job["reservation"],
        }

    @staticmethod
    def _fits(document):
        try:
            _validate_manifest(document)
            _json_encode(document, MAX_MANIFEST_BYTES)
        except protocol.ProtocolError:
            return False
        return True

    def _guard_lease(self, document):
        if document["service_state"] != "ready" or document["service_epoch"] is None:
            return self._unavailable_code(document)
        if not self._lease_is_live():
            return "UNAVAILABLE"
        return None

    def submit(self, requests, interactive=False, waiter_id=None):
        if type(interactive) is not bool:
            raise protocol.ProtocolError("interactive must be boolean")
        if type(requests) is not list or len(requests) > MAX_BATCH:
            raise protocol.ProtocolError("submission batch must contain at most 50 requests")
        if waiter_id is not None:
            _job_id(waiter_id)
        if interactive and waiter_id is None:
            raise protocol.ProtocolError("interactive submission requires a registered waiter")
        validated = []
        for item in requests:
            try:
                request = protocol.validate_job(item)
            except protocol.ProtocolError:
                validated.append((None, item))
            else:
                validated.append((request, request))
        if not validated:
            return []

        def mutate(document):
            unavailable = self._guard_lease(document)
            if unavailable is not None:
                code = unavailable
                return [self._row_backpressured(original, code) for _request, original in validated]
            self._collect(document)
            ledger = {
                "reservations": {
                    kind: sum(
                        job["reservation"] and job["interactive"] == kind
                        for job in document["jobs"]
                    )
                    for kind in (False, True)
                },
                "backlog": {
                    kind: sum(
                        job["state"] == "backlog" and job["interactive"] == kind
                        for job in document["jobs"]
                    )
                    for kind in (False, True)
                },
            }
            waiter_count = sum(len(job["waiters"]) for job in document["jobs"])
            aggregate_bytes = self._aggregate_result_bytes(document)
            nonterminal_count = sum(job["state"] in _NONTERMINAL for job in document["jobs"])
            manifest_bytes = len(_json_encode(document, MAX_MANIFEST_BYTES))

            def item_size(collection, item):
                return len(_json_encode(item, MAX_MANIFEST_BYTES)) + bool(collection)

            def evict(job):
                nonlocal aggregate_bytes, nonterminal_count, manifest_bytes
                index = document["jobs"].index(job)
                tombstones_before = list(document["tombstones"])
                manifest_bytes -= item_size(document["jobs"][:-1], job)
                if job["state"] in _TERMINAL:
                    aggregate_bytes -= job["result_ref"]["bytes"]
                else:
                    nonterminal_count -= 1
                document["jobs"].remove(job)
                tombstone = {
                    "job_id": job["job_id"],
                    "dedup_key": job["dedup_key"],
                    "ordinal": job["terminal_ordinal"],
                    "result": protocol.make_result(job["job_id"], "failed", "RESULT_EXPIRED"),
                }
                manifest_bytes += item_size(document["tombstones"], tombstone)
                document["tombstones"].append(tombstone)
                document["tombstones"].sort(key=lambda entry: entry["ordinal"])
                if len(document["tombstones"]) > TOMBSTONES:
                    dropped = document["tombstones"][0]
                    manifest_bytes -= item_size(document["tombstones"][:-1], dropped)
                    del document["tombstones"][0]
                evicted.append((index, job, tombstones_before))

            def admission_room():
                while aggregate_bytes + nonterminal_count * ABORT_RESULT_BYTES + ABORT_RESULT_BYTES > MAX_AGGREGATE_RESULT_BYTES:
                    unwaited = sorted(
                        (
                            job
                            for job in document["jobs"]
                            if job["state"] in _TERMINAL and not job["waiters"]
                        ),
                        key=lambda job: job["terminal_ordinal"],
                    )
                    if not unwaited:
                        return False
                    evict(unwaited[0])
                return True

            rows = []
            for request, original in validated:
                if request is None:
                    rows.append(self._row_backpressured(original, "FAILED"))
                    continue
                before = (
                    dict(ledger["reservations"]),
                    dict(ledger["backlog"]),
                    waiter_count,
                    aggregate_bytes,
                    nonterminal_count,
                    document["next_ordinal"],
                    manifest_bytes,
                )
                collision = None
                old_waiter_count = old_priority = None
                appended = None
                evicted = []

                def rollback():
                    nonlocal waiter_count, aggregate_bytes, nonterminal_count, manifest_bytes
                    document["next_ordinal"] = before[5]
                    if appended is not None and appended in document["jobs"]:
                        document["jobs"].remove(appended)
                    if collision is not None:
                        del collision["waiters"][old_waiter_count:]
                        collision["priority"] = old_priority
                    if evicted:
                        document["tombstones"][:] = evicted[0][2]
                        for index, job, _tombstones in reversed(evicted):
                            document["jobs"].insert(index, job)
                    ledger["reservations"] = before[0]
                    ledger["backlog"] = before[1]
                    waiter_count, aggregate_bytes, nonterminal_count, manifest_bytes = before[2:5] + (before[6],)

                try:
                    key = protocol.dedup_key(request)
                    collision = self._collision(document, key)
                    if collision is not None:
                        collision_bytes_before = len(
                            _json_encode(collision, MAX_MANIFEST_BYTES)
                        )
                        old_waiter_count = len(collision["waiters"])
                        old_priority = collision["priority"]
                        if waiter_id is not None and waiter_id not in collision["waiters"]:
                            if waiter_count >= TOTAL_WAITERS:
                                rollback()
                                rows.append(self._row_backpressured(request, "BACKPRESSURED"))
                                continue
                            collision["waiters"].append(waiter_id)
                            waiter_count += 1
                        if waiter_id is not None and collision["state"] in _NONTERMINAL:
                            collision["priority"] = True
                        row = self._row_joined(collision)
                        manifest_bytes += (
                            len(_json_encode(collision, MAX_MANIFEST_BYTES))
                            - collision_bytes_before
                        )
                    else:
                        collision = None
                        if len(document["jobs"]) >= TOTAL_JOBS or not admission_room():
                            rollback()
                            rows.append(self._row_backpressured(request, "BACKPRESSURED"))
                            continue
                        ordinal_before = document["next_ordinal"]
                        appended, code = self._append_job(
                            document,
                            request,
                            interactive,
                            waiter_id,
                            ledger=ledger,
                        )
                        manifest_bytes += len(str(document["next_ordinal"])) - len(str(ordinal_before))
                        if appended is None:
                            rollback()
                            rows.append(self._row_backpressured(request, code))
                            continue
                        manifest_bytes += item_size(document["jobs"][:-1], appended)
                        if waiter_id is not None:
                            waiter_count += 1
                        nonterminal_count += 1
                        row = self._row_accepted(appended)
                    if manifest_bytes > MAX_MANIFEST_BYTES:
                        rollback()
                        rows.append(self._row_backpressured(request, "BACKPRESSURED"))
                    else:
                        rows.append(row)
                except protocol.ProtocolError as error:
                    rollback()
                    code = (
                        "BACKPRESSURED"
                        if str(error) == "canonical document exceeds its byte bound"
                        else "FAILED"
                    )
                    rows.append(self._row_backpressured(original, code))
            if not self._fits(document):
                raise protocol.ProtocolError("final admission candidate exceeds its bounds")
            return rows

        return self._transaction(mutate)

    def join(self, request, waiter_id):
        _job_id(waiter_id)
        return self.submit([request], interactive=True, waiter_id=waiter_id)[0]

    @staticmethod
    def _family(job):
        operation = job["request"]["operation"]
        if operation == "metadata.trailer":
            return "trailer"
        if operation == "metadata.show":
            return "show"
        return None

    def _promote(self, document):
        if document["service_state"] != "ready":
            return
        for interactive, cap, backlog_cap in (
            (True, INTERACTIVE_RESERVATIONS, INTERACTIVE_BACKLOG),
            (False, BACKGROUND_RESERVATIONS, BACKGROUND_BACKLOG),
        ):
            used = sum(job["reservation"] and job["interactive"] == interactive for job in document["jobs"])
            waiting = sorted(
                (job for job in document["jobs"] if job["state"] == "backlog" and job["interactive"] == interactive),
                key=lambda job: job["ordinal"],
            )
            for job in waiting[: max(0, cap - used)]:
                job["state"] = "queued"
                job["reservation"] = True

    def _choose(self, document):
        candidates = [job for job in document["jobs"] if job["state"] == "queued" and job["service_epoch"] == document["service_epoch"]]
        if not candidates:
            return None
        rank = min(0 if job["interactive"] else 1 if job["priority"] else 2 for job in candidates)
        candidates = [job for job in candidates if (0 if job["interactive"] else 1 if job["priority"] else 2) == rank]
        metadata = [job for job in candidates if self._family(job) is not None]
        if metadata:
            families = {self._family(job) for job in metadata}
            if len(families) == 2:
                wanted = "show" if document["last_metadata_family"] == "trailer" else "trailer"
                metadata = [job for job in metadata if self._family(job) == wanted]
            return min(metadata, key=lambda job: job["ordinal"])
        return min(candidates, key=lambda job: job["ordinal"])

    @staticmethod
    def _claim_document(job, token):
        return {
            "job_id": job["job_id"],
            "dedup_key": job["dedup_key"],
            "request": copy.deepcopy(job["request"]),
            "service_epoch": job["service_epoch"],
            "claim_token": token,
            "ordinal": job["ordinal"],
            "attempts": job["attempts"],
            "retry_at": job["retry_at"],
        }

    def claim_next(self, epoch, claim_token):
        _epoch(epoch)
        _claim_token(claim_token)

        def mutate(document):
            if not self.lease.acquired or document["service_epoch"] != epoch:
                return None
            self._collect(document)
            if document["service_state"] != "ready":
                return None
            if any(job["state"] == "active" and job["service_epoch"] == epoch for job in document["jobs"]):
                return None
            self._promote(document)
            job = self._choose(document)
            if job is None:
                return None
            job["state"] = "active"
            job["reservation"] = True
            job["claim_token"] = claim_token
            family = self._family(job)
            if family is not None:
                document["last_metadata_family"] = family
            return self._claim_document(job, claim_token)

        return self._transaction(mutate)

    def _result_path(self, reference):
        # References are strictly validated before path components reach the filesystem.
        return os.path.join(self.state_dir, reference["path"])

    def _temp_result_path(self, reference):
        return os.path.join(self.state_dir, reference["temp_path"])

    def _read_result(self, job):
        reference = job["result_ref"]
        raw = _safe_read(self._result_path(reference), protocol.MAX_RESULT_CANONICAL_BYTES)
        if len(raw) != reference["bytes"] or hashlib.sha256(raw).hexdigest() != reference["digest"]:
            raise protocol.ProtocolError("result bytes or digest do not match manifest ownership")
        decoded = protocol.validate_result(raw)
        if decoded["job_id"] != job["job_id"]:
            raise protocol.ProtocolError("result belongs to another job")
        return decoded

    def _publish_result(self, job, result_document):
        reference = job["result_ref"]
        raw = _atomic_write(
            self._result_path(reference),
            self._temp_result_path(reference),
            result_document,
            protocol.MAX_RESULT_CANONICAL_BYTES,
        )
        validated = protocol.validate_result(raw)
        if validated != result_document or validated["job_id"] != job["job_id"]:
            raise protocol.ProtocolError("published result failed validated reread")
        reference["digest"] = hashlib.sha256(raw).hexdigest()
        reference["bytes"] = len(raw)

    def _evict(self, document, job):
        document["jobs"].remove(job)
        document["tombstones"].append(
            {
                "job_id": job["job_id"],
                "dedup_key": job["dedup_key"],
                "ordinal": job["terminal_ordinal"],
                "result": protocol.make_result(job["job_id"], "failed", "RESULT_EXPIRED"),
            }
        )
        document["tombstones"].sort(key=lambda entry: entry["ordinal"])
        del document["tombstones"][:-TOMBSTONES]

    def _collect(self, document):
        while True:
            terminal = [job for job in document["jobs"] if job["state"] in _TERMINAL]
            unwaited = sorted(
                (job for job in terminal if not job["waiters"]),
                key=lambda job: job["terminal_ordinal"],
            )
            aggregate = sum(job["result_ref"]["bytes"] for job in terminal)
            if len(unwaited) <= UNWAITED_TERMINALS and aggregate <= MAX_AGGREGATE_RESULT_BYTES:
                break
            if not unwaited:
                break
            self._evict(document, unwaited[0])

    @staticmethod
    def _aggregate_result_bytes(document):
        return sum(
            job["result_ref"]["bytes"]
            for job in document["jobs"]
            if job["state"] in _TERMINAL
        )

    @staticmethod
    def _pending_abort_bytes(document, excluding=None):
        return ABORT_RESULT_BYTES * sum(
            job["state"] in _NONTERMINAL and job["job_id"] != excluding
            for job in document["jobs"]
        )

    def _make_admission_room(self, document):
        while self._aggregate_result_bytes(document) + self._pending_abort_bytes(document) + ABORT_RESULT_BYTES > MAX_AGGREGATE_RESULT_BYTES:
            unwaited = sorted(
                (job for job in document["jobs"] if job["state"] in _TERMINAL and not job["waiters"]),
                key=lambda job: job["terminal_ordinal"],
            )
            if not unwaited:
                return False
            self._evict(document, unwaited[0])
        return True

    def _make_room(self, document, incoming_bytes, incoming_waited, incoming_job, aborting):
        while True:
            terminal = [job for job in document["jobs"] if job["state"] in _TERMINAL]
            unwaited = sorted(
                (job for job in terminal if not job["waiters"]),
                key=lambda job: job["terminal_ordinal"],
            )
            aggregate = sum(job["result_ref"]["bytes"] for job in terminal)
            count_fits = len(unwaited) + (0 if incoming_waited else 1) <= UNWAITED_TERMINALS
            if aborting:
                reserve = self._pending_abort_bytes(document, excluding=incoming_job)
            else:
                reserve = ABORT_RESERVE_BYTES
            bytes_fit = aggregate + incoming_bytes + reserve <= MAX_AGGREGATE_RESULT_BYTES
            if count_fits and bytes_fit:
                return
            if not unwaited:
                raise protocol.ProtocolError("BACKPRESSURED: registered live results exhaust retention bounds")
            self._evict(document, unwaited[0])

    def _terminalize(self, document, job, result_document, promote, bounded_recovery=False):
        raw = _json_encode(result_document, protocol.MAX_RESULT_CANONICAL_BYTES)
        aborting = result_document["outcome"] == "aborted" or bounded_recovery
        self._make_room(document, len(raw), bool(job["waiters"]), job["job_id"], aborting)
        self._publish_result(job, result_document)
        job["state"] = result_document["outcome"]
        job["reservation"] = False
        terminal_order = [item["terminal_ordinal"] for item in document["jobs"] if item is not job and item["state"] in _TERMINAL]
        terminal_order.extend(item["ordinal"] for item in document["tombstones"])
        job["terminal_ordinal"] = max(terminal_order + [0]) + 1
        if promote:
            self._promote(document)
        self._collect(document)

    def complete(self, claim, result_document):
        validated = protocol.validate_result(result_document)

        def mutate(document):
            try:
                job_id = _job_id(claim["job_id"])
                epoch = _epoch(claim["service_epoch"])
                token = _claim_token(claim["claim_token"])
            except (KeyError, TypeError, protocol.ProtocolError) as error:
                raise protocol.ProtocolError("invalid fenced claim") from error
            if validated["job_id"] != job_id:
                raise protocol.ProtocolError("result job_id does not match claim")
            job = self._find_job(document, job_id)
            if not self._matches_claim(job, claim) or job["service_epoch"] != epoch or job["claim_token"] != token:
                raise protocol.ProtocolError("stale or foreign terminal ownership")
            if job["state"] in _TERMINAL:
                if self._read_result(job) == validated:
                    return validated
                raise protocol.ProtocolError("conflicting duplicate terminal transition")
            if job["state"] != "active":
                raise protocol.ProtocolError("only an active claim can complete")
            self._terminalize(document, job, validated, promote=True)
            return validated

        completed = self._transaction(mutate)
        job_id = completed["job_id"]
        _best_effort_unlink(os.path.join(self.results_dir, job_id + ".tmp"))
        return completed

    def reconcile_completion(self, claim, code="BACKPRESSURED"):
        """Resolve only the current fenced active claim after publication failure.

        If a former completion already committed, its result remains authoritative.
        Otherwise one bounded typed terminal releases the reservation using the
        same exact pending-abort budget as owner recovery.
        """

        def mutate(document):
            try:
                job_id = _job_id(claim["job_id"])
                epoch = _epoch(claim["service_epoch"])
                _claim_token(claim["claim_token"])
            except (KeyError, TypeError, protocol.ProtocolError) as error:
                raise protocol.ProtocolError("invalid fenced claim") from error
            job = self._find_job(document, job_id)
            if not self._matches_claim(job, claim) or job["service_epoch"] != epoch:
                raise protocol.ProtocolError("stale or foreign completion ownership")
            if job["state"] in _TERMINAL:
                return self._read_result(job)
            if job["state"] != "active":
                raise protocol.ProtocolError("only an active claim can be reconciled")
            outcome = "aborted" if code == "ABORTED" else "failed"
            failed = protocol.make_result(job_id, outcome, code)
            self._terminalize(
                document, job, failed, promote=True, bounded_recovery=True
            )
            return failed

        return self._transaction(mutate)

    def abort_claim(self, claim, code):
        if code == "ABORTED":
            outcome = "aborted"
            value = "ABORTED"
        else:
            outcome = "failed"
            value = code
        # Validates both the fixed error code and the job ID before any transition.
        job_id = _job_id(claim.get("job_id"))
        aborted = protocol.make_result(job_id, outcome, value)
        self.complete(claim, aborted)
        return None

    def ensure_followup(self, claim, request):
        request = protocol.validate_job(request)
        if request["operation"] != "watchlist.refresh":
            raise protocol.ProtocolError("only watchlist refresh can require a follow-up")

        def mutate(document):
            job = self._find_job(document, claim.get("job_id"))
            if not self._matches_claim(job, claim) or job["state"] != "active":
                raise protocol.ProtocolError("stale or foreign follow-up ownership")
            existing = [
                item
                for item in document["jobs"]
                if item["followup_of"] == job["job_id"] and item["state"] in _NONTERMINAL
            ]
            if existing:
                followup = min(existing, key=lambda item: item["ordinal"])
                before = copy.deepcopy(document)
                force = bool(request["force"] or followup["request"]["force"])
                newer = copy.deepcopy(request)
                if request["generation"] < followup["request"]["generation"]:
                    newer = copy.deepcopy(followup["request"])
                newer["generation"] = max(newer["generation"], followup["request"]["generation"])
                newer["force"] = force
                newer = protocol.validate_job(newer)
                followup["request"] = newer
                followup["dedup_key"] = protocol.dedup_key(newer)
                if not self._fits(document):
                    document.clear()
                    document.update(before)
                    return self._row_backpressured(request, "BACKPRESSURED")
                return self._row_joined(followup)
            collision = self._collision(document, protocol.dedup_key(request))
            if collision is not None and collision["job_id"] != job["job_id"]:
                if collision["state"] in _NONTERMINAL:
                    if collision["followup_of"] not in (None, job["job_id"]):
                        raise protocol.ProtocolError("request is already owned by another continuation")
                    collision["followup_of"] = job["job_id"]
                return self._row_joined(collision)
            before = copy.deepcopy(document)
            if len(document["jobs"]) >= TOTAL_JOBS or not self._make_admission_room(document):
                document.clear()
                document.update(before)
                return self._row_backpressured(request, "BACKPRESSURED")
            followup, code = self._append_job(
                document, request, interactive=False, waiter_id=None, followup_of=job["job_id"]
            )
            if followup is None:
                document.clear()
                document.update(before)
                return self._row_backpressured(request, code)
            if not self._fits(document):
                document.clear()
                document.update(before)
                return self._row_backpressured(request, "BACKPRESSURED")
            return self._row_accepted(followup)

        return self._transaction(mutate)

    def recover(self, epoch):
        _epoch(epoch)
        if not self.lease.acquired:
            raise protocol.ProtocolError("recovery requires the lifetime service lease")

        def mutate(document):
            if document["service_epoch"] == epoch:
                raise protocol.ProtocolError("recovery requires a fresh service epoch")
            document["service_state"] = "starting"
            document["service_epoch"] = epoch
            document["last_metadata_family"] = None
            old_work = [
                job
                for job in list(document["jobs"])
                if job["state"] in _NONTERMINAL and job["service_epoch"] != epoch
            ]
            for job in old_work:
                if self._find_job(document, job["job_id"]) is None:
                    continue
                aborted = protocol.make_result(job["job_id"], "aborted", "ABORTED")
                self._terminalize(document, job, aborted, promote=False)
            self._collect(document)
            return None

        return self._transaction(mutate)

    def set_ready(self, epoch):
        _epoch(epoch)
        if not self.lease.acquired:
            raise protocol.ProtocolError("ready publication requires the lifetime service lease")

        def mutate(document):
            if document["service_epoch"] != epoch or document["service_state"] != "starting":
                raise protocol.ProtocolError("ready publication is outside its fresh recovery barrier")
            self._promote(document)
            document["service_state"] = "ready"
            return None

        return self._transaction(mutate)

    def set_stopping(self, epoch):
        _epoch(epoch)
        if not self.lease.acquired:
            raise protocol.ProtocolError("stopping requires the lifetime service lease")

        def mutate(document):
            if document["service_epoch"] != epoch or document["service_state"] != "ready":
                raise protocol.ProtocolError("foreign service epoch cannot stop this ledger")
            document["service_state"] = "stopping"
            queued = [job for job in list(document["jobs"]) if job["state"] in ("queued", "backlog") and job["service_epoch"] == epoch]
            for job in queued:
                if self._find_job(document, job["job_id"]) is None:
                    continue
                aborted = protocol.make_result(job["job_id"], "aborted", "ABORTED")
                self._terminalize(document, job, aborted, promote=False)
            self._collect(document)
            return None

        return self._transaction(mutate)

    def result_candidate(self, job_id):
        """Probe only the deterministic result path; never trust it as an outcome."""

        _job_id(job_id)
        candidate = os.path.join(self.results_dir, job_id + ".json")
        try:
            information = os.lstat(candidate)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
            raise protocol.ProtocolError("result path is not a regular nofollow file")
        return True

    def read_wait_result(self, job_id, waiter_id):
        _job_id(job_id)

        def read(document):
            job = self._find_job(document, job_id)
            if job is None:
                for entry in document["tombstones"]:
                    if entry["job_id"] == job_id:
                        return entry["result"]
                return protocol.make_result(job_id, "failed", "RESULT_EXPIRED")
            if job["state"] in _NONTERMINAL:
                return None
            if waiter_id not in job["waiters"]:
                raise protocol.ProtocolError("waiter is not registered for this terminal result")
            return self._read_result(job)

        return self._read_transaction(read)

    def acknowledge(self, job_id, waiter_id):
        _job_id(job_id)
        _job_id(waiter_id)

        def mutate(document):
            job = self._find_job(document, job_id)
            if job is not None and waiter_id in job["waiters"]:
                job["waiters"].remove(waiter_id)
            self._collect(document)
            return None

        return self._transaction(mutate)

    def collect_garbage(self):
        return self._transaction(lambda document: self._collect(document))
