# Bounded Native TMDB Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy per-invoker TMDB executor and competing cache/snapshot writers with one bounded native Kodi service that preserves the validated UI and cleanly unwinds active shutdown.

**Architecture:** Native plugin callbacks become read-only cache/UI consumers of typed operations; one direct `xbmc.service` invocation owns serial transport and every TMDB mutation. An OS-locked epoch manifest provides atomic 32+1 admission, bounded backlogs, same-key waiter/result sharing, durable follow-ups, idempotent terminal handling and bounded ACK/GC without transporting closures or credentials.

**Tech Stack:** Python 3.8-compatible standard library, existing Requests/Unidecode, `unittest`, `xbmc.service`/`xbmc.Monitor`, Windows `msvcrt` and POSIX `fcntl` lock adapters, atomic JSON files, synthetic offline/loopback fixtures.

**Spec:** `docs/superpowers/specs/2026-11-26-tmdb-shutdown-hardening-design.md` (requires builder approval before Task 0).

## Global Constraints

- Work only on isolated `shutdown-hardening` from `051893e`; no change to `main`, `usability-improvements` or the main Kodi profile. No merge, push, tag, release, version bump, deployment or installation without separate approval.
- **Task 0 must pass before any repository production file changes.** A failed/unobserved native gate stops implementation. Prototype success does not count as final-artifact acceptance.
- Profile-global limits are exact: **32 background reservations + 1 independent interactive reservation**, **64 background backlog records**, **8 independent interactive backlog records**, **128 total jobs**, **128 waiters**, **32 unwaited terminals and 32 expiry tombstones**, **256-KiB manifest**, **1-MiB/result and 16-MiB aggregate result bytes**. Never evict a live reservation or registered unread result.
- Only named `metadata.trailer`, `metadata.show`, `watchlist.refresh`, `watchlist.watched`, `discovery.catalog`, `details.seasons`, `details.episodes` and `identity.invalidate` operations are admitted. No callable/closure, raw credential, arbitrary URL/path or executable method name crosses the protocol.
- The service creates no secondary application Python thread. Parent service-managed paths have no HTTP/executor/worker-session/cache-write fallback. `DownloadJobManager` remains independent.
- Preserve the exact six-item root order, four fixed Objevit catalogs and one-list/one-shared-genre maxima, all Webshare handoffs/InfoTags/view defaults, complete watchlist identity/scope/generation/fallback behavior, manual `refresh=1` dedup and watched semantics.
- Every task uses RED-first deterministic tests and ends with `python -B -m unittest discover -s tests -v` passing. Tests and native probes use synthetic credentials and loopback only; recursively inspect logs for secrets.
- Preserve existing request timeout/retry, response close, public 401, session identity and nested redaction behavior. No forced socket close, socket-timeout change, forced join, daemon-only rationale or generic closure transport.
- `python -B scripts/verify_release.py` and pinned read-only CI remain authoritative. Python 3.8 AST is not a Kodi 19 runtime claim; the only claimed live platform is Kodi 21.2 Omega Windows Store x64.
- README/version/install/release changes are not publication. Final release preparation still requires an explicit separate user decision and complete README synchronization.

## Review Focus

- **Cross-invoker identity replacement:** delayed old-session 401 racing QR success must leave the new account/session intact. Task 5 Step 1 and Task 9 final native contention gate pin this through OS-lock compare-and-set plus snapshot invalidation tests.
- **Collision plus partial batch/commit failure:** one colliding key or post-terminal cleanup error must not drop siblings, release a replacement owner or trigger a second terminal. Tasks 2 and 4 Step 1 define mutation-sensitive collision/fault-injection tests.
- **Oversized or stalled result retention:** large results and slow waiters must cause bounded typed backpressure, never deletion of an unread result or parent fallback. Task 2 Step 1 tests byte/waiter limits and Task 3 Step 1 tests typed expiry/cancel conversion.
- **Active native teardown:** a contender that never actually enters before response cannot prove concurrency. Tasks 0 and 9 use durable event ordering and fail/UNCHECKED on absent contender evidence; three final exits are independent of prototype exits.
- **Failed directory scheduling:** service/domain failure must preserve UI completion and cached fallback without an extra trailer/show/watchlist burst. Task 7 Step 1 asserts per-route zero extra operations and Task 8 runs the full compatibility matrix.

