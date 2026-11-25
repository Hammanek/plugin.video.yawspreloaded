# Kodi Usability Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Checklist `- [ ]` tracks the state.

**Goal:** Deliver shutdown diagnostics, modern video metadata, explicit watchlist refresh and UX clarity, offline CI/release checks, accurate documentation and permanent backups with evidence.

**Architecture:** Isolate an actual Kodi 21.2 portable profile and a local HTTP fixture for network/shutdown investigation. Use one narrow metadata adapter, reuse guarded TMDB watchlist operation paths and add only small local resources/test helpers. All features get focused RED→GREEN tests plus independent task and final reviews.

**Tech Stack:** Python 3.8+ standard library, unittest, Kodi 21.2 x64 Store, JSON-RPC, GitHub Actions, requests already required by the addon.

**Spec:** `docs/superpowers/specs/2026-11-25-kodi-usability-improvements-design.md`

## Global Constraints

- Preserve public entry point, existing routes/settings IDs and account/session/scoped snapshot/atomic I/O behavior. Only additive `refresh=1` is allowed.
- Manual refresh is the only new network trigger. No page/source behavior may silently produce progressive partial results or duplicate post-completion refresh work.
- No new app runtime dependency, live production credentials, main-profile shutdowns during development, broad timeout reductions or undocumented background work.
- `main` is not merged, pushed, tagged or released automatically. Backups copied outside worktree are explicit user-approved write actions with no deletion.
- Data/log output contains only types and counts, never real tokens/passwords or session IDs. Source changes parse under Python 3.8; only **Kodi 21.2 (Omega), Windows Store x64** is claimed live-tested.
- Tests use `python -B -m unittest discover -s tests -v` and focused exact unittest selectors. Every implementation follows an observed failing test before production edits.

## Review Focus

- Actual adapter names/types: `InfoTagVideo.setGenres` takes list, `duration` remains seconds, `aired` maps to first-aired.
- Explicit refresh must preserve prior complete snapshot on transport/internal failure and no duplicate queued refresh after successful immediate update.
- New `refresh=1` calls keep identity capture and exact scope; non-route/invalid/cancel flows complete only once.
- CI cannot mutate git state, use mutable action tags or need live credentials.
- Archive payload must begin with 25 pre-plan runtime files beneath `plugin.video.yawspreloaded/`, adding only `metadata.py` and `discovery_ui.py` when created; no tests/docs/workflow or `__pycache__` may leak into it.

---

### Task 1: Diagnose actual shutdown behavior, then fix only if evidenced

