# Performance Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement all nine performance-audit remediations on `functional-remediation` with measurable UI relief and no weakening of the existing 192-test functional, integrity, localization, or Kodi completion contracts.

**Architecture:** Add focused snapshot, bounded-job, and validation-signature services around the existing modular implementation. Warm TMDB views use complete account/language-scoped snapshots while workers refresh valid data; cache writes and I/O coalesce; media and cold Backup DB installation use dedicated bounded jobs. Cold/global-sort correctness and all lifecycle contracts remain authoritative over faster appearance.

**Tech Stack:** Python 3 / Kodi 19+, existing Requests/Unidecode/bundled modules, AtomicJsonCache, ThreadPoolExecutor (bounded), standard-library `unittest` and deterministic operation/timing benchmarks.

**Spec:** `docs/superpowers/specs/2026-11-25-performance-remediation-design.md` (approved by the user on 2026-11-25).

## Global Constraints

- Remain on the existing isolated worktree branch `functional-remediation`; no merge, push, tag, release, version bump, new runtime dependency, or live account/API/Kodi verification.
- `main.py`, `yawsp.router`, existing public routes/setting IDs/language fallback, safe local/VFS replacement, complete global Czech/rating/year order, and existing 192 tests are compatibility baselines.
- `python -m unittest discover -s tests -v` must pass after every task with pristine output. All new tests are offline/deterministic. Use no arbitrary millisecond assertion on shared hardware; controlled event handoffs/counts are authoritative and timings are repeated medians/ranges.
- Every task performs strict RED→GREEN functional regression work and records before/after counts or benchmarks in its SDD workspace. A faster response never means the underlying install/download/refresh has already succeeded.
- Process/background ownership is single and explicitly closed. Worker sessions and jobs have no nested directory/resolution completion. No service module adds `xbmc*` imports.
- User accepted stale-while-revalidate snapshots. Identity/account isolation, full result validation, failure preservation and watched invalidation are non-negotiable.

## Review Focus

- **Account/session race:** warm snapshot key must never trigger an HTTP account lookup or serve another account; identity changes during a refresh cannot overwrite the wrong scope. Task 2 pins this with account-switch and current-result revision tests.
- **Queue/worker lifetime:** plugin returns early while stream/install continues; close, rejected duplicate, failure-before-next-job, and continuation must not strand work or report success. Tasks 1, 3, and 5 test reservations, continuation, abort and close behavior.
- **Changed persistence:** legacy/malformed/partial snapshot or DB signature and concurrent/externally modified files must force recovery, not faster unsafe acceptance. Tasks 1–4 retain mutation-sensitivity and prior-byte preservation.
- **Ordering/scope of global results:** no first-page-only alphabet/rating/year output, partial refresh installation, or semantic loss from deduplication. Tasks 2 and 6 assert global sorting and normal dictionary-equality behavior.
- **Resource and memory honesty:** network zip is streamed but one JSON member still buffers during parse; VFS rename atomicity and real ARM throughput are unverified. Tasks 4 and 5 assert streaming boundaries and report—not hide—the remaining limits.

---

### Task 1: Coalesced TMDB cache reads/writes and post-complete work

**Files:**
- Modify: `yawsp_app/tmdb.py`, `yawsp_app/tmdb_ui.py`, `yawsp_app/json_cache.py` only if a narrow shared batch primitive is warranted
- Modify: `tests/test_tmdb.py`, `tests/test_tmdb_ui.py`, `tests/test_json_cache.py`
- Add to initial commit: `docs/superpowers/specs/2026-11-25-performance-remediation-design.md`, `docs/superpowers/plans/2026-11-25-performance-remediation.md`

**Interfaces:**
- Produces backward-compatible submission `BackgroundPrefetch.submit_trailers(media, media_ids, missing_known=False)` and `.submit_shows(show_ids, missing_known=False)`; `missing_known=True` suppresses duplicate discovery reads but preserves per-key reservation/validation.
- Produces worker-only deferred persistence with immediate on-demand lookup saving unchanged and flush after at most eight validated updates (job finalization flushes dirty values).
- Produces `DirectoryResult.post_complete` composing submission with existing view setup after items/list completion; `ActionRunner` stays unchanged if possible.

