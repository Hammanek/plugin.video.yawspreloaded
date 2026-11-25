# Objevit Native-First Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Objevit as a local four-category selector with native InfoWall rows and existing Webshare handoff, while removing the experimentally rejected WindowXML/script presentation completely.

**Architecture:** Reuse the existing immutable discovery catalog, sessionless public TMDB adapter and native metadata mapping. `tmdb_discovery` without `source` is a static four-row selector; a valid source renders the already native directory. Custom window/script launchers, presentation flag and custom-only localizer/decorators disappear entirely. No active background lifetimes change.

**Tech Stack:** Python 3.8-compatible source, Kodi 21.2 native plugin directory/ListItem/InfoTagVideo, existing TMDB `requests` sanitization, stdlib unittest/zip/XML and isolated cloned Kodi 21.2 Windows Store x64 for final evidence

**Spec:** `docs/superpowers/specs/2026-11-25-discovery-native-first-design.md`, including the accepted builder clarifications

## Global Constraints

- Current experimental base is `b11b36a`; `tests/test_discovery_window.py` is intentionally RED because the binding defect was never fixed. That experimental module and test are deleted explicitly as rejected scope, not replaced with a purported constructor fix.
- Root order is exactly Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist. Root and local selector issue no TMDB/Webshare calls or metadata/background jobs.
- Only absent `source` selects the four-row native selector. Present empty, non-string or unknown `source` fails locally. Valid direct `tmdb_discovery&source=<known>` still renders native rows. Do not move all four categories to the root or broaden public endpoint mapping beyond existing `CATALOG_SOURCES`.
- Every category uses one selected versioned list path (through existing `/3` adapter) and, for nonempty result list, at most one shared genre request. No pagination, account, snapshots, per-item metadata, watched/autoplay/background prefetch. Empty list skips genre and displays existing 30440. Missing API key fails before HTTP. Public 401 never changes personal identity.
- Handler uses existing `url_builder`/production runtime base and encoding. Production target is `plugin://plugin.video.yawspreloaded/?action=search&what=<localized title>&sort=largest`. No script-url derivation or hardcoded alternate base is introduced.
- Native-first means ordinary native rows. Do not force view 54 on every opening/skin: keep current Estuary-only and `tmdb_view_defaulted != "true"` post-success callback. Watchlist views are not rewritten.
- Delete `yawsp_app/discovery_window.py`, `resources/skins/default/720p/DiscoveryWindow.xml`, `tests/test_discovery_window.py`, `discovery_native_wall` and localized 30444. Add no `discovery_script.py`, `xbmc.service`, `xbmc.python.script` or replacement custom presentation. Remove unused factory, custom loading 1040 injection/localizer, and language catalog references only when actually obsolete; never remove existing API/genre/watchlist helpers used elsewhere.
- The new signature is `build_discovery_handlers(runtime, client, url_builder, genres, set_default_view, valid_configuration=None)`. `build_tmdb_handlers()` still reuses the existing client/session, public genres and set_default_view. Preserve all other handler/media/download/snapshot behavior.
- No automatic merge, push, tag, release, hosted-CI claim, network external API, real credential exposure or main-profile operation. `addon.xml` version/news and release 2.0.0 setup links remain unchanged.
- Focused checks precede each checkpoint commit. Full suite and archive verification run again **after** committing the deletion/implementation, because HEAD ls-tree and file reads cannot yet recognize uncommitted deletions. Any Important fix gets its own regression and renewed review.

## Review Focus

- Missing key means selector; explicit empty/unknown input means error: Task 2 exact-state tests and Task 4 live navigation must distinguish selector from invalid input without a root burst.
- Root or selector can accidentally request metadata before a category was chosen: Tasks 1–2 check zero calls, so only user selection triggers bounded transport.
- Deleting a flag/module may leave handler creation or native mode still trying to construct custom objects: Task 1 static/render checks ensure ordinary watchlist/root construction and every list never reads retired presentation settings or APIs.
- Native view could accidentally become unconditional across skins/openings: Task 2 verifier preserves existing helper conditions and post-success-only invocation.
- Canonical URL/public identity can regress during the presentation simplification: Tasks 1–2 keep actual public list+genre no-session, `api_key`+locale and canonical Webshare targets; Task 4 proves them in Kodi.
- Negative experimental proof and known active-background HANG can be erased or described as fixed: Task 3 retains historical records, Task 4 uses actual native execution only and never trades that shutdown bug for a UI workaround.

