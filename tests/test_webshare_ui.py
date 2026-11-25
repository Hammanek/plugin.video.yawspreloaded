import io
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
import zipfile
from xml.etree import ElementTree as ET

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.support.kodi_stubs import AddonRecorder, KodiRecorder
from tests.test_runtime import RecordingLogger
from yawsp_app.runtime import ActionRunner, DirectoryResult, KodiRuntime, PlaybackResult
from yawsp_app.webshare import WebshareError, XmlSearchPage
from yawsp_app.webshare_ui import build_webshare_handlers


class ListItem:
    def __init__(self, label="", path=""):
        self.label = label
        self.path = path
        self.art = {}
        self.info = []
        self.commands = []
        self.properties = {}

    def setArt(self, art):
        self.art = art

    def setInfo(self, kind, info):
        self.info.append((kind, info))

    def addContextMenuItems(self, commands):
        self.commands.extend(commands)

    def setProperty(self, key, value):
        self.properties[key] = value


class KodiLikeListItem(ListItem):
    """Kodi exposes getLabel(), not the fake's convenience label attribute."""

    def __getattribute__(self, name):
        if name == "label":
            raise AttributeError("Kodi ListItem has no public label attribute")
        return super().__getattribute__(name)

    def getLabel(self):
        return self.__dict__["label"]


class LocalFileOps:
    def open_write(self, path):
        return open(path, "wb")

    def replace(self, source, destination):
        return os.replace(source, destination)

    def remove(self, path):
        return os.remove(path)

    def exists(self, path):
        return os.path.exists(path)