- [ ] **Step 1: Write failing operation/behavior tests**
  Full 32 trailer or show updates currently cause 32 save/fsync paths; require <=4 flushes at checkpoint 8, success-only dirty saves in finally, immediate on-demand compatibility, no same-cache discovery read when `missing_known=True`, and submission only after endOfDirectory alongside the Estuary one-time view hook. Cover abort/final failure and data already persisted.
- [ ] **Step 2: Run focused tests to obtain baseline failures and operation counters**
  Record pre-change flush/read counts against synthetic N=1/8/32. Do not guess or rely on wall-clock thresholds.
- [ ] **Step 3: Implement deferred worker persistence and discovery reuse**
  Preserve immediate repository client lookups, success/Legacy/TTL/stale/error contracts, cache locking, atomic replacement and each worker’s own session. Compose `post_complete` so nothing can overwrite or suppress another existing completion callback.
- [ ] **Step 4: Re-run full discovery and repeated counter benchmark**
  Prove <=4 worker flushes per 32 and no submission before end. Compare median operation counts with the initial benchmark; do not weaken assertions for concurrency behavior.
- [ ] **Step 5: Commit Task 1 and approved docs**
  `git add yawsp_app tests docs/superpowers/specs/2026-11-25-performance-remediation-design.md docs/superpowers/plans/2026-11-25-performance-remediation.md && git commit -m "Coalesce TMDB metadata cache and scheduling"`.

### Task 2: Persistent watchlist snapshot with background refresh/genres

**Files:**
- Create: `yawsp_app/watchlist_snapshot.py`
- Modify: `yawsp_app/router.py`, `yawsp_app/tmdb_ui.py`, `yawsp_app/tmdb.py`, `tests/test_entrypoint.py`, `tests/test_tmdb_ui.py`, `tests/test_tmdb.py`
- Create: `tests/test_watchlist_snapshot.py`

**Interfaces:**
- Produces validated snapshot `load(scope) -> Snapshot | None`, `save_complete(scope, results, created_at) -> Snapshot`, `invalidate(scope)`, `refresh_due(scope, now)`, and `remove(scope, media_id)`.
- Scope keys include account ID, media (`movies|tv`), TMDB language and schema version only—never api/credential tokens. Lifecycle/background uses a snapshot-scope reservation and fresh worker session so no UI account/watchlist lookup is required for compatible warm identity.
- Consumes Task 1 post-complete/miss reuse, repository and owner close interfaces.

- [ ] **Step 1: Write failing warm/cold/identity tests**
  Warm valid record: zero account/watchlist calls in UI, immediate rendering with old revision while refresh is queued after completion, complete global Czech/rating/year sorting and empty-result skip of genres.
  Cold empty/malformed/incompatible record: existing complete page fallback; no save on failed page; no first-page-only sort, duplicate-page accumulation or partial replacement.
  Account/session switch, changed language/media, watched success/failed removal, refresh failure, duplicate opens and refresh race: assert no cross-account presentation, no wrong-scope save, proper invalidate/remove/retry and throttle default five minutes with injected clock.
  Process genres: one request for same `(media,language)` across invocations, no concurrent duplicate and fresh mapping after language change.
- [ ] **Step 2: Run focused tests and capture baseline network/request failures**
  Record synchronous warm-path request counters before new snapshot behavior versus required warm path; cold path remains functionally equivalent.
- [ ] **Step 3: Implement atomic snapshot service and shared genre repository**
  Persist only fully validated complete results using AtomicJsonCache; isolate per-account keys, validate per-item shape sufficiently for rendering, retain old snapshot on refresh errors, and make background refresh completion responsible for replacing the snapshot and exposing it on later open. Preserve all existing ListItem actions, cache metadata and completion.
- [ ] **Step 4: Run full discovery and warm/cold benchmark comparisons**
  Verify warm zero listing/account calls and cold pages/account+genres behavior, snapshot failure preservation, watched invalidation and both related suites green.
