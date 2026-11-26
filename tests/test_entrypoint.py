import contextlib
import importlib
import json
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.support.kodi_stubs import AddonRecorder, KodiRecorder
from tests.support.vfs_stubs import RecordingVFS
from tests.test_runtime import RecordingLogger
from tests.test_tmdb import json_response
from tests.test_webshare_ui import FakeClient, Keyboard as CompatibleKeyboard, xml
from yawsp_app import router as production_router
from yawsp_app.genre_file_cache import GenreFileCache
from yawsp_app.watchlist_snapshot import SnapshotScope
from yawsp_app.runtime import (
    ActionResult,
    ActionRunner,
    DirectoryResult,
    PlaybackResult,
)


class ListItem:
    def __init__(self, label="", path=""):
        self.label = label
        self.path = path
        self.art = {}
        self.info = []
        self.commands = []

    def setArt(self, art):
        self.art.update(art)

    def setInfo(self, kind, info):
        self.info.append((kind, info))

    def addContextMenuItems(self, commands):
        self.commands.extend(commands)


class Owner:
    def __init__(self):
        self.plugin = KodiRecorder()
        self.runner = ActionRunner(self.plugin, 17, ListItem, logger=RecordingLogger())
        self.close_calls = 0
        self.received = []

    def handlers(self):
        def directory(params):
            self.received.append(params)
            if params.get("fail"):
                raise ValueError("directory failed")
            return DirectoryResult()

        def action(params):
            self.received.append(params)
            if params.get("fail"):
                raise ValueError("action failed")
            if params.get("watched"):
                return ActionResult(handled=params.get("result") != "false", refresh=True)
            return ActionResult()

        def play(params):
            self.received.append(params)
            if params.get("fail"):
                raise ValueError("playback failed")
            return PlaybackResult(False, ListItem())

        def watchlist(params):
            if params.get("watched"):
                return action(params)
            return directory(params)

        return {
            "menu": directory,
            "search": directory,
            "queue": directory,
            "history": directory,
            "tmdb_watchlist": watchlist,
            "play": play,
            "settings": action,
            "download": action,
            "info": action,
            "tmdb_auth": action,
            "tmdb_trailer": action,
        }

    def close(self):
        self.close_calls += 1


def install_kodi_stubs():
    addon = AddonRecorder(
        addon_info={"name": "YAWsP", "profile": "special://profile/"},
        strings={
            30101: "Login required",
            30102: "Request failed",
            30302: "Downloading",
            30303: "Downloaded",
            30304: "Download failed",
            30433: "Download queued",
            30437: "Download could not be queued",
            30438: "Refresh",
            30440: "No matching files found",
            30441: "Objevit",
            30442: "Populární",
            30443: "Nejlépe hodnocené",
        },
    )
    addon.open_settings_calls = 0

    def open_settings():
        addon.open_settings_calls += 1

    addon.openSettings = open_settings
    recorder = KodiRecorder()
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmc.translatePath = lambda value: value
    xbmc.log = lambda *args: None
    xbmc.executebuiltin = recorder.executeBuiltin
    xbmc.Keyboard = lambda default, heading: CompatibleKeyboard(default, recorder.calls)
    xbmc._language = "en"
    xbmc.getLanguage = lambda: xbmc._language
    xbmc.getCondVisibility = lambda condition: True
    xbmc.getSkinDir = lambda: "skin.estuary"

    class Monitor:
        def abortRequested(self):
            return False

        def waitForAbort(self, seconds):
            return False

    xbmc.Monitor = Monitor
    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.NOTIFICATION_INFO = "info"
    xbmcgui.NOTIFICATION_WARNING = "warning"
    xbmcgui.NOTIFICATION_ERROR = "error"
    xbmcgui.ListItem = ListItem
    xbmcgui.Keyboard = lambda *args: None
    xbmcgui.getCurrentWindowId = lambda: 0

    class Dialog:
        def notification(self, *args, **kwargs):
            return recorder.notification(*args, **kwargs)

        def textviewer(self, *args):
            recorder.calls.append({"method": "textviewer", "args": args, "kwargs": {}})

    xbmcgui.Dialog = Dialog
    xbmcplugin = types.ModuleType("xbmcplugin")
    xbmcplugin.SORT_METHOD_LABEL = 1
    xbmcplugin.SORT_METHOD_EPISODE = 23
    xbmcplugin.addDirectoryItem = recorder.addDirectoryItem
    xbmcplugin.endOfDirectory = recorder.endOfDirectory
    xbmcplugin.setResolvedUrl = recorder.setResolvedUrl
    xbmcplugin.setContent = recorder.setContent
    xbmcplugin.setPluginCategory = recorder.setPluginCategory
    xbmcplugin.addSortMethod = recorder.addSortMethod
    xbmcplugin._recorder = recorder
    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda: addon
    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = lambda value: value
    modules = {
        "xbmc": xbmc,
        "xbmcgui": xbmcgui,
        "xbmcplugin": xbmcplugin,
        "xbmcaddon": xbmcaddon,
        "xbmcvfs": xbmcvfs,
    }
    return mock.patch.dict(sys.modules, modules)


@contextlib.contextmanager
def production_tmdb_invocation(responses, language="en"):
    """Compose the genuine owner/store/client with a serial native fixture."""
    from tests.test_tmdb_service_integration import NativeComposition

    class Host:
        def __init__(self):
            self.cleanups = []

        def addCleanup(self, function):
            self.cleanups.append(function)

        def assertFalse(self, value):
            if value:
                raise AssertionError("native service startup did unexpected work")

        def close(self):
            for function in reversed(self.cleanups):
                function()

    host = Host()
    native = NativeComposition(
        host, responses, share_session=True, drain_background=False
    )
    try:
        with native:
            sys.modules["xbmc"]._language = language
            yield (
                native.owner,
                native.service_session,
                native.addon,
                native.recorder,
            )
    finally:
        host.close()


def service_jobs(owner, operation=None, pending=False):
    """Read real committed service jobs, never a LegacyServiceAdapter queue."""
    with open(owner.service_store.manifest_path, encoding="utf-8") as source:
        jobs = json.load(source)["jobs"]
    if operation is not None:
        jobs = [job for job in jobs if job["request"]["operation"] == operation]
    if pending:
        jobs = [job for job in jobs if job["state"] in ("queued", "backlog", "active")]
    return jobs


def switch_json_response(data, switch):
    response = json_response(data)

    def switched_json():
        switch()
        return data

    response.json = switched_json
    return response


