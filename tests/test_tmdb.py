from email.utils import formatdate
import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.support.kodi_stubs import KodiRecorder
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.tmdb import (
    BackgroundPrefetch,
    ShowRepository,
    TMDBAbortError,
    TMDBClient,
    TMDBError,
    TrailerLookup,
    TrailerRepository,
)


class MemorySettings:
    def __init__(self, **values):
        self.values = {
            "tmdb_api_key": "api-key",
            "tmdb_access_token": "session-token",
            "tmdb_account_id": "old-account",
        }
        self.values.update(values)

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = float(now)
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class InlineWorker:
    def __init__(self):
        self.calls = 0

    def submit(self, function, *args):
        self.calls += 1
        function(*args)


class ManualWorker:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        self.jobs.append((function, args))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for function, args in jobs:
            function(*args)


class ShutdownWorker(ManualWorker):
    def __init__(self):
        super().__init__()
        self.shutdown_calls = []

    def shutdown(self, wait=True):
        self.shutdown_calls.append(wait)


def json_response(data, status_code=200, headers=None):
    return FakeResponse(json_data=data, status_code=status_code, headers=headers)


class TMDBServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.trailer_cache = AtomicJsonCache(
            os.path.join(self.temporary_directory.name, "trailers.json")
        )
        self.show_cache = AtomicJsonCache(
            os.path.join(self.temporary_directory.name, "shows.json")
        )

    def make_client(self, responses, settings=None, clock=None, abort=None):
        session = FakeSession(responses)
        settings = settings or MemorySettings()
        clock = clock or FakeClock()
        client = TMDBClient(
            session,
            settings,
            locale_mapper=lambda: "cs-CZ",
            clock=clock,
            sleeper=clock.sleep,
            abort=abort,
        )
        return client, session, settings, clock

    def read_cache(self, cache):
        with open(cache.path, encoding="utf-8") as stream:
            return json.load(stream)


