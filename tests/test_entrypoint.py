import importlib
import sys
import types
import unittest
from unittest import mock

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.support.kodi_stubs import AddonRecorder, KodiRecorder
from tests.support.vfs_stubs import RecordingVFS
from tests.test_runtime import RecordingLogger
from tests.test_webshare_ui import FakeClient, xml
from yawsp_app import router as production_router
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
            "db": directory,
            "searchdb": directory,
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
    addon = AddonRecorder(addon_info={"name": "YAWsP", "profile": "special://profile/"})
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

    def test_real_kodi_adapters_share_one_default_executor_and_map_language(self):
        with install_kodi_stubs(), mock.patch.object(
            sys, "argv", ["plugin://plugin.video.yawspreloaded/", "17", "?action=settings"]
        ), mock.patch.object(production_router, "BackgroundPrefetch") as background_type:
            background = background_type.return_value
            production_router.close()
            owner = production_router._get_owner()

            self.assertIs(owner, production_router._get_owner())
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
            ), mock.patch.object(
                production_router,
                "build_tmdb_handlers",
                return_value={
                    "menu": lambda params: menu_calls.append("tmdb") or DirectoryResult()
                },
            ):
                production_router.router("")
            self.assertEqual(menu_calls, ["webshare", "tmdb"])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)

            self.assertEqual(background_type.call_count, 1)
            production_router.close()
            background.close.assert_called_once_with(wait=False)

    def test_production_watched_without_api_key_is_one_clean_action_completion(self):
        with install_kodi_stubs(), mock.patch.object(
            sys, "argv", ["plugin://plugin.video.yawspreloaded/", "17", "?action=tmdb_watchlist"]
        ), mock.patch.object(production_router, "BackgroundPrefetch"):
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
        ), mock.patch.object(production_router, "BackgroundPrefetch"), mock.patch.object(
            production_router.requests, "Session"
        ) as session_type, mock.patch.object(production_router, "WebshareClient") as client_type:
            session = FakeSession([response])
            session.headers = {}
            session_type.return_value = session
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

            result = production_router.router("action=download&ident=abc")

            return result, session, client, sys.modules["xbmcplugin"]._recorder

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

                self.assertFalse(result.handled)
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
        ), mock.patch.object(production_router, "BackgroundPrefetch"):
            production_router.close()
            owner = production_router._get_owner()

            def webshare_handlers(runtime, client_factory):
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
