"""Production composition and native service registration contracts."""

import contextlib
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import requests

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.test_entrypoint import install_kodi_stubs
from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.genre_file_cache import GenreFileCache
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.tmdb import GenreRepository, TMDBError
from yawsp_app.tmdb_service_client import TMDBServiceClient
from yawsp_app.tmdb_service_store import ServiceStateStore
from yawsp_app.tmdb_service_worker import (
    ServiceMetadataEngine,
    ServiceOperationEngine,
    ServiceRunner,
    ServiceWatchlistEngine,
)
from yawsp_app.watchlist_snapshot import SnapshotScope, WatchlistSnapshotStore
from yawsp_app import router as production_router


ROOT = Path(__file__).resolve().parents[1]
EPOCH = "a" * 32
WAIT = 0.05


def response(data, status_code=200):
    return FakeResponse(json_data=data, status_code=status_code)


def page(rows):
    return response({"page": 1, "total_pages": 1, "results": rows})


class SerialStore(ServiceStateStore):
    """Drain the real serial runner as a deterministic native-service test seam."""

    runner = None
    drain_background = True

    def _drain(self):
        if self._draining:
            self._drain_again = True
            return
        self._draining = True
        try:
            self.runner.run_once()
        finally:
            self._draining = False
            if self._drain_again:
                self._drain_again = False
                self._drain()

    def join(self, request, waiter_id):
        row = super().join(request, waiter_id)
        if row["status"] in ("accepted", "joined"):
            self._drain()
        return row

    def submit(self, requests, **kwargs):
        rows = super().submit(requests, **kwargs)
        if (
            self.drain_background or kwargs.get("interactive")
        ) and any(row["status"] in ("accepted", "joined") for row in rows):
            self._drain()
        return rows