class ClientTests(TMDBServiceTestCase):
    def test_request_uses_mapped_language_auth_parameters_and_explicit_override(self):
        client, session, _, _ = self.make_client(
            [json_response({"ok": True}), json_response({"ok": True})]
        )

        client.get("/3/example")
        client.get("/3/example", {"language": "en-US"})

        first = session.calls[0]
        self.assertEqual(first["method"], "GET")
        self.assertEqual(first["url"], "https://api.themoviedb.org/3/example")
        self.assertEqual(first["kwargs"]["timeout"], 15)
        self.assertEqual(first["kwargs"]["params"]["language"], "cs-CZ")
        self.assertEqual(first["kwargs"]["params"]["api_key"], "api-key")
        self.assertEqual(first["kwargs"]["params"]["session_id"], "session-token")
        self.assertEqual(session.calls[1]["kwargs"]["params"]["language"], "en-US")

    def test_transport_and_http_errors_are_chained_domain_errors(self):
        for failure in (requests.Timeout("slow"), json_response({}, status_code=500)):
            with self.subTest(failure=failure):
                client, _, _, _ = self.make_client([failure])
                with self.assertRaises(TMDBError) as caught:
                    client.get("/3/example")
                self.assertIsInstance(caught.exception.__cause__, requests.RequestException)

    def test_error_responses_close_but_success_remains_open_for_parsing(self):
        for status_code, count in ((500, 1), (401, 1), (429, 3)):
            with self.subTest(status_code=status_code):
                responses = [json_response({}, status_code=status_code) for _ in range(count)]
                client, _, _, _ = self.make_client(responses)

                with self.assertRaises(TMDBError):
                    client.request("GET", "/3/example")

                self.assertTrue(all(response.closed for response in responses))

        success = json_response({"ok": True})
        client, _, _, _ = self.make_client([success])
        response = client.request("GET", "/3/example")
        self.assertIs(response, success)
        self.assertFalse(response.closed)
        self.assertEqual(response.json(), {"ok": True})
        response.close()

    def test_malformed_success_json_is_a_chained_domain_error(self):
        client, _, _, _ = self.make_client([FakeResponse(content="not-json")])

        with self.assertRaises(TMDBError) as caught:
            client.get("/3/example")

        self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_retry_after_integer_seconds_is_not_capped_to_five(self):
        clock = FakeClock()
        client, session, _, _ = self.make_client(
            [
                json_response({}, status_code=429, headers={"Retry-After": "12"}),
                json_response({"ok": True}),
            ],
            clock=clock,
        )

        self.assertEqual(client.get("/3/example"), {"ok": True})

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sum(clock.sleeps), 12)
        self.assertTrue(all(wait <= 0.25 for wait in clock.sleeps))

    def test_retry_after_http_date_uses_the_injected_clock(self):
        clock = FakeClock()
        retry_at = formatdate(clock() + 9, usegmt=True)
        client, session, _, _ = self.make_client(
            [
                json_response({}, status_code=429, headers={"Retry-After": retry_at}),
                json_response({"ok": True}),
            ],
            clock=clock,
        )

        client.get("/3/example")

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sum(clock.sleeps), 9)

    def test_invalid_retry_after_uses_bounded_exponential_fallback_and_exhaustion_errors(self):
        for retry_after in (None, "invalid", "-2", "9" * 400):
            with self.subTest(retry_after=retry_after):
                headers = {} if retry_after is None else {"Retry-After": retry_after}
                clock = FakeClock()
                client, session, _, _ = self.make_client(
                    [json_response({}, status_code=429, headers=headers)] * 3,
                    clock=clock,
                )

                with self.assertRaises(TMDBError) as caught:
                    client.get("/3/example")

                self.assertEqual(len(session.calls), 3)
                self.assertEqual(sum(clock.sleeps), 3)
                self.assertIsInstance(caught.exception.__cause__, requests.HTTPError)

    def test_abort_interrupts_wait_without_another_request(self):
        clock = FakeClock()
        aborted = threading.Event()

        def aborting_sleep(seconds):
            clock.sleep(seconds)
            aborted.set()

        session = FakeSession(
            [
                json_response({}, status_code=429, headers={"Retry-After": "60"}),
                json_response({"ok": True}),
            ]
        )
        client = TMDBClient(
            session,
            MemorySettings(),
            locale_mapper=lambda: "en-US",
            clock=clock,
            sleeper=aborting_sleep,
            abort=aborted.is_set,
        )

        with self.assertRaises(TMDBAbortError):
            client.get("/3/example")

        self.assertEqual(len(session.calls), 1)
        self.assertLess(clock(), 1_700_000_001)

    def test_identity_invalidation_forces_new_session_account_resolution_and_persistence(self):
        settings = MemorySettings(tmdb_account_id="old-account")
        client, session, _, _ = self.make_client(
            [json_response({"id": 42, "username": "new-user"})], settings=settings
        )

        client.invalidate_identity()
        settings.set("tmdb_access_token", "new-session")

        self.assertEqual(settings.get("tmdb_account_id"), "")
        self.assertEqual(client.get_account_id(), "42")
        self.assertEqual(settings.get("tmdb_account_id"), "42")
        self.assertEqual(session.calls[0]["kwargs"]["params"]["session_id"], "new-session")

    def test_account_id_is_resolved_only_once_until_identity_is_invalidated(self):
        client, session, _, _ = self.make_client(
            [json_response({"id": "account-1"})],
            settings=MemorySettings(tmdb_account_id=""),
        )

        self.assertEqual(client.get_account_id(), "account-1")
        self.assertEqual(client.get_account_id(), "account-1")
        self.assertEqual(len(session.calls), 1)

    def test_session_invalidation_clears_session_and_account_identity(self):
        settings = MemorySettings()
        client, _, _, _ = self.make_client([], settings=settings)

        client.invalidate_session()

        self.assertEqual(settings.get("tmdb_access_token"), "")
        self.assertEqual(settings.get("tmdb_account_id"), "")

    def test_http_401_invalidates_session_and_account_identity(self):
        settings = MemorySettings()
        client, _, _, _ = self.make_client(
            [json_response({}, status_code=401)], settings=settings
        )

        with self.assertRaises(TMDBError):
            client.get("/3/example")

        self.assertEqual(settings.get("tmdb_access_token"), "")
        self.assertEqual(settings.get("tmdb_account_id"), "")

    def test_missing_account_id_is_an_error_and_does_not_persist_empty_identity(self):
        settings = MemorySettings(tmdb_account_id="")
        client, _, _, _ = self.make_client([json_response({"username": "nobody"})], settings)

        with self.assertRaises(TMDBError):
            client.get_account_id()

        self.assertEqual(settings.get("tmdb_account_id"), "")


