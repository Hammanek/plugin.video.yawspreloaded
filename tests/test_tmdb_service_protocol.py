"""Closed, detached and secret-free TMDB service protocol contracts."""

import copy
import hashlib
import json
import math
import unittest

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.watchlist_snapshot import SnapshotScope


JOB_ID = "0123456789abcdef0123456789abcdef"
BINDING = "v1:" + "a" * 64


class RequestBuilderTests(unittest.TestCase):
    def test_trailer_builder_normalizes_media_and_id(self):
        self.assertEqual(
            protocol.trailer_request(" movie ", "042"),
            {
                "operation": "metadata.trailer",
                "params": {"media": "movie", "media_id": 42},
            },
        )

    def test_show_builder_normalizes_show_id(self):
        self.assertEqual(
            protocol.show_request("42"),
            {"operation": "metadata.show", "params": {"show_id": 42}},
        )

    def test_watchlist_builder_normalizes_scope_and_keeps_intents_separate(self):
        scope = SnapshotScope(" 42 ", "movies", " cs-CZ ")
        self.assertEqual(
            protocol.watchlist_request(scope, 3, True, False, BINDING),
            {
                "operation": "watchlist.refresh",
                "params": {
                    "scope": {
                        "account_id": "42",
                        "media": "movies",
                        "language": "cs-CZ",
                        "schema": 1,
                    }
                },
                "generation": 3,
                "force": True,
                "allow_missing_account": False,
                "session_binding": BINDING,
            },
        )

    def test_watched_builder_contains_scope_media_and_binding(self):
        scope = SnapshotScope("42", "tv", "en-US")
        self.assertEqual(
            protocol.watched_request(scope, "7", BINDING),
            {
                "operation": "watchlist.watched",
                "params": {
                    "scope": {
                        "account_id": "42",
                        "media": "tv",
                        "language": "en-US",
                        "schema": 1,
                    },
                    "media_id": 7,
                },
                "session_binding": BINDING,
            },
        )

    def test_public_and_detail_builders_use_only_named_operations(self):
        cases = (
            (
                protocol.catalog_request(" popular_movies ", " en-US "),
                {
                    "operation": "discovery.catalog",
                    "params": {"source": "popular_movies", "language": "en-US"},
                },
            ),
            (
                protocol.seasons_request("42", " cs-CZ ", BINDING),
                {
                    "operation": "details.seasons",
                    "params": {"show_id": 42, "language": "cs-CZ"},
                    "session_binding": BINDING,
                },
            ),
            (
                protocol.episodes_request("42", "2", " en-US ", BINDING),
                {
                    "operation": "details.episodes",
                    "params": {"show_id": 42, "season": 2, "language": "en-US"},
                    "session_binding": BINDING,
                },
            ),
            (
                protocol.episodes_request("42", "00", " en-US ", BINDING),
                {
                    "operation": "details.episodes",
                    "params": {"show_id": 42, "season": 0, "language": "en-US"},
                    "session_binding": BINDING,
                },
            ),
            (
                protocol.invalidate_identity_request(" 42 ", BINDING),
                {
                    "operation": "identity.invalidate",
                    "params": {"account_id": "42"},
                    "session_binding": BINDING,
                },
            ),
        )
        for actual, expected in cases:
            with self.subTest(operation=expected["operation"]):
                self.assertEqual(actual, expected)
                self.assertNotIn("job_id", actual)

    def test_catalog_builder_admits_exactly_the_four_local_sources(self):
        for source in (
            "popular_movies",
            "popular_shows",
            "top_rated_movies",
            "top_rated_shows",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    protocol.catalog_request(source, "en-US")["params"]["source"],
                    source,
                )
        for source in ("", "trending", "/movie/popular", "popular_movies?x=1"):
            with self.subTest(source=source):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.catalog_request(source, "en-US")

    def test_builders_return_detached_plain_dictionaries(self):
        original = {
            "account_id": "42",
            "media": "movies",
            "language": "cs-CZ",
            "schema": 1,
        }
        request = protocol.watchlist_request(original, 0, False, True, BINDING)
        original["account_id"] = "changed"
        original["media"] = "tv"
        self.assertEqual(request["params"]["scope"]["account_id"], "42")
        self.assertEqual(request["params"]["scope"]["media"], "movies")
        self.assertIs(type(request), dict)
        self.assertIs(type(request["params"]), dict)
        self.assertIs(type(request["params"]["scope"]), dict)

    def test_builder_rejects_noncanonical_numeric_and_secret_inputs(self):
        scope = SnapshotScope("42", "movies", "cs-CZ")
        cases = (
            lambda: protocol.trailer_request("../movie", 42),
            lambda: protocol.trailer_request("movie", True),
            lambda: protocol.trailer_request("movie", 0),
            lambda: protocol.trailer_request("movie", 1.5),
            lambda: protocol.show_request(-1),
            lambda: protocol.watchlist_request(scope, True, False, False, BINDING),
            lambda: protocol.watchlist_request(scope, -1, False, False, BINDING),
            lambda: protocol.watchlist_request(scope, 0, 1, False, BINDING),
            lambda: protocol.watchlist_request(scope, 0, False, 0, BINDING),
            lambda: protocol.watchlist_request(scope, 0, False, False, "raw-token"),
            lambda: protocol.episodes_request(42, True, "en-US", BINDING),
            lambda: protocol.episodes_request(42, -1, "en-US", BINDING),
            lambda: protocol.invalidate_identity_request("", BINDING),
        )
        for build in cases:
            with self.subTest(build=build):
                with self.assertRaises(protocol.ProtocolError):
                    build()

    def test_builders_reject_nonfinite_identity_numerics(self):
        def builds(value):
            account_scope = {
                "account_id": value,
                "media": "movies",
                "language": "cs-CZ",
                "schema": 1,
            }
            language_scope = {
                "account_id": "42",
                "media": "tv",
                "language": value,
                "schema": 1,
            }
            return (
                (
                    "catalog language",
                    lambda: protocol.catalog_request("popular_movies", value),
                ),
                (
                    "seasons language",
                    lambda: protocol.seasons_request(42, value, BINDING),
                ),
                (
                    "episodes language",
                    lambda: protocol.episodes_request(42, 1, value, BINDING),
                ),
                (
                    "watchlist account",
                    lambda: protocol.watchlist_request(
                        account_scope, 0, False, False, BINDING
                    ),
                ),
                (
                    "watched account",
                    lambda: protocol.watched_request(account_scope, 42, BINDING),
                ),
                (
                    "watchlist language",
                    lambda: protocol.watchlist_request(
                        language_scope, 0, False, False, BINDING
                    ),
                ),
                (
                    "watched language",
                    lambda: protocol.watched_request(language_scope, 42, BINDING),
                ),
                (
                    "identity account",
                    lambda: protocol.invalidate_identity_request(value, BINDING),
                ),
            )

        for value in (float("nan"), float("inf"), float("-inf")):
            for name, build in builds(value):
                with self.subTest(path=name, value=value):
                    with self.assertRaises(protocol.ProtocolError):
                        build()

    def test_builders_preserve_finite_numeric_identity_normalization(self):
        scope = {
            "account_id": 1.5,
            "media": "movies",
            "language": 2.5,
            "schema": 1,
        }
        self.assertEqual(
            protocol.catalog_request("popular_movies", 1.5)["params"]["language"],
            "1.5",
        )
        self.assertEqual(
            protocol.seasons_request(42, 1.5, BINDING)["params"]["language"],
            "1.5",
        )
        self.assertEqual(
            protocol.episodes_request(42, 1, 1.5, BINDING)["params"]["language"],
            "1.5",
        )
        for request in (
            protocol.watchlist_request(scope, 0, False, False, BINDING),
            protocol.watched_request(scope, 42, BINDING),
        ):
            self.assertEqual(request["params"]["scope"]["account_id"], "1.5")
            self.assertEqual(request["params"]["scope"]["language"], "2.5")
        self.assertEqual(
            protocol.invalidate_identity_request(1.5, BINDING)["params"]["account_id"],
            "1.5",
        )


