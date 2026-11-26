"""Test-only domain adapter for legacy UI compatibility assertions.

Production code never imports this module. It translates closed protocol requests
to already reviewed domain helpers while retaining the old cache/queue fixtures.
"""

import threading
import time

from yawsp_app import tmdb_service_protocol as protocol
from tests.support.background_prefetch import BackgroundPrefetch
from yawsp_app.discovery_ui import (
    CATALOG_SOURCES,
    map_discovery_record,
    versioned_catalog_path,
)
from yawsp_app.tmdb import (
    TMDBClient,
    TMDBError,
    execute_watched_removal,
    fetch_complete_watchlist,
    public_tmdb_client,
    resolve_watchlist_account,
    watchlist_operation_client,
)
from yawsp_app.tmdb_ui import map_episodes_response, map_seasons_response
from yawsp_app.watchlist_snapshot import SnapshotPersistenceError, SnapshotScope


class LegacyServiceAdapter:
    """Keep legacy UI tests meaningful without a production direct-writer path."""

    def __init__(
        self,
        settings,
        session,
        trailer_cache,
        show_cache,
        snapshot_store,
        genre_repository,
        locale_mapper,
        worker=None,
        client=None,
        clock=time.time,
        legacy=None,
    ):
        self.settings = settings
        self.session = session
        self.trailer_cache = trailer_cache
        self.show_cache = show_cache
        self.snapshot_store = snapshot_store
        self.genre_repository = genre_repository
        self.locale_mapper = locale_mapper
        self.clock = clock
        self.client = client
        self.legacy = legacy or BackgroundPrefetch(
            settings=settings,
            trailer_cache=trailer_cache,
            show_cache=show_cache,
            locale_mapper=locale_mapper,
            session_factory=lambda: session,
            worker=worker,
            clock=clock,
            snapshot_store=snapshot_store,
            genre_repository=genre_repository,
        )
        self._identity_lock = getattr(self.legacy, "_lock", threading.RLock())

    def __getattr__(self, name):
        return getattr(self.legacy, name)

    @property
    def worker(self):
        return self.legacy.worker

    @property
    def _lock(self):
        return self._identity_lock

    @_lock.setter
    def _lock(self, value):
        self._identity_lock = value
        if hasattr(self.legacy, "_lock"):
            self.legacy._lock = value

    def attach_repositories(self, trailers, shows):
        attach = getattr(self.legacy, "attach_repositories", None)
        if attach is not None:
            return attach(trailers, shows)

    def run_shared(self, key, operation):
        return self.legacy.run_shared(key, operation)

    def submit_metadata(self, *args, **kwargs):
        return self.legacy.submit_metadata(*args, **kwargs)

    def submit_watchlist(self, *args, **kwargs):
        return self.legacy.submit_watchlist(*args, **kwargs)

    def close(self, wait=False):
        return self.legacy.close(wait=wait)

    def _real_client(self):
        if self.client is not None:
            return self.client
        return TMDBClient(
            self.session,
            self.settings,
            self.locale_mapper,
            clock=self.clock,
            identity_lock=self._lock,
        )

    def _scope(self, raw_scope):
        if raw_scope["account_id"]:
            return SnapshotScope(**raw_scope)
        account_id = resolve_watchlist_account(
            self._real_client(),
            self.settings.get("tmdb_access_token"),
            raw_scope["language"],
            lambda _resolved: True,
            identity_lock=self._lock,
        )
        return SnapshotScope(
            account_id, raw_scope["media"], raw_scope["language"], raw_scope["schema"]
        )

    def _run_watchlist_legacy(self, request):
        request = protocol.validate_job(request)
        params = request["params"]
        scope = self._scope(params["scope"])
        params = dict(params, scope=dict(scope.__dict__))
        client = watchlist_operation_client(
            self._real_client(),
            scope,
            self.settings.get("tmdb_access_token"),
            identity_lock=self._lock,
        )
        results = fetch_complete_watchlist(client, scope)
        if results:
            self.genre_repository.get(
                public_tmdb_client(client),
                "movie" if scope.media == "movies" else "tv",
                scope.language,
            )
        saved = self.snapshot_store.save_complete(
            scope,
            results,
            self.clock(),
            expected_generation=request["generation"],
        )
        if saved is None:
            raise TMDBError("Watchlist changed during refresh")
        return {
            "scope": params["scope"],
            "revision": saved.revision,
            "generation": saved.generation,
        }

    def run_watchlist(self, request):
        try:
            return self._run_watchlist_legacy(request)
        except SnapshotPersistenceError as error:
            raise TMDBError("TMDB watchlist snapshot commit failed") from error

    def run_operation(self, request):
        request = protocol.validate_job(request)
        operation = request["operation"]
        params = request["params"]
        client = self._real_client()
        if operation == "discovery.catalog":
            source = CATALOG_SOURCES[params["source"]]
            client = public_tmdb_client(client)
            data = client.get(versioned_catalog_path(source.path))
            values = data.get("results")
            if not isinstance(values, list):
                raise TMDBError("TMDB discovery response has no results array")
            if not values:
                return {"rows": []}
            genre_map = self.genre_repository.get(
                public_tmdb_client(client), source.media, params["language"]
            )
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
        if operation == "details.seasons":
            value = map_seasons_response(
                client.get("/3/tv/{0}".format(params["show_id"]))
            )
            return value
        if operation == "details.episodes":
            return map_episodes_response(
                client.get(
                    "/3/tv/{0}/season/{1}".format(
                        params["show_id"], params["season"]
                    )
                )
            )
        if operation == "watchlist.watched":
            scope = self._scope(params["scope"])
            params = dict(params, scope=dict(scope.__dict__))
            session_id = str(self.settings.get("tmdb_access_token") or "").strip()
            operation_client = watchlist_operation_client(
                client,
                scope,
                session_id,
                identity_lock=self._lock,
            )
            if not execute_watched_removal(
                operation_client, scope, params["media_id"]
            ):
                raise TMDBError("TMDB watched operation was not accepted")
            if (
                str(self.settings.get("tmdb_access_token") or "").strip()
                != session_id
                or str(self.settings.get("tmdb_account_id") or "").strip()
                != scope.account_id
            ):
                raise TMDBError("TMDB identity changed during watched operation")
            latest = self.snapshot_store.remove(scope, params["media_id"])
            if latest is None:
                latest = self.snapshot_store.load(scope)
            if latest is None:
                raise TMDBError("TMDB watched snapshot is not complete")
            self.submit_watchlist(scope, force=True)
            return {
                "scope": params["scope"],
                "revision": latest.revision,
                "generation": latest.generation,
            }
        if operation == "identity.invalidate":
            return {
                "invalidated": self.snapshot_store.invalidate_identity(
                    params["account_id"]
                )
            }
        raise TMDBError("Unsupported legacy test operation")