class TrailerRepositoryTests(TMDBServiceTestCase):
    def test_official_youtube_trailer_is_preferred_and_returns_exact_hit_status(self):
        client, session, _, _ = self.make_client(
            [
                json_response(
                    {
                        "results": [
                            {"site": "YouTube", "type": "Teaser", "key": "teaser"},
                            {"site": "YouTube", "type": "Trailer", "key": "plain"},
                            {
                                "site": "YouTube",
                                "type": "Trailer",
                                "official": True,
                                "key": "official",
                            },
                        ]
                    }
                )
            ]
        )

        result = TrailerRepository(client, self.trailer_cache).lookup("movie", 1)

        self.assertEqual(result, TrailerLookup(status="hit", youtube_id="official"))
        self.assertEqual(session.calls[0]["kwargs"]["params"]["language"], "en-US")
        self.assertEqual(self.read_cache(self.trailer_cache)["movie:1"]["youtube_id"], "official")

    def test_non_trailer_youtube_video_is_the_final_fallback(self):
        client, _, _, _ = self.make_client(
            [json_response({"results": [{"site": "YouTube", "type": "Featurette", "key": "fallback"}]})]
        )

        result = TrailerRepository(client, self.trailer_cache).lookup("tv", 8)

        self.assertEqual(result, TrailerLookup("hit", "fallback"))

    def test_success_without_youtube_video_is_cached_as_missing_for_six_hours(self):
        clock = FakeClock()
        client, session, _, _ = self.make_client(
            [json_response({"id": 3, "results": []})], clock=clock
        )
        repository = TrailerRepository(client, self.trailer_cache, clock=clock)

        self.assertEqual(repository.lookup("movie", 3), TrailerLookup("missing", None))
        clock.now += 6 * 60 * 60 - 1
        self.assertEqual(repository.lookup("movie", 3), TrailerLookup("missing", None))
        self.assertEqual(len(session.calls), 1)
        entry = self.read_cache(self.trailer_cache)["movie:3"]
        self.assertIsNone(entry["youtube_id"])
        self.assertEqual(entry["ts"], 1_700_000_000)

    def test_valid_trailer_is_cached_for_seven_days(self):
        clock = FakeClock()
        client, session, _, _ = self.make_client(
            [
                json_response(
                    {"results": [{"site": "YouTube", "type": "Trailer", "key": "cached"}]}
                ),
                json_response({"results": []}),
            ],
            clock=clock,
        )
        repository = TrailerRepository(client, self.trailer_cache, clock=clock)

        self.assertEqual(repository.lookup("movie", 9).status, "hit")
        clock.now += 7 * 24 * 60 * 60 - 1
        self.assertEqual(repository.lookup("movie", 9), TrailerLookup("hit", "cached"))
        self.assertEqual(len(session.calls), 1)
        clock.now += 2
        self.assertEqual(repository.lookup("movie", 9), TrailerLookup("missing", None))
        self.assertEqual(len(session.calls), 2)

    def test_legacy_valid_id_is_usable_but_legacy_none_is_discarded(self):
        self.trailer_cache.save({"movie:1": "legacy-valid", "movie:2": None})
        client, session, _, _ = self.make_client([json_response({"id": 2, "results": []})])
        repository = TrailerRepository(client, self.trailer_cache)

        self.assertEqual(repository.lookup("movie", 1), TrailerLookup("hit", "legacy-valid"))
        self.assertEqual(repository.lookup("movie", 2), TrailerLookup("missing", None))
        self.assertEqual(len(session.calls), 1)

    def test_legacy_valid_id_is_migrated_so_its_ttl_survives_new_repositories(self):
        clock = FakeClock()
        self.trailer_cache.save({"movie:4": "legacy-valid"})
        client, first_session, _, _ = self.make_client([], clock=clock)
        first_repository = TrailerRepository(client, self.trailer_cache, clock=clock)

        self.assertEqual(first_repository.lookup("movie", 4), TrailerLookup("hit", "legacy-valid"))
        self.assertEqual(first_session.calls, [])
        self.assertEqual(
            self.read_cache(self.trailer_cache)["movie:4"],
            {"ts": 1_700_000_000, "youtube_id": "legacy-valid"},
        )

        clock.now += 7 * 24 * 60 * 60 + 1
        next_client, second_session, _, _ = self.make_client(
            [json_response({"id": 4, "results": []})], clock=clock
        )
        second_repository = TrailerRepository(next_client, self.trailer_cache, clock=clock)
        self.assertEqual(second_repository.lookup("movie", 4), TrailerLookup("missing", None))
        self.assertEqual(len(second_session.calls), 1)

    def test_transport_http_and_retry_exhaustion_return_error_without_negative_cache(self):
        failures = (
            requests.Timeout("offline"),
            json_response({}, status_code=401),
            json_response({}, status_code=429),
        )
        for index, failure in enumerate(failures):
            with self.subTest(failure=failure):
                clock = FakeClock()
                responses = [failure]
                if isinstance(failure, FakeResponse) and failure.status_code == 429:
                    responses = [failure] * 3
                responses.append(
                    json_response(
                        {"results": [{"site": "YouTube", "type": "Trailer", "key": "later"}]}
                    )
                )
                cache = AtomicJsonCache(
                    os.path.join(self.temporary_directory.name, "trailers-{0}.json".format(index))
                )
                client, session, _, _ = self.make_client(responses, clock=clock)
                repository = TrailerRepository(client, cache, clock=clock)

                with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
                    self.assertEqual(repository.lookup("movie", 5), TrailerLookup("error", None))
                self.assertFalse(os.path.exists(cache.path))
                self.assertEqual(repository.lookup("movie", 5), TrailerLookup("hit", "later"))

    def test_expired_trailer_does_not_masquerade_as_a_hit_when_refresh_fails(self):
        clock = FakeClock()
        self.trailer_cache.save(
            {"movie:7": {"ts": clock() - 7 * 24 * 60 * 60 - 1, "youtube_id": "stale"}}
        )
        client, _, _, _ = self.make_client([requests.Timeout("offline")], clock=clock)
        repository = TrailerRepository(client, self.trailer_cache, clock=clock)

        with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
            self.assertEqual(repository.lookup("movie", 7), TrailerLookup("error", None))

    def test_video_with_missing_youtube_id_is_an_error_not_a_genuine_negative(self):
        client, _, _, _ = self.make_client(
            [json_response({"results": [{"site": "YouTube", "type": "Trailer"}]})]
        )
        repository = TrailerRepository(client, self.trailer_cache)

        with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
            self.assertEqual(repository.lookup("movie", 10), TrailerLookup("error", None))
        self.assertFalse(os.path.exists(self.trailer_cache.path))