class DedupAndJobCodecTests(unittest.TestCase):
    def test_dedup_key_uses_canonical_prefixed_utf8_json(self):
        request = {
            "params": {"media_id": 42, "media": "movie"},
            "operation": "metadata.trailer",
        }
        canonical = (
            b'tmdb-service-v1\x00'
            b'{"operation":"metadata.trailer","params":{"media":"movie","media_id":42}}'
        )
        self.assertEqual(
            protocol.dedup_key(request), hashlib.sha256(canonical).hexdigest()
        )
        self.assertEqual(
            protocol.dedup_key(dict(reversed(list(request.items())))),
            hashlib.sha256(canonical).hexdigest(),
        )

    def test_watchlist_force_generation_and_binding_all_change_dedup(self):
        scope = SnapshotScope("42", "movies", "cs-CZ")
        baseline = protocol.watchlist_request(scope, 1, False, False, BINDING)
        variants = (
            protocol.watchlist_request(scope, 1, True, False, BINDING),
            protocol.watchlist_request(scope, 2, False, False, BINDING),
            protocol.watchlist_request(scope, 1, False, True, BINDING),
            protocol.watchlist_request(
                scope, 1, False, False, "v1:" + "b" * 64
            ),
        )
        keys = {protocol.dedup_key(baseline)}
        for variant in variants:
            with self.subTest(request=variant):
                key = protocol.dedup_key(variant)
                self.assertNotIn(key, keys)
                keys.add(key)

    def test_validate_job_round_trips_a_detached_plain_document(self):
        request = protocol.watchlist_request(
            SnapshotScope("42", "movies", "cs-CZ"), 0, False, True, BINDING
        )
        validated = protocol.validate_job(request)
        self.assertEqual(validated, request)
        self.assertIsNot(validated, request)
        self.assertIsNot(validated["params"], request["params"])
        self.assertIsNot(validated["params"]["scope"], request["params"]["scope"])
        validated["params"]["scope"]["language"] = "en-US"
        self.assertEqual(request["params"]["scope"]["language"], "cs-CZ")

    def test_validate_job_accepts_strict_json_and_rejects_duplicate_fields(self):
        request = protocol.trailer_request("movie", 42)
        encoded = json.dumps(request, separators=(",", ":"))
        self.assertEqual(protocol.validate_job(encoded), request)
        duplicates = (
            '{"operation":"metadata.show","operation":"metadata.trailer",'
            '"params":{"show_id":42}}',
            '{"operation":"metadata.show","params":{"show_id":42,"show_id":43}}',
            '{"operation":"metadata.trailer","params":'
            '{"media":"movie","media":"tv","media_id":42}}',
        )
        for document in duplicates:
            with self.subTest(document=document):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_job(document)

    def test_validate_job_rejects_unknown_executable_or_secret_fields(self):
        additions = (
            ("job_id", JOB_ID),
            ("method", "GET"),
            ("url", "https://example.invalid/"),
            ("path", "/3/movie/42"),
            ("callable", len),
            ("callback", len),
            ("api_key", "secret"),
            ("session_id", "secret"),
            ("access_token", "secret"),
        )
        for name, value in additions:
            with self.subTest(field=name):
                request = protocol.trailer_request("movie", 42)
                request[name] = value
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_job(request)

    def test_raw_job_documents_must_use_builder_canonical_identity_text(self):
        documents = (
            {
                "operation": "metadata.trailer",
                "params": {"media": " movie ", "media_id": 42},
            },
            {
                "operation": "discovery.catalog",
                "params": {"source": " popular_movies ", "language": "en-US"},
            },
            {
                "operation": "discovery.catalog",
                "params": {"source": "popular_movies", "language": " en-US "},
            },
            {
                "operation": "identity.invalidate",
                "params": {"account_id": " 42 "},
                "session_binding": BINDING,
            },
        )
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_job(document)
                with self.assertRaises(protocol.ProtocolError):
                    protocol.dedup_key(document)

    def test_validate_job_rejects_wrong_operation_parameters(self):
        documents = [
            {"operation": "os.system", "params": {}},
            {"operation": "metadata.show", "params": {"media_id": 42}},
            {"operation": "metadata.show", "params": {"show_id": True}},
            {"operation": "metadata.show", "params": {"show_id": 0}},
            {
                "operation": "details.episodes",
                "params": {"show_id": 42, "season": 1, "language": "en-US", "url": "x"},
                "session_binding": BINDING,
            },
            {
                "operation": "watchlist.refresh",
                "params": {
                    "scope": {
                        "account_id": "42",
                        "media": "movies",
                        "language": "cs-CZ",
                        "schema": True,
                    }
                },
                "generation": 0,
                "force": False,
                "allow_missing_account": False,
                "session_binding": BINDING,
            },
        ]
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_job(document)

    def test_validate_job_rejects_nonfinite_bool_and_malformed_documents(self):
        documents = [
            None,
            [],
            len,
            b"\xff",
            "not json",
            "{} trailing",
            '{"operation":"metadata.show","params":{"show_id":NaN}}',
            '{"operation":"metadata.show","params":{"show_id":1e999}}',
        ]
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_job(document)

    def test_unicode_must_be_valid_scalar_text(self):
        for document in (
            protocol.catalog_request("popular_movies", "en-US"),
            protocol.invalidate_identity_request("42", BINDING),
        ):
            field = "language" if "language" in document["params"] else "account_id"
            document["params"][field] = "bad\ud800text"
            with self.subTest(field=field):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_job(document)
        with self.assertRaises(protocol.ProtocolError):
            protocol.catalog_request("popular_movies", "\udfff")
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_job(
                '{"operation":"metadata.show","params":{"show_id":42},'
                '"x":"\\ud800"}'
            )

    def test_dedup_rejects_malformed_roots_as_protocol_errors(self):
        documents = (
            None,
            [],
            {"operation": [], "params": {}},
            {"operation": "metadata.show"},
        )
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.dedup_key(document)

    def test_request_canonical_form_is_bounded(self):
        request = protocol.catalog_request("popular_movies", "x" * 262145)
        with self.assertRaises(protocol.ProtocolError):
            protocol.dedup_key(request)


