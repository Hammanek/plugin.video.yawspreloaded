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
_RECOVERY_DIRECTORY = ".backup-db-recovery"
_PREVIOUS_DATABASE = "previous-db"
_MANIFEST_FILENAME = ".manifest.json"
_MANIFEST_VERSION = 1
_SIGNATURE_FILENAME = ".backup-db-validation.json"
_SIGNATURE_VERSION = 1


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


def _data_file_metadata(directory, parse_documents):
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
            if parse_documents:
                _read_json(entry.path)
            identity = entry.stat(follow_symlinks=False)
            data_files[entry.name] = {
                "size": identity.st_size,
                "mtime_ns": identity.st_mtime_ns,
            }
        except OSError as error:
            raise BackupDBError(
                "Cannot inspect Backup DB file: {0}".format(entry.name)
            ) from error
    if not data_files:
        raise BackupDBError("Backup DB contains no regular .txt data files")
    return data_files


def _data_file_sizes(data_files):
    return {filename: identity["size"] for filename, identity in data_files.items()}


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
    data_files = _data_file_metadata(directory, parse_documents=True)
    _check_manifest(directory, _data_file_sizes(data_files))
    return data_files


def _write_manifest(directory, data_files):
    path = _manifest_path(directory)
    manifest = {"version": _MANIFEST_VERSION, "files": data_files}
    try:
        with open(path, "w", encoding="utf-8") as output:
            json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
    except OSError as error:
        raise BackupDBError("Cannot record Backup DB manifest") from error


def _read_validation_signature(path):
    try:
        with open(path, "r", encoding="utf-8") as source:
            signature = json.load(source)
    except (OSError, UnicodeError, ValueError) as error:
        raise BackupDBError("Invalid Backup DB validation signature") from error

    if (
        not isinstance(signature, dict)
        or isinstance(signature.get("schema_version"), bool)
        or signature.get("schema_version") != _SIGNATURE_VERSION
        or not isinstance(signature.get("files"), dict)
        or not signature["files"]
    ):
        raise BackupDBError("Invalid Backup DB validation signature structure")

    for filename, identity in signature["files"].items():
        if (
            not isinstance(filename, str)
            or filename != os.path.basename(filename)
            or "/" in filename
            or "\\" in filename
            or not filename.lower().endswith(".txt")
            or not isinstance(identity, dict)
        ):
            raise BackupDBError("Invalid Backup DB validation signature entry")
        for field in ("size", "mtime_ns"):
            value = identity.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BackupDBError("Invalid Backup DB validation signature entry")
    return signature["files"]


def _write_validation_signature(path, data_files):
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".backup-db-validation-", dir=os.path.dirname(path)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(
                {"schema_version": _SIGNATURE_VERSION, "files": data_files},
                output,
                sort_keys=True,
                separators=(",", ":"),
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, TypeError, ValueError) as error:
        raise BackupDBError("Cannot record Backup DB validation signature") from error
    finally:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except OSError:
                pass


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