---

### Task 0: External native Gate 0 feasibility probe

**Files:**
- Create outside repository: `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-shutdown-hardening-gate0\probe_runner.py`
- Create outside repository: disposable probe addon/service, controlled loopback fixture and verifier under the same root
- Reuse read-only: `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-service-prototype\probe_runner.py` and its verified harness patterns
- Do not modify repository production files; approved spec/plan may be added to Task 1's first commit

**Interfaces:**
- Produces machine-readable `summary.json` and per-run monotonic event streams with PID, native invocation/thread ID, job/reservation/waiter/lease IDs, fixture stages and process status.
- Produces a single `PASS` only for all five spec Gate 0 sections; missing event ordering is `UNCHECKED` and blocks Task 1.

- [ ] **Step 1: Extend the native probe for two genuinely active waiters and lease contention**
  Hold the first fixture response until B has entered and registered its collision/waiter action. Add 32/33/34 admission and 40-key continuation counters, immediate responses for non-target keys, and owner-abort checks. First run against the legacy owner and capture fresh evidence of its known `waiting on thread` active-shutdown failure.
- [ ] **Step 2: Run the legacy probe and verify RED without forced cleanup confusion**
  Expected: legacy two-waiter/global-ledger assertions FAIL and active shutdown requires bounded harness cleanup only after recording failure. Preserve the real log/process status; do not count cleanup status as an application exit.
- [ ] **Step 3: Implement only the disposable direct-service/locked-ledger spike**
  Use one service-callback consumer, no added application Python thread, OS lifetime lease plus a minimal atomic same-key ledger and typed waiter fan-out. Do not port production TMDB/UI code or historical queue directories into this spike.
- [ ] **Step 4: Run all five Gate 0 sections and inspect actual native artifacts**
  Require the spec's exact same-PID/distinct-invocation ordering, one GET/session/terminal for collision, global 32+1 maxima, exactly-once 40-key continuation, owner-abort cleanup and no fallback thread/HTTP. Run three independent 12-second active exits with actual code `0`, `Application.Quit=OK`, no `waiting on thread` and no forced cleanup.
- [ ] **Step 5: Record verdict and stop or proceed**
  Store sanitized evidence under the external Gate 0 root and link its verdict in the SDD report. Every PASS is required before Task 1. Do not commit binaries, profiles, logs or disposable probe code to the repository.

### Task 1: Closed typed protocol and result codecs

**Files:**
- Create: `yawsp_app/tmdb_service_protocol.py`
- Create: `tests/test_tmdb_service_protocol.py`
- Add to initial implementation commit: the approved `docs/superpowers/specs/2026-11-26-tmdb-shutdown-hardening-design.md` and this plan

**Interfaces:**
- Produces detached plain-dictionary builders `trailer_request(media, media_id)`, `show_request(show_id)`, `watchlist_request(scope, generation, force, allow_missing_account, session_binding)`, `watched_request(scope, media_id, session_binding)`, `catalog_request(source, language)`, `seasons_request(show_id, language, session_binding)`, `episodes_request(show_id, season, language, session_binding)` and `invalidate_identity_request(account_id, session_binding)`.
- Produces `dedup_key(request) -> str`, `validate_job(document) -> dict`, `validate_result(document) -> dict`, `make_result(job_id, outcome, value=None) -> dict` and `ProtocolError(ValueError)`.
- Every request contains exact `operation`, identity `params`, optional generation/force/binding fields and no `job_id`; store owns IDs/state. Result outcome is exactly `succeeded|failed|aborted`; errors use fixed sanitized codes/messages only.

- [ ] **Step 1: Write failing builder/codec and hostile-document tests**
  Cover all eight operations; exact trailer/show/source/scope normalization; force/generation/binding dedup distinctions; detached output; trailer `hit|missing|error`, show value+success flag and revision-only watchlist results. Reject callables, arbitrary operation/method/URL/path fields, secrets, duplicate/unknown JSON fields, bool-as-int, nonfinite values, bad IDs, malformed Unicode/surrogates and oversized canonical forms.
