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
            30440: "No matching files found",
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
    def __init__(self):
        self.addon = Addon({})
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


class DiscoveryUITests(unittest.TestCase):
    def build(self, source="popular_movies", response=None):
        self.runtime = Runtime()
        endpoint = {name: path for name, path, _, _ in SOURCES}[source]
        self.client = Client({endpoint: response})
        self.genres = Genres()
        self.default_views = []

        def set_default_view(params):
            self.default_views.append(params)
            self.runtime.plugin.events.append("view")

        self.handlers = build_discovery_handlers(
            self.runtime,
            self.client,
            url_builder,
            self.genres,
            set_default_view,
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
                handler = self.build(source, {"results": [result_value(source)]})
                result = handler({"source": source})

                self.assertTrue(result.succeeded)
                self.assertEqual(self.client.calls, [(path, (), {})])
                expected_genre_media = "movie" if media == "movie" else "tv"
                self.assertEqual(self.genres.calls, [(expected_genre_media, "cs-CZ")])
                self.assertEqual(self.client.language_calls, 1)
                self.assertEqual(self.default_views, [])
                self.assertEqual(self.runtime.plugin.content, [content])
                self.assertEqual(self.runtime.setting_calls, [])

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

    def test_explicit_invalid_sources_fail_without_request_or_settings(self):
        for source in (
            None,
            "",
            "../account",
            "popular_movie",
            "no_such_source",
            ["popular_movies"],
            {"source": "popular_movies"},
        ):
            with self.subTest(source=source):
                handler = self.build(response={"results": []})
                result = handler({"source": source})
                self.assertFalse(result.succeeded)
                self.assertEqual(self.client.calls, [])
                self.assertEqual(self.genres.calls, [])
                self.assertEqual(self.runtime.plugin.items, [])
                self.assertEqual(self.runtime.setting_calls, [])

    def test_native_directory_completes_before_one_default_view_without_handoff(self):
        handler = self.build(response={"results": [result_value("popular_movies")]})
        runner = ActionRunner(self.runtime.plugin, 9, ListItem, logger=self.runtime.logger)

        result = runner.run_directory(lambda: handler({"source": "popular_movies"}))

        self.assertTrue(result.succeeded)
        self.assertEqual(self.runtime.plugin.events, ["add", "end", "view"])
        self.assertEqual(self.default_views, [{"category": "movies"}])
        self.assertEqual(self.runtime.plugin.builtins, [])
        self.assertEqual(len(self.runtime.plugin.items), 1)
        self.assertEqual(
            query(self.runtime.plugin.items[0]["url"]),
            {"action": "search", "what": "Localized film", "sort": "largest"},
        )

    def test_empty_native_state_has_no_genre_lookup_but_sets_view_after_success(self):
        handler = self.build(response={"results": []})
        result = handler({"source": "popular_movies"})

        self.assertTrue(result.succeeded)
        self.assertEqual(self.genres.calls, [])
        self.assertEqual(len(self.runtime.plugin.items), 1)
        empty = self.runtime.plugin.items[0]
        self.assertEqual(empty["item"].label, "No matching files found")
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

    def test_malformed_entries_are_bounded_and_valid_entries_have_normalized_native_metadata(self):
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
        handler = self.build(response=response)
        result = handler({"source": "popular_movies"})
        rows = self.runtime.plugin.items

        self.assertTrue(result.succeeded)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["item"].label, "Localized film valid (2024)")
        self.assertEqual(rows[0]["item"].video_tag.calls[-1], ("setRating", 7.25))
        fallback = rows[1]["item"]
        self.assertEqual(fallback.label, "Unknown discovery title")
        self.assertEqual(fallback.video_tag.calls, [
            ("setTitle", "Unknown discovery title"),
            ("setMediaType", "movie"),
            ("setPlot", ""),
            ("setYear", 0),
            ("setGenres", [""]),
            ("setRating", 0.0),
        ])
        rating = fallback.video_tag.calls[-1][1]
        self.assertTrue(math.isfinite(rating))
        self.assertEqual(fallback.art, {})
        self.assertEqual(self.runtime.setting_calls, [])


if __name__ == "__main__":
    unittest.main()
