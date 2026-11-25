"""Unified production router and exactly one Kodi completion adapter per call."""

import atexit
import logging
import os
import sys
import threading
import time

import requests

try:
    from urllib.parse import parse_qsl
except ImportError:  # pragma: no cover - retained for older Kodi interpreters
    from urlparse import parse_qsl

from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.runtime import (
    ActionRunner,
    CompletionKind,
    DirectoryResult,
    KodiRuntime,
    completion_for,
)
from yawsp_app.tmdb import (
    BackgroundPrefetch,
    GenreRepository,
    ShowRepository,
    TMDBClient,
    TrailerRepository,
)
from yawsp_app.tmdb_ui import build_tmdb_handlers, kodi_language
from yawsp_app.watchlist_snapshot import WatchlistSnapshotStore
from yawsp_app.webshare import WebshareClient
from yawsp_app.webshare_ui import build_webshare_handlers


_LOGGER = logging.getLogger(__name__)
_OWNER = None
_OWNER_LOCK = threading.Lock()


def dispatch(paramstring, handlers, runner):
    """Dispatch with the legacy query semantics, including unknown-action menus."""
    params = dict(parse_qsl(paramstring))
    action = params.get("action") if params else None
    selected = completion_for(action, params)
    handler = handlers.get(action) if action else None
    if handler is None:
        handler = handlers["menu"]

    if selected is CompletionKind.PLAYBACK:
        return runner.run_play(lambda: handler(params))
    if selected is CompletionKind.ACTION:
        return runner.run_action(lambda: handler(params))
    return runner.run_directory(lambda: handler(params))


class _Settings:
    """The small mutable settings protocol shared by both service clients."""

    def __init__(self, addon):
        self.addon = addon

    def get(self, key):
        return self.addon.getSetting(key)

    def set(self, key, value):
        self.addon.setSetting(key, str(value))


class _PluginAdapter:
    """Combine Kodi's plugin, GUI and executable entry points for KodiRuntime."""

    def __init__(self, xbmc, xbmcgui, xbmcplugin):
        self.xbmc = xbmc
        self.xbmcgui = xbmcgui
        self.xbmcplugin = xbmcplugin

    def addDirectoryItem(self, handle, url, listitem, isFolder=False):
        return self.xbmcplugin.addDirectoryItem(handle, url, listitem, isFolder)

    def endOfDirectory(self, handle, **kwargs):
        return self.xbmcplugin.endOfDirectory(handle, **kwargs)

    def setResolvedUrl(self, handle, succeeded, listitem):
        return self.xbmcplugin.setResolvedUrl(handle, succeeded, listitem)

    def setContent(self, handle, content):
        return self.xbmcplugin.setContent(handle, content)

    def setPluginCategory(self, handle, category):
        return self.xbmcplugin.setPluginCategory(handle, category)

    def addSortMethod(self, handle, sort_method):
        return self.xbmcplugin.addSortMethod(handle, sort_method)

    def executeBuiltin(self, command):
        return self.xbmc.executebuiltin(command)

    def notification(self, heading, message, icon=None, time=None, sound=None):
        dialog = self.xbmcgui.Dialog()
        icon = self.xbmcgui.NOTIFICATION_INFO if icon is None else icon
        time = 3000 if time is None else time
        return dialog.notification(heading, message, icon, time, sound=bool(sound))


