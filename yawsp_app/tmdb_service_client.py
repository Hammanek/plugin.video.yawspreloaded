"""Nonblocking submissions and abort-aware interactive TMDB service waits."""

import copy
import hashlib
import logging
import re
import secrets
import threading
import time

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.tmdb_youtube import valid_youtube_id
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


_LOGGER = logging.getLogger(__name__)
_WAIT_SLICE = 0.05
_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_INTERACTIVE_OPERATIONS = frozenset(
    (
        "watchlist.refresh",
        "watchlist.watched",
        "discovery.catalog",
        "details.seasons",
        "details.episodes",
        "identity.invalidate",
    )
)
_ERRORS = {
    "FAILED": (TMDBError, "TMDB operation failed"),
    "ABORTED": (TMDBAbortError, "TMDB operation aborted"),
    "UNAVAILABLE": (TMDBServiceUnavailableError, "TMDB service unavailable"),
    "BACKPRESSURED": (TMDBServiceBackpressuredError, "TMDB service backpressured"),
    "SHUTTING_DOWN": (
        TMDBServiceShuttingDownError,
        "TMDB service shutting down",
    ),
    "RESULT_EXPIRED": (
        TMDBServiceResultExpiredError,
        "TMDB service result expired",
    ),
}


class _EmptyCache:
    def load(self):
        return {}

    def save(self, _mapping):  # pragma: no cover - read-only repositories never call it
        raise protocol.ProtocolError("parent metadata cache is read-only")

    def save_merged(self, _updates, prefer_update=None):
        raise protocol.ProtocolError("parent metadata cache is read-only")


