# Public Genre OS-Lock Admission Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to execute task-by-task. Steps use checkbox (`- [ ]`) syntax. This is a disposable evidence plan; no production implementation exists without PASS.

**Goal:** Prove on actual isolated Kodi 21.2 native plugin invokers whether the proposed stdlib OS-lock can serialize a shared genre lookup during owner HTTP and release correctly during abort, rather than relying on process-local assumptions.

**Architecture:** A deployed-only controlled wrapper around the existing public genre lookup holds a candidate `msvcrt.locking` lock while the real request is in flight and completes a sanitized temporary public mapping. A second genuine native invoker contends and reloads it. A separate owner-abort case examines finally/descriptor release. Main product source is unchanged; no cache module is admitted before evidence.

**Tech Stack:** actual cloned Kodi 21.2 Windows Store x64, CPython 3.8.15, stdlib `msvcrt`/`os`, real plugin ActionRunner/TMDBClient/PublicTMDBClient/GenreRepository, stdlib offline test controller and loopback JSON-RPC/HTTP fixtures

**Spec:** `docs/superpowers/specs/2026-11-25-persistent-public-genres-design.md`, especially Builder Clarifications and the admission phase-0.

## Global Constraints

- New disposable root: `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-genre-lock-admission`. Absolutely no changes to repository source/tests/config/resources/workflows, main profile/binary/process, RPC9090, merges, pushes, tags or releases. Only a dedicated SDD task-report is allowed in repo.
- Source HEAD is currently `c1bcdf76534c39145aee4d1d4d8b4da0f80f0f9c`. Record it and unchanged status before/after. Any future documentation-only commit is not evidence of a persistent genre implementation (none exists today).
- Use cloned runtime excluding all prior `portable_data` and prior plugin code. Exact executable SHA-256 `c6b1a566a1b4185ed9f6392889600a0bef08f86babd5b21744f67daee9dc3664`. Fresh addon profile/database/effective owner per scenario. Reuse validated dependency sources only, not old config or credentials.
- Actual public invocation paths/nodes only. Do not replace ordinary real invocation with a host-only `subprocess`/thread as proof of invoker behavior. Record OS PID and actual invoker/Python thread IDs separately. If they share PID, never mislabel them as two processes.
- Local loopback-only synthetic API and images. API request is `/3/genre/movie/list` with `language=en-US` and synthetic API-key only; never session_id/Authorization/real secret. Fixture delays are chosen from known-current request behavior and may use the established bounded value; no new socket timeouts, time.sleep-as-success, daemon changes or CPython worker rewrites.
- Instrument only deployed text, not source: a wrapper surrounding actual `GenreRepository.get()` or equivalent real client `get("/3/genre/movie/list", {"language": identical string})` may obtain the tested lock. Capture lock attempt/acquire, HTTP entry/response, validation, temporary sanitized JSON merge/write, release, reload/no-second-request, abort/finally. The True production handler still needs a real validated public response and dedicated `response.close()`; do not fake a successful network or row.
- Lock may not silently become an unlocked in-memory GET during contention. Allowed outer outcomes: acquire/recheck; typed existing TMDBAbortError; storage/platform failure with no claimed persistence. If retry mechanics time out or handle fails, no unlocked fallback request is permitted to “prove” dedup.
- `tmdb_genres.json` and `tmdb_genres.lock` are disposable profile artifacts, not migration/production changes. Their absolute profile may be created at the normalized addon path only. Validated root/source contains only schema1 and public positive-int ID/nonempty names; no request URL, token, user title, user scope or private metadata.
- Raw callback data must not redefine API error recovery. Errors and OS police do not overwrite unsupported user bytes as a migration. Hostile external same-user reparse swaps are explicitly outside the guarantee; no fake new filesystem security fix. Do not install a named-mutex/service/queue or alternative framework if this lock fails.
- Wait on notified event manifests/liveness with deadlines, not arbitrary sleeps. `Application.Quit` first if stop needed. Only exact spawned process executable identity may be terminated if bounded cleanup fails; no name-based killing or platform/global/main deviations. A forced kill is never a PASS.
- Owner-abort case uses a *synthetic abort state, outside forced process termination* to exercise `TMDBAbortError`/finally/release; don't touch the real monitor background-worker Fix phase. Any code/commit says the legacy active-background shutdown HANG remains separate and unresolved.

