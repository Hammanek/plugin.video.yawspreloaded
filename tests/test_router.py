import unittest

from tests.support.kodi_stubs import KodiRecorder
from tests.test_runtime import RecordingLogger
from yawsp_app.router import dispatch
from yawsp_app.runtime import (
    ActionResult,
    ActionRunner,
    CompletionKind,
    DirectoryResult,
    PlaybackResult,
    completion_for,
)


class ListItem:
    pass


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.plugin = KodiRecorder()
        self.runner = ActionRunner(self.plugin, 3, ListItem, logger=RecordingLogger())
        self.received = []

        def directory(params):
            self.received.append(params)
            return DirectoryResult()

        self.handlers = {
            "menu": directory,
            "search": directory,
            "queue": directory,
            "history": directory,
            "play": lambda params: PlaybackResult(False, ListItem()),
            "settings": lambda params: ActionResult(),
            "download": lambda params: ActionResult(),
            "info": lambda params: ActionResult(),
            "tmdb_auth": lambda params: ActionResult(),
            "tmdb_trailer": lambda params: ActionResult(),
        }

    def test_empty_and_unknown_actions_use_directory_adapter_once(self):
        dispatch("", self.handlers, self.runner)
        dispatch("action=not_migrated", self.handlers, self.runner)

        self.assertEqual(self.received, [{}, {"action": "not_migrated"}])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 2)
        self.assertEqual(self.plugin.resolved_url_calls, [])

    def test_original_query_parsing_is_retained(self):
        dispatch(
            "action=search&what=first&what=last&empty=&encoded=P%C5%99%C3%ADli%C5%A1",
            self.handlers,
            self.runner,
        )

        self.assertEqual(
            self.received[0],
            {
                "action": "search",
                "what": "last",
                "encoded": "Příliš",
            },
        )

    def test_matrix_routes_use_only_their_selected_adapter(self):
        cases = (
            ("action=search", CompletionKind.DIRECTORY),
            ("action=queue", CompletionKind.DIRECTORY),
            ("action=history", CompletionKind.DIRECTORY),
            ("action=db", CompletionKind.DIRECTORY),
            ("action=searchdb", CompletionKind.DIRECTORY),
            ("action=play&ident=abc", CompletionKind.PLAYBACK),
            ("action=settings", CompletionKind.ACTION),
            ("action=download&ident=abc", CompletionKind.ACTION),
            ("action=info&ident=abc", CompletionKind.ACTION),
            ("action=tmdb_auth", CompletionKind.ACTION),
            ("action=tmdb_trailer&media=movie&id=42", CompletionKind.ACTION),
        )

        for query, kind in cases:
            with self.subTest(query=query):
                dispatch(query, self.handlers, self.runner)
                self.assertIs(
                    completion_for(query.split("=", 1)[1].split("&", 1)[0], {}),
                    kind,
                )

        self.assertEqual(len(self.plugin.end_of_directory_calls), 5)
        self.assertEqual(len(self.plugin.resolved_url_calls), 6)
        self.assertEqual(
            [call["succeeded"] for call in self.plugin.resolved_url_calls],
            [False, False, False, False, False, False],
        )

    def test_dynamic_tmdb_watched_routes_to_action_before_handler_migration(self):
        calls = []

        def watchlist(params):
            if not params.get("watched"):
                return DirectoryResult()
            calls.append(params)
            return ActionResult(handled=True, refresh=True)

        handlers = dict(self.handlers, tmdb_watchlist=watchlist)

        dispatch("action=tmdb_watchlist&id=42", handlers, self.runner)
        dispatch("action=tmdb_watchlist&id=42&watched=1", handlers, self.runner)

        self.assertEqual(calls, [{"action": "tmdb_watchlist", "id": "42", "watched": "1"}])
        self.assertEqual(len(self.plugin.end_of_directory_calls), 1)
        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertEqual(self.plugin.builtin_commands, ["Container.Refresh"])

    def test_cancelled_directory_and_unexpected_exception_still_complete_once(self):
        self.handlers["search"] = lambda params: DirectoryResult()
        self.handlers["history"] = lambda params: (_ for _ in ()).throw(ValueError("bad"))

        dispatch("action=search", self.handlers, self.runner)
        dispatch("action=history", self.handlers, self.runner)

        self.assertEqual(len(self.plugin.end_of_directory_calls), 2)
        self.assertEqual(
            [call["succeeded"] for call in self.plugin.end_of_directory_calls],
            [True, False],
        )

    def test_handler_receives_the_same_params_used_for_completion_selection(self):
        seen = []

        def watched(params):
            seen.append(params)
            return ActionResult(handled=False)

        dispatch(
            "action=tmdb_watchlist&watched=0",
            dict(self.handlers, tmdb_watchlist=watched),
            self.runner,
        )

        self.assertEqual(len(self.plugin.resolved_url_calls), 1)
        self.assertEqual(seen[0]["watched"], "0")


if __name__ == "__main__":
    unittest.main()