class ResultCodecTests(unittest.TestCase):
    def test_trailer_hit_missing_and_error_values_round_trip_detached(self):
        values = (
            {"status": "hit", "youtube_id": "abc123"},
            {"status": "missing", "youtube_id": None},
            {"status": "error", "youtube_id": None},
        )
        for value in values:
            with self.subTest(value=value):
                result = protocol.make_result(JOB_ID, "succeeded", value)
                self.assertEqual(
                    result,
                    {"job_id": JOB_ID, "outcome": "succeeded", "value": value},
                )
                validated = protocol.validate_result(result)
                self.assertEqual(validated, result)
                self.assertIsNot(validated["value"], result["value"])
                validated["value"]["status"] = "changed"
                self.assertEqual(result["value"]["status"], value["status"])

    def test_trailer_result_rejects_inconsistent_payloads(self):
        values = (
            {"status": "hit", "youtube_id": None},
            {"status": "missing", "youtube_id": "abc123"},
            {"status": "other", "youtube_id": None},
            {"status": "hit", "youtube_id": "abc123", "response": "secret"},
            {"status": "hit", "youtube_id": "abc123", "response": object()},
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_result(
                        protocol.make_result(JOB_ID, "succeeded", value)
                        if value["status"] in ("hit", "missing", "other")
                        and "response" not in value
                        else {"job_id": JOB_ID, "outcome": "succeeded", "value": value}
                    )

    def test_show_result_keeps_value_and_real_success_flag(self):
        for value, succeeded in (({"season": 2, "episode": 3}, True), (None, False)):
            with self.subTest(value=value, succeeded=succeeded):
                payload = {"value": value, "succeeded": succeeded}
                result = protocol.make_result(JOB_ID, "succeeded", payload)
                self.assertEqual(protocol.validate_result(result)["value"], payload)

    def test_show_result_rejects_extra_fields(self):
        result = {
            "job_id": JOB_ID,
            "outcome": "succeeded",
            "value": {"value": None, "succeeded": False, "raw": "response"},
        }
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_result(result)

    def test_watchlist_result_is_scope_revision_generation_without_rows(self):
        scope = {
            "account_id": "42",
            "media": "movies",
            "language": "cs-CZ",
            "schema": 1,
        }
        value = {"scope": scope, "revision": 9, "generation": 4}
        result = protocol.make_result(JOB_ID, "succeeded", value)
        self.assertEqual(protocol.validate_result(result), result)
        for extra in ({"results": []}, {"rows": []}, {"snapshot": {}}):
            invalid = copy.deepcopy(result)
            invalid["value"].update(extra)
            with self.subTest(extra=extra):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_result(invalid)

    def test_result_errors_have_fixed_sanitized_codes_and_messages(self):
        expected = (
            ("FAILED", "TMDB operation failed"),
            ("ABORTED", "TMDB operation aborted"),
            ("UNAVAILABLE", "TMDB service unavailable"),
            ("BACKPRESSURED", "TMDB service backpressured"),
            ("SHUTTING_DOWN", "TMDB service shutting down"),
            ("RESULT_EXPIRED", "TMDB service result expired"),
        )
        for code, message in expected:
            outcome = "aborted" if code == "ABORTED" else "failed"
            with self.subTest(code=code):
                result = protocol.make_result(JOB_ID, outcome, code)
                self.assertEqual(
                    result,
                    {
                        "job_id": JOB_ID,
                        "outcome": outcome,
                        "error": {"code": code, "message": message},
                    },
                )
                self.assertEqual(protocol.validate_result(result), result)

    def test_result_rejects_arbitrary_errors_and_wrong_outcome_shapes(self):
        documents = (
            {"job_id": JOB_ID, "outcome": "failed", "value": "remote secret"},
            {
                "job_id": JOB_ID,
                "outcome": "failed",
                "error": {"code": "FAILED", "message": "remote secret"},
            },
            {
                "job_id": JOB_ID,
                "outcome": "failed",
                "error": {
                    "code": "requests.HTTPError",
                    "message": "TMDB operation failed",
                },
            },
            {"job_id": JOB_ID, "outcome": "succeeded"},
            {"job_id": JOB_ID, "outcome": "aborted", "value": None},
            {
                "job_id": JOB_ID,
                "outcome": "cancelled",
                "error": {"code": "ABORTED", "message": "TMDB operation aborted"},
            },
        )
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_result(document)

    def test_result_rejects_bad_ids_unknown_fields_duplicates_and_nonfinite_values(
        self,
    ):
        documents = (
            {"job_id": "A" * 32, "outcome": "succeeded", "value": None},
            {"job_id": "1" * 31, "outcome": "succeeded", "value": None},
            {
                "job_id": JOB_ID,
                "outcome": "succeeded",
                "value": None,
                "traceback": "secret",
            },
            {"job_id": JOB_ID, "outcome": "succeeded", "value": float("nan")},
            {"job_id": JOB_ID, "outcome": "succeeded", "value": float("inf")},
            {"job_id": JOB_ID, "outcome": "succeeded", "value": len},
        )
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_result(document)
        duplicate = (
            '{"job_id":"' + JOB_ID + '","outcome":"succeeded",'
            '"value":null,"value":{}}'
        )
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_result(duplicate)
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_result(
                '{"job_id":"' + JOB_ID + '","outcome":"succeeded","value":1e999}'
            )

    def test_result_canonical_form_and_unicode_are_strictly_validated(self):
        oversized = "x" * 1048576
        with self.assertRaises(protocol.ProtocolError):
            protocol.make_result(JOB_ID, "succeeded", oversized)
        with self.assertRaises(protocol.ProtocolError):
            protocol.make_result(JOB_ID, "succeeded", "\ud800")
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_result(b"\xff")

    def test_make_result_rejects_invalid_job_outcome_and_default_mismatches(self):
        cases = (
            (True, "succeeded", None),
            ("1" * 32, "unknown", None),
            (JOB_ID, "succeeded", len),
            (JOB_ID, "aborted", "FAILED"),
            (JOB_ID, "failed", "ABORTED"),
            (JOB_ID, "failed", None),
        )
        for job_id, outcome, value in cases:
            with self.subTest(job_id=job_id, outcome=outcome, value=value):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.make_result(job_id, outcome, value)

    def test_result_numbers_are_finite_and_detachment_is_recursive(self):
        value = {"rows": [{"vote_average": 7.5, "genre_ids": [1, 2]}]}
        result = protocol.make_result(JOB_ID, "succeeded", value)
        value["rows"][0]["genre_ids"].append(3)
        self.assertEqual(result["value"]["rows"][0]["genre_ids"], [1, 2])
        validated = protocol.validate_result(result)
        self.assertTrue(math.isfinite(validated["value"]["rows"][0]["vote_average"]))
        validated["value"]["rows"][0]["genre_ids"].append(4)
        self.assertEqual(result["value"]["rows"][0]["genre_ids"], [1, 2])


if __name__ == "__main__":
    unittest.main()