class NativeComposition:
    def __init__(
        self, test, service_responses=(), share_session=False, drain_background=True
    ):
        self.test = test
        self.service_session = FakeSession(service_responses)
        self.parent_session = (
            self.service_session if share_session else FakeSession()
        )
        self.parent_session.headers = {}
        self.service_session.headers = self.parent_session.headers
        self.drain_background = drain_background

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.profile = self.temporary.name
        self.kodi = install_kodi_stubs()
        self.kodi.start()
        self.test.addCleanup(self.kodi.stop)
        addon = sys.modules["xbmcaddon"].Addon()
        addon.settings.update(
            {
                "tmdb_api_key": "offline-api-key",
                "tmdb_access_token": "session-1",
                "tmdb_account_id": "account-1",
                "tmdb_sort": "0",
                "tmdb_hide_unreleased": "false",
            }
        )
        addon.strings.update(
            {
                30201: "Vyhledávání",
                30202: "Chci si stáhnout",
                30203: "Historie stahování",
                30204: "Nastavení",
                30402: "Error loading",
                30408: "Unknown title",
                30409: "Season {0} ({1} episodes)",
                30410: "Season {0}",
                30412: "Watched failed",
                30413: "Watched",
                30414: "Search original title",
                30415: "Play trailer",
                30416: "Mark watched",
                30417: "Watchlist empty",
                30418: "TMDB Watchlist",
                30421: "Movies",
                30422: "Shows",
                30423: "Trailer missing",
                30424: "Trailer error",
                30425: "Install YouTube",
                30438: "Refresh",
                30439: "Local cache",
                30440: "No results",
                30441: "Objevit",
                30442: "Populární",
                30443: "Nejlépe hodnocené",
            }
        )
        sys.modules["xbmc"].translatePath = lambda _value: self.profile
        sys.modules["xbmcvfs"].translatePath = lambda _value: self.profile
        self.argv = mock.patch.object(
            sys, "argv", ["plugin://plugin.video.yawspreloaded/", "17", ""]
        )
        self.argv.start()
        self.test.addCleanup(self.argv.stop)
        self.addon = addon
        self.settings = production_router._Settings(addon)
        self.trailer_cache = AtomicJsonCache(
            os.path.join(self.profile, "tmdb_trailers.json")
        )
        self.show_cache = AtomicJsonCache(os.path.join(self.profile, "tmdb_shows.json"))
        self.snapshots = WatchlistSnapshotStore(
            AtomicJsonCache(os.path.join(self.profile, "tmdb_watchlist.json"))
        )
        self.genres = GenreRepository(GenreFileCache(
            os.path.join(self.profile, "tmdb_genres.json")
        ))
        self.store = SerialStore(self.profile)
        self.store.drain_background = self.drain_background
        self.store._draining = False
        self.store._drain_again = False
        logger = logging.getLogger("test.native.composition")
        abort = lambda: False
        self.runner = ServiceRunner(
            self.store,
            lambda session: ServiceMetadataEngine(
                self.settings,
                self.trailer_cache,
                self.show_cache,
                lambda: production_router.kodi_language(sys.modules["xbmc"].getLanguage()),
                session,
                abort,
                logger,
            ),
            lambda session: ServiceWatchlistEngine(
                self.settings,
                self.snapshots,
                lambda: production_router.kodi_language(sys.modules["xbmc"].getLanguage()),
                self.store,
                session,
                abort,
                logger,
                genre_repository=self.genres,
            ),
            lambda session: ServiceOperationEngine(
                self.settings,
                self.genres,
                lambda: production_router.kodi_language(sys.modules["xbmc"].getLanguage()),
                self.store,
                session,
                abort,
                logger,
            ),
            abort,
            lambda _seconds: False,
            logger,
        )
        self.store.runner = self.runner
        self.test.addCleanup(self.runner.close)
        # Start and publish before admission; subsequent drains retain this epoch.
        self.assertFalse(self.runner.run_once())
        # requests is a shared module object: one factory distinguishes the
        # invocation-local parent session from every later service-owned session.
        owner_session_created = []

        def session_factory():
            if not owner_session_created:
                owner_session_created.append(True)
                return self.parent_session
            return self.service_session

        self.service_http = mock.patch(
            "yawsp_app.tmdb_service_worker.requests.Session",
            side_effect=session_factory,
        )
        self.service_http.start()
        self.test.addCleanup(self.service_http.stop)
        self.no_executor = mock.patch(
            "yawsp_app.tmdb.ThreadPoolExecutor",
            side_effect=AssertionError("parent TMDB executor is forbidden"),
            create=True,
        )
        self.no_executor.start()
        self.test.addCleanup(self.no_executor.stop)
        self.store_patch = mock.patch.object(
            production_router, "ServiceStateStore", return_value=self.store, create=True
        )
        self.store_patch.start()
        self.test.addCleanup(self.store_patch.stop)
        production_router.close()
        self.owner = production_router._get_owner()
        self.test.addCleanup(production_router.close)
        self.recorder = sys.modules["xbmcplugin"]._recorder
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        production_router.close()
        self.runner.close()
        self.temporary.cleanup()

    def assertFalse(self, value):
        self.test.assertFalse(value)