- [ ] **Step 2: Run `python -B -m unittest discover -s tests -p test_tmdb_service_protocol.py -v` and verify import/behavior RED**
- [ ] **Step 3: Implement the closed schema and canonical SHA-256 key codec**
  Use canonical UTF-8 JSON prefixed by `tmdb-service-v1\0`; validate exact per-operation fields and fixed result types. Keep HTTP, files and Kodi imports out of this module.
- [ ] **Step 4: Run focused and full discovery suites**
  Expected: focused tests and all previous 398 contracts PASS, except the documented Windows symlink privilege skip.
- [ ] **Step 5: Commit `feat: add bounded TMDB service protocol` with tests and approved docs**

### Task 2: OS-locked epoch manifest, admission and bounded result ownership

**Files:**
- Create: `yawsp_app/tmdb_service_store.py`
- Create: `tests/test_tmdb_service_store.py`

**Interfaces:**
- Consumes Task 1 request/result builders and validators.
- Produces `ServiceLease.acquire() -> bool`, `.release() -> None`, `ServiceStateStore.identity_transaction()` and `ServiceStateStore.service_ready() -> bool`.
- Produces `recover(epoch) -> None`, `set_ready(epoch)`, `set_stopping(epoch)`, `submit(requests, interactive=False, waiter_id=None) -> list[dict]`, `join(request, waiter_id) -> dict`, `claim_next(epoch, claim_token) -> dict|None`, `complete(claim, result) -> dict`, `ensure_followup(claim, request) -> dict`, `read_wait_result(job_id, waiter_id) -> dict|None`, `acknowledge(job_id, waiter_id) -> None`, `abort_claim(claim, code) -> None` and `collect_garbage() -> None`.
- Submission rows are exactly `accepted(job_id, request, reservation)` / `joined(job_id, request)` / `backpressured(request, code)`; result reads return `None` only while nonterminal.

- [ ] **Step 1: Write failing ownership, startup, retention and fault tests**
  Use independent store instances with barriers. Pin atomic reservation+visible key creation, owner-fenced release, 32+1 across instances, 64/8 backlog and full typed backpressure, fresh publication during recovery, old-work abort without replay, 40-key exactly-once promotion, per-key overlapping batches, and 8-success checkpoint state inputs.
  Force temp creation and post-commit cleanup failures: acquired ownership must reconcile and committed results must never reject twice. Exercise duplicate terminal/ACK, poisoned one-job isolation, deterministic result paths/digest, 32-terminals+tombstones, all byte/waiter bounds, and zero eviction of live claims or registered unread results. Include safe read admission/symlink rejection and Unicode preservation without follow/truncate.
- [ ] **Step 2: Run `python -B -m unittest discover -s tests -p test_tmdb_service_store.py -v` and verify RED**
- [ ] **Step 3: Implement OS locks and single-manifest transaction machinery**
  Hold `lease.lock` only in the native consumer; `transaction.lock` protects a reload→detached mutate→bounded serialize→fsync/close→replace→validated reread sequence. `identity_transaction()` is separate and never surrounds network I/O. Provide Windows locking and statically compatible POSIX adapter tests without claiming live POSIX support.
- [ ] **Step 4: Implement epoch recovery, scheduling/admission, terminal ACK and GC**
  Create reservation and key owner atomically, publish ready only after old-epoch quarantine, use ordinal/family/interactive scheduling, publish digest-validated bounded results by deterministic job ID, and reconcile cleanup separately after committed terminal/follow-up transitions.
- [ ] **Step 5: Run focused suites repeatedly plus full discovery and commit**
  Run the store suite at least 20 times with deterministic fault schedules, then full discovery. Commit `feat: add atomic bounded TMDB service manifest`.

### Task 3: Read-only parent client, waiter lifecycle and typed errors

**Files:**
- Create: `yawsp_app/tmdb_service_client.py`
- Create: `tests/test_tmdb_service_client.py`
- Modify: `yawsp_app/tmdb.py` only to add typed service exception subclasses and read-only repository/cache behavior required by the adapter

