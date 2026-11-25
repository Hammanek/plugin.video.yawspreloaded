"""Native serial TMDB writer; this module is Kodi's xbmc.service entry point."""

import logging
import os

import xbmc
import xbmcaddon
import xbmcvfs

from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.service_queue import ServiceJobQueue, ServiceRunner
from yawsp_app.tmdb_ui import kodi_language
from yawsp_app.watchlist_snapshot import WatchlistSnapshotStore


_ADDON_ID = "plugin.video.yawspreloaded"
_LOGGER = logging.getLogger(__name__)


class _Settings:
    def __init__(self, addon):
        self.addon = addon

    def get(self, key):
        return self.addon.getSetting(key)

    def set(self, key, value):
        self.addon.setSetting(key, str(value))


def _profile(addon):
    path = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    return os.path.normpath(path.decode("utf-8") if isinstance(path, bytes) else path)


def run():
    """Honor enablement and run no background Python worker of our own."""
    addon = xbmcaddon.Addon(_ADDON_ID)
    if not xbmc.getCondVisibility("System.HasAddon({0})".format(_ADDON_ID)):
        return False
    profile = _profile(addon)
    settings = _Settings(addon)
    runner = ServiceRunner(
        ServiceJobQueue(os.path.join(profile, "tmdb_service_queue")),
        settings,
        AtomicJsonCache(os.path.join(profile, "tmdb_trailers.json"), logger=_LOGGER),
        AtomicJsonCache(os.path.join(profile, "tmdb_shows.json"), logger=_LOGGER),
        WatchlistSnapshotStore(
            AtomicJsonCache(os.path.join(profile, "tmdb_watchlist.json"), logger=_LOGGER)
        ),
        lambda: kodi_language(xbmc.getLanguage()),
        abort=xbmc.Monitor().abortRequested,
        logger=_LOGGER,
    )
    monitor = xbmc.Monitor()
    runner.abort = monitor.abortRequested
    runner.run(monitor)
    return True


if __name__ == "__main__":
    run()