class ProductionCompositionTests(unittest.TestCase):
    def tearDown(self):
        production_router.close()

    def test_shipped_tmdb_has_no_legacy_prefetch_or_executor_but_downloads_keep_theirs(self):
        from yawsp_app import download_jobs, tmdb as legacy_probe

        self.assertFalse(hasattr(legacy_probe, "BackgroundPrefetch"))
        self.assertFalse(hasattr(legacy_probe, "ThreadPoolExecutor"))
        self.assertTrue(hasattr(download_jobs.threading, "Thread"))
        self.assertIs(download_jobs.threading.Thread.__module__, "threading")

    def test_owner_uses_service_client_without_tmdb_executor_or_parent_worker(self):
        with NativeComposition(self) as native:
            self.assertIsInstance(native.owner.background, TMDBServiceClient)
            self.assertIs(native.owner.background.store, native.store)
            self.assertIs(native.owner.service_store, native.store)
            self.assertEqual(native.parent_session.calls, [])
            native.owner.close()
            self.assertTrue(native.store.lease.acquired)

    def test_qr_uses_real_os_identity_transaction_and_service_invalidation(self):
        with NativeComposition(self) as native:
            scope = SnapshotScope("account-1", "movies", "en-US")
            native.snapshots.save_complete(scope, [{"id": 1, "title": "Old"}], 0.0)
            native.parent_session.queue_response(response({"request_token": "request-token"}))
            native.parent_session.queue_response(response({"session_id": "session-2"}))
            transactions = []
            original = native.store.identity_transaction

            def observed():
                transactions.append(threading.current_thread().ident)
                return original()

            native.store.identity_transaction = observed
            result = production_router.router("action=tmdb_auth")
            self.assertTrue(result.handled)
            self.assertTrue(transactions)
            self.assertNotEqual(native.owner.background._lock, native.store)
            self.assertIsNone(native.snapshots.load(scope))
            self.assertEqual(native.addon.getSetting("tmdb_access_token"), "session-2")
            self.assertEqual(native.addon.getSetting("tmdb_account_id"), "")
            self.assertEqual(native.service_session.calls, [])
            self.assertEqual(
                [call["url"] for call in native.parent_session.calls],
                [
                    "https://api.themoviedb.org/3/authentication/token/new",
                    "https://api.themoviedb.org/3/authentication/session/new",
                ],
            )

    def test_real_pending_401_abort_preserves_identity_without_service_http(self):
        with NativeComposition(self) as native:
            native.parent_session.queue_response(response({"request_token": "request-token"}))
            pending = response({"success": False}, status_code=401)
            native.parent_session.queue_response(pending)

            class AbortAfterPending:
                def waitForAbort(self, _seconds):
                    return True

            native.owner._runtime_options["monitor_factory"] = AbortAfterPending
            result = production_router.router("action=tmdb_auth")
            self.assertFalse(result.handled)
            self.assertTrue(pending.closed)
            self.assertEqual(native.addon.getSetting("tmdb_access_token"), "session-1")
            self.assertEqual(native.addon.getSetting("tmdb_account_id"), "account-1")
            self.assertEqual(native.service_session.calls, [])
            self.assertEqual(len(native.parent_session.calls), 2)

    def test_real_pending_then_success_invalidates_only_old_scope_via_service(self):
        with NativeComposition(self) as native:
            old_scope = SnapshotScope("account-1", "movies", "en-US")
            replacement_scope = SnapshotScope("account-2", "movies", "en-US")
            native.snapshots.save_complete(old_scope, [{"id": 1, "title": "Old"}], 0.0)
            replacement = native.snapshots.save_complete(
                replacement_scope, [{"id": 2, "title": "Replacement"}], 100.0
            )
            native.parent_session.queue_response(response({"request_token": "request-token"}))
            native.parent_session.queue_response(response({"success": False}, status_code=401))
            native.parent_session.queue_response(response({"session_id": "session-2"}))
            invalidations = []
            original = native.store.identity_transaction

            def observed_identity():
                invalidations.append("os-transaction")
                return original()

            native.store.identity_transaction = observed_identity
            result = production_router.router("action=tmdb_auth")
            self.assertTrue(result.handled)
            self.assertIn("os-transaction", invalidations)
            self.assertIsNone(native.snapshots.load(old_scope))
            self.assertEqual(native.snapshots.load(replacement_scope), replacement)
            self.assertEqual(native.addon.getSetting("tmdb_access_token"), "session-2")
            self.assertEqual(native.addon.getSetting("tmdb_account_id"), "")
            self.assertEqual(native.service_session.calls, [])

    def test_wrong_bound_identity_cannot_delete_replacement_scopes(self):
        with NativeComposition(self) as native:
            old_scope = SnapshotScope("account-1", "movies", "en-US")
            new_scope = SnapshotScope("account-2", "movies", "en-US")
            old = native.snapshots.save_complete(old_scope, [{"id": 1, "title": "Old"}], 0.0)
            new = native.snapshots.save_complete(new_scope, [{"id": 2, "title": "New"}], 100.0)
            native.addon.setSetting("tmdb_account_id", "account-2")
            native.addon.setSetting("tmdb_access_token", "session-2")
            old_binding = "v1:" + hashlib.sha256(b"session-1").hexdigest()
            result = native.owner.background.run_operation(
                protocol.invalidate_identity_request("account-1", old_binding)
            )
            self.assertEqual(result, {"invalidated": False})
            self.assertEqual(native.snapshots.load(old_scope), old)
            self.assertEqual(native.snapshots.load(new_scope), new)
            self.assertEqual(native.addon.getSetting("tmdb_access_token"), "session-2")
            self.assertEqual(native.addon.getSetting("tmdb_account_id"), "account-2")
            self.assertEqual(native.service_session.calls, [])

    def test_root_order_and_cold_then_fresh_warm_watchlist_use_only_the_service(self):
        movie = {
            "id": 1,
            "title": "Film",
            "original_title": "Original",
            "release_date": "2026-01-02",
            "overview": "Plot",
            "genre_ids": [18],
            "vote_average": 7.5,
            "poster_path": "/poster.jpg",
        }
        with NativeComposition(
            self,
            [
                page([movie]),
                response({"genres": [{"id": 18, "name": "Drama"}]}),
                response({"results": []}),
            ],
        ) as native:
            root = production_router.router("")
            self.assertTrue(root.succeeded)
            self.assertEqual(
                [item["listitem"].label for item in native.recorder.items],
                [
                    "Objevit",
                    "Vyhledávání",
                    "Chci si stáhnout",
                    "Historie stahování",
                    "Nastavení",
                    "TMDB Watchlist",
                ],
            )
            native.recorder.items.clear()

            cold = production_router.router("action=tmdb_watchlist&category=movies")
            self.assertTrue(cold.succeeded)
            self.assertEqual(native.recorder.items[0]["listitem"].label, "Film (2026)")
            scope = SnapshotScope("account-1", "movies", "en-US")
            saved = native.snapshots.load(scope)
            self.assertEqual(saved.results, [movie])
            self.assertEqual(
                [call["url"] for call in native.service_session.calls],
                [
                    "https://api.themoviedb.org/3/account/account-1/watchlist/movies",
                    "https://api.themoviedb.org/3/genre/movie/list",
                    "https://api.themoviedb.org/3/movie/1/videos",
                ],
            )
            self.assertNotIn(
                "session_id", native.service_session.calls[1]["kwargs"]["params"]
            )
            self.assertEqual(native.parent_session.calls, [])
            native.recorder.items.clear()

            warm = production_router.router("action=tmdb_watchlist&category=movies")
            self.assertTrue(warm.succeeded)
            self.assertEqual(len(native.service_session.calls), 3)
            self.assertEqual(native.parent_session.calls, [])

    def test_parent_legacy_cache_and_snapshot_loads_never_migrate_or_write(self):
        with NativeComposition(self) as native:
            scope = SnapshotScope("account-1", "movies", "en-US")
            native.snapshots.save_complete(
                scope, [{"id": 1, "title": "Warm", "genre_ids": []}], time.time()
            )
            native.trailer_cache.save({"movie:1": "legacy-video"})
            previous = Path(native.trailer_cache.path).read_bytes()
            native.owner.genre_repository._values[("movie", "en-US")] = {}
            with mock.patch.object(
                native.trailer_cache,
                "save",
                side_effect=AssertionError("parent trailer cache write"),
            ), mock.patch.object(
                native.trailer_cache,
                "save_merged",
                side_effect=AssertionError("parent trailer cache merge"),
            ), mock.patch.object(
                native.snapshots.cache,
                "save",
                side_effect=AssertionError("parent snapshot write"),
            ), mock.patch.object(
                native.snapshots.cache,
                "save_merged",
                side_effect=AssertionError("parent snapshot merge"),
            ):
                result = production_router.router(
                    "action=tmdb_watchlist&category=movies"
                )
            self.assertTrue(result.succeeded)
            self.assertEqual(Path(native.trailer_cache.path).read_bytes(), previous)
            self.assertEqual(native.service_session.calls, [])
            self.assertEqual(native.parent_session.calls, [])

    def test_missing_account_is_resolved_by_service_without_parent_account_http(self):
        movie = {"id": 1, "title": "Resolved", "genre_ids": []}
        with NativeComposition(
            self,
            [
                response({"id": "account-1"}),
                page([movie]),
                response({"genres": []}),
                response({"results": []}),
            ],
        ) as native:
            native.addon.setSetting("tmdb_account_id", "")
            result = production_router.router(
                "action=tmdb_watchlist&category=movies"
            )
            self.assertTrue(result.succeeded)
            self.assertEqual(native.addon.getSetting("tmdb_account_id"), "account-1")
            self.assertEqual(native.parent_session.calls, [])
            self.assertEqual(
                [call["url"] for call in native.service_session.calls],
                [
                    "https://api.themoviedb.org/3/account",
                    "https://api.themoviedb.org/3/account/account-1/watchlist/movies",
                    "https://api.themoviedb.org/3/genre/movie/list",
                    "https://api.themoviedb.org/3/movie/1/videos",
                ],
            )

    def test_watched_missing_account_also_uses_service_resolution(self):
        with NativeComposition(
            self,
            [
                response({"id": "account-1"}),
                response({"success": True}),
                page([{"id": 2, "title": "Keep"}]),
                response({"genres": []}),
            ],
        ) as native:
            scope = SnapshotScope("account-1", "movies", "en-US")
            native.snapshots.save_complete(
                scope,
                [{"id": 42, "title": "Remove"}, {"id": 2, "title": "Keep"}],
                0.0,
            )
            native.addon.setSetting("tmdb_account_id", "")
            result = production_router.router(
                "action=tmdb_watchlist&category=movies&watched=42"
            )
            self.assertTrue(result.handled)
            self.assertEqual(native.addon.getSetting("tmdb_account_id"), "account-1")
            self.assertEqual(native.parent_session.calls, [])
            self.assertEqual(native.service_session.calls[0]["url"], "https://api.themoviedb.org/3/account")
            self.assertEqual(native.service_session.calls[1]["method"], "POST")

    def test_manual_success_rereads_exact_revision_without_immediate_duplicate(self):
        with NativeComposition(
            self,
            [
                page([{"id": 2, "title": "Fresh", "genre_ids": []}]),
                response({"genres": []}),
                response({"results": []}),
            ],
        ) as native:
            scope = SnapshotScope("account-1", "movies", "en-US")
            original = native.snapshots.save_complete(
                scope, [{"id": 1, "title": "Old"}], 0.0
            )
            result = production_router.router(
                "action=tmdb_watchlist&category=movies&refresh=1"
            )
            self.assertTrue(result.succeeded)
            current = native.snapshots.load(scope)
            self.assertEqual(current.revision, original.revision + 1)
            self.assertEqual(current.generation, original.generation)
            self.assertEqual(current.results, [{"id": 2, "title": "Fresh", "genre_ids": []}])
            self.assertEqual(len(native.service_session.calls), 3)
            self.assertEqual(
                [call["url"] for call in native.service_session.calls[:2]],
                [
                    "https://api.themoviedb.org/3/account/account-1/watchlist/movies",
                    "https://api.themoviedb.org/3/genre/movie/list",
                ],
            )
            self.assertEqual(native.parent_session.calls, [])

    def test_manual_failure_preserves_bytes_and_never_falls_through_to_parent(self):
        with NativeComposition(self, [requests.Timeout("offline")]) as native:
            scope = SnapshotScope("account-1", "movies", "en-US")
            original = native.snapshots.save_complete(
                scope, [{"id": 1, "title": "Durable"}], 0.0
            )
            previous = Path(native.snapshots.cache.path).read_bytes()
            with self.assertLogs("yawsp_app.router", level="ERROR"):
                with self.assertLogs("test.native.composition", level="ERROR"):
                    result = production_router.router(
                        "action=tmdb_watchlist&category=movies&refresh=1"
                    )
            self.assertFalse(result.succeeded)
            self.assertEqual(native.snapshots.load(scope), original)
            self.assertEqual(Path(native.snapshots.cache.path).read_bytes(), previous)
            self.assertEqual(len(native.service_session.calls), 1)
            self.assertEqual(native.parent_session.calls, [])

    def test_missing_warm_genre_never_calls_parent_get_and_defers_refresh_until_completion(self):
        with NativeComposition(
            self,
            [
                page([{"id": 1, "title": "Warm"}]),
                response({"genres": [{"id": 18, "name": "Drama"}]}),
                response({"results": []}),
            ],
        ) as native:
            scope = SnapshotScope("account-1", "movies", "en-US")
            native.snapshots.save_complete(
                scope,
                [{"id": 1, "title": "Warm", "genre_ids": [18]}],
                time.time(),
            )
            with mock.patch.object(
                native.owner.genre_repository,
                "get",
                side_effect=AssertionError("parent GenreRepository.get is forbidden"),
            ):
                calls_at_end = []
                original_end = sys.modules["xbmcplugin"].endOfDirectory

                def observed_end(*args, **kwargs):
                    calls_at_end.append(len(native.service_session.calls))
                    return original_end(*args, **kwargs)

                sys.modules["xbmcplugin"].endOfDirectory = observed_end
                result = production_router.router(
                    "action=tmdb_watchlist&category=movies&refresh=0"
                )
            self.assertTrue(result.succeeded)
            self.assertEqual(calls_at_end, [0])
            self.assertEqual(
                [call["url"] for call in native.service_session.calls],
                [
                    "https://api.themoviedb.org/3/account/account-1/watchlist/movies",
                    "https://api.themoviedb.org/3/genre/movie/list",
                    "https://api.themoviedb.org/3/movie/1/videos",
                ],
            )
            self.assertEqual(native.parent_session.calls, [])

    def test_watched_post_latest_snapshot_and_one_forced_followup_precede_refresh(self):
        with NativeComposition(
            self,
            [
                response({"success": True}, 201),
                page([{"id": 2, "title": "Keep"}]),
                response({"genres": []}),
            ],
        ) as native:
            scope = SnapshotScope("account-1", "movies", "en-US")
            native.snapshots.save_complete(
                scope,
                [{"id": 42, "title": "Remove"}, {"id": 2, "title": "Keep"}],
                0.0,
            )
            result = production_router.router(
                "action=tmdb_watchlist&category=movies&watched=42"
            )
            self.assertTrue(result.handled)
            self.assertTrue(result.refresh)
            self.assertEqual(
                [(call["method"], call["url"]) for call in native.service_session.calls],
                [
                    ("POST", "https://api.themoviedb.org/3/account/account-1/watchlist"),
                    ("GET", "https://api.themoviedb.org/3/account/account-1/watchlist/movies"),
                    ("GET", "https://api.themoviedb.org/3/genre/movie/list"),
                ],
            )
            self.assertEqual(native.snapshots.load(scope).results, [{"id": 2, "title": "Keep"}])
            self.assertEqual(native.recorder.builtin_commands, ["Container.Refresh"])
            self.assertEqual(native.parent_session.calls, [])

    def test_all_catalogs_and_details_are_typed_service_results_without_parent_http(self):
        movie = {
            "id": 41,
            "title": "Localized",
            "original_title": "Original",
            "release_date": "2024-03-02",
            "genre_ids": [],
            "vote_average": 7.25,
        }
        show = dict(
            movie,
            id=42,
            name="Localized show",
            original_name="Original show",
            first_air_date="2023-04-05",
        )
        show.pop("title")
        show.pop("original_title")
        show.pop("release_date")
        responses = []
        for index, rows in enumerate(([movie], [show], [movie], [show])):
            responses.append(page(rows))
            if index < 2:
                responses.append(response({"genres": []}))
        responses.extend(
            (
                response({"name": "Show", "original_name": "Original", "seasons": []}),
                response({"episodes": []}),
            )
        )
        with NativeComposition(self, responses) as native:
            for source in (
                "popular_movies",
                "popular_shows",
                "top_rated_movies",
                "top_rated_shows",
            ):
                result = production_router.router(
                    "action=tmdb_discovery&source=" + source
                )
                self.assertTrue(result.succeeded, source)
            self.assertTrue(production_router.router(
                "action=tmdb_watchlist&category=shows&show_id=42"
            ).succeeded)
            self.assertTrue(production_router.router(
                "action=tmdb_watchlist&category=shows&show_id=42&season=2"
            ).succeeded)
            self.assertEqual(
                [call["url"] for call in native.service_session.calls],
                [
                    "https://api.themoviedb.org/3/movie/popular",
                    "https://api.themoviedb.org/3/genre/movie/list",
                    "https://api.themoviedb.org/3/tv/popular",
                    "https://api.themoviedb.org/3/genre/tv/list",
                    "https://api.themoviedb.org/3/movie/top_rated",
                    "https://api.themoviedb.org/3/tv/top_rated",
                    "https://api.themoviedb.org/3/tv/42",
                    "https://api.themoviedb.org/3/tv/42/season/2",
                ],
            )
            for call in native.service_session.calls[:6]:
                self.assertNotIn("session_id", call["kwargs"]["params"])
            self.assertEqual(native.parent_session.calls, [])

    def test_failed_detail_directory_schedules_no_metadata_or_parent_fallback(self):
        with NativeComposition(self, [requests.Timeout("offline")]) as native:
            with self.assertLogs("test.native.composition", level="ERROR"):
                result = production_router.router(
                    "action=tmdb_watchlist&category=shows&show_id=42"
                )
            self.assertFalse(result.succeeded)
            self.assertEqual(len(native.service_session.calls), 1)
            self.assertIsNone(result.post_complete)
            self.assertEqual(native.parent_session.calls, [])

    def test_unavailable_service_returns_typed_failure_without_parent_http_or_rows(self):
        with NativeComposition(self) as native:
            native.store.set_stopping(native.runner._epoch)
            result = production_router.router(
                "action=tmdb_discovery&source=popular_movies"
            )
            self.assertFalse(result.succeeded)
            self.assertIsNone(result.post_complete)
            self.assertEqual(native.recorder.items, [])
            self.assertEqual(native.service_session.calls, [])
            self.assertEqual(native.parent_session.calls, [])

    def test_backpressure_returns_typed_failure_without_parent_http_or_rows(self):
        with NativeComposition(self) as native:
            request = protocol.catalog_request("popular_movies", "en-US")
            native.store.join = lambda _request, _waiter: {
                "status": "backpressured",
                "request": request,
                "code": "BACKPRESSURED",
            }
            result = production_router.router(
                "action=tmdb_discovery&source=popular_movies"
            )
            self.assertFalse(result.succeeded)
            self.assertIsNone(result.post_complete)
            self.assertEqual(native.recorder.items, [])
            self.assertEqual(native.service_session.calls, [])
            self.assertEqual(native.parent_session.calls, [])

    def test_on_demand_trailer_hit_missing_and_error_keep_existing_action_contract(self):
        cases = (
            ({"results": [{"site": "YouTube", "type": "Trailer", "key": "videohit001"}]}, True),
            ({"results": []}, False),
            (requests.Timeout("offline"), False),
        )
        for payload, handled in cases:
            with self.subTest(handled=handled):
                failed = isinstance(payload, BaseException)
                responses = (
                    [payload, payload]
                    if failed
                    else [response(payload) if isinstance(payload, dict) else payload]
                )
                with NativeComposition(self, responses) as native:
                    if failed:
                        logs = self.assertLogs("test.native.composition", level="ERROR")
                    else:
                        logs = contextlib.nullcontext()
                    with logs:
                        result = production_router.router(
                            "action=tmdb_trailer&media=movie&id=42"
                        )
                    self.assertEqual(result.handled, handled)
                    self.assertEqual(len(native.service_session.calls), 2 if failed else 1)
                    self.assertEqual(native.parent_session.calls, [])
                    if handled:
                        self.assertEqual(
                            native.recorder.builtin_commands,
                            ["PlayMedia(plugin://plugin.video.youtube/play/?video_id=videohit001)"],
                        )
                    else:
                        self.assertEqual(native.recorder.builtin_commands, [])


