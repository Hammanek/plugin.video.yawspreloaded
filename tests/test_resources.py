import ast
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

from yawsp_app.runtime import KodiRuntime


ROOT = Path(__file__).resolve().parents[1]
CATALOGS = {
    "cs_CZ": ROOT / "resources/language/resource.language.cs_cz/strings.po",
    "sk_SK": ROOT / "resources/language/resource.language.sk_sk/strings.po",
    "en_GB": ROOT / "resources/language/resource.language.en_gb/strings.po",
}

# New IDs are allocated sequentially, with wording derived from the existing UI.
# English intentionally uses the msgid fallback and therefore has empty msgstr values.
TMDB_STRINGS = (
    (30400, "To connect to TMDB, enter an API key in the settings.",
     "Pro připojení k TMDB je třeba vyplnit API Key v nastavení.",
     "Na pripojenie k TMDB je potrebné vyplniť API Key v nastavení."),
    (30401, "Invalid TMDB API key or expired session; check the settings.",
     "Neplatný TMDB API klíč nebo vypršené přihlášení – zkontroluj nastavení.",
     "Neplatný TMDB API kľúč alebo vypršané prihlásenie – skontroluj nastavenie."),
    (30402, "Error loading", "Chyba při načítání", "Chyba pri načítavaní"),
    (30403, "Scan the QR code with your phone and approve access",
     "Naskenujte QR kód telefonem a schvalte přístup",
     "Naskenujte QR kód telefónom a schváľte prístup"),
    (30404, "Approve access in your browser: {0}",
     "Schvalte přístup v prohlížeči: {0}",
     "Schváľte prístup v prehliadači: {0}"),
    (30405, "Successfully connected to TMDB!", "Úspěšně připojeno k TMDB!",
     "Úspešne pripojené k TMDB!"),
    (30406, "Authentication timed out.", "Čas na ověření vypršel.",
     "Čas na overenie vypršal."),
    (30407, "Could not connect to TMDB: {0}",
     "Připojení k TMDB se nezdařilo: {0}",
     "Pripojenie k TMDB sa nepodarilo: {0}"),
    (30408, "Unknown title", "Neznámý název", "Neznámy názov"),
    (30409, "Season {0} ({1} episodes)", "Sezóna {0} ({1} epizod)",
     "Séria {0} ({1} epizód)"),
    (30410, "Season {0}", "Sezóna {0}", "Séria {0}"),
    (30411, "Connect to TMDB before using this action.",
     "Pro tuto akci se nejprve připojte k TMDB.",
     "Pre túto akciu sa najprv pripojte k TMDB."),
    (30412, "Error removing from watchlist", "Chyba při odstraňování z watchlistu",
     "Chyba pri odstraňovaní z watchlistu"),
    (30413, "Watched and removed from watchlist",
     "Zhlédnuto a odstraněno z watchlistu", "Zhliadnuté a odstránené z watchlistu"),
    (30414, "Search original title", "Vyhledat původní název",
     "Vyhľadať pôvodný názov"),
    (30415, "Play trailer", "Přehrát trailer", "Prehrať trailer"),
    (30416, "Mark as watched", "Označit jako zhlédnuté",
     "Označiť ako zhliadnuté"),
    (30417, "Watchlist is empty", "Watchlist je prázdný", "Watchlist je prázdny"),
    (30418, "TMDB Watchlist", "TMDB Watchlist", "TMDB Watchlist"),
    (30419, "Connect to TMDB in the add-on settings...",
     "Připojit k TMDB v nastavení doplňku...",
     "Pripojiť k TMDB v nastavení doplnku..."),
    (30420, "Connect to TMDB in the add-on settings to view your watchlist.",
     "Pro zobrazení watchlistu se připojte k TMDB v nastavení doplňku.",
     "Pre zobrazenie watchlistu sa pripojte k TMDB v nastavení doplnku."),
    (30421, "Movies", "Filmy", "Filmy"),
    (30422, "TV shows", "Seriály", "Seriály"),
    (30423, "No trailer was found.", "Trailer nebyl nalezen.",
     "Trailer nebol nájdený."),
    (30424, "The trailer could not be loaded.", "Trailer se nepodařilo načíst.",
     "Trailer sa nepodarilo načítať."),
    (30425, "Install the YouTube add-on to play trailers.",
     "Pro přehrání traileru nainstalujte doplněk YouTube.",
     "Na prehratie traileru nainštalujte doplnok YouTube."),
    (30426, "TMDB", "TMDB", "TMDB"),
    (30427, "TMDB API Key", "TMDB API Key", "TMDB API Key"),
    (30428, "Connect to TMDB...", "Připojit k TMDB...", "Pripojiť k TMDB..."),
    (30429, "Watchlist sort order", "Řazení watchlistu", "Zoradenie watchlistu"),
    (30430, "Hide unreleased movies", "Skrýt zatím nevydané filmy",
     "Skryť zatiaľ nevydané filmy"),
    (30431, "TMDB Access Token", "TMDB Access Token", "TMDB Access Token"),
    (30432, "TMDB Account ID", "TMDB Account ID", "TMDB Account ID"),
)
TMDB_IDS = {entry[0] for entry in TMDB_STRINGS}
JOB_STRINGS = (
    (30433, "Download queued", "Stahování zařazeno do fronty",
     "Sťahovanie zaradené do fronty"),
    (30437, "Download could not be queued",
     "Stahování se nepodařilo zařadit do fronty",
     "Sťahovanie sa nepodarilo zaradiť do fronty"),
)
JOB_IDS = {entry[0] for entry in JOB_STRINGS}
UX_STRINGS = (
    (30438, "Refresh", "Obnovit", "Obnoviť"),
    (30439, "Local cache", "Místní mezipaměť", "Miestna vyrovnávacia pamäť"),
    (30440, "No matching files found", "Žádné výsledky", "Žiadne výsledky"),
)
UX_IDS = {entry[0] for entry in UX_STRINGS}