class ShowRepositoryTests(TMDBServiceTestCase):
    def test_next_episode_is_mapped_and_cached_for_twenty_four_hours(self):
        clock = FakeClock()
        client, session, _, _ = self.make_client(
            [
                json_response(
                    {
                        "next_episode_to_air": {
                            "season_number": 2,
                            "episode_number": 4,
                            "air_date": "2026-12-01",
                            "name": "Episode",
                        }
                    }
                )
            ],
            clock=clock,
        )
        repository = ShowRepository(client, self.show_cache, clock=clock)
        expected = {"season": 2, "episode": 4, "air_date": "2026-12-01"}

        self.assertEqual(repository.next_episode(11), expected)
        clock.now += 24 * 60 * 60 - 1
        self.assertEqual(repository.next_episode(11), expected)
        self.assertEqual(len(session.calls), 1)

    def test_genuine_none_is_a_success_cached_for_twenty_four_hours(self):
        clock = FakeClock()
        client, session, _, _ = self.make_client(
            [json_response({"id": 12, "next_episode_to_air": None})], clock=clock
        )
        repository = ShowRepository(client, self.show_cache, clock=clock)

        self.assertIsNone(repository.next_episode(12))
        clock.now += 24 * 60 * 60 - 1
        self.assertIsNone(repository.next_episode(12))
        self.assertEqual(len(session.calls), 1)
        self.assertIsNone(self.read_cache(self.show_cache)["12"]["next"])

    def test_malformed_show_payloads_are_logged_not_cached_and_remain_retryable(self):
        malformed_payloads = (
            {},
            {"next_episode_to_air": {}},
            {"next_episode_to_air": {"season_number": 1}},
            {"next_episode_to_air": {"episode_number": 2}},
            {"next_episode_to_air": {"season_number": None, "episode_number": 2}},
            {"next_episode_to_air": {"season_number": 1, "episode_number": None}},
            {"next_episode_to_air": {"season_number": "1", "episode_number": 2}},
            {"next_episode_to_air": {"season_number": 1, "episode_number": "2"}},
            {"next_episode_to_air": {"season_number": -1, "episode_number": 2}},
            {"next_episode_to_air": {"season_number": 1, "episode_number": 0}},
        )
        for index, malformed in enumerate(malformed_payloads):
            with self.subTest(payload=malformed):
                clock = FakeClock()
                show_id = 40 + index
                valid_payload = {
                    "next_episode_to_air": {
                        "season_number": 2,
                        "episode_number": 3,
                        "air_date": "2026-03-01",
                    }
                }
                client, session, _, _ = self.make_client(
                    [json_response(malformed), json_response(valid_payload)], clock=clock
                )
                cache = AtomicJsonCache(
                    os.path.join(self.temporary_directory.name, "malformed-{0}.json".format(index))
                )
                repository = ShowRepository(client, cache, clock=clock)

                with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
                    self.assertIsNone(repository.next_episode(show_id))
                self.assertFalse(os.path.exists(cache.path))

                self.assertEqual(
                    repository.next_episode(show_id),
                    {"season": 2, "episode": 3, "air_date": "2026-03-01"},
                )
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(
                    self.read_cache(cache)[str(show_id)]["next"],
                    {"season": 2, "episode": 3, "air_date": "2026-03-01"},
                )

    def test_malformed_air_date_preserves_stale_fallback_and_remains_retryable(self):
        for index, malformed_date in enumerate((20261201, {"year": 2026})):
            with self.subTest(air_date=malformed_date):
                clock = FakeClock()
                show_id = 70 + index
                stale = {"season": 1, "episode": 2, "air_date": "2026-01-01"}
                old_timestamp = clock() - 24 * 60 * 60 - 1
                cache = AtomicJsonCache(
                    os.path.join(
                        self.temporary_directory.name, "invalid-date-{0}.json".format(index)
                    )
                )
                cache.save({str(show_id): {"ts": old_timestamp, "next": stale}})
                refreshed = {"season": 2, "episode": 3, "air_date": "2026-03-01"}
                client, session, _, _ = self.make_client(
                    [
                        json_response(
                            {
                                "next_episode_to_air": {
                                    "season_number": 2,
                                    "episode_number": 3,
                                    "air_date": malformed_date,
                                }
                            }
                        ),
                        json_response(
                            {
                                "next_episode_to_air": {
                                    "season_number": 2,
                                    "episode_number": 3,
                                    "air_date": "2026-03-01",
                                }
                            }
                        ),
                    ],
                    clock=clock,
                )
                repository = ShowRepository(client, cache, clock=clock)

                with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
                    self.assertEqual(repository.next_episode(show_id), stale)
                self.assertEqual(
                    self.read_cache(cache)[str(show_id)],
                    {"ts": old_timestamp, "next": stale},
                )
                self.assertEqual(repository.next_episode(show_id), refreshed)
                self.assertEqual(len(session.calls), 2)

    def test_poisoned_persisted_air_date_is_invalidated_and_retried(self):
        for index, poisoned_date in enumerate((20261201, {"year": 2026})):
            with self.subTest(air_date=poisoned_date):
                clock = FakeClock()
                show_id = 80 + index
                cache = AtomicJsonCache(
                    os.path.join(
                        self.temporary_directory.name, "poisoned-date-{0}.json".format(index)
                    )
                )
                cache.save(
                    {
                        str(show_id): {
                            "ts": clock(),
                            "next": {
                                "season": 1,
                                "episode": 2,
                                "air_date": poisoned_date,
                            },
                        }
                    }
                )
                client, session, _, _ = self.make_client(
                    [
                        json_response(
                            {
                                "next_episode_to_air": {
                                    "season_number": 3,
                                    "episode_number": 4,
                                    "air_date": None,
                                }
                            }
                        )
                    ],
                    clock=clock,
                )
                repository = ShowRepository(client, cache, clock=clock)

                self.assertEqual(
                    repository.next_episode(show_id),
                    {"season": 3, "episode": 4, "air_date": ""},
                )
                self.assertEqual(len(session.calls), 1)

    def test_malformed_show_payload_preserves_stale_fallback_and_original_timestamp(self):
        clock = FakeClock()
        stale = {"season": 1, "episode": 2, "air_date": "2026-01-01"}
        old_timestamp = clock() - 24 * 60 * 60 - 1
        self.show_cache.save({"55": {"ts": old_timestamp, "next": stale}})
        refreshed = {"season": 1, "episode": 3, "air_date": "2026-02-01"}
        client, session, _, _ = self.make_client(
            [
                json_response({}),
                json_response(
                    {
                        "next_episode_to_air": {
                            "season_number": 1,
                            "episode_number": 3,
                            "air_date": "2026-02-01",
                        }
                    }
                ),
            ],
            clock=clock,
        )
        repository = ShowRepository(client, self.show_cache, clock=clock)

        with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
            self.assertEqual(repository.next_episode(55), stale)
        self.assertEqual(
            self.read_cache(self.show_cache)["55"],
            {"ts": old_timestamp, "next": stale},
        )

        self.assertEqual(repository.next_episode(55), refreshed)
        self.assertEqual(len(session.calls), 2)

    def test_failed_refresh_returns_stale_show_without_refreshing_timestamp_and_remains_retryable(self):
        clock = FakeClock()
        stale = {"season": 1, "episode": 2, "air_date": "2026-01-01"}
        old_timestamp = clock() - 24 * 60 * 60 - 1
        self.show_cache.save({"13": {"ts": old_timestamp, "next": stale}})
        refreshed = {"season": 1, "episode": 3, "air_date": "2026-02-01"}
        client, session, _, _ = self.make_client(
            [
                requests.Timeout("offline"),
                json_response(
                    {
                        "next_episode_to_air": {
                            "season_number": 1,
                            "episode_number": 3,
                            "air_date": "2026-02-01",
                        }
                    }
                ),
            ],
            clock=clock,
        )
        repository = ShowRepository(client, self.show_cache, clock=clock)

        with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
            self.assertEqual(repository.next_episode(13), stale)
        self.assertEqual(self.read_cache(self.show_cache)["13"]["ts"], old_timestamp)
        clock.now += 20
        self.assertEqual(repository.next_episode(13), refreshed)
        self.assertEqual(len(session.calls), 2)

    def test_failures_do_not_create_show_error_entries(self):
        for failure in (requests.Timeout("offline"), json_response({}, status_code=401)):
            with self.subTest(failure=failure):
                client, _, _, _ = self.make_client([failure])
                repository = ShowRepository(client, self.show_cache)

                with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
                    self.assertIsNone(repository.next_episode(14))
                self.assertFalse(os.path.exists(self.show_cache.path))


