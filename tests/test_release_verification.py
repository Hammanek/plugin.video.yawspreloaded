import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import warnings
import xml.etree.ElementTree as ET
import zipfile

from scripts.verify_release import (
    ADDON_ID,
    ReleaseVerificationError,
    validate_archive,
    verify_release,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VERSION = "1.2.3"


def fixture_readme(version):
    return """# Fixture

[![Version](https://img.shields.io/badge/verze-{0}-blue.svg)](https://example.invalid)
[plugin.video.yawspreloaded-{0}.zip](https://github.com/Hammanek/plugin.video.yawspreloaded/releases/download/v{0}/plugin.video.yawspreloaded-{0}.zip)
[release v{0}](https://github.com/Hammanek/plugin.video.yawspreloaded/releases/tag/v{0})
[latest](https://github.com/Hammanek/plugin.video.yawspreloaded/releases/latest)
Doplňky -> Instalovat ze souboru zip
""".format(version)


def fixture_manifest(version=FIXTURE_VERSION, library="main.py"):
    return """<?xml version="1.0" encoding="UTF-8"?>
<addon id="plugin.video.yawspreloaded" version="{0}">
  <extension point="xbmc.python.pluginsource" library="{1}"/>
</addon>
""".format(version, library)


class GitCheckoutFixture:
    def __init__(self, test_case):
        self.test_case = test_case
        self.temporary = tempfile.TemporaryDirectory()
        test_case.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkout"
        self.root.mkdir()
        self._git("init", "--quiet")
        self.write("addon.xml", fixture_manifest())
        self.write("README.md", fixture_readme(FIXTURE_VERSION))
        self.write("LICENSE", "fixture license\n")
        self.write("main.py", b"# fixture entrypoint\n")
        self.write("yawsp_app/__init__.py", b"")
        self.write("yawsp_app/metadata.py", b"FIXTURE_METADATA = b'\\x00\\xff'\n")
        self.write("resources/settings.xml", "<settings/>\n")
        self.write("resources/icon.png", b"\x89PNG\r\n\x1a\nfixture\x00")
        self.write("tests/test_not_runtime.py", b"raise AssertionError('must not ship')\n")
        self.write("docs/not-runtime.md", b"not runtime\n")
        self.write(".github/workflows/not-runtime.yml", b"not runtime\n")
        self.write(".superpowers/sdd/not-runtime.json", b"{}\n")
        self._git("add", "--", ".")
        self._git(
            "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid",
            "commit", "--quiet", "-m", "synthetic fixture",
        )

    def _git(self, *arguments):
        environment = os.environ.copy()
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        completed = subprocess.run(
            ["git"] + list(arguments),
            cwd=str(self.root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(
                "fixture git command failed with exit code {0}".format(completed.returncode)
            )
        return completed

    def write(self, relative, content):
        path = self.root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def commit(self, *paths):
        self._git("add", "--", *(paths or (".",)))
        self._git(
            "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid",
            "commit", "--quiet", "-m", "fixture update",
        )


class ReleaseVerificationTests(unittest.TestCase):
    def make_fixture(self):
        return GitCheckoutFixture(self)

    def test_current_checkout_computes_runtime_set_and_validates_crc_and_exact_bytes(self):
        tracked = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=str(ROOT), check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.splitlines()
        root_files = {
            "LICENSE", "addon.xml", "main.py", "md5crypt.py", "qrcodegen.py",
            "service.py", "yawsp.py",
        }
        expected = tuple(
            name for name in tracked
            if name in root_files
            or (name.startswith("yawsp_app/") and name.endswith(".py") and "/" not in name[10:])
            or name.startswith("resources/")
        )
        self.assertIn("yawsp_app/metadata.py", expected)

        with tempfile.TemporaryDirectory() as archive_directory:
            result = verify_release(ROOT, archive_directory=archive_directory)
            self.assertEqual(expected, result.runtime_files)
            self.assertEqual(len(expected), result.file_count)
            self.assertEqual(ADDON_ID, result.archive_root)
            self.assertEqual(
                ET.parse(str(ROOT / "addon.xml")).getroot().get("version"),
                result.version,
            )
            archive_path = Path(result.archive_path)
            self.assertEqual(
                hashlib.sha256(archive_path.read_bytes()).hexdigest(), result.sha256
            )
            with zipfile.ZipFile(str(archive_path)) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(
                    [ADDON_ID + "/" + name for name in result.runtime_files],
                    archive.namelist(),
                )
                for name in result.runtime_files:
                    with self.subTest(path=name):
                        self.assertEqual(
                            ROOT.joinpath(*name.split("/")).read_bytes(),
                            archive.read(ADDON_ID + "/" + name),
                        )

    def test_valid_fixture_result_reports_version_root_count_and_excludes_untracked_artifacts(self):
        fixture = self.make_fixture()
        fixture.write("secret_assembled.payload", b"synthetic non-runtime artifact")
        fixture.write("yawsp_app/__pycache__/stale.pyc", b"stale")
        fixture.write("resources/__pycache__/stale.pyc", b"stale")

        with tempfile.TemporaryDirectory() as archive_directory:
            result = verify_release(fixture.root, archive_directory=archive_directory)
            self.assertEqual(FIXTURE_VERSION, result.version)
            self.assertEqual(ADDON_ID, result.archive_root)
            self.assertEqual(len(result.runtime_files), result.file_count)
            self.assertIn("yawsp_app/metadata.py", result.runtime_files)
            self.assertEqual(7, result.file_count)
            with zipfile.ZipFile(str(result.archive_path)) as archive:
                self.assertIsNone(archive.testzip())
                self.assertNotIn(ADDON_ID + "/secret_assembled.payload", archive.namelist())
                self.assertFalse(any("__pycache__" in name for name in archive.namelist()))

    def test_trusted_include_list_and_non_ascii_resource_name_use_tracked_paths(self):
        fixture = self.make_fixture()
        fixture.write("resources/český obrázek.png", b"non-ascii fixture bytes")
        fixture.commit("resources/český obrázek.png")

        with tempfile.TemporaryDirectory() as archive_directory:
            result = verify_release(
                fixture.root,
                trusted_files=["addon.xml", "main.py", "resources/český obrázek.png"],
                archive_directory=archive_directory,
            )
            self.assertEqual(
                ("addon.xml", "main.py", "resources/český obrázek.png"),
                result.runtime_files,
            )

    def test_missing_git_checkout_or_nested_directory_fails_without_using_parent_repository(self):
        fixture = self.make_fixture()
        nested = fixture.root / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ReleaseVerificationError, "checkout root|repository root"):
            verify_release(nested)
        with tempfile.TemporaryDirectory() as unrelated:
            with self.assertRaisesRegex(ReleaseVerificationError, "checkout|repository"):
                verify_release(unrelated)

    def test_malformed_xml_version_and_entrypoint_are_domain_errors(self):
        cases = (
            ("<addon", "addon.xml"),
            (fixture_manifest(version=""), "version"),
            (fixture_manifest(library="not-packaged.py"), "not-packaged.py"),
        )
        for manifest, message in cases:
            with self.subTest(message=message):
                fixture = self.make_fixture()
                fixture.write("addon.xml", manifest)
                with self.assertRaisesRegex(ReleaseVerificationError, message):
                    verify_release(fixture.root)

    def test_readme_badge_and_versioned_setup_links_must_match_manifest(self):
        for replacement in (
            ("verze-1.2.3-blue.svg", "verze-9.9.9-blue.svg"),
            ("/releases/download/v1.2.3/", "/releases/download/v9.9.9/"),
            ("/releases/tag/v1.2.3", "/releases/tag/v9.9.9"),
            ("Instalovat ze souboru zip", "installation guidance removed"),
        ):
            with self.subTest(replacement=replacement[0]):
                fixture = self.make_fixture()
                readme = (fixture.root / "README.md").read_text(encoding="utf-8")
                fixture.write("README.md", readme.replace(*replacement))
                with self.assertRaisesRegex(ReleaseVerificationError, "README|badge|setup|release"):
                    verify_release(fixture.root)

    def test_missing_tracked_metadata_is_an_error_not_a_glob_omission(self):
        fixture = self.make_fixture()
        (fixture.root / "yawsp_app" / "metadata.py").rename(
            fixture.root / "yawsp_app" / "metadata.invalid"
        )
        with self.assertRaisesRegex(ReleaseVerificationError, "yawsp_app/metadata.py"):
            verify_release(fixture.root)

    def test_wrong_archive_prefix_fails_with_expected_root(self):
        fixture = self.make_fixture()
        archive = self.write_archive(
            fixture.root,
            [("wrong-root/addon.xml", (fixture.root / "addon.xml").read_bytes())],
        )
        with self.assertRaisesRegex(ReleaseVerificationError, ADDON_ID):
            validate_archive(archive, fixture.root, ["addon.xml"])

    def test_archive_rejects_traversal_absolute_extra_and_duplicate_names(self):
        fixture = self.make_fixture()
        source = (fixture.root / "main.py").read_bytes()
        valid = [
            (ADDON_ID + "/addon.xml", (fixture.root / "addon.xml").read_bytes()),
            (ADDON_ID + "/main.py", source),
        ]
        cases = (
            ("traversal", valid + [(ADDON_ID + "/../escape", b"escape")], "traversal|unsafe"),
            ("absolute", valid + [("/absolute", b"absolute")], "absolute|unsafe"),
            ("extra", valid + [(ADDON_ID + "/extra.py", b"extra")], "extra.py"),
            ("duplicate", valid + [(ADDON_ID + "/main.py", source)], "duplicate|main.py"),
        )
        for name, entries, message in cases:
            with self.subTest(case=name):
                archive = self.write_archive(fixture.root, entries, name + ".zip")
                with self.assertRaisesRegex(ReleaseVerificationError, message):
                    validate_archive(archive, fixture.root, ["addon.xml", "main.py"])

    def test_archive_altered_file_byte_differs_from_checked_out_head_source(self):
        fixture = self.make_fixture()
        entries = [
            (ADDON_ID + "/addon.xml", (fixture.root / "addon.xml").read_bytes()),
            (ADDON_ID + "/main.py", b"# byte altered after tracked source was checked\n"),
        ]
        archive = self.write_archive(fixture.root, entries, "altered.zip")
        with self.assertRaisesRegex(ReleaseVerificationError, "main.py|bytes"):
            validate_archive(archive, fixture.root, ["addon.xml", "main.py"])

    def test_bad_zip_and_archive_without_manifest_are_domain_errors(self):
        fixture = self.make_fixture()
        bad_zip = fixture.root / "bad.zip"
        bad_zip.write_bytes(b"not a zip file")
        with self.assertRaisesRegex(ReleaseVerificationError, "ZIP|archive"):
            validate_archive(bad_zip, fixture.root, ["addon.xml"])

        no_manifest = self.write_archive(fixture.root, [], "empty.zip")
        with self.assertRaisesRegex(ReleaseVerificationError, "addon.xml|missing"):
            validate_archive(no_manifest, fixture.root, ["addon.xml"])

    def test_invalid_trusted_paths_and_non_regular_sources_fail_cleanly(self):
        fixture = self.make_fixture()
        for path in ("tests/test_not_runtime.py", "../outside", "resources/missing.png"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ReleaseVerificationError, "path|tracked|runtime"):
                    verify_release(fixture.root, trusted_files=[path])
        (fixture.root / "resources" / "icon.png").unlink()
        (fixture.root / "resources" / "icon.png").mkdir()
        with self.assertRaisesRegex(ReleaseVerificationError, "icon.png|regular file"):
            verify_release(fixture.root)

    def write_archive(self, root, entries, name="archive.zip"):
        path = root / name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(str(path), "w") as archive:
                for member, content in entries:
                    archive.writestr(member, content)
        return path


if __name__ == "__main__":
    unittest.main()