---

### Task 1: Retire custom presentation and its runtime/configuration surface

**Files:**
- Delete: `yawsp_app/discovery_window.py`, `tests/test_discovery_window.py`, `resources/skins/default/720p/DiscoveryWindow.xml`
- Modify: `yawsp_app/discovery_ui.py`, `yawsp_app/tmdb_ui.py`, `yawsp_app/router.py`, `yawsp_app/runtime.py`, `resources/settings.xml`
- Modify all three PO catalogs and `tests/test_discovery_ui.py`, `tests/test_discovery_integration.py`, `tests/test_resources.py`, `tests/test_runtime.py`, and any test entrypoint references strictly required by the removed factory/core 1040 interface.

**Interfaces:**
- Consumes: current immutable catalog mappings, `_PublicTMDBClient` through `public_tmdb_client()`, existing `discovery_list_item()`, shared genre repository and `set_default_view`.
- Produces: `build_discovery_handlers(runtime, client, url_builder, genres, set_default_view, valid_configuration=None)` with native `tmdb_discovery` for valid source. No window/script/presentation factory branch exists.

- [ ] **Step 1: Rewrite focused presentation tests before source changes**

In `tests/test_discovery_ui.py`, preserve immutable-source, normalization, native metadata, public error, empty and no-I/O cases as native-directory assertions. Replace custom modal/flag-switch expectations with assertions that valid sources render native rows without discovering windows or reading a presentation setting. Preserve `post_complete` view only after success and existing empty/error semantics.

In `tests/test_discovery_integration.py`, replace the old default-custom/popular test with one requiring native row output, one list+optional genre, `set_content`, a canonical row search target and end-directory followed by one ordinary default-view callback. Delete only truly custom-window dependencies, not useful catalog guards. Add source/registry assertions proving `discovery_native_wall`, `create_discovery_window`, resource/script and 30444 are absent while used catalogs 30441–30443 remain.

Run focused suites and record expected RED: removed window/factory is still present and native-only builder still takes custom factory. Do not run full release-current-checkout test before committing deletion because tracked files still exist in HEAD.

- [ ] **Step 2: Remove experiment and its references without changing catalog/public-client behavior**

Delete the three experimental files. Remove custom `custom()` / modal construction from `discovery_ui.py`, leaving category normalization/native `native()` and existing immutable catalog/source validation untouched. Update its builder signature as above. Remove `create_discovery_window` import/use from `tmdb_ui.py` and custom presentation setting checks.

Remove obsolete 30444 setting/catalog lines and any `_runtime_options["system_localizer"]` plus `KodiRuntime.system_localized()` only when search proves no remaining production consumer after custom removal. Remove test-only 1040 fixture behaviors when the API is gone; never alter existing `localized()` addon behavior or language mapping. The script was never committed: do not add or register one.

`discover(params)` may temporarily retain its prior missing-source rejection (Task 2 changes it). Valid source goes straight through existing native rows, public genres and successful native view callback. Do not create a substitute category custom control.

- [ ] **Step 3: Run focused native regression checks and checkpoint**

Run `python -B -m unittest tests.test_discovery_ui tests.test_discovery_integration tests.test_resources tests.test_runtime tests.test_entrypoint -q` (or only the necessarily changed selectors if compatibility fixtures require targeted updates) and Python 3.8 AST on changed production Python. Expect all remaining behavior PASS. Do not accept an exception from obsolete `discovery_window.py`/import or read of retired setting.

Commit only those retirement files with message `refactor: retire experimental custom discovery window`. Immediately run **full** `python -B -m unittest discover -s tests -q`, Python 3.8 AST production files, XML/PO parsing and `python -B scripts/verify_release.py`, now that HEAD matches deletions. Expected current runtime count drops by the removed module/resource automatically; report actual rather than forcing a stale constant.

