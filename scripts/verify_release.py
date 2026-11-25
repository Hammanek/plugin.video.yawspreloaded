"""Build and verify the tracked Kodi runtime archive without network access."""

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile


ADDON_ID = "plugin.video.yawspreloaded"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_RUNTIME_FILES = frozenset(
    {
        "LICENSE",
        "addon.xml",
        "main.py",
        "md5crypt.py",
        "qrcodegen.py",
        "service.py",
        "yawsp.py",
    }
)
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".github",
        ".superpowers",
        "__pycache__",
        "docs",
        "tests",
        "user_data",
        "userdata",
    }
)
EXCLUDED_FILENAMES = frozenset(
    {
        ".env",
        ".netrc",
        "credentials",
        "credentials.json",
        "credentials.xml",
        "secret_assembled.payload",
        "session.json",
        "sessions.json",
        "tokens.json",
    }
)
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$")
DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class ReleaseVerificationError(Exception):
    """A release source or archive does not satisfy the packaging contract."""


class ReleaseVerificationResult:
    def __init__(self, version, runtime_files, sha256, archive_root, archive_path):
        self.version = version
        self.runtime_files = runtime_files
        self.file_count = len(runtime_files)
        self.sha256 = sha256
        self.archive_root = archive_root
        self.archive_path = archive_path


def _validate_repo_path(name):
    if not isinstance(name, str) or not name:
        raise ReleaseVerificationError("runtime path must be a non-empty string")
    if "\\" in name or name.startswith("/") or DRIVE_PATTERN.match(name):
        raise ReleaseVerificationError("unsafe absolute or platform-specific path: " + name)
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ReleaseVerificationError("unsafe control character in runtime path")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ReleaseVerificationError("unsafe traversal or empty path segment: " + name)
    return name


def _is_excluded(name):
    parts = name.split("/")
    lowered_parts = {part.casefold() for part in parts}
    if lowered_parts & EXCLUDED_PARTS:
        return True
    filename = parts[-1].casefold()
    return (
        filename in EXCLUDED_FILENAMES
        or filename.endswith((".pyc", ".pyo"))
    )


def _is_runtime_path(name):
    if _is_excluded(name):
        return False
    if name in ROOT_RUNTIME_FILES:
        return True
    if name.startswith("yawsp_app/"):
        relative = name[len("yawsp_app/") :]
        return "/" not in relative and relative.endswith(".py")
    return name.startswith("resources/") and len(name) > len("resources/")


