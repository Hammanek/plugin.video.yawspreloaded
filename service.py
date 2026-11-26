"""Official native, single-writer TMDB Kodi service entry point."""

import os

from yawsp_app.genre_file_cache import GenreFileCache
from yawsp_app.json_cache import AtomicJsonCache
from yawsp_app.router import _LOGGER, _Settings
from yawsp_app.tmdb import GenreRepository
from yawsp_app.tmdb_service_store import ServiceStateStore
from yawsp_app.tmdb_service_worker import (
    ServiceMetadataEngine,
    ServiceOperationEngine,
    ServiceRunner,
    ServiceWatchlistEngine,
)
from yawsp_app.tmdb_ui import kodi_language
from yawsp_app.watchlist_snapshot import WatchlistSnapshotStore


def _profile_path(xbmc, addon):
    try:
        translate_path = xbmc.translatePath
    except AttributeError:  # Kodi versions that moved it to xbmcvfs
        import xbmcvfs

        translate_path = xbmcvfs.translatePath
    profile = translate_path(addon.getAddonInfo("profile"))
    if isinstance(profile, bytes):
        profile = profile.decode("utf-8")
    return os.path.normpath(profile)


def run(monitor=None):
    """Run the consumer on this native invocation, never on an application thread."""
    import xbmc
    import xbmcaddon

    addon = xbmcaddon.Addon()
    monitor = monitor or xbmc.Monitor()
    settings = _Settings(addon)
    profile = _profile_path(xbmc, addon)
    language = lambda: kodi_language(xbmc.getLanguage())
    trailer_cache = AtomicJsonCache(
        os.path.join(profile, "tmdb_trailers.json"), logger=_LOGGER
    )
    show_cache = AtomicJsonCache(
        os.path.join(profile, "tmdb_shows.json"), logger=_LOGGER
    )
    snapshots = WatchlistSnapshotStore(
        AtomicJsonCache(os.path.join(profile, "tmdb_watchlist.json"), logger=_LOGGER)
    )
    genres = GenreRepository(
        file_cache=GenreFileCache(
            os.path.join(profile, "tmdb_genres.json"), logger=_LOGGER
        )
    )
    store = ServiceStateStore(profile)
    abort = monitor.abortRequested
    runner = ServiceRunner(
        store=store,
        metadata_engine_factory=lambda session: ServiceMetadataEngine(
            settings,
            trailer_cache,
            show_cache,
            language,
            session,
            abort,
            _LOGGER,
        ),
        watchlist_engine_factory=lambda session: ServiceWatchlistEngine(
            settings,
            snapshots,
            language,
            store,
            session,
            abort,
            _LOGGER,
            genre_repository=genres,
        ),
        operation_engine_factory=lambda session: ServiceOperationEngine(
            settings,
            genres,
            language,
            store,
            session,
            abort,
            _LOGGER,
        ),
        abort=abort,
        wait_for_abort=monitor.waitForAbort,
        logger=_LOGGER,
    )
    try:
        runner.run_until_abort()
        return True
    except Exception:
        _LOGGER.error("Native TMDB service stopped after an operation failure", exc_info=True)
        return False
    finally:
        runner.close()


if __name__ == "__main__":
    run()
