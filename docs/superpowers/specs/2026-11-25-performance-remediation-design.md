# Performance Remediation Design

## Intent and why

The read-only performance audit of `functional-remediation` identified nine remaining costs after the functional refactor: full synchronous watchlist paging, whole-cache writes and redundant loads, repeated whole-Backup-DB parsing, invocation-blocking downloads, eager menu authentication, an admission-capped rather than draining metadata queue, quadratic history deduplication, memory-buffered ZIP installation, and unprofiled download chunks.

The user requested all optimizations and explicitly selected a **persistent stale-while-revalidate watchlist snapshot** rather than progressively rendering an incomplete list. This preserves correct global Czech/rating/year sorting while allowing a warm listing to render without any watchlist HTTP request in the UI invocation.

The work stays on the current `functional-remediation` branch, with no merge, push, tag, release, or version bump in this project.

## Goals

1. Implement all PERF-01…PERF-09 fixes without weakening previously approved functional, security, recovery, or completion contracts.
2. Make a complete cached watchlist available immediately and refresh it consistently in the background. Preserve global sorting, account isolation, and invalidation.
3. Eliminate redundant UI cache/database reads and replace per-item whole-cache fsyncs with small bounded batches.
4. Ensure metadata work drains fairly rather than silently dropping excess IDs or repeatedly starving behind failures.
5. Separate long media transfers and large ZIP processing from plugin invocation while preserving streaming, validation, atomic completion, and failure reporting.
6. Establish before/after evidence using operation counts, blocking handoffs, synthetic repeated timing benchmarks, and the full regression suite.

## Non-goals and limits

- No claim that offline desktop timings equal Kodi/ARM/VFS latency or throughput.
- No real account, API, network, filesystem outside test fixtures, or live Kodi verification without separate explicit authorization.
- No globally sorted UI assembled from only the first API page. Correct global order takes precedence over apparent pagination speed.
- No narrative “optimized” claim without a directly compared operation count or fresh benchmark result.
- No public API/setting/version changes except strictly necessary new internal localized job-status messages.
- No new runtime dependency, data-loss migration, mandatory delete/reinstall, or cross-process correctness claim.

## Approaches considered

### A. Persistent snapshot and bounded background execution — selected

A complete account/language-scoped snapshot is displayed immediately on warm opens and refreshed asynchronously. Global sorting runs locally over the complete snapshot. Metadata and download jobs are bounded separately so one large transfer cannot starve TMDB metadata.

**Tradeoff accepted by the user:** after changes made on another device or TMDB session, snapshot content can be temporarily stale until background refresh completes. Correct watched actions, account changes, and failures invalidate it deterministically.

### B. Progressive incomplete listing followed by global re-render

This gives a quick cold first page but makes navigation temporarily incomplete and may create disruptive Kodi refresh cycles. It retains the same cold-network total and was rejected by the user.

### C. Reduce functionality or use local first-page sorting

Rejected: it breaks all-items ordering and changes user-visible semantics merely to show a lower request count.

## Constraints and preserved contracts

- Current branch/base: `functional-remediation` at `2087699530bb9ba332f50aab6b53695dab184a4d`.
- Existing public plugin routes, setting IDs, localization fallback, and `main.py`/`yawsp.router` entry point remain compatible.
- Existing 192 tests remain the initial regression gate; new tests are standard-library `unittest`, fully offline, and run through `python -m unittest discover -s tests -v`.
- Service modules retain the no-`xbmc*` boundary. Production lazy adapters may provide thread-safe notification/settings facades and session factories.
- `ActionRunner` completion is exactly once per invocation. Returning from a queued job accepts only queue submission; it is not a success notification for the underlying network/media operation.
- Background success notifications occur only after actual remote confirmation/complete validated file promotion. Existing `.part` cleanup, HTTP status checks, short-write handling, response closure, safe filename containment, atomic local/VFS replacement, and Backup DB rollback/recovery remain mandatory.
- Snapshot persistence uses `AtomicJsonCache` and contains no API keys, session IDs, credentials, or tokens.

## Unit/file boundaries

New focused modules are permitted when needed:

- `yawsp_app/watchlist_snapshot.py` — account/language/media-scoped snapshots, validation, freshness/throttle policy and merge/invalidation.
- `yawsp_app/download_jobs.py` — bounded asynchronous transfer job manager, session ownership, status callback protocol, close/abort semantics.
- `tests/performance/` or focused test modules alongside current tests — deterministic operation-count/latency/RSS benchmark helpers, no wall-clock thresholds on shared machines unless measuring a controlled event handoff.

Existing `tmdb.py`, `tmdb_ui.py`, `json_cache.py`, `backup_db.py`, `downloads.py`, `webshare.py`, `webshare_ui.py`, `runtime.py`, and `router.py` retain their defined responsibilities. Lifecycle ownership remains in one production owner, with explicit idempotent close.

## Domain contracts

### PERF-01 — Warm watchlist snapshot and cold fallback

- Snapshot key includes **TMDB account ID, watchlist media (movies/tv), metadata language, and schema version**. One account/session cannot display another account’s snapshot. API keys are not persisted in it.
- Valid record contains a complete results list, created timestamp/signature, monotonic revision, schema version, and optional refresh-in-progress metadata. Partial or malformed results are rejected without replacing the previous complete snapshot.
- **Warm path:** return the complete snapshot for immediate filtering/global sorting/ListItem creation without watchlist or account HTTP calls. Native account ID lookup must not be triggered just to identify a warm key before identity has been recovered. The cache’s own current identity key is maintained by successful authenticated flows.
- **Cold path:** if no complete compatible snapshot exists, retain the current carefully validated full-list retrieval and persist it only after every page succeeds. A cold error leaves no partial valid snapshot.
- Refresh uses stale-while-revalidate with an injectable freshness/refresh interval (default five minutes). Multiple opens cannot start duplicate account/media refreshes. Successful full refresh atomically replaces the same-key snapshot and increments revision/date. Failure preserves the prior snapshot and permits bounded retry.
- Refresh duplicate-page accumulation is impossible: results are keyed/rebuilt by full requested pages, validated for completeness, and not appended to old snapshots. `total_pages` and page-loop behavior remain bounded by validated response metadata and an explicit safety cap.
- Successful watched removal removes or invalidates the current account snapshot immediately and schedules refresh; failed removal leaves the snapshot untouched. Manual/forced refresh bypasses only the throttle, not validation or deduplication. Session/account invalidation forbids reuse of the old identity snapshot.
- Empty results do not fetch genres. Genre mappings are shared per process by `(media, language)` with locking/deduplication, independent of snapshot age, and refreshed on language change.
- Every open still creates Kodi items from local validated data. No on-demand trailer or episode API request is reintroduced into the drawing loop.

### PERF-02 — Cache read and write coalescing

- The listing resolves cached trailer/show values through one repository/read pass per cache. It passes the computed miss lists to submission; submission does not re-read caches just to rediscover identical misses.
- `background.submit_*()` is scheduled via `DirectoryResult.post_complete`, so reservation/cache revalidation happens after `endOfDirectory`. Tests assert ordering and that the row-rendering cache-only path remains synchronous-request free.
- `TrailerRepository.lookup`/`ShowRepository.next_episode` retains immediate persistence for on-demand actions. Worker-specific paths support deferred save: writes coalesce once per job or at a small fixed checkpoint (maximum eight successful new/updated records), then flush in `finally` for completed work.
- Atomic replacement, lock semantics, legacy migration, TTL, stale fallback, no-error-caching, and failure logging stay intact. Unpersisted worker data is cache-only and must not affect authoritative UI/state.
- Target evidence: no duplicate listing read, no submission before completion, and no more than eight successful changes per cache flush; full 32-record jobs have at most four flushes rather than 32.

### PERF-03 — Backup DB warm metadata validation and streamed install

