import unittest

from yawsp_app.search import (
    SearchQuery,
    czech_sort_key,
    matches_filename,
    page_offsets,
    parse_query,
)


class ParseQueryTests(unittest.TestCase):
    def test_browse_sentinel_has_empty_api_text_and_unfiltered_results(self):
        query = parse_query("%#NONE#%")

        self.assertEqual(query.api_text, "")
        self.assertEqual(query.tokens, ())
        self.assertTrue(query.browsing)
        self.assertTrue(matches_filename(query, "Any filename at all.mkv"))

    def test_episode_notation_is_normalized_for_api_and_matching(self):
        query = parse_query("Show S1E2")

        self.assertEqual(query.api_text, "Show S01E02")
        self.assertEqual(query.tokens, ("show", "s01e02"))
        self.assertTrue(matches_filename(query, "Show.S01E02.1080p.mkv"))
        self.assertTrue(matches_filename(query, "Show.S1E2.1080p.mkv"))
        self.assertFalse(matches_filename(query, "Show.S01E03.1080p.mkv"))

    def test_diacritic_only_query_does_not_match_every_filename(self):
        query = parse_query("Ž")

        self.assertEqual(query.tokens, ("z",))
        self.assertFalse(query.browsing)
        self.assertTrue(matches_filename(query, "Želva.mkv"))
        self.assertTrue(matches_filename(query, "Zelva.mkv"))
        self.assertFalse(matches_filename(query, "Adam.mkv"))

    def test_mixed_czech_query_matches_punctuation_and_ascii_filename(self):
        query = parse_query("  Příliš   ŽLUŤOUČKÝ, kůň! ")

        self.assertEqual(query.api_text, "Příliš   ŽLUŤOUČKÝ, kůň!")
        self.assertEqual(query.tokens, ("prilis", "zlutoucky", "kun"))
        self.assertTrue(
            matches_filename(query, "Prilis.Zlutoucky-Kun.1080p.WEB-DL.mkv")
        )
        self.assertFalse(matches_filename(query, "Prilis.Zlutoucky.Pes.mkv"))

    def test_tokens_can_match_in_any_order(self):
        query = parse_query("kůň příliš")

        self.assertTrue(matches_filename(query, "Příliš - kůň.mkv"))

    def test_empty_input_is_not_browsing_and_never_matches(self):
        query = parse_query("  \t\n ")

        self.assertEqual(query.api_text, "")
        self.assertEqual(query.tokens, ())
        self.assertFalse(query.browsing)
        self.assertFalse(matches_filename(query, "Anything.mkv"))

    def test_punctuation_only_input_never_matches_through_empty_tokens(self):
        query = parse_query("--- !!!")

        self.assertEqual(query.tokens, ())
        self.assertFalse(query.browsing)
        self.assertFalse(matches_filename(query, "Anything.mkv"))

    def test_explicit_empty_non_browsing_query_never_matches(self):
        query = SearchQuery(api_text="", tokens=(), browsing=False)

        self.assertFalse(matches_filename(query, "Anything.mkv"))


class PaginationTests(unittest.TestCase):
    def test_first_full_page_has_only_next_offset(self):
        self.assertEqual(page_offsets(0, 500, 500), (None, 500))

    def test_more_raw_results_than_limit_still_advance_one_page(self):
        self.assertEqual(page_offsets(0, 500, 501), (None, 500))

    def test_short_later_page_has_only_previous_offset(self):
        self.assertEqual(page_offsets(500, 500, 1), (0, None))

    def test_full_later_page_has_both_offsets(self):
        self.assertEqual(page_offsets(500, 500, 500), (0, 1000))

    def test_short_first_page_has_no_offsets(self):
        self.assertEqual(page_offsets(0, 500, 0), (None, None))


class CzechCollationTests(unittest.TestCase):
    def test_exact_czech_order_with_digraph_and_diacritics(self):
        names = ["Žluťoučký", "Chata", "Ábel", "Iva", "Helena", "Adam"]

        self.assertEqual(
            sorted(names, key=czech_sort_key),
            ["Adam", "Ábel", "Helena", "Chata", "Iva", "Žluťoučký"],
        )


if __name__ == "__main__":
    unittest.main()