## Review Focus

- Same-file versus actual same-thread/in-process claims: Task 1 requires a callback event before current HTTP finishes, not just sequential requests after cleanup.
- Duplicate during contention: a second genre callback must not trigger unlocked GET while an owner holds the lock/response (Task 1).
- Descriptor release when an *owner* aborts, not only when a waiter or Optimal process completes: Task 2 uses full-send callback state + cleanup, not external suspending application teardown that masks it.
- Fresh profile/data scope/reset: Tasks 1–2 only create path-normalized local sidecars, never infer the next session is using old cached map from a corrupted profile.
- No event/credential proof shortcuts: Task 3 requires actual events, status/path counts, actual thread evidence and exactly one language for the counter; site/naming cloaks are not valid success.

---

### Task 1: Contended live genre request under native invocation

**Files (disposable only):**
- Create: `stage_admission.py` — clone/provenance setup and exact temporary registration wrapper/structured recording
- Create: `run_contended.py` — native two-invocation controller
- Temporary deployed wrapper — no new runtime service; uses current production handler/public genre logic with candidate OS lock
- Create: `runs/contended/summary.json`, structured events and preserved profile/log/manifest


**Interfaces:**
- Consumes: exact `c1bcdf7` production client public genres and cloned runtime/provenance listed in spec.
- Produces: known per-invoker/thread/ts/time evidence of first holder's HTTP active when second invokes+attempts lock; one accepted completed map or explicit admission FAIL.

- [ ] **Step 1: Prepare and assert immutable baseline**
Record source hash+clean status and reproducible package/payload Python3.8 grammar. Clone excluding profiles to fresh root and verify exact executable SHA plus boot Kodi/CPython details. Find/config addon enabled, no main process/secret transport allowed. Validate each candidate wrapped call uses exact `"language":"en-US"` key string params and current sanitized/public sessionless path, not a new raw HTTP session request.
- [ ] **Step 2: Implement the disposable lock-lifecycle wrapper**
Acquire nonblocking/short OS-lock attempt with abortable loops around **actual** `GenreRepository.get()` or equivalent exact client get. Keep owner while request/delayed valid response parses and merges sanitized genre ID/name values into a fresh validated strict schema sidecar. Merge/reload under lock and only atomically replace from owned local temporary file; do not re-use unknown bytes or open path with simple truncate. A temporary stale/unknown text is never accepted as proof of persistent cache. Release using normal context/finally. If ops unsupported, return storage/platform failure instead of an extra unlocked fetch.
- [ ] **Step 3: Drive second real invoker while the first HTTP is active**
Use the ordinary chart category via actual native action/router handler. First record its lock acquire+request entry, then start second genuine invocation before the first HTTP fixture response completes. Record PID+thread IDs and second `lock_attempt` time before first `HTTP200`. If its GET still reaches fixture without lock acquisition, FAIL. If it blocks but cannot actually schedule before response, mark UNCHECKED rather than calling serialized wrapper effectiveness proven.
- [ ] **Step 4: Prove completion identity and single GET**
First writer persists valid genre map, releases, second receiver gets it from reload without a second `/3/genre/movie/list` on its callback. Assert `HTTP200` response closes; value contains expected integer-id name only, no secret fields or stale map, request count exactly one and no extra unlocked fallback. `Application.Quit` finishes normally code0, without forced cleanup. Save summary with all predicates individually, not a blanket PASS when admission could not start or contended native invocation never occurred. Independent admission review/verifier is a prerequisite to Task 2.

---

### Task 2: Owner-abort unwinding and descriptor release

**Files (disposable only):**
- Create: `run_owner_abort.py` and fresh profile/runtime fixture
- Reuse: temporary candidate wrapper exactly as Task 1, no replacement mutex/worker

