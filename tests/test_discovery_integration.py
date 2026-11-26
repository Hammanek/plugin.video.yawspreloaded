from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit

from tests.test_entrypoint import (
    json_response,
    production_tmdb_invocation,
    service_jobs,
)
from yawsp_app import router as production_router
from yawsp_app import tmdb_ui as production_tmdb_ui


ROOT = Path(__file__).resolve().parents[1]


def query(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


class DiscoveryIntegrationTests(unittest.TestCase):
    def tearDown(self):
        production_router.close()

    def test_root_places_discovery_first_without_http_or_background_work(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.strings.update(
                {
                    30201: "Search",
                    30202: "Queue",
                    30203: "History",
                    30204: "Settings",
                    30418: "TMDB Watchlist",
                }
            )
            result = production_router.router("")

            self.assertTrue(result.succeeded)
            self.assertEqual(
                [item["listitem"].label for item in recorder.items],
                [
                    "Objevit",
                    "Search",
                    "Queue",
                    "History",
                    "Settings",
                    "TMDB Watchlist",
                ],
            )
            self.assertEqual(
                query(recorder.items[0]["url"]),
                {"action": "tmdb_discovery"},
            )
            self.assertEqual(session.calls, [])
            self.assertEqual(service_jobs(owner, pending=True), [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)

    def test_missing_source_selects_four_categories_without_http(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.strings.update({30421: "Filmy", 30422: "Seriály"})

            result = production_router.router("action=tmdb_discovery")

            self.assertTrue(result.succeeded)
            self.assertIsNone(result.post_complete)
            self.assertEqual(
                [row["listitem"].label for row in recorder.items],
                [
                    "Populární Filmy",
                    "Populární Seriály",
                    "Nejlépe hodnocené Filmy",
                    "Nejlépe hodnocené Seriály",
                ],
            )
            self.assertEqual(
                [query(row["url"]) for row in recorder.items],
                [
                    {"action": "tmdb_discovery", "source": "popular_movies"},
                    {"action": "tmdb_discovery", "source": "popular_shows"},
                    {"action": "tmdb_discovery", "source": "top_rated_movies"},
                    {"action": "tmdb_discovery", "source": "top_rated_shows"},
                ],
            )
            self.assertTrue(all(row["isFolder"] for row in recorder.items))
            self.assertEqual(session.calls, [])
            self.assertEqual(service_jobs(owner, pending=True), [])
            self.assertEqual(
                recorder.end_of_directory_calls,
                [
                    {
                        "handle": 17,
                        "succeeded": True,
                        "updateListing": False,
                        "cacheToDisc": True,
                    }
                ],
            )
            self.assertEqual(recorder.builtin_commands, [])
            self.assertNotIn(("getSetting", "tmdb_api_key"), addon.calls)
            self.assertNotIn(("getSetting", "tmdb_view_defaulted"), addon.calls)
            self.assertEqual(addon.open_settings_calls, 0)

    def test_popular_movies_render_one_native_row_then_default_view_after_completion(self):
        responses = [
            json_response(
                {
                    "results": [
                        {
                            "id": 41,
                            "title": "Discovery title",
                            "original_title": "Original title",
                            "release_date": "2026-04-03",
                            "genre_ids": [18],
                            "vote_average": 7.25,
                            "overview": "Synthetic discovery plot",
                            "poster_path": "/poster.jpg",
                            "backdrop_path": "/backdrop.jpg",
                        }
                    ]
                }
            ),
            json_response({"genres": [{"id": 18, "name": "Drama"}]}),
        ]
        with production_tmdb_invocation(responses) as (owner, session, addon, recorder):
            result = production_router.router(
                "action=tmdb_discovery&source=popular_movies"
            )

            self.assertTrue(result.succeeded)
            self.assertEqual(len(recorder.items), 1)
            row = recorder.items[0]
            item = row["listitem"]
            self.assertTrue(row["isFolder"])
            self.assertEqual(item.label, "Discovery title (2026)")
            self.assertEqual(
                query(row["url"]),
                {"action": "search", "what": "Discovery title", "sort": "largest"},
            )
            self.assertEqual(
                item.info,
                [
                    (
                        "video",
                        {
                            "title": "Discovery title",
                            "mediatype": "movie",
                            "plot": "Synthetic discovery plot",
                            "year": 2026,
                            "genre": "Drama",
                            "rating": 7.25,
                        },
                    )
                ],
            )
            self.assertEqual(
                item.art,
                {
                    "poster": "https://image.tmdb.org/t/p/w500/poster.jpg",
                    "thumb": "https://image.tmdb.org/t/p/w500/poster.jpg",
                    "fanart": "https://image.tmdb.org/t/p/w780/backdrop.jpg",
                },
            )
            content_calls = [
                call for call in recorder.calls if call["method"] == "setContent"
            ]
            self.assertEqual(len(content_calls), 1)
            self.assertEqual(content_calls[0]["args"], (17, "movies"))
            self.assertNotIn(("getLocalizedString", 1040), addon.calls)
            self.assertNotIn(("getSetting", "discovery_native_wall"), addon.calls)
            self.assertEqual(
                [call["url"] for call in session.calls],
                [
                    "https://api.themoviedb.org/3/movie/popular",
                    "https://api.themoviedb.org/3/genre/movie/list",
                ],
            )
            self.assertFalse(any("/account" in call["url"] for call in session.calls))
            for call in session.calls:
                self.assertEqual(
                    call["kwargs"]["params"],
                    {"language": "en-US", "api_key": "offline-api-key"},
                )
                self.assertNotIn("session_id", call["kwargs"]["params"])
            self.assertEqual(service_jobs(owner, pending=True), [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertTrue(recorder.end_of_directory_calls[-1]["succeeded"])
            self.assertEqual(recorder.builtin_commands, ["Container.SetViewMode(54)"])
            self.assertEqual(
                [call["method"] for call in recorder.calls[-2:]],
                ["endOfDirectory", "executeBuiltin"],
            )

    def test_top_rated_shows_use_the_same_native_public_data_path(self):
        responses = [
            json_response(
                {
                    "results": [
                        {
                            "id": 42,
                            "name": "Top show",
                            "original_name": "Original show",
                            "first_air_date": "2025-01-02",
                            "genre_ids": [18],
                            "vote_average": 8.0,
                            "overview": "Synthetic show plot",
                        }
                    ]
                }
            ),
            json_response({"genres": [{"id": 18, "name": "Drama"}]}),
        ]
        with production_tmdb_invocation(responses) as (owner, session, addon, recorder):
            result = production_router.router(
                "action=tmdb_discovery&source=top_rated_shows"
            )

            self.assertTrue(result.succeeded)
            self.assertEqual(len(recorder.items), 1)
            self.assertEqual(recorder.items[0]["listitem"].label, "Top show (2025)")
            self.assertEqual(
                query(recorder.items[0]["url"]),
                {"action": "search", "what": "Top show", "sort": "largest"},
            )
            self.assertEqual(
                [call["url"] for call in session.calls],
                [
                    "https://api.themoviedb.org/3/tv/top_rated",
                    "https://api.themoviedb.org/3/genre/tv/list",
                ],
            )
            self.assertNotIn(("getSetting", "discovery_native_wall"), addon.calls)
            self.assertEqual(service_jobs(owner, pending=True), [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertEqual(recorder.builtin_commands, ["Container.SetViewMode(54)"])
            self.assertEqual(addon.getSetting("tmdb_view_defaulted"), "true")

    def test_native_public_unauthorized_preserves_personal_identity_and_sanitizes_log(self):
        unauthorized = json_response({}, status_code=401)
        unauthorized.url = (
            "https://api.themoviedb.org/3/movie/popular"
            "?api_key=offline-api-key&session_id=session-1"
        )
        with production_tmdb_invocation([unauthorized]) as (
            owner,
            session,
            addon,
            recorder,
        ):
            original_identity = (
                addon.getSetting("tmdb_access_token"),
                addon.getSetting("tmdb_account_id"),
            )
            with self.assertLogs(level="ERROR") as logged:
                result = production_router.router(
                    "action=tmdb_discovery&source=popular_movies"
                )

            self.assertFalse(result.succeeded)
            self.assertEqual(len(session.calls), 1)
            request = session.calls[0]
            self.assertEqual(request["url"], "https://api.themoviedb.org/3/movie/popular")
            self.assertEqual(
                request["kwargs"]["params"],
                {"language": "en-US", "api_key": "offline-api-key"},
            )
            self.assertNotIn("session_id", request["kwargs"]["params"])
            self.assertEqual(
                (
                    addon.getSetting("tmdb_access_token"),
                    addon.getSetting("tmdb_account_id"),
                ),
                original_identity,
            )
            self.assertEqual(original_identity, ("session-1", "account-1"))
            self.assertFalse(any("/account" in call["url"] for call in session.calls))
            self.assertEqual(service_jobs(owner, pending=True), [])
            public_log = "\n".join(logged.output)
            for secret in ("offline-api-key", "session-1", "account-1"):
                self.assertNotIn(secret, public_log)
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertFalse(recorder.end_of_directory_calls[-1]["succeeded"])
            self.assertEqual(recorder.builtin_commands, [])

    def test_valid_source_without_api_key_fails_before_settings_or_http(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_api_key", "")
            result = production_router.router(
                "action=tmdb_discovery&source=popular_movies"
            )

            self.assertFalse(result.succeeded)
            self.assertEqual(session.calls, [])
            self.assertEqual(addon.open_settings_calls, 1)
            self.assertNotIn(("getSetting", "discovery_native_wall"), addon.calls)
            self.assertEqual(service_jobs(owner, pending=True), [])
            self.assertEqual(recorder.items, [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertFalse(recorder.end_of_directory_calls[-1]["succeeded"])

    def test_explicit_unknown_source_is_local_failure_without_calls_or_settings(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_api_key", "")
            result = production_router.router(
                "action=tmdb_discovery&source=no_such_source"
            )

            self.assertFalse(result.succeeded)
            self.assertEqual(session.calls, [])
            self.assertEqual(addon.open_settings_calls, 0)
            self.assertNotIn(("getSetting", "discovery_native_wall"), addon.calls)
            self.assertEqual(service_jobs(owner, pending=True), [])
            self.assertEqual(recorder.items, [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertFalse(recorder.end_of_directory_calls[-1]["succeeded"])

    def test_custom_presentation_factory_resources_and_setting_are_absent(self):
        for relative in (
            "yawsp_app/discovery_window.py",
            "resources/skins/default/720p/DiscoveryWindow.xml",
        ):
            with self.subTest(retired_resource=relative):
                self.assertFalse(ROOT.joinpath(*relative.split("/")).exists())
        self.assertFalse((ROOT / "yawsp_app/discovery_script.py").exists())

        settings = ET.parse(str(ROOT / "resources/settings.xml")).getroot()
        setting_ids = {
            element.get("id")
            for element in settings.iter("setting")
            if element.get("id")
        }
        self.assertNotIn("discovery_native_wall", setting_ids)
        self.assertFalse(hasattr(production_tmdb_ui, "create_discovery_window"))


if __name__ == "__main__":
    unittest.main()
