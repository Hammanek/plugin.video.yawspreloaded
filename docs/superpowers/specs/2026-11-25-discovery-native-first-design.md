# Objevit — native-first design

**Status:** implementation direction accepted by the user's subsequent **Go** after builder review. The clarifications below are binding. Nothing had been implemented when this document was drafted.

## Why

The original requested default was custom Netflix-inspired WindowXML. Real Kodi showed those custom approaches cannot be adopted safely here:

1. Original `NativeDiscoveryWindow()` fails at the mandatory binding constructor.
2. Forwarding documented constructor arguments in a disposable candidate reaches `doModal`, but the plugin directory keeps Busy 10138 active and prevents custom window 13000/onInit.
3. A post-completion plugin window can reach onInit, but Back causes a second plugin-modal activation (2 modal entries/exits).
4. The approved standalone script Gate 0 then registered the extension correctly, but normal root selection of `RunScript(plugin.video.yawspreloaded,source=popular_movies)` dispatched it to **VideoPlayer::OpenInputStream**, not the registered script. OnInit never occurred; repeat activation was correctly not attempted. Independent negative-evidence review retained **14 PASS / 2 FAIL / 15 UNCHECKED** and prohibited production use.

User therefore chooses native-first instead of adding more lifecycle/launcher architecture. The already live-proven native InfoWall is the delivery path, not an unverified alternative presentation.

## Builder clarifications accepted before implementation

1. **Selector state:** only an absent `source` key opens the local category selector. `source` present-but-empty, non-string or unrecognized still fails locally. Root Objevit links to `action=tmdb_discovery` **without** `source`. Direct bookmarks with a valid source still open the requested native page.
2. **Native rendering:** “native InfoWall always renders” means ordinary native rows/pages after category selection, never an unconditional forced view-54 action. Preserve the current Estuary-only, one-time `tmdb_view_defaulted != "true"` callback behavior and no unconditional skin/view changes.
3. **URL construction:** preserve injected `url_builder` and runtime base URLs rather than replacing them with a hardcoded base in core discovery. The required production outcome remains canonical `plugin://plugin.video.yawspreloaded/...` with URL-encoded values and `sort=largest`. Dedicated-script argv paths are irrelevant because no standalone script will ship.
4. **Validation boundary:** focused TDD and scoped validations precede the implementation checkpoint. The new checkpoint commit precedes full unittest discovery and the actual release verifier, because `verify_release()` derives expected inventory from `ls-tree HEAD` and deleted tracked runtime sources cannot disappear before their deletion commit. Every fix commit is followed by a fresh full suite/verifier; no inventory mismatch is hidden.

## User-visible behavior

### Root and categories

Keep root order **Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist**. Root remains static; no discovery HTTP/trailer/show/account/prefetch job runs when it renders.

Objevit opens a normal native local category selector with four entries:

| Public key | Existing fixed path | User category |
|---|---|---|
| `popular_movies` | `/movie/popular` | Popular movies |
| `popular_shows` | `/tv/popular` | Popular TV shows |
| `top_rated_movies` | `/movie/top_rated` | Top-rated movies |
| `top_rated_shows` | `/tv/top_rated` | Top-rated TV shows |

All four endpoints remain exact mappings from existing `CATALOG_SOURCES`; no new path interpolation is added.

Only after a category selection does the handler issue one versioned selected list request through the actual `/3` adapter and, for nonempty results, at most one shared genre lookup for that media type. No page loop or progressively expanding metadata requests. Unknown public keys remain local directory failures before API-key gate/settings/network.

### Row presentation and handoff

Selected category renders native rows through existing production `discovery_list_item` and `apply_video_metadata`: title/year, locale genre, rating, plot, poster/thumb/backdrop and native InfoTagVideo. This is the same code which achieved actual Kodi `getVideoInfoTag` read-back and no addon `setInfo` deprecation in the earlier run.

`set_content()` remains `movies`/`tvshows`; the existing default-view callback still happens once after a successful directory. Public empty uses existing 30440; error reports use existing 30402 and current sanitized client behavior. API-key validation stays before valid category HTTP. Manual account/session/watchlist snapshot lookups are never required; the public TMDB client continues to omit session_id and public 401 never invalidates the personal identity.