class DeferredPersistenceConcurrencyTests(TMDBServiceTestCase):
    def show_response(self, season):
        return json_response(
            {
                "next_episode_to_air": {
                    "season_number": season,
                    "episode_number": 1,
                }
            }
        )

    def test_checkpoint_merge_preserves_unrelated_immediate_on_demand_entry(self):
        clock = FakeClock()
        worker_client, _, _, _ = self.make_client(
            [self.show_response(season) for season in range(1, 9)], clock=clock
        )
        worker = ShowRepository(
            worker_client, self.show_cache, clock=clock, _defer_save=True
        )
        on_demand_client, _, _, _ = self.make_client(
            [self.show_response(9)], clock=clock
        )
        on_demand = ShowRepository(on_demand_client, self.show_cache, clock=clock)

        for show_id in range(1, 8):
            self.assertEqual(
                worker.next_episode(show_id),
                {"season": show_id, "episode": 1, "air_date": ""},
            )
        self.assertFalse(os.path.exists(self.show_cache.path))
        self.assertEqual(
            on_demand.next_episode(9),
            {"season": 9, "episode": 1, "air_date": ""},
        )

        self.assertEqual(
            worker.next_episode(8),
            {"season": 8, "episode": 1, "air_date": ""},
        )

        self.assertEqual(
            set(self.read_cache(self.show_cache)),
            {str(show_id) for show_id in range(1, 10)},
        )

    def test_final_merge_preserves_unrelated_immediate_on_demand_entry(self):
        clock = FakeClock()
        worker_client, _, _, _ = self.make_client([self.show_response(1)], clock=clock)
        worker = ShowRepository(
            worker_client, self.show_cache, clock=clock, _defer_save=True
        )
        on_demand_client, _, _, _ = self.make_client(
            [self.show_response(2)], clock=clock
        )
        on_demand = ShowRepository(on_demand_client, self.show_cache, clock=clock)

        worker.next_episode(1)
        on_demand.next_episode(2)
        self.assertEqual(set(self.read_cache(self.show_cache)), {"2"})

        worker._flush()

        self.assertEqual(set(self.read_cache(self.show_cache)), {"1", "2"})

    def test_deferred_value_cannot_replace_newer_timestamped_on_demand_value(self):
        clock = FakeClock()
        worker_client, _, _, _ = self.make_client([self.show_response(1)], clock=clock)
        worker = ShowRepository(
            worker_client, self.show_cache, clock=clock, _defer_save=True
        )
        worker.next_episode(1)
        worker_timestamp = clock()

        clock.now += 60
        on_demand_client, _, _, _ = self.make_client(
            [self.show_response(9)], clock=clock
        )
        on_demand = ShowRepository(on_demand_client, self.show_cache, clock=clock)
        expected = {"season": 9, "episode": 1, "air_date": ""}
        self.assertEqual(on_demand.next_episode(1), expected)

        worker._flush()

        persisted = self.read_cache(self.show_cache)["1"]
        self.assertEqual(persisted["next"], expected)
        self.assertEqual(persisted["ts"], clock())
        self.assertGreater(persisted["ts"], worker_timestamp)


