"""Service-owned metadata execution and hostile serial-runner regressions."""

import json
import logging
import os
import tempfile
import threading
import unittest
from unittest import mock

import requests
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app import tmdb_service_store as store_module
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.tmdb import TrailerLookup, TrailerRepository
from yawsp_app.tmdb_service_client import TMDBServiceClient
from yawsp_app.tmdb_service_store import ServiceStateStore
from yawsp_app.tmdb_service_worker import ServiceMetadataEngine, ServiceRunner
from tests.support.http_stubs import FakeResponse, FakeSession


EPOCH = "a" * 32
WAITER = "0123456789abcdef0123456789abcdef"
OTHER_WAITER = "1123456789abcdef0123456789abcdef"


class Settings:
    def __init__(self):
        self.values = {
            "tmdb_api_key": "synthetic-api-key",
            "tmdb_access_token": "synthetic-session-token",
            "tmdb_account_id": "42",
        }

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def response(data, status_code=200, headers=None):
    return FakeResponse(json_data=data, status_code=status_code, headers=headers)


def trailer_data(identifier="official001"):
    return {
        "results": [
            {"site": "YouTube", "type": "Teaser", "key": "teaser00000"},
            {"site": "YouTube", "type": "Trailer", "key": "plain000000"},
            {
                "site": "YouTube",
                "type": "Trailer",
                "official": True,
                "key": identifier,
            },
        ]
    }


class DynamicMetadataSession:
    """Create realistic metadata responses without making network requests."""

    def __init__(self, failures=None, on_request=None):
        self.calls = []
        self.responses = []
        self.failures = dict(failures or {})
        self.attempts = {}
        self.on_request = on_request
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        self.attempts[url] = self.attempts.get(url, 0) + 1
        if self.on_request is not None:
            self.on_request(len(self.calls), url)
        failure = self.failures.get(url, 0)
        if self.attempts[url] <= failure:
            raise requests.Timeout("synthetic offline metadata request")
        if url.endswith("/videos"):
            media_id = int(url.rsplit("/", 2)[-2])
            data = trailer_data("video%06d" % media_id)
        else:
            data = {"next_episode_to_air": None}
        result = response(data)
        self.responses.append(result)
        return result

    def close(self):
        self.closed = True


class MetadataEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.trailer_cache = AtomicJsonCache(
            os.path.join(self.temporary.name, "trailers.json")
        )
        self.show_cache = AtomicJsonCache(
            os.path.join(self.temporary.name, "shows.json")
        )
        self.clock = Clock()

    def engine(self, session):
        return ServiceMetadataEngine(
            Settings(),
            self.trailer_cache,
            self.show_cache,
            lambda: "cs-CZ",
            session,
            lambda: False,
            logging.getLogger("test.service.metadata"),
            clock=self.clock,
            sleeper=self.clock.sleep,
        )

    def read_cache(self, cache):
        with open(cache.path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_trailer_and_show_values_use_exact_protocol_domain_shapes(self):
        responses = [
            response(trailer_data("official001")),
            response({"next_episode_to_air": None}),
        ]
        session = FakeSession(responses)
        engine = self.engine(session)

        trailer = engine.execute(protocol.trailer_request("movie", 7))
        show = engine.execute(protocol.show_request(8))

        self.assertEqual(trailer, {"status": "hit", "youtube_id": "official001"})
        self.assertEqual(show, {"value": None, "succeeded": True})
        self.assertIsInstance(TMDBServiceClient._decode_trailer(trailer), TrailerLookup)
        self.assertEqual(TMDBServiceClient._decode_show(show), (None, True))
        self.assertEqual(session.calls[0]["kwargs"]["timeout"], 15)
        self.assertEqual(session.calls[0]["kwargs"]["params"]["language"], "en-US")
        self.assertTrue(all(item.closed for item in responses))
        engine.close()

    def test_genuine_missing_and_none_are_cached_but_errors_are_not(self):
        clock = self.clock
        session = FakeSession(
            [
                response({"results": []}),
                requests.Timeout("offline"),
                response(trailer_data("later000000")),
            ]
        )
        engine = self.engine(session)
        missing = protocol.trailer_request("movie", 3)

        self.assertEqual(
            engine.execute(missing), {"status": "missing", "youtube_id": None}
        )
        clock.now += 6 * 60 * 60 - 1
        self.assertEqual(
            engine.execute(missing), {"status": "missing", "youtube_id": None}
        )
        self.assertEqual(len(session.calls), 1)
        engine.flush()
        self.assertIsNone(self.read_cache(self.trailer_cache)["movie:3"]["youtube_id"])

        failed = protocol.trailer_request("movie", 4)
        with self.assertLogs(engine.logger, level="ERROR"):
            self.assertEqual(
                engine.execute(failed), {"status": "error", "youtube_id": None}
            )
        self.assertNotIn("movie:4", self.read_cache(self.trailer_cache))
        self.assertEqual(
            engine.execute(failed), {"status": "hit", "youtube_id": "later000000"}
        )
        self.assertEqual(len(session.calls), 3)

    def test_show_failure_preserves_stale_value_false_without_negative_timestamp(self):
        stale = {"season": 1, "episode": 2, "air_date": "2026-01-01"}
        timestamp = self.clock() - 24 * 60 * 60 - 1
        self.show_cache.save({"13": {"ts": timestamp, "next": stale}})
        session = FakeSession(
            [
                requests.Timeout("offline"),
                response(
                    {
                        "next_episode_to_air": {
                            "season_number": 2,
                            "episode_number": 3,
                            "air_date": None,
                        }
                    }
                ),
            ]
        )
        engine = self.engine(session)
        request = protocol.show_request(13)

        with self.assertLogs(engine.logger, level="ERROR"):
            self.assertEqual(
                engine.execute(request), {"value": stale, "succeeded": False}
            )
        self.assertEqual(
            self.read_cache(self.show_cache)["13"], {"ts": timestamp, "next": stale}
        )
        refreshed = {"season": 2, "episode": 3, "air_date": ""}
        self.assertEqual(
            engine.execute(request), {"value": refreshed, "succeeded": True}
        )
        engine.flush()
        self.assertEqual(
            self.read_cache(self.show_cache)["13"]["next"], refreshed
        )

    def test_service_migration_is_the_only_fresh_legacy_timestamp_writer(self):
        self.trailer_cache.save({"movie:5": "legacy00001"})
        session = FakeSession([response(trailer_data("migrated001"))])
        engine = self.engine(session)

        self.assertEqual(
            engine.execute(protocol.trailer_request("movie", 5)),
            {"status": "hit", "youtube_id": "migrated001"},
        )
        engine.close()

        migrated = self.read_cache(self.trailer_cache)["movie:5"]
        self.assertEqual(migrated["youtube_id"], "migrated001")
        self.assertEqual(migrated["ts"], self.clock())
        independent = TrailerRepository(
            None,
            AtomicJsonCache(self.trailer_cache.path),
            clock=self.clock,
            _read_only=True,
        )
        self.assertFalse(independent.needs_lookup("movie", 5))
        ordinary_hit = self.engine(FakeSession())
        self.assertEqual(
            ordinary_hit.execute(protocol.trailer_request("movie", 5)),
            {"status": "hit", "youtube_id": "migrated001"},
        )
        ordinary_hit.close()

    def test_transport_retries_retry_after_and_every_response_close_are_preserved(self):
        responses = [
            response({}, status_code=429, headers={"Retry-After": "1"}),
            response({}, status_code=429, headers={"Retry-After": "1"}),
            response({"next_episode_to_air": None}),
        ]
        session = FakeSession(responses)
        engine = self.engine(session)

        self.assertEqual(
            engine.execute(protocol.show_request(21)),
            {"value": None, "succeeded": True},
        )

        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sum(self.clock.sleeps), 2)
        self.assertTrue(all(wait <= 0.25 for wait in self.clock.sleeps))
        self.assertTrue(all(item.closed for item in responses))
        self.assertTrue(all(call["kwargs"]["timeout"] == 15 for call in session.calls))

    def test_nested_transport_context_and_logs_exclude_credentials(self):
        api_key = "synthetic-nested-api-key"
        session_token = "synthetic-nested-session-token"
        client_name = "synthetic-nested-client"
        settings = Settings()
        settings.set("tmdb_api_key", api_key)
        settings.set("tmdb_access_token", session_token)
        prepared = requests.Request(
            "GET",
            "https://api.themoviedb.org/3/movie/1/videos",
            params={"api_key": api_key, "session_id": session_token},
        ).prepare()
        failure = requests.Timeout("secret-bearing timeout", request=prepared)
        failure.__context__ = MaxRetryError(
            client_name, prepared.url, reason=ConnectTimeoutError()
        )
        nested_context = failure.__context__
        session = FakeSession([failure])
        engine = ServiceMetadataEngine(
            settings,
            self.trailer_cache,
            self.show_cache,
            lambda: "en-US",
            session,
            lambda: False,
            logging.getLogger("test.service.redaction"),
            clock=self.clock,
            sleeper=self.clock.sleep,
        )

        with self.assertLogs(engine.logger, level="ERROR") as logged:
            result = engine.execute(protocol.trailer_request("movie", 1))

        self.assertEqual(result, {"status": "error", "youtube_id": None})
        rendered = "\n".join(logged.output)
        for secret in (api_key, session_token, client_name):
            self.assertIn(secret, str(nested_context))
            self.assertNotIn(secret, rendered)
        self.assertIsNone(failure.__context__)
        self.assertFalse(os.path.exists(self.trailer_cache.path))

    def test_eight_successes_checkpoint_and_close_flushes_final_success(self):
        session = FakeSession(
            [response({"next_episode_to_air": None}) for _ in range(9)]
        )
        engine = self.engine(session)

        with mock.patch("yawsp_app.json_cache.os.fsync", wraps=os.fsync) as fsync:
            for show_id in range(1, 9):
                engine.execute(protocol.show_request(show_id))
            self.assertEqual(fsync.call_count, 1)
            engine.execute(protocol.show_request(9))
            self.assertEqual(fsync.call_count, 1)
            engine.close()
            self.assertEqual(fsync.call_count, 2)
        self.assertEqual(set(self.read_cache(self.show_cache)), {str(i) for i in range(1, 10)})


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name
        self.addCleanup(self.temporary.cleanup)
        self.trailer_cache = AtomicJsonCache(os.path.join(self.root, "trailers.json"))
        self.show_cache = AtomicJsonCache(os.path.join(self.root, "shows.json"))
        self.clock = Clock()
        self.store = ServiceStateStore(self.root)
        self.caller = ServiceStateStore(self.root)
        self.abort = threading.Event()
        self.waits = []
        self.runners = []

    def wait_for_abort(self, seconds):
        self.waits.append(seconds)
        return self.abort.is_set()

    def metadata_factory(self, session):
        return ServiceMetadataEngine(
            Settings(),
            self.trailer_cache,
            self.show_cache,
            lambda: "en-US",
            session,
            self.abort.is_set,
            logging.getLogger("test.service.runner.metadata"),
            clock=self.clock,
            sleeper=self.clock.sleep,
        )

    def make_runner(self, session, metadata_factory=None, operation_factory=None):
        patcher = mock.patch(
            "yawsp_app.tmdb_service_worker.requests.Session", return_value=session
        )
        session_constructor = patcher.start()
        self.addCleanup(patcher.stop)
        runner = ServiceRunner(
            self.store,
            metadata_factory or self.metadata_factory,
            None,
            operation_factory,
            self.abort.is_set,
            self.wait_for_abort,
            logging.getLogger("test.service.runner"),
        )
        self.runners.append(runner)
        self.addCleanup(runner.close)
        self.assertFalse(runner.run_once(), "empty startup drain must not do work")
        session_constructor.assert_not_called()
        return runner, session_constructor

    def manifest(self):
        with open(
            os.path.join(self.root, "tmdb_service_state", "manifest.json"),
            encoding="utf-8",
        ) as handle:
            return json.load(handle)

    def result_document(self, job_id):
        path = os.path.join(
            self.root, "tmdb_service_state", "results", job_id + ".json"
        )
        with open(path, "rb") as handle:
            return protocol.validate_result(handle.read())

    def submit(self, requests):
        rows = self.caller.submit(requests)
        self.assertTrue(all(row["status"] == "accepted" for row in rows))
        return rows

    def test_forty_keys_drain_once_per_serial_cycle_with_one_closed_session(self):
        session = DynamicMetadataSession()
        runner, constructor = self.make_runner(session)
        rows = self.submit(
            [protocol.trailer_request("movie", number) for number in range(1, 41)]
        )

        self.assertTrue(runner.run_once())
        self.assertFalse(runner.run_once())

        constructor.assert_called_once_with()
        self.assertTrue(session.closed)
        self.assertEqual(len(session.calls), 40)
        self.assertEqual(len({call["url"] for call in session.calls}), 40)
        self.assertTrue(all(item.closed for item in session.responses))
        states = self.manifest()["jobs"] + self.manifest()["tombstones"]
        self.assertEqual({entry["job_id"] for entry in states}, {row["job_id"] for row in rows})
        self.assertFalse(
            [job for job in self.manifest()["jobs"] if job["state"] in ("queued", "backlog", "active")]
        )

    def test_background_trailer_and_show_jobs_round_robin_deterministically(self):
        session = DynamicMetadataSession()
        runner, _ = self.make_runner(session)
        self.submit(
            [protocol.trailer_request("tv", number) for number in range(1, 4)]
            + [protocol.show_request(number) for number in range(101, 104)]
        )

        runner.run_once()

        paths = [call["url"].replace("https://api.themoviedb.org", "") for call in session.calls]
        self.assertEqual(
            paths,
            [
                "/3/tv/1/videos",
                "/3/tv/101",
                "/3/tv/2/videos",
                "/3/tv/102",
                "/3/tv/3/videos",
                "/3/tv/103",
            ],
        )

    def test_interactive_job_preempts_only_after_active_response_completes(self):
        interactive = []
        observed = []

        def on_request(count, url):
            observed.append((count, url, list(interactive)))
            if count == 1:
                interactive.extend(
                    self.caller.join(
                        protocol.show_request(500), OTHER_WAITER
                    )[key]
                    for key in ("status", "job_id")
                )

        session = DynamicMetadataSession(on_request=on_request)
        runner, _ = self.make_runner(session)
        self.submit(
            [protocol.trailer_request("movie", 1), protocol.trailer_request("movie", 2)]
        )

        runner.run_once()

        paths = [call["url"] for call in session.calls]
        self.assertTrue(paths[0].endswith("/3/movie/1/videos"))
        self.assertTrue(paths[1].endswith("/3/tv/500"))
        self.assertTrue(paths[2].endswith("/3/movie/2/videos"))
        self.assertEqual(len(observed[1:]), 2)

    def test_overlapping_multi_key_batches_execute_only_unique_owned_jobs(self):
        other_caller = ServiceStateStore(self.root)
        session = DynamicMetadataSession()
        runner, _ = self.make_runner(session)
        shared = [protocol.trailer_request("movie", number) for number in range(1, 11)]
        left = self.caller.submit(
            shared + [protocol.trailer_request("movie", number) for number in range(11, 21)]
        )
        right = other_caller.submit(
            list(reversed(shared))
            + [protocol.trailer_request("movie", number) for number in range(21, 31)]
        )

        owners = {}
        for row in left + right:
            key = protocol.dedup_key(row["request"])
            if key in owners:
                self.assertEqual(row["job_id"], owners[key])
                self.assertEqual(row["status"], "joined")
            else:
                owners[key] = row["job_id"]
        self.assertEqual(len(owners), 30)

        runner.run_once()

        self.assertEqual(len(session.calls), 30)
        self.assertEqual(len({call["url"] for call in session.calls}), 30)

    def test_metadata_failure_gets_one_deferred_retry_and_no_negative_cache(self):
        url = "https://api.themoviedb.org/3/tv/42"
        session = DynamicMetadataSession(failures={url: 100})
        runner, _ = self.make_runner(session)
        row = self.caller.join(protocol.show_request(42), WAITER)
        self.assertEqual(row["status"], "accepted")

        with self.assertLogs("test.service.runner.metadata", level="ERROR"):
            runner.run_once()

        document = self.caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(document["job_id"], row["job_id"])
        self.assertEqual(
            TMDBServiceClient._decode_show(document["value"]), (None, False)
        )
        self.assertEqual(session.attempts[url], 2)
        self.assertEqual(self.waits, [0.05])
        self.assertFalse(os.path.exists(self.show_cache.path))

    def test_transient_failure_retries_once_then_publishes_correlated_success(self):
        url = "https://api.themoviedb.org/3/movie/42/videos"
        session = DynamicMetadataSession(failures={url: 1})
        runner, _ = self.make_runner(session)
        row = self.caller.join(protocol.trailer_request("movie", 42), WAITER)

        with self.assertLogs("test.service.runner.metadata", level="ERROR"):
            runner.run_once()

        document = self.caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(document["outcome"], "succeeded")
        self.assertEqual(document["job_id"], row["job_id"])
        self.assertEqual(
            TMDBServiceClient._decode_trailer(document["value"]),
            TrailerLookup("hit", "video000042"),
        )
        self.assertEqual(session.attempts[url], 2)
        self.assertEqual(self.waits, [0.05])

    def test_post_terminal_cleanup_fault_neither_rejects_nor_repeats_terminal(self):
        session = DynamicMetadataSession()
        runner, _ = self.make_runner(session)
        rows = self.submit(
            [protocol.trailer_request("movie", 1), protocol.show_request(2)]
        )
        original_complete = self.store.complete
        completions = []
        rejects = []

        def complete_then_cleanup_fault(claim, document):
            published = original_complete(claim, document)
            completions.append((claim["job_id"], published["outcome"]))
            raise OSError("post-terminal temp cleanup denied")

        self.store.complete = complete_then_cleanup_fault
        self.store.abort_claim = lambda claim, code: rejects.append((claim, code))

        with self.assertLogs("test.service.runner", level="ERROR"):
            self.assertTrue(runner.run_once())

        self.assertEqual(len(completions), 2)
        self.assertEqual(rejects, [])
        for row in rows:
            self.assertEqual(self.result_document(row["job_id"])["outcome"], "succeeded")
        self.assertFalse(runner.run_once())
        self.assertEqual(len(completions), 2)

    def test_precommit_temp_fault_reconciles_active_claim_and_continues(self):
        session = DynamicMetadataSession()
        runner, _ = self.make_runner(session)
        first = self.caller.join(protocol.show_request(1), WAITER)
        second = self.caller.join(protocol.show_request(2), OTHER_WAITER)
        original_temp = store_module._temp_file
        attempts = []

        def fail_first_result_temp(path):
            if os.path.basename(os.path.dirname(path)) == "results" and not attempts:
                attempts.append(path)
                raise OSError("injected precommit result fault")
            return original_temp(path)

        with mock.patch.object(store_module, "_temp_file", side_effect=fail_first_result_temp):
            self.assertTrue(runner.run_once())

        self.assertEqual(len(attempts), 1)
        first_result = self.caller.read_wait_result(first["job_id"], WAITER)
        second_result = self.caller.read_wait_result(second["job_id"], OTHER_WAITER)
        self.assertEqual(first_result["outcome"], "failed")
        self.assertEqual(first_result["error"]["code"], "BACKPRESSURED")
        self.assertEqual(second_result["outcome"], "succeeded")
        self.assertEqual(len(session.calls), 2)
        self.assertFalse([job for job in self.manifest()["jobs"] if job["state"] == "active"])
        self.assertFalse([job for job in self.manifest()["jobs"] if job["state"] in ("queued", "backlog", "active")])
        self.assertFalse(runner.run_once())

    def test_saturated_pinned_results_reconcile_runner_and_next_job(self):
        session = DynamicMetadataSession()
        runner, _ = self.make_runner(session)
        target = 16777216 - 16384
        retained = []
        index = 0
        while True:
            aggregate = sum(
                job["result_ref"]["bytes"] for job in self.manifest()["jobs"]
            )
            if aggregate >= target:
                break
            row = self.caller.submit(
                [protocol.trailer_request("movie", index + 100)],
                interactive=True,
                waiter_id="%032x" % (index + 100),
            )[0]
            claim = self.store.claim_next(
                runner._epoch, "%032x" % (index + 1000)
            )
            empty = protocol.make_result(row["job_id"], "succeeded", {"value": ""})
            envelope = len(
                json.dumps(empty, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            length = min(1048400, target - aggregate - envelope)
            self.assertGreater(length, 0)
            document = protocol.make_result(
                row["job_id"], "succeeded", {"value": "x" * length}
            )
            self.store.complete(claim, document)
            retained.append((row, index, length))
            index += 1

        first = self.caller.join(protocol.show_request(1), WAITER)
        second = self.caller.join(protocol.show_request(2), OTHER_WAITER)
        self.assertTrue(runner.run_once())

        for row, waiter, _length in retained:
            self.assertIsNotNone(
                self.caller.read_wait_result(
                    row["job_id"], "%032x" % (waiter + 100)
                )
            )
        outcomes = [
            self.caller.read_wait_result(row["job_id"], waiter)
            for row, waiter in ((first, WAITER), (second, OTHER_WAITER))
        ]
        self.assertEqual(
            [document["outcome"] for document in outcomes], ["succeeded", "failed"]
        )
        self.assertEqual(outcomes[1]["error"]["code"], "BACKPRESSURED")
        self.assertEqual(len(session.calls), 2)
        self.assertFalse([job for job in self.manifest()["jobs"] if job["state"] in ("queued", "backlog", "active")])
        self.assertFalse(runner.run_once())

    def test_base_exception_unwinds_with_one_terminal_final_flush_and_session_close(self):
        class FatalMetadata(BaseException):
            pass

        real_factory = self.metadata_factory

        def factory(session):
            real = real_factory(session)

            class Engine:
                logger = real.logger

                def execute(self, request):
                    if request["operation"] == "metadata.show" and request["params"]["show_id"] == 2:
                        raise FatalMetadata()
                    return real.execute(request)

                def flush(self):
                    return real.flush()

                def close(self):
                    return real.close()

            return Engine()

        session = DynamicMetadataSession()
        runner, _ = self.make_runner(session, metadata_factory=factory)
        rows = self.submit([protocol.show_request(1), protocol.show_request(2)])
        original_abort = self.store.abort_claim
        aborts = []

        def counted_abort(claim, code):
            aborts.append((claim["job_id"], code))
            return original_abort(claim, code)

        self.store.abort_claim = counted_abort
        with mock.patch.object(
            self.show_cache, "save_merged", wraps=self.show_cache.save_merged
        ) as save_merged:
            with self.assertLogs("test.service.runner", level="ERROR"):
                with self.assertRaises(FatalMetadata):
                    runner.run_once()

        self.assertEqual(save_merged.call_count, 1)
        self.assertTrue(session.closed)
        self.assertEqual(len(aborts), 1)
        self.assertEqual(aborts[0], (rows[1]["job_id"], "FAILED"))
        self.assertEqual(self.result_document(rows[0]["job_id"])["outcome"], "succeeded")
        self.assertEqual(self.result_document(rows[1]["job_id"])["outcome"], "failed")
        self.assertIn("1", self.read_cache_keys(self.show_cache))
        self.assertFalse(runner.run_once())
        self.assertEqual(len(aborts), 1)

    def test_poison_job_is_failed_without_blocking_unrelated_metadata(self):
        real_factory = self.metadata_factory

        def factory(session):
            real = real_factory(session)

            class Engine:
                logger = real.logger

                def execute(self, request):
                    if request["operation"] == "metadata.trailer" and request["params"]["media_id"] == 1:
                        raise RuntimeError("poison metadata implementation")
                    return real.execute(request)

                def flush(self):
                    return real.flush()

                def close(self):
                    return real.close()

            return Engine()

        session = DynamicMetadataSession()
        runner, _ = self.make_runner(session, metadata_factory=factory)
        rows = self.submit(
            [protocol.trailer_request("movie", 1), protocol.trailer_request("movie", 2)]
        )

        with self.assertLogs("test.service.runner", level="ERROR"):
            runner.run_once()

        first = self.result_document(rows[0]["job_id"])
        second = self.result_document(rows[1]["job_id"])
        self.assertEqual(first["outcome"], "failed")
        self.assertEqual(first["error"], {"code": "FAILED", "message": "TMDB operation failed"})
        self.assertEqual(second["outcome"], "succeeded")
        self.assertEqual(second["value"]["youtube_id"], "video000002")

    def test_abort_flushes_completed_success_aborts_queue_and_closes_session(self):
        session = DynamicMetadataSession()
        self.abort.set()
        self.abort.clear()
        runner, _ = self.make_runner(session)
        rows = self.submit([protocol.show_request(1), protocol.show_request(2)])
        original_request = session.request

        def abort_after_first_complete(method, url, **kwargs):
            result = original_request(method, url, **kwargs)
            self.abort.set()
            return result

        session.request = abort_after_first_complete
        with mock.patch.object(
            self.show_cache, "save_merged", wraps=self.show_cache.save_merged
        ) as save_merged:
            self.assertTrue(runner.run_once())

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.closed)
        self.assertEqual(save_merged.call_count, 1)
        self.assertEqual(self.read_cache_keys(self.show_cache), {"1"})
        self.assertEqual(self.result_document(rows[0]["job_id"])["outcome"], "aborted")
        self.assertEqual(self.result_document(rows[1]["job_id"])["outcome"], "aborted")

    def test_domain_results_decode_through_reviewed_client_shapes(self):
        session = DynamicMetadataSession()
        runner, _ = self.make_runner(session)
        trailer = self.caller.join(protocol.trailer_request("movie", 9), WAITER)
        show = self.caller.join(protocol.show_request(10), OTHER_WAITER)

        runner.run_once()

        trailer_result = self.caller.read_wait_result(trailer["job_id"], WAITER)
        show_result = self.caller.read_wait_result(show["job_id"], OTHER_WAITER)
        self.assertEqual(trailer_result["job_id"], trailer["job_id"])
        self.assertEqual(show_result["job_id"], show["job_id"])
        self.assertEqual(
            TMDBServiceClient._decode_trailer(trailer_result["value"]),
            TrailerLookup("hit", "video000009"),
        )
        self.assertEqual(TMDBServiceClient._decode_show(show_result["value"]), (None, True))
        self.assertEqual(set(trailer_result), {"job_id", "outcome", "value"})
        self.assertNotIsInstance(trailer_result["value"], tuple)

    def test_run_until_abort_uses_injected_wait_and_releases_owner_resources(self):
        session = DynamicMetadataSession()
        waits = []

        def stop_after_idle(seconds):
            waits.append(seconds)
            self.abort.set()
            return True

        runner = ServiceRunner(
            self.store,
            self.metadata_factory,
            None,
            None,
            self.abort.is_set,
            stop_after_idle,
            logging.getLogger("test.service.runner"),
        )
        self.addCleanup(runner.close)

        runner.run_until_abort()

        self.assertGreaterEqual(len(waits), 1)
        self.assertTrue(all(wait == 0.05 for wait in waits))
        self.assertEqual(session.calls, [])
        self.assertEqual(self.manifest()["service_state"], "stopping")
        self.assertFalse(self.store.lease.acquired)

    @staticmethod
    def read_cache_keys(cache):
        with open(cache.path, encoding="utf-8") as handle:
            return set(json.load(handle))


if __name__ == "__main__":
    unittest.main()
