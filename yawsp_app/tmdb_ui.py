"""TMDB Kodi presentation; completion remains owned by the router adapters."""

from datetime import date
import hashlib
import io
import math
import os
import struct
import zlib

try:
    from urllib.parse import urlencode
except ImportError:  # pragma: no cover - retained for older Kodi interpreters
    from urllib import urlencode

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app.discovery_ui import build_discovery_handlers
from yawsp_app.metadata import apply_video_metadata
from yawsp_app.runtime import ActionResult, DirectoryResult
from yawsp_app.search import czech_sort_key
from yawsp_app.tmdb import (
    GenreRepository,
    ShowRepository,
    TMDBError,
    TMDBServiceUnavailableError,
    TrailerRepository,
    WATCHLIST_PREFETCH_LIMIT,
    _read_response_json,
)
from yawsp_app.tmdb_service_store import IdentityTransactionAdapter
from yawsp_app.tmdb_youtube import valid_youtube_id, youtube_play_media
from yawsp_app.watchlist_snapshot import SnapshotScope


_IMAGE_URL = "https://image.tmdb.org/t/p/"
_WINDOW_PICTURES = 12007
_AUTH_TIMEOUT = 300
_AUTH_POLL_INTERVAL = 4


def kodi_language(language):
    """Map Kodi's current language to the TMDB locale without OS locale state."""
    normalized = (language or "").strip().casefold().replace("_", "-")
    if normalized.startswith("cs") or normalized.startswith("czech"):
        return "cs-CZ"
    if normalized.startswith("sk") or normalized.startswith("slovak"):
        return "sk-SK"
    return "en-US"


def _url(runtime, **params):
    return "{0}?{1}".format(runtime.base_url, urlencode(params))


def _cached_trailers(repository, media, media_ids):
    """Read cache once for immediate values and the matching freshness misses."""
    reader = TrailerRepository(
        None, repository.cache, clock=repository.clock, _read_only=True
    )
    values = {}
    missing = []
    with reader._lock:
        reader._load()
        for media_id in media_ids:
            try:
                key = reader._key(media, media_id)
            except (TypeError, ValueError):
                continue
            entry = reader._entry(key)
            if entry is not None and entry.get("ts") is None:
                # A nonempty legacy ID remains displayable, but only the service
                # may validate it and persist the migration timestamp.
                if entry["youtube_id"] is not None:
                    values[int(media_id)] = entry["youtube_id"]
                missing.append(int(media_id))
            elif reader._fresh(entry):
                values[int(media_id)] = entry["youtube_id"]
            else:
                missing.append(int(media_id))
    return values, missing


def _cached_shows(repository, show_ids):
    """Read displayable fresh/stale values and freshness misses in one pass."""
    reader = ShowRepository(
        None, repository.cache, clock=repository.clock, _read_only=True
    )
    values = {}
    missing = []
    with reader._lock:
        reader._load()
        for show_id in show_ids:
            try:
                key = reader._key(show_id)
            except (TypeError, ValueError):
                continue
            entry = reader._entry(key)
            if entry is not None:
                values[int(show_id)] = entry["next"]
            if not reader._fresh(entry):
                missing.append(int(show_id))
    return values, missing


def _next_episode_suffix(next_episode):
    if not isinstance(next_episode, dict) or not next_episode:
        return ""
    try:
        season = int(next_episode.get("season") or 0)
        episode = int(next_episode.get("episode") or 0)
    except (TypeError, ValueError):
        return ""
    suffix = " [COLOR gray]→ S{0:02d}E{1:02d}".format(season, episode)
    air_date = next_episode.get("air_date")
    if isinstance(air_date, str) and air_date:
        try:
            year, month, day = (int(part) for part in air_date.split("-"))
            suffix += " ({2}. {1}. {0})".format(year, month, day)
        except (TypeError, ValueError):
            pass
    return suffix + "[/COLOR]"


