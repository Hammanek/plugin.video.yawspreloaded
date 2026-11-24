"""Structurally validated and atomically recoverable Backup DB storage."""

import io
import json
import ntpath
import os
import shutil
import stat
import tempfile
import zipfile


_DATABASE_DIRECTORY = "db"
_MANIFEST_FILENAME = ".manifest.json"
_MANIFEST_VERSION = 1


class BackupDBError(RuntimeError):
    """A Backup DB archive or installed database is unusable."""


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, UnicodeError, ValueError) as error:
        raise BackupDBError("Invalid Backup DB JSON in {0}".format(path)) from error
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise BackupDBError(
            "Backup DB file {0} must contain an object with a data list".format(path)
        )
    return value


def _data_files(directory):
    try:
        entries = list(os.scandir(directory))
    except OSError as error:
        raise BackupDBError("Cannot inspect Backup DB directory") from error

    data_files = {}
    for entry in entries:
        if not entry.name.lower().endswith(".txt"):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                raise BackupDBError(
                    "Backup DB data member is not a regular file: {0}".format(entry.name)
                )
            _read_json(entry.path)
            data_files[entry.name] = entry.stat(follow_symlinks=False).st_size
        except OSError as error:
            raise BackupDBError(
                "Cannot inspect Backup DB file: {0}".format(entry.name)
            ) from error
    if not data_files:
        raise BackupDBError("Backup DB contains no regular .txt data files")
    return data_files


def _manifest_path(directory):
    return os.path.join(directory, _MANIFEST_FILENAME)


def _check_manifest(directory, actual_files):
    path = _manifest_path(directory)
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as source:
            manifest = json.load(source)
    except (OSError, UnicodeError, ValueError) as error:
        raise BackupDBError("Invalid Backup DB manifest") from error

    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != _MANIFEST_VERSION
        or not isinstance(manifest.get("files"), dict)
    ):
        raise BackupDBError("Invalid Backup DB manifest structure")

    recorded_files = manifest["files"]
    for filename, size in recorded_files.items():
        if (
            not isinstance(filename, str)
            or filename != os.path.basename(filename)
            or not filename.lower().endswith(".txt")
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise BackupDBError("Invalid Backup DB manifest entry")
    if recorded_files != actual_files:
        raise BackupDBError("Installed Backup DB does not match its manifest")


def _validate_directory(directory):
    if os.path.islink(directory) or not os.path.isdir(directory):
        raise BackupDBError("Backup DB directory is missing or unsafe")
    actual_files = _data_files(directory)
    _check_manifest(directory, actual_files)


def _write_manifest(directory, data_files):
    path = _manifest_path(directory)
    manifest = {"version": _MANIFEST_VERSION, "files": data_files}
    try:
        with open(path, "w", encoding="utf-8") as output:
            json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
    except OSError as error:
        raise BackupDBError("Cannot record Backup DB manifest") from error


def _member_target(root, member):
    filename = member.filename
    normalized = filename.replace("\\", "/")
    if (
        not filename
        or "\x00" in filename
        or normalized.startswith("/")
        or ntpath.splitdrive(filename)[0]
        or ".." in normalized.split("/")
    ):
        raise BackupDBError("Unsafe Backup DB archive member: {0}".format(filename))

    mode = member.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise BackupDBError("Unsafe Backup DB archive link: {0}".format(filename))
    file_type = stat.S_IFMT(mode)
    if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise BackupDBError("Unsafe Backup DB archive member type: {0}".format(filename))

    target = os.path.abspath(os.path.join(root, *normalized.split("/")))
    try:
        contained = os.path.commonpath((root, target)) == root
    except ValueError:
        contained = False
    if not contained:
        raise BackupDBError("Unsafe Backup DB archive member: {0}".format(filename))


def _extract_archive(archive_data, destination):
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data), "r") as archive:
            for member in archive.infolist():
                _member_target(destination, member)
            bad_member = archive.testzip()
            if bad_member is not None:
                raise BackupDBError(
                    "Backup DB archive CRC check failed for {0}".format(bad_member)
                )
            archive.extractall(destination)
    except BackupDBError:
        raise
    except (OSError, TypeError, ValueError, zipfile.BadZipFile) as error:
        raise BackupDBError("Invalid Backup DB ZIP archive") from error


def _prepared_database(temporary_directory):
    extracted = os.path.join(temporary_directory, "extracted")
    os.mkdir(extracted)
    return extracted


def _archived_database_root(extracted):
    nested = os.path.join(extracted, _DATABASE_DIRECTORY)
    return nested if os.path.isdir(nested) and not os.path.islink(nested) else extracted


class BackupDBStore:
    def __init__(self, profile):
        self.profile = os.path.abspath(os.fspath(profile))
        self.dbdir = os.path.join(self.profile, _DATABASE_DIRECTORY)

    def is_valid(self):
        try:
            _validate_directory(self.dbdir)
        except BackupDBError:
            return False
        return True

    def ensure_installed(self, archive_loader):
        """Install one fully validated archive, leaving old state intact on failure."""
        if self.is_valid():
            return True

        temporary_directory = None
        try:
            os.makedirs(self.profile, exist_ok=True)
            temporary_directory = tempfile.mkdtemp(
                prefix=".backup-db-", dir=self.profile
            )
            archive_data = archive_loader()
            extracted = _prepared_database(temporary_directory)
            _extract_archive(archive_data, extracted)
            source_database = _archived_database_root(extracted)
            data_files = _data_files(source_database)
            _write_manifest(source_database, data_files)

            staged_database = os.path.join(temporary_directory, "staged-db")
            os.replace(source_database, staged_database)
            previous_database = os.path.join(temporary_directory, "previous-db")
            previous_moved = False
            installed = False
            try:
                if os.path.lexists(self.dbdir):
                    os.replace(self.dbdir, previous_database)
                    previous_moved = True
                os.replace(staged_database, self.dbdir)
                installed = True
            finally:
                if (
                    previous_moved
                    and not installed
                    and os.path.lexists(previous_database)
                    and not os.path.lexists(self.dbdir)
                ):
                    try:
                        os.replace(previous_database, self.dbdir)
                    except OSError:
                        pass
            return True
        except BackupDBError:
            raise
        except Exception as error:
            raise BackupDBError("Backup DB installation failed: {0}".format(error)) from error
        finally:
            if temporary_directory is not None:
                shutil.rmtree(temporary_directory, ignore_errors=True)

    @staticmethod
    def group_for_query(text):
        stripped = text.strip()
        if not stripped:
            return "0.txt"
        initial = stripped[0]
        if "a" <= initial <= "z":
            initial = initial.upper()
        return initial + ".txt" if "A" <= initial <= "Z" else "0.txt"

    def load(self, filename):
        if (
            not isinstance(filename, str)
            or not filename.lower().endswith(".txt")
            or filename != os.path.basename(filename)
            or "/" in filename
            or "\\" in filename
        ):
            raise BackupDBError("Unsafe Backup DB filename: {0}".format(filename))
        _validate_directory(self.dbdir)
        data = _read_json(os.path.join(self.dbdir, filename))["data"]
        if not all(isinstance(item, dict) for item in data):
            raise BackupDBError("Backup DB data items must be JSON objects")
        return data
