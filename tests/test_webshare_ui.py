import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import zipfile
from xml.etree import ElementTree as ET

from tests.support.http_stubs import FakeResponse, FakeSession
from tests.support.kodi_stubs import AddonRecorder, KodiRecorder
from tests.test_runtime import RecordingLogger
from yawsp_app.download_jobs import DownloadJobManager
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


class ContainedLocalFileOps(LocalFileOps):
    """Fail before a regression can touch anything outside the test sandbox."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.attempts = []

    def _check(self, operation, path):
        self.attempts.append((operation, path))
        absolute = os.path.abspath(path)
        if os.path.commonpath((self.root, absolute)) != self.root:
            raise AssertionError("attempted to escape download sandbox: " + path)

    def open_write(self, path):
        self._check("write", path)
        return super().open_write(path)

    def replace(self, source, destination):
        self._check("replace-source", source)
        self._check("replace-destination", destination)
        return super().replace(source, destination)

    def remove(self, path):
        self._check("remove", path)
        return super().remove(path)


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
                30433: "Download queued",
                30434: "Backup DB installation in progress",
                30435: "Backup DB installed",
                30436: "Backup DB installation failed",
                30437: "Download could not be queued",
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
        self.worker_session = FakeSession()
        self.job_event = threading.Event()
        self.job_messages = []
        self.download_jobs = DownloadJobManager(
            session_factory=lambda: self.worker_session,
            logger=self.logger,
        )
        self.addCleanup(self.download_jobs.close, wait=False)

        def background_notify(message):
            self.job_messages.append(message)
            self.runtime.notify(message)
            if message.startswith(("Downloaded", "Download failed", "Backup DB installation failed")):
                self.job_event.set()
            if (
                message.startswith("Backup DB installed")
                and "Container.Refresh" in self.plugin.builtin_commands
            ):
                self.job_event.set()

        def background_refresh():
            self.plugin.executeBuiltin("Container.Refresh")
            if any(message.startswith("Backup DB installed") for message in self.job_messages):
                self.job_event.set()

        self.handlers = build_webshare_handlers(
            self.runtime,
            lambda: self.client,
            download_jobs=self.download_jobs,
            worker_client_factory=lambda session: self.client,
            notify=background_notify,
            refresh=background_refresh,
        )
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

    def wait_for_job(self):
        self.assertTrue(self.job_event.wait(2), "background job did not report a terminal result")
        self.job_event.clear()
        return self.job_messages[-1]

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
        self.assertEqual(self.client.revalidate_calls, 1)
        self.assertEqual(len(self.client.search_calls), 1)

    def test_search_menu_without_query_skips_authentication_and_network(self):
        with io.open(os.path.join(self.profile, "search_history"), "w", encoding="utf-8") as output:
            json.dump(["Movie", "Old"], output)

        result = self.runner.run_directory(
            lambda: self.handlers["search"]({"action": "search", "ask": 1})
        )

        self.assertTrue(result.succeeded)
        self.assertFalse(result.update_listing)
        self.assertEqual(
            [item["listitem"].label for item in self.plugin.items],
            ["New search", "Latest", "Largest", "Movie", "Old"],
        )
        self.assertEqual(self.addon.getSetting("slast"), "%#NONE#%")
        self.assertEqual(self.client.revalidate_calls, 0)
        self.assertEqual(self.client.search_calls, [])
        self.assertEqual(self.client.api_calls, [])
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.events, ["end"])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)

    def test_cancelled_search_input_returns_through_directory_adapter_once(self):
        self.keyboard_values = [None]

        result = self.runner.run_directory(
            lambda: self.handlers["search"]({"action": "search"})
        )

        self.assertTrue(result.succeeded)
        self.assertTrue(result.update_listing)
        self.assertEqual(
            [item["listitem"].label for item in self.plugin.items],
            ["New search", "Latest", "Largest"],
        )
        self.assertEqual(self.client.revalidate_calls, 0)
        self.assertEqual(self.client.search_calls, [])
        self.assertEqual(self.client.api_calls, [])
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.events, ["keyboard", "end"])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)

    def test_local_search_history_removal_skips_authentication_and_network(self):
        history_path = os.path.join(self.profile, "search_history")
        with io.open(history_path, "w", encoding="utf-8") as output:
            json.dump(["Movie", "Old"], output)
        self.keyboard_values = [None]

        result = self.runner.run_directory(
            lambda: self.handlers["search"]({"action": "search", "remove": "Movie"})
        )

        self.assertTrue(result.succeeded)
        self.assertTrue(result.update_listing)
        with io.open(history_path, "r", encoding="utf-8") as source:
            self.assertEqual(json.load(source), ["Old"])
        self.assertEqual(
            [item["listitem"].label for item in self.plugin.items],
            ["New search", "Latest", "Largest", "Old"],
        )
        self.assertEqual(self.client.revalidate_calls, 0)
        self.assertEqual(self.client.search_calls, [])
        self.assertEqual(self.client.api_calls, [])
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.events, ["keyboard", "end"])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)

    def test_failed_token_stops_before_search_api_and_returns_directory_result(self):
        self.client.token = None

        result = self.handlers["search"]({"action": "search", "what": "Movie"})

        self.assertIsInstance(result, DirectoryResult)
        self.assertFalse(result.succeeded)
        self.assertEqual(self.client.revalidate_calls, 1)
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
        self.assertEqual(self.client.revalidate_calls, 1)
        self.assertEqual(self.client.api_calls[0][0], "queue_file")
        self.assertEqual(self.client.api_calls[0][1]["wst"], "token")
        self.assertEqual(len(self.client.search_calls), 1)
        self.assertEqual(self.client.search_calls[0][0], "token")
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

    def test_main_menu_renders_static_routes_without_authentication(self):
        self.client.token = None

        result = self.runner.run_directory(
            lambda: self.handlers["menu"]({"action": "menu"})
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(self.client.revalidate_calls, 0)
        self.assertEqual(self.client.api_calls, [])
        self.assertEqual(self.session.calls, [])
        self.assertEqual(
            [item["listitem"].label for item in self.plugin.items],
            ["Search", "Queue", "History", "Backup DB", "Settings"],
        )
        self.assertEqual(
            [url_params(item["url"])["action"] for item in self.plugin.items],
            ["search", "queue", "history", "db", "settings"],
        )
        self.assertEqual(self.plugin.notifications, [])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)

    def test_installed_database_root_group_and_detail_do_not_authenticate(self):
        from yawsp_app.backup_db import BackupDBStore

        BackupDBStore(self.profile).ensure_installed(lambda: archive_bytes())
        self.client.token = None

        for params, expected_label in (
            ({"action": "db"}, "A"),
            ({"action": "db", "file": "A.txt"}, "Film"),
            (
                {"action": "db", "file": "A.txt", "key": "movie-1"},
                "HD - CZ (1 MB)",
            ),
        ):
            with self.subTest(params=params):
                self.plugin.items.clear()
                result = self.handlers["db"](params)
                self.assertTrue(result.succeeded)
                self.assertTrue(self.find_item(expected_label))

        self.assertEqual(self.client.revalidate_calls, 0)
        self.assertEqual(self.client.api_calls, [])
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.worker_session.calls, [])
        self.assertEqual(self.plugin.notifications, [])

    def test_installed_database_queue_still_authenticates_at_use(self):
        from yawsp_app.backup_db import BackupDBStore

        BackupDBStore(self.profile).ensure_installed(lambda: archive_bytes())
        self.client.api_responses["queue_file"] = [
            xml("<response><status>OK</status></response>")
        ]

        result = self.handlers["db"]({"action": "db", "toqueue": "stream-1"})

        self.assertTrue(result.succeeded)
        self.assertTrue(result.update_listing)
        self.assertEqual(self.client.revalidate_calls, 1)
        self.assertEqual(
            self.client.api_calls,
            [("queue_file", {"ident": "stream-1", "wst": "token"})],
        )

    def test_history_dedup_uses_canonical_whole_content_and_first_occurrence(self):
        from yawsp_app import webshare_ui

        source = [
            {
                "ident": "abc",
                "name": "Movie",
                "size": 1,
                "pairs": [{"language": "CZ", "quality": "HD"}],
                "streams": {"video": ["one", "two"]},
            },
            {
                "streams": {"video": ["one", "two"]},
                "pairs": [{"quality": "HD", "language": "CZ"}],
                "size": True,
                "name": "Movie",
                "ident": "abc",
            },
            {
                "ident": "abc",
                "name": "Movie",
                "size": 2,
                "pairs": [{"language": "CZ", "quality": "HD"}],
                "streams": {"video": ["one", "two"]},
            },
            {
                "ident": "abc",
                "name": "Movie",
                "size": 1,
                "pairs": [{"language": "CZ", "quality": "HD"}],
                "streams": {"video": ["two", "one"]},
            },
            {
                "ident": "abc",
                "name": "Movie",
                "size": 1,
                "pairs": [{"language": "CZ", "quality": "HD"}],
                "streams": {"video": ["one", "two"]},
            },
        ]
        deduplicate = getattr(webshare_ui, "_deduplicate_history", None)

        self.assertTrue(
            callable(deduplicate),
            "history deduplication needs a reusable canonical whole-content helper",
        )
        result = deduplicate(source)

        self.assertEqual(result, [source[0], source[2], source[3]])
        self.assertEqual(
            [id(item) for item in result],
            [id(source[0]), id(source[2]), id(source[3])],
        )

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

    def test_history_removal_without_valid_ids_reuses_fetched_response(self):
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
                    ["history"],
                )
                self.assertTrue(self.find_item("Movie.mkv (?)"))

    def test_unknown_history_removal_does_not_clear_other_valid_id_or_reload(self):
        self.client.api_responses["history"] = [
            xml(
                "<response><status>OK</status>"
                "<file><name>Keep.mkv</name><ident>keep</ident>"
                "<download_id>download-1</download_id><started_at>today</started_at>"
                "<ended_at>today</ended_at></file>"
                "<file><name>Other.mkv</name><ident>other</ident>"
                "<download_id>download-2</download_id><started_at>today</started_at>"
                "<ended_at>today</ended_at></file>"
                "</response>"
            )
        ]
        self.client.api_responses["clear_history"] = [
            xml("<response><status>OK</status></response>")
        ]

        result = self.handlers["history"](
            {"action": "history", "remove": "unrelated-ident"}
        )

        self.assertTrue(result.update_listing)
        self.assertEqual(
            [call[0] for call in self.client.api_calls], ["history"]
        )
        self.assertTrue(self.find_item("Keep.mkv (?)"))
        self.assertTrue(self.find_item("Other.mkv (?)"))

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

    def test_queue_and_history_skip_invalid_rows_but_render_valid_rows(self):
        invalid_rows = (
            "<file><ident>missing-name</ident></file>",
            "<file><name> \t </name><ident>blank-name</ident></file>",
            "<file><name>Missing ident.mkv</name></file>",
            "<file><name>Blank ident.mkv</name><ident> \t </ident></file>",
        )
        valid_row = "<file><name>Movie.mkv</name><ident>abc</ident><size>12</size></file>"
        for action in ("queue", "history"):
            with self.subTest(action=action):
                self.client.api_responses = {
                    action: [
                        xml(
                            "<response><status>OK</status>"
                            + "".join(invalid_rows)
                            + valid_row
                            + valid_row
                            + "</response>"
                        )
                    ]
                }
                self.plugin.items.clear()
                self.logger.calls.clear()
                error_count = len(self.errors)

                result = self.runner.run_directory(
                    lambda: self.handlers[action]({"action": action})
                )

                self.assertTrue(result.succeeded)
                self.assertEqual(len(self.errors), error_count)
                self.assertTrue(self.plugin.end_of_directory_calls[-1]["succeeded"])
                expected_rows = 1 if action == "history" else 2
                self.assertEqual(len(self.plugin.items), expected_rows)
                self.assertTrue(
                    all(item["listitem"].label == "Movie.mkv (12B)" for item in self.plugin.items)
                )
                self.assertEqual(len(self.logger.calls), len(invalid_rows))

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

        self.assertTrue(result.handled)
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertIn("fallback-id", self.wait_for_job())
        self.assertIn("Download failed", self.plugin.notifications[-1]["message"])

    def test_download_rejects_multicomponent_api_names_and_uses_safe_fallback(self):
        cases = (
            ("../outside.mkv", "Requested.mkv", "Requested.mkv"),
            ("..\\outside.mkv", "Requested.mkv", "Requested.mkv"),
            (os.path.join(self.tempdir.name, "absolute.mkv"), "Requested.mkv", "Requested.mkv"),
            ("C:drive-relative.mkv", "Requested.mkv", "Requested.mkv"),
            ("C:\\absolute.mkv", "Requested.mkv", "Requested.mkv"),
            ("folder\\nested.mkv", "Requested.mkv", "Requested.mkv"),
            ("../mixed\\separators.mkv", "Requested.mkv", "Requested.mkv"),
            ("..\\metadata.mkv", "../requested.mkv", "safe-ident"),
        )
        for metadata_name, requested_name, expected_name in cases:
            with self.subTest(name=metadata_name, requested=requested_name):
                file_ops = ContainedLocalFileOps(self.download_dir)
                self.runtime.file_ops = file_ops
                self.client.link_results = ["https://cdn.invalid/movie"]
                self.client.info_results = [
                    xml(
                        "<response><name>{0}</name><ident>safe-ident</ident></response>".format(
                            metadata_name.replace("&", "&amp;").replace("<", "&lt;")
                        )
                    )
                ]
                self.worker_session.queue_response(
                    FakeResponse(
                        content=b"movie",
                        headers={"Content-Length": "5"},
                        chunks=[b"movie"],
                        forbid_content=True,
                    )
                )

                result = self.handlers["download"](
                    {
                        "action": "download",
                        "ident": "safe-ident",
                        "name": requested_name,
                    }
                )

                self.assertTrue(result.handled)
                self.assertIn("Downloaded", self.wait_for_job())
                self.assertTrue(
                    all(
                        os.path.commonpath(
                            (os.path.abspath(self.download_dir), os.path.abspath(path))
                        )
                        == os.path.abspath(self.download_dir)
                        for _, path in file_ops.attempts
                    )
                )
                destination = os.path.join(self.download_dir, expected_name)
                with open(destination, "rb") as downloaded:
                    self.assertEqual(downloaded.read(), b"movie")
                self.assertEqual(os.listdir(self.download_dir), [expected_name])
                os.remove(destination)

    def test_download_streams_to_validated_destination_using_real_service(self):
        self.client.link_results = ["https://cdn.invalid/movie"]
        self.client.info_results = [
            xml("<response><name>Příliš.mkv</name><ident>abc</ident></response>")
        ]
        self.worker_session.queue_response(
            FakeResponse(
                content=b"movie",
                headers={"Content-Length": "5"},
                chunks=[b"movie"],
                forbid_content=True,
            )
        )

        result = self.handlers["download"]({"action": "download", "ident": "abc"})

        self.assertTrue(result.handled)
        self.assertIn("Downloaded", self.wait_for_job())
        destination = os.path.join(self.download_dir, "Prilis.mkv")
        with open(destination, "rb") as downloaded:
            self.assertEqual(downloaded.read(), b"movie")
        self.assertEqual(len(self.worker_session.responses), 0)

    def test_whitespace_only_searchdb_is_cancelled_without_downloading_database(self):
        self.keyboard_values = [" \t "]
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        self.worker_session.queue_response(FakeResponse(content=archive_bytes()))

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
        self.worker_session.queue_response(
            FakeResponse(content=archive_bytes("F.txt"), forbid_content=True)
        )

        result = self.runner.run_directory(
            lambda: self.handlers["searchdb"](
                {"action": "searchdb", "query": "  Film  "}
            )
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(len(self.plugin.items), 1)
        self.assertIn("Backup DB installed", self.wait_for_job())
        warm = self.runner.run_directory(
            lambda: self.handlers["searchdb"](
                {"action": "searchdb", "query": "  Film  "}
            )
        )
        self.assertTrue(warm.succeeded)
        self.assertTrue(self.find_item("Film"))
        self.assertEqual(len(self.plugin.end_of_directory_calls), 2)
        self.assertTrue(all(call["succeeded"] for call in self.plugin.end_of_directory_calls))

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

    def test_backup_db_streams_mixed_compression_and_member_larger_than_chunk(self):
        output = io.BytesIO()
        large_payload = json.dumps(
            {
                "data": [
                    {
                        "id": "movie-1",
                        "title": "Film",
                        "plot": "x" * (3 * 256 * 1024),
                        "streams": [],
                    }
                ]
            }
        ).encode("utf-8")
        small_payload = b'{"data": []}'
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("A.txt", large_payload, compress_type=zipfile.ZIP_STORED)
            archive.writestr("B.txt", small_payload, compress_type=zipfile.ZIP_DEFLATED)
        archive_data = output.getvalue()
        chunks = [archive_data[index:index + 32768] for index in range(0, len(archive_data), 32768)]
        response = FakeResponse(
            content=archive_data,
            chunks=chunks,
            forbid_content=True,
        )
        requested_chunk_sizes = []
        real_iter_content = response.iter_content

        def recording_iter_content(chunk_size=1, decode_unicode=False):
            requested_chunk_sizes.append(chunk_size)
            return real_iter_content(chunk_size, decode_unicode)

        response.iter_content = recording_iter_content
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        self.worker_session.queue_response(response)

        result = self.runner.run_directory(
            lambda: self.handlers["db"]({"action": "db", "file": "A.txt"})
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(len(self.plugin.items), 1)
        self.assertIn("Backup DB installed", self.wait_for_job())
        warm = self.runner.run_directory(
            lambda: self.handlers["db"]({"action": "db", "file": "A.txt"})
        )
        self.assertTrue(warm.succeeded)
        self.assertTrue(self.find_item("Film"))
        self.assertEqual(requested_chunk_sizes, [256 * 1024])
        self.assertGreater(len(large_payload), requested_chunk_sizes[0])
        self.assertEqual(response.raise_for_status_calls, 1)
        self.assertTrue(response.closed)
        self.assertEqual(
            sorted(os.listdir(self.profile)), [".backup-db-validation.json", "db"]
        )

    def test_backup_db_http_and_stream_failures_close_response_and_remove_temporaries(self):
        responses = [
            FakeResponse(
                content=archive_bytes(),
                status_code=503,
                forbid_content=True,
            ),
            FakeResponse(
                content=archive_bytes(),
                chunks=[archive_bytes()[:32], archive_bytes()[32:]],
                stream_error=OSError("synthetic stream interruption"),
                forbid_content=True,
            ),
        ]
        iter_calls = []
        real_iter_content = responses[1].iter_content

        def recording_iter_content(chunk_size=1, decode_unicode=False):
            iter_calls.append(chunk_size)
            return real_iter_content(chunk_size, decode_unicode)

        responses[1].iter_content = recording_iter_content
        self.client.link_results = ["https://cdn.invalid/db.zip"] * len(responses)
        for response in responses:
            self.worker_session.queue_response(response)

        for index, response in enumerate(responses):
            with self.subTest(index=index):
                result = self.runner.run_directory(
                    lambda: self.handlers["db"]({"action": "db"})
                )
                self.assertTrue(result.succeeded)
                self.assertIn("Backup DB installation failed", self.wait_for_job())
                self.assertTrue(response.closed)
                self.assertEqual(os.listdir(self.profile), [])
        self.assertEqual(iter_calls, [256 * 1024])

    def test_failed_backup_db_install_can_be_retried_after_cleanup(self):
        self.client.link_results = ["https://cdn.invalid/db.zip", "https://cdn.invalid/db.zip"]
        self.worker_session.queue_response(FakeResponse(content=b"not a zip", forbid_content=True))
        self.worker_session.queue_response(FakeResponse(content=archive_bytes(), forbid_content=True))

        failed = self.runner.run_directory(lambda: self.handlers["db"]({"action": "db"}))
        self.assertTrue(failed.succeeded)
        self.assertIn("Backup DB installation failed", self.wait_for_job())
        repaired = self.runner.run_directory(lambda: self.handlers["db"]({"action": "db"}))
        self.assertTrue(repaired.succeeded)
        self.assertIn("Backup DB installed", self.wait_for_job())
        repaired_listing = self.runner.run_directory(
            lambda: self.handlers["db"]({"action": "db", "file": "A.txt"})
        )

        self.assertTrue(repaired_listing.succeeded)
        self.assertEqual(len(self.plugin.end_of_directory_calls), 3)
        self.assertTrue(self.find_item("Film"))
        self.assertFalse(os.path.exists(os.path.join(self.profile, "db.zip")))

    def test_queued_media_returns_before_blocked_stream_and_completes_after_release(self):
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        events = []
        response = FakeResponse(headers={"Content-Length": "5"}, forbid_content=True)

        def blocked_stream(chunk_size=1, decode_unicode=False):
            events.append("stream-entered")
            entered.set()
            if not release.wait(2):
                raise AssertionError("blocked media stream was not released")
            events.append("stream-released")
            yield b"movie"

        response.iter_content = blocked_stream
        self.worker_session.queue_response(response)
        self.client.link_results = ["https://cdn.invalid/movie"]
        self.client.info_results = [
            xml("<response><name>Movie.mkv</name><ident>abc</ident></response>")
        ]
        results = []

        def invoke():
            events.append("invocation-entered")
            results.append(
                self.runner.run_action(
                    lambda: self.handlers["download"](
                        {"action": "download", "ident": "abc"}
                    )
                )
            )
            events.append("invocation-returned")
            returned.set()

        invocation = threading.Thread(target=invoke)
        invocation.start()
        self.assertTrue(returned.wait(2), "media handoff waited for its blocked stream")
        invocation.join(2)
        self.assertTrue(entered.wait(2))
        self.assertTrue(results[0].handled)
        self.assertFalse(self.job_event.is_set())
        self.assertFalse(os.path.exists(os.path.join(self.download_dir, "Movie.mkv")))

        release.set()

        self.assertIn("Downloaded", self.wait_for_job())
        self.assertTrue(response.closed)
        with open(os.path.join(self.download_dir, "Movie.mkv"), "rb") as downloaded:
            self.assertEqual(downloaded.read(), b"movie")
        self.assertLess(events.index("invocation-returned"), events.index("stream-released"))
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertEqual(self.plugin.resolved_url_calls[0]["handle"], 9)

    def test_close_during_media_stream_cleans_partial_without_stale_success(self):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        response = FakeResponse(headers={"Content-Length": "5"}, forbid_content=True)
        close_response = response.close

        def observed_close():
            close_response()
            closed.set()

        def blocked_stream(chunk_size=1, decode_unicode=False):
            entered.set()
            if not release.wait(2):
                raise AssertionError("blocked closing media stream was not released")
            yield b"movie"

        response.close = observed_close
        response.iter_content = blocked_stream
        self.worker_session.queue_response(response)
        self.client.link_results = ["https://cdn.invalid/movie"]
        self.client.info_results = [
            xml("<response><name>Movie.mkv</name><ident>abc</ident></response>")
        ]
        accepted = self.handlers["download"]({"action": "download", "ident": "abc"})
        self.assertTrue(accepted.handled)
        self.assertTrue(entered.wait(2))

        self.download_jobs.close(wait=False)
        release.set()

        self.assertTrue(closed.wait(2))
        self.assertEqual(os.listdir(self.download_dir), [])
        self.assertFalse(any(message.startswith("Downloaded") for message in self.job_messages))
        self.download_jobs.close(wait=False)

    def test_duplicate_media_destination_is_rejected_without_second_remote_operation(self):
        entered = threading.Event()
        release = threading.Event()
        response = FakeResponse(headers={"Content-Length": "5"}, forbid_content=True)

        def blocked_stream(chunk_size=1, decode_unicode=False):
            entered.set()
            if not release.wait(2):
                raise AssertionError("blocked duplicate stream was not released")
            yield b"movie"

        response.iter_content = blocked_stream
        self.worker_session.queue_response(response)
        self.client.link_results = ["https://cdn.invalid/movie"]
        self.client.info_results = [
            xml("<response><name>Movie.mkv</name><ident>abc</ident></response>")
        ]
        params = {"action": "download", "ident": "abc", "name": "Movie.mkv"}
        accepted = self.handlers["download"](params)
        self.assertTrue(accepted.handled)
        self.assertTrue(entered.wait(2))
        api_call_count = len(self.client.api_calls)

        duplicate = self.handlers["download"](params)

        self.assertFalse(duplicate.handled)
        self.assertEqual(len(self.client.api_calls), api_call_count)
        self.assertIn("Download could not be queued", self.plugin.notifications[-1]["message"])
        release.set()
        self.assertIn("Downloaded", self.wait_for_job())

    def test_cold_database_handoff_has_one_reservation_no_partial_read_and_one_refresh(self):
        entered = threading.Event()
        release = threading.Event()
        payload = archive_bytes()
        response = FakeResponse(content=payload, forbid_content=True)

        def blocked_stream(chunk_size=1, decode_unicode=False):
            entered.set()
            if not release.wait(2):
                raise AssertionError("blocked database stream was not released")
            yield payload

        response.iter_content = blocked_stream
        self.worker_session.queue_response(response)
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        returned = threading.Event()
        results = []

        def invoke():
            results.append(self.handlers["db"]({"action": "db", "file": "A.txt"}))
            returned.set()

        invocation = threading.Thread(target=invoke)
        invocation.start()
        self.assertTrue(returned.wait(2), "cold DB handoff waited for its blocked archive")
        invocation.join(2)
        self.assertTrue(entered.wait(2))
        self.assertTrue(results[0].succeeded)
        self.assertEqual(len(self.plugin.items), 1)
        self.assertEqual(
            self.plugin.items[0]["listitem"].label,
            "Backup DB installation in progress",
        )
        self.assertFalse(os.path.exists(os.path.join(self.profile, "db")))
        self.assertEqual(self.client.revalidate_calls, 1)
        api_call_count = len(self.client.api_calls)

        concurrent = self.handlers["db"]({"action": "db", "file": "A.txt"})

        self.assertTrue(concurrent.succeeded)
        self.assertEqual(len(self.plugin.items), 2)
        self.assertEqual(len(self.client.api_calls), api_call_count)
        self.assertEqual(self.plugin.builtin_commands, [])
        self.assertFalse(os.path.exists(os.path.join(self.profile, "db")))

        release.set()

        self.assertIn("Backup DB installed", self.wait_for_job())
        self.assertEqual(self.plugin.builtin_commands, ["Container.Refresh"])
        self.assertTrue(response.closed)
        warm = self.handlers["db"]({"action": "db", "file": "A.txt"})
        self.assertTrue(warm.succeeded)
        self.assertTrue(self.find_item("Film"))
        self.assertEqual(self.client.revalidate_calls, 1)
        self.assertEqual(len(self.client.api_calls), api_call_count)
        self.assertEqual(self.plugin.builtin_commands, ["Container.Refresh"])

    def test_cold_database_queue_action_runs_after_install_on_worker_client(self):
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        self.client.api_responses["queue_file"] = [
            xml("<response><status>OK</status></response>")
        ]
        self.worker_session.queue_response(
            FakeResponse(content=archive_bytes(), forbid_content=True)
        )

        accepted = self.handlers["db"]({"action": "db", "toqueue": "stream-1"})

        self.assertTrue(accepted.succeeded)
        self.assertIn("Backup DB installed", self.wait_for_job())
        self.assertEqual(
            [call[0] for call in self.client.api_calls],
            ["get_link", "queue_file"],
        )
        self.assertEqual(
            self.client.api_calls[-1][1],
            {"ident": "stream-1", "wst": "token"},
        )
        self.assertEqual(
            [call["message"] for call in self.plugin.notifications].count("Queued"),
            1,
        )
        self.assertEqual(self.plugin.builtin_commands, ["Container.Refresh"])

    def test_installed_notification_error_cannot_suppress_refresh_or_change_job_outcome(self):
        from yawsp_app.backup_db import BackupDBStore
        from yawsp_app.download_jobs import backup_db_job_key

        notification_logged = threading.Event()
        refreshed = threading.Event()
        runtime_logger = self.runtime.logger

        class NotificationLogger(type(runtime_logger)):
            def exception(inner, *args, **kwargs):
                super().exception(*args, **kwargs)
                if args and args[0] == "Backup DB installed notification failed":
                    notification_logged.set()

        logger = NotificationLogger()
        self.runtime.logger = logger

        def broken_notify(message):
            if message == "Backup DB installed":
                raise RuntimeError("synthetic installed notification failure")

        def refresh():
            self.plugin.executeBuiltin("Container.Refresh")
            refreshed.set()

        handlers = build_webshare_handlers(
            self.runtime,
            lambda: self.client,
            download_jobs=self.download_jobs,
            worker_client_factory=lambda session: self.client,
            notify=broken_notify,
            refresh=refresh,
        )
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        self.worker_session.queue_response(
            FakeResponse(content=archive_bytes(), forbid_content=True)
        )

        accepted = handlers["db"]({"action": "db"})

        self.assertTrue(accepted.succeeded)
        self.assertTrue(notification_logged.wait(2))
        self.assertTrue(refreshed.wait(2))
        self.assertEqual(
            self.download_jobs.status(backup_db_job_key(self.profile)),
            "succeeded",
        )
        self.assertTrue(BackupDBStore(self.profile).is_valid())
        self.assertEqual(self.plugin.builtin_commands, ["Container.Refresh"])
        self.assertTrue(
            any(
                call[0][0] == "Backup DB installed notification failed"
                for call in logger.calls
            )
        )
        warm = handlers["db"]({"action": "db", "file": "A.txt"})
        self.assertTrue(warm.succeeded)
        self.assertTrue(self.find_item("Film"))
        self.assertEqual(self.plugin.builtin_commands, ["Container.Refresh"])

    def test_cold_queue_notification_error_cannot_change_install_outcome_or_refresh(self):
        from yawsp_app.backup_db import BackupDBStore
        from yawsp_app.download_jobs import backup_db_job_key

        attempted = threading.Event()
        installed = threading.Event()
        refreshed = threading.Event()

        def broken_notify(message):
            if message == "Queued":
                attempted.set()
                raise RuntimeError("synthetic queue notification failure")
            if message == "Backup DB installed":
                installed.set()

        def refresh():
            self.plugin.executeBuiltin("Container.Refresh")
            refreshed.set()

        handlers = build_webshare_handlers(
            self.runtime,
            lambda: self.client,
            download_jobs=self.download_jobs,
            worker_client_factory=lambda session: self.client,
            notify=broken_notify,
            refresh=refresh,
        )
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        self.client.api_responses["queue_file"] = [
            xml("<response><status>OK</status></response>")
        ]
        self.worker_session.queue_response(
            FakeResponse(content=archive_bytes(), forbid_content=True)
        )

        accepted = handlers["db"]({"action": "db", "toqueue": "stream-1"})

        self.assertTrue(accepted.succeeded)
        self.assertTrue(attempted.wait(2))
        self.assertTrue(refreshed.wait(2))
        self.assertTrue(installed.wait(2))
        self.assertEqual(
            self.download_jobs.status(backup_db_job_key(self.profile)),
            "succeeded",
        )
        self.assertTrue(BackupDBStore(self.profile).is_valid())
        self.assertEqual(self.plugin.builtin_commands, ["Container.Refresh"])
        self.assertTrue(
            any(
                call[0][0] == "Backup DB queue notification failed"
                for call in self.logger.calls
            )
        )

    def test_database_rechecks_succeeded_reservation_before_starting_another_install(self):
        from yawsp_app.backup_db import BackupDBStore
        from yawsp_app.download_jobs import backup_db_job_key

        BackupDBStore(self.profile).ensure_installed(lambda: archive_bytes())
        manager = mock.Mock()
        manager.status.return_value = "succeeded"
        handlers = build_webshare_handlers(
            self.runtime,
            lambda: self.client,
            download_jobs=manager,
            worker_client_factory=lambda session: self.client,
            notify=self.runtime.notify,
            refresh=lambda: None,
        )
        with mock.patch.object(BackupDBStore, "is_valid", side_effect=[False, True]):
            result = handlers["db"]({"action": "db", "file": "A.txt"})

        self.assertTrue(result.succeeded)
        self.assertTrue(self.find_item("Film"))
        manager.submit.assert_not_called()
        manager.status.assert_called_with(backup_db_job_key(self.profile))

    def test_failed_database_job_does_not_refresh_or_expose_a_partial_listing(self):
        self.client.link_results = ["https://cdn.invalid/db.zip"]
        self.worker_session.queue_response(
            FakeResponse(content=b"not a zip", forbid_content=True)
        )

        accepted = self.handlers["db"]({"action": "db"})

        self.assertTrue(accepted.succeeded)
        self.assertEqual(len(self.plugin.items), 1)
        self.assertIn("Backup DB installation failed", self.wait_for_job())
        self.assertEqual(self.plugin.builtin_commands, [])
        self.assertFalse(os.path.exists(os.path.join(self.profile, "db")))

    def test_settings_is_an_action_without_directory_completion(self):
        result = self.runner.run_action(lambda: self.handlers["settings"]({"action": "settings"}))

        self.assertTrue(result.handled)
        self.assertEqual(self.addon.open_settings_calls, 1)
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertEqual(self.plugin.end_of_directory_calls, [])


if __name__ == "__main__":
    unittest.main()
