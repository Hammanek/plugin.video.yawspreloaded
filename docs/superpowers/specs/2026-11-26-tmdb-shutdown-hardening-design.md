# Bounded Native TMDB Lifecycle Design

## Status and decision

This design refines the previously selected **candidate B: a bounded new native concurrency protocol**. It does not restore historical commits `75522d4` or `3b08755`; their directory reservations, startup scans, batch publication, terminal files and reaper are rejected. Reused concepts are limited to the native single-consumer topology, closed typed descriptors, secret-free session binding, OS service lease and no-parent-fallback result sharing.

Implementation is not approved by this document alone. A reviewer must approve the design and plan, then an external native **Gate 0** must pass before production files change. No merge, push, tag, release, installation or main-profile deployment is part of the work.

The validated source baseline is `051893e8dd8b0028dcae13b7291254d1dad5c012` on the isolated `shutdown-hardening` branch. Baseline evidence is 398 passing tests with the known Windows symlink-privilege skip and a successful tracked-source archive check. `usability-improvements` and released `main` remain untouched.

## Goals

1. Move service-managed TMDB transport and every TMDB metadata/snapshot mutation into one native Kodi service invocation, with no secondary Python worker thread.
2. Make background directory handoff nonblocking and interactive waiting specific to an intended typed operation and abort-aware.
3. Enforce **32 background reservations plus one interactive reservation globally per add-on profile**, including every native invoker, with separately bounded backlogs and result retention.
4. Share accepted same-key callers through one job ID, one underlying execution/result (one request for single-item operations; the existing bounded page/list+genre sequence where applicable) without another reservation or parent HTTP fallback.
5. Make registration, admission, claim, follow-up, finalization and recovery owner-fenced, transactionally serialized, idempotent and bounded.
6. Preserve the existing native Objevit/watchlist UI, typed TMDB outcomes, response/session cleanup, error redaction, complete-snapshot fallback and media download behavior.
7. Prove native concurrent callbacks and active shutdown on isolated Kodi 21.2 Omega Windows Store x64 before claiming lifecycle readiness.

## Non-goals and compatibility boundaries

- Do not change Webshare, `DownloadJobManager`, media queue/history or their independent worker.
- Do not restore WindowXML/Netflix experiments, add autoplay, mutate watched state during listing, paginate Objevit or resolve a personal account for public discovery.
- Do not add generic callable/closure transport, a parent executor, daemon-only lifecycle claims, forced joins, forced socket close, socket-timeout changes or a synchronous metadata drain on the UI thread.
- Do not add an application dependency. Use the standard library, the declared Requests/Unidecode dependencies and existing bundled modules.
- Do not treat Python 3.8 AST acceptance as Kodi 19 or other-platform runtime proof. The only claimed live platform remains Kodi 21.2 Omega, Windows Store x64.
- External visual acceptance remains separate and unproven; native callback and InfoTag checks are not pixel review.
- README version, release links and `addon.xml` version remain at the current released version until separately approved release preparation.

## Topology and ownership

```text
native plugin callbacks                           native xbmc.service callback
(read-only caches, Kodi UI)                       (single serial consumer)
        |                                                     |
        | typed submit / join / wait / acknowledge            |
        +----------------> manifest under OS transaction -----+
                                  |                    |
                         OS service lease              +-- service-owned sessions
                         starting/ready/stopping       +-- named domain operations only
                                                       +-- metadata/genre/snapshot writes
                                                       +-- bounded terminal/result ACK/GC
```

`service.py` only adapts `xbmcaddon`, `xbmc.Monitor`, profile paths and logging. Protocol, store, client and worker modules do not import `xbmc*`. The service loop executes direct calls on its own native invocation; it never constructs `ThreadPoolExecutor` or another application thread.

Parent invokers never close or join the service. Their `close()` only acknowledges/detaches their own waiter and closes invocation-local authentication/Webshare resources. Service shutdown marks `stopping`, rejects new admission, aborts queued work without HTTP, completes normal unwinding of at most one active request, closes its owned response/session, commits permitted final writes and releases the OS lease in `finally`.

A five-second Kodi application-stop warning while delay-bound socket work unwinds is not itself a failure. `CPythonInvoker(...): waiting on thread ...`, a surviving secondary `PyThreadState`, forced process cleanup or a non-zero/hung process exit is a failure.

## Operation scope

