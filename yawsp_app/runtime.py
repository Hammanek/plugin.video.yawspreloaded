"""Injected Kodi presentation adapters and single-completion action runners."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
import logging
import os
import time
from typing import Optional


_LOGGER = logging.getLogger(__name__)


class CompletionKind(Enum):
    DIRECTORY = "directory"
    PLAYBACK = "playback"
    ACTION = "action"


@dataclass(frozen=True)
class DirectoryResult:
    succeeded: bool = True
    update_listing: bool = False
    cache_to_disc: bool = True
    post_complete: object = None


@dataclass(frozen=True)
class PlaybackResult:
    succeeded: bool
    list_item: object


@dataclass(frozen=True)
class ActionResult:
    handled: bool = True
    refresh: bool = False
    play_media: Optional[str] = None


def completion_for(action, params):
    """Select completion from both the route and its dynamic parameters."""
    params = params or {}
    if action == "play":
        return CompletionKind.PLAYBACK
    if action in ("settings", "download", "info", "tmdb_auth", "tmdb_trailer"):
        return CompletionKind.ACTION
    if action == "tmdb_watchlist" and params.get("watched"):
        return CompletionKind.ACTION
    return CompletionKind.DIRECTORY


class ActionRunner:
    """Run handlers that never finalize Kodi themselves.

    Every invocation converts exceptions and invalid handler returns into an
    explicit failure result, while the corresponding Kodi completion is made
    exactly once in ``finally``.
    """

    def __init__(
        self,
        plugin,
        handle,
        listitem_factory,
        logger=None,
        error_handler=None,
    ):
        self.plugin = plugin
        self.handle = handle
        self.listitem_factory = listitem_factory
        self.logger = logger or _LOGGER
        self.error_handler = error_handler

    def _report(self, kind, error):
        self.logger.exception("Exception in %s handler", kind)
        if self.error_handler is not None:
            self.error_handler(error)

    def run_directory(self, handler):
        result = None
        try:
            result = handler()
            if not isinstance(result, DirectoryResult):
                raise TypeError("Directory handlers must return DirectoryResult")
        except Exception as error:
            self._report("directory", error)
            result = DirectoryResult(succeeded=False)
        finally:
            completed = result if isinstance(result, DirectoryResult) else DirectoryResult(False)
            self.plugin.endOfDirectory(
                self.handle,
                succeeded=completed.succeeded,
                updateListing=completed.update_listing,
                cacheToDisc=completed.cache_to_disc,
            )
        if result.succeeded and result.post_complete is not None:
            try:
                result.post_complete()
            except Exception as error:
                self._report("directory post-completion", error)
        return result

    def run_play(self, handler):
        result = None
        try:
            result = handler()
            if not isinstance(result, PlaybackResult):
                raise TypeError("Playback handlers must return PlaybackResult")
            if result.succeeded and result.list_item is None:
                raise TypeError("Successful playback requires a list item")
        except Exception as error:
            self._report("playback", error)
            result = PlaybackResult(False, self.listitem_factory())
        finally:
            completed = (
                result
                if isinstance(result, PlaybackResult)
                else PlaybackResult(False, self.listitem_factory())
            )
            succeeded = completed.succeeded and completed.list_item is not None
            item = completed.list_item or self.listitem_factory()
            self.plugin.setResolvedUrl(self.handle, succeeded, item)
        return result

    def run_action(self, handler):
        result = None
        try:
            result = handler()
            if not isinstance(result, ActionResult):
                raise TypeError("Action handlers must return ActionResult")
        except Exception as error:
            self._report("action", error)
            result = ActionResult(handled=False)
        finally:
            self.plugin.setResolvedUrl(self.handle, False, self.listitem_factory())

        if result.handled and result.refresh:
            self.plugin.executeBuiltin("Container.Refresh")
        if result.handled and result.play_media:
            self.plugin.executeBuiltin(result.play_media)
        return result


class LocalFileOps:
    """Local filesystem implementation matching the download service boundary."""

    def open_write(self, path):
        return open(path, "wb")

    def replace(self, source, destination):
        return os.replace(source, destination)

    def remove(self, path):
        return os.remove(path)

    def exists(self, path):
        return os.path.exists(path)


def is_vfs_path(path):
    """Identify Kodi VFS URLs without mistaking Windows drive paths for URLs."""
    path = os.fspath(path)
    return "://" in path and not (len(path) >= 2 and path[1] == ":")


class _VFSOutput:
    """Adapt Kodi's boolean File.write result to checked byte-progress writes."""

    def __init__(self, output):
        self.output = output

    def write(self, data):
        result = self.output.write(data)
        if result is True:
            return len(data)
        if result is False:
            return 0
        return result

    def close(self):
        return self.output.close()


