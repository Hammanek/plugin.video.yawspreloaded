"""Native, serial execution of closed TMDB service operations.

This module is called only by the service invocation.  It deliberately has no
queue executor, parent transport callback, Kodi import, or UI responsibilities.
"""

import copy
import hashlib
import logging
import secrets
import time

import requests

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.discovery_ui import (
    CATALOG_SOURCES,
    map_discovery_record,
    versioned_catalog_path,
)
from yawsp_app.tmdb import (
    _METADATA_RETRY_BACKOFF,
    ShowRepository,
    TMDBClient,
    TMDBError,
    TMDBServiceUnavailableError,
    TrailerRepository,
    execute_show_metadata,
    execute_trailer_metadata,
    execute_watched_removal,
    fetch_complete_watchlist,
    public_tmdb_client,
    resolve_watchlist_account,
    watchlist_operation_client,
)
from yawsp_app.tmdb_service_store import IdentityTransactionAdapter
from yawsp_app.tmdb_ui import map_episodes_response, map_seasons_response
from yawsp_app.watchlist_snapshot import SnapshotScope

_IdentityContext = IdentityTransactionAdapter


_LOGGER = logging.getLogger(__name__)
_IDLE_WAIT = 0.05
_METADATA_OPERATIONS = frozenset(("metadata.trailer", "metadata.show"))
_WATCHLIST_OPERATIONS = frozenset(
    ("watchlist.refresh", "watchlist.watched", "identity.invalidate")
)
_PUBLIC_OPERATIONS = frozenset(
    ("discovery.catalog", "details.seasons", "details.episodes")
)


class ServiceMetadataEngine:
    """One drain cycle's real trailer/show repositories and success-only saves."""

    def __init__(
        self,
        settings,
        trailer_cache,
        show_cache,
        locale_mapper,
        session,
        abort,
        logger,
        clock=time.time,
        sleeper=time.sleep,
    ):
        self.logger = logger or _LOGGER
        self.abort = abort or (lambda: False)
        self._closed = False
        client = TMDBClient(
            session,
            settings,
            locale_mapper,
            clock=clock,
            sleeper=sleeper,
            abort=self.abort,
            logger=self.logger,
        )
        self._repositories = {
            "trailer": TrailerRepository(
                client,
                trailer_cache,
                clock=clock,
                logger=self.logger,
                _defer_save=True,
            ),
            "show": ShowRepository(
                client,
                show_cache,
                clock=clock,
                logger=self.logger,
                _defer_save=True,
            ),
        }

    def execute(self, request):
        """Return a detached protocol value for one validated metadata request."""
        if self._closed:
            raise TMDBError("TMDB metadata engine is closed")
        request = protocol.validate_job(request)
        operation = request["operation"]
        if operation == "metadata.trailer":
            params = request["params"]
            lookup = execute_trailer_metadata(
                self._repositories["trailer"], params["media"], params["media_id"]
            )
            return {"status": lookup.status, "youtube_id": lookup.youtube_id}
        if operation == "metadata.show":
            value, succeeded = execute_show_metadata(
                self._repositories["show"], request["params"]["show_id"]
            )
            return {"value": copy.deepcopy(value), "succeeded": succeeded}
        raise protocol.ProtocolError("metadata engine received another operation family")

    def flush(self):
        """Commit only validated successes accumulated by this service engine."""
        first_error = None
        for family, repository in self._repositories.items():
            try:
                repository._flush()
            except Exception as error:
                if first_error is None:
                    first_error = error
                self.logger.error("Unable to flush TMDB %s service cache", family)
        if first_error is not None:
            raise TMDBError("TMDB metadata cache flush failed") from first_error

    def close(self):
        """Flush final completed successes; the runner owns and closes HTTP."""
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True