- Installation still parses and validates every extracted JSON document once, checks safe archive members, covers all extracted data CRCs through verified reads, writes the manifest, and promotes atomically.
- New and migrated stores receive a sidecar validation signature outside the data directory, preserving legacy actual database bytes. Signature records schema version plus relative filenames, sizes and modification identities. It does not trust path alone or last-year mtime without size identity.
- `ensure_installed()` on a matching fully validated signature only scans O(F) file metadata and performs no complete JSON parsing. `load()` reuses the unchanged signature and reads/parses only the requested file. Any missing, malformed, extra, size-changed, or mtime-changed file triggers full revalidation/signature regeneration or repair; the corruption tests remain strict.
- No `ensure_installed()` result depends on network when valid local metadata proves installation. Interrupted/mismatching install falls back to current full install and rollback/recovery path.
- Archive downloads stream to a same-profile temporary seekable ZIP file with HTTP status checks and response closure, not `response.content`/`BytesIO`. `ZipFile(extracted_all)` reads all members and therefore detects CRC errors without a separate full `testzip()` decompression pass. If this proves not to cover an archive case, that case must make CRC verification explicit rather than silently skip it.

### Cold Backup DB installation is asynchronous too

- `PERF-04` covers **both media downloads and a missing/invalid Backup DB installation**. Installed/warm DB opens synchronously from local metadata without network queues.
- A missing/invalid DB installation has a single profile-scoped in-flight reservation. The invocation returns a small directory containing a localized “installation in progress” status item. Concurrent opens cannot install/extract twice or start unrelated partial extraction jobs.
- Only after accepted queue handoff does the invocation complete. The background worker owns Webshare revalidation, archive link/GET, streaming ZIP, validation, and atomic promotion, using the same status lifecycle and worker-owned-session rules as media downloads.
- A successful installation generates its actual success notification and exactly one guarded `Container.Refresh` after promotion. Refresh must not race extraction or expose a partial DB; it is specific to this completed cold install and does not reopen the unrelated metadata-prefetch refresh ban. Failure/cancellation generates only failure state, not success or refresh.
- During queued/running installation, listing/`load()` calls either return the same status item or wait for the freshly validated atomic store to appear; they never read an incomplete extraction. A failed prerequisite releases the reservation so a later open can retry. Existing legacy/corrupt-directory recovery and duplicate-output protections apply.

### PERF-04 — Bounded download jobs and immediate handoff

- `download()` validates locally available invocation parameters/destination shape, queues a unique job and returns promptly through the existing action adapter. Long validation/revalidation/file-info/GET/stream/promote work executes in one dedicated download worker, not on the plugin thread.
- Download jobs use separate worker-owned HTTP sessions to avoid concurrent use of the UI session. They never invoke `endOfDirectory`/`setResolvedUrl`. User feedback goes through a thread-safe injected notifier/lifecycle protocol; real “completed” is emitted only after actual replacement.
- The worker supports queue submission failure, duplicate destination policy (reject duplicates instead of uncontrolled overwrite), quota, monitor abort, idempotent close, and explicit statuses `queued/running/succeeded/failed/cancelled`. A closed worker rejects new work and cleans partial output. Pending jobs are resilient to a failed prior job (one failure cannot poison the queue).
- Tests prove controlled handoff by event, not arbitrary 1 ms timing: baseline holds a stream while invocation is blocked; queued execution returns without releasing the stream. They also prove actual completion and all failure paths with synthetic local/VFS adapters.
- Any API-controlled mount/rename atomicity caveat remains explicitly live-runtime-only. Worker use does not imply that a VFS rename is atomic on a remote server.

### PERF-05 — Lazy Webshare authentication

- Static main menu renders immediately with zero Webshare request. Remote search/queue/history/play/download actions retain their current authentication/transport contract.
- Installed local Backup DB can be browsed without forced Webshare revalidation. It authenticates only if installation/update or a queue operation actually needs Webshare.
- Invalid/absent token is discovered at the operation that requires it, preserving one meaningful recovery notification and completion.

### PERF-06 — Draining metadata policy

- Existing `max_pending=32` is a burst guard, not permission to drop the rest permanently. Submissions larger than capacity enter a bounded deterministic pending plan or an owner-managed continuation whose pending keys drain by completion and/or next open. No metadata family can silently disappear because it came after slot 32.
- Trailer-only prefetch is limited to the currently visible snapshot/page (the complete snapshot may be locally paged for rendering if later profiling requires it; this does not change global sort). On-demand lookup remains available for every item.
- Show/trailer scheduling alternates fairly between families. Error outcomes have short retry backoff rather than permanent cache writes, so the same failing 32 items cannot monopolize every subsequent open. On-demand and worker share in-flight reservations, and shutdown/abort releases pending reservations.