Result activation goes through **existing ordinary Webshare plugin search**, not a script or WindowXML: `plugin://plugin.video.yawspreloaded/?action=search&what=<encoded localized title>&sort=largest`. Optional existing original-title context command is preserved. No autoplay/watched mutation/download/metadata burst.

### Presentation setting removal

The custom presentation experiment and its flag are removed rather than left partly supported:

- delete `yawsp_app/discovery_window.py`;
- delete `resources/skins/default/720p/DiscoveryWindow.xml`;
- delete `tests/test_discovery_window.py` (including the intentional constructor regression, whose class under test no longer exists);
- remove `discovery_native_wall` from `resources/settings.xml` and label 30444 from all three locale catalogs;
- remove custom factory loading, `system_localized(1040)` injection/fallback if no other runtime user needs it, and the custom branch from `discovery_ui.py`;
- remove `discovery_script.py` permanently: no such production file exists today and none will be added.

The actual InfoWall builder no longer needs an injected custom window factory. Its signature becomes `build_discovery_handlers(runtime, client, url_builder, genres, set_default_view, valid_configuration=None)`. Its data model, source validation, public-client adapter, loading/native error and content behavior remain intact. Four selector-only URLs are produced without network. Existing direct `tmdb_discovery&source=<valid>` bookmarks keep working as native pages without the deleted presentation setting.

A historical stored false/true key has no effect going forward: native InfoWall always renders after category selection. This is an explicit accepted removal of a flag that existed only in the unmerged experimental branch, not a reopened service migration or silent setting fallback.

## Public interfaces and tests retained/changed

The existing `TMDBClient` and `public_tmdb_client()` are reused with current sanitization. `build_tmdb_handlers()` supplies the same client, public adapter, `_url`, shared genre repository and `set_default_view`; only custom factory and presentation switches are removed. Existing Webshare/watchlist/media jobs are untouched.

New/updated tests must prove:

- static root six-entry ordering and Objevit first opens local four-category selector with zero requests/background jobs;
- all four immutable endpoints and a selected source cost one versioned list plus optional one shared genre call; unknown selector fails before API config/network;
- row values, artwork, native InfoTag values and canonical handoff `sort=largest` with no account/session_id/autoplay/watched/prefetch;
- empty category skips genre and shows existing 30440; public error preserves personal session; missing API key never calls transport;
- there is no `discovery_native_wall` setting/reading/factory, no WindowXML/script module/resource registered and no stale default-custom README promise;
- `scripts/verify_release.py` catches runtime inventory dynamically. Existing active performance, security, sanitized transport, media download, snapshot/manual-refresh and archive validation suites remain intact.

The old IntentionalWindowBinding RED test disappears only because its entire unsupported experimental window/module was explicitly retired after rejected viability proofs, not to make that binding defect look fixed.

## Non-goals and lifecycle boundary

- No custom WindowXML, `xbmc.python.script`, `xbmc.service`, new thread/queue, alternate launcher or repair of the already reproduced binding failure.
- No claim that this resolves the separate active-background shutdown HANG. Final integration remains blocked/clarified by that independent lifecycle result unless separately decided.
- No change to released 2.0.0 ZIP/news/setup/version, local permanent backups or user database/text files.
- No merge, push, tag, release or main-profile update without their own approval after complete checks.

## Validation and completion gate

1. TDD native selector/default behavior first; full unittest discovery after removing the experimental test module. Python 3.8 AST plus PO/addon/settings XML gates.
2. Independent native-first functional/data/privacy review and archive/package verification. Full test suite may be green only because the intentionally removed experiment is absent, not because its constructor was patched without evidence.
3. New isolated Kodi 21.2 run of the four-category selector and at least movie/TV native pages, live public sessionless traffic, InfoTag read-back, original-title Webshare route, empty/error and InfoWall switching settings no longer affecting capabilities.
4. Confirm actual source has no obsolete window/script path and full diff/review again distinguishes live UI PASS from untouched background-shutdown HANG. Documentation updates note actual result.

Never merge or release automatically at these gates.

## Consequences

This intentionally abandons the previously requested Netflix-inspired default as unproven after multiple allowed experiments. The retained Objevit is bounded native discovery with an ordinary category selector and InfoWall, which has actual Kodi execution evidence. Existing experimental evidence is retained rather than rewritten as a custom success.

There is no production script fix retained from failed Gate 0. It would not be acceptable to call this final native implementation proof that custom WindowXML ever worked.
