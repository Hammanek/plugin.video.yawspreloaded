"""Strict current-file admission and atomic writes for public genre mappings."""

from collections.abc import Mapping
import json
import logging
import os
import re
import stat

from yawsp_app.json_cache import AtomicJsonCache


MISSING = "missing"
VALID = "valid"
UNSUPPORTED = "unsupported"

_LOGGER = logging.getLogger(__name__)
_READ_LIMIT = 128 * 1024
_MAX_NAME_LENGTH = 200
_POSITIVE_ID = re.compile(r"[1-9][0-9]*")
_SUPPORTED_GROUPS = frozenset(
    media + "/" + language
    for media in ("movie", "tv")
    for language in ("en-US", "cs-CZ", "sk-SK")
)
_READ_ERRORS = (
    OSError,
    UnicodeError,
    json.JSONDecodeError,
    TypeError,
    ValueError,
    OverflowError,
    RecursionError,
)


def _without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("Non-finite JSON number")


def _validated_group(mapping):
    if not isinstance(mapping, Mapping):
        return None
    try:
        items = mapping.items()
        validated = {}
        for identifier, name in items:
            if not isinstance(identifier, str) or not _POSITIVE_ID.fullmatch(identifier):
                return None
            if not isinstance(name, str) or not name.strip():
                return None
            if len(name) > _MAX_NAME_LENGTH:
                return None
            name.encode("utf-8", errors="strict")
            if identifier in validated:
                return None
            validated[identifier] = name
    except _READ_ERRORS:
        return None
    return validated


def _validated_document(document):
    if type(document) is not dict or set(document) != {"schema", "genres"}:
        return None
    if type(document["schema"]) is not int or document["schema"] != 1:
        return None
    if type(document["genres"]) is not dict:
        return None

    genres = {}
    for group, mapping in document["genres"].items():
        if group not in _SUPPORTED_GROUPS:
            return None
        validated = _validated_group(mapping)
        if validated is None:
            return None
        genres[group] = validated
    return {"schema": 1, "genres": genres}


def _serializable_document_fits(document):
    try:
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except _READ_ERRORS:
        return False
    return len(encoded) <= _READ_LIMIT


class GenreFileCache:
    """Read and sequentially commit one strict public genre sidecar."""

    def __init__(self, path, logger=None):
        self.path = os.path.abspath(os.fspath(path))
        self.logger = logger or _LOGGER

    def read(self):
        """Return current disk state and a detached sanitized document, if valid."""
        descriptor = None
        try:
            metadata = os.lstat(self.path)
            if not stat.S_ISREG(metadata.st_mode):
                return UNSUPPORTED, None

            flags = os.O_RDONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return UNSUPPORTED, None
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(_READ_LIMIT + 1)
        except FileNotFoundError:
            return MISSING, None
        except _READ_ERRORS:
            return UNSUPPORTED, None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        if len(raw) > _READ_LIMIT:
            return UNSUPPORTED, None
        try:
            parsed = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
            document = _validated_document(parsed)
        except _READ_ERRORS:
            return UNSUPPORTED, None
        if document is None:
            return UNSUPPORTED, None
        return VALID, document

    def get(self, media, language):
        """Return a detached supported group, or None when absent or unavailable."""
        if type(media) is not str or type(language) is not str:
            return None
        group = media + "/" + language
        if group not in _SUPPORTED_GROUPS:
            return None
        state, document = self.read()
        if state != VALID or group not in document["genres"]:
            return None
        return dict(document["genres"][group])

    def store(self, media, language, mapping):
        """Atomically commit one validated group while retaining other valid groups."""
        if type(media) is not str or type(language) is not str:
            self.logger.error("Invalid public genre cache group")
            return False
        group = media + "/" + language
        validated_group = _validated_group(mapping)
        if group not in _SUPPORTED_GROUPS or validated_group is None:
            self.logger.error("Invalid public genre cache group or mapping")
            return False

        atomic = AtomicJsonCache(self.path, logger=self.logger)
        with atomic.transaction():
            state, current = self.read()
            if state == UNSUPPORTED:
                self.logger.error("Public genre cache is unavailable for %s", group)
                return False
            if state == MISSING:
                current = {"schema": 1, "genres": {}}
            current["genres"][group] = validated_group
            if not _serializable_document_fits(current):
                self.logger.error("Public genre cache document is too large for %s", group)
                return False
            if not atomic.save(current):
                self.logger.error("Public genre cache commit failed for %s", group)
                return False
        return True