Only named descriptors can execute work. Raw URLs, paths, method names, callbacks, responses, sessions, credentials and arbitrary dictionaries are not executable payloads.

| Operation | Reservation when submitted by UI | Service-owned behavior | Parent behavior |
| --- | --- | --- | --- |
| `metadata.trailer` | Background prefetch or interactive playback | Existing trailer request, validation, success/missing/error semantics and cache mutation | Read cache; interactive waits for typed `TrailerLookup` |
| `metadata.show` | Background prefetch | Existing next-episode request, real-success flag/stale fallback and cache mutation | Read cache only |
| `watchlist.refresh` | Background warm or interactive cold/manual | Identity binding, complete pages, generation-checked complete save, durable forced continuation | Read exact compatible complete snapshot; fallback after typed failure |
| `watchlist.watched` | Interactive | Watched POST and latest-generation snapshot removal plus required follow-up | Resolve Kodi action and refresh only after handled success |
| `discovery.catalog` | Interactive | At most one fixed list GET and, for nonempty rows, at most one shared genre GET plus strict genre-file save | Validate local source; render returned detached rows and Webshare handoffs |
| `details.seasons` / `details.episodes` | Interactive | Existing named GET and response validation | Render returned detached rows; no transport fallback |
| `identity.invalidate` | Interactive follow-up to explicit auth/settings change | Scoped snapshot invalidation with current identity binding | QR/session exchange may remain explicit auth transport; no direct snapshot mutation |

All metadata/cache/snapshot writes therefore have one writer. This includes cold/manual watchlist saves, watched removal, background refresh, on-demand trailer/show lookup, public genre persistence and cache migrations. Merely moving background refresh is insufficient.

Parent `TrailerRepository` freshness/legacy loading must be a genuine read-only adapter. Legacy conversion is performed only in the service-owned migration/write path, never as a side effect of parent cache reads. `WatchlistSnapshotStore` mutators and `GenreFileCache.store()` are not called from parent UI composition.

QR creation, polling and session exchange are explicit authentication, not a metadata job, and remain abort-aware parent operations. Identity compare-and-set and delayed 401 handling use the OS identity lock described below; a service-local `RLock` is not accepted as cross-invoker protection.

## Exact document and resource bounds

Protocol schema is `1`. Limits are profile-global, not per client, interpreter or worker instance:

| Resource | Exact bound |
| --- | ---: |
| Live background reservations across all operations | **32** |
| Live independent interactive reservations | **1** |
| Background backlog awaiting a reservation | **64 job records** |
| Independent interactive backlog | **8 job records** |
| Total pending/active/terminal job records before GC | **128** |
| Registered interactive waiters across all jobs | **128** |
| Unwaited terminal results retained for collision reuse | **32 newest** |
| Expired-result tombstones | **32 newest** |
| Manifest serialized bytes | **262,144 (256 KiB)** |
| One detached typed result document | **1,048,576 bytes (1 MiB)** |
| Aggregate live terminal result bytes represented by the manifest | **16,777,216 bytes (16 MiB)** |
| Visible IDs offered by one watchlist open | existing **50** |
| Complete watchlist pages | existing **1,000** |
| Deferred cache successes per flush | existing **8** |

A result with registered waiters is never evicted. Terminal jobs with no waiters are reaped oldest-first until both count and aggregate-byte bounds hold; each eviction leaves a bounded tombstone codec `RESULT_EXPIRED`, not `NOT_FOUND`. Results still referenced by waiters count toward the total-job/aggregate bounds, so retention cannot grow without limit. If safe admission or publication cannot fit a bound, the caller receives typed backpressure/service-unavailable; no fallback HTTP is allowed.

Each job has a random 32-lowercase-hex `job_id`, monotonically increasing manifest `ordinal`, normalized 64-lowercase-hex SHA-256 `dedup_key`, operation enum, exact operation parameters, `state`, owning `service_epoch`, claim/fencing token, request generation where applicable and a deterministic result reference. Job IDs are never ordered lexically for fairness. Keys hash canonical UTF-8 JSON prefixed by `tmdb-service-v1\0`; input Unicode must be valid scalar text and unpaired surrogates are rejected without changing original files.

Manifest and result readers reject unknown/extra fields, duplicate JSON keys, bool-as-int, nonfinite numbers, noncanonical IDs, malformed UTF-8, symlinks/nonregular files and trailing/oversized input. A malformed job is quarantined/poison-isolated where possible; it cannot stop unrelated handler construction or authorize execution.

