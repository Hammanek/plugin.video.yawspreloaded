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
- README documents metadata API migration, manual `refresh=1`, cache explanatory label, added empty-state meaning, CI verifier, and durable backup location separately from Temp.
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

### Task 8: Add separate TMDB Objevit gallery

**Files:**
- Create `yawsp_app/discovery_ui.py`, `tests/test_discovery_ui.py`
- Modify `yawsp_app/tmdb_ui.py` main menu only and all three catalogs add **30441 Discover**, **30442 Popular**, **30443 Top rated**
- Test `tests/test_resources.py`, main-menu/entrypoint contracts

**Interfaces:**
- `build_discovery_handlers(runtime, client, url_builder, genres, set_default_view)` returns `{"tmdb_discovery": handler}`.
- Four immutable local mappings: `popular_movies`→`/movie/popular`, `popular_shows`→`/tv/popular`, `top_rated_movies`→`/movie/top_rated`, `top_rated_shows`→`/tv/top_rated`; only selected `client.get(path)` plus optional shared genre lookup. No personal scope or snapshot.
- Webshare result is `action=search, what=localized_title, sort=largest` via existing `_url`; original-title context uses 30414 where useful. No auto-play/watch/queue operations.
- Existing Task 2 `apply_video_metadata` supplies native tags and proper genre list. Runtime can use root menu plus now 7 entries (five webshare + TMDB Watchlist + TMDBObjevit as selected by user).

- [ ] Add failing gallery tests: handler registered; four exact endpoints/non-null title/year/rating/plot/data; selected artwork genre/list metadata; ordinary+original Webshare destination; unknown source returns failed directory; one selected page no pagination/metadata/refresh/identity-resolution calls; missing key local settings flow and error path.
- [ ] Implement small API/UI builder and one extra TMDB root menu entry beside existing watchlist. Add 30441/42/43 (including proper CS/SK translations) without modifying existing resource identifiers.
- [ ] Verify literal correct public TMDB endpoint mapping, no external input path construction, exactly one selected catalog call and no extra queued work or counts compared to existing operations. Perform all focused/full suites and independent spec+quality review.

### Task 9: Integrated isolated Kodi validation and final reviews

**Files:** all task diffs + temporary deployment/test fixture and exact CI-style package. No release work unless separately approved.

- [ ] Run full discovery, Python 3.8 AST parser, PO/XML parse, workflow lint-like checks, release verifier and git diff check.
- [ ] Deploy current code to a New isolated native Kodi 21.2 Store profile copy. Verify enabled version, all six previous main-menu items plus separate `Objevit` (7 items total), actual popular/top-rated metadata and Webshare destinations from a controlled endpoint, actual native InfoTag local metadata, manual refresh from a controlled endpoint, old complete data on failure and no `setInfo` deprecation line from this addon. Probe shutdown using Task 1's same method so before/after is comparable. Do not force quit the real main profile or print account data.
- [ ] Independent final functional/performance/DevOps review of actual diff, witnesses valid URL/main lifecycle; no unsupported device/liveness claims. New Important findings return to owning task with a failing test.
- [ ] Present exact final commit/test/live results and ask for integration/release choice; do not merge/push/tag/release automatically.