- [ ] **Step 4: Independent task review**

Review exact deletion range, native UI behavior and source/public identity before starting selector refactor. The previous RED is eliminated by deletion of rejected scope, not committed as a binding fix or silent assertion weakening.

---

### Task 2: Add the local four-category selector with exact missing-source semantics

**Files:**
- Modify: `yawsp_app/discovery_ui.py`
- Modify root first entry: `yawsp_app/tmdb_ui.py` (`discovery_menu`)
- Test: `tests/test_discovery_ui.py`, `tests/test_discovery_integration.py`, and `tests/test_backup_db_retirement.py` only if its baseline first-root target is asserted

**Interfaces:**
- Consumes: Task 1 native-only builder with same signature and original catalog/source mappings.
- Produces: `tmdb_discovery` absent-source selector using existing `category_labels()`, `_url`/`url_builder` and native `DirectoryResult`; valid source uses original native `native(source_name, source, params)`.

- [ ] **Step 1: Write failing selector/root tests**

Prove `tmdb_discovery` with no `source` produces exactly four folder URLs in immutable order `popular_movies`, `popular_shows`, `top_rated_movies`, `top_rated_shows`, localized category labels, no API/config request/network/background/default-view and one directory completion. Root Objevit has no `source` parameter and remains first among existing five menu items.

Explicit source values `""`, non-string and `not_a_catalog` still fail before setting-network checks, and valid bookmarks still issue only their corresponding category list+genre.

Run `python -B -m unittest tests.test_discovery_integration.DiscoveryIntegrationTests.test_root_places_discovery_first_without_http_or_background_work tests.test_discovery_integration.DiscoveryIntegrationTests.test_missing_source_selects_four_categories_without_http -v`. Expected current missing-source directory failure and current root linking `popular_movies`.

- [ ] **Step 2: Implement the minimal selector dispatch**

In `discovery(params)`, establish `params = params or {}` as existing behavior; **only** if `"source" not in params` create the four localized rows with `url_builder(runtime, action="tmdb_discovery", source=<key>)`, `is_folder=True`, optional existing category title/icon, and successful plain `DirectoryResult`. Otherwise preserve existing string/mapping validation, then API-key guard and native result page. No generation/store/prefetch/default view at selector. `source=""` is explicitly not absence.

Change `discovery_menu()` only to omit `source=popular_movies` from the root link. Do not touch native page helper/view logic.

- [ ] **Step 3: Verify all four category input states and commit**

Run the failing selectors, full discovery UI/integration/resource suite and root retirement no-I/O cases. Confirm no new string IDs beyond retained 30441–30443 are needed and empty/raw unknown forms behave exactly as decided. Commit with `feat: add native Objevit category selector` (including required focused test changes only).

Immediately run complete `python -B -m unittest discover -s tests -q` and `python -B scripts/verify_release.py` on committed HEAD. Then independent native selector/data/security review.

---

### Task 3: Align docs, resource contracts and historical evidence

**Files:**
- Modify: `README.md`, `tests/test_documentation.py`
- Modify only if needed for actual current behavior: performance coverage/report references and Python 3.8 production/documentation AST checks
- Do not change `addon.xml` news/version/requirements; do not alter previous failed design-plan facts

**Interfaces:**
- Consumes: Task 1 removed native factory/custom experiment and Task 2 actual selector/page/Webshare behavior.
- Produces: accurate native-first end-user setting/installation experience with no dead `discovery_native_wall` instructions or custom-ui as-delivered claim.

- [ ] **Step 1: Add failing documentation tests**

Assert README describes root Objevit → native four-category selector → native InfoWall after selection, four exact APIs, query bounds, canonical Webshare largest search, no active `discovery_native_wall` toggle/WindowXML/Netflix custom or script presentation as current behavior. Historical failed experiments must remain described only as such; `<news>` stays factual 2.0.0.

Assert performance coverage active tests reflect actual native selector and no custom-window-only aliases/settings. Do not remove static-root no-I/O or public metadata safeguards.