State layout is fixed under the add-on profile:

```text
tmdb_service_state/
    manifest.json
    transaction.lock
    identity.lock
    lease.lock
    results/<job_id>.json
    results/<job_id>.tmp
```

A job's final/temp result reference exists in its manifest record before a result file can be created. Startup and GC therefore clean only bounded manifest-referenced paths; no arbitrary directory scan is needed. Atomic temp/write/fsync/close/replace and validated reread are required. Temporary cleanup after a committed transition is reconciling cleanup and cannot reverse that transition. Directory-name/power-loss durability beyond ordinary local Kodi profile support is reported as a limitation, not claimed.

## Atomic admission, key ownership and scheduling

`transaction.lock` is an OS byte/flock lock, separate from the lifetime `lease.lock`. Every manifest compare-and-create/update/delete operation holds `transaction.lock`, reloads current bytes, validates, constructs a detached successor and atomically replaces/rereads it. A local `RLock` may reduce same-instance contention but is never the correctness boundary.

A new reservation and its visible key owner/job record are created in the **same transaction**. Release compares `(job_id, service_epoch, claim_token)`; stale recovery, old producers and old finalizers cannot delete replacement ownership. Temporary creation failures occur inside the guarded `try/finally`; committed registration/publication is authoritative even if later temp cleanup fails.

Service startup holds the lifetime lease before entering the manifest transaction. It writes a fresh random `service_epoch` and `starting`, aborts/quarantines all previous-epoch pending/active work without HTTP, cleans only referenced results, sets reservation counters to the recovered ledger and only then publishes `ready`. Submitters accept only `ready` plus a held competing OS lease. `starting`, absent lease, disabled service and `stopping` return typed `UNAVAILABLE`/`SHUTTING_DOWN`; background work is never falsely accepted for inevitable startup discard. Fresh post-barrier submissions belong to the new epoch and cannot be swept as old work.

A batch submission of up to 50 visible keys resolves each key independently in one or more bounded transactions. Existing keys attach to their winning job; only noncolliding keys allocate reservations. Reaching 32 creates bounded backlog entries rather than dropping the 33rd–40th keys. A full 64-record backlog or other hard bound returns a per-key `BACKPRESSURED` result while retaining unrelated winners/attachments. No single collision rejects a whole batch.

Accepted same-key background or interactive callers observe the same `job_id`, terminal outcome and detached value. An interactive join creates only a waiter record, never a second reservation. A colliding queued/background job is marked interactive-priority and may be promoted without disturbing the 32+1 accounting. A waiter admission at the global 128-waiter/byte bound is typed backpressure and still cannot invoke its supplied operation closure.

One consumer executes one HTTP operation at a time. Scheduling rules are:

1. Finish the already active operation; abort does not fabricate socket cancellation.
2. Select independent interactive work before all not-yet-started background work.
3. Within background metadata use existing trailer/show round-robin; otherwise use manifest ordinal FIFO.
4. When a reservation becomes available, promote the oldest eligible bounded backlog record under the same transaction.
5. Recheck global priority before each next metadata item, so eight-success write coalescing cannot starve interactive work.

Metadata retry state includes `attempts` exactly `0|1` and finite `retry_at`. One transient failure gets the existing single deferred retry, never a negative-cache timestamp. Terminal transport errors remain typed memory/protocol results, not cache hits.

Waiters register their random invocation ID before waiting. They poll only their registered `job_id` with injected abort-aware waits (0.05-second slices, returning immediately on abort), never an unbounded `Condition.wait()`. Terminal read validates the result bytes and stored SHA-256/digest before returning a detached value. `finally` acknowledges that exact `(job_id, waiter_id)`; duplicate ACK is a no-op. Cleanup failure does not turn a validated terminal success into a new execution failure or a duplicate request. Unknown/expired/stopping/abort outcomes map to typed service errors or existing `TrailerLookup("error", None)` as appropriate.

## Terminal, follow-up and persistence transactions

The only legal execution transitions are `queued/backlog -> active -> succeeded|failed|aborted`, guarded by the current service epoch and claim token. A repeated identical terminal apply is an idempotent no-op; a conflicting second transition is rejected without killing the consumer. Active cleanup, reservation release, temporary removal and result GC are separate reconciliation steps. Failure after terminal commit must not call generic reject or attempt a second terminal.