class ServiceWatchlistEngine:
    """Service-only complete snapshots, watched mutations, and scoped invalidation."""

    def __init__(
        self,
        settings,
        snapshot_store,
        locale_mapper,
        identity_lock,
        session,
        abort,
        logger,
        genre_repository=None,
    ):
        self.settings = settings
        self.genre_repository = genre_repository
        self.snapshot_store = snapshot_store
        self.locale_mapper = locale_mapper
        self.identity_lock = identity_lock
        self.session = session
        self.abort = abort or (lambda: False)
        self.logger = logger or _LOGGER
        self._claim = None
        self._closed = False
        self._client = TMDBClient(
            session,
            settings,
            locale_mapper,
            abort=self.abort,
            logger=self.logger,
            identity_lock=_IdentityContext(identity_lock),
        )

    def _identity(self):
        return _IdentityContext(self.identity_lock)

    @staticmethod
    def _setting(settings, key):
        return str(settings.get(key) or "").strip()

    @staticmethod
    def _binding(session_id):
        try:
            digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        except UnicodeError as error:
            raise TMDBError("TMDB session binding is invalid") from error
        return "v1:" + digest

    def _capture(self, request, scope, allow_missing=False):
        with self._identity():
            session_id = self._setting(self.settings, "tmdb_access_token")
            account_id = self._setting(self.settings, "tmdb_account_id")
            valid = (
                bool(session_id)
                and self._binding(session_id) == request["session_binding"]
                and (
                    account_id == scope.account_id
                    or (allow_missing and not account_id)
                )
                and self.locale_mapper() == scope.language
            )
        if not valid:
            raise TMDBError("TMDB identity changed before watchlist operation")
        return session_id, not account_id

    def _identity_matches_locked(self, scope, session_id, allow_missing=False):
        """Compare current identity only inside the caller's real OS transaction."""
        account_id = self._setting(self.settings, "tmdb_account_id")
        return (
            self._setting(self.settings, "tmdb_access_token") == session_id
            and (
                account_id == scope.account_id
                or (allow_missing and not account_id)
            )
            and self.locale_mapper() == scope.language
        )

    def _valid_operation(self, scope, session_id, allow_missing=False):
        def valid():
            with self._identity():
                return self._identity_matches_locked(
                    scope, session_id, allow_missing
                )

        return valid

    @staticmethod
    def _require_valid(valid):
        if not valid():
            raise TMDBError("TMDB identity changed during watchlist operation")

    def _require_valid_locked(self, scope, session_id, allow_missing=False):
        if not self._identity_matches_locked(scope, session_id, allow_missing):
            raise TMDBError("TMDB identity changed during watchlist operation")

    def _invalidate_if_cleared(self, scope):
        # Compare-and-tombstone is one OS transaction: a same-account QR save
        # cannot slip between the cleared check and scoped invalidation.
        with self._identity():
            cleared = not (
                self._setting(self.settings, "tmdb_access_token")
                or self._setting(self.settings, "tmdb_account_id")
            )
            if cleared:
                self.snapshot_store.invalidate_identity(scope.account_id)

    def _ensure_refresh(self, request, generation):
        if self._claim is None:
            return None
        scope = request["params"]["scope"]
        followup = protocol.watchlist_request(
            scope,
            generation,
            True,
            request["allow_missing_account"],
            request["session_binding"],
        )
        row = self.identity_lock.ensure_followup(self._claim, followup)
        if type(row) is not dict or row.get("status") not in ("accepted", "joined"):
            raise TMDBError("TMDB watchlist continuation could not be reserved")
        return row

    @staticmethod
    def _value(scope, revision, generation):
        return {
            "scope": {
                "account_id": scope.account_id,
                "media": scope.media,
                "language": scope.language,
                "schema": scope.schema,
            },
            "revision": revision,
            "generation": generation,
        }

    def bind_claim(self, claim):
        """Bind only the runner's current fenced claim to durable continuations."""
        if claim is not None and self._claim is not None:
            raise TMDBError("TMDB watchlist engine already has an active claim")
        self._claim = copy.deepcopy(claim)

    def _concrete_scope(self, request, allow_missing=False):
        raw_scope = request["params"]["scope"]
        if raw_scope["account_id"]:
            requested = SnapshotScope(**raw_scope)
        else:
            if request["operation"] == "watchlist.refresh" and not allow_missing:
                raise TMDBError("TMDB account scope is required")
            requested = None
        if requested is not None:
            return request, requested

        with self._identity():
            session_id = self._setting(self.settings, "tmdb_access_token")
            valid = (
                bool(session_id)
                and self._binding(session_id) == request["session_binding"]
                and not self._setting(self.settings, "tmdb_account_id")
                and self.locale_mapper() == raw_scope["language"]
            )
        if not valid:
            raise TMDBError("TMDB identity changed before account resolution")

        def resolution_valid(resolved_account):
            return (
                bool(resolved_account)
                and self._setting(self.settings, "tmdb_access_token") == session_id
                and self._setting(self.settings, "tmdb_account_id")
                in ("", resolved_account)
                and self.locale_mapper() == raw_scope["language"]
            )

        account_id = resolve_watchlist_account(
            self._client,
            session_id,
            raw_scope["language"],
            resolution_valid,
            identity_lock=self._identity(),
        )
        concrete = SnapshotScope(
            account_id, raw_scope["media"], raw_scope["language"], raw_scope["schema"]
        )
        request = copy.deepcopy(request)
        request["params"]["scope"] = dict(concrete.__dict__)
        return request, concrete

    def _refresh(self, request):
        allow_missing = request["allow_missing_account"]
        request, scope = self._concrete_scope(request, allow_missing)
        session_id, account_was_missing = self._capture(
            request, scope, allow_missing
        )
        valid = self._valid_operation(scope, session_id, allow_missing)
        self._require_valid(valid)
        if account_was_missing:
            def resolution_valid(resolved_account):
                return (
                    resolved_account == scope.account_id
                    and self._setting(self.settings, "tmdb_access_token") == session_id
                    and self._setting(self.settings, "tmdb_account_id") in ("", scope.account_id)
                    and self.locale_mapper() == scope.language
                )

            resolved = resolve_watchlist_account(
                self._client,
                session_id,
                scope.language,
                resolution_valid,
                identity_lock=self._identity(),
            )
            if resolved != scope.account_id:
                raise TMDBError("TMDB account resolution returned another account")
            self._require_valid(valid)
        client = watchlist_operation_client(
            self._client,
            scope,
            session_id,
            identity_lock=self._identity(),
        )
        try:
            results = fetch_complete_watchlist(client, scope, operation_valid=valid)
            if results and self.genre_repository is not None:
                # Complete the exact shared media/language mapping before the
                # nonnegative snapshot commit. Public requests cannot invalidate
                # or carry the captured personal session.
                self.genre_repository.get(
                    public_tmdb_client(client),
                    "movie" if scope.media == "movies" else "tv",
                    scope.language,
                )
            with self._identity():
                # Validation, generation-checked save and any ordinary watched
                # continuation share one identity -> snapshot -> manifest order.
                self._require_valid_locked(scope, session_id, allow_missing)
                saved = self.snapshot_store.save_complete(
                    scope,
                    results,
                    time.time(),
                    expected_generation=request["generation"],
                )
                if saved is None:
                    state = self.snapshot_store.refresh_state(scope, time.time())
                    if state.snapshot is not None:
                        self._ensure_refresh(request, state.generation)
                    raise TMDBError("TMDB watchlist generation changed during refresh")
                return self._value(scope, saved.revision, saved.generation)
        except Exception:
            # A failed list/genre refresh never partially replaces or tombstones
            # the previous complete bytes. Explicit identity.invalidate owns
            # scoped cleanup; settings-level 401 fencing remains in TMDBClient.
            raise

    def _watched(self, request):
        request, scope = self._concrete_scope(request)
        session_id, _account_was_missing = self._capture(request, scope)
        client = watchlist_operation_client(
            self._client,
            scope,
            session_id,
            identity_lock=self._identity(),
        )
        try:
            if not execute_watched_removal(
                client, scope, request["params"]["media_id"]
            ):
                raise TMDBError("TMDB watched operation was not accepted")
            with self._identity():
                self._require_valid_locked(scope, session_id)
                latest = self.snapshot_store.remove(
                    scope, request["params"]["media_id"]
                )
                if latest is None:
                    # An already-absent ID retains a latest complete snapshot;
                    # an incomplete/tombstone result cannot yield a revision or
                    # justify an automatic refresh on this business failure.
                    latest = self.snapshot_store.refresh_state(
                        scope, time.time()
                    ).snapshot
                if latest is None:
                    raise TMDBError("TMDB watched snapshot is not complete")
                self._ensure_refresh(
                    protocol.watchlist_request(
                        request["params"]["scope"],
                        latest.generation,
                        True,
                        False,
                        request["session_binding"],
                    ),
                    latest.generation,
                )
                return self._value(scope, latest.revision, latest.generation)
        except Exception:
            self._invalidate_if_cleared(scope)
            raise

    def _invalidate(self, request):
        account_id = request["params"]["account_id"]
        with self._identity():
            session_id = self._setting(self.settings, "tmdb_access_token")
            current_account = self._setting(self.settings, "tmdb_account_id")
            matches = self._binding(session_id) == request["session_binding"] and (
                not current_account or current_account == account_id
            )
            if not matches:
                return {"invalidated": False}
            return {
                "invalidated": self.snapshot_store.invalidate_identity(account_id)
            }

    def execute(self, request):
        """Execute one closed descriptor without holding identity locks over HTTP."""
        if self._closed:
            raise TMDBError("TMDB watchlist engine is closed")
        request = protocol.validate_job(request)
        operation = request["operation"]
        if operation == "watchlist.refresh":
            return self._refresh(request)
        if operation == "watchlist.watched":
            return self._watched(request)
        if operation == "identity.invalidate":
            return self._invalidate(request)
        raise protocol.ProtocolError("watchlist engine received another operation family")

    def flush(self):
        """Snapshot mutations commit synchronously; there is no deferred cache."""

    def close(self):
        self._closed = True