def _extract_archive(archive_source, destination):
    try:
        source = (
            io.BytesIO(archive_source)
            if isinstance(archive_source, (bytes, bytearray, memoryview))
            else archive_source
        )
        with zipfile.ZipFile(source, "r") as archive:
            for member in archive.infolist():
                _member_target(destination, member)
            # extractall reads every member fully; ZipExtFile verifies each CRC.
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
        self.signature_path = os.path.join(self.profile, _SIGNATURE_FILENAME)
        self.recovery_dir = os.path.join(self.profile, _RECOVERY_DIRECTORY)
        self.previous_database = os.path.join(
            self.recovery_dir, _PREVIOUS_DATABASE
        )

    def _remove_empty_recovery_directory(self):
        try:
            os.rmdir(self.recovery_dir)
        except OSError:
            pass

    def _discard_previous_database(self):
        if os.path.isdir(self.previous_database) and not os.path.islink(
            self.previous_database
        ):
            shutil.rmtree(self.previous_database)
        else:
            os.remove(self.previous_database)
        self._remove_empty_recovery_directory()

    def _recover_previous_database(self):
        if not os.path.lexists(self.previous_database):
            return
        if os.path.lexists(self.dbdir):
            if not self.is_valid():
                raise BackupDBError(
                    "Backup DB recovery and an invalid current installation both exist; "
                    "neither was removed"
                )
            try:
                self._discard_previous_database()
            except OSError as error:
                raise BackupDBError("Cannot discard recovered Backup DB state") from error
            return
        try:
            os.replace(self.previous_database, self.dbdir)
        except OSError as error:
            raise BackupDBError(
                "Cannot restore Backup DB from {0}: {1}".format(
                    self.previous_database, error
                )
            ) from error
        self._remove_empty_recovery_directory()

    def _matches_validation_signature(self):
        if os.path.islink(self.dbdir) or not os.path.isdir(self.dbdir):
            return False
        try:
            recorded = _read_validation_signature(self.signature_path)
            actual = _data_file_metadata(self.dbdir, parse_documents=False)
        except BackupDBError:
            return False
        return recorded == actual

    def _validate_and_record(self):
        data_files = _validate_directory(self.dbdir)
        _write_validation_signature(self.signature_path, data_files)

    def is_valid(self):
        if self._matches_validation_signature():
            return True
        try:
            # Structurally valid legacy/drifted data becomes warm only after its
            # refreshed metadata signature is durable; a failed write is not valid.
            self._validate_and_record()
        except BackupDBError:
            return False
        return True

    def ensure_installed(self, archive_loader):
        """Install one fully validated archive, leaving old state intact on failure.

        Byte loaders remain supported. A loader may instead return an owned temporary
        ZIP path; the store closes its extraction handle and removes that path in all
        success and failure cases.
        """
        self._recover_previous_database()
        if self._matches_validation_signature():
            return True
        try:
            self._validate_and_record()
            return True
        except BackupDBError:
            pass

        temporary_directory = None
        archive_source = None
        try:
            os.makedirs(self.profile, exist_ok=True)
            temporary_directory = tempfile.mkdtemp(
                prefix=".backup-db-", dir=self.profile
            )
            archive_source = archive_loader()
            extracted = _prepared_database(temporary_directory)
            _extract_archive(archive_source, extracted)
            source_database = _archived_database_root(extracted)
            data_files = _validate_directory(source_database)
            _write_manifest(source_database, _data_file_sizes(data_files))

            staged_database = os.path.join(temporary_directory, "staged-db")
            os.replace(source_database, staged_database)
            previous_moved = False
            installed = False
            try:
                if os.path.lexists(self.dbdir):
                    os.makedirs(self.recovery_dir, exist_ok=True)
                    os.replace(self.dbdir, self.previous_database)
                    previous_moved = True
                os.replace(staged_database, self.dbdir)
                installed = True
                # Commit the new marker only with the promoted bytes. If this
                # fails, the same rollback restores both previous data and marker.
                _write_validation_signature(self.signature_path, data_files)
            except Exception as promotion_error:
                try:
                    if installed and os.path.lexists(self.dbdir):
                        shutil.rmtree(self.dbdir)
                    if (
                        previous_moved
                        and os.path.lexists(self.previous_database)
                        and not os.path.lexists(self.dbdir)
                    ):
                        os.replace(self.previous_database, self.dbdir)
                        previous_moved = False
                        self._remove_empty_recovery_directory()
                except OSError as rollback_error:
                    raise BackupDBError(
                        "Backup DB promotion failed ({0}); rollback failed ({1}). "
                        "Original database remains at {2}".format(
                            promotion_error,
                            rollback_error,
                            self.previous_database,
                        )
                    ) from rollback_error
                raise
            if previous_moved:
                self._discard_previous_database()
            return True
        except BackupDBError:
            raise
        except Exception as error:
            raise BackupDBError("Backup DB installation failed: {0}".format(error)) from error
        finally:
            if isinstance(archive_source, (str, os.PathLike)):
                try:
                    os.remove(archive_source)
                except OSError:
                    pass
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
        if not self._matches_validation_signature():
            self._validate_and_record()
        data = _read_json(os.path.join(self.dbdir, filename))["data"]
        if not all(isinstance(item, dict) for item in data):
            raise BackupDBError("Backup DB data items must be JSON objects")
        return data
