# Persistent public genre cache design

**Status:** proposed design after user chose **Perzistentní public žánry**. No storage, lock or production change exists yet.

## Problem

Actual Kodi native four-category navigation of `c1bcdf7` selected `/3/movie/popular`, `/3/tv/popular`, `/3/movie/top_rated`, `/3/tv/top_rated` once each, correctly. But shared genres ran **2× movie + 2× TV**, while the user requirement is at most one of each across that user journey.

`GenreRepository` currently stores `_values` and `_pending` only in memory (`yawsp_app/tmdb.py:591-634`). That is useful within its one interpreter, and existing concurrent tests intentionally protect it. A newly created plugin invoker/owner gets a new empty map, so subsequent valid categories re-fetch the same public genre map. Correct existing in-memory behavior is insufficient for the actual single-process user journey through several native invocations.

## Goal and hard boundary

For a given genre group (`movie` or `tv`) and localized TMDB language (`en-US`, `cs-CZ`, `sk-SK`), validated public genre metadata is fetched at most once across separate invokers using the same add-on profile. Native InfoWall, public source scope and current sessionless transport, including public 401 isolation, remain unchanged.

This is **not** a generic metadata service, queue, watchlist state protocol or cross-interpreter cache replacement. The general service/queue migration stays reverted. Only public genre ID→localized name mappings are stored. No private media, watchlist data, account/session, request URL, token, transport object or callable crosses interpreters.

## Chosen design

### A small sidecar cache owned by the add-on runtime

Add `GenreFileCache` in a focused new module `yawsp_app/genre_file_cache.py`. The production owner constructs one with **new** files in its existing addon profile:

- `tmdb_genres.json` — only sanitized public genre data;
- `tmdb_genres.lock` — a separate OS-lock file used only to serialize this exact cache's transaction.

No existing `tmdb_watchlist.json`, `tmdb_trailers.json`, `tmdb_shows.json`, user database/text, permissions, options, migration, data deletion or permanent-backup copy is created/modified by this design. Unknown preexisting/unsupported genre files are treated read-only and never overwritten as a migration trick. The builder must not package these generated userdata files in runtime ZIP; tracked module code is admitted by existing `yawsp_app/*.py` selection.

`GenreRepository` gains an optional `file_cache` argument. No argument preserves exact existing memory/condition behavior for lightweight tests/callers. With it, lookup uses shared storage semantics plus the current condition/set for its process. `_ProductionOwner` alone supplies profile-backed cache to its already-created single GenreRepository shared with its TMDB UI. Watchlist calls can safely read the same public localized map; no personal snapshot data enters it.

### Storage schema and validation