class WatchlistGenreIntegrationTests(unittest.TestCase):
    def test_refresh_commits_snapshot_only_after_public_genres_and_skips_empty_lists(self):
        with tempfile.TemporaryDirectory() as root:
            genre_path = os.path.join(root, "tmdb_genres.json")
            snapshots = WatchlistSnapshotStore(AtomicJsonCache(
                os.path.join(root, "watchlist.json")
            ))

            class Settings:
                values = {
                    "tmdb_api_key": "api-key",
                    "tmdb_access_token": "session",
                    "tmdb_account_id": "42",
                }

                def get(self, key):
                    return self.values.get(key, "")

                def set(self, key, value):
                    self.values[key] = value

            settings = Settings()
            store = ServiceStateStore(root)
            self.addCleanup(store.lease.release)
            session = FakeSession([
                page([{"id": 1, "title": "Film", "genre_ids": [18]}]),
                response({"genres": [{"id": 18, "name": "Drama"}]}),
                page([]),
            ])
            engine = ServiceWatchlistEngine(
                settings,
                snapshots,
                lambda: "en-US",
                store,
                session,
                lambda: False,
                logging.getLogger("test.watchlist.genres"),
                genre_repository=GenreRepository(GenreFileCache(genre_path)),
            )
            binding = "v1:" + hashlib.sha256(b"session").hexdigest()
            scope = SnapshotScope("42", "movies", "en-US")
            request = protocol.watchlist_request(scope, 0, False, False, binding)
            engine.execute(request)
            state, document = GenreFileCache(genre_path).read()
            self.assertEqual(document["genres"]["movie/en-US"], {"18": "Drama"})
            self.assertEqual(snapshots.load(scope).results[0]["id"], 1)
            engine.execute(request)
            self.assertEqual(len(session.calls), 3)
            self.assertEqual(
                [call["url"] for call in session.calls],
                [
                    "https://api.themoviedb.org/3/account/42/watchlist/movies",
                    "https://api.themoviedb.org/3/genre/movie/list",
                    "https://api.themoviedb.org/3/account/42/watchlist/movies",
                ],
            )
            self.assertNotIn("session_id", session.calls[1]["kwargs"]["params"])


    def test_genre_failure_preserves_previous_snapshot_and_genre_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            genre_path = os.path.join(root, "tmdb_genres.json")
            snapshot_path = os.path.join(root, "watchlist.json")
            snapshots = WatchlistSnapshotStore(AtomicJsonCache(snapshot_path))
            scope = SnapshotScope("42", "movies", "en-US")
            original = snapshots.save_complete(scope, [{"id": 1, "title": "Old"}], 0.0)
            GenreFileCache(genre_path).store("tv", "en-US", {"18": "Old drama"})
            old_snapshot_bytes = Path(snapshot_path).read_bytes()
            old_genre_bytes = Path(genre_path).read_bytes()

            class Settings:
                def get(self, key):
                    return {
                        "tmdb_api_key": "api-key",
                        "tmdb_access_token": "session",
                        "tmdb_account_id": "42",
                    }.get(key, "")

                def set(self, key, value):
                    raise AssertionError("public/list failure must not replace identity here")

            store = ServiceStateStore(root)
            self.addCleanup(store.lease.release)
            session = FakeSession([
                page([{"id": 2, "title": "Partial"}]),
                response({}, status_code=503),
                response({}, status_code=503),
                response({}, status_code=503),
            ])
            engine = ServiceWatchlistEngine(
                Settings(),
                snapshots,
                lambda: "en-US",
                store,
                session,
                lambda: False,
                logging.getLogger("test.watchlist.genres.failure"),
                genre_repository=GenreRepository(GenreFileCache(genre_path)),
            )
            binding = "v1:" + hashlib.sha256(b"session").hexdigest()
            with self.assertRaises(TMDBError):
                engine.execute(protocol.watchlist_request(scope, 0, True, False, binding))
            self.assertEqual(Path(snapshot_path).read_bytes(), old_snapshot_bytes)
            self.assertEqual(Path(genre_path).read_bytes(), old_genre_bytes)
            self.assertEqual(snapshots.load(scope), original)


