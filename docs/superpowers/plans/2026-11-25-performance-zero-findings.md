# Performance Zero Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close PERF-01 through PERF-04 and independently re-audit the result with no confirmed functional or performance findings.

**Architecture:** Preserve the single TMDB worker, guarded identity resolution and atomic snapshot cache. Prioritize only claimed deferred on-demand work, publish identity only after its guarded response, authenticate Webshare lazily, and reuse one refresh-state decision per warm invocation. Measure request/I/O counters rather than promising latencies.

**Tech Stack:** Python 3, unittest, requests, Kodi 19+ adapters, threading.Condition/RLock, JSON snapshots.

**Spec:** `docs/superpowers/specs/2026-11-25-performance-zero-findings-design.md`

## Global Constraints

- Preserve public URL actions, settings IDs, `main.py`, directory/action/playback completion and actual success/error/cancel behavior.
- Preserve exact account/media/language snapshot scoping, completeness, atomic generation checks and independent HTTP session ownership.
- Do not increase the background reservation cap of 32 or add executors, dependencies, retry attempts or network calls.
- Every production change follows a focused RED via `python -B -m unittest <exact.test> -v`. Run `python -B -m unittest discover -s tests -v` after every task.
- Commits remain in this isolated branch. No `main` merge, remote push, version bump, tag or release without separate approval.

## Review Focus

- Existing active flights retain one shared HTTP result (Task 1 concurrent-flight tests).
- Account/session/language switches never publish obsolete identity (Task 2 switch tests).
- Local cancellation and invalid history keys do not become auth failures (Task 3 real-handler tests).
- Watched local mutations retain newer generations (Task 4 conditional/interleaved-generation tests).
- Save failure or invalid replaced cache data preserves valid prior data (Task 4 cache tests).

---

### Task 1: Prioritize deferred on-demand TMDB lookups

**Files:**
- Modify: `yawsp_app/tmdb.py:914-960`
- Test: `tests/test_tmdb.py:1923-1985`

**Interfaces:**
- Consumes: existing `_PendingLookup`, `_claim_deferred_locked(key)`, `_pending`, `_metadata_queued` and `_execute_flight`.
- Produces: no new public interface. `run_shared(key, operation)` retains return/error behavior; only claimed deferred work bypasses background capacity.

- [ ] **Step 1: Write failing capacity-wait regression**

Replace `test_deferred_on_demand_claim_waits_for_capacity_before_reserving` with `test_deferred_on_demand_claim_precedes_background_capacity_without_wait`. Submit shows `range(1, 101)` to `ManualWorker`, request ID `33` through the coordinated repository, and instrument `_condition.wait` to fail if reached before direct request. Assert result `None`, one on-demand request, `pending_at_request == [33]`, direct completion with zero worker jobs run, then bounded drain and persistence.

- [ ] **Step 2: Prove RED**

Run the exact focused test on unchanged production. Capture failure caused by capacity `Condition.wait`/direct blocking, releasing fixture threads during RED so no hang occurs.

- [ ] **Step 3: Implement claim-before-capacity ordering**

Retain active-flight sharing first. Claim the deferred key and reserve it before capacity waiting; retain existing cap behavior for all other calls and background reservations. Preserve retry/result/abort/close/continuation handling.

- [ ] **Step 4: Verify**

Run focused, concurrent-active-flight, full 100-item drain/ownership tests and full suite. Duplicate active flights keep one request; background-only active reservations never exceed 32.

- [ ] **Step 5: Commit Task 1**

Stage only `yawsp_app/tmdb.py` and `tests/test_tmdb.py`; commit `perf: prioritize claimed on-demand metadata`.

### Task 2: Publish validated TMDB account identity

**Files:**
- Modify: `yawsp_app/tmdb.py:611-670`
- Test: `tests/test_entrypoint.py:853-902`

**Interfaces:**
- Consumes: existing staged account settings, `commit(account_id)` and guarded `resolve_watchlist_account`.
- Produces: no signature changes. `commit` still returns bool but publishes only after all guards pass.

- [ ] **Step 1: Strengthen the warm-reuse regression**

Update `test_production_missing_account_second_open_reuses_complete_scoped_snapshot`: after first cold open assert `tmdb_account_id == "account-1"`; do not queue another `/account` response; after second warm open assert exactly **3 total HTTP calls** — one account, one watchlist, one genre — no second-open `/account`, and same scoped rendered rows.

- [ ] **Step 2: Prove RED**

Run exact entrypoint test on unchanged production. Expected failure: second open attempts a fourth HTTP `/account` call or stored ID is still empty.

- [ ] **Step 3: Publish guarded identity**

Change `_WatchlistResolutionSettings.commit` to publish staged validated ID only when staged ID, current account compatibility and captured session/language validity pass. Return bool unchanged; no-publish mismatches retain errors. Do not alter worker settings or URLs/config.

- [ ] **Step 4: Verify**

