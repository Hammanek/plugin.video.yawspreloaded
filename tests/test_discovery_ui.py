import math
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from yawsp_app.discovery_ui import CATALOG_SOURCES, build_discovery_handlers
from yawsp_app.runtime import ActionRunner
from yawsp_app.tmdb import TMDBError


SOURCES = (
    ("popular_movies", "/movie/popular", "movie", "movies"),
    ("popular_shows", "/tv/popular", "tv", "tvshows"),
    ("top_rated_movies", "/movie/top_rated", "movie", "movies"),
    ("top_rated_shows", "/tv/top_rated", "tv", "tvshows"),
)


def query(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


def url_builder(runtime, **params):
    return "{0}?{1}".format(runtime.base_url, urlencode(params))


def result_value(source, suffix=""):
    movie = source.endswith("movies")
    value = {
        "id": 41,
        "overview": "A synthetic plot" + suffix,
        "genre_ids": [18, 35, 999],
        "vote_average": "7.25",
        "poster_path": "/poster.jpg",
        "backdrop_path": "/backdrop.jpg",
    }
    if movie:
        value.update(
            title="Localized film" + suffix,
            original_title="Original film" + suffix,
            release_date="2024-03-02",
        )
    else:
        value.update(
            name="Localized show" + suffix,
            original_name="Original show" + suffix,
            first_air_date="2023-04-05",
        )
    return value


class InfoTag:
    def __init__(self):
        self.calls = []

    def setTitle(self, value):
        self.calls.append(("setTitle", value))

    def setMediaType(self, value):
        self.calls.append(("setMediaType", value))

    def setPlot(self, value):
        self.calls.append(("setPlot", value))

    def setYear(self, value):
        self.calls.append(("setYear", value))

    def setGenres(self, value):
        self.calls.append(("setGenres", value))

    def setRating(self, value):
        self.calls.append(("setRating", value))


class ListItem:
    def __init__(self, label="", path=""):
        self.label = label
        self.path = path
        self.video_tag = InfoTag()
        self.art = {}
        self.commands = []
        self.legacy_calls = []

    def getVideoInfoTag(self):
        return self.video_tag

    def setInfo(self, kind, info):
        self.legacy_calls.append((kind, info))

    def setArt(self, art):
        self.art.update(art)

    def addContextMenuItems(self, commands):
        self.commands.extend(commands)


class Plugin:
    def __init__(self):
        self.items = []
        self.content = []
        self.categories = []
        self.builtins = []
        self.notifications = []
        self.events = []

    def addDirectoryItem(self, handle, url, item, is_folder=False):
        self.items.append({"url": url, "item": item, "is_folder": is_folder})
        self.events.append("add")

    def setContent(self, handle, content):
        self.content.append(content)

    def setPluginCategory(self, handle, category):
        self.categories.append(category)

    def endOfDirectory(self, handle, **kwargs):
        self.events.append("end")
        self.completion = kwargs

    def setResolvedUrl(self, *args):
        self.events.append("resolve")

    def executeBuiltin(self, command):
        self.builtins.append(command)
        self.events.append(command)

    def notification(self, heading, message):
        self.notifications.append(message)


class Logger:
    def __init__(self):
        self.exceptions = []

    def exception(self, *args):
        self.exceptions.append(args)


class Addon:
    def __init__(self, settings):
        self.settings = settings
        self.strings = {
            30402: "Error loading discovery",
            30408: "Unknown discovery title",
            30414: "Search original title",
            30417: "No discovery results",
            30421: "Filmy",
            30422: "Seriály",
            30438: "Obnovit",
            30441: "Objevit",
            30442: "Populární",
            30443: "Nejlépe hodnocené",
        }

    def getLocalizedString(self, string_id):
        return self.strings[string_id]

    def getAddonInfo(self, key):
        return {"name": "YAWsP", "path": "C:\\addon"}[key]


class Runtime:
    def __init__(self, native_wall=""):
        self.addon = Addon({"discovery_native_wall": native_wall})
        self.plugin = Plugin()
        self.logger = Logger()
        self.base_url = "plugin://plugin.video.yawspreloaded/"
        self.handle = 9
        self.setting_calls = []

    def get_setting(self, key):
        self.setting_calls.append(key)
        return self.addon.settings.get(key, "")

    def localized(self, string_id):
        return self.addon.getLocalizedString(string_id)

    def addon_info(self, key):
        return self.addon.getAddonInfo(key)

    def list_item(self, label="", path=""):
        return ListItem(label=label, path=path)

    def add_item(self, url, item, is_folder=False):
        self.plugin.addDirectoryItem(self.handle, url, item, is_folder)

    def set_content(self, content):
        self.plugin.setContent(self.handle, content)

    def set_category(self, category):
        self.plugin.setPluginCategory(self.handle, category)

    def execute_builtin(self, command):
        self.plugin.executeBuiltin(command)

    def notify(self, message):
        self.plugin.notification("YAWsP", message)


class Client:
    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []
        self.language_calls = 0

    def get(self, path, *args, **kwargs):
        self.calls.append((path, args, kwargs))
        value = self.responses[path]
        if isinstance(value, BaseException):
            raise value
        return value

    def locale_mapper(self):
        self.language_calls += 1
        return "cs-CZ"

    def request(self, *args, **kwargs):
        raise AssertionError("discovery must not issue raw or account requests")

    def get_account_id(self):
        raise AssertionError("discovery must not resolve an account")


class Genres:
    def __init__(self):
        self.calls = []
        self.values = {
            "movie": {18: "Drama", 35: "Komedie"},
            "tv": {18: "Drama", 35: "Komedie"},
        }

    def __call__(self, media, language):
        self.calls.append((media, language))
        return dict(self.values[media])


class Window:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.loads = []
        self.modal_calls = 0
        self.close_calls = 0
        self.records = []
        self.destination = None
        self.failed = False
        self.auto_select = False

    def load(self, source):
        self.loads.append(source)
        try:
            self.records = list(self.kwargs["load_category"](source))
        except TMDBError:
            self.failed = True
        else:
            self.failed = False

    def doModal(self):
        self.modal_calls += 1
        if self.auto_select and self.records:
            self.destination = self.records[0].search_url

    def close(self):
        self.close_calls += 1


class DiscoveryUITests(unittest.TestCase):
    def build(
        self,
        source="popular_movies",
        response=None,
        native_wall="true",
        auto_select=False,
    ):
        self.runtime = Runtime(native_wall=native_wall)
        endpoint = {name: path for name, path, _, _ in SOURCES}[source]
        self.client = Client({endpoint: response})
        self.genres = Genres()
        self.windows = []
        self.default_views = []

        def window_factory(**kwargs):
            window = Window(**kwargs)
            window.auto_select = auto_select
            self.windows.append(window)
            return window

        self.handlers = build_discovery_handlers(
            self.runtime,
            self.client,
            url_builder,
            self.genres,
            self.default_views.append,
            window_factory,
        )
        return self.handlers["tmdb_discovery"]

    def test_catalog_mapping_is_immutable_and_limited_to_four_fixed_paths(self):
        self.assertEqual(
            {name: (source.path, source.media) for name, source in CATALOG_SOURCES.items()},
            {name: (path, media) for name, path, media, _ in SOURCES},
        )
        with self.assertRaises(TypeError):
            CATALOG_SOURCES["new"] = object()

    def test_all_four_sources_have_one_list_call_and_exact_native_metadata(self):
        for source, path, media, content in SOURCES:
            with self.subTest(source=source):
                handler = self.build(source, {"results": [result_value(source)]}, "true")
                result = handler({"source": source})

                self.assertTrue(result.succeeded)
                self.assertEqual(self.client.calls, [(path, (), {})])
                expected_genre_media = "movie" if media == "movie" else "tv"
                self.assertEqual(self.genres.calls, [(expected_genre_media, "cs-CZ")])
                self.assertEqual(self.client.language_calls, 1)
                self.assertEqual(self.windows, [])
                self.assertEqual(self.default_views, [])
                self.assertEqual(self.runtime.plugin.content, [content])
                self.assertEqual(self.runtime.setting_calls, ["discovery_native_wall"])

                row = self.runtime.plugin.items[0]
                item = row["item"]
                title = "Localized film" if media == "movie" else "Localized show"
                year = 2024 if media == "movie" else 2023
                self.assertEqual(item.label, "{0} ({1})".format(title, year))
                self.assertEqual(
                    item.video_tag.calls,
                    [
                        ("setTitle", item.label),
                        ("setMediaType", "movie" if media == "movie" else "tvshow"),
                        ("setPlot", "A synthetic plot"),
                        ("setYear", year),
                        ("setGenres", ["Drama", "Komedie"]),
                        ("setRating", 7.25),
                    ],
                )
                self.assertEqual(item.legacy_calls, [])
                self.assertEqual(
                    item.art,
                    {
                        "poster": "https://image.tmdb.org/t/p/w500/poster.jpg",
                        "thumb": "https://image.tmdb.org/t/p/w500/poster.jpg",
                        "fanart": "https://image.tmdb.org/t/p/w780/backdrop.jpg",
                    },
                )
                self.assertTrue(row["is_folder"])
                self.assertEqual(
                    query(row["url"]),
                    {"action": "search", "what": title, "sort": "largest"},
                )
                self.assertNotIn("PlayMedia", row["url"])
                self.assertFalse(any("watched" in command for _, command in item.commands))

                result.post_complete()
                self.assertEqual(
                    self.default_views,
                    [{"category": "movies" if media == "movie" else "shows"}],
                )

    def test_original_title_context_command_uses_existing_search_only(self):
        handler = self.build(response={"results": [result_value("popular_movies")]})
        handler({"source": "popular_movies"})

        commands = self.runtime.plugin.items[0]["item"].commands
        self.assertEqual(len(commands), 1)
        label, command = commands[0]
        self.assertEqual(label, "Search original title")
        self.assertTrue(command.startswith("Container.Update("))
        self.assertTrue(command.endswith(")"))
        self.assertEqual(
            query(command[len("Container.Update(") : -1]),
            {"action": "search", "what": "Original film", "sort": "largest"},
        )
        self.assertEqual(self.runtime.plugin.builtins, [])

    def test_unknown_and_missing_source_fail_without_request_or_custom_window(self):
        for source in (None, "", "../account", "popular_movie", ["popular_movies"], {"source": "popular_movies"}):
            with self.subTest(source=source):
                handler = self.build(response={"results": []}, native_wall="")
                params = {} if source is None else {"source": source}
                result = handler(params)
                self.assertFalse(result.succeeded)
                self.assertEqual(self.client.calls, [])
                self.assertEqual(self.genres.calls, [])
                self.assertEqual(self.windows, [])
                self.assertEqual(self.runtime.setting_calls, [])

    def test_setting_is_strict_and_re_evaluated_only_for_next_valid_opening(self):
        for setting, expected_windows in (("true", 0), ("", 1), ("false", 1), ("True", 1), ("1", 1)):
            with self.subTest(setting=setting):
                handler = self.build(response={"results": []}, native_wall=setting)
                result = handler({"source": "popular_movies"})
                self.assertTrue(result.succeeded)
                self.assertEqual(len(self.windows), expected_windows)
                self.assertEqual([window.modal_calls for window in self.windows], [1] * expected_windows)
                if expected_windows:
                    self.assertEqual(self.windows[0].loads, ["popular_movies"])
                    self.assertEqual(self.windows[0].close_calls, 0)
                else:
                    self.assertEqual(self.runtime.plugin.items[0]["item"].label, "No discovery results")

    def test_custom_selection_hands_off_same_url_after_directory_completion_once(self):
        handler = self.build(response={"results": [result_value("popular_movies")]}, native_wall="", auto_select=True)
        runner = ActionRunner(self.runtime.plugin, 9, ListItem, logger=self.runtime.logger)

        result = runner.run_directory(lambda: handler({"source": "popular_movies"}))

        expected_url = url_builder(
            self.runtime, action="search", what="Localized film", sort="largest"
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(self.runtime.plugin.events, ["end", "Container.Update(" + expected_url + ")"])
        self.assertEqual(self.runtime.plugin.builtins, ["Container.Update(" + expected_url + ")"])
        self.assertNotIn("PlayMedia", self.runtime.plugin.builtins[0])
        self.assertEqual(self.windows[0].close_calls, 0)
        result.post_complete()
        self.assertEqual(len(self.runtime.plugin.builtins), 1)

    def test_empty_native_state_has_no_genre_lookup_but_sets_view_after_success(self):
        handler = self.build(response={"results": []})
        result = handler({"source": "popular_movies"})

        self.assertTrue(result.succeeded)
        self.assertEqual(self.genres.calls, [])
        self.assertEqual(len(self.runtime.plugin.items), 1)
        empty = self.runtime.plugin.items[0]
        self.assertEqual(empty["item"].label, "No discovery results")
        self.assertEqual(query(empty["url"])["source"], "popular_movies")
        self.assertEqual(self.default_views, [])
        result.post_complete()
        self.assertEqual(self.default_views, [{"category": "movies"}])

    def test_native_tmdb_error_is_logged_once_without_fake_rows_or_view_hook(self):
        handler = self.build(response=TMDBError("synthetic failure"))
        result = handler({"source": "popular_movies"})

        self.assertFalse(result.succeeded)
        self.assertIsNone(result.post_complete)
        self.assertEqual(self.runtime.plugin.items, [])
        self.assertEqual(self.runtime.plugin.notifications, ["Error loading discovery"])
        self.assertEqual(len(self.runtime.logger.exceptions), 1)
        self.assertEqual(self.default_views, [])

    def test_custom_error_stays_in_window_and_does_not_close_from_handler(self):
        handler = self.build(response=TMDBError("synthetic failure"), native_wall="")
        result = handler({"source": "popular_movies"})

        self.assertFalse(result.succeeded)
        self.assertIsNone(result.post_complete)
        self.assertTrue(self.windows[0].failed)
        self.assertEqual(self.windows[0].modal_calls, 1)
        self.assertEqual(self.windows[0].close_calls, 0)
        self.assertEqual(self.runtime.plugin.notifications, [])
        self.assertEqual(len(self.runtime.logger.exceptions), 1)

    def test_malformed_entries_are_bounded_and_valid_entries_have_detached_metadata(self):
        response = {
            "results": [
                None,
                {"id": True, "title": "Boolean ids are not titles"},
                result_value("popular_movies", " valid"),
                {
                    "id": 77,
                    "title": None,
                    "original_title": None,
                    "release_date": {"bad": "year"},
                    "vote_average": float("nan"),
                    "genre_ids": "18",
                    "poster_path": 17,
                    "backdrop_path": [],
                },
                {"id": None, "title": "Missing id"},
            ]
        }
        handler = self.build(response=response, native_wall="")
        result = handler({"source": "popular_movies"})
        records = self.windows[0].records

        self.assertTrue(result.succeeded)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].rating, 7.25)
        self.assertEqual(records[1].title, "Unknown discovery title")
        self.assertEqual(records[1].year, 0)
        self.assertEqual(records[1].genres, ())
        self.assertTrue(math.isfinite(records[1].rating))
        self.assertEqual(records[1].rating, 0.0)
        self.assertEqual(dict(records[1].art), {})
        with self.assertRaises(TypeError):
            records[0].art["poster"] = "mutation"
        with self.assertRaises((AttributeError, TypeError)):
            records[0].genres += ("Mutation",)


if __name__ == "__main__":
    unittest.main()
