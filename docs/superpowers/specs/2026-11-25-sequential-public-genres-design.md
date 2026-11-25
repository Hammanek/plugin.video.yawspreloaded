# Sequential public genre sharing design

**Status:** proposed narrow follow-up after user explicitly chose “Sekvenční sdílení.” This is a distinct contract from the rejected/unproven OS-lock concurrency admission experiment. No production cache exists and no concurrency guarantee is implied.

## Actual baseline and scope boundary

On current `c1bcdf7`, native four-category operation produced one expected list call per category, but **two movie + two TV genre lookups**. `GenreRepository.get()` (`yawsp_app/tmdb.py:591-634`) holds `_values` and `_pending` only in interpreter-local memory, so each new native invoker/owner starts empty. A later attempt to use `msvcrt.locking` did not establish a second real invoker; the actual second window activation was refused while Busy remained active. The final gate was therefore **inconclusive, not a concurrency PASS or an MSVCRT defect** (15 PASS / 0 FAIL / 10 UNCHECKED).

User opts to make only the **sequential user journey** the goal: after the prior native category completes, a new ordinary invoker must reuse a validated public mapping. This does not imply a general parallel-safe queue, owner-abort release claim, generic cross-interpreter transactional cache, nor a fix for independent active-background shutdown HANG.

## Chosen mechanism — use the existing JSON primitive narrowly

Add a small optional **public genre sidecar** behind `GenreRepository`. Reuse the existing `AtomicJsonCache` in `yawsp_app/json_cache.py` as the file primitive; do not add any generic lock/service/lifecycle protocol.

Production `_ProductionOwner` creates its existing shared `GenreRepository` with a file-backed `AtomicJsonCache` at the normalized addon profile `/tmdb_genres.json`. The first caller/valid generator may create that exact profile directory as the existing save primitive already supports. No extra `.lock`, pending/active/manifest jobs, thread, queue, startup recovery, server exception transport, named mutex or closing callback is added.

### Interfaces

- `GenreRepository(file_cache=None)` retains current in-memory `_values` and condition behavior if omitted.
- The optional file-cache interface accepts a tiny strict validator/loader via the existing atomic JSON transaction (or a focused module only if warranted by test clarity) with a key `(media, language)`. It can load misses, and after a validated successful GET persists one key's sanitized mapping through the existing `load → merge → save` primitive.
- `_ProductionOwner` supplies that path exactly once to the shared genre repository. Both ordinary public discovery and watchlist genre calls may reuse the same public localized map; this doesn't create a new transport or sharing of session objects across invokes.
- Missing/unsupported cache state is never an authority for a schema migration or a new general revision rule. Return only validated data after caller reload; unknown preexisting/unsupported bytes are left unchanged and don't count as a successful persisted hit.

For file-enabled lookup:

1. Reload the current document through the existing primitive and check its strict schema before treating a cache key as hit.
2. For an existing matching key, return a detached ID/name mapping, never an alias. This occurs for the new native invocation *after the previous response's commit*, instead of an HTTP lookup. Overall request accounting is per actual genre type in a sequential valid-series experiment.
3. If there is no valid matching key, take the existing ordinary public client get for the selected media and language (same exact path and params as current), parse/close and validate the values.
4. Merge only this key plus already-valid nonpersonal genre entries, preferably with the existing `AtomicJsonCache.save_merged()` in the owner instance, which flushes/fsyncs/closes its temp and does `os.replace` same-directory. If persistence cannot be applied, let this invocation continue with current valid mapping and log the store failure through existing logger behavior, without claiming dedup on any future read or pretending correction of any invalid raw file.
5. Record a failed sequence honestly. No local `_pending` leak or forced second lifecycle global lock workaround; existing repo's condition notification/local dedup ownership remains unchanged.

This uses process-instance atomicity of the existing primitive, not cross-process lock coverage. It deliberately does not claim that a simultaneous a-and-b invocation retries the same key without an additional request. The O/S-lock experiment failed to produce a native contender entirely; it is not an implementation template here.

## Canonical data and identity privacy