**Interfaces:**
- Consumes Task 2 submission/join/read/ACK and Task 1 request/value types.
- Produces `TMDBServiceClient.submit_metadata(media, trailer_ids=(), show_ids=(), missing_known=False) -> int`, existing `submit_trailers`, `submit_shows`, `submit_watchlist`, `attach_repositories`, `run_shared(key, operation)` and idempotent nonjoining `close(wait=False)`.
- Produces `run_operation(request) -> dict` for interactive catalog/details/watchlist/watched/invalidation operations and `run_watchlist(request) -> dict` returning scope/revision only; no session/HTTP/cache factory exists in the parent client.
- `operation` in compatibility `run_shared` is never called in service mode. Unknown keys raise `ProtocolError` rather than direct execution.

- [ ] **Step 1: Write failing unavailable/backpressure/collision/waiter tests**
  Use blocked fake store handoffs and operations that raise if invoked. Assert accepted same-key concurrent trailer/show callers get one job ID/equivalent value, second terminal/ACK does not duplicate work, service unavailable/full/abort/stopping/expiry map to typed outcomes, and close detaches only its invocation waiters. Assert parent cache loading including legacy values performs zero writes.
- [ ] **Step 2: Run focused tests and verify direct-execution/read-side-write RED**
- [ ] **Step 3: Implement bounded abort-aware client polling and result decoding**
  Register waiter before the first 0.05-second injected `waitForAbort` slice; read only that job ID, validate/detach decoded values and acknowledge exact `(job_id, waiter_id)` in `finally`. Preserve a valid result if a later ACK fails, without another request.
- [ ] **Step 4: Implement parent read-only adapters and domain exception mapping**
  Keep trailer/show TTL/stale/error behavior and detached reads; move any migration/save capability to service-only construction. Never turn fixed service errors into arbitrary remote exception text.
- [ ] **Step 5: Run focused metadata/UI-related suites and full discovery, then commit**
  Commit `feat: add nonblocking TMDB service client`.

### Task 4: Service-owned trailer/show execution and fair serial scheduling

**Files:**
- Create: `yawsp_app/tmdb_service_worker.py`
- Modify: `yawsp_app/tmdb.py`
- Create: `tests/test_tmdb_service_worker.py`
- Modify: `tests/test_tmdb.py`

**Interfaces:**
- Produces `ServiceMetadataEngine(settings, trailer_cache, show_cache, locale_mapper, session, abort, logger)` with `execute(request) -> dict`, `flush() -> None` and `close() -> None` running only on the service invocation.
- Produces `ServiceRunner(store, metadata_engine_factory, watchlist_engine_factory, operation_engine_factory, abort, wait_for_abort, logger)` with `run_once() -> bool`, `run_until_abort()` and `close()`; this task supplies real metadata and temporary typed-unavailable handlers for later operation families.
- A metadata execution result contains its own `job_id` and typed trailer `TrailerLookup` or show `(public_value, succeeded)` codec without relying on positional seven-tuples/caller closure captures.

- [ ] **Step 1: Write failing service-domain and scheduler regressions**
  Reuse all current trailer/show success, genuine missing/None, stale fallback, no-negative-cache, retry and redaction tests. Pin one owned session per serial drain cycle, close after response/abort/error, maximum eight successes per flush plus final flush, round-robin fairness, interactive preemption only between items and exact one terminal per job.
  Add overlapping multi-key native-store collisions, post-terminal cleanup failure without reject/second transition, 40-key continuation, batch poison isolation and an operation callback that proves no parent transport/fallback exists.
- [ ] **Step 2: Run the new worker suite and verify missing engine/coordinator RED**
- [ ] **Step 3: Extract stable service-domain execution from existing private worker internals**
  Reuse the actual TMDB parsing/cache routines instead of another HTTP stack. Bound retry attempts 0/1, preserve existing transport timeout/Retry-After/abort sleeps and close responses in the service. The serial direct domain coordinator exists only inside the service and is never a manifest capability or parent fallback.
- [ ] **Step 4: Implement `run_once` serial claim/complete/abort reconciliation**
  Keep one active HTTP operation, recheck priority before each metadata item, use manifest-terminal authority after cleanup errors and flush completed valid successes during final unwinding.
- [ ] **Step 5: Run new worker/store/client tests and full discovery, then commit**
  Commit `feat: execute TMDB metadata in native serial worker`.

### Task 5: Snapshot single-writer boundary, watched follow-ups and identity fencing

