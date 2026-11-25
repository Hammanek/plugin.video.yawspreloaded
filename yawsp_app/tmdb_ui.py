"""TMDB Kodi presentation; completion remains owned by the router adapters."""

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
from yawsp_app.tmdb import ShowRepository, TMDBError, TrailerRepository


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
    """Read cache through repository rules without permitting a network lookup."""
    reader = TrailerRepository(None, repository.cache, clock=repository.clock)
    values = {}
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
    return values


def _cached_shows(repository, show_ids):
    """Return fresh or stale successful values; failures are never synthesized."""
    reader = ShowRepository(None, repository.cache, clock=repository.clock)
    values = {}
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
    return values


def _next_episode_suffix(next_episode):
    if not next_episode:
        return ""
    try:
        season = int(next_episode.get("season") or 0)
        episode = int(next_episode.get("episode") or 0)
    except (TypeError, ValueError):
        return ""
    suffix = " [COLOR gray]→ S{0:02d}E{1:02d}".format(season, episode)
    air_date = next_episode.get("air_date")
    if air_date:
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


def _set_watchlist(client, media, media_id):
    response = client.request(
        "POST",
        "/3/account/{0}/watchlist".format(client.get_account_id()),
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
    genres_cache = {}

    def genres(media):
        if media not in genres_cache:
            data = client.get("/3/genre/{0}/list".format(media))
            genres_cache[media] = {
                genre["id"]: genre["name"]
                for genre in data.get("genres", [])
                if isinstance(genre, dict) and "id" in genre and "name" in genre
            }
        return genres_cache[media]

    def valid_configuration():
        if not runtime.get_setting("tmdb_api_key").strip():
            runtime.notify("Pro připojení k TMDB je třeba vyplnit API Key v nastavení.")
            runtime.open_settings()
            return False
        return True

    def report_listing_error(error):
        runtime.logger.exception("TMDB listing failed: %s", error)
        if not runtime.get_setting("tmdb_access_token").strip():
            runtime.notify("Neplatný TMDB API klíč nebo vypršené přihlášení – zkontroluj nastavení.")
            runtime.open_settings()
        else:
            runtime.notify("Chyba při načítání")

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
                runtime.notify("Naskenujte QR kód telefonem a schvalte přístup")
            else:
                runtime.notify("Schvalte přístup v prohlížeči: {0}".format(url))

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
                    # the newly authorized account.
                    client.invalidate_identity()
                    runtime.set_setting("tmdb_access_token", session_id)
                    runtime.notify("Úspěšně připojeno k TMDB!")
                    return ActionResult(handled=True)
                if monitor.waitForAbort(_AUTH_POLL_INTERVAL):
                    return ActionResult(handled=False)

            runtime.notify("Čas na ověření vypršel.")
            return ActionResult(handled=False)
        except Exception as error:
            runtime.logger.exception("TMDB authentication failed")
            runtime.notify("Připojení k TMDB se nezdařilo: {0}".format(error))
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
        title = show.get("name") or show.get("original_name") or "Neznámý název"
        original_title = show.get("original_name") or title
        runtime.set_category(title)
        for season in sorted(
            show.get("seasons", []), key=lambda value: value.get("season_number", 0)
        ):
            season_number = season.get("season_number", 0)
            episode_count = season.get("episode_count", 0)
            item = runtime.list_item(
                label="Sezóna {0} ({1} epizod)".format(season_number, episode_count)
            )
            artwork = {"icon": "DefaultSeason.png"}
            if season.get("poster_path"):
                artwork["poster"] = _IMAGE_URL + "w500" + season["poster_path"]
            item.setArt(artwork)
            item.setInfo(
                "video",
                {
                    "title": "Sezóna {0}".format(season_number),
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
        series_title = params.get("series_title") or "Neznámý název"
        runtime.set_category(
            "{0} / Sezóna {1}".format(runtime.addon_info("name"), season_number)
        )
        data = client.get("/3/tv/{0}/season/{1}".format(show_id, season_number))
        today = runtime.today()
        for episode in data.get("episodes", []):
            episode_number = int(episode.get("episode_number"))
            title = episode.get("name") or "Neznámý název"
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
            runtime.notify("Pro tuto akci se nejprve připojte k TMDB.")
            runtime.open_settings()
            return ActionResult(handled=False)
        media = "movie" if params.get("category") == "movies" else "tv"
        try:
            if not _set_watchlist(client, media, params["watched"]):
                runtime.notify("Chyba při odstraňování z watchlistu")
                return ActionResult(handled=False)
            runtime.notify("Zhlédnuto a odstraněno z watchlistu")
            return ActionResult(handled=True, refresh=True)
        except Exception:
            runtime.logger.exception("TMDB watched action failed")
            runtime.notify("Chyba při odstraňování z watchlistu")
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
            page_params = {"sort_by": "created_at.desc"} if sort_mode == 1 else {}
            results = []
            page = 1
            account_id = client.get_account_id()
            while True:
                data = client.get(
                    "/3/account/{0}/watchlist/{1}".format(account_id, watchlist_media),
                    dict(page_params, page=page),
                )
                results.extend(data.get("results", []))
                if page >= int(data.get("total_pages", 1) or 1):
                    break
                page += 1

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

            genre_map = genres(media)
            media_ids = [value["id"] for value in results]
            cached_trailers = _cached_trailers(trailer_repository, media, media_ids)
            cached_shows = (
                {}
                if is_movie
                else _cached_shows(show_repository, media_ids)
            )

            for value in results:
                media_id = value["id"]
                title = value.get("title") if is_movie else value.get("name")
                original_title = (
                    value.get("original_title")
                    if is_movie
                    else value.get("original_name")
                )
                title = title or original_title or "Neznámý název"
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
                        "Vyhledat původní název",
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
                youtube_id = cached_trailers.get(media_id)
                if youtube_id:
                    trailer_command = "PlayMedia(plugin://plugin.video.youtube/play/?video_id={0})".format(
                        youtube_id
                    )
                else:
                    trailer_command = "RunPlugin({0})".format(
                        _url(
                            runtime,
                            action="tmdb_trailer",
                            media=media,
                            id=media_id,
                        )
                    )
                commands.append(("Přehrát trailer", trailer_command))
                commands.append(
                    (
                        "Označit jako zhlédnuté",
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
                item = runtime.list_item(label="Watchlist je prázdný")
                item.setArt({"icon": "DefaultVideoPlaylists.png"})
                runtime.add_item(
                    _url(runtime, action="tmdb_watchlist", category=category),
                    item,
                )

            # Scheduling occurs only after all cached rows have been submitted to
            # Kodi. Workers persist data but never refresh a running container.
            missing_trailers = [
                media_id for media_id in media_ids if media_id not in cached_trailers
            ]
            if missing_trailers:
                background.submit_trailers(media, missing_trailers)
            if not is_movie:
                background.submit_shows(media_ids)
            runtime.set_content("movies" if is_movie else "tvshows")
            return DirectoryResult(post_complete=lambda: set_default_view(params))
        except TMDBError as error:
            report_listing_error(error)
            return DirectoryResult(succeeded=False)

    def watchlist(params):
        runtime.set_category("TMDB Watchlist")
        watched_action = bool(params.get("watched"))
        if not valid_configuration():
            if watched_action:
                return ActionResult(handled=False)
            return DirectoryResult(succeeded=False)
        if watched_action:
            return watched(params)
        if not runtime.get_setting("tmdb_access_token").strip():
            if "category" not in params:
                item = runtime.list_item(label="Připojit k TMDB v nastavení doplňku...")
                item.setArt({"icon": "DefaultAddonService.png"})
                runtime.add_item(_url(runtime, action="settings"), item)
                return DirectoryResult()
            runtime.notify("Pro zobrazení watchlistu se připojte k TMDB v nastavení doplňku.")
            runtime.open_settings()
            return DirectoryResult(succeeded=False)

        if "category" not in params:
            for label, category, icon in (
                ("Filmy", "movies", "DefaultMovies.png"),
                ("Seriály", "shows", "DefaultTVShows.png"),
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
                "Trailer nebyl nalezen."
                if lookup.status == "missing"
                else "Trailer se nepodařilo načíst."
            )
            return ActionResult(handled=False)
        if not runtime.addon_available("plugin.video.youtube"):
            runtime.notify("Pro přehrání traileru nainstalujte doplněk YouTube.")
            return ActionResult(handled=False)
        return ActionResult(
            handled=True,
            play_media="PlayMedia(plugin://plugin.video.youtube/play/?video_id={0})".format(
                lookup.youtube_id
            ),
        )

    def menu(params):
        item = runtime.list_item(label="TMDB Watchlist")
        item.setArt({"icon": "DefaultVideoPlaylists.png"})
        runtime.add_item(_url(runtime, action="tmdb_watchlist"), item, True)
        return DirectoryResult()

    return {
        "menu": menu,
        "tmdb_watchlist": watchlist,
        "tmdb_auth": authenticate,
        "tmdb_trailer": trailer,
    }