class TMDBServiceClient:
    """A parent invocation's read-only cache adapter and service waiter set."""

    def __init__(
        self,
        store,
        settings,
        trailer_cache,
        show_cache,
        locale_mapper,
        snapshot_store=None,
        clock=time.time,
        abort=None,
        wait_for_abort=None,
        logger=None,
        id_factory=None,
    ):
        self.store = store
        self.settings = settings
        self.trailer_cache = trailer_cache
        self.show_cache = show_cache
        self.snapshot_store = snapshot_store
        self.locale_mapper = locale_mapper
        self.clock = clock
        self.abort = abort or (lambda: False)
        self.wait_for_abort = wait_for_abort
        self.logger = logger or _LOGGER
        self._id_factory = id_factory or secrets.token_hex
        self._lock = threading.RLock()
        self._waiters = {}
        self._closed = False
        self._trailer_repository = None
        self._show_repository = None
        self._trailer_reader = TrailerRepository(
            None,
            trailer_cache if trailer_cache is not None else _EmptyCache(),
            clock=clock,
            logger=self.logger,
            _read_only=True,
        )
        self._show_reader = ShowRepository(
            None,
            show_cache if show_cache is not None else _EmptyCache(),
            clock=clock,
            logger=self.logger,
            _read_only=True,
        )

    @staticmethod
    def _raise_code(code):
        try:
            error_type, message = _ERRORS[code]
        except (KeyError, TypeError) as error:
            raise protocol.ProtocolError("unknown TMDB service error code") from error
        raise error_type(message)

    def _store_call(self, operation, *args):
        try:
            return operation(*args)
        except OSError:
            # Store I/O details are not protocol messages and never authorize a
            # parent transport or closure fallback.
            raise TMDBServiceUnavailableError("TMDB service unavailable") from None

    @staticmethod
    def _row_job(row, request):
        if type(row) is not dict:
            raise protocol.ProtocolError("service admission row must be an object")
        status = row.get("status")
        if status == "backpressured":
            if set(row) != {"status", "request", "code"}:
                raise protocol.ProtocolError("invalid rejected service admission")
            TMDBServiceClient._raise_code(row["code"])
        if status not in ("accepted", "joined"):
            raise protocol.ProtocolError("unknown service admission status")
        expected = {"status", "job_id", "request"}
        if status == "accepted":
            expected.add("reservation")
        if set(row) != expected:
            raise protocol.ProtocolError("invalid accepted service admission")
        job_id = row["job_id"]
        if type(job_id) is not str or _IDENTIFIER.fullmatch(job_id) is None:
            raise protocol.ProtocolError("service returned a noncanonical job ID")
        returned_request = protocol.validate_job(row["request"])
        if returned_request != request:
            raise protocol.ProtocolError("service admission returned another request")
        if status == "accepted" and type(row["reservation"]) is not bool:
            raise protocol.ProtocolError("accepted reservation must be boolean")
        return job_id

    def _new_waiter_id(self):
        with self._lock:
            for _attempt in range(128):
                waiter_id = self._id_factory(16)
                if (
                    type(waiter_id) is str
                    and _IDENTIFIER.fullmatch(waiter_id) is not None
                    and waiter_id not in self._waiters
                ):
                    return waiter_id
            raise protocol.ProtocolError("could not allocate a unique waiter ID")

    def _register(self, request):
        with self._lock:
            if self._closed:
                raise TMDBAbortError("TMDB operation aborted")
            waiter_id = self._new_waiter_id()
            self._waiters[waiter_id] = {"job_id": None, "acknowledged": False}
        try:
            row = self._store_call(self.store.join, request, waiter_id)
            job_id = self._row_job(row, request)
        except BaseException:
            with self._lock:
                self._waiters.pop(waiter_id, None)
            raise
        with self._lock:
            self._waiters[waiter_id]["job_id"] = job_id
            closed = self._closed
        if closed:
            self._acknowledge(waiter_id)
            raise TMDBAbortError("TMDB operation aborted")
        return job_id, waiter_id

    def _acknowledge(self, waiter_id):
        with self._lock:
            record = self._waiters.get(waiter_id)
            if record is None or record["acknowledged"]:
                return
            job_id = record["job_id"]
            if job_id is None:
                # close() cannot ACK before join reveals the exact job ID. Keep
                # this pending handoff registered without joining its invocation.
                return
            record["acknowledged"] = True
            self._waiters.pop(waiter_id, None)
        try:
            self.store.acknowledge(job_id, waiter_id)
        except Exception:
            # ACK is reconciliation after the validated outcome. Its failure can
            # neither revoke a valid result nor authorize another submission.
            self.logger.warning("Unable to acknowledge TMDB service waiter", exc_info=True)

    def _wait_slice(self):
        if self.abort():
            return True
        if self.wait_for_abort is None:
            time.sleep(_WAIT_SLICE)
        elif self.wait_for_abort(_WAIT_SLICE):
            return True
        return self.abort()

    def _read_result(self, job_id, waiter_id):
        try:
            while True:
                with self._lock:
                    if self._closed:
                        raise TMDBAbortError("TMDB operation aborted")
                if self.abort():
                    raise TMDBAbortError("TMDB operation aborted")
                try:
                    candidate_probe = getattr(self.store, "result_candidate", None)
                    if candidate_probe is not None and not self._store_call(
                        candidate_probe, job_id
                    ):
                        document = None
                    else:
                        # A candidate merely triggers the authoritative locked
                        # manifest/result/digest/waiter read. It proves no success.
                        document = self._store_call(
                            self.store.read_wait_result, job_id, waiter_id
                        )
                except protocol.ProtocolError as error:
                    with self._lock:
                        closed = self._closed
                    if closed and str(error) == (
                        "waiter is not registered for this terminal result"
                    ):
                        raise TMDBAbortError("TMDB operation aborted") from None
                    raise
                if document is None:
                    if self._wait_slice():
                        raise TMDBAbortError("TMDB operation aborted")
                    continue
                validated = protocol.validate_result(document)
                if validated["job_id"] != job_id:
                    raise protocol.ProtocolError("result belongs to another waiter job")
                return validated
        finally:
            self._acknowledge(waiter_id)

    def _execute(self, request):
        job_id, waiter_id = self._register(request)
        document = self._read_result(job_id, waiter_id)
        if document["outcome"] == "succeeded":
            return copy.deepcopy(document["value"])
        self._raise_code(document["error"]["code"])

    @staticmethod
    def _scope_value(scope, name):
        if isinstance(scope, dict):
            return scope.get(name)
        return getattr(scope, name, None)

    def _session_binding(self):
        session_id = str(self.settings.get("tmdb_access_token") or "").strip()
        try:
            digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        except UnicodeError as error:
            raise protocol.ProtocolError("session binding must be scalar text") from error
        return "v1:" + digest

    @staticmethod
    def _decode_trailer(value):
        if type(value) is not dict or set(value) != {"status", "youtube_id"}:
            raise protocol.ProtocolError("trailer service result has invalid fields")
        status = value["status"]
        youtube_id = value["youtube_id"]
        if status == "hit":
            if not valid_youtube_id(youtube_id):
                raise protocol.ProtocolError("trailer hit requires a YouTube ID")
        elif status in ("missing", "error"):
            if youtube_id is not None:
                raise protocol.ProtocolError(
                    "missing/error trailer result cannot have a YouTube ID"
                )
        else:
            raise protocol.ProtocolError("unknown trailer result status")
        return TrailerLookup(status, youtube_id)

    @staticmethod
    def _decode_show(value):
        if type(value) is not dict or set(value) != {"value", "succeeded"}:
            raise protocol.ProtocolError("show service result has invalid fields")
        if type(value["succeeded"]) is not bool:
            raise protocol.ProtocolError("show succeeded must be boolean")
        episode = value["value"]
        if episode is not None and type(episode) is not dict:
            raise protocol.ProtocolError("show value must be an object or null")
        return copy.deepcopy(episode), value["succeeded"]

    def attach_repositories(self, trailer_repository, show_repository):
        """Route lookups through the service and make both parent caches read-only."""
        self._trailer_repository = trailer_repository
        self._show_repository = show_repository
        for repository in (trailer_repository, show_repository):
            with repository._lock:
                repository._coordinator = self
                repository._read_only = True
                repository._save_pending = False
                repository._dirty_entries.clear()
                repository._changes_since_flush = 0

    def run_shared(self, key, operation):
        """Decode a metadata result; *operation* is deliberately never invoked."""
        del operation
        try:
            family = key[0]
            if family == "trailer" and len(key) == 3:
                media = str(key[1]).strip()
                media_id = int(key[2])
                request = protocol.trailer_request(media, media_id)
            elif family == "show" and len(key) == 2:
                media_id = int(key[1])
                request = protocol.show_request(media_id)
            else:
                raise protocol.ProtocolError("unknown TMDB shared metadata key")
        except (TypeError, ValueError, IndexError) as error:
            raise protocol.ProtocolError("unknown TMDB shared metadata key") from error

        try:
            value = self._execute(request)
            if family == "trailer":
                return self._decode_trailer(value)
            return self._decode_show(value)
        except TMDBError:
            if family == "trailer":
                self.logger.exception("TMDB trailer service lookup failed")
                return TrailerLookup("error", None)
            if self._show_repository is not None:
                return self._show_repository._service_failure(media_id)
            raise

    def run_operation(self, request):
        request = protocol.validate_job(request)
        if request["operation"] not in _INTERACTIVE_OPERATIONS:
            raise protocol.ProtocolError("operation is not an interactive service request")
        value = self._execute(request)
        if type(value) is not dict:
            raise protocol.ProtocolError("interactive service result must be an object")
        return value

    def run_watchlist(self, request):
        request = protocol.validate_job(request)
        if request["operation"] != "watchlist.refresh":
            raise protocol.ProtocolError("run_watchlist requires watchlist.refresh")
        value = self.run_operation(request)
        if set(value) != {"scope", "revision", "generation"}:
            raise protocol.ProtocolError("watchlist service result has invalid fields")
        return {"scope": copy.deepcopy(value["scope"]), "revision": value["revision"]}

    def _submit_requests(self, requests):
        accepted = 0
        for start in range(0, len(requests), 50):
            batch = requests[start : start + 50]
            rows = self._store_call(self.store.submit, batch)
            if type(rows) is not list or len(rows) != len(batch):
                raise protocol.ProtocolError("service returned an invalid admission batch")
            for row in rows:
                if type(row) is not dict:
                    raise protocol.ProtocolError("service admission row must be an object")
                status = row.get("status")
                if status in ("accepted", "joined"):
                    accepted += 1
                elif status != "backpressured":
                    raise protocol.ProtocolError("unknown service admission status")
        return accepted

    def submit_metadata(
        self, media, trailer_ids=(), show_ids=(), missing_known=False
    ):
        with self._lock:
            if self._closed:
                return 0
        requests = []
        seen = set()
        normalized_media = str(media).strip()
        for media_id in trailer_ids:
            try:
                normalized_id = int(media_id)
                if not missing_known and not self._trailer_reader.needs_lookup(
                    normalized_media, normalized_id
                ):
                    continue
                request = protocol.trailer_request(normalized_media, normalized_id)
                key = protocol.dedup_key(request)
                if key not in seen:
                    requests.append(request)
                    seen.add(key)
            except (TypeError, ValueError, protocol.ProtocolError):
                continue
        for show_id in show_ids:
            try:
                normalized_id = int(show_id)
                if not missing_known and not self._show_reader.needs_lookup(
                    normalized_id
                ):
                    continue
                request = protocol.show_request(normalized_id)
                key = protocol.dedup_key(request)
                if key not in seen:
                    requests.append(request)
                    seen.add(key)
            except (TypeError, ValueError, protocol.ProtocolError):
                continue
        return self._submit_requests(requests)

    def submit_trailers(self, media, media_ids, missing_known=False):
        return self.submit_metadata(
            media, media_ids, (), missing_known=missing_known
        )

    def submit_shows(self, show_ids, missing_known=False):
        return self.submit_metadata("tv", (), show_ids, missing_known=missing_known)

    def submit_watchlist(
        self,
        scope,
        force=False,
        allow_missing_account=False,
        refresh_due=None,
        generation=None,
    ):
        if self.snapshot_store is None:
            return 0
        session_id = str(self.settings.get("tmdb_access_token") or "").strip()
        account_id = str(self.settings.get("tmdb_account_id") or "").strip()
        scope_account = str(self._scope_value(scope, "account_id") or "").strip()
        if not session_id or not (
            account_id == scope_account or (allow_missing_account and not account_id)
        ):
            return 0
        if self.locale_mapper() != self._scope_value(scope, "language"):
            return 0
        if not force:
            if refresh_due is None:
                refresh_due = self.snapshot_store.refresh_due(scope, self.clock())
            if not refresh_due:
                return 0
        if generation is None:
            generation = self.snapshot_store.generation(scope)
        request = protocol.watchlist_request(
            scope,
            generation,
            bool(force),
            bool(allow_missing_account),
            self._session_binding(),
        )
        with self._lock:
            if self._closed:
                return 0
        return self._submit_requests([request])

    def close(self, wait=False):
        """Detach this invocation only; never join or stop the global service."""
        del wait
        with self._lock:
            if self._closed and not self._waiters:
                return
            self._closed = True
            waiter_ids = list(self._waiters)
        for waiter_id in waiter_ids:
            self._acknowledge(waiter_id)
