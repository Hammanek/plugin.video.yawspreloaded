"""Exact public/detail requests and detached native service result values."""

import hashlib
import json
import logging
import os
import tempfile
import unittest
from unittest import mock

import requests

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.genre_file_cache import GenreFileCache, VALID
from yawsp_app.tmdb import GenreRepository, TMDBClient, TMDBError, public_tmdb_client
from yawsp_app.tmdb_service_store import ServiceStateStore
from yawsp_app.tmdb_service_worker import ServiceOperationEngine, ServiceRunner
from tests.support.http_stubs import FakeResponse, FakeSession


WAITERS = tuple("%032x" % (number + 1) for number in range(3))
BINDING = "v1:" + hashlib.sha256(b"personal-session").hexdigest()


class Settings:
    def __init__(self):
        self.values = {
            "tmdb_api_key": "synthetic-api-key",
            "tmdb_access_token": "personal-session",
            "tmdb_account_id": "42",
        }
        self.calls = []

    def get(self, key):
        self.calls.append(("get", key))
        return self.values.get(key, "")

    def set(self, key, value):
        self.calls.append(("set", key, value))
        self.values[key] = value


class ForbiddenIdentityLock:
    def __enter__(self):
        raise AssertionError("public discovery must never acquire the identity lock")

    def __exit__(self, exc_type, exc_value, traceback):
        raise AssertionError("public discovery must never leave an identity lock")


class OperationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.genre_path = os.path.join(self.root, "tmdb_genres.json")
        self.settings = Settings()
        self.language = {"value": "en-US"}

    def engine(self, session, repository=None, identity_lock=None):
        return ServiceOperationEngine(
            self.settings,
            repository
            if repository is not None
            else GenreRepository(GenreFileCache(self.genre_path)),
            lambda: self.language["value"],
            identity_lock,
            session,
            lambda: False,
            logging.getLogger("test.service.operations"),
        )

    @staticmethod
    def catalog_value(media, suffix=""):
        value = {
            "id": 41,
            "overview": "Děj" + suffix,
            "genre_ids": [18, 35, 999],
            "vote_average": "7.25",
            "poster_path": "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
        }
        if media == "movie":
            value.update(
                title="Lokalizovaný film" + suffix,
                original_title="Original film" + suffix,
                release_date="2024-03-02",
            )
        else:
            value.update(
                name="Lokalizovaný seriál" + suffix,
                original_name="Original show" + suffix,
                first_air_date="2023-04-05",
            )
        return value

    @staticmethod
    def expected_catalog_row(media, suffix=""):
        movie = media == "movie"
        return {
            "media": media,
            "media_id": 41,
            "title": ("Lokalizovaný film" if movie else "Lokalizovaný seriál")
            + suffix,
            "original_title": ("Original film" if movie else "Original show")
            + suffix,
            "year": 2024 if movie else 2023,
            "genres": ["Drama", "Komedie"],
            "rating": 7.25,
            "plot": "Děj" + suffix,
            "art": {
                "poster": "https://image.tmdb.org/t/p/w500/poster.jpg",
                "thumb": "https://image.tmdb.org/t/p/w500/poster.jpg",
                "fanart": "https://image.tmdb.org/t/p/w780/backdrop.jpg",
            },
        }

    @staticmethod
    def genre_response(media):
        return FakeResponse(
            json_data={
                "genres": [
                    {"id": 18, "name": "Drama"},
                    {"id": 35, "name": "Komedie"},
                ]
            }
        )

    def read_genre_bytes(self):
        with open(self.genre_path, "rb") as handle:
            return handle.read()

    def write_valid_genres(self):
        contents = json.dumps(
            {"schema": 1, "genres": {"tv/en-US": {"1": "Original"}}},
            ensure_ascii=False,
        ).encode("utf-8")
        with open(self.genre_path, "wb") as handle:
            handle.write(contents)
        return contents

    def test_four_sources_make_one_list_get_and_shared_movie_tv_genre_gets(self):
        list_responses = {
            "popular_movies": FakeResponse(
                json_data={"results": [self.catalog_value("movie", " one")]}
            ),
            "popular_shows": FakeResponse(
                json_data={"results": [self.catalog_value("tv", " one")]}
            ),
            "top_rated_movies": FakeResponse(
                json_data={"results": [self.catalog_value("movie", " two")]}
            ),
            "top_rated_shows": FakeResponse(
                json_data={"results": [self.catalog_value("tv", " two")]}
            ),
        }
        movie_genres = self.genre_response("movie")
        tv_genres = self.genre_response("tv")
        session = FakeSession(
            [
                list_responses["popular_movies"],
                movie_genres,
                list_responses["popular_shows"],
                tv_genres,
                list_responses["top_rated_movies"],
                list_responses["top_rated_shows"],
            ]
        )
        engine = self.engine(session, identity_lock=ForbiddenIdentityLock())
        expected_paths = {
            "popular_movies": "/3/movie/popular",
            "popular_shows": "/3/tv/popular",
            "top_rated_movies": "/3/movie/top_rated",
            "top_rated_shows": "/3/tv/top_rated",
        }
        suffixes = {
            "popular_movies": " one",
            "popular_shows": " one",
            "top_rated_movies": " two",
            "top_rated_shows": " two",
        }

        for source, path in expected_paths.items():
            with self.subTest(source=source):
                result = engine.execute(protocol.catalog_request(source, "cs-CZ"))
                media = "movie" if source.endswith("movies") else "tv"
                self.assertEqual(
                    result,
                    {"rows": [self.expected_catalog_row(media, suffixes[source])]},
                )
                protocol.make_result("a" * 32, "succeeded", result)

        self.assertEqual(
            [(call["method"], call["url"]) for call in session.calls],
            [
                ("GET", "https://api.themoviedb.org/3/movie/popular"),
                ("GET", "https://api.themoviedb.org/3/genre/movie/list"),
                ("GET", "https://api.themoviedb.org/3/tv/popular"),
                ("GET", "https://api.themoviedb.org/3/genre/tv/list"),
                ("GET", "https://api.themoviedb.org/3/movie/top_rated"),
                ("GET", "https://api.themoviedb.org/3/tv/top_rated"),
            ],
        )
        for call in session.calls:
            self.assertEqual(
                call["kwargs"]["params"],
                {"language": "cs-CZ", "api_key": "synthetic-api-key"},
            )
            self.assertNotIn("session_id", call["kwargs"]["params"])
            self.assertEqual(call["kwargs"]["timeout"], 15)
        self.assertTrue(
            all(response.closed for response in list_responses.values())
        )
        self.assertTrue(movie_genres.closed)
        self.assertTrue(tv_genres.closed)
        with open(self.genre_path, encoding="utf-8") as handle:
            cached = json.load(handle)
        self.assertEqual(cached["schema"], 1)
        self.assertEqual(set(cached["genres"]), {"movie/cs-CZ", "tv/cs-CZ"})
        self.assertEqual(
            cached["genres"]["movie/cs-CZ"],
            {"18": "Drama", "35": "Komedie"},
        )
        self.assertEqual(
            GenreFileCache(self.genre_path).read(),
            (VALID, cached),
        )
        self.assertEqual(self.settings.values["tmdb_access_token"], "personal-session")
        self.assertEqual(self.settings.values["tmdb_account_id"], "42")
        self.assertNotIn(("set", "tmdb_access_token", ""), self.settings.calls)
        self.assertNotIn(("set", "tmdb_account_id", ""), self.settings.calls)

    def test_fresh_engine_uses_persisted_genre_group_without_another_genre_get(self):
        repository = GenreRepository(GenreFileCache(self.genre_path))
        first = self.engine(
            FakeSession(
                [
                    FakeResponse(json_data={"results": [self.catalog_value("movie")]}),
                    self.genre_response("movie"),
                ]
            ),
            repository,
        )
        first.execute(protocol.catalog_request("popular_movies", "cs-CZ"))

        session = FakeSession(
            [FakeResponse(json_data={"results": [self.catalog_value("movie")]})]
        )
        second = self.engine(session, GenreRepository(GenreFileCache(self.genre_path)))
        result = second.execute(protocol.catalog_request("top_rated_movies", "cs-CZ"))

        self.assertEqual(result["rows"][0]["genres"], ["Drama", "Komedie"])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["url"], "https://api.themoviedb.org/3/movie/top_rated")

    def test_missing_empty_malformed_and_failed_lists_never_write_genres(self):
        cases = (
            ("missing", FakeResponse(json_data={}), True),
            ("empty", FakeResponse(json_data={"results": []}), False),
            ("wrong-results", FakeResponse(json_data={"results": {}}), True),
            ("malformed-json", FakeResponse(content=b"not-json"), True),
            ("failed", requests.Timeout("synthetic offline"), True),
        )
        for label, response, fails in cases:
            with self.subTest(label=label):
                original = self.write_valid_genres()
                session = FakeSession([response])
                engine = self.engine(session, identity_lock=ForbiddenIdentityLock())
                if fails:
                    with self.assertRaises(TMDBError):
                        engine.execute(
                            protocol.catalog_request("popular_movies", "cs-CZ")
                        )
                else:
                    self.assertEqual(
                        engine.execute(
                            protocol.catalog_request("popular_movies", "cs-CZ")
                        ),
                        {"rows": []},
                    )
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(self.read_genre_bytes(), original)
                if isinstance(response, FakeResponse):
                    self.assertTrue(response.closed)

    def test_public_401_preserves_identity_lock_file_and_response_cleanup(self):
        original = self.write_valid_genres()
        response = FakeResponse(json_data={}, status_code=401)
        session = FakeSession([response])

        with self.assertRaises(TMDBError):
            self.engine(
                session, identity_lock=ForbiddenIdentityLock()
            ).execute(protocol.catalog_request("popular_shows", "cs-CZ"))

        self.assertEqual(self.settings.values["tmdb_access_token"], "personal-session")
        self.assertEqual(self.settings.values["tmdb_account_id"], "42")
        self.assertEqual(self.settings.calls, [("get", "tmdb_api_key")])
        self.assertFalse(any(call[0] == "set" for call in self.settings.calls))
        self.assertEqual(self.read_genre_bytes(), original)
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("session_id", session.calls[0]["kwargs"]["params"])
        self.assertTrue(response.closed)

    def test_genre_commit_follows_response_close_and_supports_strict_reload(self):
        events = []
        list_response = FakeResponse(
            json_data={"results": [self.catalog_value("movie")]}
        )
        genre_response = self.genre_response("movie")
        session = FakeSession([list_response, genre_response])

        class ObservedCache(GenreFileCache):
            def store(inner, media, language, mapping):
                events.append(("store", list_response.closed, genre_response.closed))
                committed = super().store(media, language, mapping)
                independent = GenreFileCache(inner.path)
                self.assertEqual(independent.read()[0], VALID)
                events.append("strict-reload")
                return committed

        result = self.engine(
            session, GenreRepository(ObservedCache(self.genre_path))
        ).execute(protocol.catalog_request("popular_movies", "cs-CZ"))

        self.assertEqual(result["rows"][0]["genres"], ["Drama", "Komedie"])
        self.assertEqual(events, [("store", True, True), "strict-reload"])

    def test_malformed_or_failed_genre_response_never_persists_a_partial_group(self):
        self.write_valid_genres()
        original = self.read_genre_bytes()
        for response in (FakeResponse(json_data={}), requests.Timeout("offline")):
            with self.subTest(response=type(response).__name__):
                session = FakeSession(
                    [
                        FakeResponse(
                            json_data={"results": [self.catalog_value("movie")]}
                        ),
                        response,
                    ]
                )
                with self.assertRaises(TMDBError):
                    self.engine(session).execute(
                        protocol.catalog_request("popular_movies", "cs-CZ")
                    )
                self.assertEqual(self.read_genre_bytes(), original)

    def test_seasons_and_episodes_use_exact_authenticated_requests_and_native_values(self):
        show_data = {
            "name": "Seriál",
            "original_name": "Original",
            "seasons": [
                {"season_number": 2, "episode_count": 3, "poster_path": "/s.jpg"}
            ],
        }
        episode_data = {
            "episodes": [
                {
                    "episode_number": 4,
                    "name": "Díl",
                    "air_date": "2026-11-20",
                    "overview": "Děj dílu",
                    "vote_average": 8.5,
                    "runtime": 50,
                    "still_path": "/episode.jpg",
                }
            ]
        }
        responses = [
            FakeResponse(json_data=show_data),
            FakeResponse(json_data=episode_data),
        ]
        session = FakeSession(responses)
        engine = self.engine(session)

        seasons = engine.execute(protocol.seasons_request(8, "cs-CZ", BINDING))
        episodes = engine.execute(
            protocol.episodes_request(8, 2, "cs-CZ", BINDING)
        )

        self.assertEqual(
            seasons,
            {
                "title": "Seriál",
                "original_title": "Original",
                "seasons": [
                    {"season_number": 2, "episode_count": 3, "poster_path": "/s.jpg"}
                ],
            },
        )
        self.assertEqual(
            episodes,
            {
                "episodes": [
                    {
                        "episode_number": 4,
                        "name": "Díl",
                        "air_date": "2026-11-20",
                        "overview": "Děj dílu",
                        "vote_average": 8.5,
                        "runtime": 50,
                        "still_path": "/episode.jpg",
                    }
                ]
            },
        )
        self.assertEqual(
            [(call["method"], call["url"]) for call in session.calls],
            [
                ("GET", "https://api.themoviedb.org/3/tv/8"),
                ("GET", "https://api.themoviedb.org/3/tv/8/season/2"),
            ],
        )
        for call in session.calls:
            self.assertEqual(
                call["kwargs"]["params"],
                {
                    "language": "cs-CZ",
                    "api_key": "synthetic-api-key",
                    "session_id": "personal-session",
                },
            )
            self.assertEqual(call["kwargs"]["timeout"], 15)
        self.assertTrue(all(response.closed for response in responses))
        self.assertFalse(os.path.exists(self.genre_path))
        show_data["seasons"][0]["episode_count"] = 999
        episode_data["episodes"][0]["overview"] = "changed"
        self.assertEqual(seasons["seasons"][0]["episode_count"], 3)
        self.assertEqual(episodes["episodes"][0]["overview"], "Děj dílu")
        protocol.make_result("b" * 32, "succeeded", seasons)
        protocol.make_result("c" * 32, "succeeded", episodes)

    def test_season_mapper_rejects_non_dict_rows_and_wrong_field_shapes(self):
        valid_season = {
            "season_number": 2,
            "episode_count": 3,
            "poster_path": "/s.jpg",
        }
        cases = {
            "non-dict-row": ["not-a-season"],
            "title": {"name": {}, "seasons": [valid_season]},
            "original-title": {"original_name": [], "seasons": [valid_season]},
            "season-number": {"seasons": [dict(valid_season, season_number={})]},
            "season-number-nonfinite": {
                "seasons": [dict(valid_season, season_number=float("inf"))]
            },
            "episode-count": {"seasons": [dict(valid_season, episode_count=[])]},
            "episode-count-nonfinite": {
                "seasons": [dict(valid_season, episode_count=float("nan"))]
            },
            "poster-path": {"seasons": [dict(valid_season, poster_path=[])]},
        }
        for label, data in cases.items():
            with self.subTest(field=label):
                engine = self.engine(FakeSession([FakeResponse(json_data=data)]))
                with self.assertRaises(TMDBError):
                    engine.execute(protocol.seasons_request(8, "cs-CZ", BINDING))

    def test_episode_mapper_rejects_non_dict_rows_and_wrong_field_shapes(self):
        valid_episode = {
            "episode_number": 4,
            "name": "Díl",
            "air_date": "2026-11-20",
            "overview": "Děj dílu",
            "vote_average": 8.5,
            "runtime": 50,
            "still_path": "/episode.jpg",
        }
        cases = {
            "non-dict-row": [None],
            "episode-number": [dict(valid_episode, episode_number={})],
            "episode-number-bool": [dict(valid_episode, episode_number=True)],
            "episode-number-nonfinite": [
                dict(valid_episode, episode_number=float("nan"))
            ],
            "name": [dict(valid_episode, name={})],
            "air-date": [dict(valid_episode, air_date={})],
            "overview": [dict(valid_episode, overview=[])],
            "rating": [dict(valid_episode, vote_average=[])],
            "rating-nonfinite-string": [dict(valid_episode, vote_average="NaN")],
            "runtime": [dict(valid_episode, runtime={})],
            "runtime-nonnumeric-string": [dict(valid_episode, runtime="unknown")],
            "runtime-nonfinite": [dict(valid_episode, runtime=float("inf"))],
            "still-path": [dict(valid_episode, still_path=[])],
        }
        for label, episodes in cases.items():
            with self.subTest(field=label):
                engine = self.engine(
                    FakeSession([FakeResponse(json_data={"episodes": episodes})])
                )
                with self.assertRaises(TMDBError):
                    engine.execute(
                        protocol.episodes_request(8, 2, "cs-CZ", BINDING)
                    )

    def test_detail_mappers_preserve_optional_values_and_supported_scalars(self):
        seasons = self.engine(
            FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "name": None,
                            "original_name": None,
                            "seasons": [{"poster_path": None}],
                        }
                    )
                ]
            )
        ).execute(protocol.seasons_request(8, "cs-CZ", BINDING))
        self.assertEqual(
            seasons,
            {
                "title": "",
                "original_title": "",
                "seasons": [
                    {"season_number": 0, "episode_count": 0, "poster_path": None}
                ],
            },
        )

        episodes = self.engine(
            FakeSession(
                [
                    FakeResponse(
                        json_data={
                            "episodes": [
                                {
                                    "episode_number": "4",
                                    "name": None,
                                    "air_date": "not-an-iso-date",
                                    "overview": None,
                                    "vote_average": "8.5",
                                    "runtime": "50",
                                    "still_path": None,
                                },
                                {
                                    "episode_number": 5.0,
                                    "vote_average": "",
                                    "runtime": "",
                                },
                            ]
                        }
                    )
                ]
            )
        ).execute(protocol.episodes_request(8, 2, "cs-CZ", BINDING))
        self.assertEqual(
            episodes,
            {
                "episodes": [
                    {
                        "episode_number": "4",
                        "name": None,
                        "air_date": "not-an-iso-date",
                        "overview": None,
                        "vote_average": "8.5",
                        "runtime": "50",
                        "still_path": None,
                    },
                    {
                        "episode_number": 5.0,
                        "name": None,
                        "air_date": None,
                        "overview": None,
                        "vote_average": "",
                        "runtime": "",
                        "still_path": None,
                    },
                ]
            },
        )

    def test_empty_detail_arrays_succeed_and_malformed_arrays_are_typed_errors(self):
        engine = self.engine(
            FakeSession(
                [
                    FakeResponse(json_data={"name": "Show", "seasons": []}),
                    FakeResponse(json_data={"episodes": []}),
                ]
            )
        )
        self.assertEqual(
            engine.execute(protocol.seasons_request(8, "cs-CZ", BINDING)),
            {"title": "Show", "original_title": "Show", "seasons": []},
        )
        self.assertEqual(
            engine.execute(protocol.episodes_request(8, 2, "cs-CZ", BINDING)),
            {"episodes": []},
        )

        for request, data in (
            (protocol.seasons_request(8, "cs-CZ", BINDING), {"seasons": {}}),
            (protocol.episodes_request(8, 2, "cs-CZ", BINDING), {"episodes": {}}),
        ):
            with self.subTest(operation=request["operation"]):
                with self.assertRaises(TMDBError):
                    self.engine(FakeSession([FakeResponse(json_data=data)])).execute(
                        request
                    )

    def test_real_runner_executes_operation_family_without_parent_transport(self):
        session = FakeSession(
            [
                FakeResponse(json_data={"results": [self.catalog_value("movie")]}),
                self.genre_response("movie"),
                FakeResponse(
                    json_data={
                        "name": "Seriál",
                        "original_name": "Original",
                        "seasons": [],
                    }
                ),
                FakeResponse(json_data={"episodes": []}),
            ]
        )
        store = ServiceStateStore(self.root)
        caller = ServiceStateStore(self.root)

        def forbidden_factory(_session):
            raise AssertionError("another operation family must not be constructed")

        runner = ServiceRunner(
            store,
            forbidden_factory,
            forbidden_factory,
            lambda current: self.engine(current),
            lambda: False,
            lambda _seconds: False,
            logging.getLogger("test.service.operations.runner"),
        )
        self.addCleanup(runner.close)
        self.assertFalse(runner.run_once())
        requests_to_run = (
            protocol.catalog_request("popular_movies", "cs-CZ"),
            protocol.seasons_request(8, "cs-CZ", BINDING),
            protocol.episodes_request(8, 2, "cs-CZ", BINDING),
        )
        rows = [
            caller.join(request, waiter)
            for request, waiter in zip(requests_to_run, WAITERS)
        ]
        with mock.patch(
            "yawsp_app.tmdb_service_worker.requests.Session", return_value=session
        ) as session_factory:
            self.assertTrue(runner.run_once())

        session_factory.assert_called_once_with()
        documents = [
            caller.read_wait_result(row["job_id"], waiter)
            for row, waiter in zip(rows, WAITERS)
        ]
        self.assertEqual([item["outcome"] for item in documents], ["succeeded"] * 3)
        self.assertEqual(documents[0]["value"]["rows"][0]["title"], "Lokalizovaný film")
        self.assertEqual(documents[1]["value"]["original_title"], "Original")
        self.assertEqual(documents[2]["value"], {"episodes": []})
        self.assertEqual(len(session.calls), 4)
        self.assertTrue(session.closed)
        self.assertTrue(store.lease.acquired)
        runner.close()
        self.assertFalse(store.lease.acquired)
        for document in documents:
            encoded = protocol.make_result(
                document["job_id"], document["outcome"], document["value"]
            )
            self.assertEqual(set(encoded), {"job_id", "outcome", "value"})
            self.assertNotIsInstance(document["value"], FakeResponse)

    def test_real_client_public_wrapper_remains_the_public_transport_boundary(self):
        session = FakeSession([FakeResponse(json_data={"results": []})])
        client = public_tmdb_client(
            TMDBClient(
                session,
                self.settings,
                lambda: "cs-CZ",
                identity_lock=ForbiddenIdentityLock(),
            )
        )
        self.assertEqual(client.get("/3/movie/popular"), {"results": []})
        self.assertEqual(self.settings.values["tmdb_access_token"], "personal-session")
        self.assertNotIn("session_id", session.calls[0]["kwargs"]["params"])


if __name__ == "__main__":
    unittest.main()
