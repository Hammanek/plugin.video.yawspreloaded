"""Validated, non-secret file IPC for the single native TMDB service writer."""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid

import requests

from yawsp_app.tmdb import (
    BackgroundPrefetch,
    GenreRepository,
    ShowRepository,
    TMDBAbortError,
    TMDBError,
    TrailerLookup,
    TrailerRepository,
    _API_URL,
)
from yawsp_app.watchlist_snapshot import SnapshotScope


SCHEMA_VERSION = 1
MAX_METADATA_ITEMS = 32
MAX_DOCUMENT_BYTES = 256 * 1024
_MAX_ID = 2**63 - 1
_MAX_TIMESTAMP = 4102444800.0  # 2100-01-01; also rejects nonfinite timestamps.
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?\Z")
_STATES = ("incoming", "pending", "active", "completed", "rejected", "aborted")
_TERMINALS = ("completed", "rejected", "aborted")
_COMMON_FIELDS = {"schema", "job_id", "operation", "created_at", "dedup_keys", "payload"}
_SECRET_FIELDS = {
    "api_key",
    "access_token",
    "authorization",
    "callback",
    "password",
    "path",
    "secret",
    "session_id",
    "token",
    "url",
}


class QueueValidationError(ValueError):
    """A document, result or state transition is outside the strict protocol."""


class QueueStateError(RuntimeError):
    """An IPC state is inconsistent or cannot be safely owned."""


class DuplicateJob(QueueStateError):
    """The exact generated job identity already exists in any published state."""


class ServiceConflictError(QueueStateError):
    """Another native service invocation already owns this queue."""


@dataclass(frozen=True)
class Claim:
    job_id: str
    path: str
    document: object


def _fail(message):
    raise QueueValidationError(message)


def _exact_mapping(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        _fail(label + " schema mismatch")
    for key in value:
        if isinstance(key, str) and key.casefold() in _SECRET_FIELDS:
            _fail("secret or executable field is not allowed")
    return value


def _number(value, label, minimum=0.0, maximum=_MAX_TIMESTAMP):
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < minimum
        or value > maximum
    ):
        _fail(label + " must be a finite number in range")
    return float(value)


def _integer(value, label, minimum=0, maximum=_MAX_ID):
    if type(value) is not int or value < minimum or value > maximum:
        _fail(label + " must be an integer in range")
    return value


def _job_id(value):
    if not isinstance(value, str) or _JOB_ID.fullmatch(value) is None:
        _fail("job id must be a generated lowercase hexadecimal identity")
    return value


def _new_job_id(job_id):
    return uuid.uuid4().hex if job_id is None else _job_id(job_id)


def _now(now):
    return time.time() if now is None else _number(now, "created_at")


def _trailer_key(media, media_id):
    return "trailer/{0}/{1}".format(media, media_id)


def _show_key(media_id):
    return "show/{0}".format(media_id)


def _watchlist_key(scope):
    raw = "{0}\0{1}\0{2}".format(
        scope["account_id"], scope["media"], scope["language"]
    ).encode("utf-8")
    return "watchlist/" + hashlib.sha256(raw).hexdigest()


def _scope(value):
    if hasattr(value, "account_id") and not isinstance(value, dict):
        value = {
            "account_id": value.account_id,
            "media": value.media,
            "language": value.language,
        }
    value = _exact_mapping(value, ("account_id", "media", "language"), "watchlist scope")
    account_id = value["account_id"]
    language = value["language"]
    if not isinstance(account_id, str) or _ACCOUNT.fullmatch(account_id) is None:
        _fail("watchlist account id is not a safe bounded identifier")
    if value["media"] not in ("movies", "tv"):
        _fail("watchlist media must be movies or tv")
    if not isinstance(language, str) or _LANGUAGE.fullmatch(language) is None:
        _fail("watchlist language is invalid")
    return {"account_id": account_id, "media": value["media"], "language": language}


def session_binding_digest(session_id, account_id, media, language):
    """Bind a job to the current secret without serializing that secret."""
    if not isinstance(session_id, str) or not session_id or len(session_id) > 4096:
        _fail("captured session must be a non-empty bounded string")
    scope = _scope({"account_id": account_id, "media": media, "language": language})
    digest = hashlib.sha256()
    digest.update(b"yawsp-native-service-session-binding-v1\0")
    digest.update(session_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        "{0}\0{1}\0{2}".format(
            scope["account_id"], scope["media"], scope["language"]
        ).encode("utf-8")
    )
    return digest.hexdigest()


def _metadata_item(value):
    fields = ("family", "cache_key", "media", "media_id", "attempts", "retry_at")
    value = _exact_mapping(value, fields, "metadata item")
    family = value["family"]
    media_id = _integer(value["media_id"], "metadata media_id", minimum=1)
    attempts = _integer(value["attempts"], "metadata attempts", maximum=1)
    retry_at = _number(value["retry_at"], "metadata retry_at")
    media = value["media"]
    cache_key = value["cache_key"]
    if family == "trailer":
        if media not in ("movie", "tv"):
            _fail("trailer media must be movie or tv")
        expected_cache = "{0}:{1}".format(media, media_id)
        dedup_key = _trailer_key(media, media_id)
    elif family == "show":
        if media is not None:
            _fail("show metadata item must have null media")
        expected_cache = str(media_id)
        dedup_key = _show_key(media_id)
    else:
        _fail("metadata family must be trailer or show")
    if cache_key != expected_cache:
        _fail("metadata cache key does not match normalized identity")
    normalized = {
        "family": family,
        "cache_key": expected_cache,
        "media": media,
        "media_id": media_id,
        "attempts": attempts,
        "retry_at": retry_at,
    }
    return normalized, dedup_key