**Files:**
- Read: `yawsp_app/router.py`, `yawsp_app/tmdb.py`, `yawsp_app/download_jobs.py`, existing concurrency/monitor tests.
- Temporary only: `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-shutdown-probe\` (portable Kodi + addon + local HTTP fixture).
- Modify only if a repro proves the owning defect: responsible source/test above.

**Interfaces:** Actual owner `close()` → `background.close(wait=False)` and a controlled local delayed HTTP call. Synthetic values only, no real main-profile process/profile.

- [ ] Record baseline log: `Stopping the application` → `Stopping player` → settings saves → `Application stopped` → five-second Python thread warning. Do not call this an addon bug without a causal controlled path.
- [ ] Create a portable app copy outside repository with only test settings and current addon. Point HTTP constants only in the temporary copy at localhost, serve deterministic valid XML/JSON with no credentials. Do not run the portable app with the original main `portable_data`.
- [ ] Run separate current-code probes: idle no pending work, one delayed metadata fetch, whole watchlist fetch, queue of local DB/media placeholder operations if relevant. Capture request entered, response delayed/return, status parsed, monitor abort seen, worker session/request/instruction completed and owner close returned. No token/time fields output.
- [ ] Reproduce graceful closed-loop behavior with local server tests, record all thread family IDs and last completed stage. Start/stop only the created test process. Any failed close that passes beyond its known network state is evidence for source trace, not a blanket latency complaint.
- [ ] If root cause is established, write the named minimal RED regression and prove it before editing source. If no reproducible code defect is established, keep production unchanged and document exact outcome/limited evidence. Review investigation and any corrective diff before the next task.

### Task 2: Migrate current video metadata to native InfoTagVideo API

**Files:**
- Create: `yawsp_app/metadata.py`
- Modify: `yawsp_app/webshare_ui.py`, `yawsp_app/tmdb_ui.py` only current video `setInfo("video", ...)` calls
- Test: `tests/test_metadata.py`, existing webshare/TMDB row contract tests, Kodi stubs used by task

**Interfaces:**
- `apply_video_metadata(listitem, info)`: obtains `getVideoInfoTag()` and applies six known forms plus title/media type.
- Supported keys: `title`→`setTitle`, `mediatype`→`setMediaType`, `plot`→`setPlot`, `year`→`setYear`, `genre`→`setGenres`, `rating`→`setRating`, `season`→`setSeason`, `episode`→`setEpisode`, `duration`→`setDuration`, `aired`→`setFirstAired`.
- A string genre splits on ` / ` into list; nonempty list/tuple is copied. Other current input values stay unchanged. Do not invent setters/fallback names.
- If `getVideoInfoTag()` is absent (legacy test double), explicitly call existing `listitem.setInfo("video", info)` once.

- [ ] Add failing adapter tests covering all supported real handler keys and exact argument types (`duration=3600`, `season=2`, `episode=4`, `year=2026`, `rating=7.25`, genre list). Prove current production lacks adapter rather than a test doubles error.
- [ ] Implement the small helper; replace all and only current seven production `.setInfo("video", ...)` sites with it, no other item labels, cast, artwork or play behavior.
- [ ] Add focused real row tests for Webshare title/plot metadata, season number+count, episodes air date/duration and watchlist genre/rating. Check stub still reproduces old fallback intentionally, not an invented fake `InfoTagVideo` API.
- [ ] Run focused tests and full suite; independent review validates actual setters were checked against Kodi public docs, not stub expectations alone.

### Task 3: Add explicit watchlist refresh, local-cache label and Webshare empty-state UX

**Files:**
- Modify: `yawsp_app/tmdb_ui.py:495-755`, `yawsp_app/webshare_ui.py:610-645` and their tests
- Modify all 3 language catalogs: add **30438 Refresh**, **30439 Local cache**, **30440 No matching files found** (skilled localized Czech/Slovak text, no renumbering)
- Test: `tests/test_tmdb_ui.py`, `tests/test_webshare_ui.py`, `tests/test_resources.py`

**Interfaces:**
- Existing `action=tmdb_watchlist&category=movies|shows&refresh=1` forces full explicit fetch even when compatible snapshot exists.
- Successful manual fetch saves and renders complete new snapshot with its captured generation and does not queue an unnecessary immediate duplicate background refresh. Ordinary exact non-manual calls keep their existing submission behavior and warm call counts.
- Category status for an existing snapshot contains localized local-cache text; failed manual query keeps old complete snapshot as the durable fallback and uses normal listing error reporting. No `Container.Refresh` before write completes.
- The Webshare empty-state is a row only when the valid page yields zero displayed files; empty local search menu/cancel stays as before. Auth remote boundaries unchanged.

- [ ] Add tests for manual refresh, failed page fallback, no duplicate watchlist worker call, cache label, empty filtered search + existing pagination, and all three locale IDs. Prove intended missing/failing current behavior before production code.
- [ ] Implement the optional flow, unconditional final refresh row, status text and localized empty-state only.
- [ ] Prove unchanged warm snapshot `0 account/watchlist API calls`, no duplicate background handoff after manual save, and unchanged snapshot generation/peers/error semantics. Run complete suite and review.

### Task 4: Add offline CI and deterministic archive verifier

**Files:**
- Create: `.github/workflows/tests.yml`
- Create: `scripts/verify_release.py`
- Test: `tests/test_release_verification.py`, `tests/test_devops_workflow.py`

**Interfaces:**
- CI: `permissions: contents: read`, push and pull_request, Python 3.8/3.12 matrix. `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`, `actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c` exact SHAs. Install `requests==2.32.3` (test/tooling only) then full discovery + verifier.
- `verify_release.py`: reads `addon.xml`, README and selected current git runtime filenames, builds a temp ZIP beneath a single matching `plugin.video.yawspreloaded/` root and validates 1) version badge/setup presence, 2) exact file allowlist and current runtime count (25 before this plan, plus metadata and discovery as added), 3) CRC and every zipped byte equals source. No test/docs/workflow/SDD/pycache or credentials are allowed. Print only counts/version/hash, exit nonzero on failure.
- Workflow parse test inspects YAML text semantically enough to assert permissions, triggers, full 40-character lowercase SHA action refs, no token-write permissions, actual test commands and no release/push.

- [ ] Add failing path/XML/archive tests: valid current root passes; wrong root or extra file invalid; missing metadata.py if added becomes failure. Prove proper current manifest+README while gate catches both filename bytes and source differences.
- [ ] Implement verifier + workflow, export a temporary CI archive, run its checks and full suite including resource contracts. No global git or release creation.

### Task 5: Finalize `<news>`, documentation and actual-version compatibility claim

**Files:**
- Modify: `addon.xml` news only, `README.md`, `tests/test_documentation.py` as needed for new evidence.
- Never change application settings schema, API dependency version or current release version as part of this task.

**Interfaces:**
- `<news>` is concise factual XML content for already-released **2.0.0**; no fake later version.
- README documents metadata API migration, manual `refresh=1`, cache explanatory label, added empty-state meaning, first-position Objevit with four categories and explicit ordering, reversible custom Netflix / native InfoWall style (including next-open semantics and native-wall setting), Backup DB removal without user-data deletion, CI verifier, and durable backup location separately from Temp.
- Exact live claim **Kodi 21.2 (Omega), Windows Store x64** vs Python 3.8 static compatibility; no claim Kodi 19 was actually started.

- [ ] Before editing run a focused documentation/environment claim check capturing required missing assertions without merely removing them.
- [ ] Update user-facing features, backup resilience guidance and CI docs; include honest lack of actual playback/test of Kodi 19/ARM/SMB/NFS. Parse real XML and PO files strictly as UTF-8, check README/XML version agreement and setup links.
- [ ] Run focused resources/docs + full discovery and independent prose/config review.

### Task 6: Preserve backups in permanent profile storage

**Files:**
- Read only hashes of existing `C:\Users\jkrat\AppData\Local\Temp\opencode\yawsp-main-profile-pre35b4e4a.zip` and `yawsp-main-profile-pre2.0.0-manifest.zip`.
- Copy to `C:\Users\jkrat\AppData\Local\Packages\XBMCFoundation.Kodi_4n2hpmxwrvr6p\LocalCache\Roaming\Kodi\userdata\backup\plugin.video.yawspreloaded\`.
- No source-repository changes.

**Interfaces:** Only copy existing approved files and preserve original locations/bytes. Do not read/archive/out-print token fields or expand credentials.

- [ ] Ensure destination folder can be created; if an exact-named target exists, compare SHA-256 and abort on difference instead of overwriting user work.
- [ ] Copy only the two approved backups; verify both source and destination SHA-256 and same bytes. Check no archive contents are printed.
- [ ] Record target path and non-destructive validation for user/documentation. Do not delete Temp originals.

### Task 7: Retire obsolete Backup DB feature and its handlers

**Files:**
- Delete `yawsp_app/backup_db.py`, `tests/test_backup_db.py`
- Modify `yawsp_app/webshare_ui.py`, `yawsp_app/download_jobs.py`, `resources/settings.xml` only if obsolete UI settings are actually present, all three PO catalogs only for DB-only IDs 30434/30435/30436, `README.md`, `.superpowers/sdd/2026-11-25-performance-remediation/performance-coverage.json`, `tests/test_documentation.py` and relevant Webshare/entrypoint tests; retain unrelated queue/history tests.
- Create focused retirement regression `tests/test_backup_db_retirement.py`.

**Interfaces:**
- No `BackupDBStore` import/instantiation, Backup DB root label, DB installer/worker, `db` or `searchdb` handlers/actions, no text download/read for the old feature.
- `DownloadJobKey` retains **media** only, and only its unrelated job lifecycle tests/behavior. No `backup_db_job_key` calls remain.
- Explicit retired `db`/`searchdb` requests fall through existing root menu without `user_data`, authentication, database parsing or network. No arbitrary redirect or user-file destruction.
- Historical performance coverage marks obsolete perf requirement as `removed` instead of pretending its symbols exist; don't delete evidence of unchanged history or regressions that cover media data.
- User `database*`/JSON TXT files and permanent backup archives stay untouched; `close` and real download completion remain included.

- [ ] Add failing regression with actual main menu and route/handlers before any deletion. Verify logout/token would not be called on retired routes, exactly no remaining DB UI/job namespaces and existing media path can still queue/close/complete. Also verify obsolete IDs resolve or are removed consistently across locale parser, no dead `backup_db` imports/symbol maps.
- [ ] Delete only retirement-specific files/branches/test cases and update docs/performance map. No opportunistic rewriting of generic media download or shared worker policy.
- [ ] Run test discovery and an independent removal review: no old actions can silently hit code missing its now-deleted dependency; legacy bookmarks return a benign directory, not a trace.

### Task 8: Add separate TMDB Objevit gallery

**Files:**
- Create `yawsp_app/discovery_ui.py`, `yawsp_app/discovery_window.py`, `resources/skins/default/720p/DiscoveryWindow.xml`, `tests/test_discovery_ui.py`, `tests/test_discovery_window.py`
- Modify `yawsp_app/tmdb_ui.py` menu/handler registration, `yawsp_app/router.py` for a **default-first** discovery entry before prior main menu items (no completion changes), `resources/settings.xml` for additive `discovery_native_wall` default false, and catalogs add **30441 Discover**, **30442 Popular**, **30443 Top rated**, **30444 Use native wall in Objevit**
- Test `tests/test_resources.py`, main-menu/entrypoint/settings and performance contracts

**Interfaces:**
- Immutable local mappings: `popular_movies`→`/movie/popular`, `popular_shows`→`/tv/popular`, `top_rated_movies`→`/movie/top_rated`, `top_rated_shows`→`/tv/top_rated`; only selected `client.get(path)` plus optional shared genre lookup. No personal account/snapshot and no page loop.
- `build_discovery_handlers(runtime, client, url_builder, genres, set_default_view, window_factory)` receives existing helpers plus injected native custom window; factory can be locally replaced by semantics-faithful Kodi double, never inventing a fake unsupported API.
- Shared result model supplies title/original, year, genre list, rating, plot, poster/thumb/backdrop and native info tags. Result `action=search, what=localized_title, sort=largest` using existing `_url`, plus optional original-title search 30414. No autoplay/watch/queue/trailer calls.
- Default false uses custom Netflix-inspired native Kodi UI: category buttons, selected-item backdrop, metadata, explicit focus/grid visuals, loading/empty/error states, left/right/up/down, Enter and Back. One expected modal/window close per route, including report/recovery/error; no heavy animation or new framework. Inspect supported native controls from real 21.2 binding before choosing exact names.
- Additive setting `discovery_native_wall=true` reuses native InfoWall; it never opens custom factory. Switching setting only changes the next opening, not watchlist, and doesn't require reinstall. Provider route with unset value starts with user-selected custom default.
- Root is **Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist** after retirement. Static root does no discovery HTTP.

- [ ] Add failing endpoints/menu tests for all four sources plus safe unknown source, field mapping and Webshare target; verify exactly one selected list call at most one shared genre call, no identity resolution, pagination, media metadata refresh or trailer lookup.
- [ ] Add failing presenter tests for four categories and focus state, loading/empty/error, custom close-on-back (exactly once), Webshare activation once via Enter and view switch → next native directory. Show phoning, not static screenshots, through source-backed documented controls and test callback invocations. Ensure user keys cannot produce a second close/search call.
- [ ] Implement small bounded UI/data/helper, its native resource XML, selected presentation and first root entry without replacing old watchlist. Add user-selected CS/SK/EN localization and new boolean in TMDB settings.
- [ ] Verify route/completion, context menu original-title and existing genre/InfoTag names on native grid + custom window, never outside known immutable CDL. Prove API count independent of presentation and test full suite + independent UI/functional review.

### Task 9: Integrated isolated Kodi validation and final reviews

**Files:** all task diffs + temporary deployment/test fixture and exact CI-style package. No release work unless separately approved.

- [ ] Run full discovery, Python 3.8 AST parser, PO/XML parse, workflow lint-like checks, release verifier and git diff check.
- [ ] Deploy current code to a New isolated native Kodi 21.2 Store profile copy. Verify enabled version and root order **Objevit, Vyhledávání, Chci si stáhnout, Historie stahování, Nastavení, TMDB Watchlist** (6 after DB retirement). Verify custom Netflix view, reversible native wall, focus navigation and Webshare destinations from a controlled endpoint, actual native InfoTag local metadata, manual refresh/failure behavior and no `setInfo` deprecation line from this addon. Probe shutdown using Task 1's same method so before/after is comparable. Do not force quit the real main profile or print account data.
- [ ] Independent final functional/performance/DevOps review of actual diff, witnesses valid URL/main lifecycle; no unsupported device/liveness claims. New Important findings return to owning task with a failing test.
- [ ] Present exact final commit/test/live results and ask for integration/release choice; do not merge/push/tag/release automatically.