def load_catalog(language):
    path = CATALOGS[language]
    text = path.read_bytes().decode("utf-8", errors="strict")
    entries = {}
    current_id = None
    for line in text.splitlines():
        match = re.fullmatch(r'msgctxt "#(\d+)"', line)
        if match:
            current_id = int(match.group(1))
            if current_id in entries:
                raise AssertionError(
                    "duplicate msgctxt #{0} in {1}".format(current_id, path)
                )
            entries[current_id] = {"msgid": None, "msgstr": None}
        elif current_id is not None and line.startswith("msgid "):
            entries[current_id]["msgid"] = ast.literal_eval(line[6:])
        elif current_id is not None and line.startswith("msgstr "):
            entries[current_id]["msgstr"] = ast.literal_eval(line[7:])
    return text, entries


def production_trees():
    paths = [ROOT / "main.py", ROOT / "yawsp.py"]
    paths.extend(sorted((ROOT / "yawsp_app").glob("*.py")))
    for path in paths:
        yield path, ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def numeric_argument(node, static_names):
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return {node.value}
    if isinstance(node, ast.Name):
        return static_names.get(node.id, set())
    return set()


def localization_references(tree):
    """Resolve direct Kodi calls and calls through KodiRuntime.localized."""
    static_names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            if isinstance(target, ast.Name):
                values = numeric_argument(value, static_names)
                if values:
                    static_names.setdefault(target.id, set()).update(values)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Tuple):
            if isinstance(node.iter, (ast.Tuple, ast.List)):
                for index, target in enumerate(node.target.elts):
                    if not isinstance(target, ast.Name):
                        continue
                    for row in node.iter.elts:
                        if isinstance(row, (ast.Tuple, ast.List)) and index < len(row.elts):
                            values = numeric_argument(row.elts[index], static_names)
                            static_names.setdefault(target.id, set()).update(values)

    references = set()
    unresolved = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("getLocalizedString", "localized") or not node.args:
            continue
        values = numeric_argument(node.args[0], static_names)
        if values:
            references.update(values)
        elif not (
            node.func.attr == "getLocalizedString"
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "string_id"
        ):
            unresolved.append((node.lineno, node.func.attr))
    return references, unresolved


class CatalogAddon:
    """Expose real PO entries through Kodi's localized-string boundary."""

    def __init__(self, entries):
        self.entries = entries

    def getLocalizedString(self, string_id):
        entry = self.entries[string_id]
        return entry["msgstr"] or entry["msgid"]