Run focused plus `test_production_account_resolution_never_publishes_speculative_identity`, all production account/language switches, initially-missing-account worker tests and full suite. Warm fixture stays at three calls; obsolete/failure resolutions publish nothing.

- [ ] **Step 5: Commit Task 2**

Stage only Task 2 files; commit `perf: reuse validated TMDB account identity`.

### Task 3: Skip Webshare authentication for local-only paths

**Files:**
- Modify: `yawsp_app/webshare_ui.py:647-687`
- Test: `tests/test_webshare_ui.py`

**Interfaces:**
- Consumes: existing token, local history, queue and present-search functions/routes.
- Produces: no route changes. Local menu/cancel/remove need no token; remote search/toqueue still authenticate once per invocation.

- [ ] **Step 1: Write local-zero-I/O regressions**

Add real handler tests for no-`what` menu, keyboard cancellation returning `None`, and local `remove` without a subsequent remote query. For each assert `client.revalidate_calls == 0`, `client.search_calls == []`, `session.calls == []`, expected local history/output and completion. Keep a remote-search control asserting one revalidation and one search.

- [ ] **Step 2: Prove RED**

Run all three on unchanged production; record revalidation `1` rather than `0`, not a fixture error.

- [ ] **Step 3: Lazy token resolution**

Do local remove/keyboard/history work before authentication. Resolve one token lazily at `to_queue` and `present_search`, reusing it for both. Preserve failed remote-token and cancellation completion behavior.

- [ ] **Step 4: Verify**

Run local, remote, failed-token, history, toqueue and cancel tests then full discovery. Exactly 0 local network calls; remote authentication remains.

- [ ] **Step 5: Commit Task 3**

Stage only Task 3 files; commit `perf: avoid authentication for local Webshare actions`.

### Task 4: Reuse snapshot refresh state and one-load saves

**Files:**
- Modify: `yawsp_app/watchlist_snapshot.py:13-214`
- Modify: `yawsp_app/tmdb.py:1095-1136`
- Modify: `yawsp_app/tmdb_ui.py:498-529`, `:727-741`
- Test: `tests/test_watchlist_snapshot.py`, `tests/test_tmdb.py`, `tests/test_tmdb_ui.py`

**Interfaces:**
- Add frozen `SnapshotRefreshState(snapshot: Optional[Snapshot], generation: int, refresh_due: bool)` using `typing.Optional` for Kodi 19 / Python 3.8 compatibility.
- Add `WatchlistSnapshotStore.refresh_state(scope, now) -> SnapshotRefreshState`, performing one load with existing snapshot/generation/five-minute-due semantics.
- `submit_watchlist(scope, force=False, allow_missing_account=False, refresh_due=None, generation=None)` checks existing cache when optional state is absent; supplied due/generation skips another load. Supplied due false does not enqueue.
- `save_complete` keeps arguments/returns, performs one transactional load, target merge and existing `save`, preserving other scopes/revision/generation guards.

- [ ] **Step 1: Add store-level RED tests**

Instrument `AtomicJsonCache.load` including nested RLock calls. Assert one load for `refresh_state`; one for accepted save; one load and no write for rejected generation; exact existing refresh threshold and other-scope/copy-isolation semantics.

- [ ] **Step 2: Prove RED**

Run each store test. Missing refresh-state API and accepted save's existing two loads must fail for those exact reasons.

- [ ] **Step 3: Implement store coalescing**

Use one load for refresh state and one transactional load/in-memory target merge/existing atomic save for saves. Keep disk schema and `json_cache.py` untouched.

- [ ] **Step 4: Measure warm invocation RED and integrate state**

Instrument the warm UI test: assert fresh invocation **1 load instead of 2**, stale/SWR invocation plus worker save **2 total loads instead of 5**. Run it before integration to capture existing counts. Pass UI captured state to post-completion submit; keep cold successful-save no-refresh behavior.

- [ ] **Step 5: Verify**

Run new counts, all conditional/interleaved snapshot and identity tests, watched-during-refresh/followup tests, warm/cold UI and full discovery. Conflicts return `None` without writes, unrelated scopes survive and network counts do not increase.

- [ ] **Step 6: Commit Task 4**

Stage only Task 4 source/tests; commit `perf: coalesce TMDB snapshot cache loads`.

### Task 5: Whole-diff independent verification

**Files:** all production/test diffs from BASE through HEAD.

**Interfaces:** consumes four tested commits and produces final evidence-backed audit, no release operations.

- [ ] **Step 1: Final offline verification**

Run full unittest discovery, `git diff --check BASE..HEAD` and AST/XML parsing. Print no secrets; use no live accounts.

- [ ] **Step 2: Independent whole-branch functional/performance reviews**

Verify all four findings against actual diff. Compare before/after counts and separate live Kodi/ARM/VFS limits from open defects. Critical/Important findings return to the owning task's implementer with a failing test.

- [ ] **Step 3: Final evidence report**

Report exact tests, request/load counts, commits and remaining environment limits. Do not claim zero findings if any confirmed issue remains. Leave merge, push and release to the user.
