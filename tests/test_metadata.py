import unittest

from yawsp_app.metadata import apply_video_metadata


class RecordingInfoTag:
    """Only the documented Kodi setters used by the adapter are available."""

    def __init__(self):
        self.calls = []

    def setTitle(self, title):
        self.calls.append(("setTitle", title))

    def setMediaType(self, media_type):
        self.calls.append(("setMediaType", media_type))

    def setPlot(self, plot):
        self.calls.append(("setPlot", plot))

    def setYear(self, year):
        self.calls.append(("setYear", year))

    def setGenres(self, genres):
        self.calls.append(("setGenres", genres))

    def setRating(self, rating):
        self.calls.append(("setRating", rating))

    def setSeason(self, season):
        self.calls.append(("setSeason", season))

    def setEpisode(self, episode):
        self.calls.append(("setEpisode", episode))

    def setDuration(self, duration):
        self.calls.append(("setDuration", duration))

    def setFirstAired(self, first_aired):
        self.calls.append(("setFirstAired", first_aired))


class NativeListItem:
    def __init__(self, tag=None):
        self.tag = tag if tag is not None else RecordingInfoTag()
        self.get_tag_calls = 0
        self.legacy_calls = []

    def getVideoInfoTag(self):
        self.get_tag_calls += 1
        return self.tag

    def setInfo(self, kind, info):
        self.legacy_calls.append((kind, info))


class LegacyListItem:
    def __init__(self):
        self.calls = []

    def setInfo(self, kind, info):
        self.calls.append((kind, info))


class MetadataTests(unittest.TestCase):
    def test_native_info_tag_receives_every_supported_value_with_exact_types(self):
        item = NativeListItem()
        info = {
            "title": "Příliš žluťoučký",
            "mediatype": "episode",
            "plot": "Díl o štěněti",
            "year": 2026,
            "genre": "Drama / Comedy",
            "rating": 7.25,
            "season": 2,
            "episode": 4,
            "duration": 3600,
            "aired": "2026-11-25",
        }

        result = apply_video_metadata(item, info)

        self.assertIsNone(result)
        self.assertEqual(item.get_tag_calls, 1)
        self.assertEqual(item.legacy_calls, [])
        self.assertEqual(
            item.tag.calls,
            [
                ("setTitle", "Příliš žluťoučký"),
                ("setMediaType", "episode"),
                ("setPlot", "Díl o štěněti"),
                ("setYear", 2026),
                ("setGenres", ["Drama", "Comedy"]),
                ("setRating", 7.25),
                ("setSeason", 2),
                ("setEpisode", 4),
                ("setDuration", 3600),
                ("setFirstAired", "2026-11-25"),
            ],
        )
        values = dict(item.tag.calls)
        self.assertIs(type(values["setYear"]), int)
        self.assertIs(type(values["setRating"]), float)
        self.assertIs(type(values["setSeason"]), int)
        self.assertIs(type(values["setEpisode"]), int)
        self.assertIs(type(values["setDuration"]), int)
        self.assertIs(type(values["setGenres"]), list)

    def test_explicit_genre_lists_and_tuples_are_copied_not_aliased(self):
        for original in (["Drama", "Comedy"], ("Drama", "Comedy")):
            with self.subTest(input_type=type(original)):
                item = NativeListItem()

                apply_video_metadata(item, {"genre": original})

                applied = item.tag.calls[0][1]
                self.assertEqual(applied, ["Drama", "Comedy"])
                self.assertIs(type(applied), list)
                self.assertIsNot(applied, original)

    def test_legacy_double_receives_the_original_info_dict_exactly_once(self):
        item = LegacyListItem()
        info = {
            "title": "Legacy",
            "genre": "Drama / Comedy",
            "unsupported": "legacy Kodi retains its default semantics",
        }

        result = apply_video_metadata(item, info)

        self.assertIsNone(result)
        self.assertEqual(len(item.calls), 1)
        self.assertEqual(item.calls[0][0], "video")
        self.assertIs(item.calls[0][1], info)

    def test_native_unknown_keys_are_rejected_before_any_supported_setter(self):
        item = NativeListItem()

        with self.assertRaisesRegex(ValueError, "Title, unsupported"):
            apply_video_metadata(
                item, {"title": "Must not be partially applied", "Title": "Bad", "unsupported": 1}
            )

        self.assertEqual(item.tag.calls, [])
        self.assertEqual(item.legacy_calls, [])
        self.assertEqual(item.get_tag_calls, 1)

    def test_missing_native_setter_is_not_misreported_as_a_legacy_double(self):
        class IncompleteInfoTag:
            def setTitle(self, title):
                pass

        item = NativeListItem(IncompleteInfoTag())

        with self.assertRaises(AttributeError):
            apply_video_metadata(item, {"title": "Native", "plot": "No fake setter"})

        self.assertEqual(item.legacy_calls, [])
        self.assertEqual(item.get_tag_calls, 1)


if __name__ == "__main__":
    unittest.main()
