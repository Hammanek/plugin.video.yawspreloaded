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
            "yawsp_app/backup_db.py",
            "yawsp_app/json_cache.py",
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
        self.assertNotRegex(self.readme, r"(?i)(?:testováno|ověřeno) (?:v|na) (?:reálném |živém )?Kodi")
        self.assertNotRegex(self.readme, r"(?i)živé (?:API )?testy (?:byly )?(?:úspěšně )?(?:spuštěny|prošly)")

    def test_readme_preserves_version_installation_and_tmdb_qr_setup(self):
        addon_version = ET.parse(str(ROOT / "addon.xml")).getroot().get("version")
        self.assertEqual(addon_version, "1.3.15")
        self.assertIn("verze-1.3.15-blue.svg", self.readme)
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
        self.assertRegex(self.readme, r"(?i)Container\.Refresh.{0,180}(?:až po|teprve po)")
        self.assertRegex(self.readme, r"(?i)při chybě.{0,100}(?:neprobehne|žádný)|chybě.{0,100}žádný.{0,30}Refresh")
        self.assertRegex(self.readme, r"(?i)nejvýše čtyři|max_pending=4")
        self.assertRegex(self.readme, r"(?i)idempotentní close|close\(wait=False\)")
        self.assertRegex(self.readme, r"(?i)vlastní HTTP relaci|worker.{0,30}session")

    def test_readme_documents_db_streaming_and_offline_measurement_limits(self):
        for phrase in ("mtime_ns", "O(F)", "pouze vybraný soubor", "256 KiB"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.readme)
        self.assertRegex(self.readme, r"(?i)sidecar|postranní")
        self.assertRegex(self.readme, r"(?i)jeden JSON dokument|jednoho JSON dokumentu")
        self.assertRegex(self.readme, r"(?i)bez (?:živého |reálného )?Kodi|nebyl[ay]?.{0,80}Kodi")
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

    def test_performance_coverage_resolves_real_symbols_and_test_methods(self):
        coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(coverage["schema_version"], 1)
        self.assertEqual(coverage["version"], "1.3.15")
        self.assertFalse(coverage["released"])
        self.assertEqual(
            coverage["standard_test_command"],
            "python -m unittest discover -s tests -v",
        )
        self.assertTrue(coverage["measurement_scope"]["offline_only"])
        self.assertFalse(coverage["measurement_scope"]["live_kodi_verified"])
        mappings = {entry["id"]: entry for entry in coverage["requirements"]}
        self.assertEqual(set(mappings), PERFORMANCE_IDS)
        self.assertEqual(len(coverage["requirements"]), len(PERFORMANCE_IDS))
        mapped_selectors = [
            selector
            for entry in coverage["requirements"]
            for selector in entry["mutation_sensitive_tests"]
        ]
        execution = coverage["selector_execution"]
        self.assertEqual(len(mapped_selectors), execution["mapped_selector_entries"])
        self.assertEqual(len(set(mapped_selectors)), execution["unique_selectors"])
        for requirement_id, entry in mappings.items():
            with self.subTest(requirement=requirement_id):
                self.assertTrue(entry["production_symbols"])
                self.assertTrue(entry["mutation_sensitive_tests"])
                self.assertTrue(entry["benchmarks"])
                self.assertTrue(entry["measured_result"]["summary"])
                self.assertIn(
                    entry["measured_result"]["outcome"],
                    {"improved", "unchanged", "tradeoff"},
                )
                self.assertTrue(entry["limitations"])
                for production in entry["production_symbols"]:
                    definition = module_definition(
                        production["module"], production["symbol"]
                    )
                    self.assertIsNotNone(
                        definition,
                        "unresolved {0}.{1}".format(
                            production["module"], production["symbol"]
                        ),
                    )
                for selector in entry["mutation_sensitive_tests"]:
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
