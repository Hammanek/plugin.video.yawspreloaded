"""Complete, account-scoped TMDB watchlist snapshots with atomic updates."""

import copy
from dataclasses import dataclass
import json
import math


SCHEMA_VERSION = 1
DEFAULT_REFRESH_INTERVAL = 5 * 60


@dataclass(frozen=True)
class SnapshotScope:
    """The non-secret identity of one compatible snapshot."""

    account_id: str
    media: str
    language: str
    schema: int = SCHEMA_VERSION

    def __post_init__(self):
        account_id = str(self.account_id).strip()
        language = str(self.language).strip()
        if not account_id:
            raise ValueError("snapshot account id must not be empty")
        if self.media not in ("movies", "tv"):
            raise ValueError("snapshot media must be movies or tv")
        if not language:
            raise ValueError("snapshot language must not be empty")
        if type(self.schema) is not int or self.schema != SCHEMA_VERSION:
            raise ValueError("unsupported snapshot schema")
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "language", language)


@dataclass(frozen=True)
class Snapshot:
    scope: SnapshotScope
    results: list
    created_at: float
    revision: int


class SnapshotPersistenceError(RuntimeError):
    """The previous complete snapshot could not be atomically updated."""


def _timestamp(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _valid_item(value):
    if not isinstance(value, dict):
        return False
    if type(value.get("id")) is not int or value["id"] <= 0:
        return False
    for key in (
        "title",
        "original_title",
        "name",
        "original_name",
        "release_date",
        "first_air_date",
        "overview",
        "poster_path",
        "backdrop_path",
    ):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            return False
    rating = value.get("vote_average")
    if rating is not None and not _timestamp(rating):
        return False
    genre_ids = value.get("genre_ids", [])
    return isinstance(genre_ids, list) and all(
        type(genre_id) is int for genre_id in genre_ids
    )


def validated_results(results):
    """Copy a complete result list only when every row is safe to render."""
    if not isinstance(results, list) or not all(_valid_item(value) for value in results):
        raise ValueError("watchlist results must be a list of renderable objects")
    return copy.deepcopy(results)


class WatchlistSnapshotStore:
    """Persist complete snapshots without replacing unrelated account scopes."""

    def __init__(self, cache, refresh_interval=DEFAULT_REFRESH_INTERVAL):
        if not _timestamp(refresh_interval) or refresh_interval < 0:
            raise ValueError("refresh interval must be a finite non-negative number")
        self.cache = cache
        self.refresh_interval = float(refresh_interval)

    @staticmethod
    def _key(scope):
        return json.dumps(
            [scope.schema, scope.account_id, scope.media, scope.language],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _record(scope, results, created_at, revision):
        return {
            "schema": scope.schema,
            "account_id": scope.account_id,
            "media": scope.media,
            "language": scope.language,
            "complete": True,
            "results": results,
            "created_at": created_at,
            "revision": revision,
        }

    @staticmethod
    def _snapshot(scope, record):
        if not isinstance(record, dict) or record.get("complete") is not True:
            return None
        if (
            record.get("schema") != scope.schema
            or record.get("account_id") != scope.account_id
            or record.get("media") != scope.media
            or record.get("language") != scope.language
            or not _timestamp(record.get("created_at"))
            or type(record.get("revision")) is not int
            or record["revision"] < 1
        ):
            return None
        try:
            results = validated_results(record.get("results"))
        except (TypeError, ValueError):
            return None
        return Snapshot(scope, results, record["created_at"], record["revision"])

    def load(self, scope):
        """Return only the exact compatible complete scope, never a sole fallback."""
        record = self.cache.load().get(self._key(scope))
        return self._snapshot(scope, record)

    def save_complete(self, scope, results, created_at):
        """Merge one validated complete result and preserve every other scope."""
        results = validated_results(results)
        if not _timestamp(created_at):
            raise ValueError("snapshot creation time must be finite")
        with self.cache._lock:
            key = self._key(scope)
            previous_record = self.cache.load().get(key)
            previous_revision = (
                previous_record.get("revision")
                if isinstance(previous_record, dict)
                else None
            )
            revision = (
                previous_revision + 1
                if type(previous_revision) is int and previous_revision >= 1
                else 1
            )
            record = self._record(scope, results, created_at, revision)
            saved = self.cache.save_merged({self._key(scope): record})
        if not saved:
            raise SnapshotPersistenceError("Unable to persist complete watchlist snapshot")
        return Snapshot(scope, copy.deepcopy(results), created_at, revision)

    def invalidate(self, scope):
        """Tombstone one scope with an update-only merge so peers survive."""
        with self.cache._lock:
            previous = self.load(scope)
            if previous is None:
                return False
            record = self._record(
                scope, [], previous.created_at, previous.revision + 1
            )
            record["complete"] = False
            if not self.cache.save_merged({self._key(scope): record}):
                raise SnapshotPersistenceError("Unable to invalidate watchlist snapshot")
        return True

    def invalidate_identity(self, account_id):
        """Forget every language/media row for an invalidated authenticated identity."""
        account_id = str(account_id).strip()
        if not account_id:
            return False
        with self.cache._lock:
            current = self.cache.load()
            updates = {}
            for key, record in current.items():
                if not isinstance(record, dict) or record.get("account_id") != account_id:
                    continue
                revision = record.get("revision")
                record["revision"] = (
                    revision + 1 if type(revision) is int and revision >= 1 else 1
                )
                record["complete"] = False
                record["results"] = []
                updates[key] = record
            if updates and not self.cache.save_merged(updates):
                raise SnapshotPersistenceError(
                    "Unable to invalidate account watchlist snapshots"
                )
        return bool(updates)

    def refresh_due(self, scope, now):
        if not _timestamp(now):
            raise ValueError("current time must be finite")
        snapshot = self.load(scope)
        return snapshot is None or now - snapshot.created_at >= self.refresh_interval

    def remove(self, scope, media_id):
        """Remove one watched row while retaining the original refresh timestamp."""
        snapshot = self.load(scope)
        if snapshot is None:
            return None
        normalized_id = int(media_id)
        remaining = [value for value in snapshot.results if value["id"] != normalized_id]
        if len(remaining) == len(snapshot.results):
            return None
        return self.save_complete(scope, remaining, snapshot.created_at)