def _shared_key(value):
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        _fail("shared key must be a typed key")
    if len(value) == 3 and value[0] == "trailer":
        media_id = _integer(value[2], "shared media_id", minimum=1)
        if value[1] not in ("movie", "tv"):
            _fail("shared trailer media must be movie or tv")
        return ["trailer", value[1], media_id], _trailer_key(value[1], media_id)
    if len(value) == 2 and value[0] == "show":
        media_id = _integer(value[1], "shared media_id", minimum=1)
        return ["show", media_id], _show_key(media_id)
    _fail("unsupported shared key")


def _dedup_keys(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_METADATA_ITEMS:
        _fail("dedup keys must be a bounded list")
    result = []
    for key in value:
        if not isinstance(key, str) or len(key) > 160 or any(
            character in key for character in ("\\", ":", "//", "..")
        ):
            _fail("dedup key is not a normalized identity")
        if key in result:
            _fail("duplicate dedup key")
        result.append(key)
    return result


def build_metadata_document(items, source="background", job_id=None, now=None):
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_METADATA_ITEMS:
        _fail("metadata batch must contain one through 32 items")
    normalized_items = []
    keys = []
    for item in items:
        normalized, key = _metadata_item(item)
        if key in keys:
            _fail("metadata batch contains a duplicate key")
        normalized_items.append(normalized)
        keys.append(key)
    if source != "background":
        _fail("unsupported metadata source")
    return {
        "schema": SCHEMA_VERSION,
        "job_id": _new_job_id(job_id),
        "operation": "run_metadata",
        "created_at": _now(now),
        "dedup_keys": keys,
        "payload": {"source": source, "items": normalized_items},
    }


def build_shared_document(key, source="interactive", job_id=None, now=None):
    normalized, dedup_key = _shared_key(key)
    if source != "interactive":
        _fail("unsupported shared source")
    return {
        "schema": SCHEMA_VERSION,
        "job_id": _new_job_id(job_id),
        "operation": "run_shared",
        "created_at": _now(now),
        "dedup_keys": [dedup_key],
        "payload": {"source": source, "key": normalized},
    }


def build_watchlist_document(
    scope,
    session_id,
    generation,
    refresh_due,
    allow_missing_account,
    followup=False,
    job_id=None,
    now=None,
):
    scope = _scope(scope)
    digest = session_binding_digest(
        session_id, scope["account_id"], scope["media"], scope["language"]
    )
    _integer(generation, "watchlist generation")
    if type(refresh_due) is not bool or type(allow_missing_account) is not bool:
        _fail("watchlist due/account flags must be booleans")
    if type(followup) is not bool:
        _fail("watchlist followup flag must be boolean")
    return {
        "schema": SCHEMA_VERSION,
        "job_id": _new_job_id(job_id),
        "operation": "run_watchlist",
        "created_at": _now(now),
        "dedup_keys": [_watchlist_key(scope)],
        "payload": {
            "scope": scope,
            "session_digest": digest,
            "generation": generation,
            "refresh_due": refresh_due,
            "allow_missing_account": allow_missing_account,
            "followup": followup,
        },
    }


def _next_episode(value):
    if value is None:
        return None
    value = _exact_mapping(value, ("season", "episode", "air_date"), "show result")
    _integer(value["season"], "show season")
    _integer(value["episode"], "show episode", minimum=1)
    if not isinstance(value["air_date"], str) or len(value["air_date"]) > 32:
        _fail("show air date must be a bounded string")
    return {
        "season": value["season"],
        "episode": value["episode"],
        "air_date": value["air_date"],
    }


def _result(key, value):
    if key.startswith("trailer/"):
        value = _exact_mapping(value, ("kind", "status", "youtube_id"), "trailer result")
        if value["kind"] != "trailer" or value["status"] not in ("hit", "missing", "error"):
            _fail("invalid trailer result")
        youtube_id = value["youtube_id"]
        if value["status"] == "hit":
            if not isinstance(youtube_id, str) or not 1 <= len(youtube_id.strip()) <= 128:
                _fail("hit trailer result requires a bounded YouTube id")
            youtube_id = youtube_id.strip()
        elif youtube_id is not None:
            _fail("non-hit trailer result requires a null YouTube id")
        return {"kind": "trailer", "status": value["status"], "youtube_id": youtube_id}
    if key.startswith("show/"):
        value = _exact_mapping(value, ("kind", "value", "success"), "show result")
        if value["kind"] != "show" or type(value["success"]) is not bool:
            _fail("invalid show result")
        return {"kind": "show", "value": _next_episode(value["value"]), "success": value["success"]}
    _fail("unsupported result key")


def validate_document(document):
    """Return a detached normalized document; never accept executable locations."""
    if not isinstance(document, dict):
        _fail("job document must be an object")
    has_terminal = "terminal" in document
    fields = _COMMON_FIELDS | ({"terminal"} if has_terminal else set())
    document = _exact_mapping(document, fields, "job")
    if type(document["schema"]) is not int or document["schema"] != SCHEMA_VERSION:
        _fail("unsupported job schema")
    job_id = _job_id(document["job_id"])
    created_at = _number(document["created_at"], "created_at")
    keys = _dedup_keys(document["dedup_keys"])
    operation = document["operation"]
    payload = document["payload"]
    if operation == "run_metadata":
        rebuilt = build_metadata_document(
            payload.get("items") if isinstance(payload, dict) else None,
            source=payload.get("source") if isinstance(payload, dict) else None,
            job_id=job_id,
            now=created_at,
        )
        _exact_mapping(payload, ("source", "items"), "metadata payload")
    elif operation == "run_shared":
        rebuilt = build_shared_document(
            payload.get("key") if isinstance(payload, dict) else None,
            source=payload.get("source") if isinstance(payload, dict) else None,
            job_id=job_id,
            now=created_at,
        )
        _exact_mapping(payload, ("source", "key"), "shared payload")
    elif operation == "run_watchlist":
        if not isinstance(payload, dict):
            _fail("watchlist payload schema mismatch")
        _exact_mapping(
            payload,
            (
                "scope",
                "session_digest",
                "generation",
                "refresh_due",
                "allow_missing_account",
                "followup",
            ),
            "watchlist payload",
        )
        scope = _scope(payload["scope"])
        rebuilt = build_watchlist_document(
            scope,
            # Validation cannot reconstruct a secret. Build then replace the digest.
            "validation-placeholder",
            payload["generation"],
            payload["refresh_due"],
            payload["allow_missing_account"],
            followup=payload["followup"],
            job_id=job_id,
            now=created_at,
        )
        digest = payload["session_digest"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            _fail("watchlist session digest must be lowercase SHA-256")
        rebuilt["payload"]["session_digest"] = digest
    else:
        _fail("unsupported job operation")
    if rebuilt["dedup_keys"] != keys:
        _fail("dedup keys do not match normalized job payload")
    if not has_terminal:
        return deepcopy(rebuilt)

    terminal = document["terminal"]
    state = terminal.get("state") if isinstance(terminal, dict) else None
    if state == "completed":
        terminal = _exact_mapping(terminal, ("state", "finished_at", "results"), "terminal")
        _number(terminal["finished_at"], "terminal finished_at")
        results = terminal["results"]
        if not isinstance(results, dict):
            _fail("completed results must be an object")
        if operation == "run_watchlist":
            if results:
                _fail("watchlist jobs have no interactive result members")
            normalized_results = {}
        else:
            if set(results) != set(keys):
                _fail("completed results must exactly match the claimed keys")
            normalized_results = {key: _result(key, results[key]) for key in keys}
        rebuilt["terminal"] = {
            "state": "completed",
            "finished_at": terminal["finished_at"],
            "results": normalized_results,
        }
    elif state in ("rejected", "aborted"):
        terminal = _exact_mapping(terminal, ("state", "finished_at", "reason"), "terminal")
        _number(terminal["finished_at"], "terminal finished_at")
        if terminal["reason"] not in (
            "invalid",
            "failed",
            "interrupted",
            "service_shutdown",
            "stale_startup",
        ):
            _fail("terminal reason is not allowlisted")
        rebuilt["terminal"] = {
            "state": state,
            "finished_at": terminal["finished_at"],
            "reason": terminal["reason"],
        }
    else:
        _fail("terminal state is invalid")
    return deepcopy(rebuilt)


class ServiceLease:
    """An OS-owned cross-interpreter lock, released even if a process dies."""

    def __init__(self, queue, owner_id):
        self.queue = queue
        self.owner_id = owner_id
        self.stream = None
        self.locked = False
        queue._ensure_directories()
        path = os.path.join(queue.root, "service.lock")
        self.stream = open(path, "a+b")
        try:
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self._lock()
            self.stream.seek(0)
            self.stream.truncate()
            self.stream.write((owner_id + "\n").encode("ascii"))
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.locked = True
        except Exception:
            self.close()
            raise

    def _lock(self):
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ServiceConflictError("TMDB service is already owned") from error
        else:
            import fcntl

            try:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ServiceConflictError("TMDB service is already owned") from error

    def close(self):
        if self.stream is None:
            return
        try:
            if self.locked or self.stream is not None:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    try:
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None
            self.locked = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class ServiceJobQueue:
    """One profile-local queue with strict no-replace publication and claims."""

    def __init__(self, root, clock=time.time):
        self.root = os.path.abspath(os.fspath(root))
        self.clock = clock

    def _path(self, state, job_id):
        if state not in _STATES:
            raise QueueStateError("unknown queue state")
        return os.path.join(self.root, state, _job_id(job_id) + ".json")

    def _reservation_path(self, dedup_key):
        encoded = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()
        return os.path.join(self.root, "reservations", encoded)

    def _ensure_directories(self):
        for state in _STATES + ("reservations",):
            os.makedirs(os.path.join(self.root, state), exist_ok=True)

    def _acquire_reservations(self, document):
        acquired = []
        job_id = document["job_id"]
        try:
            for dedup_key in document["dedup_keys"]:
                reservation = self._reservation_path(dedup_key)
                os.mkdir(reservation)
                acquired.append(reservation)
                owner = os.path.join(reservation, "job")
                with open(owner, "xb") as stream:
                    stream.write(job_id.encode("ascii"))
                    stream.flush()
                    os.fsync(stream.fileno())
        except (FileExistsError, OSError):
            for reservation in acquired:
                self._remove_reservation(reservation)
            if any(
                os.path.isdir(self._reservation_path(key))
                for key in document["dedup_keys"]
            ):
                raise DuplicateJob("job key is already pending or active")
            raise
        return acquired

    @staticmethod
    def _remove_reservation(path):
        try:
            os.unlink(os.path.join(path, "job"))
        except FileNotFoundError:
            pass
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass

    def _release_reservations(self, document):
        for dedup_key in document["dedup_keys"]:
            self._remove_reservation(self._reservation_path(dedup_key))

    def _release_job_reservations(self, job_id):
        root = os.path.join(self.root, "reservations")
        if not os.path.isdir(root):
            return
        for name in os.listdir(root):
            path = os.path.join(root, name)
            try:
                with open(os.path.join(path, "job"), encoding="ascii") as stream:
                    if stream.read().strip() == job_id:
                        self._remove_reservation(path)
            except FileNotFoundError:
                continue

    @staticmethod
    def _write(stream, document):
        encoded = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_DOCUMENT_BYTES:
            _fail("job document exceeds the byte bound")
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())

    @staticmethod
    def _publish_link(source, target):
        try:
            os.link(source, target)
        except FileExistsError as error:
            raise DuplicateJob("generated job identity is already published") from error

    @staticmethod
    def _unlink_after_readers(path):
        # Completed/aborted state may already be durable. Windows readers do not
        # share-delete, so clean up only the obsolete active link with a finite
        # retry instead of attempting a colliding second terminal transition.
        for attempt in range(100):
            try:
                os.unlink(path)
                return
            except FileNotFoundError:
                return
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(0.005)

    @staticmethod
    def _load_path(path):
        if os.path.getsize(path) > MAX_DOCUMENT_BYTES:
            _fail("job document exceeds the byte bound")
        with open(path, "rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            _fail("job document exceeds the byte bound")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise QueueValidationError("malformed JSON job document") from error
        return validate_document(document)

    def publish(self, document):
        document = validate_document(document)
        if "terminal" in document:
            _fail("terminal documents cannot be newly published")
        self._ensure_directories()
        job_id = document["job_id"]
        for state in _STATES[1:]:
            if os.path.exists(self._path(state, job_id)):
                raise DuplicateJob("generated job identity already exists")
        acquired = self._acquire_reservations(document)
        published = False
        descriptor, temporary = tempfile.mkstemp(
            prefix=job_id + ".", suffix=".tmp", dir=os.path.join(self.root, "incoming")
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                self._write(stream, document)
            self._publish_link(temporary, self._path("pending", job_id))
            published = True
            return job_id
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            if not published:
                for reservation in acquired:
                    self._remove_reservation(reservation)

    def claim_one(self):
        self._ensure_directories()
        pending = os.path.join(self.root, "pending")
        for name in sorted(os.listdir(pending)):
            if not name.endswith(".json") or _JOB_ID.fullmatch(name[:-5]) is None:
                continue
            job_id = name[:-5]
            source = self._path("pending", job_id)
            target = self._path("active", job_id)
            try:
                self._publish_link(source, target)
            except DuplicateJob:
                continue
            try:
                os.unlink(source)
            except OSError:
                # Windows readers do not share-delete by default. A concurrent
                # parent scan may briefly hold the pending file open; never turn
                # that transient into a second claim or lose the published job.
                try:
                    os.unlink(target)
                except OSError:
                    pass
                continue
            try:
                document = self._load_path(target)
                if document["job_id"] != job_id:
                    _fail("claimed filename and job identity differ")
            except (OSError, QueueValidationError):
                document = None
            return Claim(job_id, target, document)
        return None

    def _terminal_path(self, job_id):
        found = [state for state in _TERMINALS if os.path.exists(self._path(state, job_id))]
        if len(found) > 1:
            raise QueueStateError("job has multiple terminal states")
        return self._path(found[0], job_id) if found else None

    def _finish(self, claim, state, results=None, reason=None):
        if not isinstance(claim, Claim) or claim.document is None:
            _fail("only a validated active claim can be finalized")
        expected = self._path("active", claim.job_id)
        if os.path.abspath(claim.path) != os.path.abspath(expected) or not os.path.isfile(expected):
            _fail("claim is not the sole active owner")
        document = validate_document(claim.document)
        if "terminal" in document:
            _fail("terminal job cannot be finalized again")
        if self._terminal_path(claim.job_id) is not None:
            raise DuplicateJob("terminal job identity already exists")
        terminal = {"state": state, "finished_at": _number(self.clock(), "finished_at")}
        if state == "completed":
            terminal["results"] = results if results is not None else {}
        else:
            terminal["reason"] = reason or ("failed" if state == "rejected" else "interrupted")
        document["terminal"] = terminal
        document = validate_document(document)
        descriptor, temporary = tempfile.mkstemp(
            prefix=claim.job_id + ".", suffix=".tmp", dir=os.path.join(self.root, "incoming")
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                self._write(stream, document)
            self._publish_link(temporary, self._path(state, claim.job_id))
            self._unlink_after_readers(expected)
            self._release_reservations(document)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def complete(self, claim, results):
        self._finish(claim, "completed", results=results)

    def reject(self, claim, reason="invalid"):
        if claim.document is None:
            self._finish_unvalidated(claim, "rejected", reason)
        else:
            self._finish(claim, "rejected", reason=reason)

    def abort(self, claim, reason="interrupted"):
        if claim.document is None:
            self._finish_unvalidated(claim, "aborted", reason)
        else:
            self._finish(claim, "aborted", reason=reason)

    def _finish_unvalidated(self, claim, state, reason):
        expected = self._path("active", claim.job_id)
        if os.path.abspath(claim.path) != os.path.abspath(expected) or not os.path.isfile(expected):
            _fail("unvalidated claim is not active")
        target = self._path(state, claim.job_id)
        self._publish_link(expected, target)
        self._unlink_after_readers(expected)
        self._release_job_reservations(claim.job_id)

    def find_nonterminal(self, dedup_key):
        _dedup_keys([dedup_key])
        for state in ("active", "pending"):
            directory = os.path.join(self.root, state)
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".json") or _JOB_ID.fullmatch(name[:-5]) is None:
                    continue
                job_id = name[:-5]
                try:
                    document = self._load_path(self._path(state, job_id))
                except (OSError, QueueValidationError):
                    continue
                if dedup_key in document["dedup_keys"]:
                    return job_id
        return None

    def read_nonterminal(self, job_id):
        for state in ("active", "pending"):
            path = self._path(state, job_id)
            if os.path.isfile(path):
                document = self._load_path(path)
                if "terminal" in document or document["job_id"] != job_id:
                    _fail("published job identity or envelope is invalid")
                return document
        return None

    def read_terminal(self, job_id, require_completed=False):
        target = self._terminal_path(job_id)
        if target is None:
            return None
        document = self._load_path(target)
        if "terminal" not in document or document["job_id"] != job_id:
            _fail("terminal identity or envelope is invalid")
        if require_completed and document["terminal"]["state"] != "completed":
            _fail("job did not complete successfully")
        return document

    def recover_stale(self):
        """Abort all published pre-start work without interpreting or replaying it."""
        self._ensure_directories()
        recovered = 0
        for state in ("pending", "active"):
            directory = os.path.join(self.root, state)
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".json") or _JOB_ID.fullmatch(name[:-5]) is None:
                    continue
                source = os.path.join(directory, name)
                job_id = name[:-5]
                # A terminal link may have survived a crash immediately before the
                # active source was unlinked. Preserve that sole terminal outcome.
                if state == "active" and self._terminal_path(job_id) is not None:
                    self._unlink_after_readers(source)
                    self._release_job_reservations(job_id)
                    continue
                target = self._path("aborted", job_id)
                try:
                    self._publish_link(source, target)
                    os.unlink(source)
                except DuplicateJob:
                    continue
                recovered += 1
                # Valid stale documents receive an explicit terminal envelope; a
                # partial JSON file stays aborted bytes and is never consumable.
                try:
                    document = self._load_path(target)
                    claim = Claim(job_id, target, document)
                    temporary_target = self._path("active", job_id)
                    self._publish_link(target, temporary_target)
                    os.unlink(target)
                    claim = Claim(job_id, temporary_target, document)
                    self.abort(claim, reason="stale_startup")
                except (OSError, QueueValidationError, QueueStateError):
                    # If the envelope cannot be upgraded, the original moved bytes
                    # remain in aborted and cannot be claimed by the service loop.
                    if not os.path.exists(target):
                        raise
                    self._release_job_reservations(job_id)
        # Reservations left by a producer crash before pending publication are
        # not jobs and cannot reserve their key forever.
        reservations = os.path.join(self.root, "reservations")
        wanted = set()
        for state in ("pending", "active"):
            directory = os.path.join(self.root, state)
            for name in os.listdir(directory):
                if name.endswith(".json") and _JOB_ID.fullmatch(name[:-5]):
                    try:
                        document = self._load_path(os.path.join(directory, name))
                    except (OSError, QueueValidationError):
                        continue
                    wanted.update(
                        os.path.basename(self._reservation_path(key))
                        for key in document["dedup_keys"]
                    )
        for name in os.listdir(reservations):
            if name not in wanted:
                self._remove_reservation(os.path.join(reservations, name))
        return recovered

    def acquire_service(self, owner_id=None):
        return ServiceLease(self, owner_id or uuid.uuid4().hex)

    def service_available(self):
        try:
            lease = self.acquire_service()
        except ServiceConflictError:
            return True
        else:
            lease.close()
            return False


def _internal_metadata_item(item):
    if not isinstance(item, (tuple, list)) or len(item) != 7:
        raise TypeError("invalid BackgroundPrefetch metadata item")
    key, cache_key, family, media, media_id, attempts, retry_at = item
    normalized, dedup_key = _metadata_item(
        {
            "family": family,
            "cache_key": cache_key,
            "media": media,
            "media_id": media_id,
            "attempts": attempts,
            "retry_at": retry_at,
        }
    )
    expected_key = (
        ("trailer", media, media_id) if family == "trailer" else ("show", media_id)
    )
    if key != expected_key:
        raise TypeError("metadata reservation key does not match its item")
    return normalized, dedup_key


class ServiceJobWorker:
    """Parent producer and typed waiter; it never owns HTTP or a worker pool."""

    def __init__(
        self,
        queue,
        abort=None,
        clock=time.monotonic,
        sleeper=time.sleep,
        poll_interval=0.05,
        wait_timeout=30.0,
        max_pending=32,
        require_service=True,
    ):
        self.queue = queue
        self.abort = abort or (lambda: False)
        self.clock = clock
        self.sleeper = sleeper
        self.poll_interval = _number(poll_interval, "poll interval", minimum=0.001, maximum=5.0)
        self.wait_timeout = _number(wait_timeout, "wait timeout", minimum=0.0, maximum=300.0)
        self.max_pending = _integer(max_pending, "interactive capacity", minimum=1, maximum=128)
        self.require_service = bool(require_service)
        self._condition = threading.Condition(threading.Lock())
        self._flights = {}
        self._closed = False
        self._metadata_jobs = {}
        self._watchlist_jobs = {}
        self._draining = False
        self.background = None

    @property
    def interactive_count(self):
        with self._condition:
            return len(self._flights)

    def bind_background(self, background):
        self.background = background

    def _run_metadata(self, *args):
        raise TypeError("worker methods are not queue entry points")

    def _run_watchlist(self, *args):
        raise TypeError("worker methods are not queue entry points")

    def submit(self, function, *args):
        name = getattr(function, "__name__", "")
        if name == "_run_metadata":
            self._submit_metadata(args)
        elif name == "_run_watchlist":
            self._submit_watchlist(args)
        else:
            # Reject before mkdir/publication and, importantly, never call it inline.
            raise TypeError("only _run_metadata and _run_watchlist may be submitted")
        return None

    def _metadata_submission(self, args):
        if len(args) != 1 or not isinstance(args[0], list) or not args[0]:
            raise TypeError("metadata submission requires one accepted batch")
        items = [_internal_metadata_item(item)[0] for item in args[0]]
        try:
            return build_metadata_document(items)
        except QueueValidationError as error:
            raise TypeError("metadata batch is outside the service schema") from error

    def _track_metadata(self, job_id, items):
        tracked = self._metadata_jobs.setdefault(job_id, [])
        known = {(item["family"], item["media"], item["media_id"]) for item in tracked}
        for item in items:
            identity = (item["family"], item["media"], item["media_id"])
            if identity not in known:
                tracked.append(item)
                known.add(identity)

    def _submit_metadata(self, args):
        document = self._metadata_submission(args)
        claimed_documents = []
        new_items = []
        for item, dedup_key in zip(document["payload"]["items"], document["dedup_keys"]):
            existing = self.queue.find_nonterminal(dedup_key)
            if existing is None:
                new_items.append(item)
            else:
                claimed_documents.append((existing, [item]))
        if new_items:
            new_document = build_metadata_document(new_items)
            job_id = self.queue.publish(new_document)
            claimed_documents.append((job_id, new_items))
        for job_id, items in claimed_documents:
            self._track_metadata(job_id, items)

        background = self.background
        if background is not None:
            keys = [item[0] for item in args[0]]
            # Reservation/dedup state stays in _metadata_queued until terminal
            # reaping; only the executable local slot is handed to the service.
            background._finish(None, keys, preserve_metadata=keys)
            if self._draining:
                return
            self._draining = True
            try:
                while not background._closed:
                    if background._schedule_metadata() == 0:
                        break
            finally:
                self._draining = False

    def _watchlist_submission(self, args):
        if len(args) != 5:
            raise TypeError("watchlist submission has unexpected arguments")
        keys, scope, session_id, generation, allow_missing = args
        if (
            not isinstance(keys, list)
            or len(keys) != 1
            or not isinstance(keys[0], tuple)
            or len(keys[0]) != 2
            or keys[0][0] != "watchlist"
            or keys[0][1] is not scope
        ):
            raise TypeError("watchlist reservation does not match scope")
        try:
            return build_watchlist_document(
                scope,
                session_id,
                generation,
                True,
                allow_missing,
            )
        except QueueValidationError as error:
            raise TypeError("watchlist job is outside the service schema") from error

    def _submit_watchlist(self, args):
        document = self._watchlist_submission(args)
        scope = args[1]
        job_id = self.queue.find_nonterminal(document["dedup_keys"][0])
        if job_id is None:
            job_id = self.queue.publish(document)
        self._watchlist_jobs[job_id] = scope

    def reap(self):
        """Apply durable terminal outcomes on a later parent invocation."""
        background = self.background
        if background is None:
            return
        for job_id, items in list(self._metadata_jobs.items()):
            terminal = self.queue.read_terminal(job_id)
            if terminal is None:
                continue
            state = terminal["terminal"]["state"]
            with background._condition:
                for item in items:
                    key = (
                        ("trailer", item["media"], item["media_id"])
                        if item["family"] == "trailer"
                        else ("show", item["media_id"])
                    )
                    dedup_key = (
                        _trailer_key(item["media"], item["media_id"])
                        if item["family"] == "trailer"
                        else _show_key(item["media_id"])
                    )
                    if state == "completed":
                        result = self._decode_result(
                            dedup_key, terminal["terminal"]["results"][dedup_key]
                        )
                        reconciled = background._metadata_item(
                            item["family"],
                            item["cache_key"],
                            item["media"],
                            item["media_id"],
                            attempts=1,
                            retry_at=item["retry_at"],
                        )
                        background._record_metadata_result(reconciled, result)
                    else:
                        background._metadata_queued.discard(key)
                        background._error_retry_at.pop(key, None)
            del self._metadata_jobs[job_id]
        for job_id, scope in list(self._watchlist_jobs.items()):
            if self.queue.read_terminal(job_id) is None:
                continue
            background._release([("watchlist", scope)])
            with background._lock:
                background._watchlist_followups.pop(scope, None)
            del self._watchlist_jobs[job_id]

    def _check_callable(self):
        if self._closed:
            raise TMDBError("TMDB service client is closed")
        if self.abort():
            raise TMDBAbortError("TMDB service request aborted")
        if self.require_service and not self.queue.service_available():
            raise TMDBError("TMDB native service is unavailable")

    @staticmethod
    def _decode_result(dedup_key, result):
        if dedup_key.startswith("trailer/"):
            return TrailerLookup(result["status"], result["youtube_id"])
        # This is the existing ShowRepository internal result. Its public
        # next_episode() method unwraps [0] and therefore returns the typed dict.
        return deepcopy(result["value"]), result["success"]

    def _terminal_result(self, flight):
        document = self.queue.read_terminal(flight["job_id"])
        if document is None:
            return None
        terminal = document["terminal"]
        if terminal["state"] == "aborted":
            raise TMDBAbortError("TMDB native service aborted the request")
        if terminal["state"] == "rejected":
            raise TMDBError("TMDB native service rejected the request")
        return self._decode_result(
            flight["dedup_key"], terminal["results"][flight["dedup_key"]]
        )

    def _finish(self, flight, result=None, error=None):
        with self._condition:
            if flight["done"]:
                return
            flight["result"] = result
            flight["error"] = error
            flight["done"] = True
            current = self._flights.get(flight["dedup_key"])
            if current is flight:
                del self._flights[flight["dedup_key"]]
            self._condition.notify_all()

    def _wait_owner(self, flight, deadline):
        while True:
            try:
                result = self._terminal_result(flight)
                if result is not None:
                    self._finish(flight, result=result)
                    return
                if self.abort():
                    raise TMDBAbortError("TMDB service request aborted")
                if self.require_service and not self.queue.service_available():
                    raise TMDBError("TMDB native service stopped before completion")
                if self.clock() >= deadline:
                    raise TMDBError("TMDB native service wait timed out")
            except Exception as error:
                self._finish(flight, error=error)
                return
            self.sleeper(self.poll_interval)

    def _claim_shared_job(self, shared_key, dedup_key, deadline):
        while True:
            job_id = self.queue.find_nonterminal(dedup_key)
            if job_id is not None:
                return job_id
            try:
                return self.queue.publish(build_shared_document(shared_key))
            except DuplicateJob:
                # The winner holds the key reservation during its short
                # incoming→pending fsync window. Wait for that atomic publication.
                if self.clock() >= deadline:
                    raise QueueStateError("shared key owner did not publish in time")
                self.sleeper(self.poll_interval)

    def run_shared(self, key, operation):
        """Serialize only *key*; *operation* never crosses this process boundary."""
        shared_key, dedup_key = _shared_key(key)
        self.reap()
        self._check_callable()
        deadline = self.clock() + self.wait_timeout
        with self._condition:
            while True:
                flight = self._flights.get(dedup_key)
                if flight is not None:
                    owner = False
                    break
                if len(self._flights) < self.max_pending + 1:
                    flight = {
                        "dedup_key": dedup_key,
                        "job_id": None,
                        "done": False,
                        "result": None,
                        "error": None,
                    }
                    self._flights[dedup_key] = flight
                    owner = True
                    break
                if self.abort():
                    raise TMDBAbortError("TMDB service request aborted")
                if self.clock() >= deadline:
                    raise TMDBError("TMDB interactive reservation wait timed out")
                self._condition.wait(self.poll_interval)

        if owner:
            try:
                job_id = self._claim_shared_job(shared_key, dedup_key, deadline)
                flight["job_id"] = job_id
                self._wait_owner(flight, deadline)
            except Exception as error:
                self._finish(flight, error=error)
        else:
            with self._condition:
                while not flight["done"]:
                    if flight["job_id"] is None:
                        # The owner publishes before any waiter can finish; no closure
                        # or ad-hoc fallback is needed on this bounded wait.
                        self._condition.wait(self.poll_interval)
                        continue
                    self._condition.wait(self.poll_interval)
                    if self.abort():
                        raise TMDBAbortError("TMDB shared wait aborted")
                    if self.clock() >= deadline:
                        raise TMDBError("TMDB shared result wait timed out")

        if flight["error"] is not None:
            error = flight["error"]
            if isinstance(error, (TMDBError, TMDBAbortError)):
                raise error
            raise TMDBError("TMDB native service protocol failed") from error
        return deepcopy(flight["result"])

    def shutdown(self, wait=False):
        # `wait` is deliberately not honored by joining: this producer owns no
        # execution resource. A current on-demand caller still has its bounded wait.
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class DirectWorker:
    """Run real BackgroundPrefetch engines only on the native service thread."""

    def submit(self, function, *args):
        return function(*args)

    def shutdown(self, wait=False):
        return None


class _StaleJob(Exception):
    pass


class _WatchlistSaveRecorder:
    def __init__(self, store):
        self.store = store
        self.last_save = "not-called"

    def save_complete(self, *args, **kwargs):
        self.last_save = self.store.save_complete(*args, **kwargs)
        return self.last_save

    def __getattr__(self, name):
        return getattr(self.store, name)


class ServiceRunner:
    """Consume strict documents serially, owning one HTTP session per real batch."""

    def __init__(
        self,
        queue,
        settings,
        trailer_cache,
        show_cache,
        snapshot_store,
        locale_mapper,
        session_factory=requests.Session,
        abort=None,
        clock=time.time,
        sleeper=time.sleep,
        api_url=_API_URL,
        logger=None,
    ):
        self.queue = queue
        self.settings = settings
        self.trailer_cache = trailer_cache
        self.show_cache = show_cache
        self.snapshot_store = snapshot_store
        self.locale_mapper = locale_mapper
        self.session_factory = session_factory
        self.abort = abort or (lambda: False)
        self.clock = clock
        self.sleeper = sleeper
        self.api_url = api_url
        self.logger = logger or logging.getLogger(__name__)

    def _background(self, snapshot_store=None):
        return BackgroundPrefetch(
            settings=self.settings,
            trailer_cache=self.trailer_cache,
            show_cache=self.show_cache,
            locale_mapper=self.locale_mapper,
            session_factory=self.session_factory,
            worker=DirectWorker(),
            abort=self.abort,
            clock=self.clock,
            sleeper=self.sleeper,
            api_url=self.api_url,
            logger=self.logger,
            snapshot_store=self.snapshot_store if snapshot_store is None else snapshot_store,
            genre_repository=GenreRepository(),
        )

    @staticmethod
    def _items(document):
        source_items = (
            document["payload"]["items"]
            if document["operation"] == "run_metadata"
            else [
                {
                    "family": "trailer",
                    "cache_key": "{0}:{1}".format(
                        document["payload"]["key"][1], document["payload"]["key"][2]
                    ),
                    "media": document["payload"]["key"][1],
                    "media_id": document["payload"]["key"][2],
                    "attempts": 0,
                    "retry_at": 0.0,
                }
                if document["payload"]["key"][0] == "trailer"
                else {
                    "family": "show",
                    "cache_key": str(document["payload"]["key"][1]),
                    "media": None,
                    "media_id": document["payload"]["key"][1],
                    "attempts": 0,
                    "retry_at": 0.0,
                }
            ]
        )
        return source_items

    def _metadata_results(self, document):
        readers = {
            "trailer": TrailerRepository(None, self.trailer_cache, clock=self.clock),
            "show": ShowRepository(None, self.show_cache, clock=self.clock),
        }
        for reader in readers.values():
            with reader._lock:
                reader._load()
        results = {}
        for item, key in zip(self._items(document), document["dedup_keys"]):
            reader = readers[item["family"]]
            with reader._lock:
                raw = reader._entry(item["cache_key"])
                fresh = reader._fresh(raw)
                if item["family"] == "trailer":
                    if not fresh:
                        result = {"kind": "trailer", "status": "error", "youtube_id": None}
                    else:
                        result = {
                            "kind": "trailer",
                            "status": "hit" if raw["youtube_id"] else "missing",
                            "youtube_id": raw["youtube_id"],
                        }
                else:
                    result = {
                        "kind": "show",
                        "value": deepcopy(raw["next"] if raw is not None else None),
                        "success": bool(fresh),
                    }
            results[key] = result
        return results

    def _run_metadata(self, document):
        background = self._background()
        try:
            items = [
                background._metadata_item(
                    item["family"],
                    item["cache_key"],
                    item["media"],
                    item["media_id"],
                    attempts=item["attempts"],
                    retry_at=item["retry_at"],
                )
                for item in self._items(document)
            ]
            background._prepare_metadata(items)
            scheduled = background._schedule_metadata()
            if scheduled != len(items):
                raise QueueStateError("native service did not own every metadata item")
            return self._metadata_results(document)
        finally:
            background.close(wait=False)

    def _current_watchlist_session(self, scope, captured_digest):
        session_id = str(self.settings.get("tmdb_access_token") or "").strip()
        try:
            current_digest = session_binding_digest(
                session_id, scope.account_id, scope.media, scope.language
            )
        except QueueValidationError as error:
            raise _StaleJob() from error
        if current_digest != captured_digest:
            raise _StaleJob()
        return session_id

    def _run_watchlist(self, document, claim_job_id):
        payload = document["payload"]
        scope = SnapshotScope(
            payload["scope"]["account_id"],
            payload["scope"]["media"],
            payload["scope"]["language"],
        )
        recorder = _WatchlistSaveRecorder(self.snapshot_store)
        background = self._background(snapshot_store=recorder)
        try:
            session_id = self._current_watchlist_session(
                scope, payload["session_digest"]
            )
            background._run_watchlist(
                [("watchlist", scope)],
                scope,
                session_id,
                payload["generation"],
                payload["allow_missing_account"],
            )
            if not background._watchlist_identity(
                scope, session_id, payload["allow_missing_account"]
            ):
                raise _StaleJob()
            if recorder.last_save == "not-called":
                raise QueueStateError("watchlist operation produced no complete save")
            if recorder.last_save is None and not payload["followup"]:
                # A local watched mutation advanced generation during this fetch.
                # Rebuild one fresh validated document, never replay the stale one.
                generation = self.snapshot_store.generation(scope)
                followup = build_watchlist_document(
                    scope,
                    session_id,
                    generation,
                    True,
                    payload["allow_missing_account"],
                    followup=True,
                )
                return {}, followup
            return {}, None
        finally:
            background.close(wait=False)

    def process_one(self):
        claim = self.queue.claim_one()
        if claim is None:
            return False
        if claim.document is None:
            self.queue.reject(claim)
            return True
        try:
            if self.abort():
                raise _StaleJob()
            followup = None
            if claim.document["operation"] == "run_watchlist":
                results, followup = self._run_watchlist(claim.document, claim.job_id)
            else:
                results = self._run_metadata(claim.document)
            if self.abort():
                raise _StaleJob()
            self.queue.complete(claim, results)
            if followup is not None:
                try:
                    self.queue.publish(followup)
                except Exception:
                    self.logger.exception("Unable to publish watchlist generation followup")
        except _StaleJob:
            self.queue.abort(claim)
        except SystemExit:
            self.queue.abort(claim, reason="service_shutdown")
            raise
        except Exception:
            self.logger.exception("Native TMDB service job failed")
            self.queue.reject(claim, reason="failed")
        return True

    def run(self, monitor, wait_slice=0.05):
        """Own startup recovery, service exclusion and the single consuming loop."""
        with self.queue.acquire_service():
            self.queue.recover_stale()
            while not monitor.abortRequested():
                if not self.process_one():
                    if monitor.waitForAbort(wait_slice):
                        break
