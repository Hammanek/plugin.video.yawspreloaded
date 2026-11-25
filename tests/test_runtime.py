import unittest

from tests.support.kodi_stubs import KodiRecorder
from yawsp_app.runtime import (
    ActionResult,
    ActionRunner,
    CompletionKind,
    DirectoryResult,
    PlaybackResult,
    completion_for,
)


class ListItem:
    def __init__(self, label="", path=""):
        self.label = label
        self.path = path
        self.properties = {}

    def setProperty(self, key, value):
        self.properties[key] = value


class RecordingLogger:
    def __init__(self):
        self.calls = []

    def exception(self, *args, **kwargs):
        self.calls.append((args, kwargs))


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

    def executeBuiltin(self, command):
        self.events.append(command)
        return super().executeBuiltin(command)


class CompletionSelectionTests(unittest.TestCase):
    def test_every_matrix_action_has_its_required_completion_kind(self):
        cases = (
            ({}, CompletionKind.DIRECTORY),
            ({"action": "unknown"}, CompletionKind.DIRECTORY),
            ({"action": "search"}, CompletionKind.DIRECTORY),
            ({"action": "queue"}, CompletionKind.DIRECTORY),
            ({"action": "history"}, CompletionKind.DIRECTORY),
            ({"action": "db"}, CompletionKind.DIRECTORY),
            ({"action": "searchdb"}, CompletionKind.DIRECTORY),
            ({"action": "tmdb_watchlist"}, CompletionKind.DIRECTORY),
            (
                {"action": "tmdb_watchlist", "media": "tv", "season": "2"},
                CompletionKind.DIRECTORY,
            ),
            (
                {"action": "tmdb_watchlist", "watched": "1"},
                CompletionKind.ACTION,
            ),
            ({"action": "play"}, CompletionKind.PLAYBACK),
            ({"action": "settings"}, CompletionKind.ACTION),
            ({"action": "download"}, CompletionKind.ACTION),
            ({"action": "info"}, CompletionKind.ACTION),
            ({"action": "tmdb_auth"}, CompletionKind.ACTION),
            ({"action": "tmdb_trailer"}, CompletionKind.ACTION),
        )

        for params, expected in cases:
            with self.subTest(params=params):
                self.assertIs(completion_for(params.get("action"), params), expected)

    def test_empty_watched_parameter_remains_a_directory(self):
        self.assertIs(
            completion_for("tmdb_watchlist", {"action": "tmdb_watchlist", "watched": ""}),
            CompletionKind.DIRECTORY,
        )


class ActionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.plugin = OrderingRecorder(self.events)
        self.errors = []
        self.logger = RecordingLogger()
        self.runner = ActionRunner(
            self.plugin,
            handle=7,
            listitem_factory=ListItem,
            logger=self.logger,
            error_handler=self.errors.append,
        )

    def test_directory_handler_cannot_inner_complete_and_adapter_ends_once(self):
        def handler():
            self.events.append("handler")
            return DirectoryResult(update_listing=True, cache_to_disc=False)

        result = self.runner.run_directory(handler)

        self.assertEqual(result, DirectoryResult(True, True, False))
        self.assertEqual(self.events, ["handler", "end"])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)
        self.assertEqual(
            self.plugin.end_of_directory_calls[0],
            {
                "handle": 7,
                "succeeded": True,
                "updateListing": True,
                "cacheToDisc": False,
            },
        )
        self.assertEqual(self.plugin.resolved_url_calls, [])

    def test_directory_post_completion_hook_runs_after_the_only_end(self):
        def handler():
            self.events.append("handler")
            return DirectoryResult(post_complete=lambda: self.events.append("after"))

        result = self.runner.run_directory(handler)

        self.assertTrue(result.succeeded)
        self.assertEqual(self.events, ["handler", "end", "after"])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)

    def test_directory_failure_and_exception_both_end_unsuccessfully_once(self):
        results = (
            self.runner.run_directory(lambda: DirectoryResult(succeeded=False)),
            self.runner.run_directory(lambda: (_ for _ in ()).throw(ValueError("bad"))),
        )

        self.assertEqual([result.succeeded for result in results], [False, False])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 2)
        self.assertEqual(
            [call["succeeded"] for call in self.plugin.end_of_directory_calls],
            [False, False],
        )
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], ValueError)

    def test_directory_rejects_none_instead_of_returning_it(self):
        result = self.runner.run_directory(lambda: None)

        self.assertIsInstance(result, DirectoryResult)
        self.assertFalse(result.succeeded)
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], TypeError)
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)

    def test_playback_success_and_invalid_link_each_resolve_exactly_once(self):
        playable = ListItem("Movie", "https://cdn.invalid/movie")

        success = self.runner.run_play(lambda: PlaybackResult(True, playable))
        invalid = self.runner.run_play(
            lambda: PlaybackResult(False, ListItem("Movie"))
        )

        self.assertIs(success.list_item, playable)
        self.assertFalse(invalid.succeeded)
        self.assertEqual(len(self.plugin.resolved_url_calls), 2)
        self.assertEqual(
            [call["succeeded"] for call in self.plugin.resolved_url_calls], [True, False]
        )
        self.assertIs(self.plugin.resolved_url_calls[0]["listitem"], playable)
        self.assertEqual(self.plugin.end_of_directory_calls, [])

    def test_playback_exception_resolves_false_once_and_is_reported(self):
        result = self.runner.run_play(
            lambda: (_ for _ in ()).throw(RuntimeError("metadata failed"))
        )

        self.assertIsInstance(result, PlaybackResult)
        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.list_item)
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertFalse(self.plugin.resolved_url_calls[0]["succeeded"])
        self.assertEqual(len(self.errors), 1)

    def test_action_refresh_happens_only_after_false_resolution(self):
        def handler():
            self.events.append("handler")
            return ActionResult(handled=True, refresh=True)

        result = self.runner.run_action(handler)

        self.assertTrue(result.handled)
        self.assertEqual(self.events, ["handler", "resolve", "Container.Refresh"])
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertFalse(self.plugin.resolved_url_calls[0]["succeeded"])

    def test_unhandled_action_and_exception_never_refresh(self):
        self.runner.run_action(ActionResult)
        self.runner.run_action(lambda: ActionResult(handled=False, refresh=True))
        self.runner.run_action(lambda: (_ for _ in ()).throw(ValueError("bad API")))

        self.assertEqual(self.plugin.builtin_commands, [])
        self.assertEqual(len(self.plugin.resolved_url_calls), 3)
        self.assertTrue(
            all(not call["succeeded"] for call in self.plugin.resolved_url_calls)
        )

    def test_external_media_is_ordered_after_action_resolution(self):
        command = "PlayMedia(plugin://plugin.video.youtube/play/?video_id=abc123)"

        self.runner.run_action(
            lambda: ActionResult(handled=True, play_media=command)
        )

        self.assertEqual(self.events, ["resolve", command])

    def test_failed_lookup_never_invokes_external_media(self):
        self.runner.run_action(
            lambda: ActionResult(
                handled=False,
                play_media="PlayMedia(plugin://plugin.video.youtube/play/?video_id=bad)",
            )
        )

        self.assertEqual(self.plugin.builtin_commands, [])
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)


if __name__ == "__main__":
    unittest.main()