class BackgroundPrefetchTests(TMDBServiceTestCase):
    def make_background(self, sessions, worker, settings=None, abort=None, max_pending=32):
        session_iter = iter(sessions)
        created = []

        def session_factory():
            session = next(session_iter)
            created.append(session)
            return session

        background = BackgroundPrefetch(
            settings=settings or MemorySettings(tmdb_account_id="42"),
            trailer_cache=self.trailer_cache,
            show_cache=self.show_cache,
            locale_mapper=lambda: "cs-CZ",
            session_factory=session_factory,
            worker=worker,
            abort=abort,
            clock=FakeClock(),
            max_pending=max_pending,
        )
        return background, created

    def test_trailer_submission_is_nonblocking_deduplicated_and_persists_worker_results(self):
        sessions = [
            FakeSession(
                [
                    json_response(
                        {"results": [{"site": "YouTube", "type": "Trailer", "key": "one"}]}
                    ),
                    json_response({"id": 2, "results": []}),
                ]
            ),
            FakeSession(
                [json_response({"results": [{"site": "YouTube", "type": "Trailer", "key": "three"}]})]
            ),
        ]
        worker = ManualWorker()
        background, created = self.make_background(sessions, worker)

        self.assertEqual(background.submit_trailers("movie", [1, 1, 2]), 2)
        self.assertEqual(background.submit_trailers("movie", [1, 2, 3]), 1)
        self.assertEqual(created, [])
        self.assertFalse(os.path.exists(self.trailer_cache.path))

        worker.run_all()

        self.assertEqual(
            self.read_cache(self.trailer_cache)["movie:1"]["youtube_id"], "one"
        )
        self.assertIsNone(self.read_cache(self.trailer_cache)["movie:2"]["youtube_id"])
        self.assertEqual(
            self.read_cache(self.trailer_cache)["movie:3"]["youtube_id"], "three"
        )
        self.assertTrue(all(session.closed for session in created))
        self.assertEqual(background.submit_trailers("movie", [1, 2, 3]), 0)

    def test_inline_worker_makes_submission_deterministic_and_saves_show_results(self):
        session = FakeSession(
            [json_response({"next_episode_to_air": {"season_number": 3, "episode_number": 2}})]
        )
        worker = InlineWorker()
        background, created = self.make_background([session], worker)

        self.assertEqual(background.submit_shows([21, 21]), 1)

        self.assertEqual(worker.calls, 1)
        self.assertEqual(created, [session])
        self.assertTrue(session.closed)
        self.assertEqual(
            self.read_cache(self.show_cache)["21"]["next"],
            {"season": 3, "episode": 2, "air_date": ""},
        )

    def test_pending_work_is_bounded(self):
        worker = ManualWorker()
        background, _ = self.make_background([], worker, max_pending=2)

        self.assertEqual(background.submit_shows([1, 2, 3]), 2)
        self.assertEqual(background.submit_shows([4]), 0)
        self.assertEqual(len(worker.jobs), 1)

    def test_aborted_before_worker_start_closes_owned_session_without_http(self):
        aborted = threading.Event()
        aborted.set()
        session = FakeSession([])
        worker = ManualWorker()
        background, created = self.make_background([session], worker, abort=aborted.is_set)
        background.submit_shows([30])

        worker.run_all()

        self.assertEqual(created, [session])
        self.assertEqual(session.calls, [])
        self.assertTrue(session.closed)

    def test_close_shuts_down_shared_executor_once_and_rejects_later_submissions(self):
        worker = ShutdownWorker()
        background, _ = self.make_background([], worker)

        background.close(wait=False)
        background.close(wait=False)

        self.assertEqual(worker.shutdown_calls, [False])
        self.assertEqual(background.submit_shows([31]), 0)
        self.assertEqual(worker.jobs, [])

    def test_close_prevents_queued_http_work_while_owned_sessions_still_close(self):
        session = FakeSession(
            [json_response({"id": 30, "next_episode_to_air": None})]
        )
        worker = ShutdownWorker()
        background, created = self.make_background([session], worker)
        background.submit_shows([30])

        background.close(wait=False)
        worker.run_all()

        self.assertEqual(session.calls, [])
        self.assertEqual(created, [session])
        self.assertTrue(session.closed)

    def test_worker_cache_writes_coalesce_at_eight_validated_successes(self):
        expected = {
            ("trailers", 1): 1,
            ("trailers", 8): 1,
            ("trailers", 32): 4,
            ("shows", 1): 1,
            ("shows", 8): 1,
            ("shows", 32): 4,
        }
        actual = {}
        for kind, count in expected:
            with self.subTest(kind=kind, count=count):
                cache = AtomicJsonCache(
                    os.path.join(
                        self.temporary_directory.name,
                        "{0}-{1}.json".format(kind, count),
                    )
                )
                unused_cache = self.show_cache if kind == "trailers" else self.trailer_cache
                responses = [
                    json_response(
                        {
                            "results": [
                                {
                                    "site": "YouTube",
                                    "type": "Trailer",
                                    "key": "video-{0}".format(index),
                                }
                            ]
                        }
                        if kind == "trailers"
                        else {"next_episode_to_air": None}
                    )
                    for index in range(count)
                ]
                worker = InlineWorker()
                background = BackgroundPrefetch(
                    settings=MemorySettings(tmdb_account_id="42"),
                    trailer_cache=cache if kind == "trailers" else unused_cache,
                    show_cache=cache if kind == "shows" else unused_cache,
                    locale_mapper=lambda: "en-US",
                    session_factory=lambda: FakeSession(responses),
                    worker=worker,
                    clock=FakeClock(),
                )
                with patch("yawsp_app.json_cache.os.fsync", wraps=os.fsync) as fsync:
                    if kind == "trailers":
                        accepted = background.submit_trailers(
                            "movie", range(1, count + 1)
                        )
                    else:
                        accepted = background.submit_shows(range(1, count + 1))
                self.assertEqual(accepted, count)
                actual[(kind, count)] = fsync.call_count

        self.assertEqual(actual, expected)

    def test_missing_known_skips_discovery_reads_without_changing_validation_or_dedup(self):
        worker = ManualWorker()
        background, _ = self.make_background([], worker)

        with patch.object(self.show_cache, "load", wraps=self.show_cache.load) as load:
            accepted = background.submit_shows(
                [21, "21", "invalid", 22], missing_known=True
            )

        self.assertEqual(accepted, 2)
        self.assertEqual(load.call_count, 0)
        self.assertEqual(len(worker.jobs), 1)
        self.assertEqual(background.submit_shows([21, 22], missing_known=True), 0)

    def test_worker_flushes_completed_successes_when_aborted(self):
        session = FakeSession(
            [json_response({"next_episode_to_air": None}) for _ in range(3)]
        )
        worker = ManualWorker()
        background, _ = self.make_background(
            [session], worker, abort=lambda: len(session.calls) >= 2
        )
        background.submit_shows([31, 32, 33], missing_known=True)

        with patch("yawsp_app.json_cache.os.fsync", wraps=os.fsync) as fsync:
            worker.run_all()

        self.assertEqual(fsync.call_count, 1)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(set(self.read_cache(self.show_cache)), {"31", "32"})
        self.assertTrue(session.closed)

    def test_worker_flushes_prior_successes_after_unexpected_job_failure(self):
        session = FakeSession([json_response({"next_episode_to_air": None})])
        abort_checks = []

        def failing_abort():
            abort_checks.append(True)
            if len(abort_checks) == 3:
                raise RuntimeError("abort predicate failed unexpectedly")
            return False

        worker = ManualWorker()
        background, _ = self.make_background([session], worker, abort=failing_abort)
        background.submit_shows([34, 35], missing_known=True)

        with self.assertLogs("yawsp_app.tmdb", level="ERROR"):
            with patch("yawsp_app.json_cache.os.fsync", wraps=os.fsync) as fsync:
                worker.run_all()

        self.assertEqual(fsync.call_count, 1)
        self.assertEqual(set(self.read_cache(self.show_cache)), {"34"})
        self.assertTrue(session.closed)
        self.assertEqual(background.submit_shows([35], missing_known=True), 1)

    def test_direct_lookup_persistence_remains_immediate(self):
        client, _, _, _ = self.make_client(
            [json_response({"next_episode_to_air": None})]
        )
        repository = ShowRepository(client, self.show_cache)

        with patch("yawsp_app.json_cache.os.fsync", wraps=os.fsync) as fsync:
            self.assertIsNone(repository.next_episode(36))
            self.assertEqual(self.read_cache(self.show_cache)["36"]["next"], None)
            self.assertEqual(fsync.call_count, 1)

    def test_no_ui_refresh_is_performed_by_background_completion(self):
        # BackgroundPrefetch has no plugin/UI argument or refresh callback; completion only updates JSON.
        session = FakeSession([json_response({"id": 31, "next_episode_to_air": None})])
        worker = InlineWorker()
        background, _ = self.make_background([session], worker)

        background.submit_shows([31])

        self.assertEqual(self.read_cache(self.show_cache)["31"]["next"], None)

    def test_real_background_completion_does_not_call_kodi_or_plugin_ui(self):
        session = FakeSession([json_response({"id": 32, "next_episode_to_air": None})])
        worker = InlineWorker()
        background, created = self.make_background([session], worker)
        kodi = KodiRecorder()
        plugin = KodiRecorder()
        kodi_module = SimpleNamespace(executebuiltin=kodi.executeBuiltin)

        with patch.dict(
            sys.modules, {"xbmc": kodi_module, "xbmcplugin": plugin}
        ):
            self.assertEqual(background.submit_shows([32]), 1)

        self.assertEqual(self.read_cache(self.show_cache)["32"]["next"], None)
        self.assertEqual(created, [session])
        self.assertTrue(session.closed)
        self.assertEqual(kodi.builtin_commands, [])
        self.assertEqual(kodi.calls, [])
        self.assertEqual(plugin.calls, [])


if __name__ == "__main__":
    unittest.main()