A strict document contains only `{"schema":1,"genres":{"<media>/<language>":{"<positive integer id>":"<localized genre name>"}}}`. Root contains exactly these fields, no request URL, media records, snapshot fields, credentials, PEK/polyglot or token. Entry keys are restricted to the three actual mode/language combinations used by the addon (`movie|tv` × `en-US|cs-CZ|sk-SK`). Keep the request parameters `language` exactly equal to those map keys without transforming them.

- Positive integer JSON IDs as string keys; nonempty finite bounded localized names. No booleans, NaN, negative/zero IDs, list/object names, over-limit types or embedded personal fields. Reject mismatched root/key shape and unsupported schema selectors rather than partially trusting/mixing them.
- Every read validates the **whole** supported document before a merge. Invalid/unknown input is never overwritten to make this test green. If its format is unsupported, use current invocation behavior, record that persistence is unavailable, and don't return an invented cached document.
- Empty validated map is a successful mapping if API actually returns empty array. Distinguish a present key with empty map from a missing/invalid key. Failed HTTP, malformed server payload and closed malformed response never create a negative/error cache entry or timestamp.
- `kodi_language()` currently returns only `en-US`, `cs-CZ`, or `sk-SK`. Other languages supplied by legacy/custom callers retain existing process-only get, with identical language passed to the public API in both cached and uncached lookup. Do not reject a supported old caller's different language or rename its key to force shared semantics.
- Validated genre mappings continue current memory-lifetime semantics: no automatic per-hour/TTL expiry or metadata prefetch schedule is introduced. Their public nature makes them shareable across ordinary account contexts without sharing user account/session.

The regular `TMDBClient` transport, nested exception redaction and `_PublicTMDBClient` behavior stay unchanged. Public 401 has no authority over personal watchlist settings/snapshot.

## Anti-goals

- No `xbmc.service`, new worker/process, OS lock argument for a broader global transactional state, callback serializing, network-based lock or generic cache/lifecycle fallback.
- No modification of ordinary watchlist cache mixes, trailer/show metadata, BackgroundPrefetch reservation fairness/32+1 policies, media/disk worker completion, RPC timeouts, daemon state, or the independently reproduced shutdown HANG.
- No purge of unknown userdata/backup data, filename migration, true POSIX directory-fsync-as-Windows-power-loss safety claim, or commit authorization from file presence alone.
- No reusing the Artifact-only negative verifier's hardcoded **INCONCLUSIVE** labels as a future proof of functional deduplication or measured MSVCRT failure. Every later claim must use the actual produced artifact/case.
- No forced GUI parallel actor claim or a `sleep()` used as concurrency evidence. Actual sequential event ordering must be demonstrated. If operationally the next callback can't start before prior category response (as captured in the product), that is exactly what this narrower design covers—not a general GPU/UI scheduler contract.

### Read admission and commit protocol

The write primitive (`AtomicJsonCache.save`/`save_merged` helpers such as its `os.replace` and flush) is useful, but its **public `load()` must not be used as a decision boundary for this cache**. `AtomicJsonCache.load()` can return an older valid in-memory document after unreadable/malformed current input. That would hide both a first corrupt file and a later corrupt replacement.

Instead, a dedicated strict candidate reader reads current bytes from the normalized sidecar path, using bounded 128 KiB UTF-8 decoding and stdlib JSON parsing with **duplicate-key rejection at every object scope**. It returns exactly one of three states:

- `MISSING`: only an OS-confirmed file-not-found, never an invalid/malformed read converted into a miss;
- `VALID`: the actual bytes form exactly the supported sanitized schema (all its format/encoding/key restrictions);
- `UNSUPPORTED`: any I/O, encoding, duplicate key, JSON shape, root/schema/key or value violation, or an unsupported/personal selector.

Before classifying, candidate path is checked for regular-file source admission (`lstat` and safe OS type/handle checks where available). Symlink and other nonregular paths yield `UNSUPPORTED`; reads never truncate and path components never come from API metadata.

No merge, trusted hit or write is allowed in `UNSUPPORTED`. The file remains untouched, and no older `_data` may substitute for a failed current-byte read to preserve healthy-looking results. Missing-file creation is permitted only from a successful validated public response, using the existing primitive's directory creation and replace behavior—not during `UNSUPPORTED` admission.