A watchlist key binds account/media/language/schema, force intent, captured generation and versioned non-secret session-binding digest. A forced request at generation N+1 cannot join and disappear behind an older generation-N request solely because scope matches. Remote session tokens are resolved only from current service settings at execution; raw values never enter manifests, results or logs.

When a watched removal or identity change invalidates an active refresh, the required newest-generation follow-up is inserted/coalesced in the same manifest transaction that accepts the mutation or finalizes a generation conflict. If continuation already exists, its desired generation/force state advances monotonically. It must not live only in a producer's local dictionary or be published after the predecessor's terminal commit.

Snapshot operations preserve exact account/media/language/schema checks, complete-all-pages validation, immutable committed results, monotonically increasing generation/revision and prior snapshot bytes after refresh/persistence failure. A successful watched operation removes from the latest complete rows and increments generation before any Kodi refresh. Failed directories do not trigger extra metadata work.

Only service-owned repository objects may perform migration or deferred cache flushes. Up to eight validated successes coalesce; final successful/abort/error unwinding flushes completed valid successes without caching failed work. Parent reads return detached values and cannot alter disk. Since parents no longer perform competing metadata/snapshot writes, the service consumer supplies the transaction boundary across interpreters. The OS identity lock separately serializes compare-and-set of current session/account settings between native QR and service-originated invalidation:

- delayed old-session 401 invalidates only if raw current session still exactly equals the captured session;
- QR replacement compares expected identity under the same lock, writes new session/cleared account and then waits for the named service invalidation/cleanup result where required;
- identity/network waits never hold `transaction.lock`, `identity.lock` or the lifetime service lease across HTTP;
- identity invalidation deletes/tombstones only matching account scopes and cannot erase a newer session/account.

Committed valid metadata/snapshot files remain readable normally after service restart. Their presence never grants replay permission to an aborted manifest job.

## Error, privacy and transport contracts

`TMDBError` and `TMDBAbortError` remain the UI-facing domain hierarchy, extended by typed service-unavailable, backpressured, shutting-down and result-expired causes/results. Protocol codecs admit only fixed sanitized error codes and fixed messages; never serialize/reconstruct arbitrary exception messages, class paths, tracebacks, Requests objects or response content.

Existing Requests behavior remains in `tmdb.py`: exact request timeout/retry rules, abort-aware retry sleep, `429 Retry-After` integer/date handling, public-sessionless 401 behavior, response close rules, session-binding digest and transport redaction through nested `__cause__`/`__context__` chains. Service errors cannot trigger parent HTTP, generic closure execution or a local cache write in an error handler.

Manifests/settings/result evidence contain no raw API key, session token, Webshare credential, arbitrary URL/path or secret-bearing exception text. Test and live-native fixtures use synthetic loopback identities and data; logs are inspected recursively for fixture credentials as well as accidental real settings.

## Preserved user-visible contracts

- Root order remains **Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist**.
- Objevit keeps exactly four immutable catalogs. One selection performs at most one list GET and one shared genre GET per media/language, with no account resolution, pagination, trailer/show burst, watched mutation or autoplay. Rows keep artwork, localized title/year/genre/rating/plot, InfoWall behavior and exact localized/original `action=search&what=<localized>&sort=largest` Webshare handoffs.
- `refresh=1` remains explicit manual refresh. A completed manual operation never schedules an immediate duplicate automatic refresh. Failure preserves/renders the last exact complete snapshot when available and never saves partial pages.
- Warm snapshot opens have zero parent account/watchlist calls; background data appears on later open with no automatic container refresh. Watched success keeps its existing single post-action refresh semantics.
- Existing six public genre groups, strict 128-KiB schema/current-byte file admission and valid Unicode behavior remain. Sequential commit semantics are now stronger because the only writer is the native service; file validation is not misrepresented as an OS transaction lock.
- Media downloads continue independently, with no generalized claim that their worker has been migrated.

## Required proof gates

### Gate 0: external native feasibility before production changes

Build only an external disposable probe under `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-shutdown-hardening-gate0`, using isolated fresh Kodi profiles and controlled loopback fixtures. The current legacy failure is first reproduced, then the proposed direct-service/locked-ledger topology is exercised. No repository production file or real profile is touched.

Required machine-checked evidence:

1. **Two active native waiters:** while one service fixture response is held, two native plugin callbacks A and B enter the real interactive path for the same normalized key before the response. Same PID, distinct invocation/thread IDs, one GET/session/owner, equivalent typed results and no parent HTTP. RPC `OK` alone proves nothing; event ordering is `A.request_job_registered < B.callback_entered < A.response_sent` and both callbacks overlap in flight.
2. **OS lease/admission:** a second native contender cannot acquire the held service lease or execute a second consumer/request during delayed HTTP. With one response held and the rest immediate, 40 distinct background calls finish exactly once through bounded continuation while live reservations never exceed 32; adding independent interactive work reaches exactly 33, not 34. The 33rd distinct background plus second independent interactive receive their defined bounded wait/backpressure, not silent loss.
3. **Same-key fan-out:** separate client/store instances register a colliding waiter while A is active. One active claim, one request/session, one terminal/cache transition and equivalent validated results are observed by both callers; neither a duplicate-reject-only result nor closure fallback is accepted.
4. **Owner abort:** abort the service owner during active delayed HTTP while B waits. Response/session close as applicable, owner lock release, waiter/reservation cleanup and typed B outcome are recorded. Early socket wake is not required; a secondary helper Python thread is forbidden.
5. **Three fresh 12-second exits:** the external prototype performs three independent runs. Each sends one delayed HTTP 200, observes `Application.Quit=OK` during flight, closes the response object when one was returned, and always completes session/finalization/service cleanup, process exit `0` under the harness 25-second guard, no forced cleanup and no `CPythonInvoker ... waiting on thread` for any invoker. Queued/active/backlog are empty afterward; work is aborted rather than completed/replayed.

Failure or unobserved contender execution is `FAIL/UNCHECKED`, not scheduling success. Passing Gate 0 approves implementation topology only; it does not approve the final artifact.

### Final integrated-artifact acceptance

Repeat all five native gates against the final packed source in three new isolated profiles, including three new active 12-second shutdown exits. Historical prototype exits do not count. Collect monotonic event sequences, PID/invocation/thread/job/reservation/lease identities, fixture request/response/close stages and real process status before any cleanup.

Run independent functional/cache, security/privacy and performance reviews, full offline tests, Python 3.8 AST checks, resource/XML checks and `scripts/verify_release.py`. Archives must match exact final source bytes, root, version and CRC and exclude symlinks/userdata. CI actions and permissions remain unchanged/read-only and pinned.

External visual review remains a separate gate. Only after lifecycle and visual reviews may the user be asked for explicit merge/deploy/release approval; release preparation must then synchronize every README feature/setup/install statement and version badge with `addon.xml`.

## Test matrix and failure regressions

Every implementation task is RED-first and finishes with `python -B -m unittest discover -s tests -v`. At minimum, deterministic tests cover:

- cross-instance overlapping registration/cache/snapshot updates without lost unrelated changes;
- forced newer watchlist generation, identity/language switch, watched removal and exactly one durable forced follow-up;
- previous pending/active records and fresh publication across the startup barrier without HTTP replay or fresh-job sweep;
- overlapping multi-key batches where winners, collisions, 40-item continuation and full backpressure retain all noncolliding siblings;
- tempfile/publication failure after reservation, committed transition followed by cleanup failure, repeat terminal/ACK and malformed single-job poison isolation;
- global reservation/backlog/result/waiter/byte limits and GC that never evicts live reservations or registered unread results;
- no-read-side migration writes, no parent HTTP/executor/thread/session for service-managed work, no direct closure path and typed unavailable/abort/busy conversion;
- strict document schema, duplicate keys, unknown fields, nonfinite/bool values, malformed UTF-8/surrogates, symlinks and unsafe result files;
- nested transport/log redaction, response closure, stale 401 versus QR replacement and public 401 identity preservation;
- the complete existing native UI, genre, watchlist, resource and archive suites without weakened assertions.

## Explicit residual limitations

The service lease prevents two live owners, not a permanently hung network stack. Interactive waits and socket completion remain abort-aware/cooperative; shutdown is not advertised as instantaneous. The design proves no Kodi 19, ARM, NFS/SMB or remote-VFS runtime behavior. Result-size/backpressure errors are deliberately safe outcomes for pathological oversized data, not permission to fetch in the parent. Local atomic replacement and fsync do not prove machine-power-loss durability of directory names on every filesystem.
