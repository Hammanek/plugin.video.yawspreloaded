import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.test_entrypoint import json_response, production_tmdb_invocation
from yawsp_app import router as production_router
from yawsp_app import tmdb_ui as production_tmdb_ui


def query(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


class RecordingDiscoveryWindow:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.loaded_sources = []
        self.modal_calls = 0
        self.destination = None
        self.failed = False
        self.pending_sources = []

    def load(self, source):
        self.pending_sources.append(source)

    def doModal(self):
        self.modal_calls += 1
        if self.pending_sources:
            source = self.pending_sources.pop(0)
            self.loaded_sources.append(source)
            records = self.kwargs["load_category"](source)
            if records:
                self.destination = records[0].search_url


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
                {"action": "tmdb_discovery", "source": "popular_movies"},
            )
            self.assertEqual(session.calls, [])
            self.assertEqual(owner.background.worker.jobs, [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)

    def test_default_custom_loads_one_category_and_hands_off_after_completion(self):
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
            windows = []
            with mock.patch.object(
                production_tmdb_ui,
                "create_discovery_window",
                side_effect=lambda **kwargs: windows.append(
                    RecordingDiscoveryWindow(**kwargs)
                )
                or windows[-1],
                create=True,
            ):
                result = production_router.router(
                    "action=tmdb_discovery&source=popular_movies"
                )

            self.assertTrue(result.succeeded)
            self.assertEqual(len(windows), 1)
            self.assertEqual(windows[0].loaded_sources, ["popular_movies"])
            self.assertEqual(windows[0].modal_calls, 1)
            self.assertEqual(
                windows[0].kwargs["labels"]["loading"], "Loading directory"
            )
            self.assertEqual(
                windows[0].kwargs["labels"]["empty"], "No matching files found"
            )
            self.assertNotIn(("getLocalizedString", 1040), addon.calls)
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
            self.assertEqual(owner.background.worker.jobs, [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertTrue(recorder.end_of_directory_calls[-1]["succeeded"])
            self.assertEqual(len(recorder.builtin_commands), 1)
            command = recorder.builtin_commands[0]
            self.assertTrue(command.startswith("Container.Update("))
            self.assertEqual(
                query(command[len("Container.Update(") : -1]),
                {"action": "search", "what": "Discovery title", "sort": "largest"},
            )
            self.assertEqual(
                [call["method"] for call in recorder.calls[-2:]],
                ["endOfDirectory", "executeBuiltin"],
            )

    def test_native_wall_setting_reuses_directory_without_custom_factory(self):
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
            addon.setSetting("discovery_native_wall", "true")
            with mock.patch.object(
                production_tmdb_ui, "create_discovery_window", create=True
            ) as window_factory:
                result = production_router.router(
                    "action=tmdb_discovery&source=top_rated_shows"
                )

            self.assertTrue(result.succeeded)
            window_factory.assert_not_called()
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
            self.assertEqual(owner.background.worker.jobs, [])
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
            addon.setSetting("discovery_native_wall", "true")
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
            self.assertEqual(owner.background.worker.jobs, [])
            public_log = "\n".join(logged.output)
            for secret in ("offline-api-key", "session-1", "account-1"):
                self.assertNotIn(secret, public_log)
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertFalse(recorder.end_of_directory_calls[-1]["succeeded"])

    def test_valid_source_without_api_key_fails_before_settings_or_http(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_api_key", "")
            with mock.patch.object(
                production_tmdb_ui, "create_discovery_window", create=True
            ) as window_factory:
                result = production_router.router(
                    "action=tmdb_discovery&source=popular_movies"
                )

            self.assertFalse(result.succeeded)
            window_factory.assert_not_called()
            self.assertEqual(session.calls, [])
            self.assertEqual(addon.open_settings_calls, 1)
            self.assertNotIn(("getSetting", "discovery_native_wall"), addon.calls)
            self.assertEqual(owner.background.worker.jobs, [])
            self.assertEqual(recorder.items, [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertFalse(recorder.end_of_directory_calls[-1]["succeeded"])

    def test_unknown_source_is_local_failure_without_calls_or_window(self):
        with production_tmdb_invocation([]) as (owner, session, addon, recorder):
            addon.setSetting("tmdb_api_key", "")
            with mock.patch.object(
                production_tmdb_ui, "create_discovery_window", create=True
            ) as window_factory:
                result = production_router.router(
                    "action=tmdb_discovery&source=not_a_catalog"
                )

            self.assertFalse(result.succeeded)
            window_factory.assert_not_called()
            self.assertEqual(session.calls, [])
            self.assertEqual(addon.open_settings_calls, 0)
            self.assertEqual(owner.background.worker.jobs, [])
            self.assertEqual(recorder.items, [])
            self.assertEqual(len(recorder.end_of_directory_calls), 1)
            self.assertFalse(recorder.end_of_directory_calls[-1]["succeeded"])


if __name__ == "__main__":
    unittest.main()