Only already `VALID` current document and current transaction can merge another sanitized genre mapping. Other extra/unknown/public member keys violate the schema deliberately, so unexpected inputs don't get swallowed by root validation. This includes genuine duplicate observable genre IDs in member mapping, object-level schema fields and name attributes. `json` must not silently normalize duplicates by last write before validation.

Additional semantics:

- One transaction unit is this invocation's newly validated public data. A different cache/GenreRepository instance is instantiated only after the writer's close+save+replace+successful reload. A next read needs its own fresh candidate path and actual cached value. Domain logic must not pretend that instance-level RLock protects cross-invoker generation or that an old supported file is still there after a malicious exchange.
- Hostile/concurrent actor **rewrites are deliberately outside a concurrency guarantee**. Lack of an OS lock here does not allow calling the primitive cross-invoker-safe. Only the ordinary ordered write/commit then receiver relationship is measured. On unknown unsafe paths or changed unsupported bytes no migration/overwrite is authorized as success.
- `MISSING` is confirmed unavailable file, not a missing genre key: `VALID` with entire schema but no matching key yields a regular fetch. A supported key with an explicitly present empty mapping is a genuine validated hit and must not be turned into a miss via truthiness.
- Whole-document store provenance is deliberately allowed only for generated sanitized records; validity proves a shape and excluded identity fields, not whether human-supplied valid-looking genre names are semantically personal text. There are no user-specified private metadata or credentials mapped in, but no false claim that a string's content can be classified as automatically nonpersonal.
- `json` keys must be canonical ASCII decimal integers in range (for example `1`, not `01`, signs, Unicode digits, whitespace or booleans); names use nonempty non-whitespace existing strings from valid API entries. Root numeric `schema` must be integer **1**, not `True`; all predicates require `type(value) is int`, never a bool-as-int shortcut.

Entry parsing follows current API behavior structurally: supported client JSON `genres` must be an array of objects and current filters on integer/string fields are retained. Filtering/dropping an invalid/nonpositive-ID/blank-name entry in the new sidecar does not turn a valid overall response into a new transport 30402 path solely because it has such an entry. Only sanitized modes are persisted; invalid ignored raw metadata is simply not stored. When complete server data is valid, its content is unchanged. New strictness is specifically for storage/trust decisions rather than making valid older rows fail.

### First commit before subsequent reader

Persisted completion is not response.close, API `get()` return or just DirectoryResult.done. Require **all** of these events in explicit order:

`public HTTP 200 → response.close → data fetched+validated → store admission reload+validated merge → flush/fsync+close+save result True → os.replace commit → strict validation reload returns VALID`.

Only after this commit-observed time may a later native category invoker begin. If save fails, existing memory entry `_values` is committed and `_pending` is released/notified within existing condition handling so the same owner continues with current in-memory value and waits properly. A subsequent separately constructed owner **must fetch again**; no dedup is claimed. In particular a cache/Filesystem or save error or replaced bad store must not make its later directory appear to have satisfied merge/reload. When native after-commit reopen returns metadata, any later request must be a regular list request, never another genre call because discovery lists are intentionally not cached.

A failed save/indispensable current-byte corruption is retained as a cache unavailability signal, not cleaned up across converters or into generic locks. A later valid response may repair only when its transaction admission is `MISSING` or valid same-schema current data and atomic save succeeds. No unflagged `_values` stale close is allowed to mutate pending ownership.

## Builder clarifications for schema and semantics

- Six public keys are permitted: **two media (`movie`,`tv`) × three languages (`en-US`,`cs-CZ`,`sk-SK`)**, not the earlier mistaken phrase “three combinations.” Other locale/media constructors retain their exact old process-only logic and strings; no new language translation in requests.
- Per-category sequence for final valid user journey: root 0 list/genre; `popular_movies` 1 movie-list + 1 movie-genre; `popular_shows` 1 TV-list + 1 TV-genre; `top_rated_movies` 1 movie-list + 0 genre; `top_rated_shows` 1 TV-list + 0 genre. It is a countercontract for genre persistence, not discovery-page cache reuse. Any direct later repeat of the list itself gets the expected ordinary list request; forbidden extra calls refer specifically to `/3/genre/{media}/list`.
- The input empty-list case differs from an empty genre-map value. `results=[]` must yield no genre call and no persisted genre data. Empty genre map explicitly present on a supported sidecar key is valid persisted metadata, not absent key.
- Actual production DirectoryResult/saving must be assigned atomically before later native calls, then row source still re-renders correctly from cached genre names with title/local numbers/media type and current native InfoTag. No generic fileroot/goodlooking error prompt substitutes all stepped user navigation. First response closing has to precede writing’s declared filename replacement independently, not by visual position above stack/line numbers.

