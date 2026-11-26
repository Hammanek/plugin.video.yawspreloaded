# Test-only legacy scheduler for older domain tests; not a runtime module.

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time

import requests

from yawsp_app.tmdb import (
    GenreRepository,
    ShowRepository,
    TMDBClient,
    TrailerRepository,
    _API_URL,
    _METADATA_RETRY_BACKOFF,
    _MISSING,
    _WatchlistWorkerSettings,
    execute_show_metadata,
    execute_trailer_metadata,
    fetch_complete_watchlist,
)

_LOGGER = logging.getLogger("yawsp_app.tmdb")
_CANCELLED = object()


class _PendingLookup:
    """One reservation's result, shared with any concurrent on-demand caller."""

    def __init__(self):
        self.done = False
        self.result = _MISSING
        self.error = None


class BackgroundPrefetch:
    """A bounded, draining metadata queue with no presentation/UI responsibilities."""

    def __init__(
        self,
        settings,
        trailer_cache,
        show_cache,
        locale_mapper,
        session_factory=requests.Session,
        worker=None,
        abort=None,
        clock=time.time,
        sleeper=time.sleep,
        max_pending=32,
        api_url=_API_URL,
        logger=None,
        snapshot_store=None,
        genre_repository=None,
    ):
        self.settings = settings
        self.trailer_cache = trailer_cache
        self.show_cache = show_cache
        self.snapshot_store = snapshot_store
        self.genre_repository = genre_repository or GenreRepository()
        self.locale_mapper = locale_mapper
        self.session_factory = session_factory
        self.worker = worker or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tmdb-prefetch"
        )
        self.abort = abort or (lambda: False)
        self.clock = clock
        self.sleeper = sleeper
        self.max_pending = max(1, int(max_pending))
        self.api_url = api_url
        self.logger = logger or _LOGGER
        self._pending = {}
        self._watchlist_pending = set()
        self._metadata_queues = {"trailer": deque(), "show": deque()}
        self._metadata_queued = set()
        self._next_metadata_family = "trailer"
        self._error_retry_at = {}
        self._watchlist_followups = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._closed = False

    def _abort_requested(self):
        with self._condition:
            if self._closed:
                return True
            aborted = self.abort()
            if aborted:
                self._cancel_metadata()
            return aborted

    def _cancel_metadata(self):
        """Forget both active and deferred derivative work on close/observed abort."""
        with self._condition:
            self._metadata_queues = {"trailer": deque(), "show": deque()}
            self._metadata_queued.clear()
            self._error_retry_at.clear()
            for key, flight in list(self._pending.items()):
                del self._pending[key]
                if not flight.done:
                    flight.done = True
                    flight.result = _CANCELLED
            self._condition.notify_all()

    def _client(self, session):
        return TMDBClient(
            session,
            self.settings,
            self.locale_mapper,
            api_url=self.api_url,
            clock=self.clock,
            sleeper=self.sleeper,
            abort=self._abort_requested,
            logger=self.logger,
            identity_lock=self._lock,
        )

    def attach_repositories(self, trailer_repository, show_repository):
        """Share this owner's reservations with the per-open on-demand repositories."""
        trailer_repository._coordinator = self
        show_repository._coordinator = self

    def _reserve(self, keys, ignore_capacity=False):
        accepted = []
        with self._condition:
            if self._closed:
                return []
            for key in keys:
                if ignore_capacity:
                    if key in self._watchlist_pending:
                        continue
                    self._watchlist_pending.add(key)
                    accepted.append(key)
                    continue
                if key in self._pending or len(self._pending) >= self.max_pending:
                    continue
                self._pending[key] = _PendingLookup()
                accepted.append(key)
        return accepted

    def close(self, wait=False):
        """Cancel every reservation and shut down this owner's executor once."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._cancel_metadata()
            self._watchlist_followups.clear()
            self._release(list(self._watchlist_pending))
        shutdown = getattr(self.worker, "shutdown", None)
        if shutdown is not None:
            shutdown(wait=wait)

    def _release(self, keys, preserve_metadata=()):
        preserve_metadata = set(preserve_metadata)
        with self._condition:
            for key in list(keys):
                self._watchlist_pending.discard(key)
                flight = self._pending.pop(key, None)
                if flight is None:
                    continue
                if not flight.done:
                    flight.done = True
                    flight.result = _CANCELLED
                if key not in preserve_metadata:
                    self._metadata_queued.discard(key)
            self._condition.notify_all()

    def _execute_flight(
        self, key, flight, operation, release=True, result_handler=None
    ):
        try:
            result = operation()
        except BaseException as error:
            with self._condition:
                flight.error = error
                flight.done = True
                if release and self._pending.get(key) is flight:
                    del self._pending[key]
                self._condition.notify_all()
            raise
        with self._condition:
            if result_handler is not None:
                result_handler(result)
            flight.result = result
            flight.done = True
            if release and self._pending.get(key) is flight:
                del self._pending[key]
            self._condition.notify_all()
        return result

    def _run_reserved(self, key, operation):
        with self._condition:
            flight = self._pending.get(key)
            if flight is None or flight.done:
                return _CANCELLED
        # Keep completed metadata reserved until its deferred cache flush. A new
        # caller in that interval receives this result instead of fetching again.
        return self._execute_flight(key, flight, operation, release=False)

    def _claim_deferred_locked(self, key):
        """Atomically turn a deferred queue item into an on-demand flight."""
        if key not in self._metadata_queued:
            return None
        family = key[0]
        queue = self._metadata_queues[family]
        for index, item in enumerate(queue):
            if item[0] == key:
                del queue[index]
                return item
        return None

    def _requeue_metadata_locked(self, item, front=False):
        queue = self._metadata_queues[item[2]]
        if any(queued[0] == item[0] for queued in queue):
            return
        self._metadata_queued.add(item[0])
        if front:
            queue.appendleft(item)
        else:
            queue.append(item)

    def _record_metadata_result(self, item, result):
        key, _, family, media, media_id, attempts, _ = item
        if result is _CANCELLED or self._closed or self.abort():
            return
        if family == "trailer":
            succeeded = result.status != "error"
        else:
            succeeded = result[1]
        if succeeded:
            self._metadata_queued.discard(key)
            self._error_retry_at.pop(key, None)
        elif attempts < 1:
            retry_at = self.clock() + _METADATA_RETRY_BACKOFF
            self._error_retry_at[key] = retry_at
            self._requeue_metadata_locked(
                self._metadata_item(
                    family,
                    item[1],
                    media,
                    media_id,
                    attempts=1,
                    retry_at=retry_at,
                )
            )
        else:
            # Errors remain memory-only and expire after one short backoff; no
            # timestamp or negative value enters the cache.
            self._metadata_queued.discard(key)
            self._error_retry_at[key] = self.clock() + _METADATA_RETRY_BACKOFF

    def run_shared(self, key, operation):
        """Run one operation per key; existing waiters receive that same outcome."""
        owned_flight = None
        claimed_item = None
        with self._condition:
            while True:
                flight = self._pending.get(key)
                if flight is not None:
                    while not flight.done:
                        self._condition.wait()
                    if flight.error is not None:
                        raise flight.error
                    if flight.result is not _CANCELLED:
                        return flight.result
                    continue
                if self._abort_requested():
                    break
                claimed_item = self._claim_deferred_locked(key)
                if (
                    claimed_item is not None
                    and len(self._pending) >= self.max_pending + 1
                ):
                    self._requeue_metadata_locked(claimed_item, front=True)
                    self._condition.wait()
                    continue
                if claimed_item is None and len(self._pending) >= self.max_pending:
                    self._condition.wait()
                    continue
                owned_flight = _PendingLookup()
                self._pending[key] = owned_flight
                break
        if owned_flight is None:
            return operation()
        try:
            if claimed_item is None:
                return self._execute_flight(key, owned_flight, operation)
            return self._execute_flight(
                key,
                owned_flight,
                operation,
                result_handler=lambda result: self._record_metadata_result(
                    claimed_item, result
                ),
            )
        except BaseException:
            if claimed_item is not None:
                with self._condition:
                    if not self._closed and not self.abort():
                        self._requeue_metadata_locked(claimed_item, front=True)
            raise
        finally:
            if claimed_item is not None and not self._abort_requested():
                self._schedule_metadata()

    @staticmethod
    def _metadata_item(family, cache_key, media, media_id, attempts=0, retry_at=0.0):
        key = (
            ("trailer", media, media_id)
            if family == "trailer"
            else ("show", media_id)
        )
        return key, cache_key, family, media, media_id, attempts, retry_at

    def _prepare_metadata(self, candidates):
        with self._condition:
            if self._closed:
                return
            for item in candidates:
                key, _, family, _, _, _, _ = item
                if key in self._metadata_queued or key in self._pending:
                    continue
                if self._error_retry_at.get(key, 0.0) > self.clock():
                    continue
                self._metadata_queued.add(key)
                self._metadata_queues[family].append(item)

    def _schedule_metadata(self):
        """Reserve an alternating batch, then owner-schedule its continuation."""
        with self._condition:
            if self._closed:
                return 0
            capacity = self.max_pending - len(self._pending)
            if capacity <= 0:
                return 0
            batch = []
            while len(batch) < capacity:
                family = self._next_metadata_family
                other = "show" if family == "trailer" else "trailer"
                if not self._metadata_queues[family]:
                    family = other
                if not self._metadata_queues[family]:
                    break
                batch.append(self._metadata_queues[family].popleft())
                self._next_metadata_family = other
            if not self._metadata_queues["trailer"] and self._metadata_queues["show"]:
                self._next_metadata_family = "show"
            elif not self._metadata_queues["show"] and self._metadata_queues["trailer"]:
                self._next_metadata_family = "trailer"
            owned_batch = []
            for item in batch:
                key = item[0]
                if key in self._pending:
                    continue
                self._pending[key] = _PendingLookup()
                owned_batch.append(item)
            batch = owned_batch
            keys = [item[0] for item in batch]
        try:
            if batch:
                self.worker.submit(self._run_metadata, batch)
        except Exception:
            with self._condition:
                for item in reversed(batch):
                    self._metadata_queues[item[2]].appendleft(item)
                for key in keys:
                    flight = self._pending.pop(key, None)
                    if flight is not None:
                        flight.done = True
                        flight.result = _CANCELLED
                self._condition.notify_all()
            self.logger.exception("Unable to submit TMDB metadata background work")
            return 0
        return len(batch)

    def submit_metadata(
        self, media, trailer_ids=(), show_ids=(), missing_known=False
    ):
        """Combine both families so a UI open has deterministic round-robin order."""
        candidates = []
        seen = set()
        trailer_reader = TrailerRepository(None, self.trailer_cache, clock=self.clock)
        normalized_media = str(media).strip()
        for media_id in trailer_ids:
            try:
                normalized_id = int(media_id)
                cache_key = trailer_reader._key(normalized_media, normalized_id)
            except (TypeError, ValueError):
                continue
            key = ("trailer", normalized_media, normalized_id)
            if key in seen or (
                not missing_known
                and not trailer_reader.needs_lookup(normalized_media, normalized_id)
            ):
                continue
            seen.add(key)
            candidates.append(
                self._metadata_item(
                    "trailer", cache_key, normalized_media, normalized_id
                )
            )
        show_reader = ShowRepository(None, self.show_cache, clock=self.clock)
        for show_id in show_ids:
            try:
                normalized_id = int(show_id)
                cache_key = show_reader._key(normalized_id)
            except (TypeError, ValueError):
                continue
            key = ("show", normalized_id)
            if key in seen or (
                not missing_known and not show_reader.needs_lookup(normalized_id)
            ):
                continue
            seen.add(key)
            candidates.append(
                self._metadata_item("show", cache_key, None, normalized_id)
            )
        self._prepare_metadata(candidates)
        return self._schedule_metadata()

    def submit_trailers(self, media, media_ids, missing_known=False):
        return self.submit_metadata(media, media_ids, (), missing_known=missing_known)

    def submit_shows(self, show_ids, missing_known=False):
        return self.submit_metadata("tv", (), show_ids, missing_known=missing_known)

    def _watchlist_identity(self, scope, session_id, allow_missing_account=False):
        account_id = str(self.settings.get("tmdb_account_id") or "").strip()
        current_session = str(self.settings.get("tmdb_access_token") or "").strip()
        return (
            bool(current_session)
            and current_session == session_id
            and (
                account_id == scope.account_id
                or (allow_missing_account and not account_id)
            )
            and self.locale_mapper() == scope.language
        )

    def submit_watchlist(
        self,
        scope,
        force=False,
        allow_missing_account=False,
        refresh_due=None,
        generation=None,
    ):
        """Reserve one compatible refresh; all network activity stays off the UI."""
        if self.snapshot_store is None:
            return 0
        session_id = str(self.settings.get("tmdb_access_token") or "").strip()
        if not self._watchlist_identity(
            scope, session_id, allow_missing_account
        ):
            return 0
        if not force:
            if refresh_due is None:
                refresh_due = self.snapshot_store.refresh_due(scope, self.clock())
            if not refresh_due:
                return 0
        key = ("watchlist", scope)
        if generation is None:
            generation = self.snapshot_store.generation(scope)
        # Full account snapshots have an independent reservation: derivative
        # trailer/show bursts must never postpone identity refreshes indefinitely.
        accepted = self._reserve([key], ignore_capacity=True)
        if not accepted:
            if force:
                with self._lock:
                    if key in self._watchlist_pending and not self._closed:
                        # A forced request made after a local mutation must not be
                        # lost behind an older fetch. Coalesce repeated requests to
                        # exactly one later job rather than recursively refreshing.
                        self._watchlist_followups[scope] = allow_missing_account
                        return 1
            return 0
        try:
            self.worker.submit(
                self._run_watchlist,
                accepted,
                scope,
                session_id,
                generation,
                allow_missing_account,
            )
        except Exception:
            self._release(accepted)
            self.logger.exception("Unable to submit TMDB watchlist refresh")
            return 0
        return 1

    def _run_watchlist(
        self, keys, scope, session_id, generation, allow_missing_account=False
    ):
        session = None
        try:
            if self._abort_requested() or not self._watchlist_identity(
                scope, session_id, allow_missing_account
            ):
                return
            session = self.session_factory()
            worker_settings = _WatchlistWorkerSettings(
                self.settings, scope, session_id, identity_lock=self._lock
            )
            client = TMDBClient(
                session,
                worker_settings,
                lambda: scope.language,
                api_url=self.api_url,
                clock=self.clock,
                sleeper=self.sleeper,
                abort=self._abort_requested,
                logger=self.logger,
                identity_lock=self._lock,
            )
            results = fetch_complete_watchlist(
                client,
                scope,
                operation_valid=lambda: self._watchlist_identity(scope, session_id, allow_missing_account),
            )
            # The account, session and language are all rechecked after the last
            # response. A switched identity cannot write either old or new scope.
            if not self._watchlist_identity(scope, session_id, allow_missing_account):
                return
            self.snapshot_store.save_complete(
                scope,
                results,
                self.clock(),
                expected_generation=generation,
            )
        except Exception:
            current_session = str(
                self.settings.get("tmdb_access_token") or ""
            ).strip()
            current_account = str(self.settings.get("tmdb_account_id") or "").strip()
            if not current_session and not current_account:
                try:
                    self.snapshot_store.invalidate_identity(scope.account_id)
                except Exception:
                    self.logger.exception("Unable to invalidate watchlist identity")
            if not (
                current_session
                and current_account
                and not self._watchlist_identity(scope, session_id, allow_missing_account)
            ):
                self.logger.exception("TMDB watchlist refresh failed")
        finally:
            self._finish(session, keys)
            with self._lock:
                followup_allow_missing = self._watchlist_followups.pop(scope, None)
            if followup_allow_missing is not None:
                self.submit_watchlist(
                    scope,
                    force=True,
                    allow_missing_account=followup_allow_missing,
                )
            if not self._abort_requested():
                self._schedule_metadata()

    def _finish(self, session, keys, preserve_metadata=()):
        try:
            if session is not None:
                session.close()
        except Exception:
            self.logger.exception("Unable to close TMDB background HTTP session")
        finally:
            self._release(keys, preserve_metadata=preserve_metadata)

    def _run_metadata(self, keys):
        """Run one mixed batch, retaining deferred saves and bounded retries."""
        session = None
        repositories = {}
        resolved_keys = set()
        unexpected_failure = False
        try:
            # The executor job, not the directory/UI thread, creates and owns
            # one HTTP session for this batch's entire lifetime.
            session = self.session_factory()
            client = self._client(session)
            repositories = {
                "trailer": TrailerRepository(
                    client,
                    self.trailer_cache,
                    clock=self.clock,
                    logger=self.logger,
                    _defer_save=True,
                ),
                "show": ShowRepository(
                    client,
                    self.show_cache,
                    clock=self.clock,
                    logger=self.logger,
                    _defer_save=True,
                ),
            }
            for item in keys:
                key, _, family, media, media_id, attempts, retry_at = item
                if self._abort_requested():
                    break
                delay = min(0.25, max(0.0, retry_at - self.clock()))
                if delay:
                    self.sleeper(delay)
                    if self._abort_requested():
                        break
                if family == "trailer":
                    result = self._run_reserved(
                        key,
                        lambda media=media, media_id=media_id: execute_trailer_metadata(
                            repositories["trailer"], media, media_id
                        ),
                    )
                else:
                    result = self._run_reserved(
                        key,
                        lambda media_id=media_id: execute_show_metadata(
                            repositories["show"], media_id
                        ),
                    )
                if result is _CANCELLED:
                    break
                with self._condition:
                    self._record_metadata_result(item, result)
                resolved_keys.add(key)
        except Exception:
            unexpected_failure = True
            self.logger.exception("TMDB metadata prefetch failed")
        finally:
            recoverable = []
            if unexpected_failure:
                with self._condition:
                    for item in keys:
                        key = item[0]
                        if key in resolved_keys:
                            continue
                        flight = self._pending.get(key)
                        if (
                            flight is not None
                            and flight.done
                            and flight.result is not _MISSING
                        ):
                            continue
                        recoverable.append(item)
            if not self._closed:
                for family, repository in repositories.items():
                    try:
                        repository._flush()
                    except Exception:
                        self.logger.exception(
                            "Unable to flush TMDB %s prefetch cache", family
                        )
            preserved = set()
            with self._condition:
                if recoverable and not self._closed:
                    # Publish runnable recovery entries before old-flight release.
                    # A direct caller can then claim the item and its retry semantics
                    # rather than becoming an unowned ordinary flight in the gap.
                    for item in reversed(recoverable):
                        self._requeue_metadata_locked(item, front=True)
                        preserved.add(item[0])
            self._finish(
                session,
                [item[0] for item in keys],
                preserve_metadata=preserved,
            )
            if not self._abort_requested():
                self._schedule_metadata()