class ServiceOperationEngine:
    """Sessionless public catalogs and identity-bound serial TV detail reads."""

    def __init__(
        self,
        settings,
        genre_repository,
        locale_mapper,
        identity_lock,
        session,
        abort,
        logger,
    ):
        self.settings = settings
        self.genre_repository = genre_repository
        self.locale_mapper = locale_mapper
        self.identity_lock = identity_lock
        self.session = session
        self.abort = abort or (lambda: False)
        self.logger = logger or _LOGGER
        self._closed = False

    def _client(self, language, public=False):
        client = TMDBClient(
            self.session,
            self.settings,
            lambda: language if language is not None else self.locale_mapper(),
            abort=self.abort,
            logger=self.logger,
            identity_lock=_IdentityContext(self.identity_lock),
        )
        return public_tmdb_client(client) if public else client

    @staticmethod
    def _normalization_error():
        return TMDBError("TMDB operation response could not be normalized")

    def _catalog(self, request):
        params = request["params"]
        source = CATALOG_SOURCES[params["source"]]
        client = self._client(params["language"], public=True)
        data = client.get(versioned_catalog_path(source.path))
        values = data.get("results")
        if not isinstance(values, list):
            raise TMDBError("TMDB discovery response has no results array")
        if not values:
            return {"rows": []}
        genre_map = self.genre_repository.get(
            client, source.media, params["language"]
        )
        try:
            return {
                "rows": [
                    row
                    for row in (
                        map_discovery_record(source, value, genre_map)
                        for value in values
                    )
                    if row is not None
                ]
            }
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ) as error:
            raise self._normalization_error() from error

    def _seasons(self, request):
        params = request["params"]
        try:
            return map_seasons_response(
                self._client(params["language"]).get(
                    "/3/tv/{0}".format(params["show_id"])
                )
            )
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ) as error:
            raise self._normalization_error() from error

    def _episodes(self, request):
        params = request["params"]
        try:
            return map_episodes_response(
                self._client(params["language"]).get(
                    "/3/tv/{0}/season/{1}".format(
                        params["show_id"], params["season"]
                    )
                )
            )
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ) as error:
            raise self._normalization_error() from error

    def execute(self, request):
        """Return only bounded detached values for one validated named request."""
        if self._closed:
            raise TMDBError("TMDB operation engine is closed")
        request = protocol.validate_job(request)
        operation = request["operation"]
        if operation == "discovery.catalog":
            return self._catalog(request)
        if operation == "details.seasons":
            return self._seasons(request)
        if operation == "details.episodes":
            return self._episodes(request)
        raise protocol.ProtocolError("operation engine received another operation family")

    def flush(self):
        """GenreRepository commits each successful response synchronously."""

    def close(self):
        """The runner owns the shared session and every HTTP response is closed."""
        self._closed = True