def _qr_png(profile, url):
    """Render a QR PNG using the bundled qrcodegen, returning no UI result."""
    import qrcodegen

    qr = qrcodegen.QrCode.encode_text(url, qrcodegen.QrCode.Ecc.MEDIUM)
    scale = 8
    border = 4
    size = qr.get_size() + 2 * border
    pixels = size * scale
    rows = [bytearray(b"\xff" * pixels * 3) for _ in range(pixels)]
    for module_y in range(qr.get_size()):
        for module_x in range(qr.get_size()):
            if not qr.get_module(module_x, module_y):
                continue
            start_y = (module_y + border) * scale
            start_x = (module_x + border) * scale
            for offset_y in range(scale):
                row = rows[start_y + offset_y]
                base = start_x * 3
                for offset_x in range(scale):
                    row[base + offset_x * 3 : base + offset_x * 3 + 3] = b"\x00\x00\x00"

    def chunk(tag, data):
        raw = tag + data
        return (
            struct.pack(">I", len(data))
            + raw
            + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", pixels, pixels, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    os.makedirs(profile, exist_ok=True)
    path = os.path.join(profile, "tmdb_qr.png")
    with io.open(path, "wb") as output:
        output.write(png)
    return path


def _make_qr(runtime, url):
    generator = getattr(runtime, "qr_generator", None)
    try:
        return generator(url) if generator is not None else _qr_png(runtime.profile, url)
    except Exception:
        runtime.logger.exception("TMDB QR generation failed")
        return None


def _pending_session_id(client, request_token):
    """Poll session creation while treating TMDB's pending statuses as a result."""
    try:
        response = client.request(
            "POST",
            "/3/authentication/session/new",
            json_data={"request_token": request_token},
            invalidate_on_unauthorized=False,
        )
    except TMDBError as error:
        cause = error.__cause__
        response = getattr(cause, "response", None)
        if response is not None and response.status_code in (400, 401, 404):
            response.close()
            return None
        raise

    try:
        data = _read_response_json(
            response, "TMDB session response is malformed", abort=getattr(client, "abort", None)
        )
        if not isinstance(data, dict):
            raise TMDBError("TMDB session response must be an object")
        session_id = data.get("session_id")
        return session_id if isinstance(session_id, str) and session_id.strip() else None
    except (TypeError, ValueError) as error:
        raise TMDBError("TMDB session response is malformed") from error
    finally:
        response.close()


def _detail_text(row, field):
    value = row.get(field)
    if value is not None and not isinstance(value, str):
        raise TMDBError("TMDB detail response has an invalid " + field)
    return value


def _detail_number(value, field, integer=False, optional=False):
    if optional and (value is None or value == ""):
        return value
    if isinstance(value, bool):
        raise TMDBError("TMDB detail response has an invalid " + field)
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise TMDBError("TMDB detail response has an invalid " + field)
        return value
    if isinstance(value, str):
        try:
            converted = int(value) if integer else float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise TMDBError("TMDB detail response has an invalid " + field) from error
        if not integer and not math.isfinite(converted):
            raise TMDBError("TMDB detail response has an invalid " + field)
        return value
    raise TMDBError("TMDB detail response has an invalid " + field)


def _season_values(season):
    if not isinstance(season, dict):
        raise TMDBError("TMDB season response contains a non-object row")
    return {
        "season_number": _detail_number(
            season.get("season_number", 0), "season_number"
        ),
        "episode_count": _detail_number(
            season.get("episode_count", 0), "episode_count"
        ),
        "poster_path": _detail_text(season, "poster_path"),
    }


def map_seasons_response(show):
    """Extract detached season values without Kodi objects or navigation URLs."""
    if not isinstance(show, dict):
        raise TMDBError("TMDB season response must be an object")
    values = show.get("seasons", [])
    if not isinstance(values, list):
        raise TMDBError("TMDB season response has no seasons array")
    name = _detail_text(show, "name")
    original_name = _detail_text(show, "original_name")
    title = name or original_name or ""
    return {
        "title": title,
        "original_title": original_name or title,
        "seasons": [_season_values(season) for season in values],
    }


def _episode_values(episode):
    if not isinstance(episode, dict):
        raise TMDBError("TMDB episode response contains a non-object row")
    return {
        "episode_number": _detail_number(
            episode.get("episode_number"), "episode_number", integer=True
        ),
        "name": _detail_text(episode, "name"),
        "air_date": _detail_text(episode, "air_date"),
        "overview": _detail_text(episode, "overview"),
        "vote_average": _detail_number(
            episode.get("vote_average"), "vote_average", optional=True
        ),
        "runtime": _detail_number(
            episode.get("runtime"), "runtime", integer=True, optional=True
        ),
        "still_path": _detail_text(episode, "still_path"),
    }


def map_episodes_response(data):
    """Extract detached native episode inputs while retaining UI formatting."""
    if not isinstance(data, dict):
        raise TMDBError("TMDB episode response must be an object")
    values = data.get("episodes", [])
    if not isinstance(values, list):
        raise TMDBError("TMDB episode response has no episodes array")
    return {"episodes": [_episode_values(episode) for episode in values]}


def build_tmdb_handlers(runtime, client_factory, repositories, background):
    """Build all TMDB actions without performing per-item synchronous lookups."""
    client = client_factory()
    trailer_repository = repositories["trailers"]
    show_repository = repositories["shows"]
    attach_repositories = getattr(background, "attach_repositories", None)
    if attach_repositories is not None:
        attach_repositories(trailer_repository, show_repository)
    snapshot_store = getattr(background, "snapshot_store", None)
    identity_owner = getattr(background, "identity_lock", None)
    if identity_owner is None:
        identity_owner = getattr(background, "_lock", None)
    identity_lock = IdentityTransactionAdapter(identity_owner)
    genre_repository = getattr(background, "genre_repository", None) or GenreRepository()

    def genres(media, language):
        """Render strict cache hits only; refresh completion owns genre writes."""
        return genre_repository.get_cached(media, language)

    def language():
        mapper = getattr(client, "locale_mapper", None)
        return mapper() if mapper is not None else "en-US"

    def session_binding(session_id=None):
        if session_id is None:
            session_id = runtime.get_setting("tmdb_access_token").strip()
        return "v1:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def valid_configuration():
        if not runtime.get_setting("tmdb_api_key").strip():
            runtime.notify(runtime.localized(30400))
            runtime.open_settings()
            return False
        return True

    def report_listing_error(error):
        runtime.logger.exception("TMDB listing failed: %s", error)
        if not runtime.get_setting("tmdb_access_token").strip():
            runtime.notify(runtime.localized(30401))
            runtime.open_settings()
        else:
            runtime.notify(runtime.localized(30402))

    def set_default_view(params):
        if params.get("category") not in ("movies", "shows"):
            return
        if runtime.skin_dir() != "skin.estuary":
            return
        if runtime.get_setting("tmdb_view_defaulted") == "true":
            return
        runtime.execute_builtin("Container.SetViewMode(54)")
        runtime.set_setting("tmdb_view_defaulted", "true")

    class DiscoveryClient:
        """Consume detached public rows produced by discovery.catalog."""

        locale_mapper = staticmethod(getattr(client, "locale_mapper", None))

        @staticmethod
        def load_catalog(source_name):
            value = background.run_operation(
                protocol.catalog_request(source_name, language())
            )
            if set(value) != {"rows"} or not isinstance(value["rows"], list):
                raise TMDBError("TMDB discovery service result is malformed")
            return value["rows"]

    discovery_handlers = build_discovery_handlers(
        runtime,
        DiscoveryClient(),
        _url,
        genres,
        set_default_view,
        valid_configuration,
    )

    def authenticate(params=None):
        if not valid_configuration():
            return ActionResult(handled=False)

        viewer_active = False
        viewer_seen = False
        try:
            token_data = client.get("/3/authentication/token/new")
            request_token = token_data.get("request_token")
            if not isinstance(request_token, str) or not request_token.strip():
                raise TMDBError("TMDB authentication response has no request token")
            request_token = request_token.strip()
            url = "https://www.themoviedb.org/authenticate/" + request_token

            qr_path = _make_qr(runtime, url)
            if qr_path:
                viewer_active = True
                runtime.execute_builtin("ShowPicture({0})".format(qr_path))
                runtime.notify(runtime.localized(30403))
            else:
                runtime.notify(runtime.localized(30404).format(url))

            monitor = runtime.monitor_factory()
            deadline = runtime.clock() + _AUTH_TIMEOUT
            while runtime.clock() < deadline:
                if viewer_active:
                    window_id = runtime.current_window_id()
                    if window_id == _WINDOW_PICTURES:
                        viewer_seen = True
                    elif viewer_seen:
                        runtime.logger.info("TMDB QR viewer closed by user")
                        return ActionResult(handled=False)

                session_id = _pending_session_id(client, request_token)
                if session_id:
                    with identity_lock:
                        old_session = runtime.get_setting("tmdb_access_token").strip()
                        old_account_id = runtime.get_setting("tmdb_account_id").strip()
                    if old_account_id:
                        # The only snapshot invalidator waits outside every lock;
                        # its request is valid only for the captured old session.
                        background.run_operation(
                            protocol.invalidate_identity_request(
                                old_account_id, session_binding(old_session)
                            )
                        )
                    with identity_lock:
                        # Another QR/settings writer may have advanced identity
                        # while invalidation waited. Never overwrite that winner.
                        if (
                            runtime.get_setting("tmdb_access_token").strip()
                            != old_session
                            or runtime.get_setting("tmdb_account_id").strip()
                            != old_account_id
                        ):
                            raise TMDBError("TMDB identity changed during authorization")
                        runtime.set_setting("tmdb_account_id", "")
                        runtime.set_setting("tmdb_access_token", session_id)
                    runtime.notify(runtime.localized(30405))
                    return ActionResult(handled=True)
                if monitor.waitForAbort(_AUTH_POLL_INTERVAL):
                    return ActionResult(handled=False)

            runtime.notify(runtime.localized(30406))
            return ActionResult(handled=False)
        except Exception as error:
            runtime.logger.exception("TMDB authentication failed")
            runtime.notify(runtime.localized(30407).format(error))
            return ActionResult(handled=False)
        finally:
            if viewer_active:
                try:
                    if runtime.current_window_id() == _WINDOW_PICTURES:
                        runtime.execute_builtin("Action(Back)")
                except Exception:
                    runtime.logger.exception("Unable to close the TMDB QR viewer")

    def list_seasons(params):
        show_id = params["show_id"]
        value = background.run_operation(
            protocol.seasons_request(show_id, language(), session_binding())
        )
        show = map_seasons_response(
            {
                "name": value.get("title"),
                "original_name": value.get("original_title"),
                "seasons": value.get("seasons"),
            }
        )
        title = show["title"] or runtime.localized(30408)
        original_title = show["original_title"] or title
        runtime.set_category(title)
        for season in sorted(
            show["seasons"], key=lambda value: value.get("season_number", 0)
        ):
            season_number = season.get("season_number", 0)
            episode_count = season.get("episode_count", 0)
            item = runtime.list_item(
                label=runtime.localized(30409).format(season_number, episode_count)
            )
            artwork = {"icon": "DefaultSeason.png"}
            if season.get("poster_path"):
                artwork["poster"] = _IMAGE_URL + "w500" + season["poster_path"]
            item.setArt(artwork)
            apply_video_metadata(
                item,
                {
                    "title": runtime.localized(30410).format(season_number),
                    "mediatype": "season",
                    "season": season_number,
                    "episode": episode_count,
                },
            )
            runtime.add_item(
                _url(
                    runtime,
                    action="tmdb_watchlist",
                    show_id=show_id,
                    season=season_number,
                    series_title=original_title,
                    category="shows",
                ),
                item,
                True,
            )
        runtime.set_content("seasons")
        return DirectoryResult()

    def list_episodes(params):
        show_id = params["show_id"]
        season_number = int(params["season"])
        series_title = params.get("series_title") or runtime.localized(30408)
        runtime.set_category(
            "{0} / {1}".format(
                runtime.addon_info("name"), runtime.localized(30410).format(season_number)
            )
        )
        value = background.run_operation(
            protocol.episodes_request(
                show_id, season_number, language(), session_binding()
            )
        )
        data = map_episodes_response(value)
        today = runtime.today()
        for episode in data["episodes"]:
            episode_number = int(episode.get("episode_number"))
            title = episode.get("name") or runtime.localized(30408)
            air_date = episode.get("air_date")
            label = title
            if air_date:
                try:
                    year, month, day = (int(part) for part in air_date.split("-"))
                    formatted = " ({2:02d}.{1:02d}.{0})".format(year, month, day)
                    color = "red" if date(year, month, day) > today else "gray"
                    label += " [COLOR {0}]{1}[/COLOR]".format(color, formatted)
                except (TypeError, ValueError):
                    runtime.logger.exception("Invalid TMDB episode air date")

            item = runtime.list_item(label=label)
            if episode.get("still_path"):
                item.setArt({"thumb": _IMAGE_URL + "w300" + episode["still_path"]})
            info = {
                "title": label,
                "mediatype": "episode",
                "plot": episode.get("overview") or "",
                "season": season_number,
                "episode": episode_number,
                "duration": int(episode.get("runtime") or 0) * 60,
                "rating": float(episode.get("vote_average") or 0),
            }
            if air_date:
                info["aired"] = air_date
            apply_video_metadata(item, info)
            runtime.add_item(
                _url(
                    runtime,
                    action="search",
                    what="{0} S{1:02d}E{2:02d}".format(
                        series_title, season_number, episode_number
                    ),
                ),
                item,
                True,
            )
        runtime.set_content("episodes")
        runtime.sort_by_episode()
        return DirectoryResult()

    def watched(params):
        if not runtime.get_setting("tmdb_access_token").strip():
            runtime.notify(runtime.localized(30411))
            runtime.open_settings()
            return ActionResult(handled=False)
        try:
            session_id = runtime.get_setting("tmdb_access_token").strip()
            operation_language = language()
            account_id = runtime.get_setting("tmdb_account_id").strip()
            watchlist_media = (
                "movies" if params.get("category") == "movies" else "tv"
            )
            scope = (
                SnapshotScope(account_id, watchlist_media, operation_language)
                if account_id
                else {
                    "account_id": "",
                    "media": watchlist_media,
                    "language": operation_language,
                    "schema": 1,
                }
            )
            background.run_operation(
                protocol.watched_request(
                    scope, params["watched"], session_binding(session_id)
                )
            )
            runtime.notify(runtime.localized(30413))
            return ActionResult(handled=True, refresh=True)
        except Exception:
            runtime.logger.exception("TMDB watched action failed")
            runtime.notify(runtime.localized(30412))
            return ActionResult(handled=False)

    def list_watchlist(params):
        category = params.get("category")
        manual_refresh = params.get("refresh") == "1"
        is_movie = category == "movies"
        media = "movie" if is_movie else "tv"
        watchlist_media = "movies" if is_movie else "tv"
        try:
            try:
                sort_mode = int(runtime.get_setting("tmdb_sort") or 0)
            except (TypeError, ValueError):
                sort_mode = 0
            snapshot = None
            refresh_state = None
            scope = None
            operation_session = runtime.get_setting("tmdb_access_token").strip()
            operation_language = language()
            account_id = runtime.get_setting("tmdb_account_id").strip()
            account_was_missing = not account_id
            if snapshot_store is not None and account_id:
                scope = SnapshotScope(account_id, watchlist_media, operation_language)
                refresh_state = snapshot_store.refresh_state(scope, runtime.clock())
                snapshot = refresh_state.snapshot
            # Missing accounts use an unresolved watchlist.refresh scope. The
            # service performs /account, then returns the concrete commit scope;
            # no account or list request is ever issued by this parent session.

            def operation_valid():
                return (
                    runtime.get_setting("tmdb_access_token").strip()
                    == operation_session
                    and (
                        runtime.get_setting("tmdb_account_id").strip() == account_id
                        or (
                            account_was_missing
                            and not runtime.get_setting("tmdb_account_id").strip()
                        )
                    )
                    and language() == operation_language
                )

            def require_valid_operation():
                if not operation_valid():
                    raise TMDBError("TMDB identity changed during watchlist operation")

            def cached_fallback_valid():
                invalidated = (
                    not runtime.get_setting("tmdb_access_token").strip()
                    and not runtime.get_setting("tmdb_account_id").strip()
                )
                return language() == operation_language and (
                    operation_valid() or invalidated
                )

            require_valid_operation()
            listing_error = None
            cached_render = snapshot is not None and not manual_refresh
            warm_open = cached_render
            if cached_render:
                results = list(snapshot.results)
            else:
                if snapshot_store is None:
                    raise TMDBServiceUnavailableError(
                        "TMDB watchlist snapshot reader is unavailable"
                    )
                refresh_error = None
                requested_scope = scope or {
                    "account_id": "",
                    "media": watchlist_media,
                    "language": operation_language,
                    "schema": 1,
                }
                try:
                    value = background.run_watchlist(
                        protocol.watchlist_request(
                            requested_scope,
                            refresh_state.generation if refresh_state else 0,
                            manual_refresh,
                            account_was_missing,
                            session_binding(operation_session),
                        )
                    )
                    scope = SnapshotScope(**value["scope"])
                    account_id = scope.account_id
                    require_valid_operation()
                    committed = snapshot_store.load(scope)
                    if committed is None or committed.revision != value["revision"]:
                        raise TMDBError(
                            "TMDB watchlist service revision did not commit"
                        )
                    snapshot = committed
                    results = list(committed.results)
                except TMDBError as error:
                    refresh_error = error

                if refresh_error is not None:
                    fallback = (
                        ((scope is not None and snapshot_store.load(scope)) or snapshot)
                        if manual_refresh
                        else None
                    )
                    if fallback is not None and cached_fallback_valid():
                        snapshot = fallback
                        results = list(snapshot.results)
                        listing_error = refresh_error
                        cached_render = True
                        report_listing_error(refresh_error)
                    else:
                        raise refresh_error

            if cached_render:
                runtime.set_category(
                    "{0} / {1}".format(
                        runtime.localized(30418), runtime.localized(30439)
                    )
                )
            elif manual_refresh:
                runtime.set_category(runtime.localized(30418))

            if is_movie and runtime.get_setting("tmdb_hide_unreleased") == "true":
                today = runtime.today().isoformat()
                results = [
                    value
                    for value in results
                    if not value.get("release_date") or value["release_date"] <= today
                ]
            if sort_mode == 0:
                results = sorted(
                    results,
                    key=lambda value: czech_sort_key(
                        value.get("title") or value.get("name") or ""
                    ),
                )
            elif sort_mode == 2:
                results = sorted(
                    results,
                    key=lambda value: float(value.get("vote_average") or 0),
                    reverse=True,
                )
            elif sort_mode == 3:
                results = sorted(
                    results,
                    key=lambda value: value.get("release_date")
                    or value.get("first_air_date")
                    or "",
                    reverse=True,
                )

            # Genuine complete empty results need no locale/genre request.
            genre_map = genres(media, operation_language) if results else {}
            genre_refresh_due = bool(results) and genre_map is None
            if genre_map is None:
                genre_map = {}
            media_ids = [value["id"] for value in results]
            visible_ids = media_ids[:WATCHLIST_PREFETCH_LIMIT]
            visible_id_set = set(visible_ids)
            cached_trailers, missing_trailers = _cached_trailers(
                trailer_repository, media, visible_ids
            )
            cached_shows, missing_shows = (
                ({}, [])
                if is_movie
                else _cached_shows(show_repository, media_ids)
            )
            missing_shows = [
                show_id for show_id in missing_shows if show_id in visible_id_set
            ]
            # Genre/cache activity above may have yielded to another invocation;
            # never present rows under a newly selected account or language.
            if listing_error is None:
                require_valid_operation()
            elif not cached_fallback_valid():
                raise TMDBError("TMDB identity changed while rendering cached fallback")

            for value in results:
                media_id = value["id"]
                title = value.get("title") if is_movie else value.get("name")
                original_title = (
                    value.get("original_title")
                    if is_movie
                    else value.get("original_name")
                )
                title = title or original_title or runtime.localized(30408)
                year = (value.get("release_date") or value.get("first_air_date") or "")[:4]
                label = "{0} ({1})".format(title, year) if year else title
                if not is_movie:
                    label += _next_episode_suffix(cached_shows.get(media_id))

                item = runtime.list_item(label=label)
                artwork = {}
                if value.get("poster_path"):
                    artwork["poster"] = _IMAGE_URL + "w500" + value["poster_path"]
                    artwork["thumb"] = artwork["poster"]
                if value.get("backdrop_path"):
                    artwork["fanart"] = _IMAGE_URL + "w780" + value["backdrop_path"]
                if artwork:
                    item.setArt(artwork)

                commands = [
                    (
                        runtime.localized(30414),
                        "Container.Update({0})".format(
                            _url(
                                runtime,
                                action="search",
                                what=original_title or title,
                                sort="largest",
                            )
                        ),
                    )
                ]
                trailer_command = "RunPlugin({0})".format(
                    _url(
                        runtime,
                        action="tmdb_trailer",
                        media=media,
                        id=media_id,
                    )
                )
                commands.append((runtime.localized(30415), trailer_command))
                commands.append(
                    (
                        runtime.localized(30416),
                        "RunPlugin({0})".format(
                            _url(
                                runtime,
                                action="tmdb_watchlist",
                                category=category,
                                watched=media_id,
                            )
                        ),
                    )
                )
                item.addContextMenuItems(commands)
                apply_video_metadata(
                    item,
                    {
                        "title": title,
                        "mediatype": "movie" if is_movie else "tvshow",
                        "plot": value.get("overview") or "",
                        "year": int(year) if year.isdigit() else 0,
                        "genre": " / ".join(
                            genre_map[genre_id]
                            for genre_id in value.get("genre_ids", [])
                            if genre_id in genre_map
                        ),
                        "rating": float(value.get("vote_average") or 0),
                    },
                )
                url = (
                    _url(runtime, action="search", what=title, sort="largest")
                    if is_movie
                    else _url(
                        runtime,
                        action="tmdb_watchlist",
                        show_id=media_id,
                        category="shows",
                    )
                )
                runtime.add_item(url, item, True)

            if not results:
                item = runtime.list_item(label=runtime.localized(30417))
                item.setArt({"icon": "DefaultVideoPlaylists.png"})
                runtime.add_item(
                    _url(runtime, action="tmdb_watchlist", category=category),
                    item,
                )

            item = runtime.list_item(label=runtime.localized(30438))
            item.setArt({"icon": "DefaultAddonsRecentlyUpdated.png"})
            runtime.add_item(
                _url(
                    runtime,
                    action="tmdb_watchlist",
                    category=category,
                    refresh=1,
                ),
                item,
            )

            runtime.set_content("movies" if is_movie else "tvshows")

            def post_complete():
                try:
                    set_default_view(params)
                except Exception:
                    runtime.logger.exception("Unable to set TMDB default view")
                if warm_open:
                    # Ordinary warm snapshots retain their throttled background refresh.
                    # Manual completion already fetched or retained authoritative data.
                    try:
                        background.submit_watchlist(
                            scope,
                            allow_missing_account=account_was_missing,
                            refresh_due=(
                                refresh_state.refresh_due or genre_refresh_due
                            ),
                            generation=refresh_state.generation,
                        )
                    except Exception:
                        runtime.logger.exception("Unable to submit watchlist refresh")
                # Cache misses were discovered with the row values above. Workers
                # persist data but never refresh a running container.
                if missing_trailers or missing_shows:
                    # One combined submission establishes fair family order before
                    # the single worker can start draining either family.
                    try:
                        background.submit_metadata(
                            media,
                            missing_trailers,
                            missing_shows,
                            missing_known=True,
                        )
                    except Exception:
                        runtime.logger.exception("Unable to submit TMDB metadata")

            return DirectoryResult(
                succeeded=listing_error is None, post_complete=post_complete
            )
        except TMDBError as error:
            # Snapshot invalidation belongs only to the service's explicit
            # identity.invalidate operation; failed list/genre bytes stay intact.
            report_listing_error(error)
            return DirectoryResult(succeeded=False)

    def watchlist(params):
        runtime.set_category(runtime.localized(30418))
        watched_action = bool(params.get("watched"))
        if not valid_configuration():
            if watched_action:
                return ActionResult(handled=False)
            return DirectoryResult(succeeded=False)
        if watched_action:
            return watched(params)
        if not runtime.get_setting("tmdb_access_token").strip():
            if "category" not in params:
                item = runtime.list_item(label=runtime.localized(30419))
                item.setArt({"icon": "DefaultAddonService.png"})
                runtime.add_item(_url(runtime, action="settings"), item)
                return DirectoryResult()
            runtime.notify(runtime.localized(30420))
            runtime.open_settings()
            return DirectoryResult(succeeded=False)

        if "category" not in params:
            for label, category, icon in (
                (runtime.localized(30421), "movies", "DefaultMovies.png"),
                (runtime.localized(30422), "shows", "DefaultTVShows.png"),
            ):
                item = runtime.list_item(label=label)
                item.setArt({"icon": icon})
                runtime.add_item(
                    _url(runtime, action="tmdb_watchlist", category=category),
                    item,
                    True,
                )
            return DirectoryResult()
        if "show_id" in params:
            try:
                return list_episodes(params) if "season" in params else list_seasons(params)
            except TMDBError as error:
                report_listing_error(error)
                return DirectoryResult(succeeded=False)
        return list_watchlist(params)

    def trailer(params):
        lookup = trailer_repository.lookup(params["media"], params["id"])
        if lookup.status != "hit" or not valid_youtube_id(lookup.youtube_id):
            runtime.notify(
                runtime.localized(30423)
                if lookup.status == "missing"
                else runtime.localized(30424)
            )
            return ActionResult(handled=False)
        if not runtime.addon_available("plugin.video.youtube"):
            runtime.notify(runtime.localized(30425))
            return ActionResult(handled=False)
        return ActionResult(
            handled=True, play_media=youtube_play_media(lookup.youtube_id)
        )

    def menu(params):
        item = runtime.list_item(label=runtime.localized(30418))
        item.setArt({"icon": "DefaultVideoPlaylists.png"})
        runtime.add_item(_url(runtime, action="tmdb_watchlist"), item, True)
        return DirectoryResult()

    def discovery_menu(params):
        item = runtime.list_item(label=runtime.localized(30441))
        item.setArt({"icon": "DefaultMovies.png"})
        runtime.add_item(
            _url(runtime, action="tmdb_discovery"),
            item,
            True,
        )
        return DirectoryResult()

    discovery_handlers.update({
        "menu": menu,
        "discovery_menu": discovery_menu,
        "tmdb_watchlist": watchlist,
        "tmdb_auth": authenticate,
        "tmdb_trailer": trailer,
    })
    return discovery_handlers
