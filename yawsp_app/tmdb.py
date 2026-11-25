"""TMDB transport, success-only caches, and bounded background prefetching."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import threading
import time
from typing import Literal, Optional

import requests

from yawsp_app.watchlist_snapshot import validated_results


_LOGGER = logging.getLogger(__name__)
_API_URL = "https://api.themoviedb.org"
_TIMEOUT = 15
_MAX_ATTEMPTS = 3
_RETRY_WAIT_SLICE = 0.25
_FALLBACK_RETRY_CAP = 30.0
_MAX_WATCHLIST_PAGES = 1000
TRAILER_TTL = 7 * 24 * 60 * 60
TRAILER_MISSING_TTL = 6 * 60 * 60
SHOW_TTL = 24 * 60 * 60
WATCHLIST_PREFETCH_LIMIT = 50
_METADATA_RETRY_BACKOFF = 0.05
_MISSING = object()
_CANCELLED = object()


class TMDBError(RuntimeError):
    """A TMDB transport, HTTP, or response validation failure."""


class TMDBAbortError(TMDBError):
    """The caller's abort predicate interrupted TMDB work."""


@dataclass(frozen=True)
class TrailerLookup:
    status: Literal["hit", "missing", "error"]
    youtube_id: Optional[str]


class TMDBClient:
    """TMDB v3 client with injectable time, abort, locale, and persistence."""

    def __init__(
        self,
        session,
        settings,
        locale_mapper,
        api_url=_API_URL,
        clock=time.time,
        sleeper=time.sleep,
        abort=None,
        logger=None,
        identity_lock=None,
    ):
        self.session = session
        self.settings = settings
        self.locale_mapper = locale_mapper
        self.api_url = api_url.rstrip("/")
        self.clock = clock
        self.sleeper = sleeper
        self.abort = abort or (lambda: False)
        self.logger = logger or _LOGGER
        self.identity_lock = identity_lock

    def _setting(self, key):
        value = self.settings.get(key)
        return "" if value is None else str(value).strip()

    def invalidate_identity(self):
        """Clear account identity before a new session is stored or used."""
        self.settings.set("tmdb_account_id", "")

    def invalidate_session(self):
        """Invalidate both the v3 session and its account-specific identity."""
        self.invalidate_identity()
        self.settings.set("tmdb_access_token", "")

    def _invalidate_request_session(self, session_id):
        """Clear a 401 identity only while its request session is still current."""
        with self.identity_lock or nullcontext():
            if self._setting("tmdb_access_token") == session_id:
                self.invalidate_session()

    def get_account_id(self):
        account_id = self._setting("tmdb_account_id")
        if account_id:
            return account_id

        data = self.get("/3/account")
        raw_id = data.get("id")
        account_id = "" if raw_id is None else str(raw_id).strip()
        if not account_id:
            error = ValueError("TMDB account response has no account id")
            raise TMDBError("TMDB account response has no account id") from error
        self.settings.set("tmdb_account_id", account_id)
        return account_id

    def _fallback_delay(self, retry_index):
        return min(float(2**retry_index), _FALLBACK_RETRY_CAP)

    def _retry_delay(self, response, retry_index):
        value = response.headers.get("Retry-After")
        if value is None:
            return self._fallback_delay(retry_index)
        value = value.strip()
        try:
            seconds = int(value)
            if seconds < 0:
                raise ValueError("negative Retry-After")
            return float(seconds)
        except (ValueError, OverflowError):
            pass

        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at is None:
                raise ValueError("missing HTTP date")
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            now = datetime.fromtimestamp(self.clock(), timezone.utc)
            return max(0.0, (retry_at - now).total_seconds())
        except (TypeError, ValueError, OverflowError, OSError):
            return self._fallback_delay(retry_index)

    def _wait(self, seconds):
        # Decrement locally as well as using an injected clock: a test sleeper
        # that does not advance its clock still produces a finite wait.
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if self.abort():
                raise TMDBAbortError("TMDB retry wait aborted")
            interval = min(remaining, _RETRY_WAIT_SLICE)
            self.sleeper(interval)
            remaining -= interval

    def request(
        self, method, path, params=None, json_data=None, invalidate_on_unauthorized=True
    ):
        query = {"language": self.locale_mapper()}
        if params:
            query.update(params)
        api_key = self._setting("tmdb_api_key")
        if api_key:
            query["api_key"] = api_key
        session_id = self._setting("tmdb_access_token")
        if session_id:
            query["session_id"] = session_id

        for attempt in range(_MAX_ATTEMPTS):
            if self.abort():
                raise TMDBAbortError("TMDB request aborted")
            try:
                response = self.session.request(
                    method,
                    self.api_url + path,
                    headers={
                        "accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    params=query,
                    json=json_data,
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as error:
                error.args = ()
                raise TMDBError("TMDB {0} request failed".format(path)) from error

            if response.status_code == 429 and attempt + 1 < _MAX_ATTEMPTS:
                delay = self._retry_delay(response, attempt)
                response.close()
                self._wait(delay)
                continue

            succeeded = False
            try:
                response.raise_for_status()
            except requests.RequestException as error:
                if response.status_code == 401 and invalidate_on_unauthorized:
                    self._invalidate_request_session(session_id)
                error.args = ()
                raise TMDBError(
                    "TMDB {0} returned HTTP {1}".format(path, response.status_code)
                ) from error
            else:
                succeeded = True
                return response
            finally:
                if not succeeded:
                    response.close()

        raise TMDBError("TMDB retry handling exhausted without a response")

    def get(self, path, params=None):
        response = self.request("GET", path, params=params)
        try:
            try:
                data = response.json()
            except (ValueError, TypeError) as error:
                raise TMDBError("TMDB {0} returned malformed JSON".format(path)) from error
            if not isinstance(data, dict):
                error = ValueError("TMDB JSON response root must be an object")
                raise TMDBError("TMDB {0} returned a non-object JSON response".format(path)) from error
            return data
        finally:
            response.close()


def fetch_complete_watchlist(client, scope, operation_valid=None):
    """Fetch and validate a new full list; never append to an older snapshot."""
    watchlist_path = "/3/account/{0}/watchlist/{1}".format(
        scope.account_id, scope.media
    )
    pages = {}
    expected_pages = None
    page = 1
    while expected_pages is None or page <= expected_pages:
        if operation_valid is not None and not operation_valid():
            raise TMDBError("TMDB watchlist identity changed during pagination")
        data = client.get(
            watchlist_path,
            {"sort_by": "created_at.desc", "page": page},
        )
        try:
            total_pages = data.get("total_pages", 1)
            if (
                type(total_pages) is not int
                or total_pages < 1
                or total_pages > _MAX_WATCHLIST_PAGES
            ):
                raise ValueError("invalid or excessive total_pages")
            response_page = data.get("page", page)
            if type(response_page) is not int or response_page != page:
                raise ValueError("duplicate or inconsistent response page")
            if expected_pages is None:
                expected_pages = total_pages
            elif total_pages != expected_pages:
                raise ValueError("total_pages changed during complete fetch")
            # Assignment by requested page makes accidental duplicate accumulation
            # impossible; every response page is validated before it participates.
            pages[page] = validated_results(data.get("results"))
        except (TypeError, ValueError) as error:
            raise TMDBError(
                "TMDB watchlist response failed complete-page validation"
            ) from error
        if operation_valid is not None and not operation_valid():
            raise TMDBError("TMDB watchlist identity changed during pagination")
        page += 1

    return [value for number in range(1, expected_pages + 1) for value in pages[number]]


class _Repository:
    def __init__(self, client, cache, clock=time.time, logger=None, _defer_save=False):
        self.client = client
        self.cache = cache
        self.clock = clock
        self.logger = logger or _LOGGER
        self._entries = None
        self._lock = threading.RLock()
        self._defer_save = _defer_save
        self._coordinator = None
        self._save_pending = False
        self._dirty_entries = {}
        self._changes_since_flush = 0

    def _shared_lookup(self, key, operation):
        if self._coordinator is None:
            return operation()
        return self._coordinator.run_shared(key, operation)

    def _load(self):
        if self._entries is None:
            self._entries = self.cache.load()

    def _save_success(self, key):
        """Persist immediately on demand, or checkpoint a worker after eight successes."""
        self._save_pending = True
        self._dirty_entries[key] = self._entries[key]
        if not self._defer_save:
            self._flush()
            return
        self._changes_since_flush += 1
        if self._changes_since_flush >= 8:
            self._flush()

    @staticmethod
    def _prefer_deferred_entry(candidate, current):
        if not isinstance(current, dict) or not _Repository._valid_timestamp(
            current.get("ts")
        ):
            return True
        return candidate["ts"] >= current["ts"]

    def _flush(self):
        with self._lock:
            if not self._save_pending:
                return
            if self._defer_save:
                saved = self.cache.save_merged(
                    self._dirty_entries, prefer_update=self._prefer_deferred_entry
                )
            else:
                saved = self.cache.save(self._entries)
            if saved:
                self._save_pending = False
                self._dirty_entries.clear()
                self._changes_since_flush = 0

    @staticmethod
    def _valid_timestamp(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )


class TrailerRepository(_Repository):
    """Cache only successful videos responses, never transport/API failures."""

    def _load(self):
        if self._entries is not None:
            return
        super()._load()
        migrated = False
        for key, raw in list(self._entries.items()):
            if raw is None:
                del self._entries[key]
                migrated = True
            elif isinstance(raw, str) and raw.strip():
                self._entries[key] = {"ts": self.clock(), "youtube_id": raw.strip()}
                migrated = True
        if migrated:
            # Persist once so migration time, and therefore the seven-day TTL,
            # does not restart whenever a new repository opens the old cache.
            self.cache.save(self._entries)

    def _entry(self, key):
        raw = self._entries.get(key, _MISSING)
        if raw is _MISSING:
            return None
        # Bare non-empty IDs are the valid legacy representation. Legacy None
        # is deliberately not upgraded into a six-hour negative response.
        if isinstance(raw, str) and raw.strip():
            entry = {"ts": self.clock(), "youtube_id": raw.strip()}
            self._entries[key] = entry
            return entry
        if not isinstance(raw, dict) or not self._valid_timestamp(raw.get("ts")):
            self._entries.pop(key, None)
            return None
        youtube_id = raw.get("youtube_id", _MISSING)
        if youtube_id is not None and not (
            isinstance(youtube_id, str) and youtube_id.strip()
        ):
            self._entries.pop(key, None)
            return None
        return {"ts": raw["ts"], "youtube_id": youtube_id}

    @staticmethod
    def _key(media, media_id):
        media = str(media).strip()
        if not media:
            raise ValueError("media must not be empty")
        return "{0}:{1}".format(media, int(media_id))

    def _fresh(self, entry):
        if entry is None:
            return False
        ttl = TRAILER_MISSING_TTL if entry["youtube_id"] is None else TRAILER_TTL
        return self.clock() - entry["ts"] < ttl

    def needs_lookup(self, media, media_id):
        with self._lock:
            self._load()
            try:
                key = self._key(media, media_id)
            except (TypeError, ValueError):
                return False
            return not self._fresh(self._entry(key))

    @staticmethod
    def _select_youtube_id(data):
        results = data.get("results")
        if not isinstance(results, list):
            raise TMDBError("TMDB videos response has no results array")
        videos = [
            video
            for video in results
            if isinstance(video, dict) and video.get("site") == "YouTube"
        ]
        trailers = [video for video in videos if video.get("type") == "Trailer"]
        official = [video for video in trailers if video.get("official")]
        candidates = official or trailers or videos
        if not candidates:
            return None
        youtube_id = candidates[0].get("key")
        if not isinstance(youtube_id, str) or not youtube_id.strip():
            raise TMDBError("Selected TMDB YouTube video has no valid id")
        return youtube_id.strip()

    def lookup(self, media, media_id):
        normalized_id = int(media_id)
        normalized_media = str(media).strip()
        return self._shared_lookup(
            ("trailer", normalized_media, normalized_id),
            lambda: self._lookup(normalized_media, normalized_id),
        )

    def _lookup(self, media, media_id):
        key = self._key(media, media_id)
        with self._lock:
            self._load()
            cached = self._entry(key)
            if self._fresh(cached):
                if cached["youtube_id"] is None:
                    return TrailerLookup("missing", None)
                return TrailerLookup("hit", cached["youtube_id"])

            try:
                data = self.client.get(
                    "/3/{0}/{1}/videos".format(media, int(media_id)),
                    {"language": "en-US"},
                )
                youtube_id = self._select_youtube_id(data)
            except Exception:
                self.logger.exception("TMDB trailer lookup failed for %s", key)
                return TrailerLookup("error", None)

            entry = {"ts": self.clock(), "youtube_id": youtube_id}
            self._entries[key] = entry
            self._save_success(key)
            if youtube_id is None:
                return TrailerLookup("missing", None)
            return TrailerLookup("hit", youtube_id)


class ShowRepository(_Repository):
    """Cache next-episode successes and retain expired values only as fallback."""

    @staticmethod
    def _key(show_id):
        return str(int(show_id))

    def _entry(self, key):
        raw = self._entries.get(key, _MISSING)
        if raw is _MISSING:
            return None
        if not isinstance(raw, dict) or not self._valid_timestamp(raw.get("ts")):
            self._entries.pop(key, None)
            return None
        if "next" not in raw or not (raw["next"] is None or isinstance(raw["next"], dict)):
            self._entries.pop(key, None)
            return None
        next_episode = raw["next"]
        if next_episode is not None:
            season = next_episode.get("season")
            episode = next_episode.get("episode")
            air_date = next_episode.get("air_date")
            if (
                type(season) is not int
                or season < 0
                or type(episode) is not int
                or episode < 1
                or (air_date is not None and not isinstance(air_date, str))
            ):
                self._entries.pop(key, None)
                return None
            next_episode = {
                "season": season,
                "episode": episode,
                "air_date": air_date or "",
            }
        return {"ts": raw["ts"], "next": next_episode}

    def _fresh(self, entry):
        return entry is not None and self.clock() - entry["ts"] < SHOW_TTL

    def needs_lookup(self, show_id):
        with self._lock:
            self._load()
            try:
                key = self._key(show_id)
            except (TypeError, ValueError):
                return False
            return not self._fresh(self._entry(key))

    @staticmethod
    def _next_from_response(data):
        if "next_episode_to_air" not in data:
            raise TMDBError("TMDB show response is missing next_episode_to_air")
        episode = data["next_episode_to_air"]
        if episode is None:
            return None
        if not isinstance(episode, dict):
            raise TMDBError("TMDB next_episode_to_air must be an object or null")
        season = episode.get("season_number")
        episode_number = episode.get("episode_number")
        if (
            type(season) is not int
            or season < 0
            or type(episode_number) is not int
            or episode_number < 1
        ):
            raise TMDBError("TMDB next episode has invalid season or episode numbers")
        air_date = episode.get("air_date")
        if air_date is not None and not isinstance(air_date, str):
            raise TMDBError("TMDB next episode air_date must be a string or null")
        return {
            "season": season,
            "episode": episode_number,
            "air_date": air_date or "",
        }

    def next_episode(self, show_id):
        normalized_id = int(show_id)
        return self._shared_lookup(
            ("show", normalized_id),
            lambda: self._next_episode(normalized_id),
        )[0]

    def _next_episode(self, show_id):
        """Return the display value and whether fresh/successful data supplied it."""
        key = self._key(show_id)
        with self._lock:
            self._load()
            cached = self._entry(key)
            if self._fresh(cached):
                return cached["next"], True

            try:
                data = self.client.get("/3/tv/{0}".format(int(show_id)))
                next_episode = self._next_from_response(data)
            except Exception:
                self.logger.exception("TMDB show detail failed for %s", key)
                # Do not write an error timestamp: an expired value remains
                # displayable but every later call is still eligible to retry.
                return (cached["next"] if cached is not None else None), False

            self._entries[key] = {"ts": self.clock(), "next": next_episode}
            self._save_success(key)
            return next_episode, True


class GenreRepository:
    """Share process-local genre mappings and deduplicate concurrent requests."""

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._values = {}
        self._pending = set()

    def get(self, client, media, language):
        key = (media, language)
        with self._condition:
            while True:
                if key in self._values:
                    return dict(self._values[key])
                if key not in self._pending:
                    self._pending.add(key)
                    break
                self._condition.wait()

        try:
            data = client.get(
                "/3/genre/{0}/list".format(media), {"language": language}
            )
            raw_genres = data.get("genres")
            if not isinstance(raw_genres, list):
                raise TMDBError("TMDB genre response has no genres array")
            values = {
                genre["id"]: genre["name"]
                for genre in raw_genres
                if isinstance(genre, dict)
                and type(genre.get("id")) is int
                and isinstance(genre.get("name"), str)
            }
        except Exception:
            with self._condition:
                self._pending.discard(key)
                self._condition.notify_all()
            raise

        with self._condition:
            self._values[key] = values
            self._pending.discard(key)
            self._condition.notify_all()
        return dict(values)


class _WatchlistWorkerSettings:
    """Keep one refresh on its captured session without persisting that secret."""

    def __init__(self, settings, scope, session_id, identity_lock=None):
        self.settings = settings
        self.scope = scope
        self.session_id = session_id
        self.identity_lock = identity_lock

    def get(self, key):
        if key == "tmdb_access_token":
            return self.session_id
        if key == "tmdb_account_id":
            return self.scope.account_id
        return self.settings.get(key)

    def set(self, key, value):
        # A delayed 401 from an obsolete job must not invalidate a newly chosen
        # identity. Otherwise preserve TMDBClient's normal invalidation behavior.
        with self.identity_lock or nullcontext():
            if (
                str(self.settings.get("tmdb_access_token") or "").strip()
                == self.session_id
            ):
                self.settings.set(key, value)


class _WatchlistResolutionSettings:
    """Stage a resolved account ID until its captured identity is still current."""

    def __init__(self, settings, session_id, identity_valid, identity_lock=None):
        self.settings = settings
        self.session_id = session_id
        self.identity_valid = identity_valid
        self.identity_lock = identity_lock
        self.staged_account_id = ""

    def get(self, key):
        if key == "tmdb_access_token":
            return self.session_id
        if key == "tmdb_account_id":
            return self.staged_account_id
        return self.settings.get(key)

    def set(self, key, value):
        if key == "tmdb_account_id":
            self.staged_account_id = str(value).strip()
            return
        # Preserve normal 401 invalidation only while this lookup's session is
        # current; a newer session must never be changed by an obsolete response.
        with self.identity_lock or nullcontext():
            if (
                str(self.settings.get("tmdb_access_token") or "").strip()
                == self.session_id
            ):
                self.settings.set(key, value)

    def commit(self, account_id):
        """Publish a staged ID only while its captured identity is still current."""
        with self.identity_lock or nullcontext():
            current_account = str(self.settings.get("tmdb_account_id") or "").strip()
            if not (
                self.staged_account_id == account_id
                and current_account in ("", account_id)
                and self.identity_valid(account_id)
            ):
                return False
            self.settings.set("tmdb_account_id", account_id)
            return True


def resolve_watchlist_account(
    client, session_id, language, identity_valid, identity_lock=None
):
    """Resolve without letting an obsolete /account response overwrite new settings."""
    if not isinstance(client, TMDBClient):
        return client.get_account_id()
    cached_account = client._setting("tmdb_account_id")
    if cached_account:
        return cached_account

    settings = _WatchlistResolutionSettings(
        client.settings, session_id, identity_valid, identity_lock=identity_lock
    )
    resolver = TMDBClient(
        client.session,
        settings,
        lambda: language,
        api_url=client.api_url,
        clock=client.clock,
        sleeper=client.sleeper,
        abort=client.abort,
        logger=client.logger,
        identity_lock=identity_lock,
    )
    account_id = resolver.get_account_id()
    if not settings.commit(account_id):
        raise TMDBError("TMDB identity changed during account resolution")
    return account_id


def watchlist_operation_client(client, scope, session_id, identity_lock=None):
    """Freeze real client credentials/locale while retaining lightweight test clients."""
    if not isinstance(client, TMDBClient):
        return client
    return TMDBClient(
        client.session,
        _WatchlistWorkerSettings(
            client.settings, scope, session_id, identity_lock=identity_lock
        ),
        lambda: scope.language,
        api_url=client.api_url,
        clock=client.clock,
        sleeper=client.sleeper,
        abort=client.abort,
        logger=client.logger,
        identity_lock=identity_lock,
    )


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
                        lambda media=media, media_id=media_id: repositories[
                            "trailer"
                        ]._lookup(media, media_id),
                    )
                else:
                    result = self._run_reserved(
                        key,
                        lambda media_id=media_id: repositories["show"]._next_episode(
                            media_id
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
