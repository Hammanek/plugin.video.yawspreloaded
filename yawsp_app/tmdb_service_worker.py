"""Native, serial execution of closed TMDB service operations.

This module is called only by the service invocation.  It deliberately has no
queue executor, parent transport callback, Kodi import, or UI responsibilities.
"""

import copy
import logging
import secrets
import time

import requests

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.tmdb import (
    _METADATA_RETRY_BACKOFF,
    ShowRepository,
    TMDBClient,
    TMDBError,
    TMDBServiceUnavailableError,
    TrailerRepository,
    execute_show_metadata,
    execute_trailer_metadata,
)


_LOGGER = logging.getLogger(__name__)
_IDLE_WAIT = 0.05
_METADATA_OPERATIONS = frozenset(("metadata.trailer", "metadata.show"))


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
        # Task 4 intentionally rejects these later operation families below.
        # Their factories are accepted now so Tasks 5/6 can supply real
        # service-local engines without changing this composition interface.
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
        self._engines = []

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

    def _ensure_engines(self):
        if self._session is None:
            # The only Requests session is constructed here, inside a draining
            # service invocation.  No caller-supplied transport is accepted.
            self._session = requests.Session()
        if not self._engines:
            self._engines.append(self.metadata_engine_factory(self._session))

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
        # A store completion is the sole terminal authority. Once it has been
        # attempted, a later cleanup fault can never authorize generic reject or
        # a conflicting second terminal transition.
        try:
            self.store.complete(claim, document)
        except Exception:
            self.logger.error("Unable to reconcile completed TMDB service result")

    def _abort_claim(self, claim, code):
        try:
            self.store.abort_claim(claim, code)
        except Exception:
            self.logger.error("Unable to reconcile final TMDB service error")

    def _execute_claim(self, claim):
        request = claim["request"]
        operation = request["operation"]
        if operation not in _METADATA_OPERATIONS:
            # Later families cannot invoke a parent callback or accidentally
            # construct HTTP while their real engines are not yet implemented.
            self._abort_claim(claim, "UNAVAILABLE")
            return
        try:
            self._ensure_engines()
            engine = self._engines[0]
            value = self._execute_metadata(claim, engine)
            document = protocol.make_result(claim["job_id"], "succeeded", value)
        except _OperationAborted:
            self._abort_claim(claim, "ABORTED")
            return
        except Exception:
            # Domain repositories normally convert transport/validation errors
            # into typed values. This branch isolates implementation poison and
            # never logs arbitrary exception text or nested request details.
            self.logger.error("TMDB metadata service operation failed")
            self._abort_claim(claim, "FAILED")
            return
        except BaseException:
            self.logger.error("TMDB metadata service operation is unwinding")
            code = "ABORTED" if self.abort() else "FAILED"
            self._abort_claim(claim, code)
            raise
        self._complete(claim, document)

    def _finish_cycle(self):
        try:
            for engine in self._engines:
                try:
                    engine.flush()
                except Exception:
                    self.logger.error("Unable to flush final TMDB service metadata")
            for engine in self._engines:
                try:
                    engine.close()
                except Exception:
                    self.logger.error("Unable to close TMDB service metadata engine")
        finally:
            session = self._session
            self._session = None
            self._engines = []
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
