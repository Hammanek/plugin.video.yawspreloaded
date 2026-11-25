import ast
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
EVIDENCE_DIR = ROOT / ".superpowers" / "sdd" / "2026-11-25-performance-remediation"
COVERAGE_PATH = EVIDENCE_DIR / "performance-coverage.json"
PERFORMANCE_IDS = {"PERF-{0:02d}".format(number) for number in range(1, 10)}


def module_definition(module_name, symbol_name):
    """Resolve mapped definitions without importing Kodi-facing modules."""
    path = ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    body = tree.body
    definition = None
    for component in symbol_name.split("."):
        definition = next(
            (
                node
                for node in body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == component
            ),
            None,
        )
        if definition is None:
            return None
        body = definition.body
    return definition


def public_native_result_contract(paragraph):
    """Evaluate native-result privacy and handoff semantics in one paragraph."""
    text = re.sub(r"\s+", " ", paragraph.strip())
    sessionless = (
        r"požadavky\s+zachovávají\s+`api_key`\s+a\s+lokalizaci,\s+"
        r"ale\s+nikdy\s+neposílají\s+`session_id`"
    )
    no_autoplay = r"bez\s+autoplay"
    no_watched = no_autoplay + r",\s+watched\s+změn"
    no_account_or_watchlist_queries = (
        no_watched + r",\s+account/watchlist\s+požadavků"
    )
    no_metadata_prefetch = (
        no_account_or_watchlist_queries
        + r",\s+dávkového\s+dotažování\s+metadat\s+či\s+background\s+prefetch"
    )
    no_personal_account_or_snapshot = (
        r"osobní\s+účet,\s+session\s+ani\s+watchlist\s+snapshot\s+nejsou\s+potřeba"
    )
    required = (
        ("webshare_search_not_player", r"Výsledek\s+otevře\s+běžné\s+hledání\s+Webshare"),
        ("mandatory_api_key", r"TMDB\s+API\s+klíč\s+zůstává\s+povinný"),
        ("sessionless_list_and_genre", sessionless),
        ("personal_account_session_not_required", no_personal_account_or_snapshot),
        ("watchlist_snapshot_not_required", no_personal_account_or_snapshot),
        ("no_autoplay", no_autoplay + r","),
        ("no_watched_mutations", no_watched + r","),
        ("no_account_queries", no_account_or_watchlist_queries + r","),
        ("no_watchlist_queries", no_account_or_watchlist_queries + r","),
        ("no_background_trailer_show_metadata_prefetch", no_metadata_prefetch),
    )
    reasons = [
        name
        for name, pattern in required
        if not re.search(pattern, text, re.IGNORECASE)
    ]

    # Remove only the two explicitly approved exclusion spans. Every separate
    # clause must still account for its own positive activity and negation.
    unchecked = re.sub(sessionless, " ", text, flags=re.IGNORECASE)
    unchecked = re.sub(
        no_metadata_prefetch, " ", unchecked, flags=re.IGNORECASE
    )

    def near(left, right):
        return r"(?:" + left + r")[^.;!?]{0,80}?(?:" + right + r")"

    def activities(target, action):
        return (near(target, action), near(action, target))

    def approved_negations(target, action):
        negation = (
            r"\b(?:no|not|never|without|skip(?:s|ping)?|avoid(?:s|ing)?|"
            r"do(?:es)?\s+not|don'?t|doesn'?t|žádn\w*|žiadn\w*|ziadn\w*|"
            r"nen[ií]|nejsou|nie|bez)\b"
        )
        return near(negation, target), near(negation, action)

    session = r"`?(?:session_id|session\s+id|tmdb_access_token)`?"
    session_action = (
        r"\b(?:send\w*|sent|pass\w*|include\w*|transmi\w*|present\w*|"
        r"forward\w*|provide?\w*|attach\w*|shar\w*|posíl\w*|odesíl\w*|"
        r"odesl\w*|odešl\w*|předáv\w*|předán\w*|zahrnuj\w*|vklád\w*|"
        r"posiel\w*|odosiel\w*|odosl\w*|odovzdáv\w*)\b"
    )
    auto = r"\b(?:autoplay|auto[\s-]+play(?:ing|back)?)\b"
    automatic_play = (
        r"\bautomatic(?:ally)?\b[^.;!?]{0,50}\bplay\w*"
        r"|\bplay\w*[^.;!?]{0,50}\bautomatic(?:ally)?\b"
        r"|\bautomatick\w*[^.;!?]{0,50}\bpřehráv\w*"
        r"|\bpřehráv\w*[^.;!?]{0,50}\bautomatick\w*"
    )
    player = r"\b(?:native|plugin|Kodi)\s+(?:native\s+)?player\b"
    player_action = (
        r"\b(?:start\w*|enable\w*|launch\w*|play\w*|call\w*|open\w*|"
        r"use\w*|activat\w*|invok\w*|spoušt\w*|přehráv\w*)\b"
    )
    watched = (
        r"\b(?:watched(?:[\s-]+(?:state|status|flags?))?|zhlédnut\w*|"
        r"zhliadnut\w*)\b"
    )
    watched_action = (
        r"\b(?:updat\w*|writ\w*|mutat\w*|chang\w*|modif\w*|mark\w*|"
        r"set\w*|toggle\w*|označ\w*|nastav\w*|změn\w*|zmen\w*|"
        r"uprav\w*|zapíš\w*|zapis\w*)\b"
    )
    account = r"\b(?:account|úč\w*|uc\w*)\b"
    watchlist = (
        r"\b(?:watchlist\w*(?:\s+\w+){0,2}\s+snapshot\w*|watchlist\w*|"
        r"snapshot\w*\s+watchlist\w*)\b"
    )
    data_action = (
        r"\b(?:quer\w*|request\w*|writ\w*|updat\w*|mutat\w*|chang\w*|"
        r"modif\w*|fetch\w*|read\w*|load\w*|sav\w*|delet\w*|refresh\w*|"
        r"sync\w*|operat\w*|operac\w*|dotaz\w*|požadavk\w*|poziadavk\w*|"
        r"zapis\w*|aktualiz\w*|změn\w*|zmen\w*|uprav\w*|načít\w*|"
        r"uklád\w*|uklad\w*|maže\w*)\b"
    )
    media = (
        r"\b(?:trailer\w*(?:/show\w*)?|show\w*|seriál\w*|serial\w*|"
        r"metadata|metadat\w*)\b"
    )
    preload = r"\b(?:prefetch\w*|preload\w*|přednač\w*|prednač\w*)\b"
    activity_rules = (
        (
            "positive_session_transmission",
            activities(session, session_action),
            approved_negations(session, session_action),
        ),
        (
            "positive_autoplay",
            (
                auto,
                automatic_play,
                near(player, player_action),
                near(player_action, player),
            ),
            approved_negations(auto + "|" + automatic_play, player_action),
        ),
        (
            "positive_watched_mutations",
            activities(watched, watched_action),
            approved_negations(watched, watched_action),
        ),
        (
            "positive_account_queries",
            activities(account, data_action),
            approved_negations(account, data_action),
        ),
        (
            "positive_watchlist_snapshot_operations",
            activities(watchlist, data_action),
            approved_negations(watchlist, data_action),
        ),
        (
            "positive_background_metadata_prefetch",
            (
                near(r"\bbackground\b", preload),
                near(preload, r"\bbackground\b"),
                near(r"\bmetadata\b", preload),
                near(preload, r"\bmetadata\b"),
                near(media, preload),
                near(preload, media),
                r"\bdávkov\w*[^.;!?]{0,50}\bdotaž\w*[^.;!?]{0,30}\bmetadat\b",
            ),
            approved_negations(media + r"|\bbackground\b", preload),
        ),
    )
    double_negation = re.compile(
        r"\b(?:not\s+(?:no|never|not)|never\s+not|nie\s+nie|není\s+bez|neni\s+bez)\b",
        re.IGNORECASE,
    )
    positive_reasons = set()
    coordinator = (
        r"\s*(?:,\s*)?\b(?:but|although|though|however|yet|whereas|while|"
        r"ale|avšak|však|and|or|nor|ani|i|a)\b\s*"
    )
    clauses = [
        clause.strip()
        for sentence in re.split(r"(?<=[.!?;])\s+|[\r\n]+", unchecked)
        for clause in re.split(coordinator, sentence, flags=re.IGNORECASE)
        if clause.strip()
    ]
    for clause in clauses:
        allow_negations = not double_negation.search(clause)
        for reason, positives, negatives in activity_rules:
            candidate = clause
            if allow_negations:
                for negative in negatives:
                    candidate = re.sub(
                        negative, " ", candidate, flags=re.IGNORECASE
                    )
            if any(
                re.search(positive, candidate, re.IGNORECASE)
                for positive in positives
            ):
                positive_reasons.add(reason)
    reasons.extend(
        name for name, _, _ in activity_rules if name in positive_reasons
    )
    return not reasons, reasons