- [ ] **Step 5: Commit**
  `git add yawsp_app/watchlist_snapshot.py yawsp_app/router.py yawsp_app/tmdb.py yawsp_app/tmdb_ui.py tests && git commit -m "Add account-scoped watchlist snapshots"`.

### Task 3: Draining, fair and deduplicated metadata policy

**Files:**
- Modify: `yawsp_app/tmdb.py`, `yawsp_app/tmdb_ui.py`, `tests/test_tmdb.py`, `tests/test_tmdb_ui.py`

**Interfaces:**
- Produces `WATCHLIST_PREFETCH_LIMIT = 50` visible items per open with alternating trailer/show families; larger submissions form a continuation/draining plan rather than silently dropping key 33+.
- Produces shared in-flight reservation for background/on-demand lookups and per-key short bounded error retry backoff without negative-cache semantics.
- Consumes the Task 1 deferred cache and `missing_known` behavior and Task 2 visible snapshot.

- [ ] **Step 1: Write failing queue/retry/isolation tests**
  100 visible/metadata submissions: no discarded key, maximum active reservation remains 32, deterministic round-robin families, all drain without another UI open. Show persistent failing first 32 gets one bounded retry rather than monopolizing later submissions. Concurrent on-demand/worker same key results in one HTTP lookup; duplicate submissions and close release all reservations without stray writes or refresh.
- [ ] **Step 2: Run focused queue counters and verify baseline admission-drop behavior**
- [ ] **Step 3: Implement continuation/fair retry without increasing worker parallelism**
  Keep one owner and bounded active work, preserve clean cancellation and successful cache writes. Do not cache transport failures to solve starvation. Capture per-key attempt counts/results and the first 50 visible IDs rather than all historical watchlist entries.
- [ ] **Step 4: Run full discovery and operation counter comparison**
  Show complete eventual key coverage, <=32 active, shared lookup counts and fair-order behavior. Existing background no-refresh contract remains intact.
- [ ] **Step 5: Commit**
  `git add yawsp_app/tmdb.py yawsp_app/tmdb_ui.py tests/test_tmdb.py tests/test_tmdb_ui.py && git commit -m "Drain metadata queues fairly without duplicate work"`.

### Task 4: Warm Backup DB signature and streamed ZIP installation

**Files:**
- Modify: `yawsp_app/backup_db.py`, `yawsp_app/webshare_ui.py`, `tests/test_backup_db.py`, `tests/test_webshare_ui.py`
- Create if reuse helps other owners: `tests/support/archive_stubs.py`

**Interfaces:**
- Produces structural validation signature under profile outside the DB directory containing `(schema_version, relative_name, size, mtime_ns)` for every data file, persisted atomically.
- Existing `BackupDBStore.ensure_installed(archive_loader)` accepts current byte fixtures for compatibility and a new seekable/streamable production path; no public caller needs whole-response bytes.
- Root warm validation is O(F) metadata plus small signature load; subsequent `load()` parses only the selected file. Full install validation/extraction/recovery remains unchanged semantically.

- [ ] **Step 1: Write failing warm/migration/tamper and stream tests**
  Existing valid DB: first migration full parse/signature then new store instances use zero full JSON validation reads; root groups and target load parse only the target. Missing/extra/size/mtime change or corrupt marker revalidates fully and repairs/recreates. Legacy actual data bytes are not modified.
  Install: streamed archive response may not access `.content`, local seekable temporary ZIP is used, extraction proves CRC failure and no separate whole `testzip()` pass is necessary, temporary sources cleanup and ordinary full validation/rollback remain. Include mixed compressed files and a member bigger than one memory chunk; report—not hide—the peak of `json.load()` per member.
- [ ] **Step 2: Record baseline read counts/timing and confirm new contracts fail**
  Reuse 27-file/500-item fixture and count baseline ensure+load=55 JSON documents as the local offline comparison, not a target-device latency promise.
- [ ] **Step 3: Implement sidecar signatures and streaming install source**
  Preserve safe member checks, native zip CRC-on-read during full extraction, full structural validation before promotion, and old recovery paths. Any untrusted marker mismatch triggers full validation, never shortcut repair.
