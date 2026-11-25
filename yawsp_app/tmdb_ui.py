"""TMDB Kodi presentation; completion remains owned by the router adapters."""

from contextlib import nullcontext
from datetime import date
import io
import os
import struct
import zlib

try:
    from urllib.parse import urlencode
except ImportError:  # pragma: no cover - retained for older Kodi interpreters
    from urllib import urlencode

from yawsp_app.runtime import ActionResult, DirectoryResult
from yawsp_app.search import czech_sort_key
from yawsp_app.tmdb import (
    GenreRepository,
    ShowRepository,
    TMDBError,
    TrailerRepository,
    WATCHLIST_PREFETCH_LIMIT,
    fetch_complete_watchlist,
    resolve_watchlist_account,
    watchlist_operation_client,
)
from yawsp_app.watchlist_snapshot import (
    SnapshotPersistenceError,
    SnapshotScope,
)


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
    reader = TrailerRepository(None, repository.cache, clock=repository.clock)
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
            if reader._fresh(entry):
                values[int(media_id)] = entry["youtube_id"]
            else:
                missing.append(int(media_id))
    return values, missing


def _cached_shows(repository, show_ids):
    """Read displayable fresh/stale values and freshness misses in one pass."""
    reader = ShowRepository(None, repository.cache, clock=repository.clock)
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
        data = response.json()
        if not isinstance(data, dict):
            raise TMDBError("TMDB session response must be an object")
        session_id = data.get("session_id")
        return session_id if isinstance(session_id, str) and session_id.strip() else None
    except (TypeError, ValueError) as error:
        raise TMDBError("TMDB session response is malformed") from error
    finally:
        response.close()


def _set_watchlist(client, media, media_id, account_id=None):
    account_id = account_id or client.get_account_id()
    response = client.request(
        "POST",
        "/3/account/{0}/watchlist".format(account_id),
        json_data={"media_type": media, "media_id": int(media_id), "watchlist": False},
    )
    try:
        data = response.json()
        return (
            response.status_code in (200, 201)
            and isinstance(data, dict)
            and data.get("success") is True
        )
    finally:
        response.close()


