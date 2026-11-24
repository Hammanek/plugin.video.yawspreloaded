import unittest
from xml.etree import ElementTree as ET

import requests

from tests.support.http_stubs import FakeResponse, FakeSession
from yawsp_app.search import parse_query
from yawsp_app.webshare import WebshareClient, WebshareError


class MemorySettings:
    def __init__(self, **values):
        self.values = {
            "wsuser": "alice",
            "wspass": "password",
            "token": "",
            "duuid": "device-123",
        }
        self.values.update(values)

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


def xml_response(xml, status_code=200):
    return FakeResponse(content=xml, status_code=status_code)


def client_for(responses, **settings):
    session = FakeSession(responses)
    client = WebshareClient(session, MemorySettings(**settings))
    return client, session, client.settings


class TransportTests(unittest.TestCase):
    def test_timeout_raises_chained_domain_error_instead_of_returning_none(self):
        client, _, _ = client_for([requests.Timeout("timed out")])

        with self.assertRaises(WebshareError) as caught:
            client.search("token", parse_query("query"), "", "", 20, 0)

        self.assertIsInstance(caught.exception.__cause__, requests.Timeout)

    def test_http_500_raises_chained_domain_error_instead_of_returning_none(self):
        client, _, _ = client_for([xml_response("", status_code=500)])

        with self.assertRaises(WebshareError) as caught:
            client.login()

        self.assertIsInstance(caught.exception.__cause__, requests.HTTPError)

    def test_malformed_xml_raises_chained_domain_error(self):
        client, _, _ = client_for([xml_response("<response><status>OK</status>")])

        with self.assertRaises(WebshareError) as caught:
            client.get_link("ident", "token")

        self.assertIsInstance(caught.exception.__cause__, ET.ParseError)

    def test_non_ok_search_response_is_a_domain_error(self):
        client, _, _ = client_for(
            [xml_response("<response><status>ERROR</status></response>")]
        )

        with self.assertRaises(WebshareError):
            client.search("token", parse_query("query"), "", "", 20, 0)

    def test_non_ok_link_response_is_a_domain_error(self):
        client, _, _ = client_for(
            [xml_response("<response><status>ERROR</status></response>")]
        )

        with self.assertRaises(WebshareError):
            client.get_link("ident", "token")


class AuthenticationTests(unittest.TestCase):
    def test_login_uses_bundled_md5_protocol_and_stores_returned_token(self):
        client, session, settings = client_for(
            [
                xml_response(
                    "<response><status>OK</status><salt>salt1234</salt></response>"
                ),
                xml_response(
                    "<response><status>OK</status><token>new-token</token></response>"
                ),
            ]
        )

        token = client.login()

        self.assertEqual(token, "new-token")
        self.assertEqual(settings.get("token"), "new-token")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[0]["kwargs"]["data"], {"username_or_email": "alice"}
        )
        self.assertEqual(
            session.calls[1]["kwargs"]["data"],
            {
                "username_or_email": "alice",
                "password": "cb96eb202b56a652fa18108461fcc0e8d34f961c",
                "digest": "caf8d422b75dd7b72d21dcaa24d666d2",
                "keep_logged_in": 1,
            },
        )
        self.assertTrue(
            session.calls[1]["url"].endswith("/login/"),
            session.calls[1]["url"],
        )
        self.assertEqual(session.calls[1]["kwargs"]["timeout"], 30)

    def test_rejected_login_returns_none_and_leaves_no_saved_token(self):
        client, session, settings = client_for(
            [
                xml_response(
                    "<response><status>OK</status><salt>salt1234</salt></response>"
                ),
                xml_response("<response><status>ERROR</status></response>"),
            ],
            token="stale-token",
        )

        self.assertIsNone(client.login())
        self.assertEqual(settings.get("token"), "")
        self.assertEqual(len(session.calls), 2)

    def test_rejected_salt_request_returns_none_without_attempting_login(self):
        client, session, settings = client_for(
            [xml_response("<response><status>ERROR</status></response>")]
        )

        self.assertIsNone(client.login())
        self.assertEqual(settings.get("token"), "")
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0]["url"].endswith("/salt/"))

    def test_empty_credentials_return_none_without_transport_or_old_token(self):
        client, session, settings = client_for([], wsuser="", token="stale-token")

        self.assertIsNone(client.login())
        self.assertEqual(settings.get("token"), "")
        self.assertEqual(session.calls, [])

    def test_successful_login_requires_token_element(self):
        client, _, settings = client_for(
            [
                xml_response(
                    "<response><status>OK</status><salt>salt1234</salt></response>"
                ),
                xml_response("<response><status>OK</status></response>"),
            ]
        )

        with self.assertRaises(WebshareError):
            client.login()
        self.assertEqual(settings.get("token"), "")

    def test_valid_existing_token_requires_vip_but_returns_despite_non_vip_value(self):
        client, session, _ = client_for(
            [
                xml_response(
                    "<response><status>OK</status><vip>0</vip></response>"
                )
            ],
            token="existing-token",
        )

        self.assertEqual(client.revalidate(), "existing-token")
        self.assertEqual(
            session.calls[0]["kwargs"]["data"], {"wst": "existing-token"}
        )

    def test_successful_revalidation_with_missing_vip_is_invalid_response(self):
        client, session, _ = client_for(
            [xml_response("<response><status>OK</status></response>")],
            token="existing-token",
        )

        with self.assertRaises(WebshareError):
            client.revalidate()
        self.assertEqual(len(session.calls), 1)

    def test_invalid_existing_token_is_cleared_and_failed_fallback_returns_none(self):
        client, session, settings = client_for(
            [
                xml_response("<response><status>ERROR</status></response>"),
                xml_response(
                    "<response><status>OK</status><salt>salt1234</salt></response>"
                ),
                xml_response("<response><status>ERROR</status></response>"),
            ],
            token="expired-token",
        )

        self.assertIsNone(client.revalidate())
        self.assertEqual(settings.get("token"), "")
        self.assertEqual(
            [call["url"].rsplit("/", 2)[-2] for call in session.calls],
            ["user_data", "salt", "login"],
        )