- [ ] **Step 4: Run full discovery and warm-vs-cold/migration benchmarks**
  Assert second process performs no complete source JSON parse except requested target, new external edits force full recheck, streamed sources are bounded, no testzip×2 decode work, and all previous corruption/recovery cases pass.
- [ ] **Step 5: Commit**
  `git add yawsp_app/backup_db.py yawsp_app/webshare_ui.py tests && git commit -m "Stream database installs and validate warm state by metadata"`.

### Task 5: Immediate accepted handoff for media and cold DB jobs

**Files:**
- Create: `yawsp_app/download_jobs.py`
- Modify: `yawsp_app/webshare_ui.py`, `yawsp_app/router.py`, `yawsp_app/runtime.py`, `tests/test_entrypoint.py`, `tests/test_webshare_ui.py`, `tests/test_runtime.py`
- Create: `tests/test_download_jobs.py`
- Modify: `resources/language/*/strings.po`, `tests/test_resources.py` only for new required internal status messages, starting at unused ID 30433

**Interfaces:**
- Produces `DownloadJobManager.submit(key, operation) -> bool` for typed job keys (media destination, profile DB install), statuses `queued/running/succeeded/failed/cancelled`, duplicate rejection, bounded queue, monitor abort, worker-owned HTTP session, idempotent close.
- `operation` runs real validation/revalidation/link/info/GET/stream/install and uses injected lifecycle notification callbacks; it never resolves directories/media. Database completion alone may refresh Kodi once after successful atomic promotion.
- Consumes `stream_download`, `BackupDBStore.ensure_installed`, snapshot owner lifecycle, all existing safe download/file/recovery contracts.

- [ ] **Step 1: Write failing handoff and job-state tests**
  Controlled-event test: legacy direct media invocation cannot finish before releasing a blocked stream; queued invocation must return accepted before release, then succeed only after actual promote. Duplicate destination/DB reject, quota, monitor abort, idempotent close, error-before-next-job and rejected submit cleanup are covered.
  Cold/missing DB: return one status item after accepted handoff, one profile-scoped install reservation, `Container.Refresh` exactly once after effective install, no refresh/success on failure, current directory reads avoid partial writes, retry remains possible. Warm installed DB needs no job/revalidation.
  Media: every previous HTTP failure/short write/invalid filename/local-VFS/filename-safe lifecycle contract still holds with worker-owned sessions and safe notifier protocol.
- [ ] **Step 2: Run focused event tests and capture old invocation-blocking result**
  Do not use sleep(100ms) as proof. Use blocked fake streams/install operations and event sequencing.
- [ ] **Step 3: Implement one bounded owner/job manager and wire real UI actions**
  Queue every expensive operation atomically with status reservation, capture only appropriate immutable job state per invocation, and close through `_ProductionOwner`. Notify queued state separately from actual complete/failure state. Only the new cold install completion refresh is permitted; metadata remains refresh-free.
- [ ] **Step 4: Run full discovery, resource checks and event benchmark**
  Prove no action remains blocked on full content/install/validate before accepted handoff, yet promotion/final notification occurs only after true work. All completion routes still finish exactly once and every existing download/DB safety suite is green.
- [ ] **Step 5: Commit**
  `git add yawsp_app/download_jobs.py yawsp_app/webshare_ui.py yawsp_app/router.py yawsp_app/runtime.py resources tests && git commit -m "Move media and database installs into bounded workers"`.

### Task 6: Webshare lazy auth, linear history and measured chunks

**Files:**
- Modify: `yawsp_app/webshare_ui.py`, `yawsp_app/downloads.py`, `tests/test_webshare_ui.py`, `tests/test_downloads.py`
- Create or extend: `tests/support/benchmarks.py` for reusable median operation/timing measurements without test-time flakiness.

**Interfaces:**
- Produces main `menu(params)` with no token request/auth. Installed Backup DB root/group/detail opens with no auth; explicit remote init/queue operations authenticate at use through Task 5 worker.
- Produces stable O(H) deduplication preserving the exact current whole-dictionary comparison semantics (canonical encoding/content, not lossy id-only shortcuts) and no unnecessary history re-fetch when no valid `download_id` was removed.
- `stream_download(..., chunk_size=256*1024)` default selected only if measured synthetic results support it; optional smaller/custom size remains accepted and memory stays streamed/bounded.

