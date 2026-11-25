import ast
import contextlib
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.support.http_stubs import FakeSession
from tests.support.kodi_stubs import AddonRecorder, KodiRecorder
from tests.test_entrypoint import install_kodi_stubs
from tests.test_runtime import RecordingLogger
from yawsp_app import download_jobs, router as production_router
from yawsp_app.download_jobs import DownloadJobKey, DownloadJobManager
from yawsp_app.runtime import ActionRunner, KodiRuntime
from yawsp_app.webshare_ui import build_webshare_handlers


ROOT = Path(__file__).resolve().parents[1]


class ListItem:
    def __init__(self, label="", path=""):
        self.label = label
        self.path = path
        self.art = {}
        self.commands = []

    def setArt(self, art):
        self.art.update(art)

    def addContextMenuItems(self, commands):
        self.commands.extend(commands)


class BackupDBRetirementTests(unittest.TestCase):
    def tearDown(self):
        production_router.close()

    def test_webshare_menu_has_only_live_static_routes(self):
        with tempfile.TemporaryDirectory() as profile:
            plugin = KodiRecorder()
            session = FakeSession()
            client = mock.Mock()
            runtime = KodiRuntime(
                plugin=plugin,
                addon=AddonRecorder(
                    strings={
                        30201: "Search",
                        30202: "Queue",
                        30203: "History",
                        30204: "Settings",
                    },
                    addon_info={"name": "YAWsP"},
                ),
                session=session,
                profile=profile,
                base_url="plugin://plugin.video.yawspreloaded/",
                handle=9,
                listitem_factory=ListItem,
                keyboard_factory=lambda default, heading: None,
                textviewer=lambda heading, text: None,
                logger=RecordingLogger(),
            )
            handlers = build_webshare_handlers(runtime, lambda: client)
            runner = ActionRunner(plugin, 9, ListItem, logger=RecordingLogger())

            result = runner.run_directory(lambda: handlers["menu"]({}))

            self.assertTrue(result.succeeded)
            self.assertEqual(
                [item["listitem"].label for item in plugin.items],
                ["Search", "Queue", "History", "Settings"],
            )
            self.assertEqual(
                [
                    parse_qs(urlsplit(item["url"]).query)["action"][-1]
                    for item in plugin.items
                ],
                ["search", "queue", "history", "settings"],
            )
            self.assertNotIn("db", handlers)
            self.assertNotIn("searchdb", handlers)
            self.assertEqual(len(plugin.end_of_directory_calls), 1)
            self.assertEqual(plugin.resolved_url_calls, [])
            self.assertEqual(client.mock_calls, [])
            self.assertEqual(session.calls, [])

    @contextlib.contextmanager
    def production_owner(self, profile):
        with install_kodi_stubs(), mock.patch.object(
            sys,
            "argv",
            ["plugin://plugin.video.yawspreloaded/", "17", ""],
        ), mock.patch.object(
            production_router, "BackgroundPrefetch"
        ), mock.patch.object(
            production_router.requests, "Session"
        ) as session_type, mock.patch.object(
            production_router, "WebshareClient"
        ) as webshare_type, mock.patch.object(
            production_router, "TMDBClient"
        ) as tmdb_type:
            session = FakeSession()
            session.headers = {}
            session_type.return_value = session
            webshare = webshare_type.return_value
            tmdb = tmdb_type.return_value
            addon = sys.modules["xbmcaddon"].Addon()
            addon.strings.update(
                {
                    30201: "Search",
                    30202: "Queue",
                    30203: "History",
                    30204: "Settings",
                    30418: "TMDB Watchlist",
                }
            )
            translate = lambda value: profile
            sys.modules["xbmc"].translatePath = translate
            sys.modules["xbmcvfs"].translatePath = translate
            production_router.close()
            owner = production_router._get_owner()
            try:
                yield owner, session, webshare, tmdb, addon, sys.modules[
                    "xbmcplugin"
                ]._recorder
            finally:
                production_router.close()

    @staticmethod
    def invoke_root(query, recorder):
        first_item = len(recorder.items)
        first_completion = len(recorder.end_of_directory_calls)
        result = production_router.router(query)
        items = recorder.items[first_item:]
        return result, items, recorder.end_of_directory_calls[first_completion:]

    def test_retired_bookmarks_match_root_fallback_without_auth_network_or_user_data_io(self):
        queries = (
            "action=db",
            "action=db&file=A.txt&key=movie-1&search=Film&toqueue=harmless",
            "action=searchdb&query=",
            "action=searchdb&query=Film&search=keep&toqueue=harmless",
        )
        with tempfile.TemporaryDirectory() as profile:
            user_files = (
                Path(profile, "database.txt"),
                Path(profile, "database.json"),
                Path(profile, "db", "A.txt"),
            )
            for path in user_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic user data must remain untouched")
            original_hashes = {
                path: hashlib.sha256(path.read_bytes()).digest() for path in user_files
            }

            with self.production_owner(profile) as (
                owner,
                session,
                webshare,
                tmdb,
                addon,
                recorder,
            ):
                forbidden = AssertionError("retired route attempted user-data I/O")
                with mock.patch("builtins.open", side_effect=forbidden) as file_open, mock.patch(
                    "io.open", side_effect=forbidden
                ) as io_open, mock.patch(
                    "os.listdir", side_effect=forbidden
                ) as listdir, mock.patch(
                    "os.scandir", side_effect=forbidden
                ) as scandir, mock.patch(
                    "os.path.exists", side_effect=forbidden
                ) as exists, mock.patch(
                    "os.path.isfile", side_effect=forbidden
                ) as isfile, mock.patch(
                    "os.path.isdir", side_effect=forbidden
                ) as isdir, mock.patch(
                    "os.path.islink", side_effect=forbidden
                ) as islink, mock.patch(
                    "os.path.lexists", side_effect=forbidden
                ) as lexists:
                    baseline_result, baseline_items, _ = self.invoke_root(
                        "action=not_migrated&file=A.txt", recorder
                    )
                    baseline_labels = [item["listitem"].label for item in baseline_items]
                    baseline_urls = [item["url"] for item in baseline_items]
                    self.assertTrue(baseline_result.succeeded)

                    for query in queries:
                        with self.subTest(query=query):
                            result, items, completions = self.invoke_root(query, recorder)
                            self.assertTrue(result.succeeded)
                            self.assertEqual(
                                [item["listitem"].label for item in items],
                                ["Search", "Queue", "History", "Settings", "TMDB Watchlist"],
                            )
                            self.assertEqual(
                                [item["listitem"].label for item in items],
                                baseline_labels,
                            )
                            self.assertEqual([item["url"] for item in items], baseline_urls)
                            self.assertEqual(len(completions), 1)
                            self.assertTrue(completions[0]["succeeded"])

                    for boundary in (
                        file_open,
                        io_open,
                        listdir,
                        scandir,
                        exists,
                        isfile,
                        isdir,
                        islink,
                        lexists,
                    ):
                        self.assertEqual(boundary.call_count, 0)

                self.assertEqual(webshare.method_calls, [])
                self.assertEqual(tmdb.method_calls, [])
                self.assertEqual(session.calls, [])
                self.assertFalse(
                    [
                        call
                        for call in addon.calls
                        if call[0] == "getSetting"
                        and call[1]
                        in ("token", "duuid", "tmdb_access_token", "tmdb_account_id")
                    ]
                )
                self.assertEqual(recorder.resolved_url_calls, [])
                self.assertEqual(recorder.builtin_commands, [])
                self.assertEqual(recorder.notifications, [])

            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).digest() for path in user_files},
                original_hashes,
            )

    def test_job_keys_are_media_only_and_legacy_family_cannot_reserve_work(self):
        sessions = []
        manager = DownloadJobManager(session_factory=lambda: sessions.append(object()))
        self.addCleanup(manager.close, wait=False)

        self.assertEqual(DownloadJobKey("media", "movie.mkv").kind, "media")
        with self.assertRaises(ValueError):
            DownloadJobKey("backup_db", "profile")
        self.assertFalse(hasattr(download_jobs, "backup_db_job_key"))
        self.assertEqual(manager._active, {})
        self.assertEqual(len(manager._pending), 0)
        self.assertEqual(sessions, [])

    def test_production_source_has_no_retired_module_symbols_or_handlers(self):
        self.assertFalse((ROOT / "yawsp_app" / "backup_db.py").exists())
        retired_definitions = {
            "BackupDBStore",
            "backup_db_job_key",
            "_BackupDatabaseOperation",
            "database_status_item",
            "ensure_database_available",
            "database_listing",
            "db",
            "searchdb",
        }
        for path in (ROOT / "yawsp_app").glob("*.py"):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.assertNotIn(node.name, retired_definitions)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        self.assertNotEqual(node.module, "yawsp_app.backup_db")
                    elif isinstance(node, ast.Import):
                        self.assertNotIn(
                            "yawsp_app.backup_db", [alias.name for alias in node.names]
                        )
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertNotIn("Backup DB", node.value)


if __name__ == "__main__":
    unittest.main()