- [ ] **Step 2: Update prose/news-surrounding documentation honestly**

Replace default-custom user-facing text with native four-category selector and existing ordinary InfoWall (only prose; no view-mode rewrite). Explain the rejected custom experiment is retired with 30444 setting. Preserve `refresh=1`, metadata, CI, durable backup and Kodi19/lifecycle unsupported claims already accepted.

Record that a separate active-background shutdown HANG persists despite new native UI passing; never imply integration approval. No version/runtime/minimum Kodi API changes. Update performance evidence counts only after executing the full active selector list and retaining actual log.

- [ ] **Step 3: Commit then full resource/documentation/package validation**

Run focused doc/resource tests, commit `docs: describe native-first Objevit`. Then run full unittest discovery, Python 3.8 AST, XML/PO, `git diff --check` and release verifier. Independently review that docs are aligned to actual native behavior and no stale custom unverified "tested" promise exists.

---

### Task 4: Validate exact native selector in isolated Kodi and final integration evidence

**Files:**
- Create only outside repo: new temporary root `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-native-first-validation\` with harness/profiles/reports
- Modify only ignored test report(s) in the current native-first SDD workspace; source changes only if independent GUI proof produces a separate regression

**Interfaces:**
- Consumes: complete Task 1–3 committed native-only package.
- Produces: distinct native execution result versus the prior failed custom WindowXML and still-open active-background shutdown.

- [ ] **Step 1: Verify source/package clone isolation and runtime limits**

Reuse only the same checked cloned runtime family/provenance with exact expected clone SHA-256, excluding all old portable_data/old plugin data. Deploy exact latest committed package and use synthetic loopback TMDB/Webshare/artwork only. New fresh profile and addon discovery; main 9090/profile/binary/process untouched. Confirm package contains no discovery_window/script or custom-settings declaration.

- [ ] **Step 2: Execute actual four-selector/native-page scenarios**

Use real ActionRunner root and Input operations, capturing requests only as method/path/status, with session-id markers never logged as values:
1. Root six items, then Objevit four localized selector rows, no HTTP/background activity.
2. Every one of four category selections has one list request with correct path; total shared genre traffic ≤1 per media type. No account/pagination/trailer/show/prefetch. Title/year/genre/rating/plot and artwork are real production values with loopback art.
3. Real InfoTag read-back exact for movie and TV rows. Default view follows existing Estuary one-time condition, not an every-open forced view.

Select one target in both media types and prove `action=search&what=<encoded localized title>&sort=largest` at existing Webshare loopback. Verify original-title context if activated, without autoplay/watched effects.

- [ ] **Step 3: Validate non-happy input/public identity/no legacy UI**

Empty response produces 30440 and zero genre. 503 follows normal localized failure. Valid-source 401 preserves personal session/account and sanitized logs. Missing key fails before HTTP with settings notification. Public list and genre have API key+locale and no session_id. Unknown explicit source fails local, never selector (that would conflate missing with unknown), and missing key opens selector still without HTTP.


Greps/evidence prove no legacy `discovery_window`/`system_localized` or script-generated route appears in deployed logs/source. Store actual counts, readbacks, method paths, completion ordering and source hashes in the run JSON. Do not assert actual texture geometry/poster/focus paint from list metadata alone.

- [ ] **Step 4: Final verification and blocked-lifecycle conclusion**

Use full suite, Python 3.8 grammar, PO/XML, `git diff --check`, release verifier and active performance/security/DevOps review for actual new commit/diff. Compare active-background shutdown against existing recorded HANG and do not declare it fixed; no daemon/socket/service patch is allowed. An idle controller clean exit is not presented as a background-lifecycle fix.

Return separate `NATIVE UI PASS/FAIL`, `PUBLIC PRIVACY PASS/FAIL`, `PACKAGE/OFFLINE PASS/FAIL` and `LEGACY ACTIVE SHUTDOWN RESULT` with links to individual source-backed evidence. Any native regression returns to its owning task with failing test and no release action. Ask user for final integration/release decision rather than merging automatically.