class Addon(AddonRecorder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.open_settings_calls = 0

    def openSettings(self):
        self.open_settings_calls += 1
        self.calls.append(("openSettings",))


class OrderingRecorder(KodiRecorder):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def endOfDirectory(self, *args, **kwargs):
        self.events.append("end")
        return super().endOfDirectory(*args, **kwargs)

    def setResolvedUrl(self, *args, **kwargs):
        self.events.append("resolve")
        return super().setResolvedUrl(*args, **kwargs)


class Keyboard:
    def __init__(self, value, events):
        self.value = value
        self.events = events
        self.default = None
        self.heading = None

    def doModal(self):
        self.events.append("keyboard")

    def isConfirmed(self):
        return self.value is not None

    def getText(self):
        return self.value or ""


class FakeClient:
    def __init__(self):
        self.token = "token"
        self.search_page = XmlSearchPage(files=(), raw_count=0)
        self.search_calls = []
        self.api_calls = []
        self.api_responses = {}
        self.link_results = []
        self.info_results = []
        self.revalidate_calls = 0

    def revalidate(self):
        self.revalidate_calls += 1
        return self.token

    def search(self, token, query, category, sort, limit, offset):
        self.search_calls.append((token, query, category, sort, limit, offset))
        if not token:
            raise AssertionError("search must not receive an empty token")
        return self.search_page

    def _post(self, endpoint, data):
        self.api_calls.append((endpoint, data))
        if not data.get("wst"):
            raise AssertionError("API request must not receive an empty token")
        responses = self.api_responses.get(endpoint)
        if not responses:
            raise AssertionError("unexpected API endpoint " + endpoint)
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def get_link(self, ident, token, dtype="video_stream"):
        self.api_calls.append(("get_link", {"ident": ident, "wst": token, "dtype": dtype}))
        if not token:
            raise AssertionError("get_link must not receive an empty token")
        result = self.link_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def get_info(self, ident, token):
        self.api_calls.append(("get_info", {"ident": ident, "wst": token}))
        if not token:
            raise AssertionError("get_info must not receive an empty token")
        result = self.info_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def xml(text):
    return ET.fromstring(text)


def archive_bytes(filename="A.txt"):
    output = io.BytesIO()
    payload = json.dumps(
        {
            "data": [
                {
                    "id": "movie-1",
                    "title": "Film",
                    "streams": [
                        {
                            "ident": "stream-1",
                            "quality": "HD",
                            "lang": "CZ",
                            "size": "1 MB",
                        }
                    ],
                }
            ]
        }
    ).encode("utf-8")
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(filename, payload)
    return output.getvalue()


def url_params(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


class WebshareUITests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.profile = os.path.join(self.tempdir.name, "profile")
        os.mkdir(self.profile)
        self.download_dir = os.path.join(self.tempdir.name, "downloads")
        os.mkdir(self.download_dir)
        self.events = []
        self.plugin = OrderingRecorder(self.events)
        self.addon = Addon(
            settings={
                "scategory": "1",
                "ssort": "0",
                "shistory": "20",
                "slast": "%#NONE#%",
                "dfolder": self.download_dir,
                "dnormalize": "true",
                "dnotify": "true",
                "dnevery": "10%",
            },
            strings={
                30007: "Search",
                30010: "Search DB",
                30101: "Login required",
                30102: "Request failed",
                30104: "History removed",
                30105: "Queued",
                30106: "Dequeued",
                30107: "Unavailable",
                30201: "Search",
                30202: "Queue",
                30203: "History",
                30204: "Settings",
                30205: "New search",
                30206: "Previous",
                30207: "Next",
                30208: "Latest",
                30209: "Largest",
                30211: "Info",
                30212: "Download",
                30213: "Remove",
                30214: "Queue",
                30215: "Dequeue",
                30216: "Search for",
                30301: "Unknown size",
                30302: "Downloading",
                30303: "Downloaded",
                30304: "Download failed",
            },
            addon_info={"name": "YAWsP", "id": "plugin.video.yawspreloaded"},
        )
        self.session = FakeSession()
        self.keyboard_values = []
        self.texts = []
        self.client = FakeClient()
        self.logger = RecordingLogger()
        self.runtime = KodiRuntime(
            plugin=self.plugin,
            addon=self.addon,
            session=self.session,
            profile=self.profile,
            base_url="plugin://plugin.video.yawspreloaded/",
            handle=9,
            listitem_factory=ListItem,
            keyboard_factory=self.make_keyboard,
            textviewer=self.show_text,
            file_ops=LocalFileOps(),
            logger=self.logger,
        )
        self.handlers = build_webshare_handlers(self.runtime, lambda: self.client)
        self.errors = []
        self.runner = ActionRunner(
            self.plugin,
            9,
            ListItem,
            logger=self.logger,
            error_handler=self.errors.append,
        )

    def make_keyboard(self, default, heading):
        value = self.keyboard_values.pop(0)
        keyboard = Keyboard(value, self.events)
        keyboard.default = default
        keyboard.heading = heading
        return keyboard

    def show_text(self, heading, text):
        self.events.append("textviewer")
        self.texts.append((heading, text))

    def find_item(self, label):
        return next(item for item in self.plugin.items if item["listitem"].label == label)

    def test_file_rows_do_not_depend_on_fake_listitem_label_attribute(self):
        self.runtime.listitem_factory = KodiLikeListItem
        self.client.search_page = XmlSearchPage(
            files=(xml("<file><name>Movie.mkv</name><ident>abc</ident></file>"),),
            raw_count=1,
        )

        result = self.handlers["search"]({"action": "search", "what": "Movie"})

        self.assertTrue(result.succeeded)
        self.assertEqual(self.plugin.items[0]["listitem"].getLabel(), "Movie.mkv (?)")

    def test_cancelled_search_input_returns_through_directory_adapter_once(self):
        self.keyboard_values = [None]

        result = self.runner.run_directory(
            lambda: self.handlers["search"]({"action": "search"})
        )

        self.assertTrue(result.succeeded)
        self.assertTrue(result.update_listing)
        self.assertEqual(self.client.search_calls, [])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)

    def test_failed_token_stops_before_search_api_and_returns_directory_result(self):
        self.client.token = None

        result = self.handlers["search"]({"action": "search", "what": "Movie"})

        self.assertIsInstance(result, DirectoryResult)
        self.assertFalse(result.succeeded)
        self.assertEqual(self.client.search_calls, [])
        self.assertEqual(self.client.api_calls, [])

    def test_browse_sentinel_displays_unfiltered_latest_results(self):
        self.client.search_page = XmlSearchPage(
            files=(xml("<file><name>Latest.mkv</name><ident>latest</ident><size>12</size></file>"),),
            raw_count=1,
        )

        result = self.handlers["search"](
            {"action": "search", "what": "%#NONE#%", "sort": "recent"}
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(self.addon.getSetting("slast"), "%#NONE#%")
        token, query, _, sort, _, _ = self.client.search_calls[0]
        self.assertEqual(token, "token")
        self.assertTrue(query.browsing)
        self.assertEqual(query.api_text, "")
        self.assertEqual(sort, "recent")
        self.assertTrue(self.find_item("Latest.mkv (12B)"))

    def test_previous_and_next_use_raw_count_and_preserve_listing_state(self):
        self.client.search_page = XmlSearchPage(files=(), raw_count=2)
        params = {
            "action": "search",
            "what": "Movie",
            "category": "video",
            "sort": "rating",
            "limit": "2",
            "offset": "2",
        }

        self.handlers["search"](params)

        previous = url_params(self.find_item("Previous")["url"])
        following = url_params(self.find_item("Next")["url"])
        self.assertEqual(previous["offset"], "0")
        self.assertEqual(following["offset"], "4")
        for listing in (previous, following):
            self.assertEqual(listing["what"], "Movie")
            self.assertEqual(listing["category"], "video")
            self.assertEqual(listing["sort"], "rating")
            self.assertEqual(listing["limit"], "2")
        self.assertEqual(self.client.search_calls[0][-1], 2)

    def test_toqueue_keeps_search_filters_sort_limit_and_offset(self):
        self.client.api_responses["queue_file"] = [
            xml("<response><status>OK</status></response>")
        ]
        self.client.search_page = XmlSearchPage(
            files=(xml("<file><name>Show.S01E02.mkv</name><ident>abc</ident></file>"),),
            raw_count=1,
        )
        params = {
            "action": "search",
            "what": "Show S01E02",
            "category": "video",
            "sort": "recent",
            "limit": "2",
            "offset": "2",
            "toqueue": "abc",
        }

        result = self.handlers["search"](params)

        self.assertTrue(result.update_listing)
        self.assertEqual(self.client.api_calls[0][0], "queue_file")
        self.assertEqual(self.client.api_calls[0][1]["wst"], "token")
        self.assertEqual(self.client.search_calls[0][-2:], (2, 2))
        item = self.find_item("Show.S01E02.mkv (?)")
        queue_command = next(
            command for _, command in item["listitem"].commands if "Container.Update" in command
        )
        queued_params = url_params(queue_command[len("Container.Update(") : -1])
        self.assertEqual(
            queued_params,
            {
                "action": "search",
                "what": "Show S01E02",
                "category": "video",
                "sort": "recent",
                "limit": "2",
                "offset": "2",
                "toqueue": "abc",
            },
        )

    def test_queue_removal_refreshes_listing_without_inner_completion(self):
        self.client.api_responses["dequeue_file"] = [
            xml("<response><status>OK</status></response>")
        ]
        self.client.api_responses["queue"] = [
            xml("<response><status>OK</status></response>")
        ]

        result = self.handlers["queue"]({"action": "queue", "dequeue": "abc"})

        self.assertTrue(result.update_listing)
        self.assertEqual(
            [call[0] for call in self.client.api_calls], ["dequeue_file", "queue"]
        )
        self.assertEqual(self.client.api_calls[0][1], {"ident": "abc", "wst": "token"})
        self.assertEqual(self.plugin.end_of_directory_calls, [])

    def test_history_removal_clears_download_ids_then_reloads(self):
        self.client.api_responses["history"] = [
            xml(
                "<response><status>OK</status><file>"
                "<name>Movie.mkv</name><ident>abc</ident><download_id>download-1</download_id>"
                "<started_at>today</started_at><ended_at>today</ended_at>"
                "</file></response>"
            ),
            xml("<response><status>OK</status></response>"),
        ]
        self.client.api_responses["clear_history"] = [
            xml("<response><status>OK</status></response>")
        ]

        result = self.handlers["history"]({"action": "history", "remove": "abc"})

        self.assertTrue(result.update_listing)
        self.assertEqual(
            [call[0] for call in self.client.api_calls],
            ["history", "clear_history", "history"],
        )
        self.assertEqual(self.client.api_calls[1][1]["ids[]"], ["download-1"])

    def test_history_removal_never_clears_missing_or_blank_download_ids(self):
        download_id_elements = (
            "<download_id />",
            "",
            "<download_id> \t </download_id>",
        )
        for download_id_element in download_id_elements:
            with self.subTest(download_id_element=download_id_element):
                history_item = (
                    "<file><name>Movie.mkv</name><ident>abc</ident>"
                    + download_id_element
                    + "<started_at>today</started_at><ended_at>today</ended_at></file>"
                )
                self.client.api_responses = {
                    "history": [
                        xml(
                            "<response><status>OK</status>"
                            + history_item
                            + "</response>"
                        ),
                        xml("<response><status>OK</status></response>"),
                    ],
                    # Keep the assertion focused on the forbidden request, not a
                    # fake-session error if the production guard regresses.
                    "clear_history": [xml("<response><status>OK</status></response>")],
                }
                self.client.api_calls.clear()

                result = self.handlers["history"]({"action": "history", "remove": "abc"})

                self.assertTrue(result.update_listing)
                self.assertEqual(
                    [call[0] for call in self.client.api_calls],
                    ["history", "history"],
                )

    def test_malformed_queue_and_history_status_fail_through_real_runner(self):
        responses = (
            "<response />",
            "<response><status /></response>",
            "<response><status> \t </status></response>",
        )
        for action, endpoint in (("queue", "queue"), ("history", "history")):
            for response in responses:
                with self.subTest(action=action, response=response):
                    self.client.api_responses = {endpoint: [xml(response)]}
                    error_count = len(self.errors)
                    completion_count = len(self.plugin.end_of_directory_calls)
                    notification_count = len(self.plugin.notifications)

                    result = self.runner.run_directory(
                        lambda: self.handlers[action]({"action": action})
                    )

                    self.assertFalse(result.succeeded)
                    self.assertEqual(len(self.errors), error_count + 1)
                    self.assertIsInstance(self.errors[-1], WebshareError)
                    self.assertEqual(
                        len(self.plugin.end_of_directory_calls), completion_count + 1
                    )
                    self.assertFalse(self.plugin.end_of_directory_calls[-1]["succeeded"])
                    self.assertEqual(
                        len(self.plugin.notifications), notification_count
                    )
                    self.assertEqual(len(self.client.api_calls), 1)
                    self.client.api_calls.clear()

    def test_non_ok_queue_and_history_status_remain_business_rejections(self):
        for action, endpoint in (("queue", "queue"), ("history", "history")):
            with self.subTest(action=action):
                self.client.api_responses = {
                    endpoint: [xml("<response><status>ERROR</status></response>")]
                }
                notification_count = len(self.plugin.notifications)

                result = self.runner.run_directory(
                    lambda: self.handlers[action]({"action": action})
                )

                self.assertTrue(result.succeeded)
                self.assertEqual(self.errors, [])
                self.assertEqual(len(self.plugin.notifications), notification_count + 1)
                self.assertTrue(self.plugin.end_of_directory_calls[-1]["succeeded"])
                self.client.api_calls.clear()

    def test_invalid_play_link_returns_failure_and_runner_resolves_once(self):
        self.client.link_results = [""]

        result = self.handlers["play"](
            {"action": "play", "ident": "abc", "name": "Movie"}
        )
        returned = self.runner.run_play(lambda: result)

        self.assertIsInstance(result, PlaybackResult)
        self.assertFalse(result.succeeded)
        self.assertFalse(returned.succeeded)
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)

    def test_info_textviewer_precedes_action_completion(self):
        self.client.info_results = [
            xml("<response><name>Movie.mkv</name><ident>abc</ident><size>12</size></response>")
        ]

        result = self.runner.run_action(
            lambda: self.handlers["info"]({"action": "info", "ident": "abc"})
        )

        self.assertTrue(result.handled)
        self.assertEqual(self.events, ["textviewer", "resolve"])
        self.assertIn("Name: Movie.mkv", self.texts[0][1])
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)

    def test_download_failure_uses_ident_as_filename_before_metadata_arrives(self):
        self.client.link_results = [RuntimeError("link failed")]

        result = self.runner.run_action(
            lambda: self.handlers["download"]({"action": "download", "ident": "fallback-id"})
        )

        self.assertFalse(result.handled)
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertEqual(len(self.plugin.notifications), 1)
        self.assertIn("fallback-id", self.plugin.notifications[0]["message"])

    def test_download_streams_to_validated_destination_using_real_service(self):
        self.client.link_results = ["https://cdn.invalid/movie"]
        self.client.info_results = [
            xml("<response><name>Příliš.mkv</name><ident>abc</ident></response>")
        ]
        self.session.queue_response(
            FakeResponse(
                content=b"movie",
                headers={"Content-Length": "5"},
                chunks=[b"movie"],
                forbid_content=True,
            )
        )

        result = self.handlers["download"]({"action": "download", "ident": "abc"})

        self.assertTrue(result.handled)
        destination = os.path.join(self.download_dir, "Prilis.mkv")
        with open(destination, "rb") as downloaded:
            self.assertEqual(downloaded.read(), b"movie")
        self.assertEqual(len(self.session.responses), 0)

    def test_whitespace_only_searchdb_is_cancelled_without_downloading_database(self):
        self.keyboard_values = [" \t "]
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        self.session.queue_response(FakeResponse(content=archive_bytes()))

        result = self.runner.run_directory(
            lambda: self.handlers["searchdb"]({"action": "searchdb"})
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(self.client.api_calls, [])
        self.assertEqual(self.session.calls, [])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)
        self.assertTrue(self.plugin.end_of_directory_calls[0]["succeeded"])

    def test_padded_searchdb_query_uses_trimmed_group_and_filter(self):
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        self.session.queue_response(FakeResponse(content=archive_bytes("F.txt")))

        result = self.runner.run_directory(
            lambda: self.handlers["searchdb"](
                {"action": "searchdb", "query": "  Film  "}
            )
        )

        self.assertTrue(result.succeeded)
        self.assertTrue(self.find_item("Film"))
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)
        self.assertTrue(self.plugin.end_of_directory_calls[0]["succeeded"])

    def test_cancelled_and_empty_searchdb_complete_without_loading_database(self):
        self.keyboard_values = [None, ""]
        self.addon.setSetting("dfolder", "")

        cancelled = self.runner.run_directory(lambda: self.handlers["searchdb"]({"action": "searchdb"}))
        empty = self.runner.run_directory(lambda: self.handlers["searchdb"]({"action": "searchdb"}))

        self.assertTrue(cancelled.succeeded)
        self.assertTrue(empty.succeeded)
        self.assertEqual(self.client.api_calls, [])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 2)
        self.assertTrue(all(call["succeeded"] for call in self.plugin.end_of_directory_calls))

    def test_failed_backup_db_install_can_be_retried_after_cleanup(self):
        self.client.link_results = ["https://cdn.invalid/db.zip", "https://cdn.invalid/db.zip"]
        self.session.queue_response(FakeResponse(content=b"not a zip"))
        self.session.queue_response(FakeResponse(content=archive_bytes()))

        failed = self.runner.run_directory(lambda: self.handlers["db"]({"action": "db"}))
        repaired = self.runner.run_directory(
            lambda: self.handlers["db"]({"action": "db", "file": "A.txt"})
        )

        self.assertFalse(failed.succeeded)
        self.assertTrue(repaired.succeeded)
        self.assertEqual(len(self.plugin.end_of_directory_calls), 2)
        self.assertTrue(self.find_item("Film"))
        self.assertFalse(os.path.exists(os.path.join(self.profile, "db.zip")))

    def test_settings_is_an_action_without_directory_completion(self):
        result = self.runner.run_action(lambda: self.handlers["settings"]({"action": "settings"}))

        self.assertTrue(result.handled)
        self.assertEqual(self.addon.open_settings_calls, 1)
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertEqual(self.plugin.end_of_directory_calls, [])


if __name__ == "__main__":
    unittest.main()