**Interfaces:**
- Consumes: successful Task 1 active-request OS-lock contention and one-fetch valid mapping proof.
- Produces: owner-side abort and unlock/finally process-log accurately separated from waiter cancellation and independent valid termination. Releasing of aborted sleep alone cannot prove an in-flight holder.

- [ ] **Step 1: Hold the lock during another real HTTP entry**
Launch genuine holder using fresh profile and direct actual `GenreRepository.get()` / public gateway lock, with proof its descriptor owns the lock while fixture has not responded. Record no row/source shortcuts and PID/thread.

- [ ] **Step 2: Trigger abort without killing process**
Use a deployed synthetic abort predicate or notifier/adaptor callback so production handler takes current existing abort path/throws `TMDBAbortError` and executes `finally`. Require cleanup to clear owner lock and close only owned session/response while preserving a real kernel-release timestamp. Distinguish that from the fixture finishing/another actor releasing it earlier.
- [ ] **Step 3: Verify recovery does not replay queued/invalid work**
Acquire/recheck at a later legitimate call, observe no abandoned file-lock active, no secret/invalid committed payload, and startup stable dependency/library teardown. Main active-background HANG is retained separate and not retested for rescue claim. A leftover invalid/unsupported temporary map is not a completed usable cache.
- [ ] **Step 4: Cleanly stop**
RPC `Application.Quit` must complete with code0/no forced cleanup after actor returns or the abort path has unwound. If lock remains unavailable after response+unwinding until timeout, mark owner-release FAIL and skip final admission. No second“recovery” is allowed to manufacture UI progress.

---

### Task 3: Independent gate evidence compaction and response

**Files (disposable only):**
- Create: `verify_admission.py` and `admission-verification.json` under new root
- Create workspace report only: `.superpowers/sdd/2026-11-25-persistent-public-genres/admission-report.md`

**Interfaces:**
- Consumes: actual case artifacts and source hashes from Task 1/task2, not narrator claim or old genre matrix.
- Produces: explicit `ADMITTED: YES/NO`, public lock viability proof/no implementation authority, and per-check diagnostic evidence.

- [ ] **Step 1: Machine-verify topology and contention**
Require actual first HTTP-entry before second lock attempt, with no second first-response before that attempt. Capture native thread identities/PID distinction correctly, initial request-inflight proof and all-before/after close/release events.



- [ ] **Step 2: Verify single GET and owner-abort release**
Validate first request exactly one path, valid public schema, no user secrets, correct commit+reload+no-second-request and response.close. Owner abort type/finally release and exact kernel/event times required; no `checks_passed` boolean alone can decide. Missing entries mean **UNCHECKED**, not PASS. Normal quit is independent of lock viability proof.
- [ ] **Step 3: Run gate verifier and retain stop condition**
Use actual pre-saved artifacts: `python -B verify_admission.py`. Exit 0 only if all native conditions are truly PASS; otherwise nonzero and specific evidence link for FAIL/UNCHECKED. No stale edits modifying saved artifacts to hide first negative outcomes or setup discards. Lock attempts causing another raw fallback request immediately fail gate.



- [ ] **Step 4: Report without authorizing implementation**
Document precise OS-lock mechanism/timing with same-invoker evidence, workload, abort release fact, remaining durability/hostility limits and direct-support state. On NO preserve the option to revisit per-invocation request limits without cache. On YES ask for new storage implementation plan: how small public sidecar/exact validated file schema is integrated into owner + GenreRepository with deterministic two-process/invoker tests, generic malformed-file handling and final new native genre-bounded matrix. No happen-once merge/source commit or release authority derived from passing this admission gate. Independently inspect gate code and saved events before giving verdict.

## Self-review

Two different Gates distinguish Windows descriptor semantics in-flight from post-response reuse and aborted-owner release from waiter release-disabled process. Tests/episodes are not mistaken for actual production genres by merely exercising a fake client on ordinary OS threads. The selected wrapper may not make network requests on behalf of an unaware UI loop through the back door to decorate the graph. Results always have codepoint/history/source identity and no release/state-back reversal to an arbitrary service queue. Scope includes both Native acceptance and explicit negative contradiction/remediation stopping if OS locks cannot support persistent dedup under the same process's actual usage.