class _OperationAborted(Exception):
    """Internal control flow; never serialized as an arbitrary exception."""


class ServiceRunner:
    """Claim and finish at most one manifest operation at a time on this invocation."""

    def __init__(
        self,
        store,
        metadata_engine_factory,
        watchlist_engine_factory,
        operation_engine_factory,
        abort,
        wait_for_abort,
        logger,
    ):
        self.store = store
        self.metadata_engine_factory = metadata_engine_factory
        # Domain ownership stays separate: identity invalidation belongs only
        # to the watchlist engine; public/detail reads use the operation engine.
        self.watchlist_engine_factory = watchlist_engine_factory
        self.operation_engine_factory = operation_engine_factory
        self.abort = abort or (lambda: False)
        self.wait_for_abort = wait_for_abort
        self.logger = logger or _LOGGER
        self._epoch = None
        self._started = False
        self._stopping = False
        self._closed = False
        self._session = None
        self._engines = {}

    def _ensure_started(self):
        if self._started:
            return
        if self._closed:
            raise TMDBServiceUnavailableError("TMDB service shutting down")
        if not self.store.lease.acquire():
            raise TMDBServiceUnavailableError("TMDB service unavailable")
        try:
            epoch = secrets.token_hex(16)
            self.store.recover(epoch)
            self.store.set_ready(epoch)
        except BaseException:
            self.store.lease.release()
            raise
        self._epoch = epoch
        self._started = True

    def _wait(self, seconds):
        if self.abort():
            raise _OperationAborted()
        if self.wait_for_abort is None:
            time.sleep(seconds)
        elif self.wait_for_abort(seconds):
            raise _OperationAborted()
        if self.abort():
            raise _OperationAborted()

    def _ensure_engines(self, operation):
        if operation in _METADATA_OPERATIONS:
            family = "metadata"
        elif operation in _WATCHLIST_OPERATIONS:
            family = "watchlist"
        else:
            family = "operation"
        engine = self._engines.get(family)
        if engine is not None:
            return engine
        if self._session is None:
            # The only Requests session is constructed here, inside a draining
            # service invocation.  No caller-supplied transport is accepted.
            self._session = requests.Session()
        if family == "metadata":
            engine = self.metadata_engine_factory(self._session)
        elif family == "watchlist":
            engine = self.watchlist_engine_factory(self._session)
        else:
            engine = self.operation_engine_factory(self._session)
        self._engines[family] = engine
        return engine

    @staticmethod
    def _valid_metadata_success(request, value):
        if request["operation"] == "metadata.trailer":
            return (
                type(value) is dict
                and set(value) == {"status", "youtube_id"}
                and value.get("status") in ("hit", "missing")
            )
        return (
            type(value) is dict
            and set(value) == {"value", "succeeded"}
            and value.get("succeeded") is True
        )

    def _execute_metadata(self, claim, engine):
        request = claim["request"]
        attempts = claim["attempts"]
        if claim["retry_at"]:
            # Preserve the legacy bounded retry sleep if a future store claim
            # carries a persisted retry time. Initial Task 4 claims use zero.
            delay = min(0.25, max(0.0, claim["retry_at"] - time.time()))
            if delay:
                self._wait(delay)
        while True:
            if self.abort():
                raise _OperationAborted()
            value = engine.execute(request)
            if self.abort():
                raise _OperationAborted()
            # A completed result is validated through the same closed codec as
            # other service values; domain failures still use succeeded IPC with
            # TrailerLookup("error") or (public value, False) semantics.
            candidate = protocol.make_result(claim["job_id"], "succeeded", value)
            if self._valid_metadata_success(request, value) or attempts >= 1:
                return candidate["value"]
            attempts = 1
            # The fenced job remains active across the single short deferred
            # domain retry. No second reservation, owner, or session is created.
            self._wait(_METADATA_RETRY_BACKOFF)

    def _complete(self, claim, document):
        # A store completion is the sole terminal authority. Reconcile the exact
        # active+fenced record: never guess whether its original result committed.
        try:
            self.store.complete(claim, document)
        except Exception:
            self.logger.error("Unable to publish TMDB service result; reconciling owner")
            try:
                self.store.reconcile_completion(claim)
            except Exception:
                # Persistent I/O can leave the active record for the next fresh
                # epoch to recover. Stop this owner instead of spinning behind it.
                self.logger.error("Unable to reconcile TMDB service owner; stopping")
                self.close()
                raise TMDBServiceUnavailableError(
                    "TMDB service result reconciliation failed"
                ) from None

    def _abort_claim(self, claim, code):
        try:
            self.store.abort_claim(claim, code)
        except Exception:
            self.logger.error("Unable to reconcile final TMDB service error")

    def _execute_watchlist(self, claim, engine):
        engine.bind_claim(claim)
        try:
            if self.abort():
                raise _OperationAborted()
            value = engine.execute(claim["request"])
            if self.abort():
                raise _OperationAborted()
            return value
        finally:
            engine.bind_claim(None)

    def _execute_claim(self, claim):
        request = claim["request"]
        operation = request["operation"]
        if operation not in (
            _METADATA_OPERATIONS | _WATCHLIST_OPERATIONS | _PUBLIC_OPERATIONS
        ):
            self._abort_claim(claim, "UNAVAILABLE")
            return
        if operation in _METADATA_OPERATIONS:
            family = "metadata"
        elif operation in _WATCHLIST_OPERATIONS:
            family = "watchlist"
        else:
            family = "operation"
        try:
            engine = self._ensure_engines(operation)
            if family == "metadata":
                value = self._execute_metadata(claim, engine)
            elif family == "watchlist":
                value = self._execute_watchlist(claim, engine)
            else:
                if self.abort():
                    raise _OperationAborted()
                value = engine.execute(request)
                if self.abort():
                    raise _OperationAborted()
            document = protocol.make_result(claim["job_id"], "succeeded", value)
        except _OperationAborted:
            self._abort_claim(claim, "ABORTED")
            return
        except Exception:
            # Domain repositories normally convert transport/validation errors
            # into typed values. This branch isolates implementation poison and
            # never logs arbitrary exception text or nested request details.
            self.logger.error("TMDB %s service operation failed", family)
            self._abort_claim(claim, "FAILED")
            return
        except BaseException:
            self.logger.error("TMDB %s service operation is unwinding", family)
            code = "ABORTED" if self.abort() else "FAILED"
            self._abort_claim(claim, code)
            raise
        self._complete(claim, document)

    def _finish_cycle(self):
        try:
            for engine in self._engines.values():
                try:
                    engine.flush()
                except Exception:
                    self.logger.error("Unable to flush final TMDB service metadata")
            for engine in self._engines.values():
                try:
                    engine.close()
                except Exception:
                    self.logger.error("Unable to close TMDB service metadata engine")
        finally:
            session = self._session
            self._session = None
            self._engines = {}
            if session is not None:
                try:
                    session.close()
                except Exception:
                    self.logger.error("Unable to close TMDB service HTTP session")

    def _stop(self):
        if self._stopping or not self._started:
            return
        self._stopping = True
        try:
            self.store.set_stopping(self._epoch)
        except Exception:
            self.logger.error("Unable to publish TMDB service stopping state")

    def run_once(self):
        """Drain currently eligible jobs serially with one session at most."""
        self._ensure_started()
        if self._closed:
            return False
        worked = False
        try:
            while not self._closed and not self.abort():
                claim = self.store.claim_next(self._epoch, secrets.token_hex(16))
                if claim is None:
                    break
                worked = True
                self._execute_claim(claim)
            if self.abort() or self._closed:
                self._stop()
            return worked
        finally:
            # Completed valid successes survive ordinary errors, abort, and
            # BaseException unwinding; response/session ownership ends here.
            self._finish_cycle()

    def run_until_abort(self):
        """Run directly on the service invocation and poll only when idle."""
        try:
            while not self._closed and not self.abort():
                worked = self.run_once()
                if not worked and not self._closed and not self.abort():
                    if self.wait_for_abort is None:
                        time.sleep(_IDLE_WAIT)
                    elif self.wait_for_abort(_IDLE_WAIT):
                        break
        finally:
            self.close()

    def close(self):
        """Stop unstarted work and release service-owned resources in finally."""
        if self._closed and not self.store.lease.acquired:
            return
        self._closed = True
        try:
            self._stop()
            self._finish_cycle()
        finally:
            self.store.lease.release()