- [ ] **Step 1: Write failing auth/dedup/chunk contracts**
  Entry/menu and valid installed local DB must call no `user_data`, salt or login. Distant actions still validate. Canonical pair shape/order/duplicate data means the same first-occurrence deduplication as original. Removing absent/blank ID uses the current stored response without a second GET; positive IDs still clear then reload once.
  Baseline timings: H=20/200/1000 list-membership vs canonical actual helper. Chunk local/VFS 64/256/1024 KiB repeats medians/ranges for 1/8/64 MiB streams and writes, including short writes with no response.content.
- [ ] **Step 2: Run focused failures and save baseline counters/results**
  Record a faster chunk only when it does not regress integrity or memory semantics. If measurements are ambiguous, retain 64 KiB rather than force the 256 KiB default.
- [ ] **Step 3: Implement smallest auth/dedup/default adjustments preserving root behavior**
  No skip of API credentials on truly remote operation; no new persistently cached static data beyond current directory behavior. Set chunk size only after the direct repeated comparison supports it; test exactly reduced number of write calls and identical data.
- [ ] **Step 4: Run full discovery and measured before/after history/chunk benchmarks**
  Confirm zero menu/installed-DB calls, linear deduplication/no redundant history reload and the selected memory/write compromise. Do not claim live NFS/SMB speedups without data.
- [ ] **Step 5: Commit**
  `git add yawsp_app/webshare_ui.py yawsp_app/downloads.py tests && git commit -m "Reduce Webshare navigation cost and history overhead"`.

### Task 7: Performance proof, documentation and whole-suite verification

**Files:**
- Modify: `README.md`, `tests/test_documentation.py`
- Create: `tests/test_performance_contracts.py` only if a check does not naturally belong in existing regression modules
- Update only via verified evidence: SDD benchmark/coverage artifacts; do not rewrite requirements to cover gaps.

**Interfaces:**
- Consumes all completed optimized contracts.
- Produces documented offline performance expectations and mapping PERF-01…PERF-09 to actual production methods + actual repeated test/benchmark results. Published state remains **not released** and version `1.3.15`.

- [ ] **Step 1: Write failing documentation/evidence requirements**
  README must name the new snapshot/job responsibilities, retain QR/setup/release/version and explain that accepted queued work is not completed work. For each PERF ID require concrete named regression/benchmark/evidence selector, not merely a summary claim. Run against old docs and confirm failures.
- [ ] **Step 2: Update documentation without promising live hardware latency**
  Document warm snapshot eventual consistency, cold fallback/invalidation, background media/DB jobs, warm DB validation, bounded worker ownership, streaming refresh safety and how to run benchmarks/tests.
- [ ] **Step 3: Run all PERF-specific baselines and optimized proofs**
  Execute repeated medians/ranges for history/cache/DB/chunks and controlled no-request/order/event handoffs for snapshot/auth/job completion. Map all nine IDs to at least one mutation-sensitive behavior and one measured operation improvement or an explicitly honest measured no-change with reason. No changing target assertions to imply savings without the data.
- [ ] **Step 4: Run complete regression/static suite**
  `python -m unittest discover -s tests -v`, recursive AST `utf-8-sig`, `addon.xml`/settings XML, resources, `git diff --check`, service no-`xbmc*`, and facade/owner-composition checks. Confirm snapshot/lifecycle/corruption/error recovery paths and no residual direct synchronous cold install/whole media path.
- [ ] **Step 5: Commit final documentation and verified residual fixes**
  Commit as `Document performance remediation evidence`.

---

## Task invariants

- No concurrent implementation subagents within one task; later dispatches receive exact produced interfaces via their brief, not whole historical summaries.
- Every review gets a diff package and checks evidence fidelity as well as functionality. Unproven optimization claims are findings.
- Full discovery runs after every task. A regression or removed old-public behavior blocks the next task.
- No live-media transfer or actual Kodi invocation is claimed from offline adapters. No release/tag/version change is performed by this plan.