def build_tmdb_handlers(runtime, client_factory, repositories, background):
    """Build all TMDB actions without performing per-item synchronous lookups."""
    client = client_factory()
    trailer_repository = repositories["trailers"]
    show_repository = repositories["shows"]
    attach_repositories = getattr(background, "attach_repositories", None)
    if attach_repositories is not None:
        attach_repositories(trailer_repository, show_repository)
    snapshot_store = getattr(background, "snapshot_store", None)
    identity_lock = getattr(background, "_lock", None)
    genre_repository = getattr(background, "genre_repository", None) or GenreRepository()

    def genres(media, language):
        return genre_repository.get(client, media, language)

    def language():
        mapper = getattr(client, "locale_mapper", None)
        return mapper() if mapper is not None else "en-US"

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
                    # Identity belongs to the old session and must never leak into
                    # the newly authorized account. The resolver commits under this
                    # same owner lock; network polling above remains outside it.
                    with identity_lock or nullcontext():
                        old_account_id = runtime.get_setting("tmdb_account_id").strip()
                        client.invalidate_identity()
                        if snapshot_store is not None and old_account_id:
                            try:
                                snapshot_store.invalidate_identity(old_account_id)
                            except SnapshotPersistenceError:
                                runtime.logger.exception(
                                    "Unable to invalidate old watchlist identity"
                                )
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
        show = client.get("/3/tv/{0}".format(show_id))
        title = show.get("name") or show.get("original_name") or runtime.localized(30408)
        original_title = show.get("original_name") or title
        runtime.set_category(title)
        for season in sorted(
            show.get("seasons", []), key=lambda value: value.get("season_number", 0)
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
            item.setInfo(
                "video",
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
        data = client.get("/3/tv/{0}/season/{1}".format(show_id, season_number))
        today = runtime.today()
        for episode in data.get("episodes", []):
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
            item.setInfo("video", info)
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
        media = "movie" if params.get("category") == "movies" else "tv"
        try:
            session_id = runtime.get_setting("tmdb_access_token").strip()
            operation_language = language()
            account_was_missing = not runtime.get_setting("tmdb_account_id").strip()

            def account_resolution_valid(resolved_account):
                return (
                    runtime.get_setting("tmdb_access_token").strip() == session_id
                    and runtime.get_setting("tmdb_account_id").strip()
                    in ("", resolved_account)
                    and language() == operation_language
                )

            account_id = resolve_watchlist_account(
                client,
                session_id,
                operation_language,
                account_resolution_valid,
                identity_lock=identity_lock,
            )

            def operation_valid():
                return (
                    runtime.get_setting("tmdb_access_token").strip() == session_id
                    and (
                        runtime.get_setting("tmdb_account_id").strip() == account_id
                        or (
                            account_was_missing
                            and not runtime.get_setting("tmdb_account_id").strip()
                        )
                    )
                    and language() == operation_language
                )

            scope = SnapshotScope(
                account_id,
                "movies" if media == "movie" else "tv",
                operation_language,
            )
            if not operation_valid():
                raise TMDBError("TMDB identity changed before watched operation")
            operation_client = watchlist_operation_client(
                client, scope, session_id, identity_lock=identity_lock
            )
            if not _set_watchlist(
                operation_client, media, params["watched"], account_id=account_id
            ):
                runtime.notify(runtime.localized(30412))
                return ActionResult(handled=False)
            if snapshot_store is not None:
                if not operation_valid():
                    raise TMDBError("TMDB identity changed during watched operation")
                try:
                    snapshot_store.remove(scope, int(params["watched"]))
                except (SnapshotPersistenceError, TypeError, ValueError):
                    runtime.logger.exception("Unable to update watched snapshot")
                background.submit_watchlist(
                    scope,
                    force=True,
                    allow_missing_account=account_was_missing,
                )
            runtime.notify(runtime.localized(30413))
            return ActionResult(handled=True, refresh=True)
        except Exception:
            runtime.logger.exception("TMDB watched action failed")
            runtime.notify(runtime.localized(30412))
            return ActionResult(handled=False)

    def list_watchlist(params):
        category = params.get("category")
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
            if snapshot is None:
                # A missing/incompatible identity always follows the current cold
                # account path; another account's sole cache entry is not a key hint.
                def account_resolution_valid(resolved_account):
                    return (
                        runtime.get_setting("tmdb_access_token").strip()
                        == operation_session
                        and runtime.get_setting("tmdb_account_id").strip()
                        in ("", resolved_account)
                        and language() == operation_language
                    )

                account_id = resolve_watchlist_account(
                    client,
                    operation_session,
                    operation_language,
                    account_resolution_valid,
                    identity_lock=identity_lock,
                )
                scope = SnapshotScope(account_id, watchlist_media, operation_language)
                if snapshot_store is not None:
                    # Identity resolution may recover a scope whose snapshot was
                    # saved before this process forgot the optional account ID.
                    refresh_state = snapshot_store.refresh_state(scope, runtime.clock())
                    snapshot = refresh_state.snapshot

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

            require_valid_operation()
            if snapshot is not None:
                results = list(snapshot.results)
            else:
                expected_generation = (
                    refresh_state.generation
                    if snapshot_store is not None
                    else None
                )
                operation_client = watchlist_operation_client(
                    client,
                    scope,
                    operation_session,
                    identity_lock=identity_lock,
                )
                results = fetch_complete_watchlist(
                    operation_client, scope, operation_valid
                )
                require_valid_operation()
                if snapshot_store is not None:
                    try:
                        saved = snapshot_store.save_complete(
                            scope,
                            results,
                            runtime.clock(),
                            expected_generation=expected_generation,
                        )
                    except SnapshotPersistenceError:
                        # The complete data is still renderable; only its warm reuse
                        # is unavailable on this invocation.
                        runtime.logger.exception("Unable to persist watchlist snapshot")
                    else:
                        if saved is None:
                            raise TMDBError("Watchlist changed during cold listing")

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
            require_valid_operation()

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
                item.setInfo(
                    "video",
                    {
                        "title": label,
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

            runtime.set_content("movies" if is_movie else "tvshows")

            def post_complete():
                try:
                    set_default_view(params)
                except Exception:
                    runtime.logger.exception("Unable to set TMDB default view")
                if snapshot is not None:
                    # submit_watchlist applies the five-minute throttle and reserves
                    # the scope. A cold successful save deliberately skips this.
                    try:
                        background.submit_watchlist(
                            scope,
                            allow_missing_account=account_was_missing,
                            refresh_due=refresh_state.refresh_due,
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

            return DirectoryResult(post_complete=post_complete)
        except TMDBError as error:
            if (
                snapshot_store is not None
                and account_id
                and not runtime.get_setting("tmdb_account_id").strip()
            ):
                try:
                    snapshot_store.invalidate_identity(account_id)
                except SnapshotPersistenceError:
                    runtime.logger.exception("Unable to invalidate watchlist identity")
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
        if lookup.status != "hit" or not lookup.youtube_id:
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
            handled=True,
            play_media="PlayMedia(plugin://plugin.video.youtube/play/?video_id={0})".format(
                lookup.youtube_id
            ),
        )

    def menu(params):
        item = runtime.list_item(label=runtime.localized(30418))
        item.setArt({"icon": "DefaultVideoPlaylists.png"})
        runtime.add_item(_url(runtime, action="tmdb_watchlist"), item, True)
        return DirectoryResult()

    return {
        "menu": menu,
        "tmdb_watchlist": watchlist,
        "tmdb_auth": authenticate,
        "tmdb_trailer": trailer,
    }