### PERF-07 — Linear history deduplication and request reuse

- Replace list membership with a stable hashable key preserving the current dictionary-equality effects for normal entries. Use `(ident, name)` only if valid for current endpoint; otherwise use a canonical recursively hashable encoding that preserves current semantics.
- If removal finds no valid `download_id`, render the already downloaded history response instead of requesting history again. Positive clear behavior and idempotent location semantics stay unchanged.

### PERF-08 — Covered by PERF-03 streamed ZIP processing

Success eliminates whole-archive `response.content`/`BytesIO` duplication and the redundant decompression pass: network/archive I/O streams to a seekable temporary file with bounded chunks and validated lifecycle.

**Memory-bound clarification (builder-check resolution):** mandatory structural validation still uses standard-library `json.load()` for one JSON member at a time. Peak memory therefore includes that member’s decoded text and native objects (and can approach the largest member’s size plus JSON overhead), not an arbitrarily bounded byte amount. No new streaming-JSON dependency or speculative hard cap is introduced because the archive’s legitimate maximum member size is undocumented. Boundary/RSS evaluation must state this limitation explicitly and compare against the old full-archive copy, rather than claiming constant total memory independent of the largest JSON member.

### PERF-09 — Chunk sizing with measurements, not assumption

- `stream_download` retains an injectable `chunk_size` optional argument. Default up to 256 KiB if repeated desktop/local/VFS-double measurements support it, reducing call count 4× over 64 KiB without materially increasing peak memory. No 10 GiB buffering or `response.content` behavior is introduced.
- Tests retain identical data, counts, closed response and failure semantics. True local/SMB recommendations remain conditional on later real backend profiling. A lack of measurable benefit must result in retaining 64 KiB rather than changing a constant merely for appearance.

## Measurement and regression plan

The pre-change offline reference facts recorded in the audit include:

- 1000 Czech titles sorted in 52.989 ms and 500 filenames matched in 7.384 ms; these are not priority bottlenecks and require no micro-rewrite.
- 1000/2000 history deduplication takes 25.794/97.835 ms in synthetic dict/list comparisons.
- 27-file Backup DB install-plus-load does 55 JSON reads (27 + 28), with full structural parsing ~18 ms for the test fixture.
- 1000-entry AtomicJsonCache save median 13.18 ms and fresh load median 5.31 ms; 5000-entry save 54.65 ms and load 26.40 ms. Worker currently writes up to 32 times.
- QR generation is a one-off 56.36 ms and remains out of the optimization path.

Each task must provide:

1. Before-and-after operation counts or a controlled event/latency benchmark for its changed hot path, with deterministic utilities reusable after optimization. Arithmetic timing summaries must use repeated medians/ranges, not a single cold run.
2. Strict RED→GREEN functional regressions for failure/cancel/error/migration paths. A performance improvement that regresses a previously passing functional test is not accepted.
3. Full discovery plus AST/XML/resource/static-boundary checks per implementation task.
4. Before/after evidence files in task’s SDD workspace, including exact commands, synthetic dataset dimensions, machine-independent counts, and honest timing limits.

Final acceptance targets:

- Warm compatible watchlist snapshot: **zero watchlist/account requests in UI**, one local full global sort, refresh scheduled without blocking.
- Cold empty snapshot: current complete/validated fallback behavior with no partial snapshot or local-first-page mis-sort.
- No metadata scheduling before completion and no per-item full-cache write in workers.
- Matching Backup DB warm signature: no full validation JSON reads; every UI `load()` parses only the target file.
- Static menu: zero Webshare request; installed DB needs none unless a remote operation occurs.
- History work linear and no history reload when no IDs are removed.
- Large downloads return after accepted queue handoff while actual completion still follows validation/promote; memory remains streaming/bounded.
- Every prior audit-ID behavior selector and the complete discovery suite still passes.

## Deferred to builder within these contracts

Exact snapshot/member class names, internal key serialization, queue data structures, maximum checkpoint size within the specified cap, notification status names/internal result models, sidecar location/name outside the data directory, and conservative threshold for benchmark selection are implementation details. No choice may silently reduce data validity, generate incorrect global order, reuse an account’s data across identity, or declare a queued transfer completed early.
