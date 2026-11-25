import contextlib
import datetime
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.support.kodi_stubs import AddonRecorder, KodiRecorder
from tests.test_runtime import RecordingLogger
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.runtime import ActionRunner, KodiRuntime
from yawsp_app.tmdb import (
    BackgroundPrefetch,
    ShowRepository,
    TMDBClient,
    TMDBError,
    TrailerRepository,
)
from yawsp_app.tmdb_ui import _next_episode_suffix, build_tmdb_handlers


class ListItem:
    def __init__(self, label="", path=""):
        self.label = label
        self.path = path
        self.art = {}
        self.info = []
        self.commands = []
        self.properties = {}

    def setArt(self, art):
        self.art.update(art)

    def setInfo(self, kind, info):
        self.info.append((kind, info))

    def addContextMenuItems(self, commands):
        self.commands.extend(commands)

    def setProperty(self, key, value):
        self.properties[key] = value


class Addon(AddonRecorder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.open_settings_calls = 0

    def openSettings(self):
        self.open_settings_calls += 1


class Plugin(KodiRecorder):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def addDirectoryItem(self, *args, **kwargs):
        self.events.append("add")
        return super().addDirectoryItem(*args, **kwargs)

    def endOfDirectory(self, *args, **kwargs):
        self.events.append("end")
        return super().endOfDirectory(*args, **kwargs)

    def setResolvedUrl(self, *args, **kwargs):
        self.events.append("resolve")
        return super().setResolvedUrl(*args, **kwargs)

    def executeBuiltin(self, command):
        self.events.append(command)
        return super().executeBuiltin(command)


class Response:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code
        self.closed = False

    def json(self):
        if isinstance(self.data, BaseException):
            raise self.data
        return self.data

    def close(self):
        self.closed = True


class Client:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.responses = {}
        self.post_results = []
        self.account_id = "account-1"

    def queue_get(self, path, *responses):
        self.responses.setdefault(path, []).extend(responses)

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        responses = self.responses.get(path)
        if not responses:
            raise AssertionError("unexpected TMDB GET " + path)
        response = responses.pop(0)
        if callable(response):
            response = response()
        if isinstance(response, BaseException):
            raise response
        return response

    def get_account_id(self):
        return self.account_id

    def request(
        self, method, path, params=None, json_data=None, invalidate_on_unauthorized=True
    ):
        self.calls.append((method, path, params, json_data))
        if not self.post_results:
            raise AssertionError("unexpected TMDB request " + path)
        result = self.post_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if result.status_code >= 400:
            http_error = requests.HTTPError("pending", response=result)
            raise TMDBError("authentication is pending") from http_error
        return result

    def invalidate_identity(self):
        self.settings.setSetting("tmdb_account_id", "")


class ServiceSettings:
    def __init__(self, addon):
        self.addon = addon

    def get(self, key):
        return self.addon.getSetting(key)

    def set(self, key, value):
        self.addon.setSetting(key, value)


class DeferredWorker:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        self.jobs.append((function, args))


class Background:
    def __init__(self, events):
        self.events = events
        self.trailers = []
        self.shows = []
        self.trailer_missing_known = []
        self.show_missing_known = []

    def submit_trailers(self, media, media_ids, missing_known=False):
        ids = list(media_ids)
        self.events.append("trailers")
        self.trailers.append((media, ids))
        self.trailer_missing_known.append(missing_known)
        return len(ids)

    def submit_shows(self, show_ids, missing_known=False):
        ids = list(show_ids)
        self.events.append("shows")
        self.shows.append(ids)
        self.show_missing_known.append(missing_known)
        return len(ids)


class UILogger(RecordingLogger):
    def info(self, *args, **kwargs):
        pass


class Clock:
    def __init__(self):
        self.value = 1_700_000_000.0

    def __call__(self):
        return self.value


class Monitor:
    def __init__(self, clock, abort=False):
        self.clock = clock
        self.abort = abort
        self.waits = []

    def waitForAbort(self, seconds):
        self.waits.append(seconds)
        self.clock.value += seconds
        return self.abort

    def abortRequested(self):
        return self.abort


def query(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


def command_url(command, prefix):
    assert command.startswith(prefix) and command.endswith(")")
    return command[len(prefix) : -1]


class TMDBUITests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.events = []
        self.plugin = Plugin(self.events)
        self.addon = Addon(
            settings={
                "tmdb_api_key": "api-key",
                "tmdb_access_token": "session-token",
                "tmdb_account_id": "account-1",
                "tmdb_sort": "0",
                "tmdb_hide_unreleased": "false",
            },
            addon_info={"name": "YAWsP", "id": "plugin.video.yawspreloaded"},
        )
        self.ui_logger = UILogger()
        self.runtime = KodiRuntime(
            plugin=self.plugin,
            addon=self.addon,
            session=object(),
            profile=self.temporary_directory.name,
            base_url="plugin://plugin.video.yawspreloaded/",
            handle=9,
            listitem_factory=ListItem,
            keyboard_factory=lambda *args: None,
            textviewer=lambda *args: None,
            logger=self.ui_logger,
        )
        self.runtime.today = lambda: datetime.date(2026, 11, 25)
        self.runtime.addon_available = lambda addon_id: True
        self.clock = Clock()
        self.runtime.clock = self.clock
        self.window_ids = [12007]
        self.runtime.current_window_id = lambda: self.window_ids[-1]
        self.monitor = Monitor(self.clock)
        self.runtime.monitor_factory = lambda: self.monitor
        self.runtime.qr_generator = lambda url: os.path.join(
            self.temporary_directory.name, "qr.png"
        )
        self.client = Client(self.addon)
        self.trailer_cache = AtomicJsonCache(
            os.path.join(self.temporary_directory.name, "trailers.json")
        )
        self.show_cache = AtomicJsonCache(
            os.path.join(self.temporary_directory.name, "shows.json")
        )
        self.background = Background(self.events)
        self.handlers = self.build_handlers()
        self.runner = ActionRunner(
            self.plugin, 9, ListItem, logger=RecordingLogger()
        )

    def build_handlers(self):
        repositories = {
            "trailers": TrailerRepository(
                self.client, self.trailer_cache, clock=self.clock
            ),
            "shows": ShowRepository(self.client, self.show_cache, clock=self.clock),
        }
        return build_tmdb_handlers(
            self.runtime, lambda: self.client, repositories, self.background
        )

    def queue_watchlist(self, results, total_pages=1):
        self.client.queue_get(
            "/3/account/account-1/watchlist/movies",
            {"results": results, "total_pages": total_pages},
        )
        self.client.queue_get("/3/genre/movie/list", {"genres": []})

    def test_watchlist_uses_cached_values_and_never_waits_for_item_lookups(self):
        now = self.clock()
        self.trailer_cache.save(
            {"movie:1": {"ts": now, "youtube_id": "cached-video"}}
        )
        self.queue_watchlist(
            [{"id": 1, "title": "Film", "release_date": "2026-01-02"}]
        )
        self.handlers = self.build_handlers()

        result = self.handlers["tmdb_watchlist"](
            {"action": "tmdb_watchlist", "category": "movies"}
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(
            [call[1] for call in self.client.calls],
            ["/3/account/account-1/watchlist/movies", "/3/genre/movie/list"],
        )
        self.assertEqual(self.background.trailers, [])
        commands = self.plugin.items[0]["listitem"].commands
        trailer_action = next(
            command for _, command in commands if "action=tmdb_trailer" in command
        )
        self.assertTrue(trailer_action.startswith("RunPlugin("))
        self.assertEqual(query(command_url(trailer_action, "RunPlugin("))["id"], "1")
        self.assertFalse(any("PlayMedia" in command for _, command in commands))
        self.assertLess(self.events.index("add"), self.events.index("trailers") if "trailers" in self.events else len(self.events))
        self.assertEqual(self.plugin.builtin_commands, [])

    def test_cached_watchlist_trailer_checks_youtube_without_direct_playmedia(self):
        self.trailer_cache.save(
            {"movie:42": {"ts": self.clock(), "youtube_id": "cached-video"}}
        )
        movie = {"id": 42, "title": "Film", "release_date": "2026-01-02"}
        self.queue_watchlist([movie])
        self.handlers = self.build_handlers()
        self.handlers["tmdb_watchlist"](
            {"action": "tmdb_watchlist", "category": "movies"}
        )
        trailer_action = next(
            command
            for _, command in self.plugin.items[0]["listitem"].commands
            if "action=tmdb_trailer" in command
        )
        params = query(command_url(trailer_action, "RunPlugin("))
        calls_before = list(self.client.calls)
        self.events.clear()
        self.plugin.builtin_commands.clear()

        self.runtime.addon_available = lambda addon_id: False
        result = self.runner.run_action(
            lambda: self.handlers["tmdb_trailer"](params)
        )

        self.assertFalse(result.handled)
        self.assertEqual(self.client.calls, calls_before)
        self.assertEqual(self.events, ["resolve"])
        self.assertEqual(self.plugin.builtin_commands, [])
        self.assertFalse(any("PlayMedia" in command for command in self.plugin.builtin_commands))

    def test_uncached_trailer_is_actionable_and_background_data_appears_on_later_open(self):
        movie = {"id": 2, "title": "Film", "release_date": "2026-01-02"}
        self.queue_watchlist([movie])

        self.runner.run_directory(
            lambda: self.handlers["tmdb_watchlist"](
                {"action": "tmdb_watchlist", "category": "movies"}
            )
        )

        self.assertEqual(self.background.trailers, [("movie", [2])])
        action = next(
            command
            for _, command in self.plugin.items[0]["listitem"].commands
            if "action=tmdb_trailer" in command
        )
        self.assertEqual(query(command_url(action, "RunPlugin("))["id"], "2")
        self.assertLess(self.events.index("add"), self.events.index("trailers"))
        self.assertEqual(self.plugin.builtin_commands, [])

        self.trailer_cache.save(
            {"movie:2": {"ts": self.clock(), "youtube_id": "later-video"}}
        )
        self.plugin.items.clear()
        self.queue_watchlist([movie])
        self.handlers = self.build_handlers()
        self.handlers["tmdb_watchlist"](
            {"action": "tmdb_watchlist", "category": "movies"}
        )
        later_action = next(
            command
            for _, command in self.plugin.items[0]["listitem"].commands
            if "action=tmdb_trailer" in command
        )
        self.assertEqual(query(command_url(later_action, "RunPlugin("))["id"], "2")

    def test_uncached_show_data_is_not_fetched_inline_and_later_cache_changes_label(self):
        show = {"id": 3, "name": "Seriál", "first_air_date": "2025-01-01"}
        self.client.queue_get(
            "/3/account/account-1/watchlist/tv",
            {"results": [show], "total_pages": 1},
        )
        self.client.queue_get("/3/genre/tv/list", {"genres": []})

        self.runner.run_directory(
            lambda: self.handlers["tmdb_watchlist"](
                {"action": "tmdb_watchlist", "category": "shows"}
            )
        )

        self.assertEqual(self.background.shows, [[3]])
        self.assertNotIn("S02E04", self.plugin.items[0]["listitem"].label)
        self.show_cache.save(
            {
                "3": {
                    "ts": self.clock(),
                    "next": {"season": 2, "episode": 4, "air_date": "2026-12-01"},
                }
            }
        )
        self.plugin.items.clear()
        self.client.queue_get(
            "/3/account/account-1/watchlist/tv",
            {"results": [show], "total_pages": 1},
        )
        self.client.queue_get("/3/genre/tv/list", {"genres": []})
        self.handlers = self.build_handlers()

        self.handlers["tmdb_watchlist"](
            {"action": "tmdb_watchlist", "category": "shows"}
        )
        self.assertIn("S02E04 (1. 12. 2026)", self.plugin.items[0]["listitem"].label)

    def test_malformed_air_date_does_not_break_next_episode_presentation(self):
        for air_date in (20261201, {"year": 2026}):
            with self.subTest(air_date=air_date):
                self.assertEqual(
                    _next_episode_suffix(
                        {"season": 2, "episode": 4, "air_date": air_date}
                    ),
                    " [COLOR gray]→ S02E04[/COLOR]",
                )

    def test_submissions_and_view_hook_share_one_post_completion_callback(self):
        self.runtime.skin_dir = lambda: "skin.estuary"
        show = {"id": 3, "name": "Seriál", "first_air_date": "2025-01-01"}
        self.client.queue_get(
            "/3/account/account-1/watchlist/tv",
            {"results": [show], "total_pages": 1},
        )
        self.client.queue_get("/3/genre/tv/list", {"genres": []})

        result = self.runner.run_directory(
            lambda: self.handlers["tmdb_watchlist"](
                {"action": "tmdb_watchlist", "category": "shows"}
            )
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(
            self.events,
            [
                "add",
                "end",
                "Container.SetViewMode(54)",
                "trailers",
                "shows",
            ],
        )
        self.assertEqual(self.events.count("end"), 1)
        self.assertEqual(self.background.trailers, [("tv", [3])])
        self.assertEqual(self.background.shows, [[3]])
        self.assertEqual(self.background.trailer_missing_known, [True])
        self.assertEqual(self.background.show_missing_known, [True])

    def test_watchlist_reuses_single_discovery_read_per_cache_for_submission(self):
        show = {"id": 3, "name": "Seriál", "first_air_date": "2025-01-01"}
        self.client.queue_get(
            "/3/account/account-1/watchlist/tv",
            {"results": [show], "total_pages": 1},
        )
        self.client.queue_get("/3/genre/tv/list", {"genres": []})

        self.background = BackgroundPrefetch(
            settings=ServiceSettings(self.addon),
            trailer_cache=self.trailer_cache,
            show_cache=self.show_cache,
            locale_mapper=lambda: "en-US",
            session_factory=lambda: self.fail("worker must not start during submission"),
            worker=DeferredWorker(),
            clock=self.clock,
        )
        self.handlers = self.build_handlers()

        with patch.object(
            self.trailer_cache, "load", wraps=self.trailer_cache.load
        ) as trailer_load, patch.object(
            self.show_cache, "load", wraps=self.show_cache.load
        ) as show_load:
            self.runner.run_directory(
                lambda: self.handlers["tmdb_watchlist"](
                    {"action": "tmdb_watchlist", "category": "shows"}
                )
            )

        self.assertEqual(trailer_load.call_count, 1)
        self.assertEqual(show_load.call_count, 1)

    def test_stale_show_value_is_displayed_while_still_scheduled_for_refresh(self):
        stale = {"season": 1, "episode": 2, "air_date": "2026-01-01"}
        self.show_cache.save(
            {
                "3": {
                    "ts": self.clock() - 24 * 60 * 60 - 1,
                    "next": stale,
                }
            }
        )
        show = {"id": 3, "name": "Seriál", "first_air_date": "2025-01-01"}
        self.client.queue_get(
            "/3/account/account-1/watchlist/tv",
            {"results": [show], "total_pages": 1},
        )
        self.client.queue_get("/3/genre/tv/list", {"genres": []})

        self.runner.run_directory(
            lambda: self.handlers["tmdb_watchlist"](
                {"action": "tmdb_watchlist", "category": "shows"}
            )
        )

        self.assertIn("S01E02", self.plugin.items[0]["listitem"].label)
        self.assertEqual(self.background.shows, [[3]])

    def test_estuary_infowall_default_runs_once_after_directory_completion(self):
        self.runtime.skin_dir = lambda: "skin.estuary"
        for _ in range(2):
            self.queue_watchlist([])
            result = self.runner.run_directory(
                lambda: self.handlers["tmdb_watchlist"](
                    {"action": "tmdb_watchlist", "category": "movies"}
                )
            )
            self.assertTrue(result.succeeded)

        view_commands = [
            command
            for command in self.plugin.builtin_commands
            if command == "Container.SetViewMode(54)"
        ]
        self.assertEqual(view_commands, ["Container.SetViewMode(54)"])
        self.assertEqual(self.addon.getSetting("tmdb_view_defaulted"), "true")
        self.assertLess(self.events.index("end"), self.events.index(view_commands[0]))

    def test_invalid_session_recovers_to_connection_entry_without_reusing_account(self):
        path = "/3/account/account-1/watchlist/movies"

        def invalidate():
            self.addon.setSetting("tmdb_access_token", "")
            self.addon.setSetting("tmdb_account_id", "")
            raise TMDBError("session expired")

        self.client.queue_get(path, invalidate)
        self.client.queue_get("/3/genre/movie/list", {"genres": []})

        result = self.handlers["tmdb_watchlist"](
            {"action": "tmdb_watchlist", "category": "movies"}
        )

        self.assertFalse(result.succeeded)
        self.assertEqual(self.addon.getSetting("tmdb_access_token"), "")
        self.assertEqual(self.addon.getSetting("tmdb_account_id"), "")
        self.assertEqual(self.addon.open_settings_calls, 1)
        self.handlers["tmdb_watchlist"]({"action": "tmdb_watchlist", "category": "movies"})
        self.assertEqual(self.addon.open_settings_calls, 2)
        self.assertEqual(len(self.client.calls), 1)

    def test_successful_watched_post_closes_response_then_resolves_before_refresh(self):
        response = Response({"success": True}, 201)
        self.client.post_results = [response]

        result = self.runner.run_action(
            lambda: self.handlers["tmdb_watchlist"](
                {"action": "tmdb_watchlist", "category": "movies", "watched": "42"}
            )
        )

        self.assertTrue(result.handled)
        self.assertTrue(result.refresh)
        self.assertTrue(response.closed)
        method, path, _, payload = self.client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/3/account/account-1/watchlist")
        self.assertEqual(
            payload, {"media_type": "movie", "media_id": 42, "watchlist": False}
        )
        self.assertEqual(self.events, ["resolve", "Container.Refresh"])

    def test_false_watched_result_and_api_error_resolve_without_refresh(self):
        for result_value in (Response({"success": False}), TMDBError("write failed")):
            with self.subTest(result=result_value):
                self.client.post_results = [result_value]
                self.events.clear()
                self.plugin.builtin_commands.clear()

                result = self.runner.run_action(
                    lambda: self.handlers["tmdb_watchlist"](
                        {"action": "tmdb_watchlist", "category": "shows", "watched": "7"}
                    )
                )

                self.assertFalse(result.handled)
                self.assertEqual(self.events, ["resolve"])
                self.assertEqual(self.plugin.builtin_commands, [])
                if isinstance(result_value, Response):
                    self.assertTrue(result_value.closed)

    def test_zero_watched_id_remains_an_action_not_a_directory_result(self):
        self.client.post_results = [Response({"success": False})]

        result = self.runner.run_action(
            lambda: self.handlers["tmdb_watchlist"](
                {"action": "tmdb_watchlist", "category": "movies", "watched": "0"}
            )
        )

        self.assertFalse(result.handled)
        self.assertEqual(self.client.calls[0][0], "POST")
        self.assertEqual(self.events, ["resolve"])

    def test_czech_collation_and_unreleased_movie_filter_are_deterministic(self):
        self.addon.setSetting("tmdb_hide_unreleased", "true")
        movies = [
            {"id": 1, "title": "Chata", "release_date": "2026-01-01"},
            {"id": 2, "title": "Hora", "release_date": "2026-01-01"},
            {"id": 3, "title": "Budoucí", "release_date": "2026-12-01"},
            {"id": 4, "title": "Neurčené", "release_date": ""},
        ]
        self.queue_watchlist(movies)

        self.handlers["tmdb_watchlist"](
            {"action": "tmdb_watchlist", "category": "movies"}
        )

        self.assertEqual(
            [item["listitem"].label.split(" (")[0] for item in self.plugin.items],
            ["Hora", "Chata", "Neurčené"],
        )

    def test_season_and_episode_directories_set_exact_content_and_episode_queries(self):
        self.client.queue_get(
            "/3/tv/8",
            {
                "name": "Seriál",
                "original_name": "Original",
                "seasons": [
                    {"season_number": 2, "episode_count": 3, "poster_path": "/s.jpg"}
                ],
            },
        )
        result = self.handlers["tmdb_watchlist"](
            {"action": "tmdb_watchlist", "category": "shows", "show_id": "8"}
        )
        self.assertTrue(result.succeeded)
        self.assertIn(
            ("setContent", (9, "seasons")),
            [(call["method"], call["args"]) for call in self.plugin.calls],
        )
        season_query = query(self.plugin.items[-1]["url"])
        self.assertEqual(season_query["season"], "2")
        self.assertEqual(season_query["series_title"], "Original")

        self.client.queue_get(
            "/3/tv/8/season/2",
            {
                "episodes": [
                    {
                        "episode_number": 4,
                        "name": "Díl",
                        "air_date": "2026-11-20",
                        "overview": "Plot",
                        "vote_average": 8.5,
                        "runtime": 50,
                    }
                ]
            },
        )
        params = query(self.plugin.items[-1]["url"])
        result = self.handlers["tmdb_watchlist"](params)
        self.assertTrue(result.succeeded)
        self.assertIn(
            ("setContent", (9, "episodes")),
            [(call["method"], call["args"]) for call in self.plugin.calls],
        )
        episode_query = query(self.plugin.items[-1]["url"])
        self.assertEqual(episode_query["action"], "search")
        self.assertEqual(episode_query["what"], "Original S02E04")
        info = self.plugin.items[-1]["listitem"].info[0][1]
        self.assertEqual(info["duration"], 3000)
        self.assertEqual(info["aired"], "2026-11-20")

    def authenticate_with_real_client(self, *responses):
        token_response = FakeResponse(json_data={"request_token": "request-token"})
        self.http_session = FakeSession([token_response] + list(responses))
        self.client = TMDBClient(
            self.http_session,
            ServiceSettings(self.addon),
            locale_mapper=lambda: "en-US",
            clock=self.clock,
            sleeper=lambda seconds: None,
            logger=self.ui_logger,
        )
        self.handlers = self.build_handlers()
        return self.runner.run_action(
            lambda: self.handlers["tmdb_auth"]({"action": "tmdb_auth"})
        )

    def authenticate(self, **params):
        self.client.queue_get(
            "/3/authentication/token/new", {"request_token": "request-token"}
        )
        return self.runner.run_action(
            lambda: self.handlers["tmdb_auth"]({"action": "tmdb_auth", **params})
        )

    def test_qr_success_resets_account_before_saving_session_and_closes_active_viewer(self):
        self.client.post_results = [Response({"session_id": "new-session"})]
        self.addon.setSetting("tmdb_account_id", "old-account")

        result = self.authenticate()

        self.assertTrue(result.handled)
        self.assertEqual(self.addon.getSetting("tmdb_access_token"), "new-session")
        self.assertEqual(self.addon.getSetting("tmdb_account_id"), "")
        account_reset = self.addon.calls.index(("setSetting", "tmdb_account_id", ""))
        session_save = self.addon.calls.index(("setSetting", "tmdb_access_token", "new-session"))
        self.assertLess(account_reset, session_save)
        self.assertIn("Action(Back)", self.plugin.builtin_commands)
        self.assertNotIn("Container.Update", self.plugin.builtin_commands)
        self.assertTrue(self.client.post_results == [])
        self.assertEqual(self.events.count("resolve"), 1)
        self.assertLess(self.events.index("Action(Back)"), self.events.index("resolve"))

    def test_qr_timeout_and_abort_close_viewer_once_in_finally(self):
        for abort in (False, True):
            with self.subTest(abort=abort):
                self.setUp()
                self.monitor.abort = abort
                self.client.post_results = [Response({"success": False}, 401)] * 100

                result = self.authenticate()

                self.assertFalse(result.handled)
                self.assertEqual(self.plugin.builtin_commands.count("Action(Back)"), 1)
                self.assertNotIn("Container.Refresh", self.plugin.builtin_commands)

    def test_qr_user_close_does_not_navigate_the_underlying_container(self):
        self.client.post_results = [Response({"success": False}, 401)]
        calls = []

        def window():
            calls.append(1)
            if len(calls) == 1:
                return 12007
            self.window_ids[-1] = 10000
            return 10000

        self.runtime.current_window_id = window

        result = self.authenticate()

        self.assertFalse(result.handled)
        self.assertNotIn("Action(Back)", self.plugin.builtin_commands)
        self.assertNotIn("Action(Back)", [event for event in self.events])
        self.assertEqual(len(self.plugin.notifications), 1)
        self.assertEqual(self.ui_logger.calls, [])

    def test_real_client_pending_401_then_abort_preserves_existing_identity(self):
        pending = FakeResponse(json_data={"success": False}, status_code=401)
        self.monitor.abort = True

        result = self.authenticate_with_real_client(pending)

        self.assertFalse(result.handled)
        self.assertTrue(pending.closed)
        self.assertEqual(self.addon.getSetting("tmdb_access_token"), "session-token")
        self.assertEqual(self.addon.getSetting("tmdb_account_id"), "account-1")

    def test_real_client_pending_401_then_user_close_preserves_existing_identity(self):
        pending = FakeResponse(json_data={"success": False}, status_code=401)
        window_calls = []

        def window():
            window_calls.append(1)
            return 12007 if len(window_calls) == 1 else 10000

        self.runtime.current_window_id = window
        result = self.authenticate_with_real_client(pending)

        self.assertFalse(result.handled)
        self.assertTrue(pending.closed)
        self.assertEqual(self.addon.getSetting("tmdb_access_token"), "session-token")
        self.assertEqual(self.addon.getSetting("tmdb_account_id"), "account-1")

    def test_real_client_pending_401_then_success_replaces_identity_only_on_success(self):
        pending = FakeResponse(json_data={"success": False}, status_code=401)
        success = FakeResponse(json_data={"session_id": "new-session"})

        result = self.authenticate_with_real_client(pending, success)

        self.assertTrue(result.handled)
        self.assertTrue(pending.closed)
        self.assertTrue(success.closed)
        self.assertEqual(self.addon.getSetting("tmdb_access_token"), "new-session")
        self.assertEqual(self.addon.getSetting("tmdb_account_id"), "")
        setting_writes = [call for call in self.addon.calls if call[0] == "setSetting"]
        self.assertEqual(
            setting_writes,
            [
                ("setSetting", "tmdb_account_id", ""),
                ("setSetting", "tmdb_access_token", "new-session"),
            ],
        )

    def test_qr_polling_exception_is_handled_and_still_closes_active_viewer(self):
        self.client.post_results = [RuntimeError("poll failed")]

        result = self.authenticate()

        self.assertFalse(result.handled)
        self.assertEqual(self.plugin.builtin_commands.count("Action(Back)"), 1)
        self.assertEqual(self.events[-2:], ["Action(Back)", "resolve"])
        self.assertEqual(len(self.plugin.notifications), 2)

    def trailer(self, available=True):
        self.runtime.addon_available = lambda addon_id: available
        return self.runner.run_action(
            lambda: self.handlers["tmdb_trailer"](
                {"action": "tmdb_trailer", "media": "movie", "id": "42"}
            )
        )

    def test_on_demand_trailer_hit_resolves_false_before_youtube_playmedia(self):
        self.client.queue_get(
            "/3/movie/42/videos",
            {"results": [{"site": "YouTube", "type": "Trailer", "key": "video-hit"}]},
        )

        result = self.trailer()

        self.assertTrue(result.handled)
        self.assertEqual(
            self.events,
            ["resolve", "PlayMedia(plugin://plugin.video.youtube/play/?video_id=video-hit)"],
        )
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertFalse(self.plugin.resolved_url_calls[0]["succeeded"])

    def test_on_demand_trailer_missing_and_error_never_play_media(self):
        for videos, expected in (({"results": []}, "missing"), (TMDBError("offline"), "error")):
            with self.subTest(expected=expected):
                self.setUp()
                self.client.queue_get("/3/movie/42/videos", videos)
                self.handlers = self.build_handlers()

                log_context = (
                    self.assertLogs("yawsp_app.tmdb", level="ERROR")
                    if expected == "error"
                    else contextlib.nullcontext()
                )
                with log_context:
                    result = self.trailer()

                self.assertFalse(result.handled)
                self.assertEqual(self.events, ["resolve"])
                self.assertEqual(self.plugin.builtin_commands, [])

    def test_youtube_unavailable_never_issues_playmedia(self):
        self.trailer_cache.save(
            {"movie:42": {"ts": self.clock(), "youtube_id": "video-hit"}}
        )
        self.handlers = self.build_handlers()

        result = self.trailer(available=False)

        self.assertFalse(result.handled)
        self.assertEqual(self.events, ["resolve"])
        self.assertEqual(self.plugin.builtin_commands, [])


if __name__ == "__main__":
    unittest.main()