class DocumentationContractTests(unittest.TestCase):
    def setUp(self):
        self.readme = README_PATH.read_text(encoding="utf-8")

    def test_readme_documents_each_module_responsibility(self):
        modules = (
            "main.py",
            "yawsp.py",
            "yawsp_app/runtime.py",
            "yawsp_app/webshare.py",
            "yawsp_app/search.py",
            "yawsp_app/downloads.py",
            "yawsp_app/json_cache.py",
            "yawsp_app/metadata.py",
            "yawsp_app/discovery_ui.py",
            "yawsp_app/tmdb.py",
            "yawsp_app/webshare_ui.py",
            "yawsp_app/tmdb_ui.py",
            "yawsp_app/router.py",
        )
        for module in modules:
            with self.subTest(module=module):
                row = re.search(
                    r"^\|\s*`" + re.escape(module) + r"`\s*\|\s*([^|]+?)\s*\|\s*$",
                    self.readme,
                    re.MULTILINE,
                )
                self.assertIsNotNone(row, "missing module/responsibility table row")
                self.assertTrue(row.group(1).strip(), "missing module responsibility")

    def test_readme_documents_unittest_command_and_offline_limits(self):
        self.assertIn("python -m unittest discover -s tests -v", self.readme)
        self.assertRegex(self.readme, r"(?i)bez (?:přístupu k )?síti|offline")
        self.assertRegex(self.readme, r"(?i)nebyl[ay]? .{0,100}(?:Kodi|Webshare|TMDB)")
        self.assertRegex(self.readme, r"(?i)žádné (?:skutečné )?přihlašovací údaje|bez přihlašovacích údajů")
        self.assertNotRegex(self.readme, r"(?i)Nebyla provedena živá ověření v reálném Kodi")
        self.assertIn("live smoke v hlavním profilu Kodi 21.2", self.readme)
        self.assertIn("338 výsledků", self.readme)
        self.assertIn("66 filmů", self.readme)

    def test_readme_preserves_version_installation_and_tmdb_qr_setup(self):
        addon_version = ET.parse(str(ROOT / "addon.xml")).getroot().get("version")
        self.assertEqual(addon_version, "2.0.0")
        self.assertIn("verze-2.0.0-blue.svg", self.readme)
        self.assertIn(
            "https://github.com/Hammanek/plugin.video.yawspreloaded/releases/latest",
            self.readme,
        )
        self.assertIn("Instalovat ze souboru zip", self.readme)
        self.assertIn("https://www.themoviedb.org/settings/api", self.readme)
        self.assertIn("QR kód", self.readme)
        self.assertRegex(self.readme, r"(?i)připojit k TMDB")

    def test_readme_documents_snapshot_consistency_and_complete_cold_saves(self):
        self.assertIn("`yawsp_app/watchlist_snapshot.py`", self.readme)
        for phrase in ("účtu TMDB", "typu média", "jazyka", "verze schématu"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)
        self.assertRegex(self.readme, r"(?i)warm.{0,300}nul|nul.{0,300}watchlist")
        self.assertRegex(self.readme, r"(?i)dočasně zastaral|eventual")
        self.assertRegex(self.readme, r"(?i)pozdějším otevření|dalším otevření")
        self.assertRegex(self.readme, r"(?i)žádný částečný snapshot|částečný snapshot.{0,40}ne")
        self.assertRegex(self.readme, r"(?i)všechny stránky|každá stránka")

    def test_readme_documents_bounded_jobs_and_truthful_completion(self):
        self.assertIn("`yawsp_app/download_jobs.py`", self.readme)
        self.assertRegex(self.readme, r"(?i)přijetí úlohy.{0,100}není.{0,100}dokončení")
        self.assertRegex(self.readme, r"(?i)vzdálené operace|vzdáleného úspěchu")
        self.assertRegex(self.readme, r"(?i)nejvýše čtyři|max_pending=4")
        self.assertRegex(self.readme, r"(?i)idempotentní close|close\(wait=False\)")
        self.assertRegex(self.readme, r"(?i)vlastní HTTP relaci|worker.{0,30}session")

    def test_readme_marks_db_evidence_historical_and_documents_safe_retirement(self):
        self.assertRegex(self.readme, r"(?i)odstraněn\w* Backup DB|Backup DB.{0,40}odstraněn")
        self.assertIn("`db`", self.readme)
        self.assertIn("`searchdb`", self.readme)
        self.assertRegex(self.readme, r"(?i)nemažou ani nemigrují")
        self.assertNotIn("`yawsp_app/backup_db.py` |", self.readme)
        for phrase in ("mtime_ns", "O(F)", "pouze vybraný soubor", "256 KiB"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)
        self.assertRegex(self.readme, r"(?i)sidecar|postranní")
        self.assertRegex(self.readme, r"(?i)jeden JSON dokument|jednoho JSON dokumentu")
        self.assertIn("Nebyla provedena živá ověření na ARM, NFS/SMB či vzdáleném VFS", self.readme)
        self.assertRegex(self.readme, r"(?i)medián.{0,80}(?:rozsah|minimum|maximum)|rozsah.{0,80}medián")
        self.assertIn("performance-coverage.json", self.readme)
        for benchmark in (
            "task-1_operation_benchmark.py",
            "task-2-benchmark.py",
            "task-3-benchmark.py",
            "benchmark_backup_db.py",
            "task-5-event-benchmark.py",
            "task-6-dedup-benchmark.py",
            "task-6-benchmark.py",
        ):
            with self.subTest(benchmark=benchmark):
                self.assertIn(benchmark, self.readme)

    def test_addon_news_is_concise_xml_for_the_existing_2_0_0_release(self):
        addon = ET.parse(str(ROOT / "addon.xml")).getroot()
        news = addon.findtext("./extension[@point='xbmc.addon.metadata']/news")
        self.assertIsNotNone(news)
        normalized = " ".join(news.split())
        self.assertTrue(normalized)
        self.assertLessEqual(len(normalized), 1000)
        self.assertIn("2.0.0", normalized)
        for phrase in ("jednotným routerem", "32", "watchlist", "QR", "front", "histori", "Kodi"):
            with self.subTest(phrase=phrase):
                self.assertRegex(normalized, "(?i)" + re.escape(phrase))
        for unreleased in (
            "Objevit",
            "discovery_native_wall",
            "Backup DB",
            "InfoTag",
            "nativní služb",
            "transport",
            "CI",
            "2.0.1",
            "2.1",
        ):
            with self.subTest(unreleased=unreleased):
                self.assertNotRegex(normalized, "(?i)" + re.escape(unreleased))

    def test_readme_documents_native_metadata_and_manual_watchlist_refresh(self):
        for phrase in (
            "InfoTagVideo",
            "getVideoInfoTag()",
            "setFirstAired",
            'setInfo("video", info)',
            "Kodi 19",
            "celý vstup",
            "refresh=1",
            "Obnovit",
            "Místní mezipaměť",
            "poslední platný snapshot",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)
        self.assertRegex(self.readme, r"(?i)chyb\w*.{0,180}snapshot|snapshot.{0,180}chyb\w*")
        self.assertRegex(self.readme, r"(?i)ručn\w*.{0,250}duplicitn\w*.{0,100}(?:požadav|refresh)|duplicitn\w*.{0,150}(?:watchlist|pozadí)")
        self.assertIn("Žádné výsledky", self.readme)
        self.assertIn("raw_count", self.readme)
        self.assertRegex(self.readme, r"(?i)stránkov\w*.{0,160}raw_count|raw_count.{0,160}stránkov\w*")

    def test_readme_documents_complete_discovery_navigation_and_presentation(self):
        section_match = re.search(
            r"^## 🧭 Objevit a pohledy\s*$\n(.*?)(?=^## )",
            self.readme,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(section_match, "missing Objevit documentation section")
        section = section_match.group(1).strip()
        paragraphs = [
            re.sub(r"\s+", " ", paragraph.strip())
            for paragraph in re.split(r"\n\s*\n", section)
        ]
        self.assertNotIn("discovery_native_wall=true", section)
        self.assertNotRegex(
            section,
            r"(?i)(?:nastavte|povolte|zapněte).{0,100}discovery_native_wall|discovery_native_wall.{0,100}(?:přepne|povolí|zapne)",
        )
        self.assertNotRegex(section, r"(?i)výchozí vlastní prezentace|vlastní okno.{0,50}(?:nabízí|se otevírá)")
        self.assertGreaterEqual(len(paragraphs), 4)
        selector, public_rows, native_view, retirement = paragraphs[:4]

        self.assertIn(
            "**Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist**",
            selector,
        )
        for key, path in (
            ("popular_movies", "/movie/popular"),
            ("popular_shows", "/tv/popular"),
            ("top_rated_movies", "/movie/top_rated"),
            ("top_rated_shows", "/tv/top_rated"),
        ):
            with self.subTest(key=key, path=path):
                self.assertRegex(
                    selector,
                    r"`" + re.escape(key) + r"`[^.]{0,60}`" + re.escape(path) + r"`",
                )
        self.assertRegex(selector, r"(?i)pouze.{0,50}chybějíc\w*.{0,30}`source`")
        self.assertIn("neřetězcový", selector)
        self.assertRegex(
            selector,
            r"(?i)prázdn\w*.{0,80}neznám\w*.{0,100}místn\w*.{0,30}chyb",
        )
        self.assertRegex(
            selector,
            r"(?i)hlavní menu i výběr kategorií jsou statické.{0,160}bez.{0,60}HTTP.{0,100}pozadí",
        )
        self.assertRegex(
            selector,
            r"(?i)výběr kategorií.{0,120}ne.{0,50}zpětn\w* volání pohledu",
        )
        self.assertRegex(
            selector,
            r"(?i)nejvýše jednu stránku.{0,120}nejvýše jeden sdílený požadavek na žánry",
        )
        self.assertRegex(
            selector,
            r"(?i)nepokračuje\s+automaticky\s+dalšími\s+stránkami",
        )

        self.assertIn("https://api.themoviedb.org/3", public_rows)
        public_contract_ok, public_contract_reasons = public_native_result_contract(
            public_rows
        )
        self.assertTrue(public_contract_ok, public_contract_reasons)
        self.assertRegex(public_rows, r"(?i)API klíč.{0,80}(?:povinný|potřebný)")
        self.assertRegex(
            public_rows,
            r"(?i)(?:401|neautorizovan\w*).{0,140}(?:zachov|nezměn|neinvalid).{0,100}identit",
        )
        self.assertRegex(public_rows, r"(?i)apply_video_metadata.{0,200}InfoTag")
        self.assertRegex(
            public_rows,
            r"(?i)Řádky\s+s\s+artwork,\s+lokalizovaným\s+názvem,\s+rokem,\s+žánry,\s+hodnocením\s+a\s+popisem",
        )
        self.assertIn("30440", public_rows)
        self.assertRegex(
            public_rows,
            re.escape("plugin://plugin.video.yawspreloaded/?action=search&what=")
            + r"<[^>\r\n]+>&sort=largest",
        )
        self.assertRegex(
            public_rows,
            r"(?i)původního názvu.{0,200}Container\.Update.{0,100}(?:ne|nikoli).{0,40}skript",
        )

        safe_exclusions = (
            "bez autoplay, watched změn, account/watchlist požadavků, "
            "dávkového dotažování metadat či background prefetch"
        )

        def mutate(current, replacement):
            mutated = public_rows.replace(current, replacement, 1)
            self.assertNotEqual(mutated, public_rows, "negative fixture did not mutate")
            return mutated

        negative_fixtures = (
            (
                "always_transmits_session",
                mutate(
                    "ale nikdy neposílají `session_id`",
                    "a vždy posílají `session_id`",
                ),
                ("positive_session_transmission",),
            ),
            (
                "autoplay_watched_and_prefetch",
                mutate(
                    safe_exclusions,
                    "automatické přehrávání, watched změny a background prefetch",
                ),
                (
                    "positive_autoplay",
                    "positive_watched_mutations",
                    "positive_background_metadata_prefetch",
                ),
            ),
            (
                "account_queries",
                public_rows + " The client performs account queries.",
                ("positive_account_queries",),
            ),
            (
                "watchlist_snapshot_operations",
                public_rows + " The client performs watchlist snapshot operations.",
                ("positive_watchlist_snapshot_operations",),
            ),
            (
                "healthy_text_with_combined_contradictions",
                public_rows
                + " The client always sends `session_id`, starts automatic playback "
                "and watched changes, performs account queries and watchlist snapshot "
                "operations, and runs background prefetch.",
                (
                    "positive_session_transmission",
                    "positive_autoplay",
                    "positive_watched_mutations",
                    "positive_account_queries",
                    "positive_watchlist_snapshot_operations",
                    "positive_background_metadata_prefetch",
                ),
            ),
            (
                "double_negatives",
                public_rows
                + " This is not no automatic playback and not no watched changes.",
                ("positive_autoplay", "positive_watched_mutations"),
            ),
            (
                "background_prefetching_gerund",
                public_rows + " The client runs background prefetching.",
                ("positive_background_metadata_prefetch",),
            ),
            (
                "session_passed_with_every_request",
                public_rows + " The client passes session_id with every request.",
                ("positive_session_transmission",),
            ),
            (
                "watchlist_snapshot_updates",
                public_rows + " The client performs watchlist snapshot updates.",
                ("positive_watchlist_snapshot_operations",),
            ),
            (
                "enables_auto_play",
                public_rows + " The client enables auto-play.",
                ("positive_autoplay",),
            ),
            (
                "watched_updates",
                public_rows + " The client performs watched updates.",
                ("positive_watched_mutations",),
            ),
            (
                "account_requests",
                public_rows + " The client runs account requests.",
                ("positive_account_queries",),
            ),
            (
                "session_object_before_passive_verb",
                public_rows + " session_id is always sent.",
                ("positive_session_transmission",),
            ),
            (
                "session_passive_past",
                public_rows + " session_id was passed with each request.",
                ("positive_session_transmission",),
            ),
            (
                "automatically_plays",
                public_rows + " The client automatically plays titles.",
                ("positive_autoplay",),
            ),
            (
                "marks_movie_watched",
                public_rows + " The client marks the movie watched.",
                ("positive_watched_mutations",),
            ),
            (
                "background_metadata_prefetch_requests",
                public_rows + " The client runs background metadata prefetch requests.",
                ("positive_background_metadata_prefetch",),
            ),
            (
                "trailer_show_preloading",
                public_rows + " The client performs trailer/show preloading.",
                ("positive_background_metadata_prefetch",),
            ),
            (
                "mutates_watchlist_snapshot",
                public_rows + " The client mutates the watchlist snapshot.",
                ("positive_watchlist_snapshot_operations",),
            ),
            (
                "account_writes",
                public_rows + " The client performs account writes.",
                ("positive_account_queries",),
            ),
            (
                "czech_session_passive",
                public_rows + " session_id je vždy odesláno.",
                ("positive_session_transmission",),
            ),
            (
                "session_transmission_noun",
                public_rows + " The client enables automatic transmission of session_id.",
                ("positive_session_transmission",),
            ),
            (
                "starts_auto_play_separate_words",
                public_rows + " The client starts auto play.",
                ("positive_autoplay",),
            ),
            (
                "czech_automatic_playback",
                public_rows + " Klient automaticky spustí přehrávání.",
                ("positive_autoplay",),
            ),
            (
                "czech_marks_movie_watched",
                public_rows + " Klient označí film jako zhlédnutý.",
                ("positive_watched_mutations",),
            ),
            (
                "czech_account_queries",
                public_rows + " Klient provádí dotazy na účet.",
                ("positive_account_queries",),
            ),
            (
                "account_passive_query",
                public_rows + " The public account is queried.",
                ("positive_account_queries",),
            ),
            (
                "watchlist_snapshot_writes",
                public_rows + " The client writes the watchlist snapshot.",
                ("positive_watchlist_snapshot_operations",),
            ),
            (
                "czech_trailer_preloading",
                public_rows + " Klient spouští přednačítání trailerů.",
                ("positive_background_metadata_prefetch",),
            ),
            (
                "slovak_trailer_preloading",
                public_rows + " Klient spúšťa prednačítanie trailerov.",
                ("positive_background_metadata_prefetch",),
            ),
            (
                "metadata_preloading",
                public_rows + " The client is preloading metadata.",
                ("positive_background_metadata_prefetch",),
            ),
            (
                "playing_automatically_object_order",
                public_rows + " The client starts playing automatically.",
                ("positive_autoplay",),
            ),
            (
                "czech_playback_automatic_object_order",
                public_rows + " Přehrávání se spustí automaticky.",
                ("positive_autoplay",),
            ),
            (
                "negative_autoplay_but_positive_session",
                public_rows + " No autoplay, but session_id is sent.",
                ("positive_session_transmission",),
            ),
            (
                "negative_account_but_positive_autoplay",
                public_rows + " No account queries, but the client enables auto-play.",
                ("positive_autoplay",),
            ),
            (
                "negative_autoplay_but_positive_watched",
                public_rows + " No autoplay, but the client performs watched updates.",
                ("positive_watched_mutations",),
            ),
            (
                "negative_autoplay_but_positive_session_without_comma",
                public_rows + " No autoplay but session_id is sent.",
                ("positive_session_transmission",),
            ),
            (
                "negative_account_ale_positive_autoplay",
                public_rows + " No account queries ale klient povolí auto-play.",
                ("positive_autoplay",),
            ),
            (
                "negative_autoplay_avsak_positive_watched",
                public_rows + " Bez autoplay avšak klient vykoná watched updates.",
                ("positive_watched_mutations",),
            ),
            (
                "negative_autoplay_vsak_positive_session",
                public_rows + " Bez autoplay, však klient odešle session_id.",
                ("positive_session_transmission",),
            ),
            (
                "native_player_invocation",
                public_rows + " The native player is invoked for the result.",
                ("positive_autoplay",),
            ),
            (
                "czech_active_session_send",
                public_rows + " Klient odešle session_id.",
                ("positive_session_transmission",),
            ),
            (
                "slovak_marks_movie_watched",
                public_rows + " Klient označí film ako zhliadnutý.",
                ("positive_watched_mutations",),
            ),
        )
        for fixture_name, fixture, expected_reasons in negative_fixtures:
            with self.subTest(negative_fixture=fixture_name):
                ok, reasons = public_native_result_contract(fixture)
                self.assertFalse(
                    ok,
                    "inverted statement accepted; expected {0}, diagnostics {1!r}".format(
                        expected_reasons, reasons
                    ),
                )
                for reason in expected_reasons:
                    self.assertIn(reason, reasons)
        watchlist_reference_ok, watchlist_reference_reasons = (
            public_native_result_contract(
                public_rows + " Hlavní menu nadále obsahuje položku TMDB Watchlist."
            )
        )
        self.assertTrue(watchlist_reference_ok, watchlist_reference_reasons)
        approved_negative_ok, approved_negative_reasons = public_native_result_contract(
            public_rows
            + " Never pass session_id; no autoplay; do not mark the movie watched; "
            "skip account queries; skip watchlist snapshot requests; "
            "no background prefetching; no trailer/show preloading; "
            "session_id is never sent; session_id není odesláno; no automatic playback."
        )
        self.assertTrue(approved_negative_ok, approved_negative_reasons)
        coordinated_negative_ok, coordinated_negative_reasons = (
            public_native_result_contract(
                public_rows
                + " No autoplay, but session_id is never sent; "
                "no account queries but the client does not enable auto-play; "
                "no autoplay, but the client does not perform watched updates."
            )
        )
        self.assertTrue(coordinated_negative_ok, coordinated_negative_reasons)
        on_demand_ok, on_demand_reasons = public_native_result_contract(
            public_rows
            + " Ordinary Webshare search fetches results on demand; "
            "the TMDB Watchlist menu item remains available."
        )
        self.assertTrue(on_demand_ok, on_demand_reasons)

        self.assertIn("InfoWall", native_view)
        self.assertIn("Estuary", native_view)
        self.assertRegex(
            native_view,
            r"(?i)úspěšn\w*.{0,80}vybran\w* kategorie.{0,180}tmdb_view_defaulted.{0,80}true",
        )
        self.assertIn("Container.SetViewMode(54)", native_view)
        self.assertRegex(
            native_view,
            r"(?i)(?:statický )?výběr kategorií.{0,100}ne.{0,30}pohled",
        )
        self.assertRegex(native_view, r"(?i)(?:každé|opakované) otevření.{0,100}ne.{0,50}54")
        self.assertRegex(
            native_view,
            r"(?i)watchlist.{0,160}(?:pohled|styl).{0,80}neměn|(?:pohled|styl).{0,80}watchlist\w*.{0,80}neměn",
        )
        self.assertRegex(
            native_view,
            r"(?i)(?:budoucím|případném) schváleném nasazení.{0,100}ne\w*.{0,50}reinstal",
        )

        self.assertRegex(retirement, r"(?i)trvale vyřazen|trvale odstraněn")
        self.assertIn("30444", retirement)
        self.assertRegex(
            retirement,
            r"(?i)discovery_native_wall.{0,180}(?:historick\w*|uložen\w*).{0,120}neaktivn",
        )
        self.assertRegex(retirement, r"(?i)žádný samostatný skript|žádné samostatné skripty")
        self.assertNotIn("| `yawsp_app/discovery_window.py` |", self.readme)

    def test_readme_documents_ci_verifier_without_persistent_archive(self):
        for phrase in (
            ".github/workflows/tests.yml",
            "contents: read",
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c",
            "3.8/3.12",
            "python -B -m unittest discover -s tests -v",
            "python -B scripts/verify_release.py",
            "HEAD",
            "runtime_files=",
            "sha256=",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)
        self.assertRegex(self.readme, r"(?i)dynami\w*.{0,100}(?:počet|soubor)|(?:počet|soubor).{0,100}dynami\w*")
        self.assertRegex(self.readme, r"(?i)dočasn\w*.{0,160}(?:archiv|ZIP)|(?:archiv|ZIP).{0,160}dočasn\w*")
        self.assertRegex(self.readme, r"(?i)neukládá.{0,80}(?:trvalý|release)|žádný.{0,60}trvalý.{0,40}(?:archiv|artefakt)")
        self.assertRegex(self.readme, r"(?i)lokáln\w* offline.{0,180}(?:hosted|GitHub Actions)|(?:hosted|GitHub Actions).{0,180}lokáln\w* offline")

    def test_readme_documents_durable_backups_without_exposing_archive_data(self):
        self.assertIn("Kodi/userdata/backup/plugin.video.yawspreloaded", self.readme)
        for filename in (
            "yawsp-main-profile-pre35b4e4a.zip",
            "yawsp-main-profile-pre2.0.0-manifest.zip",
        ):
            with self.subTest(filename=filename):
                self.assertIn(filename, self.readme)
        self.assertIn("SHA-256", self.readme)
        self.assertRegex(self.readme, r"(?i)Temp.{0,180}originál|originál.{0,180}Temp")
        self.assertRegex(self.readme, r"(?i)ne(?:vypis|zobraz)\w*.{0,80}(?:obsah|taj|přihlaš)|bez.{0,50}výpis\w*.{0,50}(?:obsah|archiv)")

    def test_readme_limits_live_environment_claims_and_marks_task4_pending(self):
        self.assertIn("Kodi 21.2 (Omega), Windows Store x64", self.readme)
        for phrase in ("Python 3.8", "AST", "Python 3.12", "Kodi 19", "ARM", "SMB/NFS", "Task 4"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)
        self.assertNotIn("Task 9", self.readme)
        self.assertRegex(self.readme, r"(?i)Python 3\.8.{0,100}(?:AST|statick).{0,200}(?:ne|není).{0,120}Kodi 19|Kodi 19.{0,150}Python 3\.8")
        self.assertRegex(self.readme, r"(?i)nebyl\w*.{0,100}skutečn\w* přehrávání|skutečn\w* přehrávání.{0,100}nebyl\w*")
        self.assertRegex(self.readme, r"(?i)této větve.{0,140}Objevit|Objevit.{0,140}této větve")
        self.assertRegex(self.readme, r"(?i)Task 4.{0,260}před sloučením|před sloučením.{0,260}Task 4")
        self.assertRegex(self.readme, r"(?i)nativn\w*.{0,30}služb\w*.{0,100}nebyl\w* vydán|nebyl\w* vydán.{0,100}nativn\w*.{0,30}služb\w*")
        lifecycle_match = re.search(
            r"^### Naměřené chování a jeho hranice\s*$\n(.*?)(?=^## )",
            self.readme,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(lifecycle_match)
        lifecycle = re.sub(r"\s+", " ", lifecycle_match.group(1))
        self.assertRegex(
            lifecycle,
            r"(?i)současn\w*.{0,100}Objevit.{0,180}nebyl\w*.{0,80}(?:živě|interaktiv)",
        )
        self.assertRegex(lifecycle, r"(?i)Task 4.{0,180}izolovan\w* profil")
        self.assertRegex(
            lifecycle,
            r"(?i)HANG.{0,220}(?:aktivn\w*.{0,80})?(?:pozadí|background).{0,120}nezávisl|(?:aktivn\w*.{0,80})?(?:pozadí|background).{0,180}HANG.{0,120}nezávisl",
        )
        self.assertRegex(lifecycle, r"(?i)HANG.{0,220}nevyřešen|nevyřešen\w*.{0,180}HANG")
        self.assertRegex(lifecycle, r"(?i)hlavního profilu.{0,160}(?:sloučen|vydán|nasazen)|(?:sloučen|vydán|nasazen).{0,160}hlavního profilu")

    def test_performance_coverage_tracks_native_selector_without_custom_ui(self):
        coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
        mappings = {entry["id"]: entry for entry in coverage["requirements"]}
        selector = (
            "tests.test_discovery_integration.DiscoveryIntegrationTests."
            "test_missing_source_selects_four_categories_without_http"
        )
        self.assertIn(selector, mappings["PERF-05"]["mutation_sensitive_tests"])
        active_selectors = [
            test_selector
            for entry in coverage["requirements"]
            if entry.get("status", "active") == "active"
            for test_selector in entry["mutation_sensitive_tests"]
        ]
        self.assertEqual(len(active_selectors), 31)
        self.assertEqual(len(set(active_selectors)), 31)
        for entry in coverage["requirements"]:
            if entry.get("status", "active") != "active":
                continue
            active_contract = json.dumps(
                {
                    "production_symbols": entry["production_symbols"],
                    "mutation_sensitive_tests": entry["mutation_sensitive_tests"],
                }
            )
            with self.subTest(requirement=entry["id"]):
                self.assertNotRegex(
                    active_contract,
                    r"(?i)discovery_window|discovery_script|create_discovery_window|discovery_native_wall",
                )
        execution = coverage["current_selector_execution"]
        self.assertEqual(execution["mapped_selector_entries"], 31)
        self.assertEqual(execution["unique_selectors"], 31)
        self.assertEqual(execution["passed"], 31)
        self.assertEqual(
            execution["runs"][-1],
            {"log": "task-native-first-selectors.log", "tests": 31, "status": "OK"},
        )

    def test_performance_coverage_resolves_real_symbols_and_test_methods(self):
        coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(coverage["schema_version"], 1)
        self.assertEqual(coverage["version"], "2.0.0")
        self.assertFalse(coverage["released"])
        self.assertEqual(
            coverage["standard_test_command"],
            "python -m unittest discover -s tests -v",
        )
        self.assertTrue(coverage["measurement_scope"]["offline_only"])
        self.assertTrue(coverage["measurement_scope"]["live_kodi_verified"])
        self.assertTrue(coverage["measurement_scope"]["live_network_verified"])
        mappings = {entry["id"]: entry for entry in coverage["requirements"]}
        self.assertEqual(set(mappings), PERFORMANCE_IDS)
        self.assertEqual(len(coverage["requirements"]), len(PERFORMANCE_IDS))
        mapped_selectors = [
            selector
            for entry in coverage["requirements"]
            for selector in entry.get("mutation_sensitive_tests", [])
        ]
        execution = coverage["current_selector_execution"]
        self.assertEqual(len(mapped_selectors), execution["mapped_selector_entries"])
        self.assertEqual(len(set(mapped_selectors)), execution["unique_selectors"])
        self.assertEqual(execution["status"], "OK")
        self.assertEqual(execution["passed"], execution["mapped_selector_entries"])
        self.assertTrue(execution["runs"])
        self.assertEqual(
            {requirement_id for requirement_id, entry in mappings.items() if entry.get("status") == "removed"},
            {"PERF-03", "PERF-08"},
        )
        for requirement_id, entry in mappings.items():
            with self.subTest(requirement=requirement_id):
                self.assertIn(entry.get("status", "active"), {"active", "removed"})
                if entry.get("status") == "removed":
                    self.assertTrue(entry["removal_scope"])
                    self.assertTrue(entry["historical_production_symbols"])
                    self.assertTrue(entry["historical_mutation_sensitive_tests"])
                else:
                    self.assertTrue(entry["production_symbols"])
                    self.assertTrue(entry["mutation_sensitive_tests"])
                self.assertTrue(entry["benchmarks"])
                self.assertTrue(entry["measured_result"]["summary"])
                self.assertIn(
                    entry["measured_result"]["outcome"],
                    {"improved", "unchanged", "tradeoff"},
                )
                self.assertTrue(entry["limitations"])
                for production in entry.get("production_symbols", []):
                    definition = module_definition(
                        production["module"], production["symbol"]
                    )
                    self.assertIsNotNone(
                        definition,
                        "unresolved {0}.{1}".format(
                            production["module"], production["symbol"]
                        ),
                    )
                for selector in entry.get("mutation_sensitive_tests", []):
                    match = re.fullmatch(
                        r"(tests\.[A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\.(test_[A-Za-z0-9_]+)",
                        selector,
                    )
                    self.assertIsNotNone(match, selector)
                    module_name, class_name, method_name = match.groups()
                    definition = module_definition(
                        module_name, class_name + "." + method_name
                    )
                    self.assertIsInstance(definition, ast.FunctionDef, selector)
                    self.assertTrue(
                        any(
                            isinstance(node, (ast.Assert, ast.Call))
                            and (
                                isinstance(node, ast.Assert)
                                or (
                                    isinstance(node.func, ast.Attribute)
                                    and node.func.attr.startswith("assert")
                                )
                            )
                            for node in ast.walk(definition)
                        ),
                        "selector has no assertion call: " + selector,
                    )
                for benchmark in entry["benchmarks"]:
                    self.assertRegex(benchmark["script"], r"^[A-Za-z0-9_.-]+\.py$")
                    self.assertIn(benchmark["script"], benchmark["command"])
                    self.assertTrue(benchmark["evidence"])
                if requirement_id == "PERF-07":
                    self.assertRegex(
                        " ".join(entry["limitations"]),
                        r"(?i)(?:20|200|small|mal)",
                    )
                if requirement_id == "PERF-08":
                    self.assertRegex(
                        " ".join(entry["limitations"]),
                        r"(?i)JSON.{0,120}(?:member|dokument)|(?:member|dokument).{0,120}JSON",
                    )


if __name__ == "__main__":
    unittest.main()
