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
            "yawsp_app/discovery_window.py",
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
        self.assertIn(
            "**Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist**",
            self.readme,
        )
        for phrase in (
            "popular_movies",
            "popular_shows",
            "top_rated_movies",
            "top_rated_shows",
            "nejvýše jednu stránku",
            "nejvýše jeden sdílený požadavek na žánry",
            "API klíč",
            "sort=largest",
            "WindowXML",
            "Netflix",
            "Back",
            "discovery_native_wall=true",
            "příštím otevření",
            "InfoWall",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)
        self.assertRegex(self.readme, r"(?i)nepotřebuje.{0,140}(?:osobní.{0,30})?(?:účet|session)|bez.{0,80}(?:osobního )?(?:účtu|session)")
        self.assertRegex(self.readme, r"(?i)dálkov\w*.{0,120}kláves|kláves.{0,120}dálkov\w*")
        self.assertRegex(self.readme, r"(?i)načítání.{0,120}prázdn\w*.{0,120}chyb\w*|prázdn\w*.{0,120}chyb\w*.{0,120}Obnovit")
        self.assertRegex(self.readme, r"(?i)bez autoplay|automatick\w* přehrávání")
        self.assertRegex(self.readme, r"(?i)watched.{0,100}(?:prefetch|pozadí)|prefetch.{0,100}watched")
        self.assertRegex(self.readme, r"(?i)watchlist.{0,160}(?:pohled|styl).{0,80}neměn|(?:pohled|styl).{0,80}watchlist\w*.{0,80}neměn")

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

    def test_readme_limits_live_environment_claims_and_marks_task9_pending(self):
        self.assertIn("Kodi 21.2 (Omega), Windows Store x64", self.readme)
        for phrase in ("Python 3.8", "AST", "Python 3.12", "Kodi 19", "ARM", "SMB/NFS", "Task 9"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)
        self.assertRegex(self.readme, r"(?i)Python 3\.8.{0,100}(?:AST|statick).{0,200}(?:ne|není).{0,120}Kodi 19|Kodi 19.{0,150}Python 3\.8")
        self.assertRegex(self.readme, r"(?i)nebyl\w*.{0,100}skutečn\w* přehrávání|skutečn\w* přehrávání.{0,100}nebyl\w*")
        self.assertRegex(self.readme, r"(?i)této větve.{0,140}Objevit|Objevit.{0,140}této větve")
        self.assertRegex(self.readme, r"(?i)Task 9.{0,220}před sloučením|před sloučením.{0,220}Task 9")
        self.assertRegex(self.readme, r"(?i)nativn\w*.{0,30}služb\w*.{0,100}nebyl\w* vydán|nebyl\w* vydán.{0,100}nativn\w*.{0,30}služb\w*")

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
