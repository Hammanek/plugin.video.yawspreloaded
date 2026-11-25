from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"


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


if __name__ == "__main__":
    unittest.main()