class KodiVfsFileOps:
    """Kodi VFS implementation; false writes/renames are never false success."""

    def __init__(self, vfs=None):
        if vfs is None:
            import xbmcvfs

            vfs = xbmcvfs
        self.vfs = vfs

    def open_write(self, path):
        return _VFSOutput(self.vfs.File(path, "w"))

    def replace(self, source, destination):
        if not self.vfs.rename(source, destination):
            raise OSError("Kodi VFS could not rename download into place")

    def remove(self, path):
        if not self.vfs.delete(path):
            raise OSError("Kodi VFS could not remove partial download")

    def exists(self, path):
        return self.vfs.exists(path)


class KodiRuntime:
    """Small injected facade over Kodi, settings, HTTP and filesystem services."""

    def __init__(
        self,
        plugin,
        addon,
        session,
        profile,
        base_url,
        handle,
        listitem_factory,
        keyboard_factory,
        textviewer,
        file_ops=None,
        vfs_file_ops=None,
        logger=None,
        label_sort_method=1,
        episode_sort_method=23,
        today=None,
        clock=time.time,
        monitor_factory=None,
        current_window_id=None,
        addon_available=None,
        skin_dir=None,
        qr_generator=None,
    ):
        self.plugin = plugin
        self.addon = addon
        self.session = session
        self.profile = profile
        self.base_url = base_url
        self.handle = handle
        self.listitem_factory = listitem_factory
        self.keyboard_factory = keyboard_factory
        self.textviewer = textviewer
        self._injected_file_ops = file_ops
        self.file_ops = file_ops or LocalFileOps()
        self.vfs_file_ops = vfs_file_ops
        self.logger = logger or _LOGGER
        self.label_sort_method = label_sort_method
        self.episode_sort_method = episode_sort_method
        self.today = today or date.today
        self.clock = clock
        self.monitor_factory = monitor_factory
        self.current_window_id = current_window_id or (lambda: None)
        self.addon_available = addon_available or (lambda addon_id: False)
        self.skin_dir = skin_dir or (lambda: "")
        self.qr_generator = qr_generator

    def get_setting(self, key):
        return self.addon.getSetting(key)

    def set_setting(self, key, value):
        self.addon.setSetting(key, value)

    def file_ops_for(self, path):
        if self._injected_file_ops is not None or not is_vfs_path(path):
            return self.file_ops
        if self.vfs_file_ops is None:
            self.vfs_file_ops = KodiVfsFileOps()
        return self.vfs_file_ops

    def localized(self, string_id):
        return self.addon.getLocalizedString(string_id)

    def addon_info(self, key):
        return self.addon.getAddonInfo(key)

    def notify(self, message):
        self.plugin.notification(self.addon_info("name"), message)

    def ask(self, default, heading):
        keyboard = self.keyboard_factory(default, heading)
        keyboard.doModal()
        return keyboard.getText() if keyboard.isConfirmed() else None

    def show_text(self, heading, text):
        self.textviewer(heading, text)

    def list_item(self, label="", path=""):
        return self.listitem_factory(label=label, path=path)

    def add_item(self, url, list_item, is_folder=False):
        self.plugin.addDirectoryItem(self.handle, url, list_item, is_folder)

    def set_category(self, category):
        self.plugin.setPluginCategory(self.handle, category)

    def sort_by_label(self):
        self.plugin.addSortMethod(self.handle, self.label_sort_method)

    def sort_by_episode(self):
        self.plugin.addSortMethod(self.handle, self.episode_sort_method)

    def set_content(self, content):
        self.plugin.setContent(self.handle, content)

    def execute_builtin(self, command):
        self.plugin.executeBuiltin(command)

    def open_settings(self):
        self.addon.openSettings()