A JSON document contains only `{"schema": 1, "genres": {"movie/en-US": {"<positive integer TMDB id>": "<nonempty localized name>"}}}`. Actual id-to-name mapping uses string forms in JSON to avoid key-type ambiguity. Lifecycle is the same as current repo: valid cached genre mappings do not expire on a timer (previously they lived only for the owner's memory lifetime). No new time-based refresh clock or per-item burst is introduced.

Validation before reading or writing:

- only a strict `schema=1` root with the required `genres` mapping;
- only `movie`, `tv` mapped against that exact existing language enum; no arbitrary media or locale key path;
- positive integer IDs and nonempty bounded strings for public names; no booleans/NaN/special arrays/nested session/personal fields;
- a detached copy per read (no caller aliases the saved document);
- empty validated mapping from the server remains valid, but failed HTTP/parse does not create a negative/error timestamp or cache hit.

Malformed/unsafe/unsupported data is treated as unavailable/miss. Existing bytes are not rewritten on that failure path. A valid new fetch may still serve current UI, but is not claimed as persistent storage if the file cannot be safely handled. Ordinary existing TMDB/public error propagation is retained and no fake empty genre is used to hide real transport or write errors.

### One bounded cross-interpreter transaction

For a key, the transaction under an OS-backed shared lock performs exactly:

1. reload and validate the current file;
2. if a valid value exists, return it detaching the value;
3. if missing, claim ownership of only this lookup (outside flow already holds the current process-local `_pending` entry);
4. perform the existing client GET and strict genre-list validation;
5. while still owning the lock, re-validate/merge only that key into a fresh detached copy of the current document so another media/language's nonpersonal valid values cannot be overwritten;
6. serialize deterministically as UTF-8 and atomically replace the JSON from a temporary file in the same directory (flush + fsync + close), then release;
7. re-read known-good file on another request instead of issuing HTTP again.

The lookup networking is a **current interactive lookup** by the same caller. Holding OS lock through validation/write exists only to prevent concurrent same-key duplicate requests and lost update during load→merge→replace. A waiting awoken invoker must reload and can find a completed value, so its extra HTTP must never happen as a race/different owner fallback.

Use Python stdlib only: Windows `msvcrt.locking`, POSIX `fcntl.flock`, with tiny bounded lock-attempt retries handled by an explicit lock helper. It opens the lock safely without truncation/link traversal and checks the OS handle after opening. `O_NOFOLLOW` is used where available, with safe regular-file fallback validation. Another application's foreign prelude/protocol is not parsed and no hardlink state, pending/active terminal docs or lease-recovery scan exist.

Locks are automatically released with the descriptor by context manager and OS cleanup on process termination, so restart cannot leave a lease job to re-execute. Lock files aren't "pending work", are never interpreted as executable or credentials, and do not require a startup purge/PID envelope. Temporary genre-file artifacts are cleaned only if they're ours and replace didn't commit, with no delete of original user cache unless atomically accepted new valid data already existed.

Lock waiting checks `client.abort` at bounded short intervals and raises existing `TMDBAbortError` without waiting a join on another thread or starting a new one. Otherwise it waits for the kernel to release an actual owner's lock. Existing request timeout/retry/abort behavior is unchanged; this is not a redesigned timeout or a shutdown fix. A stale foreign lock description never causes a generic drop/reconnect—Kodi will return as a normal current caller/abort path permits. If OS locking/storage cannot be safely used, record the concern and use only that invocation's in-memory read semantics without overwriting unsupported files or falsely claiming persistent cap compliance.

No secret is ever included in new logs/provenance. Reproduction/fixtures log only operation/media/language/key/count/state/raw file hash sanitized to the generated genre cache path (no request URL queries or personal settings).

## Alternatives and limitations

- **Process-local only:** was measured FAIL for four native categories (2×movie + 2×TV). It cannot meet the requested journey-wide bound without an untruthful scope change.
- **Opaque shared generic job/service or broad metadata revision protocol:** rejected. Its concurrency and cache-generation review found material issues not present in this small immutable public-map case.
- **Persistent sidecar:** Chosen by user. It adds one new data format and narrowly scoped OS lock based only on public genre data, not replacement of all caches or introduction of per-job closure transports. Repeated invalid/file-unavailable states are clearly not claimed to meet the whole-cache TTL bound.

Lock acquisition waiting must not be promoted into a shutdown solution for the independently reproduced active metadata worker HANG. That HANG remains outside this design. A fresh native process checking one completed owner can still perform pending interactive genre work. A crash/revisit that occurs during its abort must leave only OS-releasedLOCK, not arbitrary unfinished new request jobs.

The very small root-key set at most 2 media × 3 locales is bounded by schema validation. No unbounded queue/document backlog, full directory reconciliation or unknown key IDs are accepted. The lock is not a public factory for service uses. Ordinary metadata/worker 32+1 limits and parent/media lifecycle are untouched.

## Builder clarifications — immutable before admission

1. **PythonInvoker topology is measured, not assumed.** Although expected to use distinct Python interpreter contexts/threads inside one OS process, this must be proven by actual thread/PID records in an isolated Kodi run. A separate subprocess-only test is not proof of in-process invoker behavior. OS lock acquisition must be demonstrated under that exact thread-and-handle layout, both while owner HTTP is in flight and during owner abort teardown.
2. **Acquisition retries may bound the IO wait, not change dedup semantics.** Use a short per-attempt or nonblocking OS-lock attempt loop. Once one candidate owner is known, another invocation either acquires and re-reads the store, is cancelled via the existing `client.abort` typed abort path, or fails a defined storage error/fallback. **It must never fall back to unlocked HTTP because lock acquisition timed out** and must never treat a pending fetch as evidence that retrying unlocked HTTP is safe. There is no guaranteed wall-clock acquisition cutoff beyond abortable cancellation/OS release; the actual ongoing owner remains governed by current request/retry/abort semantics, which this design does not alter. Define an outer storage-error type for permission/platform issues and preserve its log without host secret values.
3. **Hostile filesystem exchanges are outside the security guarantee.** The protocol standardizes cooperating addon PythonInvokers, their descriptors, and accidental preexisting link or malformed files. It does not claim resistance against a malicious actor with the same user's filesystem privileges performing concurrent reparse or rename swaps. Any accidental link or nonregular path fails source validation and is never overwritten. Unsupported payloads are never treated as valid metadata or silently migrated.
4. **Canonical key construction remains exact.** `kodi_language()` returns only `en-US` when it is not Czech/Slovak, `cs-CZ` for `cs`/`Czech`/`cs-*`, and `sk-SK` for `sk`/`Slovak`/`sk-*`. It is the same string supplied as current `params["language"]`: no transformed alias, translated endpoint or cache-only locale name. File keys correspond to the same request. Other locales used by third-party callers retain existing per-instance/memory semantics, are not file-persisted with a different spelling, and are not rejected or assigned another API language. No rewrite of tmbd client/GenreRepository request parameters occurs.
5. **Fresh profile creation is a prerequisite only at the normalized owner-provided `self.profile` path.** If absent, the new helper may `os.makedirs(self.profile, exist_ok=True)`, creating that exact addon profile and any needed parents. No alternative or arbitrary directory, migration, permission rewrite, or other add-on's folder is created. Error paths report non-secret profile/error classes and may not silently trade a valid cached acquisition for a persistent cache promise.
6. **Durability means ordinary completed transaction and process reload, not guaranteed hardware consistency after power loss.** Flush+fsync+close before atomic replace and reload under lock provide complete value/recovery proof on process exit. POSIX directory fsync cannot promise an equivalent Windows power-loss contract here. It is fine for a power failure to lose one new valid genre file and cause a fresh public lookup later; that has no user database/database identity implication.

The following admission gate precedes any production cache design. Its failure means a per-process/invocation discussion or halt, not automatic replacement with another singleton/mutex design.

## Native lock admission phase 0 — before implementing persistent storage

Use an isolated new deployed clone with the **actual production public invocation path**, not a direct host `subprocess` substitute. The temporary annotation/helper eligible for the probe may serialize a real `GenreRepository.get()` or public `/3/genre/{type}/list` request using the candidate OS lock, but no production module/config/file is edited.

- Record actual process ID and native/current thread IDs for at least two separate native plugin invokers using the same fresh profile and same localized key. Verify whether they share OS PID (expected separate PythonInvoker/OS threads) and cannot be summarized as two processes.
- Claim and hold the proposed OS lock during one *actual short synthetic public genre HTTP-inflight* operation. Only local delayed loopback, bounded by existing request timeout/retry/abort behavior; no external service or real API call.
- Contend for the exact lock from the second genuine invoker. Prove whether it waits at the OS lock boundary. Its callback MUST NOT make a second HTTP while first request is active. If the lock does not contend at all on Windows same-process byte locking, the proposed `msvcrt.locking` as a cross-invoker mechanism is **NOT ADMITTED**; no later helper or `_pending` sleep can mark this as a valid guarantee.
- Release after the original valid request/valid document commit. The second invoker must read the newly available valid mapping without making another request. The count for this key is exactly one; metadata response and `response.close` evidence stay intact.
- Explicitly interrupt an **owner while it holds the lock/inflight request** using the synthetic abort predicate/harness action rather than forcing the host Kodi process. Measure owner abort unwinding and descriptor release, distinguishing failed lock ownership from Python/HTTP finally release. Check temporary artifact commit rules, reserved key cleanup and eventual normal process exit. If abort leaves an owner unable to release lock before process teardown, report exact failure — it cannot be turned into a new indefinite lock acquisition through some callback fallback.
- If Windows lock semantics are unsuitable, another actor owns no valid entry, or temporary file/data behavior does not meet root-merge consistency, stop. Do not translate to native-mutex+file queue work as an opportunistic fourth task because that would be a different architecture requiring a separately approved design and proof.

Only this admission proof is permitted before detailed implementation planning. No claim that OS locks are safe simply because process descriptors theoretically close should bypass the native same-thread emulation. A phase-0 PASS admits the implementation tests below; a phase-0 FAIL preserves the current `NATIVE UI` requirement failure without a consolidation to publishable cooperation.


## Required proof (only after phase-0 admission)

### RED-first deterministic tests

1. Two independently constructed real `GenreRepository` instances with distinct test clients and the same sidecar: current behavior fetches same key twice; desired persistent behavior downloads once. Return detached genre map each time.
2. Two actual OS processes contend while one request is in progress under shared sidecar lock. First writer persists; second awakens only after release/reload and makes no second HTTP. Distinct media/locale keys retain both values, one valid request per key.
3. During abort after another process owns lock, the wait raises `TMDBAbortError`; no local joining/interpreter fallback HTTP/temp stale write or pending leak happens.
4. Missing/valid empty documents, malformed JSON/root/type/extra identity fields/zero or noninteger IDs/NaN/path-key mismatch and over-limit/non-string names do not become trusted hits, and unsupported original bytes are unchanged. A symlink/nonregular lock/data path cannot truncate or redirect owner's bytes.

### Live acceptance

After committed code review, run the same exact real matrix that returned 2× each against a **fresh profile**:

- one lookup of each movie/TV genre total, not per page or per invoker;
- all four list calls still exactly once each;
- old InfoTag/title/year/genre/rating/plot/artwork/InfoWall/content and missing/error/401 behaviors unchanged;
- original three CSS-scoped languages use their own cache key (a second language may fetch once independently);
- explicit separate-invoke provenance plus shared-genre file contents/strict valid schema and request-family counts. No metadata prefetch account or public personal identity changes.

Inspect real Windows lock acquisition/stream release on Kodi 21.2 Windows Store x64, including same-profile second invocation off another media/lang. Do not accept offline mocks as proof of Windows `msvcrt` semantics. Actual pixel appearance remains unproven even when native Input/control data is real.

A final archive includes the new helper only if it passes source/test checks; dynamic file inventory must be recalculated after commit. No automatic documentation “resolved” claim exists before live POST-CAP evidence. No merge, push, tag, release or main-profile writing is authorized.

## Implementation discipline

The builder may choose precise helper names/byte-lock retry internals but must not silently substitute a generic shared JSON cache that has only instance `RLock` or returns a new root snapshot without reloading under lock. Keep the protocol specific to public genres only. No Redis/network locks, arbitrary operation document/path or deadline/socket rework. Every failure is preserved rather than ignored as fixed cache guarantee.

If this design cannot be proven on actual Windows cross-invoker locking without loss of nonpersonal values, it does **not** graduate to production: fall back to a separate discussion of per-invocation limits or stop integration instead of adding architectures incrementally.
