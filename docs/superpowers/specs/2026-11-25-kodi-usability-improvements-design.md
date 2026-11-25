# Kodi Usability and Lifecycle Improvements — approved design

**Date:** 2026-11-25
**Base commit:** `bc1d08f1530b7d9d7191131f27de75d6f3755780`
**User approval:** Complete scoped work approved in conversation; no additional release or publication was authorized.

## Intent

Implement all agreed usability and release-process improvements without inventing a shutdown fix or claiming unverified compatibility. Investigation and installation tests happen in isolated temporary Kodi profiles; no secrets, account sessions, original-profile writes or forced termination of the main process is allowed during development. The existing permanent main profile can only receive explicit non-destructive backup copies described below.

## Scope and decisions

1. **Shutdown first.** Inspect the real log (already shows application saved settings/stopped player followed by a Python thread not stopping within five seconds). Reproduce in a portable Kodi 21.2 profile using controlled local HTTP responses and actual addon routes/owner workers. Trace request entry, abort signal, response wait/close and owner/client close. If a defect is reproduced, first add a deterministic regression, then make the minimum appropriate change at the responsible boundary. If no reproducible application defect is found, document the measured result instead of inventing a fix.
2. **Metadata adapter.** Replace currently used `ListItem.setInfo("video", data)` calls with one Kodi-agnostic helper using `getVideoInfoTag()`. Use dedicated setters present in Kodi documented public APIs: title, media type, plot, year, genres (as the required list), rating, season, episode, duration and first aired date. Preserve current values/types and old-item construction. A documented legacy fallback is retained for the test's older item variant, not guessed alternative API names.
3. **UX.** Add a localized **Obnovit** action at the end of the existing watchlist with new optional `refresh=1`. Keep the prior snapshot until a complete explicit fetch validates and saves. Manual fetch reuses the captured account/session/language and operation client; only it may incur additional network. Show a localized local-cache label for an existing snapshot. Render an explanatory empty state if all Webshare files on a valid page are filtered out without altering remote queries/pagination.
4. **Localization IDs.** Add **30438 = Refresh**, **30439 = Local cache**, **30440 = No matching files found** in English, Czech and Slovak. Do not renumber existing IDs or strings.
5. **CI and archive.** Add a read-only-permissions GitHub Actions workflow with fully pinned actions: checkout and setup-python. Run the existing offline unittest command on Python 3.8 and 3.12 plus a standard-library release verifier that checks README/XML version, runtime file allowlist, root directory and CRC + source-bytes equality of the built archive. Do not upload artifacts, create releases, contact live APIs or introduce application dependencies.
6. **`<news>` and documentation.** Populate released 2.0.0 news safely as XML text. Document the new UI behavior and clearly distinguish **verified Kodi 21.2 (Omega), Windows Store x64** from static Python 3.8 grammar checks and untested platforms. Preserve all benchmark limitations.
## Addendum: Separate `Objevit` gallery approved by user

User selected a **new main-menu sibling**, not replacing TMDB Watchlist into one submenu. `build_tmdb_handlers` therefore adds one Objevit row beside the existing watchlist route, giving **7 root items** (previous static menu 6 plus Objevit). Existing watchlist routing remains untouched.

Use `tmdb_discovery` as the only new route with four source mappings: `/movie/popular` (mode `popular_movies`), `/tv/popular` (mode `popular_shows`), `/movie/top_rated` (mode `top_rated_movies`) and `/tv/top_rated` (mode `top_rated_shows`). Require a TMDB API key but do not require/resolve a personal account/session or read/write watchlist snapshots. A valid category triggers one selected-list request and at most one shared genre lookup; no page loop, metadata prefetch, queued background refresh or trailer lookup occurs. Unknown modes return an unsuccessful directory without an invented external URL.

A new focused `discovery_ui.py` is created rather than expanding the large watchlist module. Its builder receives `runtime`, current `client`, `_url` URL builder, `genres(media, language)`, and `set_default_view` from existing TMDB UI to avoid circular imports. It uses the previously reviewed `apply_video_metadata`, proper list genres, native poster/fanart/backdrop artwork and returns `tmdb_discovery` handler to existing `build_tmdb_handlers`. Root `menu` adds the single entry without replacing the existing five Webshare entries and current watchlist entry.

Each result has localized `Objevit`, `Populární`/`Nejlépe hodnocené` section names, title/year/rating/plot/genre metadata and a Webshare destination with **title + sort=largest**. Original-title search uses existing ID30414 when useful. It does not navigate into watched actions, remove anything from TMDB watchlist, play a movie or auto-prefetch trailers. Each selected category is validated locally before `client.get`.

Introduce only three new locale keys assigned sequential free IDs: **30441 Discover**, **30442 Popular**, **30443 Top rated** in English, Czech and Slovak. An empty valid result uses existing ID30440's explanatory row; transport/malformed responses follow `report_listing_error` requiring no secret output.

After the gallery is added, the runtime archive allowlist increases by one file (`discovery_ui.py`). Update verifier/tests to consume the current filename list and exact count instead of a stale (26 after metadata vs 25 before it) hardcoded total.
 Copy, without deleting originals, existing addon/manifest backups into `%USERPROFILE%\AppData\Local\Packages\XBMCFoundation.Kodi_4n2hpmxwrvr6p\LocalCache\Roaming\Kodi\userdata\backup\plugin.video.yawspreloaded` in the authorized main profile. Preserve bytes/SHA-256 and never print backup data or credentials. No execution/modification of another Kodi user/profile.
8. **Final verification.** County-by-count regressions for metadata mapping and no-auto-refresh behavior, all localizations/catalogs, workflow/action SHA pinning, archive roots/bytes and documentation truth. Run unit discovery and independent functional/performance/devops reviews. Live validation in an isolated Kodi 21.2 profile verifies metadata UI, manual refresh and shutdown behavior actually relevant to the change before any integration choice. No automatic main merge, push, tag or release is included.

## Interfaces and invariants

- Keep `main.py`, existing public route families and all settings IDs. `refresh=1` is additive and does not mutate previously existing calls without it.
- Keep captured identity locks, identity scoping and complete snapshot generation/revision constraints. Failed explicit refresh leaves previous complete data untouched and skips an unnecessary post-complete duplicate background submission. The normal warm/cold conditional submit behavior remains.
- Keep no secrets in git, logs, tests, reports or generated release archives. Use only synthetic credentials and offline HTTP.
- New files: one narrowly scoped metadata helper, one standard-library release verifier, one CI workflow, and their focused tests.
- Code must parse with Python 3.8 grammar for Kodi 19 compatibility claims, but only Kodi 21.2 runtime behavior is claimed verified.