class SearchTests(unittest.TestCase):
    def test_search_sends_normalized_episode_query_and_request_parameters(self):
        client, session, _ = client_for(
            [
                xml_response(
                    "<response><status>OK</status>"
                    "<file><name>Show.S01E02.mkv</name><ident>abc</ident></file>"
                    "</response>"
                )
            ]
        )

        page = client.search(
            "token", parse_query("Show S1E2"), "video", "recent", 50, 100
        )

        self.assertEqual(page.raw_count, 1)
        self.assertEqual(len(page.files), 1)
        self.assertEqual(
            session.calls[0]["kwargs"]["data"],
            {
                "what": "Show S01E02",
                "category": "video",
                "sort": "recent",
                "limit": 50,
                "offset": 100,
                "wst": "token",
                "maybe_removed": "true",
            },
        )

    def test_search_retains_raw_count_and_skips_items_missing_name_or_ident(self):
        client, _, _ = client_for(
            [
                xml_response(
                    "<response><status>OK</status>"
                    "<file><name>valid.mkv</name><ident>valid-id</ident></file>"
                    "<file><ident>missing-name</ident></file>"
                    "<file><name>missing-ident.mkv</name></file>"
                    "</response>"
                )
            ]
        )

        with self.assertLogs("yawsp_app.webshare", level="WARNING") as logs:
            page = client.search("token", parse_query("valid"), "", "", 20, 0)

        self.assertEqual(page.raw_count, 3)
        self.assertEqual(len(page.files), 1)
        self.assertIsInstance(page.files[0], ET.Element)
        self.assertEqual(page.files[0].findtext("name"), "valid.mkv")
        self.assertEqual(page.files[0].findtext("ident"), "valid-id")
        self.assertEqual(len(logs.output), 2)


class FileOperationTests(unittest.TestCase):
    def test_info_retries_non_ok_response_with_maybe_removed(self):
        client, session, _ = client_for(
            [
                xml_response("<response><status>ERROR</status></response>"),
                xml_response(
                    "<response><status>OK</status><name>file.mkv</name>"
                    "<ident>file-id</ident><size>123</size></response>"
                ),
            ]
        )

        result = client.get_info("file-id", "token")

        self.assertIsInstance(result, ET.Element)
        self.assertEqual(result.findtext("name"), "file.mkv")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[0]["kwargs"]["data"],
            {"ident": "file-id", "wst": "token"},
        )
        self.assertEqual(
            session.calls[1]["kwargs"]["data"],
            {"ident": "file-id", "wst": "token", "maybe_removed": "true"},
        )

    def test_info_raises_after_allowed_retry_also_rejects_file(self):
        client, session, _ = client_for(
            [
                xml_response("<response><status>ERROR</status></response>"),
                xml_response("<response><status>ERROR</status></response>"),
            ]
        )

        with self.assertRaises(WebshareError):
            client.get_info("file-id", "token")
        self.assertEqual(len(session.calls), 2)

    def test_info_success_requires_name_and_ident(self):
        fixtures = (
            "<response><status>OK</status><ident>file-id</ident></response>",
            "<response><status>OK</status><name>file.mkv</name></response>",
        )
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                client, _, _ = client_for([xml_response(fixture)])
                with self.assertRaises(WebshareError):
                    client.get_info("file-id", "token")

    def test_link_uses_device_uuid_dtype_and_returns_raw_link(self):
        client, session, _ = client_for(
            [
                xml_response(
                    "<response><status>OK</status>"
                    "<link>https://cdn.invalid/file.mkv</link></response>"
                )
            ]
        )

        link = client.get_link("file-id", "token", dtype="file_download")

        self.assertEqual(link, "https://cdn.invalid/file.mkv")
        self.assertEqual(
            session.calls[0]["kwargs"]["data"],
            {
                "ident": "file-id",
                "wst": "token",
                "download_type": "file_download",
                "device_uuid": "device-123",
            },
        )

    def test_successful_link_response_requires_link_element(self):
        client, _, _ = client_for(
            [xml_response("<response><status>OK</status></response>")]
        )

        with self.assertRaises(WebshareError):
            client.get_link("file-id", "token")

    def test_generated_device_uuid_is_saved_and_reused_by_independent_link_calls(self):
        client, session, settings = client_for(
            [
                xml_response(
                    "<response><status>OK</status><link>https://cdn.invalid/1</link>"
                    "</response>"
                ),
                xml_response(
                    "<response><status>OK</status><link>https://cdn.invalid/2</link>"
                    "</response>"
                ),
            ],
            duuid="",
        )

        self.assertEqual(client.get_link("one", "token"), "https://cdn.invalid/1")
        self.assertEqual(client.get_link("two", "token"), "https://cdn.invalid/2")

        generated = settings.get("duuid")
        self.assertTrue(generated)
        self.assertEqual(
            [call["kwargs"]["data"]["device_uuid"] for call in session.calls],
            [generated, generated],
        )


if __name__ == "__main__":
    unittest.main()