**Files:**
- Modify: `yawsp_app/watchlist_snapshot.py`
- Modify: `yawsp_app/tmdb.py`
- Modify: `yawsp_app/tmdb_service_worker.py`
- Create: `tests/test_tmdb_service_watchlist.py`
- Modify: `tests/test_watchlist_snapshot.py`

**Interfaces:**
- Produces `ServiceWatchlistEngine(settings, snapshot_store, locale_mapper, identity_lock, session, abort, logger)` with `execute(request) -> dict` for `watchlist.refresh`, `watchlist.watched` and `identity.invalidate`.
- Refresh returns `{scope, revision, generation}` after exact complete-save validation; parent can reread that committed scope/revision. Watched returns handled API success and latest snapshot revision or a typed business failure.
- `ensure_followup()` is called before predecessor terminal when generation conflict/watched invalidation requires it and coalesces the newest force/generation intent.

- [ ] **Step 1: Write failing cross-instance snapshot and identity tests**
  Barriers must prove an older refresh cannot overwrite newer watched removal/invalidation and cannot lose unrelated account scopes. Cover force N+1 during active N, crash/fault before terminal, exactly one durable continuation, identity/language switch before/during pages, missing-account resolution, stale old 401 versus QR replacement under the real OS identity lock, public 401 preservation, failed persistence bytes and no immediate duplicate manual follow-up.
- [ ] **Step 2: Run the new watchlist service tests and verify cross-writer RED**
- [ ] **Step 3: Extract complete refresh/removal/invalidation domain commands**
  Reuse `fetch_complete_watchlist`, scoped operation clients and snapshot validation. Bind current settings through versioned non-secret digests and compare/set raw current session only inside `identity_transaction()`; never hold store locks across pages/HTTP.
- [ ] **Step 4: Integrate durable continuation and service-only snapshot mutation**
  Save only all validated pages, remove from latest generation after successful watched POST, preserve created/revision semantics and failed-refresh fallback. The invalidation command tombstones only matching accounts and cannot erase a newer identity.
- [ ] **Step 5: Run watchlist/snapshot/worker suites and full discovery, then commit**
  Commit `feat: fence TMDB snapshots and identity across invokers`.

### Task 6: Public discovery/details service operations without extra requests

**Files:**
- Modify: `yawsp_app/discovery_ui.py` only to extract pure response mapping if needed; no visual/navigation redesign
- Modify: `yawsp_app/tmdb_ui.py` only to extract seasons/episodes response mapping
- Modify: `yawsp_app/tmdb_service_worker.py`
- Create: `tests/test_tmdb_service_discovery.py`
- Modify: `tests/test_genre_file_cache.py` only if ownership wiring requires additional (not relaxed) coverage

**Interfaces:**
- Produces `ServiceOperationEngine(settings, genre_repository, locale_mapper, identity_lock, session, abort, logger).execute(request) -> dict` for `discovery.catalog`, `details.seasons` and `details.episodes`. `identity.invalidate` remains exclusively in Task 5's `ServiceWatchlistEngine`.
- Catalog returns detached rows accepted by the existing Objevit renderer, not Kodi `ListItem` objects. Season/episode results contain detached API/UI values, not Responses.
- Parent UI retains pure renderers and receives immutable/detached result values; no transport-capable public/details client is needed for these routes.

- [ ] **Step 1: Write failing exact-request and preservation tests**
  For each of four immutable sources require one list GET and at most one corresponding movie/TV genre GET, only for nonempty rows, with shared media/language genre requests and persistent genre writes solely in service. Require public 401 personal-identity preservation, no account/session on public requests, no pagination/account/trailer/show/watched/autoplay work.
  Assert exact localized/original query data and seasons/episodes parameters; malformed/empty/failed responses remain typed without file writes. Genre suite must retain current-byte/schema/Unicode/symlink checks.
- [ ] **Step 2: Run focused discovery tests and verify operation-handler RED**
- [ ] **Step 3: Extract named public/details domain operations**
  Keep public sessionless view and existing language mapping. Reuse `GenreRepository` in the service-owned sequential write path so response close→validate→save/fsync/close→replace→independent strict reload remains exact.
- [ ] **Step 4: Wire operations into the serial runner with detached result codecs**
  Do not issue service-to-parent callbacks, Kodi completion or UI metadata bursts. Preserve one serial request and all result-size/type bounds.
