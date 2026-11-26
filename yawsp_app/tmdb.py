"""TMDB transport, read-only/read-write repositories, and serial domain helpers."""

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


class TMDBError(RuntimeError):
    """A TMDB transport, HTTP, or response validation failure."""


class TMDBAbortError(TMDBError):
    """The caller's abort predicate interrupted TMDB work."""


class TMDBServiceError(TMDBError):
    """A fixed, typed failure at the native TMDB service boundary."""


class TMDBServiceUnavailableError(TMDBServiceError):
    """The native TMDB service is unavailable."""


class TMDBServiceBackpressuredError(TMDBServiceError):
    """The native TMDB service rejected work at a bounded limit."""


class TMDBServiceShuttingDownError(TMDBServiceError):
    """The native TMDB service is stopping or this invocation is closed."""


class TMDBServiceResultExpiredError(TMDBServiceError):
    """The requested native service result is no longer retained."""


def _sanitize_requests_error(error):
    """Retain the direct cause type while keeping nested request details out of logs."""
    error.args = ()
    error.__context__ = None
    error.__cause__ = None
    error.__suppress_context__ = True


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
                _sanitize_requests_error(error)
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
                _sanitize_requests_error(error)
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


class _PublicTMDBClient(TMDBClient):
    """Use the shared transport without exposing or invalidating account identity."""

    def _setting(self, key):
        if key == "tmdb_access_token":
            return ""
        return super()._setting(key)

    def _invalidate_request_session(self, session_id):
        """Public responses carry no authority over the personal TMDB session."""


def public_tmdb_client(client):
    """Return a public-only real client while retaining lightweight test doubles."""
    if not isinstance(client, TMDBClient):
        return client
    return _PublicTMDBClient(
        client.session,
        client.settings,
        client.locale_mapper,
        api_url=client.api_url,
        clock=client.clock,
        sleeper=client.sleeper,
        abort=client.abort,
        logger=client.logger,
        identity_lock=client.identity_lock,
    )


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
    def __init__(
        self,
        client,
        cache,
        clock=time.time,
        logger=None,
        _defer_save=False,
        _read_only=False,
    ):
        self.client = client
        self.cache = cache
        self.clock = clock
        self.logger = logger or _LOGGER
        self._entries = None
        self._lock = threading.RLock()
        self._defer_save = _defer_save
        self._read_only = _read_only
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
        with self._lock:
            if self._read_only:
                return
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
            if self._read_only or not self._save_pending:
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
        if migrated and not self._read_only:
            # Only a service-owned repository may migrate legacy cache bytes.
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

    def _service_failure(self, show_id):
        """Keep the legacy display fallback when the parent service call fails."""
        key = self._key(show_id)
        self.logger.exception("TMDB show detail failed for %s", key)
        with self._lock:
            self._load()
            cached = self._entry(key)
            return (cached["next"] if cached is not None else None), False

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


def execute_trailer_metadata(repository, media, media_id):
    """Execute the stable trailer domain operation without a queue closure."""
    return repository._lookup(str(media).strip(), int(media_id))


def execute_show_metadata(repository, show_id):
    """Return the public next-episode value and its genuine-success flag."""
    return repository._next_episode(int(show_id))


class GenreRepository:
    """Share process-local genre mappings and optionally sequential public files."""

    def __init__(self, file_cache=None):
        self._file_cache = file_cache
        self._condition = threading.Condition(threading.RLock())
        self._values = {}
        self._pending = set()

    def get_cached(self, media, language):
        """Return only a persisted/process-memory hit; never perform transport or writes."""
        key = (media, language)
        file_cache = self._file_cache
        if media in ("movie", "tv") and language in (
            "en-US",
            "cs-CZ",
            "sk-SK",
        ):
            if file_cache is not None:
                persisted = file_cache.get(media, language)
                if persisted is not None:
                    return {
                        int(identifier): name for identifier, name in persisted.items()
                    }
        with self._condition:
            values = self._values.get(key)
            return dict(values) if values is not None else None

    def get(self, client, media, language):
        key = (media, language)
        file_cache = self._file_cache
        if media not in ("movie", "tv") or language not in (
            "en-US",
            "cs-CZ",
            "sk-SK",
        ):
            file_cache = None
        if file_cache is not None:
            try:
                persisted = file_cache.get(media, language)
                if persisted is not None:
                    return {
                        int(identifier): name for identifier, name in persisted.items()
                    }
            except Exception:
                _LOGGER.warning("Public genre cache lookup failed")

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

        if file_cache is not None:
            try:
                stored_values = {
                    str(identifier): name
                    for identifier, name in values.items()
                    if identifier > 0 and name.strip() and len(name) <= 200
                }
                stored = file_cache.store(media, language, stored_values)
            except Exception:
                _LOGGER.warning("Public genre cache commit failed")
            else:
                if not stored:
                    _LOGGER.warning("Public genre cache commit was not completed")

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


def execute_watched_removal(client, scope, media_id):
    """Post the existing watched mutation and validate its handled API success."""
    response = client.request(
        "POST",
        "/3/account/{0}/watchlist".format(scope.account_id),
        json_data={
            "media_type": "movie" if scope.media == "movies" else "tv",
            "media_id": int(media_id),
            "watchlist": False,
        },
    )
    try:
        try:
            data = response.json()
        except (TypeError, ValueError) as error:
            raise TMDBError("TMDB watched response is malformed") from error
        return (
            response.status_code in (200, 201)
            and isinstance(data, dict)
            and data.get("success") is True
        )
    finally:
        response.close()