The rest of this proposal intentionally does not claim general parallel locking or mechanical unlimited synchronous generation. It is the narrowly selected case of standard *ordered* first valid result then second scan with optional later streams using same profile document.

## Required artifact behavior for other inputs

1. **Direct current-code/document boundary RED:** show persisted current `_values` doesn't bridge two cache owners; accepted new behavior makes physically two separate newly initialized real GenreRepository objects use one total public GET after second owner is constructed after previous valid commit. Repeat with a forced client error: no negative-cache hit fabricated. Detaching map return must not leak alias mutations.
2. **Commit ordering test:** first saved root is only sanitized strict schema. Second repository at later time reads and validates it without another client call; its request count is strictly **1**, not the current dual fetch. Both movie and TV keys validate independently, with exactly 1 each in appropriate sequence (not unlimited other-language keys).
3. **Formats/permission cases:** missing file parent (allowed normalized addon profile creation); malformed/root/unknown schema/non-integer IDs/bools/NaN/empty names/setup directory unsafe paths ignored without overwriting initial bytes. Unsupported old file hash remains identical. Object returns are detached; no mutable in-memory leak makes a later caller accidentally revise stored maps.
4. Save failure/logging has no noncancel fallback made-up success, no net-new generic job. Compatibility of current in-memory tests is preserved by default argument. Actual response.close + nested exceptions and current credentials omitting behavior stay green.

## Final native proof on actual sequential route

Run the same real fresh-profile sequence that failed before for four category choices:

- Normal root → Objevit (4 localized entries, zero request).
- `popular_movies` finishes with an observable successful DirectoryResult, valid strict schema saved and response closed.
- New native invocations then select `popular_shows`, `top_rated_movies`, `top_rated_shows` with complete prior response/commit times recorded. Their valid new mappings may trigger the first genre fetch for that media/lang only. All four list paths remain exactly one per category.
- **Total genre calls across the chosen normal sequence must be exactly 1 movie + 1 TV**, not a min/max average per callback nor an in-memory-only counter. Subsequent same-category open should reuse with no new fetch. Prove callbacks return normalized rows and InfoTag types/date/runtime matching native values, not only a JSON file.
- Explicitly establish second native callback occurs after first complete transaction, not a scheduling illusion during earlier request. Record PID/native thread/handle/time_ns as evidence of actual sequential invocation, not of unproven concurrent locking.
- File content includes allowed schema+keys only, event list excludes account/SessionAuthorization/watchlist snapshot operations and no metadata prefetch. Public 401 identity protection, empty/error30440/30402, Invalid API key and invalid-source behavior remain unchanged.
- Use a fresh loopback/isolated Kodi 21.2 Windows Store x64 clone excluding old portable_data, plugins/source config or secret state. Expose no request query/credentials in logs. Do not rerun/reclaim the independent legacy-active-background HANG.

Any regression without ordered response and subsequent callback is not evidence of this sequential requirement. A missing file/repeated identical group still failing means this design is not ready and should not be promoted via another lock architecture. Phase 3 must compare actual immutable final Git commit/source package and perform independent functional, privacy/security and performance review.

## Reports and acceptance semantics

When approved for implementation, use focused TDD before edits, then checkpoint commit, complete unittest discovery, Python 3.8 AST, XML/PO/diff check and recomputed release verifier including any helper module. Use history/hashes for source/doc preservation and ordinary process-exit cleanup. Document only sequential after-response sharing; do not claim “persistent genres are concurrency-safe”, “all interpreter caches are now transactional”, or shutdown problem solved.

No merge, push, release tag/upload or main-profile deployment is allowed by this spec. Permission here is for this exact narrow proof and release-gating only after it actually succeeds, not implicit license for broader service discovery.