class NativeRegistrationTests(unittest.TestCase):
    def test_exactly_one_native_service_extension_targets_root_entrypoint(self):
        root = ET.parse(str(ROOT / "addon.xml")).getroot()
        services = [
            extension
            for extension in root.findall("extension")
            if extension.get("point") == "xbmc.service"
        ]
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].get("library"), "service.py")

    def test_entrypoint_runs_serial_runner_directly_and_always_closes_it(self):
        with install_kodi_stubs(), tempfile.TemporaryDirectory() as profile:
            sys.modules["xbmc"].translatePath = lambda _value: profile
            sys.modules.pop("service", None)
            service = importlib.import_module("service")
            runner = mock.Mock()
            runner.run_until_abort.return_value = None
            created = []
            monitor = sys.modules["xbmc"].Monitor()

            def no_thread(*args, **kwargs):
                raise AssertionError("native service must not create an application thread")

            with mock.patch.object(service, "ServiceRunner", return_value=runner) as runner_type, mock.patch.object(
                threading, "Thread", side_effect=no_thread
            ):
                created.append(runner_type)
                self.assertTrue(service.run(monitor=monitor))
            runner.run_until_abort.assert_called_once_with()
            runner.close.assert_called_once_with()
            self.assertEqual(created[0].call_args.kwargs["wait_for_abort"], monitor.waitForAbort)


if __name__ == "__main__":
    unittest.main()