- [ ] **Step 5: Run discovery/genre/UI tests and full discovery, then commit**
  Commit `feat: route public TMDB operations through single writer`.

### Task 7: Production router/UI integration and official native service registration

**Files:**
- Create: `service.py`
- Modify: `addon.xml`
- Modify: `yawsp_app/router.py`
- Modify: `yawsp_app/tmdb_ui.py`
- Modify: `yawsp_app/discovery_ui.py` only for explicit service-result consumption
- Modify: `yawsp_app/tmdb_service_worker.py`
- Modify: `tests/test_entrypoint.py`, `tests/test_tmdb_ui.py`, existing discovery UI tests, `tests/test_resources.py`

**Interfaces:**
- `service.run(monitor=None) -> bool` constructs native adapters and runs `ServiceRunner.run_until_abort()` directly; root `service.py` is the only new Kodi adapter/entry point.
- `_ProductionOwner` composes `TMDBServiceClient` without `BackgroundPrefetch` executor/worker-session construction. `DownloadJobManager` gets its independent existing session factory unchanged.
- Parent cold/manual/watched/detail/discovery handlers use `run_operation`/`run_watchlist`, reread revision-matched snapshots and preserve existing handler signatures where possible. `close()` detaches service-client waiters but never stops/joins the global service.

- [ ] **Step 1: Write failing real-composition and per-route compatibility tests**
  Instrument owner construction: no `ThreadPoolExecutor`, background Python thread, parent metadata session or parent cache/snapshot save/migration is permitted. Validate exactly one official `xbmc.service` extension and direct native loop.
  For warm/cold/manual/failed watchlist, watched, on-demand trailer, four catalogs and seasons/episodes assert typed results/fallbacks, Kodi completion, refresh order and zero extra metadata work after failed directories. Keep exact root order, handoffs, InfoTags, view-default/run actions and current DownloadJobManager behavior.
- [ ] **Step 2: Run composition/UI/resource tests and verify legacy direct-writer/executor RED**
- [ ] **Step 3: Replace production composition and remove caller fallbacks**
  Route every metadata/snapshot mutation to the service. Keep QR/session exchange explicit but use the shared identity transaction. Remove obsolete executor/legacy production worker paths rather than leaving selectable unsafe modes; test helpers may retain narrow direct domain engines only when they cannot masquerade as parent production fallback.
- [ ] **Step 4: Implement `service.py` and addon extension**
  Use the same add-on profile, settings, locale, logger and monitor. Acquire lease, recover/ready, poll jobs with `waitForAbort`, set stopping, abort unstarted work, unwind/close in `finally`; do not create an application thread.
- [ ] **Step 5: Run all offline tests, resource/XML and archive verification, then commit**
  Run full discovery and `python -B scripts/verify_release.py`. Expected: all tests pass except known symlink privilege skip; archive contains `service.py` plus new runtime modules with exact bytes and no userdata. Commit `feat: register native single-writer TMDB service`.

### Task 8: Whole-branch historical-fault regression, documentation and independent offline review

**Files:**
- Create: `tests/test_tmdb_service_regressions.py` only for cross-module races not already housed in focused suites
- Modify: failing implementation files only after a test demonstrates a real remaining fault
- Modify: `README.md` without changing version/release/install targets; describe actual implemented service and honest pending native/visual gates
- Create: `.superpowers/sdd/2026-11-26-tmdb-shutdown-hardening/offline-verification.json` with synthetic evidence only

**Interfaces:**
- Consumers are all previous tasks' public service interfaces and existing UI/persistence contracts.
- Produces an explicit old-finding→new-test selector map and independent read-only functional/cache, security/privacy and performance verdicts; no unresolved P0/P1 finding proceeds to live acceptance.

- [ ] **Step 1: Run old-rejected-protocol regression selectors against their historical source reasoning and add any missing RED tests**
  Cover every defect listed in the spec: live incoming reservation sweep, owner-token replacement, overlapping batch loss, offline false acceptance, newer force/follow-up loss, terminal cleanup second reject, malformed terminal poisoning, cross-instance lost writes, read-side migration, retention/readers and admission/result-byte bounds. Assert both positive and negative controls so tests are not vacuous.