class _ProductionOwner:
    """Own the default executor/session for the add-on process, not one route."""

    def __init__(self):
        # These imports are deliberately lazy: importing the compatibility
        # facade in tests or documentation tooling does not require Kodi.
        import xbmc
        import xbmcaddon
        import xbmcgui
        import xbmcplugin

        self.xbmc = xbmc
        self.xbmcgui = xbmcgui
        self.addon = xbmcaddon.Addon()
        self.settings = _Settings(self.addon)
        self.plugin = _PluginAdapter(xbmc, xbmcgui, xbmcplugin)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) "
                    "Gecko/20100101 Firefox/137.0"
                ),
                "Referer": "https://webshare.cz",
            }
        )
        try:
            translate_path = xbmc.translatePath
        except AttributeError:  # Kodi versions that moved it to xbmcvfs
            import xbmcvfs

            translate_path = xbmcvfs.translatePath
        profile = translate_path(self.addon.getAddonInfo("profile"))
        if isinstance(profile, bytes):
            profile = profile.decode("utf-8")
        self.profile = os.path.normpath(profile)
        self.monitor = xbmc.Monitor()
        self.language = lambda: kodi_language(xbmc.getLanguage())
        self._runtime_options = {
            "plugin": self.plugin,
            "addon": self.addon,
            "session": self.session,
            "profile": self.profile,
            "listitem_factory": xbmcgui.ListItem,
            "keyboard_factory": xbmcgui.Keyboard,
            "textviewer": xbmcgui.Dialog().textviewer,
            "logger": _LOGGER,
            "label_sort_method": xbmcplugin.SORT_METHOD_LABEL,
            "episode_sort_method": xbmcplugin.SORT_METHOD_EPISODE,
            "clock": time.time,
            "monitor_factory": xbmc.Monitor,
            "current_window_id": xbmcgui.getCurrentWindowId,
            "addon_available": lambda addon_id: bool(
                xbmc.getCondVisibility("System.HasAddon({0})".format(addon_id))
            ),
            "skin_dir": xbmc.getSkinDir,
        }
        # Retain these compatibility attributes for callers that inspect the
        # owner; routing itself always builds fresh per-invocation state below.
        self.runtime = self._new_runtime()
        self.trailer_cache = AtomicJsonCache(
            os.path.join(self.profile, "tmdb_trailers.json"), logger=_LOGGER
        )
        self.show_cache = AtomicJsonCache(
            os.path.join(self.profile, "tmdb_shows.json"), logger=_LOGGER
        )
        self.snapshot_store = WatchlistSnapshotStore(
            AtomicJsonCache(
                os.path.join(self.profile, "tmdb_watchlist.json"), logger=_LOGGER
            )
        )
        self.genre_repository = GenreRepository()
        self.background = BackgroundPrefetch(
            settings=self.settings,
            trailer_cache=self.trailer_cache,
            show_cache=self.show_cache,
            locale_mapper=self.language,
            session_factory=requests.Session,
            abort=self.monitor.abortRequested,
            logger=_LOGGER,
            snapshot_store=self.snapshot_store,
            genre_repository=self.genre_repository,
        )
        self.runner = ActionRunner(
            self.plugin,
            self.runtime.handle,
            xbmcgui.ListItem,
            logger=_LOGGER,
            error_handler=self._handler_error,
        )
        self._closed = False

    def _new_runtime(self):
        return KodiRuntime(
            base_url=sys.argv[0],
            handle=int(sys.argv[1]),
            **self._runtime_options
        )

    def invocation(self):
        """Build isolated URL/handle state while retaining process-wide services."""
        runtime = self._new_runtime()
        runner = ActionRunner(
            self.plugin,
            runtime.handle,
            self._runtime_options["listitem_factory"],
            logger=_LOGGER,
            error_handler=lambda error: self._handler_error(error, runtime),
        )
        return self.handlers(runtime), runner

    def _handler_error(self, error, runtime=None):
        message = self.addon.getLocalizedString(30102) or "Request failed"
        (runtime or self.runtime).notify(message)

    def handlers(self, runtime=None):
        """Create per-open repositories so persisted worker data is re-read."""
        runtime = runtime or self.runtime
        webshare_client = WebshareClient(self.session, self.settings)
        tmdb_client = TMDBClient(
            self.session,
            self.settings,
            locale_mapper=self.language,
            abort=self.monitor.abortRequested,
            logger=_LOGGER,
        )
        webshare = build_webshare_handlers(runtime, lambda: webshare_client)
        tmdb = build_tmdb_handlers(
            runtime,
            lambda: tmdb_client,
            {
                "trailers": TrailerRepository(tmdb_client, self.trailer_cache, logger=_LOGGER),
                "shows": ShowRepository(tmdb_client, self.show_cache, logger=_LOGGER),
            },
            self.background,
        )
        base_menu = webshare["menu"]
        tmdb_menu = tmdb.pop("menu")

        def menu(params):
            webshare_result = base_menu(params)
            tmdb_result = tmdb_menu(params)
            return DirectoryResult(
                succeeded=webshare_result.succeeded and tmdb_result.succeeded,
                update_listing=(
                    webshare_result.update_listing or tmdb_result.update_listing
                ),
                cache_to_disc=(
                    webshare_result.cache_to_disc and tmdb_result.cache_to_disc
                ),
            )

        webshare.update(tmdb)
        webshare["menu"] = menu
        return webshare

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.background.close(wait=False)
        try:
            self.session.close()
        except Exception:
            _LOGGER.exception("Unable to close the add-on HTTP session")


def _get_owner():
    global _OWNER
    if _OWNER is None:
        with _OWNER_LOCK:
            if _OWNER is None:
                _OWNER = _ProductionOwner()
    return _OWNER


def close():
    """Close the one process owner; a later invocation may create a fresh one."""
    global _OWNER
    with _OWNER_LOCK:
        owner, _OWNER = _OWNER, None
    if owner is not None:
        owner.close()


def router(paramstring):
    """Route one invocation through the unified handlers and completion runner."""
    owner = _get_owner()
    invocation = getattr(owner, "invocation", None)
    if invocation is not None:
        handlers, runner = invocation()
        return dispatch(paramstring, handlers, runner)
    return dispatch(paramstring, owner.handlers(), owner.runner)


atexit.register(close)