class ResourceContractTests(unittest.TestCase):
    def test_catalogs_are_strict_utf8_without_replacement_or_duplicate_ids(self):
        for language in CATALOGS:
            with self.subTest(language=language):
                text, _ = load_catalog(language)
                self.assertNotIn("\ufffd", text, CATALOGS[language])

    def test_legacy_references_and_english_progress_fallback(self):
        for language in CATALOGS:
            with self.subTest(language=language):
                _, entries = load_catalog(language)
                self.assertIn(30007, entries)
                self.assertIn(30216, entries)
                self.assertEqual(entries[30007]["msgid"], "Search")
                self.assertEqual(entries[30216]["msgid"], "Search in Backup DB")
                self.assertEqual(entries[30043]["msgid"], "Notify progress")
        _, english = load_catalog("en_GB")
        self.assertEqual(english[30043]["msgstr"], "")

    def test_sequential_tmdb_ids_have_exact_wording_in_real_catalogs(self):
        self.assertEqual(TMDB_IDS, set(range(30400, 30433)))
        for language, translation_index in (("cs_CZ", 2), ("sk_SK", 3), ("en_GB", None)):
            with self.subTest(language=language):
                _, entries = load_catalog(language)
                self.assertEqual(set(), TMDB_IDS - set(entries))
                for string_id, msgid, czech, slovak in TMDB_STRINGS:
                    with self.subTest(language=language, string_id=string_id):
                        self.assertEqual(entries[string_id]["msgid"], msgid)
                        if translation_index is None:
                            self.assertEqual(entries[string_id]["msgstr"], "")
                        else:
                            expected = (czech, slovak)[translation_index - 2]
                            self.assertEqual(entries[string_id]["msgstr"], expected)

    def test_sequential_download_job_ids_have_exact_wording_in_real_catalogs(self):
        self.assertEqual(JOB_IDS, {30433, 30437})
        for language, translation_index in (("cs_CZ", 2), ("sk_SK", 3), ("en_GB", None)):
            with self.subTest(language=language):
                _, entries = load_catalog(language)
                self.assertTrue({30434, 30435, 30436}.isdisjoint(entries))
                for string_id, msgid, czech, slovak in JOB_STRINGS:
                    with self.subTest(language=language, string_id=string_id):
                        self.assertEqual(entries[string_id]["msgid"], msgid)
                        expected = "" if translation_index is None else (czech, slovak)[translation_index - 2]
                        self.assertEqual(entries[string_id]["msgstr"], expected)

    def test_watchlist_and_empty_search_ux_ids_have_exact_native_wording(self):
        self.assertEqual(UX_IDS, {30438, 30439, 30440})
        for language, translation_index in (("cs_CZ", 2), ("sk_SK", 3), ("en_GB", None)):
            with self.subTest(language=language):
                _, entries = load_catalog(language)
                for string_id, msgid, czech, slovak in UX_STRINGS:
                    with self.subTest(language=language, string_id=string_id):
                        self.assertEqual(entries[string_id]["msgid"], msgid)
                        expected = "" if translation_index is None else (czech, slovak)[translation_index - 2]
                        self.assertEqual(entries[string_id]["msgstr"], expected)

    def test_all_settings_numeric_labels_and_lvalues_resolve(self):
        settings = ET.parse(str(ROOT / "resources/settings.xml")).getroot()
        references = set()
        new_tmdb_labels = set()
        for element in settings.iter():
            label = element.get("label")
            if label and label.isdigit():
                references.add(int(label))
                if int(label) >= 30400:
                    new_tmdb_labels.add(int(label))
            lvalues = element.get("lvalues")
            if lvalues:
                values = lvalues.split("|")
                self.assertTrue(all(value.isdigit() for value in values), lvalues)
                references.update(int(value) for value in values)
        self.assertEqual(new_tmdb_labels, set(range(30426, 30433)))
        for language in CATALOGS:
            with self.subTest(language=language):
                _, entries = load_catalog(language)
                self.assertEqual(set(), references - set(entries))

    def test_static_localization_references_resolve_through_existing_adapter(self):
        references = set()
        unresolved = []
        for path, tree in production_trees():
            file_references, file_unresolved = localization_references(tree)
            references.update(file_references)
            unresolved.extend((str(path),) + item for item in file_unresolved)
        self.assertEqual([], unresolved)
        self.assertIn(30007, references)
        self.assertTrue(TMDB_IDS - set(range(30426, 30433)) <= references)
        self.assertTrue(JOB_IDS <= references)
        self.assertTrue(UX_IDS <= references)
        for language in CATALOGS:
            with self.subTest(language=language):
                _, entries = load_catalog(language)
                self.assertEqual(set(), references - set(entries))
                runtime = SimpleNamespace(addon=CatalogAddon(entries))
                for string_id in sorted(references):
                    self.assertTrue(KodiRuntime.localized(runtime, string_id))

    def test_tmdb_visible_sinks_have_no_hardcoded_text(self):
        path = ROOT / "yawsp_app/tmdb_ui.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        references, unresolved = localization_references(tree)
        self.assertEqual([], unresolved)
        self.assertEqual(set(range(30400, 30426)) | {30438, 30439}, references)

        violations = []

        def check_text(expression, line, sink):
            if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
                if any(character.isalpha() for character in expression.value):
                    violations.append((line, sink, expression.value))
            elif isinstance(expression, ast.IfExp):
                check_text(expression.body, line, sink)
                check_text(expression.orelse, line, sink)
            elif isinstance(expression, ast.BoolOp):
                for value in expression.values:
                    check_text(value, line, sink)
            elif isinstance(expression, ast.BinOp):
                check_text(expression.left, line, sink)
                check_text(expression.right, line, sink)
            elif isinstance(expression, ast.JoinedStr):
                for value in expression.values:
                    check_text(value, line, sink)
            elif (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Attribute)
                and expression.func.attr == "format"
            ):
                check_text(expression.func.value, line, sink)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            if method == "notify" and node.args:
                check_text(node.args[0], node.lineno, method)
            elif method == "set_category" and node.args:
                check_text(node.args[0], node.lineno, method)
            elif method in ("list_item", "setInfo"):
                for keyword in node.keywords:
                    if keyword.arg == "label":
                        check_text(keyword.value, node.lineno, method + ".label")
                    elif keyword.arg == "title":
                        check_text(keyword.value, node.lineno, method + ".title")
            elif method == "addContextMenuItems" and node.args:
                for command in ast.walk(node.args[0]):
                    if isinstance(command, (ast.Tuple, ast.List)) and command.elts:
                        check_text(command.elts[0], node.lineno, method)
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
