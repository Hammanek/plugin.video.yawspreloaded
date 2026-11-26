"""RED-first remediation coverage for streaming, trailer IDs and private errors."""

import json
import logging
import os
import tempfile
import traceback
import unittest

import requests

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.test_tmdb_service_worker import trailer_data
from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.tmdb import (
    TMDBClient,
    TMDBError,
    TrailerLookup,
    TrailerRepository,
    execute_watched_removal,
)
from yawsp_app.tmdb_service_client import TMDBServiceClient
from yawsp_app.tmdb_ui import _pending_session_id
from yawsp_app.watchlist_snapshot import SnapshotScope


class Settings:
    def __init__(self):
        self.values = {
            "tmdb_api_key": "synthetic-api-key",
            "tmdb_access_token": "synthetic-session-token",
            "tmdb_account_id": "synthetic-account-42",
        }

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


class StreamingAndPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings()

    def client(self, responses):
        session = FakeSession(responses)
        client = TMDBClient(session, self.settings, lambda: "cs-CZ")
        return client, session

    def padded_object(self, size):
        prefix = b'{"ok":true}'
        self.assertGreaterEqual(size, len(prefix))
        return prefix + b" " * (size - len(prefix))

    def test_response_stream_cap_has_exact_boundary_and_early_length_guard(self):
        accepted = FakeResponse(content=self.padded_object(1048576))
        client, session = self.client([accepted])
        self.assertEqual(client.get("/3/example"), {"ok": True})
        self.assertTrue(session.calls[0]["kwargs"]["stream"])
        self.assertTrue(accepted.closed)

        for with_header in (False, True):
            with self.subTest(oversized_header=with_header):
                response = FakeResponse(
                    content=self.padded_object(1048577),
                    headers=(
                        {"Content-Length": "1048577"} if with_header else None
                    ),
                )
                client, session = self.client([response])
                with self.assertRaises(TMDBError):
                    client.get("/3/example")
                self.assertTrue(response.closed)
                self.assertTrue(session.calls[0]["kwargs"]["stream"])

        class EarlyResponse(FakeResponse):
            iterated = False

            def iter_content(self, *args, **kwargs):
                self.iterated = True
                return super().iter_content(*args, **kwargs)

        early = EarlyResponse(
            content=b"{}", headers={"Content-Length": "1048577"}
        )
        client, _ = self.client([early])
        with self.assertRaises(TMDBError):
            client.get("/3/example")
        self.assertFalse(early.iterated)
        self.assertTrue(early.closed)

    def test_stream_errors_utf8_and_auth_watched_paths_use_same_bounded_decoder(self):
        malformed = FakeResponse(content=b"\xff")
        client, session = self.client([malformed])
        with self.assertRaises(TMDBError):
            client.get("/3/example")
        self.assertTrue(malformed.closed)
        self.assertTrue(session.calls[0]["kwargs"]["stream"])

        stream_failure = FakeResponse(
            content=b"not-json",
            chunks=[b"{"],
            stream_error=requests.exceptions.ChunkedEncodingError("synthetic read failure"),
        )
        client, _ = self.client([stream_failure])
        with self.assertRaises(TMDBError) as caught:
            client.get("/3/example")
        self.assertIsInstance(caught.exception.__cause__, requests.exceptions.ChunkedEncodingError)
        self.assertTrue(stream_failure.closed)

        pending = FakeResponse(json_data={"session_id": "new-session"})
        client, session = self.client([pending])
        self.assertEqual(_pending_session_id(client, "request-token"), "new-session")
        self.assertTrue(session.calls[0]["kwargs"]["stream"])
        self.assertTrue(pending.closed)

        watched = FakeResponse(status_code=201, json_data={"success": True})
        client, session = self.client([watched])
        scope = SnapshotScope("synthetic-account-42", "movies", "cs-CZ")
        self.assertTrue(execute_watched_removal(client, scope, 7))
        self.assertTrue(session.calls[0]["kwargs"]["stream"])
        self.assertTrue(watched.closed)

    def render_error(self, operation):
        rendered = []
        try:
            operation()
        except TMDBError as error:
            rendered.extend(traceback.format_exception(type(error), error, error.__traceback__))
            with self.assertLogs("test.tmdb.privacy", level="ERROR") as logged:
                logging.getLogger("test.tmdb.privacy").exception(
                    "operation failed", exc_info=error
                )
            rendered.extend(logged.output)
            return error, "\n".join(rendered)
        self.fail("expected typed TMDB failure")

    def test_account_is_absent_from_error_text_logs_and_nested_tracebacks(self):
        account = "synthetic-account-42"
        unauthorized = FakeResponse(status_code=500, url="https://api.themoviedb.org/3/account/%s/watchlist/movies" % account)
        client, _ = self.client([unauthorized])
        error, rendered = self.render_error(
            lambda: client.get("/3/account/%s/watchlist/movies" % account)
        )
        self.assertIsInstance(error, TMDBError)
        self.assertIn("500", str(error))
        self.assertNotIn(account, rendered)
        self.assertTrue(unauthorized.closed)

        prepared = requests.Request(
            "GET",
            "https://api.themoviedb.org/3/account/%s/watchlist" % account,
            params={"api_key": "synthetic-api-key", "session_id": "synthetic-session-token"},
        ).prepare()
        nested = requests.Timeout("raw account path: /3/account/%s/watchlist" % account, request=prepared)
        client, _ = self.client([nested])
        _error, nested_rendered = self.render_error(
            lambda: execute_watched_removal(
                client, SnapshotScope(account, "movies", "cs-CZ"), 8
            )
        )
        self.assertNotIn(account, nested_rendered)
        self.assertNotIn("synthetic-api-key", nested_rendered)
        self.assertNotIn("synthetic-session-token", nested_rendered)

        client, _ = self.client([FakeResponse(status_code=503)])
        error, _ = self.render_error(lambda: client.get("/3/public/example"))
        self.assertIn("/3/public/example", str(error))
        self.assertIn("503", str(error))


class TrailerIdentifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_ingestion_protocol_decode_and_builtin_use_strict_video_id(self):
        valid = "Abcdef_123-"
        self.assertEqual(len(valid), 11)
        malicious = (
            "short",
            "A" * 12,
            valid + ")",
            "aaaaaaaaaa\n",
            "a)PlayMedia",
        )
        for index, video_id in enumerate(malicious):
            with self.subTest(video_id=repr(video_id)):
                cache = AtomicJsonCache(
                    os.path.join(self.temporary.name, "malicious-%d.json" % index)
                )
                session = FakeSession([FakeResponse(json_data=trailer_data(video_id))])
                repository = TrailerRepository(TMDBClient(session, Settings(), lambda: "en-US"), cache)
                self.assertEqual(repository.lookup("movie", 7), TrailerLookup("error", None))
                self.assertFalse(os.path.exists(cache.path))
                with self.assertRaises(protocol.ProtocolError):
                    TMDBServiceClient._decode_trailer({"status": "hit", "youtube_id": video_id})

        cache = AtomicJsonCache(os.path.join(self.temporary.name, "trailers.json"))
        session = FakeSession([FakeResponse(json_data=trailer_data(valid))])
        repository = TrailerRepository(TMDBClient(session, Settings(), lambda: "en-US"), cache)
        self.assertEqual(repository.lookup("movie", 7), TrailerLookup("hit", valid))
        self.assertEqual(
            TMDBServiceClient._decode_trailer({"status": "hit", "youtube_id": valid}),
            TrailerLookup("hit", valid),
        )

        from yawsp_app.tmdb_youtube import youtube_play_media

        self.assertEqual(
            youtube_play_media(valid),
            "PlayMedia(plugin://plugin.video.youtube/play/?video_id=Abcdef_123-)",
        )
        for video_id in malicious:
            with self.assertRaises(ValueError):
                youtube_play_media(video_id)


if __name__ == "__main__":
    unittest.main()