def _git_output(root, arguments):
    try:
        completed = subprocess.run(
            ["git"] + list(arguments),
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReleaseVerificationError("unable to run git for release verification") from exc
    if completed.returncode != 0:
        raise ReleaseVerificationError("path is not a usable git checkout root: " + str(root))
    return completed.stdout


def _checkout_and_tracked_files(source_root):
    try:
        root = Path(source_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ReleaseVerificationError("release source is not an accessible checkout root") from exc
    if not root.is_dir():
        raise ReleaseVerificationError("release source is not a checkout root: " + str(root))

    inside = _git_output(root, ["rev-parse", "--is-inside-work-tree"])
    if inside.strip() != b"true":
        raise ReleaseVerificationError("release source is not a git work tree: " + str(root))
    top_level = _git_output(root, ["rev-parse", "--show-toplevel"])
    try:
        top_text = top_level.decode("utf-8").strip()
        actual_root = Path(top_text).resolve(strict=True)
    except (UnicodeDecodeError, OSError, RuntimeError, ValueError) as exc:
        raise ReleaseVerificationError("git returned an invalid repository root") from exc
    if os.path.normcase(str(actual_root)) != os.path.normcase(str(root)):
        raise ReleaseVerificationError(
            "release source must be the repository root, not a nested directory: " + str(root)
        )

    # NUL delimiters preserve spaces and non-ASCII names without decoder ambiguity.
    raw_names = _git_output(root, ["ls-tree", "-r", "--name-only", "-z", "HEAD"])
    names = []
    for raw_name in raw_names.split(b"\0"):
        if not raw_name:
            continue
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseVerificationError("git returned a non-UTF-8 tracked path") from exc
        names.append(_validate_repo_path(name))
    return root, tuple(names)


def _select_runtime_files(tracked, trusted_files):
    tracked_set = set(tracked)
    if "README.md" not in tracked_set:
        raise ReleaseVerificationError("README.md is not tracked by HEAD")
    if trusted_files is None:
        selected = tuple(name for name in tracked if _is_runtime_path(name))
    else:
        requested = list(trusted_files)
        if not requested:
            raise ReleaseVerificationError("trusted runtime include list is empty")
        checked = []
        for name in requested:
            name = _validate_repo_path(name)
            if not _is_runtime_path(name):
                raise ReleaseVerificationError("trusted path is not a runtime file: " + name)
            if name not in tracked_set:
                raise ReleaseVerificationError("trusted runtime path is not tracked by HEAD: " + name)
            checked.append(name)
        if len(set(checked)) != len(checked):
            raise ReleaseVerificationError("trusted runtime include list contains duplicates")
        selected = tuple(sorted(checked))
    if "addon.xml" not in selected:
        raise ReleaseVerificationError("selected runtime files do not include addon.xml")
    return selected


def _read_source_file(root, name):
    name = _validate_repo_path(name)
    current = root
    try:
        for part in name.split("/")[:-1]:
            current = current / part
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise ReleaseVerificationError(
                    "source parent is not a standard directory: " + name
                )
        path = current / name.split("/")[-1]
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ReleaseVerificationError("source path is not a regular file: " + name)
        return path.read_bytes()
    except ReleaseVerificationError:
        raise
    except OSError as exc:
        raise ReleaseVerificationError("unable to read source file: " + name) from exc


def _decode_utf8(data, name):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseVerificationError(name + " is not valid UTF-8") from exc


def _manifest_version_and_entrypoint(root):
    source = _decode_utf8(_read_source_file(root, "addon.xml"), "addon.xml")
    try:
        manifest = ET.fromstring(source)
    except ET.ParseError as exc:
        raise ReleaseVerificationError("addon.xml is malformed XML") from exc
    if manifest.tag != "addon" or manifest.get("id") != ADDON_ID:
        raise ReleaseVerificationError("addon.xml has the wrong addon root or id")
    version = manifest.get("version", "")
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseVerificationError("addon.xml has a missing or unsafe version")

    entrypoints = [
        extension.get("library", "")
        for extension in manifest.findall("extension")
        if extension.get("point") == "xbmc.python.pluginsource"
    ]
    if len(entrypoints) != 1:
        raise ReleaseVerificationError("addon.xml needs exactly one plugin source extension")
    entrypoint = _validate_repo_path(entrypoints[0])
    return version, entrypoint


def _verify_readme(root, version):
    readme = _decode_utf8(_read_source_file(root, "README.md"), "README.md")
    repository = "https://github.com/Hammanek/plugin.video.yawspreloaded"
    required = {
        "README version badge": "https://img.shields.io/badge/verze-{0}-blue.svg".format(version),
        "README setup ZIP link": "{0}/releases/download/v{1}/{2}-{1}.zip".format(
            repository, version, ADDON_ID
        ),
        "README version release link": "{0}/releases/tag/v{1}".format(repository, version),
        "README latest-release link": repository + "/releases/latest",
        "README setup ZIP guidance": "Instalovat ze souboru zip",
    }
    for description, expected in required.items():
        if expected not in readme:
            raise ReleaseVerificationError(description + " is missing or does not match addon.xml")


def _archive_member_name(name):
    if not isinstance(name, str) or not name:
        raise ReleaseVerificationError("archive contains an empty member name")
    if "\\" in name or name.startswith("/") or DRIVE_PATTERN.match(name):
        raise ReleaseVerificationError("unsafe absolute or platform-specific archive path: " + name)
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ReleaseVerificationError("unsafe control character in archive path")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ReleaseVerificationError("unsafe archive path traversal or empty segment: " + name)
    return name


def _zip_info(name):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def validate_archive(archive_path, source_root, runtime_files, archive_root=ADDON_ID):
    """Validate member names, uniqueness, CRCs and exact checked-out source bytes."""
    if archive_root != ADDON_ID:
        raise ReleaseVerificationError("archive root must be " + ADDON_ID)
    prefix = archive_root + "/"
    expected_relative = tuple(runtime_files)
    expected = set()
    for relative in expected_relative:
        expected.add(prefix + _validate_repo_path(relative))

    try:
        archive_path = Path(archive_path)
        with zipfile.ZipFile(str(archive_path), "r") as archive:
            infos = archive.infolist()
            actual = []
            for info in infos:
                member = _archive_member_name(info.filename)
                if not member.startswith(prefix) or len(member) <= len(prefix):
                    raise ReleaseVerificationError(
                        "archive member has wrong top-level root; expected " + prefix + ": " + member
                    )
                actual.append(member)
            if len(actual) != len(set(actual)):
                raise ReleaseVerificationError("archive contains duplicate member names")
            actual_set = set(actual)
            missing = sorted(expected - actual_set)
            extra = sorted(actual_set - expected)
            if missing or extra:
                details = []
                if missing:
                    details.append("missing=" + repr(missing))
                if extra:
                    details.append("extra=" + repr(extra))
                raise ReleaseVerificationError("archive member set mismatch: " + "; ".join(details))
            try:
                corrupt_member = archive.testzip()
            except (OSError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
                raise ReleaseVerificationError("archive CRC validation failed") from exc
            if corrupt_member is not None:
                raise ReleaseVerificationError("archive CRC mismatch: " + corrupt_member)
            for info in infos:
                relative = info.filename[len(prefix) :]
                try:
                    archived = archive.read(info)
                except (OSError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
                    raise ReleaseVerificationError(
                        "unable to read archived file: " + relative
                    ) from exc
                source = _read_source_file(source_root, relative)
                if archived != source:
                    raise ReleaseVerificationError(
                        "archived bytes differ from source file: " + relative
                    )
    except ReleaseVerificationError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ReleaseVerificationError("invalid or unreadable ZIP archive") from exc


def _build_archive(root, runtime_files, version, archive_directory):
    try:
        directory = Path(archive_directory)
        directory.mkdir(parents=True, exist_ok=True)
        archive_path = directory / "{0}-{1}.zip".format(ADDON_ID, version)
        with zipfile.ZipFile(str(archive_path), "x") as archive:
            for relative in runtime_files:
                data = _read_source_file(root, relative)
                archive.writestr(_zip_info(ADDON_ID + "/" + relative), data)
    except ReleaseVerificationError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ReleaseVerificationError("unable to build release ZIP archive") from exc
    return archive_path


def _archive_sha256(archive_path):
    digest = hashlib.sha256()
    try:
        with archive_path.open("rb") as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise ReleaseVerificationError("unable to hash release ZIP archive") from exc
    return digest.hexdigest()


def _verify_in_directory(root, runtime_files, version, archive_directory):
    archive_path = _build_archive(root, runtime_files, version, archive_directory)
    validate_archive(archive_path, root, runtime_files)
    return ReleaseVerificationResult(
        version=version,
        runtime_files=runtime_files,
        sha256=_archive_sha256(archive_path),
        archive_root=ADDON_ID,
        archive_path=str(archive_path),
    )


def verify_release(source_root=None, trusted_files=None, archive_directory=None):
    """Verify a checkout, using a private temporary archive unless a test directory is supplied."""
    if source_root is None:
        source_root = PROJECT_ROOT
    root, tracked = _checkout_and_tracked_files(source_root)
    runtime_files = _select_runtime_files(tracked, trusted_files)
    version, entrypoint = _manifest_version_and_entrypoint(root)
    if entrypoint not in runtime_files:
        raise ReleaseVerificationError("addon entry point is not selected for packaging: " + entrypoint)
    _verify_readme(root, version)
    if archive_directory is not None:
        return _verify_in_directory(root, runtime_files, version, archive_directory)
    with tempfile.TemporaryDirectory(prefix="yawsp-release-") as temporary:
        return _verify_in_directory(root, runtime_files, version, temporary)


def main():
    try:
        result = verify_release()
    except ReleaseVerificationError as exc:
        print("release verification failed: " + str(exc), file=sys.stderr)
        return 1
    print("version=" + result.version)
    print("runtime_files={0}".format(result.file_count))
    print("sha256=" + result.sha256)
    print("archive_root=" + result.archive_root)
    print("archive_path=" + result.archive_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