class EntrypointTests(unittest.TestCase):
    def tearDown(self):
        close = getattr(production_router, "close", None)
        if close is not None:
            close()

    def test_kodi_import_exposes_the_same_callable_unified_router(self):
        with install_kodi_stubs(), mock.patch.object(
            sys, "argv", ["plugin://plugin.video.yawspreloaded/", "17", "?action=settings"]
        ):
            sys.modules.pop("yawsp", None)
            yawsp = importlib.import_module("yawsp")

        self.assertTrue(callable(yawsp.router))
        self.assertIs(yawsp.router, production_router.router)

    def test_production_keyboard_factory_uses_xbmc_keyboard_compatible_api(self):
        created = []

        class RealKeyboard:
            def __init__(self, default, heading):
                self.default = default
                self.heading = heading
                self.modal = False
                created.append(self)

            def doModal(self):
                self.modal = True

            def isConfirmed(self):
                return True

            def getText(self):
                return self.default

        with install_kodi_stubs(), mock.patch.object(
            sys, "argv", ["plugin://plugin.video.yawspreloaded/", "17", ""]
        ), tempfile.TemporaryDirectory() as profile:
            sys.modules["xbmc"].Keyboard = RealKeyboard
            del sys.modules["xbmcgui"].Keyboard
            sys.modules["xbmc"].translatePath = lambda value: profile
            sys.modules["xbmcvfs"].translatePath = lambda value: profile
            production_router.close()
            owner = production_router._ProductionOwner()
            try:
                answer = owner.runtime.ask("Movie", "Search")
                self.assertEqual(answer, "Movie")
                self.assertEqual(len(created), 1)
                self.assertTrue(created[0].modal)
                self.assertEqual(created[0].heading, "Search")
            finally:
                owner.close()

    def test_complete_legacy_action_matrix_completes_exactly_once(self):
        owner = Owner()
        cases = (
            ("", "directory"),
            ("action=unknown", "directory"),
            ("action=search&what=Movie", "directory"),
            ("action=queue", "directory"),
            ("action=history", "directory"),
            ("action=db", "directory"),
            ("action=searchdb&query=", "directory"),
            ("action=tmdb_watchlist", "directory"),
            ("action=tmdb_watchlist&category=movies", "directory"),
            ("action=tmdb_watchlist&category=shows&show_id=8", "directory"),
            ("action=tmdb_watchlist&category=shows&show_id=8&season=2", "directory"),
            ("action=tmdb_watchlist&category=movies&watched=42&result=false", "action"),
            ("action=play&ident=abc", "playback"),
            ("action=settings", "action"),
            ("action=download&ident=abc", "action"),
            ("action=info&ident=abc", "action"),
            ("action=tmdb_auth", "action"),
            ("action=tmdb_trailer&media=movie&id=42", "action"),
        )
        with mock.patch.object(production_router, "_get_owner", return_value=owner, create=True):
            for paramstring, kind in cases:
                with self.subTest(paramstring=paramstring):
                    before_directories = len(owner.plugin.end_of_directory_calls)
                    before_resolutions = len(owner.plugin.resolved_url_calls)

                    production_router.router(paramstring)

                    directory_delta = len(owner.plugin.end_of_directory_calls) - before_directories
                    resolution_delta = len(owner.plugin.resolved_url_calls) - before_resolutions
                    if kind == "directory":
                        self.assertEqual((directory_delta, resolution_delta), (1, 0))
                    else:
                        self.assertEqual((directory_delta, resolution_delta), (0, 1))
                        self.assertFalse(owner.plugin.resolved_url_calls[-1]["succeeded"])

        self.assertEqual(len(owner.received), len(cases))

    def test_error_and_cancel_routes_still_complete_once_through_facade_contract(self):
        owner = Owner()
        recoverable = (
            ("action=search&fail=1", "directory"),
            ("action=searchdb", "directory"),
            ("action=tmdb_watchlist&category=movies&watched=42&result=false", "action"),
            ("action=tmdb_auth&fail=1", "action"),
            ("action=play&fail=1", "playback"),
        )
        with mock.patch.object(production_router, "_get_owner", return_value=owner, create=True):
            for paramstring, kind in recoverable:
                with self.subTest(paramstring=paramstring):
                    production_router.router(paramstring)
        self.assertEqual(len(owner.plugin.end_of_directory_calls), 2)
        self.assertEqual(len(owner.plugin.resolved_url_calls), 3)
        self.assertFalse(owner.plugin.end_of_directory_calls[0]["succeeded"])

    def test_real_owner_supplies_one_profile_genre_file_cache_without_startup_io(self):
        with tempfile.TemporaryDirectory() as profile, install_kodi_stubs(), mock.patch.object(
            sys, "argv", ["plugin://plugin.video.yawspreloaded/", "17", ""]
        ), mock.patch.object(production_router, "TMDBServiceClient"), mock.patch.object(
            production_router, "DownloadJobManager"
        ):
            translate = lambda value: profile
            sys.modules["xbmc"].translatePath = translate
            sys.modules["xbmcvfs"].translatePath = translate
            production_router.close()
            owner = production_router._get_owner()
            try:
                file_cache = owner.genre_repository._file_cache
                expected_path = production_router.os.path.join(owner.profile, "tmdb_genres.json")
                self.assertIsInstance(file_cache, GenreFileCache)
                self.assertEqual(file_cache.path, expected_path)
                self.assertIs(file_cache.logger, production_router._LOGGER)
                self.assertFalse(production_router.os.path.exists(expected_path))
            finally:
                production_router.close()

    def test_real_kodi_adapters_share_service_client_and_map_language(self):
        with install_kodi_stubs(), mock.patch.object(
            sys, "argv", ["plugin://plugin.video.yawspreloaded/", "17", "?action=settings"]
        ), mock.patch.object(production_router, "TMDBServiceClient") as background_type, mock.patch.object(
            production_router, "DownloadJobManager"
        ) as download_jobs_type:
            background = background_type.return_value
            download_jobs = download_jobs_type.return_value
            production_router.close()
            owner = production_router._get_owner()

            self.assertIs(owner, production_router._get_owner())
            self.assertNotIn("system_localizer", owner._runtime_options)
            production_router.router("action=settings")
            recorder = sys.modules["xbmcplugin"]._recorder
            self.assertEqual(len(recorder.resolved_url_calls), 1)
            self.assertEqual(sys.modules["xbmcaddon"].Addon().open_settings_calls, 1)

            for language, expected in (
                ("cs", "cs-CZ"),
                ("Czech", "cs-CZ"),
                ("sk-SK", "sk-SK"),
                ("Slovak", "sk-SK"),
                ("en", "en-US"),
                ("de", "en-US"),
            ):
                with self.subTest(language=language):
                    sys.modules["xbmc"]._language = language
                    self.assertEqual(owner.language(), expected)

            menu_calls = []
            with mock.patch.object(
                production_router,
                "build_webshare_handlers",
                return_value={
                    "menu": lambda params: menu_calls.append("webshare") or DirectoryResult()
                },
            ) as webshare_build, mock.patch.object(
                production_router,
                "build_tmdb_handlers",
                return_value={
                    "menu": lambda params: menu_calls.append("tmdb") or DirectoryResult()
                },
            ):
                production_router.router("")
            self.assertEqual(menu_calls, ["webshare", "tmdb"])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertIs(webshare_build.call_args.kwargs["download_jobs"], download_jobs)

            self.assertEqual(background_type.call_count, 1)
            self.assertEqual(download_jobs_type.call_count, 1)
            self.assertEqual(
                download_jobs_type.call_args.kwargs["abort"](), False
            )
            self.assertTrue(
                hasattr(owner, "snapshot_store"),
                "production owner does not provide persistent watchlist snapshots",
            )
            self.assertIs(
                background_type.call_args.kwargs["snapshot_store"], owner.snapshot_store
            )
            self.assertIs(
                background_type.call_args.kwargs["store"],
                owner.service_store,
            )
            self.assertIs(background.genre_repository, owner.genre_repository)
            production_router.close()
            background.close.assert_called_once_with(wait=False)
            download_jobs.close.assert_called_once_with(wait=False)

    def test_production_watched_without_api_key_is_one_clean_action_completion(self):
        with install_kodi_stubs(), mock.patch.object(
            sys, "argv", ["plugin://plugin.video.yawspreloaded/", "17", "?action=tmdb_watchlist"]
        ), mock.patch.object(production_router, "TMDBServiceClient"):
            production_router.close()
            owner = production_router._get_owner()
            logger = RecordingLogger()
            owner.runner.logger = logger
            recorder = sys.modules["xbmcplugin"]._recorder
            addon = sys.modules["xbmcaddon"].Addon()

            production_router.router(
                "action=tmdb_watchlist&category=movies&watched=42"
            )

            self.assertEqual(len(recorder.resolved_url_calls), 1)
            self.assertFalse(recorder.resolved_url_calls[0]["succeeded"])
            self.assertEqual(recorder.end_of_directory_calls, [])
            self.assertEqual(len(recorder.notifications), 1)
            self.assertEqual(addon.open_settings_calls, 1)
            self.assertEqual(logger.calls, [])

    def run_production_vfs_download(self, response, vfs):
        with install_kodi_stubs(), mock.patch.object(
            sys,
            "argv",
            ["plugin://plugin.video.yawspreloaded/", "17", "?action=download"],
        ), mock.patch.object(production_router, "TMDBServiceClient"), mock.patch.object(
            production_router.requests, "Session"
        ) as session_type, mock.patch.object(production_router, "WebshareClient") as client_type:
            ui_session = FakeSession()
            ui_session.headers = {}
            worker_session = FakeSession([response])
            worker_session.headers = {}
            session_type.side_effect = [ui_session, worker_session]
            response_closed = threading.Event()
            close_response = response.close

            def observed_close():
                close_response()
                response_closed.set()

            response.close = observed_close
            client = FakeClient()
            client.link_results = ["https://cdn.invalid/movie"]
            client.info_results = [
                xml("<response><name>Příliš žluťoučký.mkv</name><ident>abc</ident></response>")
            ]
            client_type.return_value = client
            addon = sys.modules["xbmcaddon"].Addon()
            addon.setSetting("dfolder", "smb://server/share")
            addon.setSetting("dnormalize", "false")
            addon.setSetting("dnotify", "false")
            vfs_module = sys.modules["xbmcvfs"]
            vfs_module.exists = vfs.exists
            vfs_module.File = vfs.File
            vfs_module.rename = vfs.rename
            vfs_module.delete = vfs.delete
            production_router.close()
            production_router._get_owner()
            recorder = sys.modules["xbmcplugin"]._recorder
            terminal_notification = threading.Event()
            record_notification = recorder.notification

            def observed_notification(*args, **kwargs):
                outcome = record_notification(*args, **kwargs)
                if args[1].startswith(("Downloaded", "Download failed")):
                    terminal_notification.set()
                return outcome

            recorder.notification = observed_notification

            result = production_router.router("action=download&ident=abc")
            if not terminal_notification.wait(2):
                raise AssertionError("production media worker did not report its terminal result")
            if not response_closed.is_set():
                raise AssertionError("production media worker did not close its response")
            if ui_session.calls:
                raise AssertionError("media worker used the UI owner's HTTP session")

            return result, worker_session, client, sys.modules["xbmcplugin"]._recorder

    def test_production_remote_download_uses_vfs_stream_close_and_rename(self):
        response = FakeResponse(
            headers={"Content-Length": "6"},
            chunks=[b"abc", b"def"],
            forbid_content=True,
        )
        vfs = RecordingVFS(folders={"smb://server/share"})

        result, session, _, recorder = self.run_production_vfs_download(response, vfs)

        destination = "smb://server/share/Příliš žluťoučký.mkv"
        self.assertTrue(result.handled)
        self.assertEqual(vfs.files, {destination: b"abcdef"})
        self.assertEqual(vfs.renames[0][1], destination)
        self.assertTrue(vfs.renames[0][0].startswith(destination + ".part-"))
        self.assertTrue(all(output.closed for output in vfs.open_files))
        self.assertTrue(response.closed)
        self.assertEqual(session.calls[0]["kwargs"]["stream"], True)
        self.assertEqual(len(recorder.resolved_url_calls), 1)
        self.assertEqual(recorder.resolved_url_calls[-1]["handle"], 17)

    def test_production_remote_download_failure_closes_and_cleans_partial_vfs_file(self):
        for failure in ("stream", "write", "rename"):
            with self.subTest(failure=failure):
                response = FakeResponse(
                    headers={"Content-Length": "6"},
                    chunks=[b"abcdef"],
                    stream_error=RuntimeError("interrupted") if failure == "stream" else None,
                    forbid_content=True,
                )
                vfs = RecordingVFS(folders={"smb://server/share"})
                vfs.fail_writes = failure == "write"
                vfs.fail_renames = failure == "rename"

                with self.assertLogs("yawsp_app.router", level="ERROR"):
                    result, _, _, _ = self.run_production_vfs_download(response, vfs)

                self.assertTrue(result.handled)
                self.assertEqual(vfs.files, {})
                self.assertEqual(len(vfs.renames), 1 if failure == "rename" else 0)
                self.assertEqual(len(vfs.open_files), 1)
                self.assertTrue(vfs.open_files[0].closed)
                self.assertEqual(len(vfs.deleted), 1)
                self.assertTrue(response.closed)

    def test_real_owner_rebuilds_runtime_and_runner_for_sequential_invocations(self):
        with install_kodi_stubs(), mock.patch.object(
            sys,
            "argv",
            ["plugin://first.invalid/", "17", ""],
        ), mock.patch.object(production_router, "TMDBServiceClient"):
            production_router.close()
            owner = production_router._get_owner()

            def webshare_handlers(runtime, client_factory, **kwargs):
                def directory(params):
                    runtime.add_item(
                        runtime.base_url + "?generated=1", runtime.list_item(label="Directory")
                    )
                    return DirectoryResult()

                def play(params):
                    return PlaybackResult(
                        True, runtime.list_item(label="Play", path=runtime.base_url + "media")
                    )

                def action(params):
                    return ActionResult(play_media="PlayMedia(" + runtime.base_url + "action)")

                return {"menu": directory, "search": directory, "play": play, "settings": action}

            def tmdb_handlers(runtime, client_factory, repositories, background):
                return {"menu": lambda params: DirectoryResult()}

            with mock.patch.object(
                production_router, "build_webshare_handlers", side_effect=webshare_handlers
            ), mock.patch.object(
                production_router, "build_tmdb_handlers", side_effect=tmdb_handlers
            ):
                for route, kind in (
                    ("action=search", "directory"),
                    ("action=play", "playback"),
                    ("action=settings", "action"),
                ):
                    for handle, base_url in (
                        (17, "plugin://first.invalid/"),
                        (23, "plugin://second.invalid/"),
                    ):
                        with self.subTest(route=route, handle=handle):
                            sys.argv[:] = [base_url, str(handle), "?" + route]
                            production_router.router(route)
                            recorder = sys.modules["xbmcplugin"]._recorder
                            if kind == "directory":
                                self.assertEqual(recorder.items[-1]["handle"], handle)
                                self.assertEqual(
                                    recorder.items[-1]["url"], base_url + "?generated=1"
                                )
                                self.assertEqual(
                                    recorder.end_of_directory_calls[-1]["handle"]
                                    if "handle" in recorder.end_of_directory_calls[-1]
                                    else recorder.calls[-1]["args"][0],
                                    handle,
                                )
                            else:
                                self.assertEqual(
                                    recorder.resolved_url_calls[-1]["handle"], handle
                                )
                                if kind == "playback":
                                    self.assertEqual(
                                        recorder.resolved_url_calls[-1]["listitem"].path,
                                        base_url + "media",
                                    )
                                else:
                                    self.assertEqual(
                                        recorder.builtin_commands[-1],
                                        "PlayMedia(" + base_url + "action)",
                                    )

            self.assertIs(owner, production_router._get_owner())

    def test_production_tmdb_invocation_never_replaces_real_service_composition(self):
        with production_tmdb_invocation([]) as (owner, _session, _addon, _recorder):
            self.assertIsInstance(owner.background, production_router.TMDBServiceClient)
            self.assertIs(owner.background.store, owner.service_store)
            self.assertFalse(hasattr(owner.background, "worker"))

    def test_production_cold_mid_page_account_switch_discards_then_recovers(self):
        def switch_account(addon):
            addon.setSetting("tmdb_account_id", "account-2")
            addon.setSetting("tmdb_access_token", "session-2")

        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            session.queue_response(
                json_response(
                    {"page": 1, "total_pages": 2, "results": [{"id": 1, "title": "Old"}]}
                )
            )
            session.queue_response(
                switch_json_response(
                    {"page": 2, "total_pages": 2, "results": [{"id": 2, "title": "Mixed"}]},
                    lambda: switch_account(addon),
                )
            )

            with self.assertLogs("yawsp_app.router", level="ERROR"):
                result = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertFalse(result.succeeded)
            self.assertEqual(recorder.items, [])
            self.assertIsNone(
                owner.snapshot_store.load(SnapshotScope("account-1", "movies", "en-US"))
            )
            self.assertIsNone(
                owner.snapshot_store.load(SnapshotScope("account-2", "movies", "en-US"))
            )

            session.queue_response(
                json_response({"total_pages": 1, "results": [{"id": 9, "title": "Current"}]})
            )
            session.queue_response(json_response({"genres": []}))
            recovered = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertTrue(recovered.succeeded)
            self.assertEqual(recorder.items[-2]["listitem"].label, "Current")
            self.assertEqual(recorder.items[-1]["listitem"].label, "Refresh")
            self.assertIsNone(
                owner.snapshot_store.load(SnapshotScope("account-1", "movies", "en-US"))
            )
            self.assertEqual(
                owner.snapshot_store.load(
                    SnapshotScope("account-2", "movies", "en-US")
                ).results,
                [{"id": 9, "title": "Current"}],
            )

    def test_production_cold_mid_page_language_switch_discards_then_recovers(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            session.queue_response(
                json_response(
                    {"page": 1, "total_pages": 2, "results": [{"id": 1, "title": "English"}]}
                )
            )
            session.queue_response(
                switch_json_response(
                    {"page": 2, "total_pages": 2, "results": [{"id": 2, "title": "Smíšené"}]},
                    lambda: setattr(sys.modules["xbmc"], "_language", "cs"),
                )
            )

            with self.assertLogs("yawsp_app.router", level="ERROR"):
                result = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertFalse(result.succeeded)
            self.assertEqual(recorder.items, [])
            for language in ("en-US", "cs-CZ"):
                self.assertIsNone(
                    owner.snapshot_store.load(
                        SnapshotScope("account-1", "movies", language)
                    )
                )

            session.queue_response(
                json_response({"total_pages": 1, "results": [{"id": 3, "title": "Česky"}]})
            )
            session.queue_response(json_response({"genres": []}))
            recovered = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertTrue(recovered.succeeded)
            self.assertEqual(recorder.items[-2]["listitem"].label, "Česky")
            self.assertEqual(recorder.items[-1]["listitem"].label, "Refresh")
            self.assertIsNone(
                owner.snapshot_store.load(SnapshotScope("account-1", "movies", "en-US"))
            )
            self.assertEqual(
                owner.snapshot_store.load(
                    SnapshotScope("account-1", "movies", "cs-CZ")
                ).results,
                [{"id": 3, "title": "Česky"}],
            )

    def test_production_watched_switch_does_not_mutate_new_account_or_queue_refresh(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            first_scope = SnapshotScope("account-1", "movies", "en-US")
            second_scope = SnapshotScope("account-2", "movies", "en-US")
            first = owner.snapshot_store.save_complete(
                first_scope, [{"id": 42, "title": "First"}], 100.0
            )
            second = owner.snapshot_store.save_complete(
                second_scope, [{"id": 42, "title": "Second"}], 100.0
            )

            def switch_account():
                addon.setSetting("tmdb_account_id", "account-2")
                addon.setSetting("tmdb_access_token", "session-2")

            session.queue_response(
                switch_json_response({"success": True}, switch_account)
            )

            with self.assertLogs("yawsp_app.router", level="ERROR"):
                result = production_router.router(
                    "action=tmdb_watchlist&category=movies&watched=42"
                )

            self.assertFalse(result.handled)
            self.assertEqual(owner.snapshot_store.load(first_scope), first)
            self.assertEqual(owner.snapshot_store.load(second_scope), second)
            self.assertEqual(service_jobs(owner, pending=True), [])
            self.assertEqual(session.calls[0]["url"], "https://api.themoviedb.org/3/account/account-1/watchlist")
            self.assertEqual(session.calls[0]["kwargs"]["params"]["session_id"], "session-1")
            self.assertEqual(recorder.builtin_commands, [])

    def test_production_stored_account_cleared_before_submit_rejects_refresh(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            scope = SnapshotScope("account-1", "movies", "en-US")
            owner.snapshot_store.save_complete(
                scope,
                [{"id": 42, "title": "Remove"}, {"id": 2, "title": "Keep"}],
                100.0,
            )
            remove = owner.snapshot_store.remove

            def remove_then_clear_account(_store, scope, media_id):
                snapshot = remove(scope, media_id)
                addon.setSetting("tmdb_account_id", "")
                return snapshot

            session.queue_response(json_response({"success": True}))
            with mock.patch.object(
                type(owner.snapshot_store),
                "remove",
                autospec=True,
                side_effect=remove_then_clear_account,
            ):
                result = production_router.router(
                    "action=tmdb_watchlist&category=movies&watched=42"
                )

            self.assertTrue(result.handled)
            self.assertEqual(addon.getSetting("tmdb_account_id"), "")
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-1")
            self.assertEqual(service_jobs(owner, pending=True), [])
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(
                session.calls[0]["url"],
                "https://api.themoviedb.org/3/account/account-1/watchlist",
            )
            self.assertEqual(
                session.calls[0]["kwargs"]["params"]["session_id"], "session-1"
            )
            self.assertEqual(
                owner.snapshot_store.load(scope).results,
                [{"id": 2, "title": "Keep"}],
            )

    def test_production_missing_account_second_open_reuses_complete_scoped_snapshot(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_account_id", "")
            movies = [
                {"id": 1, "title": "Chata", "release_date": "2026-01-01"},
                {"id": 2, "title": "Áčko", "release_date": "2025-01-01"},
            ]
            session.queue_response(json_response({"id": "account-1"}))
            session.queue_response(
                json_response({"total_pages": 1, "results": movies})
            )
            session.queue_response(
                json_response({"genres": [{"id": 12, "name": "Adventure"}]})
            )

            first = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertTrue(first.succeeded)
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-1")
            self.assertEqual(len(session.calls), 3)
            self.assertEqual(session.calls[0]["url"], "https://api.themoviedb.org/3/account")
            self.assertIn("/watchlist/movies", session.calls[1]["url"])
            self.assertIn("/genre/movie/list", session.calls[2]["url"])
            self.assertEqual(
                [item["listitem"].label.split(" (")[0] for item in recorder.items[:-1]],
                ["Áčko", "Chata"],
            )
            self.assertEqual(recorder.items[-1]["listitem"].label, "Refresh")
            scope = SnapshotScope("account-1", "movies", "en-US")
            owner.snapshot_store.save_complete(scope, movies, 0.0)
            recorder.items.clear()

            second = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertTrue(second.succeeded)
            self.assertEqual(len(session.calls), 3)
            self.assertEqual(
                [call for call in session.calls if "/watchlist/movies" in call["url"]],
                [session.calls[1]],
            )
            self.assertEqual(
                [call for call in session.calls if "/genre/movie/list" in call["url"]],
                [session.calls[2]],
            )
            self.assertEqual(
                [item["listitem"].label.split(" (")[0] for item in recorder.items[:-1]],
                ["Áčko", "Chata"],
            )
            self.assertEqual(recorder.items[-1]["listitem"].label, "Refresh")
            self.assertEqual(owner.snapshot_store.load(scope).results, movies)
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-1")
            watchlist_jobs = [
                job
                for job in service_jobs(owner, "watchlist.refresh", pending=True)
                if not job["interactive"]
            ]
            self.assertEqual(len(watchlist_jobs), 1)
            self.assertEqual(
                watchlist_jobs[0]["request"]["params"]["scope"],
                dict(scope.__dict__),
            )
            self.assertIs(watchlist_jobs[0]["request"]["force"], False)
            self.assertEqual(watchlist_jobs[0]["state"], "queued")

    def test_production_worker_session_factories_clone_main_headers_without_sharing(self):
        with tempfile.TemporaryDirectory() as profile, install_kodi_stubs(), mock.patch.object(
            sys,
            "argv",
            ["plugin://plugin.video.yawspreloaded/", "17", ""],
        ):
            translate = lambda value: profile
            sys.modules["xbmc"].translatePath = translate
            sys.modules["xbmcvfs"].translatePath = translate
            production_router.close()
            owner = production_router._get_owner()
            try:
                self.assertIsInstance(owner.background, production_router.TMDBServiceClient)
                self.assertFalse(hasattr(owner.background, "session_factory"))
                self.assertFalse(hasattr(owner.background, "worker"))
                workers = (
                    owner.download_jobs._session_factory(),
                    owner.download_jobs._session_factory(),
                )
                try:
                    self.assertTrue(all(session is not owner.session for session in workers))
                    self.assertEqual(len({id(session) for session in workers}), 2)
                    for session in workers:
                        self.assertEqual(
                            session.headers["User-Agent"],
                            owner.session.headers["User-Agent"],
                        )
                        self.assertEqual(
                            session.headers["Referer"],
                            owner.session.headers["Referer"],
                        )
                        self.assertEqual(session.headers, owner.session.headers)
                        self.assertIsNot(session.headers, owner.session.headers)
                finally:
                    for session in workers:
                        session.close()
            finally:
                production_router.close()

    def test_production_initially_missing_account_keeps_refresh_scope_and_session(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            scope = SnapshotScope("account-1", "movies", "en-US")
            owner.snapshot_store.save_complete(
                scope,
                [{"id": 42, "title": "Remove"}, {"id": 2, "title": "Keep"}],
                100.0,
            )
            addon.setSetting("tmdb_account_id", "")
            session.queue_response(json_response({"id": "account-1"}))
            session.queue_response(json_response({"success": True}))
            session.queue_response(
                json_response(
                    {"total_pages": 1, "results": [{"id": 2, "title": "Refreshed"}]}
                )
            )
            session.queue_response(json_response({"genres": []}))

            result = production_router.router(
                "action=tmdb_watchlist&category=movies&watched=42"
            )

            self.assertTrue(result.handled)
            refreshes = [
                job
                for job in service_jobs(owner, "watchlist.refresh")
                if not job["interactive"]
            ]
            self.assertEqual(len(refreshes), 1)
            self.assertEqual(refreshes[0]["state"], "succeeded")
            self.assertTrue(refreshes[0]["request"]["force"])
            owner.service_store.runner.run_once()
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-1")
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-1")
            self.assertEqual(len(session.calls), 4)
            self.assertEqual(
                session.calls[0]["url"], "https://api.themoviedb.org/3/account"
            )
            self.assertEqual(
                session.calls[1]["url"],
                "https://api.themoviedb.org/3/account/account-1/watchlist",
            )
            self.assertEqual(
                session.calls[2]["url"],
                "https://api.themoviedb.org/3/account/account-1/watchlist/movies",
            )
            self.assertEqual(session.calls[3]["url"], "https://api.themoviedb.org/3/genre/movie/list")
            self.assertNotIn("session_id", session.calls[3]["kwargs"]["params"])
            for call in session.calls[:3]:
                self.assertEqual(call["kwargs"]["params"]["session_id"], "session-1")
            self.assertEqual(
                owner.snapshot_store.load(scope).results,
                [{"id": 2, "title": "Refreshed"}],
            )

    def test_production_cold_account_lookup_switch_preserves_new_identity_and_recovers(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_account_id", "")

            def select_new_account():
                addon.setSetting("tmdb_account_id", "account-2")
                addon.setSetting("tmdb_access_token", "session-2")

            session.queue_response(
                switch_json_response({"id": "account-1"}, select_new_account)
            )

            with self.assertLogs("yawsp_app.router", level="ERROR"):
                result = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertFalse(result.succeeded)
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-2")
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-2")
            self.assertEqual(session.calls[0]["url"], "https://api.themoviedb.org/3/account")
            self.assertEqual(session.calls[0]["kwargs"]["params"]["session_id"], "session-1")
            self.assertIsNone(
                owner.snapshot_store.load(SnapshotScope("account-1", "movies", "en-US"))
            )
            self.assertIsNone(
                owner.snapshot_store.load(SnapshotScope("account-2", "movies", "en-US"))
            )

            session.queue_response(
                json_response({"total_pages": 1, "results": [{"id": 9, "title": "Current"}]})
            )
            session.queue_response(json_response({"genres": []}))
            recovered = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertTrue(recovered.succeeded)
            self.assertEqual(
                session.calls[1]["url"],
                "https://api.themoviedb.org/3/account/account-2/watchlist/movies",
            )
            self.assertEqual(session.calls[1]["kwargs"]["params"]["session_id"], "session-2")
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-2")
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-2")
            self.assertIsNone(
                owner.snapshot_store.load(SnapshotScope("account-1", "movies", "en-US"))
            )
            self.assertEqual(
                owner.snapshot_store.load(
                    SnapshotScope("account-2", "movies", "en-US")
                ).results,
                [{"id": 9, "title": "Current"}],
            )

    def _assert_production_qr_switch_after_final_guard(
        self, initial_query, recovery_query=None, account_unauthorized=False
    ):
        recovery_query = recovery_query or "action=tmdb_watchlist&category=movies"
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_account_id", "")
            original_request = session.request

            def route_response(method, url, **kwargs):
                status_code = 200
                if url.endswith("/3/account"):
                    session_id = kwargs["params"].get("session_id")
                    if account_unauthorized and session_id != "session-2":
                        data = {"success": False}
                        status_code = 401
                    else:
                        data = {
                            "id": "account-1" if session_id == "session-1" else "account-2"
                        }
                elif url.endswith("/3/authentication/token/new"):
                    data = {"request_token": "request-token"}
                elif url.endswith("/3/authentication/session/new"):
                    data = {"session_id": "session-2"}
                elif method.upper() == "POST" and url.endswith("/watchlist"):
                    data = {"success": True}
                elif url.endswith("/watchlist/movies"):
                    data = {"total_pages": 1, "results": []}
                else:
                    raise AssertionError("unexpected offline URL: {0}".format(url))
                session.queue_response(json_response(data, status_code=status_code))
                return original_request(method, url, **kwargs)

            session.request = route_response
            transition_condition = threading.Condition()
            transition_state = []
            qr_threads = []

            def transition_advanced(state):
                with transition_condition:
                    transition_state.append(state)
                    transition_condition.notify_all()

            class ObservedIdentityLock:
                def __init__(self, delegate):
                    self.delegate = delegate

                def __enter__(self):
                    if qr_threads and threading.current_thread() is qr_threads[0]:
                        transition_advanced("lock-waiting")
                    self.delegate.acquire()
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    self.delegate.release()

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

            original_identity_transaction = owner.service_store.identity_transaction

            def observed_identity_transaction():
                if qr_threads and threading.current_thread() is qr_threads[0]:
                    transition_advanced("lock-waiting")
                return original_identity_transaction()

            owner.service_store.identity_transaction = observed_identity_transaction
            original_set = addon.setSetting

            def coordinate_exact_publication_boundary(key, value):
                if (
                    key == "tmdb_access_token"
                    and value == "session-2"
                    and qr_threads
                    and threading.current_thread() is qr_threads[0]
                ):
                    original_set(key, value)
                    transition_advanced("replaced")
                    return
                publication_boundary = (
                    not account_unauthorized
                    and key == "tmdb_account_id"
                    and value == "account-1"
                )
                invalidation_boundary = (
                    account_unauthorized
                    and key == "tmdb_access_token"
                    and value == ""
                )
                if (publication_boundary or invalidation_boundary) and not qr_threads:
                    # Exactly after the selected final guard and immediately before
                    # its old account publication or captured-session invalidation.
                    qr_thread = threading.Thread(
                        target=lambda: qr_results.append(
                            production_router.router("action=tmdb_auth")
                        )
                    )
                    qr_threads.append(qr_thread)
                    qr_thread.start()
                    with transition_condition:
                        if not transition_condition.wait_for(
                            lambda: bool(transition_state), 2
                        ):
                            raise AssertionError("QR transition did not reach its seam")
                original_set(key, value)

            addon.setSetting = coordinate_exact_publication_boundary
            qr_results = []
            first_results = []
            first_thread = threading.Thread(
                target=lambda: first_results.append(
                    production_router.router(initial_query)
                )
            )
            with mock.patch.object(production_router._LOGGER, "exception"):
                first_thread.start()
                first_thread.join(2)
                for qr_thread in qr_threads:
                    qr_thread.join(2)

                self.assertFalse(first_thread.is_alive())
                self.assertTrue(qr_threads)
                self.assertFalse(qr_threads[0].is_alive())
                self.assertTrue(qr_results[0].handled)
                self.assertEqual(len(first_results), 1)
                first_persisted_pair = (
                    addon.getSetting("tmdb_account_id"),
                    addon.getSetting("tmdb_access_token"),
                )

                if "watched=" in recovery_query:
                    owner.snapshot_store.save_complete(
                        SnapshotScope("account-2", "movies", "en-US"),
                        [{"id": 42, "title": "Remove current"}],
                        100.0,
                    )
                second_result = production_router.router(recovery_query)

            mixed_requests = [
                call
                for call in session.calls
                if call["url"].endswith("/3/account/account-1/watchlist/movies")
                and call["kwargs"]["params"].get("session_id") == "session-2"
            ]
            self.assertEqual(mixed_requests, [])
            self.assertEqual(first_persisted_pair, ("", "session-2"))
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-2")
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-2")
            if isinstance(second_result, ActionResult):
                self.assertTrue(second_result.handled)
                self.assertTrue(second_result.refresh)
                recovery_calls = [
                    call
                    for call in session.calls
                    if call["method"] == "POST"
                    and call["url"].endswith("/3/account/account-2/watchlist")
                    and call["kwargs"]["params"].get("session_id") == "session-2"
                ]
                self.assertEqual(len(recovery_calls), 1)
                self.assertEqual(
                    recovery_calls[0]["kwargs"]["json"],
                    {"media_type": "movie", "media_id": 42, "watchlist": False},
                )
            else:
                self.assertTrue(second_result.succeeded)
                self.assertTrue(
                    any(
                        call["url"].endswith("/3/account/account-2/watchlist/movies")
                        and call["kwargs"]["params"].get("session_id") == "session-2"
                        for call in session.calls
                    )
                )

    def test_production_qr_switch_after_final_guard_cannot_publish_mixed_identity(self):
        self._assert_production_qr_switch_after_final_guard(
            "action=tmdb_watchlist&category=movies"
        )

    def test_production_watched_qr_switch_after_final_guard_cannot_publish_mixed_identity(self):
        self._assert_production_qr_switch_after_final_guard(
            "action=tmdb_watchlist&category=movies&watched=42"
        )

    def test_production_background_stale_401_cannot_clear_new_qr_session(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            original_request = session.request

            def route_response(method, url, **kwargs):
                status_code = 200
                session_id = kwargs["params"].get("session_id")
                if url.endswith("/3/account/account-1/watchlist/movies"):
                    data = {"success": False}
                    status_code = 401
                elif url.endswith("/3/account/account-2/watchlist/movies"):
                    data = {"total_pages": 1, "results": []}
                elif url.endswith("/3/account"):
                    data = {"id": "account-2"}
                elif url.endswith("/3/authentication/token/new"):
                    data = {"request_token": "request-token"}
                elif url.endswith("/3/authentication/session/new"):
                    data = {"session_id": "session-2"}
                else:
                    raise AssertionError("unexpected offline URL: {0}".format(url))
                session.queue_response(json_response(data, status_code=status_code))
                return original_request(method, url, **kwargs)

            session.request = route_response
            transition_condition = threading.Condition()
            transition_state = []
            qr_threads = []
            refresh_threads = []

            def transition_advanced(state):
                with transition_condition:
                    transition_state.append(state)
                    transition_condition.notify_all()

            class ObservedIdentityLock:
                def __init__(self, delegate):
                    self.delegate = delegate

                def __enter__(self):
                    if qr_threads and threading.current_thread() is qr_threads[0]:
                        transition_advanced("lock-waiting")
                    self.delegate.acquire()
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    self.delegate.release()

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

            original_identity_transaction = owner.service_store.identity_transaction

            def observed_identity_transaction():
                if qr_threads and threading.current_thread() is qr_threads[0]:
                    transition_advanced("lock-waiting")
                return original_identity_transaction()

            owner.service_store.identity_transaction = observed_identity_transaction
            original_set = addon.setSetting

            def coordinate_exact_invalidation_boundary(key, value):
                if (
                    key == "tmdb_access_token"
                    and value == "session-2"
                    and qr_threads
                    and threading.current_thread() is qr_threads[0]
                ):
                    original_set(key, value)
                    transition_advanced("replaced")
                    return
                if (
                    key == "tmdb_access_token"
                    and value == ""
                    and refresh_threads
                    and threading.current_thread() is refresh_threads[0]
                    and not qr_threads
                ):
                    # The old refresh passed its current-session check. QR now races
                    # the real worker's captured-session invalidation write.
                    qr_thread = threading.Thread(
                        target=lambda: qr_results.append(
                            production_router.router("action=tmdb_auth")
                        )
                    )
                    qr_threads.append(qr_thread)
                    qr_thread.start()
                    with transition_condition:
                        if not transition_condition.wait_for(
                            lambda: bool(transition_state), 2
                        ):
                            raise AssertionError("QR transition did not reach its seam")
                original_set(key, value)

            scope = SnapshotScope("account-1", "movies", "en-US")
            self.assertEqual(owner.background.submit_watchlist(scope, force=True), 1)
            addon.setSetting = coordinate_exact_invalidation_boundary
            qr_results = []
            refresh_thread = threading.Thread(target=owner.service_store.runner.run_once)
            refresh_threads.append(refresh_thread)
            with mock.patch.object(production_router._LOGGER, "exception"):
                refresh_thread.start()
                refresh_thread.join(2)
                for qr_thread in qr_threads:
                    qr_thread.join(2)

                self.assertFalse(refresh_thread.is_alive())
                self.assertTrue(qr_threads)
                self.assertFalse(qr_threads[0].is_alive())
                self.assertTrue(qr_results[0].handled)
                first_persisted_pair = (
                    addon.getSetting("tmdb_account_id"),
                    addon.getSetting("tmdb_access_token"),
                )
                next_result = production_router.router(
                    "action=tmdb_watchlist&category=movies"
                )

            mixed_requests = [
                call
                for call in session.calls
                if call["url"].endswith("/3/account/account-1/watchlist/movies")
                and call["kwargs"]["params"].get("session_id") == "session-2"
            ]
            self.assertEqual(mixed_requests, [])
            self.assertEqual(first_persisted_pair, ("", "session-2"))
            self.assertTrue(next_result.succeeded)
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-2")
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-2")
            self.assertTrue(
                any(
                    call["url"].endswith("/3/account/account-2/watchlist/movies")
                    and call["kwargs"]["params"].get("session_id") == "session-2"
                    for call in session.calls
                )
            )
            self.assertEqual(service_jobs(owner, pending=True), [])

    def _assert_production_stale_generic_401(
        self, stale_url, prepare_stale_request, expected_stale_calls=1
    ):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            original_request = session.request
            unauthorized_responses = []

            def route_response(method, url, **kwargs):
                status_code = 200
                if url.endswith(stale_url):
                    data = {"success": False}
                    status_code = 401
                elif url.endswith("/3/account"):
                    data = {"id": "account-2"}
                elif url.endswith("/3/authentication/token/new"):
                    data = {"request_token": "request-token"}
                elif url.endswith("/3/authentication/session/new"):
                    data = {"session_id": "session-2"}
                elif url.endswith("/3/account/account-2/watchlist/movies"):
                    data = {"total_pages": 1, "results": []}
                else:
                    raise AssertionError("unexpected offline URL: {0}".format(url))
                response = json_response(data, status_code=status_code)
                if status_code == 401:
                    unauthorized_responses.append(response)
                session.queue_response(response)
                return original_request(method, url, **kwargs)

            session.request = route_response
            transition_condition = threading.Condition()
            transition_state = []
            qr_threads = []

            def transition_advanced(state):
                with transition_condition:
                    transition_state.append(state)
                    transition_condition.notify_all()

            original_identity_transaction = owner.service_store.identity_transaction
            identity_holders = set()

            @contextlib.contextmanager
            def observed_identity_transaction():
                thread_id = threading.get_ident()
                is_qr = qr_threads and threading.current_thread() is qr_threads[0]
                if is_qr:
                    transition_advanced("lock-waiting")
                with original_identity_transaction():
                    with transition_condition:
                        identity_holders.add(thread_id)
                    if is_qr:
                        transition_advanced("lock-acquired")
                    try:
                        yield owner.service_store
                    finally:
                        with transition_condition:
                            identity_holders.discard(thread_id)

            owner.service_store.identity_transaction = observed_identity_transaction
            original_set = addon.setSetting
            stale_threads = []

            def coordinate_exact_invalidation_boundary(key, value):
                if (
                    key == "tmdb_access_token"
                    and value == "session-2"
                    and qr_threads
                    and threading.current_thread() is qr_threads[0]
                ):
                    original_set(key, value)
                    transition_advanced("replaced")
                    return
                if (
                    key == "tmdb_access_token"
                    and value == ""
                    and stale_threads
                    and threading.current_thread() is stale_threads[0]
                    and not qr_threads
                ):
                    qr_thread = threading.Thread(
                        target=lambda: qr_results.append(
                            production_router.router("action=tmdb_auth")
                        )
                    )
                    qr_threads.append(qr_thread)
                    qr_thread.start()
                    with transition_condition:
                        if not transition_condition.wait_for(
                            lambda: "lock-waiting" in transition_state, 2
                        ):
                            raise AssertionError("QR transition did not reach its seam")
                        if threading.get_ident() not in identity_holders:
                            # Broken metadata has no compare-and-clear guard. Force
                            # the complete replacement before its stale clear; the
                            # guarded main-UI control instead releases QR above.
                            if not transition_condition.wait_for(
                                lambda: "replaced" in transition_state, 2
                            ):
                                raise AssertionError(
                                    "Unguarded QR replacement did not complete"
                                )
                original_set(key, value)

            qr_results = []
            stale_results = []
            start_stale_request = prepare_stale_request(owner)
            stale_thread = threading.Thread(
                target=lambda: stale_results.append(start_stale_request())
            )
            stale_threads.append(stale_thread)
            addon.setSetting = coordinate_exact_invalidation_boundary
            with mock.patch.object(production_router._LOGGER, "exception"):
                stale_thread.start()
                stale_thread.join(2)
                for qr_thread in qr_threads:
                    qr_thread.join(2)

                self.assertFalse(stale_thread.is_alive())
                self.assertTrue(qr_threads)
                self.assertFalse(qr_threads[0].is_alive())
                self.assertTrue(qr_results[0].handled)
                self.assertEqual(len(stale_results), 1)
                first_persisted_pair = (
                    addon.getSetting("tmdb_account_id"),
                    addon.getSetting("tmdb_access_token"),
                )
                next_result = production_router.router(
                    "action=tmdb_watchlist&category=movies"
                )

            stale_calls = [call for call in session.calls if call["url"].endswith(stale_url)]
            self.assertEqual(len(stale_calls), expected_stale_calls)
            self.assertTrue(
                all(call["kwargs"]["params"].get("session_id") == "session-1"
                    for call in stale_calls)
            )
            self.assertTrue(unauthorized_responses)
            self.assertTrue(all(response.closed for response in unauthorized_responses))
            self.assertEqual(first_persisted_pair, ("", "session-2"))
            self.assertTrue(next_result.succeeded)
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-2")
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-2")
            recovery_calls = [
                call
                for call in session.calls
                if call["url"].endswith("/3/account/account-2/watchlist/movies")
                and call["kwargs"]["params"].get("session_id") == "session-2"
            ]
            self.assertEqual(len(recovery_calls), 1)

    def test_production_background_metadata_stale_401_cannot_clear_new_qr_session(self):
        def prepare(owner):
            self.assertEqual(owner.background.submit_trailers("movie", [42]), 1)
            return owner.service_store.runner.run_once

        self._assert_production_stale_generic_401(
            "/3/movie/42/videos", prepare, expected_stale_calls=1
        )

    def test_production_main_ui_stale_401_cannot_clear_new_qr_session(self):
        def prepare(owner):
            return lambda: production_router.router(
                "action=tmdb_watchlist&category=shows&show_id=42"
            )

        self._assert_production_stale_generic_401("/3/tv/42", prepare)

    def test_production_stale_account_401_cannot_clear_new_qr_session(self):
        self._assert_production_qr_switch_after_final_guard(
            "action=tmdb_watchlist&category=movies",
            account_unauthorized=True,
        )

    def test_production_watched_stale_account_401_cannot_clear_new_qr_session(self):
        watched_query = "action=tmdb_watchlist&category=movies&watched=42"
        self._assert_production_qr_switch_after_final_guard(
            watched_query,
            recovery_query=watched_query,
            account_unauthorized=True,
        )

    def test_production_watched_account_lookup_switch_preserves_new_identity_and_recovers(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_account_id", "")

            def select_new_account():
                addon.setSetting("tmdb_account_id", "account-2")
                addon.setSetting("tmdb_access_token", "session-2")

            session.queue_response(
                switch_json_response({"id": "account-1"}, select_new_account)
            )

            with self.assertLogs("yawsp_app.router", level="ERROR"):
                result = production_router.router(
                    "action=tmdb_watchlist&category=movies&watched=42"
                )

            self.assertFalse(result.handled)
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(session.calls[0]["url"], "https://api.themoviedb.org/3/account")
            self.assertEqual(session.calls[0]["kwargs"]["params"]["session_id"], "session-1")
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-2")
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-2")
            self.assertEqual(recorder.builtin_commands, [])

            first_scope = SnapshotScope("account-1", "movies", "en-US")
            second_scope = SnapshotScope("account-2", "movies", "en-US")
            first = owner.snapshot_store.save_complete(
                first_scope, [{"id": 42, "title": "First"}], 100.0
            )
            owner.snapshot_store.save_complete(
                second_scope,
                [{"id": 42, "title": "Remove current"}, {"id": 2, "title": "Keep"}],
                100.0,
            )
            session.queue_response(json_response({"success": True}))
            session.queue_response(
                json_response(
                    {"total_pages": 1, "results": [{"id": 2, "title": "Keep"}]}
                )
            )
            session.queue_response(json_response({"genres": []}))

            recovered = production_router.router(
                "action=tmdb_watchlist&category=movies&watched=42"
            )

            self.assertTrue(recovered.handled)
            self.assertEqual(
                session.calls[1]["url"],
                "https://api.themoviedb.org/3/account/account-2/watchlist",
            )
            self.assertEqual(session.calls[1]["kwargs"]["params"]["session_id"], "session-2")
            self.assertEqual(owner.snapshot_store.load(first_scope), first)
            self.assertEqual(
                owner.snapshot_store.load(second_scope).results,
                [{"id": 2, "title": "Keep"}],
            )
            refreshes = [
                job
                for job in service_jobs(owner, "watchlist.refresh")
                if not job["interactive"]
            ]
            self.assertEqual(len(refreshes), 1)
            self.assertEqual(refreshes[0]["state"], "succeeded")
            self.assertTrue(refreshes[0]["request"]["force"])
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-2")
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-2")

    def test_production_account_resolution_never_publishes_speculative_identity(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_account_id", "")
            original_set = addon.setSetting
            publication_attempts = []
            response_returned = []
            account_response = json_response({"id": "account-1"})
            account_json = account_response.json

            def returned_account_json():
                data = account_json()
                response_returned.append(True)
                return data

            def switch_at_speculative_publication(key, value):
                if key == "tmdb_account_id" and value == "account-1":
                    publication_attempts.append(value)
                    if not response_returned:
                        original_set("tmdb_account_id", "account-2")
                        original_set("tmdb_access_token", "session-2")
                original_set(key, value)

            account_response.json = returned_account_json
            addon.setSetting = switch_at_speculative_publication
            session.queue_response(account_response)
            session.queue_response(
                json_response({"total_pages": 1, "results": [{"id": 1, "title": "Staged"}]})
            )
            session.queue_response(json_response({"genres": []}))

            result = production_router.router("action=tmdb_watchlist&category=movies")

            self.assertTrue(result.succeeded)
            self.assertEqual(response_returned, [True])
            self.assertEqual(publication_attempts, ["account-1"])
            self.assertEqual(addon.getSetting("tmdb_account_id"), "account-1")
            self.assertEqual(addon.getSetting("tmdb_access_token"), "session-1")
            self.assertEqual(
                session.calls[1]["url"],
                "https://api.themoviedb.org/3/account/account-1/watchlist/movies",
            )
            self.assertEqual(session.calls[1]["kwargs"]["params"]["session_id"], "session-1")
            self.assertEqual(
                owner.snapshot_store.load(
                    SnapshotScope("account-1", "movies", "en-US")
                ).results,
                [{"id": 1, "title": "Staged"}],
            )
            self.assertEqual(recorder.items[-2]["listitem"].label, "Staged")
            self.assertEqual(recorder.items[-1]["listitem"].label, "Refresh")

    def test_route_calls_share_one_cached_owner_until_close(self):
        owners = []

        def create_owner():
            owner = Owner()
            owners.append(owner)
            return owner

        with mock.patch.object(production_router, "_ProductionOwner", side_effect=create_owner, create=True):
            production_router.close()
            production_router.router("")
            production_router.router("action=tmdb_watchlist&category=movies")

            self.assertEqual(len(owners), 1)
            production_router.close()
            self.assertEqual(owners[0].close_calls, 1)


if __name__ == "__main__":
    unittest.main()
