# Native visual acceptance — 2.1.0 candidate

Date: 2026-11-26

## Verdict and boundary

**PASS with an explicitly bounded scope.** An independent reviewer opened and evaluated thirteen actual native Kodi frames and returned 13/13 PASS. This is not a claim derived from JSON response codes, `InfoTag` values, screenshot existence, or generated artwork.

Validated environment: **Kodi 21.2 (Omega), Windows x64 / CPython 3.8.15, Estuary**, in fresh disposable profiles. Every TMDB request used synthetic loopback fixtures. Artwork was a local `special://temp` PNG. No real credentials, Webshare playback, repository downloads, installed main profile, or host firewall-rule changes were used.

The main production mutation remained local. No tag, push, GitHub release, or main-profile installation is authorized by this report.

## Runtime provenance

- Merged shutdown-hardening baseline: `6955e2aeac3e1e1d2ab53df98d316aa61661c530`.
- Native catalog/base-title fix: `dbe9702024772761b716d57b2b2382a0bb329489`.
- Usable local-fallback fix: `9bbe887d11505c34ce5659886e652d2c189c337e`.
- Final episode-title fix and episode-info frame: `4d1728e4bedde461eaca4a7e2b28d241c1544fab`.
- Each application contains exactly 32 selected runtime files. Native captures were compared to their deployed SHA-256 inventory. Later title/fallback commits are separately identified rather than relabeling an old capture as new code. Version/news and documentation preparation does not silently replace those runtime identities.

The installed Kodi executable hash was `c6b1a566a1b4185ed9f6392889600a0bef08f86babd5b21744f67daee9dc3664`. The production add-on bytes themselves were not instrumented. A disposable third-party Requests bootstrap supplied only the observation and loopback mapping, and the native image mapper exchanged synthetic external image URLs for local textures. These modifications are outside the candidate source/archive.

## Independently inspected frames

The exact filenames, dimensions, PID, capture times and SHA-256 values are in `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-visual-acceptance\visual-verification.json`. Screenshots and generated Kodi profiles are retained externally, not bundled.

| Frame | Visual check |
| --- | --- |
| `discovery-10/root.png` | Six entries in required order, visible Discover focus, 1/6 |
| `discovery-10/categories.png` | Four fixed categories in required order, visible first-category focus, 1/4 |
| `discovery-10/popular_movies_focus.png` | Focused media row, native Czech title/plot sidebar, correct poster geometry |
| `discovery-10/popular_movies_info.png` | One year only, Czech plot, rating 8.4, genre, movie type, poster/focus readable |
| `discovery-10/top_rated_shows_info.png` | TV detail with one year only and intact native presentation |
| `watchlist-04/cold_movies.png` | Complete cold listing, Alpha/Beta with Refresh, not a spinner |
| `watchlist-04/warm_movies.png` | Same valid rendered warm snapshot |
| `watchlist-04/manual_refresh.png` | Native ordinary list with two complete titles and final Refresh |
| `watchlist-04/failed_refresh_fallback.png` | Local cache label, both preserved titles and Refresh, error notice, not Files |
| `watchlist-04/movie_context_menu.png` | Snapshot Alpha context: original-title search, trailer, watched; parent is not selected |
| `watchlist-04/after_watched.png` | Only Beta and Refresh remain, watched notification visible |
| `watchlist-04/episodes.png` | Both episodes with dates and ratings, no clipping |
| `episode-info-fixed/episode_info.png` | Pilot, one year, clean episode name, first aired, rating 8.7, duration 24:00, native texture |

The earlier firewall-obscured, parent-focus, duplicated-title, busy-spinner and Files-fallback captures are deliberately rejected as acceptance evidence. No generated or edited pixels replaced the rejected native frames.

## Functional evidence that accompanies the images

- Discovery made exactly **four catalog list GETs plus one movie-genre and one TV-genre GET**. No account, session, pagination or extra metadata burst occurred. Both localized and original titles were retained; search URLs use `action=search` and `sort=largest`. Native `Container.SetViewMode(54)` occurred once and `tmdb_view_defaulted` was saved true. Later native views were not forcibly rewritten.
- Cold opened a complete Alpha/Beta movie snapshot. Warm reopen produced **zero account/list requests**. Manual `refresh=1` produced a new complete revision without immediately creating a duplicate background watchlist refresh.
- Controller page 2 returned 500. Snapshot bytes after failure matched the previous complete revision exactly (`aaf55379188382ed675c6acc0bffecaba66cd48a296946142eead24bddb303f2` for that phase). The fallback directory completed `succeeded=true` and the actual image retained its content with a truthful error notification.
- Watched used `media_type:"movie"`, `media_id:7101`, `watchlist:false`. Parent result confirmed **revision 3 / generation 1**. Manifest contained exactly one durable follow-up to that watched job. One actual `xbmc.executebuiltin("Container.Refresh")` was observed. Snapshot **revision 1 → 2 → 3, generation 0 → 1**, is distinct from the later ordinary GUI refresh of the displayed manual route, which produced complete **revision 4** and still only ID 7102.
- Episode 1 read back duration **1440 s**; episode 2 **1560 s**. The GUI displayed `24:00` for the focused first episode. The GUI season was explicitly selected before Video info. Only `/3/tv/7301` and `/3/tv/7301/season/1` were requested in the isolated scene. A failed season request produced no additional metadata submissions.
- Native Kodi quit returned zero in accepted capture phases. This doesn't substitute for the independent earlier active-shutdown/abort checks, which remain separate historical source-specific evidence.

## Defects found and verified closed

1. Native titles repeated a year carried separately by `InfoTag.year`. Regression: discovery/watchlist native metadata tests. Fixed in `dbe9702`.
2. A complete local fallback returned `DirectoryResult(succeeded=False)` along with a reported refresh error, causing Kodi to leave the useful view for Files. Regression: real production composition asserts a usable directory, original row and Refresh, byte-identical complete snapshot, and no parent HTTP. Fixed in `9bbe887`.
3. Native episode title carried row markup with an air date, causing another duplicated year. Regression: native season/episode metadata test. Fixed in `4d1728e`.

Each production change was small and accompanied by a failing test first, followed by a passing full 600-test run. Test-only helper fixes are not presented as application defects or production fixes.

## Explicit limits

Czech and Slovak diacritics are readable in inspected frames. Valid U+1F408 survives the data path but the stock Estuary font renders it as tofu. This report does not claim universal glyph support, native-font substitution, pixel-level comparison with production TMDB photographic artwork, or accessibility certification.

Unproven: other Kodi versions/skins/resolutions, Linux/POSIX/ARM, SMB/NFS/remote VFS, live remote services or credentials, actual playback, independent hardware or screen readers, power-loss recovery and main-profile installation. Images for dated fixtures show data correctness and UI usability, not remote service quality guarantee.

## Release verification record

The accompanying local release checks are `episode-title-fix-600-tests.log`, `fallback-fix-600-tests-serial.log`, final documentation checks and `scripts/verify_release.py`. Final candidate commit identity and byte-validated ZIP SHA-256 are recorded after the complete documentation/version verification step, never inferred from a captured earlier archive or from a Git commit that changed after the package. Publication of that candidate remains a separate approval gate.