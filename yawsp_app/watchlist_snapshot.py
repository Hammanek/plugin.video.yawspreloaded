"""Complete, account-scoped TMDB watchlist snapshots with atomic updates."""

import copy
from dataclasses import dataclass
import json
import math
from typing import Optional


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
    generation: int = 0


@dataclass(frozen=True)
class SnapshotRefreshState:
    snapshot: Optional[Snapshot]
    generation: int
    refresh_due: bool


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
    def _record(scope, results, created_at, revision, generation=0):
        return {
            "schema": scope.schema,
            "account_id": scope.account_id,
            "media": scope.media,
            "language": scope.language,
            "complete": True,
            "results": results,
            "created_at": created_at,
            "revision": revision,
            "generation": generation,
        }

    @staticmethod
    def _generation(record):
        if not isinstance(record, dict):
            return 0
        value = record.get("generation", 0)
        return value if type(value) is int and value >= 0 else 0

    @staticmethod
    def _revision(record):
        if not isinstance(record, dict):
            return 0
        value = record.get("revision")
        return value if type(value) is int and value >= 1 else 0

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
            or type(record.get("generation", 0)) is not int
            or record.get("generation", 0) < 0
        ):
            return None
        try:
            results = validated_results(record.get("results"))
        except (TypeError, ValueError):
            return None
        return Snapshot(
            scope,
            results,
            record["created_at"],
            record["revision"],
            WatchlistSnapshotStore._generation(record),
        )

    def load(self, scope):
        """Return only the exact compatible complete scope, never a sole fallback."""
        record = self.cache.load().get(self._key(scope))
        return self._snapshot(scope, record)

    def generation(self, scope):
        """Return the local-mutation generation captured by refresh operations."""
        with self.cache.transaction():
            return self._generation(self.cache.load().get(self._key(scope)))

    def save_complete(
        self,
        scope,
        results,
        created_at,
        expected_generation=None,
        increment_generation=False,
    ):
        """Atomically commit complete results unless a local mutation made them stale."""
        results = validated_results(results)
        if not _timestamp(created_at):
            raise ValueError("snapshot creation time must be finite")
        if expected_generation is not None and (
            type(expected_generation) is not int or expected_generation < 0
        ):
            raise ValueError("expected generation must be a non-negative integer")
        with self.cache.transaction():
            key = self._key(scope)
            current = self.cache.load()
            previous_record = current.get(key)
            generation = self._generation(previous_record)
            if (
                expected_generation is not None
                and generation != expected_generation
            ):
                return None
            if increment_generation:
                generation += 1
            record = self._record(
                scope,
                results,
                created_at,
                self._revision(previous_record) + 1,
                generation,
            )
            current[key] = record
            saved = self.cache.save(current)
        if not saved:
            raise SnapshotPersistenceError("Unable to persist complete watchlist snapshot")
        return Snapshot(
            scope, copy.deepcopy(results), created_at, record["revision"], generation
        )

    def invalidate(self, scope):
        """Tombstone one scope and make every older refresh commit stale."""
        with self.cache.transaction():
            key = self._key(scope)
            previous_record = self.cache.load().get(key)
            previous = self._snapshot(scope, previous_record)
            record = self._record(
                scope,
                [],
                previous.created_at if previous is not None else 0.0,
                self._revision(previous_record) + 1,
                self._generation(previous_record) + 1,
            )
            record["complete"] = False
            if not self.cache.save_merged({key: record}):
                raise SnapshotPersistenceError("Unable to invalidate watchlist snapshot")
        return previous is not None

    def invalidate_identity(self, account_id):
        """Forget every language/media row for an invalidated authenticated identity."""
        account_id = str(account_id).strip()
        if not account_id:
            return False
        with self.cache.transaction():
            current = self.cache.load()
            updates = {}
            for key, record in current.items():
                if not isinstance(record, dict) or record.get("account_id") != account_id:
                    continue
                record["revision"] = self._revision(record) + 1
                record["generation"] = self._generation(record) + 1
                record["complete"] = False
                record["results"] = []
                updates[key] = record
            if updates and not self.cache.save_merged(updates):
                raise SnapshotPersistenceError(
                    "Unable to invalidate account watchlist snapshots"
                )
        return bool(updates)

    def refresh_state(self, scope, now):
        """Capture a compatible snapshot, generation and refresh decision in one load."""
        if not _timestamp(now):
            raise ValueError("current time must be finite")
        record = self.cache.load().get(self._key(scope))
        snapshot = self._snapshot(scope, record)
        return SnapshotRefreshState(
            snapshot,
            self._generation(record),
            snapshot is None or now - snapshot.created_at >= self.refresh_interval,
        )

    def refresh_due(self, scope, now):
        return self.refresh_state(scope, now).refresh_due

    def remove(self, scope, media_id):
        """Atomically remove from the latest rows and invalidate older refresh work."""
        normalized_id = int(media_id)
        while True:
            with self.cache.transaction():
                previous_record = self.cache.load().get(self._key(scope))
                snapshot = self._snapshot(scope, previous_record)
                if snapshot is None:
                    self.invalidate(scope)
                    return None
                remaining = [
                    value for value in snapshot.results if value["id"] != normalized_id
                ]
                removed = len(remaining) != len(snapshot.results)
                saved = self.save_complete(
                    scope,
                    remaining,
                    snapshot.created_at,
                    expected_generation=snapshot.generation,
                    increment_generation=True,
                )
                if saved is not None:
                    return saved if removed else None
            # Only a reentrant/local test hook or a sharing caller able to enter
            # this same transaction can retry; ordinary threads serialize above.
