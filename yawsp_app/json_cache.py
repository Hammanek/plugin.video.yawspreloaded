"""Small JSON mappings persisted with same-directory atomic replacement."""

from collections.abc import Mapping
import copy
import json
import logging
import os
import tempfile
import threading


_LOGGER = logging.getLogger(__name__)


class AtomicJsonCache:
    """Load and save a JSON object without destroying the last valid file.

    The last valid in-memory value is retained when a later load or save fails.
    I/O and serialization failures are reported through the injected logger and
    return an empty/previous mapping or ``False`` rather than escaping into UI.
    """

    def __init__(self, path, logger=None):
        self.path = os.path.abspath(os.fspath(path))
        self.logger = logger or _LOGGER
        self._data = {}
        self._lock = threading.RLock()

    def load(self):
        """Return the latest valid mapping, or the previous valid in-memory one."""
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as stream:
                    data = json.load(stream)
                if not isinstance(data, dict):
                    raise ValueError("JSON cache root must be an object")
            except FileNotFoundError:
                return copy.deepcopy(self._data)
            except (OSError, ValueError, TypeError):
                self.logger.exception("Unable to load JSON cache %s", self.path)
                return copy.deepcopy(self._data)

            self._data = data
            return copy.deepcopy(data)

    def save(self, mapping):
        """Atomically replace the cache with *mapping*, preserving it on failure."""
        with self._lock:
            if not isinstance(mapping, Mapping):
                self.logger.error("JSON cache value for %s must be a mapping", self.path)
                return False

            directory = os.path.dirname(self.path)
            basename = os.path.basename(self.path)
            descriptor = None
            temporary_path = None
            try:
                data = copy.deepcopy(dict(mapping))
                os.makedirs(directory, exist_ok=True)
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix="." + basename + ".", suffix=".tmp", dir=directory
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    descriptor = None
                    json.dump(data, stream, ensure_ascii=False, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.path)
                temporary_path = None
            except (OSError, ValueError, TypeError):
                self.logger.exception("Unable to save JSON cache %s", self.path)
                return False
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if temporary_path is not None:
                    try:
                        os.unlink(temporary_path)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        self.logger.exception(
                            "Unable to remove temporary JSON cache %s", temporary_path
                        )

            self._data = data
            return True

    def save_merged(self, updates, prefer_update=None):
        """Reload, merge only *updates*, and atomically save under one lock."""
        with self._lock:
            if not isinstance(updates, Mapping):
                self.logger.error("JSON cache value for %s must be a mapping", self.path)
                return False

            current = self.load()
            for key, candidate in updates.items():
                if (
                    key in current
                    and prefer_update is not None
                    and not prefer_update(candidate, current[key])
                ):
                    continue
                current[key] = candidate
            return self.save(current)