- [ ] **Step 2: Implement only demonstrated residual fixes and repeat deterministic stress schedules**
  No weakening/skipping/flaky sleeps. Use injected events/barriers and independent instances; preserve final source lineage and exact error responses.
- [ ] **Step 3: Run independent read-only reviews and remediate scoped findings RED-first**
  Supply each reviewer the final diff plus spec/tests. Require actual verdict/evidence, including secret-chain and log review, global capacity/performance counts and UI/fallback coverage. Do not modify source during a review invocation itself.
- [ ] **Step 4: Run complete static/offline verification and update honest documentation**
  Commands: full discovery; Python 3.8 `ast.parse(feature_version=(3, 8))` for every runtime `.py`; XML/resource tests; `git diff --check`; `python -B scripts/verify_release.py`. Verify `.github/workflows/tests.yml` byte-unchanged/read-only/pinned. README must not claim final native/visual acceptance yet or alter released version/install links.
- [ ] **Step 5: Commit `test: prove bounded TMDB service ownership` and the reviewed docs/evidence**

### Task 9: Final integrated native acceptance and release-readiness boundary

**Files:**
- Create outside repository: `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-shutdown-hardening-final\` fresh isolated profiles, loopback fixtures, event evidence and verifier
- Modify only from verified results: `.superpowers/sdd/2026-11-26-tmdb-shutdown-hardening/native-verification.json` and honest README/evidence text
- Do not create tag, release, permanent archive, installation package for main profile or merge commit

**Interfaces:**
- Same five gates as Task 0 but driven through the final real routes/service, final source archive and fresh unique PID/profile per run.
- Produces native verdict `PASS|FAIL|UNCHECKED`, exact source commit/archive SHA-256, three final active-shutdown run IDs and explicit separate `visual_acceptance=pending` unless external review is actually completed.

- [ ] **Step 1: Verify exact artifact identity and repeat native concurrency/admission/collision gates**
  Two genuinely overlapping native interactive callbacks must share one typed result while the fixture is held; global lease stays single, 40 distinct jobs complete exactly once, live reservations stay at 32+1 and queue/result counts remain bounded. Reject RPC-only, host-only, sequential or later-observed evidence.
- [ ] **Step 2: Repeat owner-abort and three active 12-second shutdown runs**
  Real `Application.Quit` after request entry and before one delayed HTTP 200; service response/session/final cleanup and callback unwinding are machine-ordered. Each process independently exits `0` under the 25-second guard with no forced cleanup, `waiting on thread`, replayable active/backlog record or second request.
- [ ] **Step 3: Run real-route discovery/watchlist/native metadata checks**
  Preserve 4 list requests + 1 movie genre + 1 TV genre across the four selections, exact Webshare handoffs/InfoTags/view behavior, warm/manual/failed snapshot and watched semantics. Inspect sanitized logs/profile records for real/fixture secrets and forbidden UI/thread patterns.
- [ ] **Step 4: Repeat full tests and archive verification on the exact accepted source**
  All P0/P1 findings or missing events block acceptance. Report remaining platform, socket-cooperation and visual limitations explicitly. Do not use the earlier prototype exits as substitutes.
- [ ] **Step 5: Commit evidence only if accurate, then stop for separate user decisions**
  If evidence or tracked documentation changed, rerun Step 4 on the final commit and update its recorded identity so no accepted-artifact claim points to an earlier tree. Ask separately for external visual review and only afterward for merge/deploy/release permission. Release preparation must update README version badge/features/setup/install to exactly match `addon.xml`, include that update before tagging, and verify published commit/archive after separately authorized publishing.

---

## Implementation invariants

- Tasks are sequential because codec, store, client and worker interfaces compose into one safety boundary; do not concurrently edit the same module from parallel implementation agents.
- Every reviewer is read-only. A fault becomes a named RED test before remediation; the reviewer output must include actual file/line and executed behavior, not a generic approval.
- Full discovery after every task is mandatory. Regressions block the next task. Gate 0 failure prevents production changes; final native failure prevents any release claim.
- No historical commit restoration, no retry-by-parent, no unbounded queue/TTL-only safety rationale, no automatic retry permission from committed cache data and no credentials in artifacts.
