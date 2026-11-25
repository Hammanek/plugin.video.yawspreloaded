"""TMDB transport, success-only caches, and bounded background prefetching."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import threading
import time
from typing import Literal, Optional

import requests


_LOGGER = logging.getLogger(__name__)
_API_URL = "https://api.themoviedb.org"
_TIMEOUT = 15
_MAX_ATTEMPTS = 3
_RETRY_WAIT_SLICE = 0.25
_FALLBACK_RETRY_CAP = 30.0
TRAILER_TTL = 7 * 24 * 60 * 60
TRAILER_MISSING_TTL = 6 * 60 * 60
SHOW_TTL = 24 * 60 * 60
_MISSING = object()


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
    ):
        self.session = session
        self.settings = settings
        self.locale_mapper = locale_mapper
        self.api_url = api_url.rstrip("/")
        self.clock = clock
        self.sleeper = sleeper
        self.abort = abort or (lambda: False)
        self.logger = logger or _LOGGER

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
                raise TMDBError("TMDB {0} request failed".format(path)) from error

            if response.status_code == 429 and attempt + 1 < _MAX_ATTEMPTS:
                delay = self._retry_delay(response, attempt)
                response.close()
                self._wait(delay)
                continue

            try:
                response.raise_for_status()
            except requests.RequestException as error:
                if response.status_code == 401 and invalidate_on_unauthorized:
                    self.invalidate_session()
                raise TMDBError(
                    "TMDB {0} returned HTTP {1}".format(path, response.status_code)
                ) from error
            return response

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


class _Repository:
    def __init__(self, client, cache, clock=time.time, logger=None):
        self.client = client
        self.cache = cache
        self.clock = clock
        self.logger = logger or _LOGGER
        self._entries = None
        self._lock = threading.RLock()

    def _load(self):
        if self._entries is None:
            self._entries = self.cache.load()

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
            self.cache.save(self._entries)
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
        return {"ts": raw["ts"], "next": raw["next"]}

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
        return {
            "season": season,
            "episode": episode_number,
            "air_date": episode.get("air_date") or "",
        }

    def next_episode(self, show_id):
        key = self._key(show_id)
        with self._lock:
            self._load()
            cached = self._entry(key)
            if self._fresh(cached):
                return cached["next"]

            try:
                data = self.client.get("/3/tv/{0}".format(int(show_id)))
                next_episode = self._next_from_response(data)
            except Exception:
                self.logger.exception("TMDB show detail failed for %s", key)
                # Do not write an error timestamp: an expired value remains
                # displayable but every later call is still eligible to retry.
                return cached["next"] if cached is not None else None

            self._entries[key] = {"ts": self.clock(), "next": next_episode}
            self.cache.save(self._entries)
            return next_episode


class BackgroundPrefetch:
    """A bounded, deduplicated queue with no presentation/UI responsibilities."""

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
    ):
        self.settings = settings
        self.trailer_cache = trailer_cache
        self.show_cache = show_cache
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
        self._pending = set()
        self._lock = threading.Lock()
        self._closed = False

    def _abort_requested(self):
        with self._lock:
            closed = self._closed
        return closed or self.abort()

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
        )

    def _reserve(self, keys):
        accepted = []
        with self._lock:
            if self._closed:
                return []
            for key in keys:
                if key in self._pending or len(self._pending) >= self.max_pending:
                    continue
                self._pending.add(key)
                accepted.append(key)
        return accepted

    def close(self, wait=False):
        """Stop accepting work and shut down this owner's executor exactly once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        shutdown = getattr(self.worker, "shutdown", None)
        if shutdown is not None:
            shutdown(wait=wait)

    def _release(self, keys):
        with self._lock:
            for key in keys:
                self._pending.discard(key)

    def _enqueue(self, keys, function):
        accepted = self._reserve(keys)
        if not accepted:
            return 0
        try:
            self.worker.submit(function, accepted)
        except Exception:
            self._release(accepted)
            self.logger.exception("Unable to submit TMDB background work")
            return 0
        return len(accepted)

    def submit_trailers(self, media, media_ids):
        reader = TrailerRepository(None, self.trailer_cache, clock=self.clock)
        keys = []
        seen = set()
        for media_id in media_ids:
            try:
                normalized_id = int(media_id)
                cache_key = reader._key(media, normalized_id)
            except (TypeError, ValueError):
                continue
            key = ("trailer", str(media).strip(), normalized_id)
            if key in seen or not reader.needs_lookup(media, normalized_id):
                continue
            seen.add(key)
            keys.append((key, cache_key, media, normalized_id))
        return self._enqueue(keys, self._run_trailers)

    def submit_shows(self, show_ids):
        reader = ShowRepository(None, self.show_cache, clock=self.clock)
        keys = []
        seen = set()
        for show_id in show_ids:
            try:
                normalized_id = int(show_id)
                cache_key = reader._key(normalized_id)
            except (TypeError, ValueError):
                continue
            key = ("show", normalized_id)
            if key in seen or not reader.needs_lookup(normalized_id):
                continue
            seen.add(key)
            keys.append((key, cache_key, normalized_id))
        return self._enqueue(keys, self._run_shows)

    def _finish(self, session, keys):
        try:
            if session is not None:
                session.close()
        except Exception:
            self.logger.exception("Unable to close TMDB background HTTP session")
        finally:
            self._release([item[0] for item in keys])

    def _run_trailers(self, keys):
        session = None
        try:
            # The executor job, not the directory/UI thread, creates and owns
            # this HTTP session for its lifetime.
            session = self.session_factory()
            repository = TrailerRepository(
                self._client(session), self.trailer_cache, clock=self.clock, logger=self.logger
            )
            for _, cache_key, media, media_id in keys:
                if self._abort_requested():
                    break
                repository.lookup(media, media_id)
        except Exception:
            self.logger.exception("TMDB trailer prefetch failed")
        finally:
            self._finish(session, keys)

    def _run_shows(self, keys):
        session = None
        try:
            session = self.session_factory()
            repository = ShowRepository(
                self._client(session), self.show_cache, clock=self.clock, logger=self.logger
            )
            for _, cache_key, show_id in keys:
                if self._abort_requested():
                    break
                repository.next_episode(show_id)
        except Exception:
            self.logger.exception("TMDB show prefetch failed")
        finally:
            self._finish(session, keys)
