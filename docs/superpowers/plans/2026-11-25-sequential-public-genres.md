# Sequential Public Genre Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task, with independent task reviews. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Reuse validated public genres across separately constructed native invocation owners, so a completed valid response → atomic write → validated reload → later ordinary invoker produces exactly **1 movie + 1 TV genre GET** during the same four-category user journey.

**Architecture:** Introduce one tightly scoped strict reader/writer at `tmdb_genres.json`. `GenreRepository(file_cache=None)` keeps its current memory behavior unless production opts in. The production owner provides the sidecar; actual response closes and public validated map commits before any subsequent native invocation opens and reads it. There is no cross-process lock or service, no queue, no generic metadata migration and no claim of concurrent freshness, ownership-abort release or solving a background HANG.

**Tech Stack:** Python 3.8, stdlib unittest, existing `requests` sanitized TMDB client, `AtomicJsonCache` for atomic temp/flush/fsync/close/replace, native Kodi 21.2 Windows Store x64 for sequential evidence

**Spec:** `docs/superpowers/specs/2026-11-25-sequential-public-genres-design.md`, including builder clarifications on raw-byte load status, duplicate keys, valid-first commit, canonical keys/values and exact category deltas.

## Global Constraints

- Precomputed current source stays isolated; current root/source is `4cf492081d15c775a967e69c15a9ffd5b2945c1a` plus only subsequent documentation before this plan. `NATIVE UI` previously FAILED for genre counts 2 movie + 2 TV. There is no active `GenreFileCache` or `tmdb_genres.json` in production.
- **Actual read outcomes:** `MISSING` only real file-not-found/parent-absence; `VALID` after supported raw UTF‑8 doc validation; `UNSUPPORTED` for IO/permission, nonregular/symlink source, malformed/duplicate keys, >128KiB, noncanonical numeric representation, invalid names/schema, unknown fields or provenance selector. Never map read-error onto hit/miss.
- Strict document root only: `{"schema":1,"genres":{...}}`. `schema` must be integer **1** not bool. Supported key exact `movie|tv` / `en-US|cs-CZ|sk-SK`: six total. Must not modify unknown data bytes or write extra/raw private fields.
- JSON member keys must be canonical ASCII decimal positive integers: `0` or first `[1-9][0-9]*`. No leading zeros/signs/whitespace/Unicode digits. IDs retired values use current TMDB public ID/name data; preserve structural current API filtering and ignore only invalid raw inputs for storage without inventing reasons for a 30402 after otherwise valid response.
- Once persisted, valid **present empty `results` map** is hit. Do not implement cache truthiness check that confuses empty map with absent key. Empty public `results` never calls GenreRepository by itself and never persists a genre event. Missing genre-map key means regular one-off fetch, not cache miss on a valid empty map.
- File reader supports nonregular/symlink rejection without truncating or redirecting unrelated target bytes. This addresses cooperative native invokers and accidental malformed/preexisting inputs, not defenses against malicious same-user reparse swaps. No general cross-process-safe promise exists.
- After valid response **must close** before validated map fsync+close/save/replace+re-read. Current private/internal object’s `_data` is **not evidence of raw bytes read or current committed record**. `GenreCacheRead.reload` after writer's commit is the canonical proof; `DirectoryResult` or response.close alone does not establish persistence.
- Existing `AtomicJsonCache` transaction/load+merge+save gets no global magical power; it only performs the current owner and already validated snapshot’s atomic write as its own stdlib implementation. Need distinct cache **and** repository instances in all tests proving separate owner logic. On save failure, preserve current `_values` and release `_pending` properly with notification. A later new owner refetches, without producing a fidelity claim.
- `public_TMDBClient` hides session but retains API key/locale and inherited sanitized errors/retry/abort responses. No request URL, session/account snapshot or private scope is persisted. Public 401 has zero authority to mutate personal watchlist settings or cache file. Validate raw server payload only after true production client handling. No negative/error cache on 401/503/transport parse/malformed server payload.
- Other media/languages use the original memory path, no sidecar reads/writes, and default no-argument constructor remains backward-compatible. Only new normal-route behavior adds shared valid localized info. No language transformation other than existing `kodi_language()` returning exact `en-US`, `cs-CZ` or `sk-SK` and using identical string in `params["language"]`.
- No new response deadlines, timeout/daemon/worker path, `xbmc.service`/script/WindowXML, named mutex, pipeline of documents, manual generic full inter-interpreter shared metadata lock, or large atomic lock server. Main real profile/executable/process/RPC9090/credential and existing backup archives are untouched. No merge/push/tag/release/distributed CI claim without separate final consent.
- New files are only tracked helper/production/tests/docs. Generated private sidecars/temp are userdata only and don't enter ZIP. Do not hardcode release inventory in verifier tests or production; recompute after commit (expected 26 with helper if tests pass).
- Focused TDD precedes implementation/checkpoint. After each commit run complete discovery and release verifier. Every important issue gets regression-first correction, fresh scoped review and not silent overwriting unknown evidence.

## Review Focus

- ` load()` returning older data can mask unreadable/corrupt disk: Task 1 explicit current/raw-lstat/read states, second-instance validity and fresh detached values.
- Malformed root/duplicate keys/leading zeros/True schema/unsupported keys cannot sneak into atomic merge: Task 1 fail-closed cases and byte-preserved original.
- A successful fetch followed by write failure can leave pending work or hallucinated second-owner reuse: Tasks 1–2 notification/in-memory behavior plus mandatory fresh-owner subsequent fetch.
- Evidence of second invoker starting before writer’s replacement rather than after: Task 4 actual `.replace` timestamp before ordinary later native callback, then cached genre exact row and useful Webshare handoff.
- Main spec goals could be conflated with classical transactional/concurrent service, patched OS locks or daemon-shutdown fix: all task reports explicitly scope to sequence only; no new permission to resurrect any rejected design.

---

### Task 1: Implement strict file-backed genre reader and atomic-validated write

**Files:**
- Create: `yawsp_app/genre_file_cache.py`
- Create: `tests/test_genre_file_cache.py`

**Interfaces:**
- Consumes: existing `yawsp_app.json_cache.AtomicJsonCache` as an atomic-write primitive, its logger/error-handling contracts. No external dependencies.
- Produces `GenreFileCache` supporting stable strict canonical group keys `(media, language)`. Read outcome is explicit `(state, sanitized_document_or_none)`, states `MISSING`/`VALID`/`UNSUPPORTED`. Public getter returns detached mapping for existing group (including `{}`), and typed boolean store applies only after successful check + save/replace. Getter never reuses old `_data` as fresh disk truth. Other caller-owner browsers cannot accidentally interpret root error as supported cache document.

- [ ] **Step 1: Write strict schema and IO behavior regressions first**
In a temporary profile per case, exercise missing file (only ENOENT), raw valid doc, missing key, exact empty genre map (hit empty), malformed and duplicate documents, invalid root, unexpected record family, non-int schema True, unsupported genres/media/language fields, leading zero and Unicode digit key, zero/negative id, whitespace/empty/over-limit name, NaN/JSON specials, recursion (>bound if feasible), >128KiB input, symlink/nonregular. Assert first doc has `schema=1`, actual public positive integers as JSON string keys and nonempty strings only, not raw URL/session or flavors names. Each invalid read is UNSUPPORTED and no candidate write preserves prior raw byte hash everywhere outside target original. Getter `get(...)` asserts list-lifecycle reads detached maps, not same underlying object.

Add scenario where originally valid doc decoded previously, then current bytes replaced by malformed input. Do not use newly warmed helper’s `_data` to assume clean current. Different read contexts/test mode must see `UNSUPPORTED`, not prior clean value or miss. Build **new `GenreFileCache` per invocation** to test actual cross-owner scenario, not shared `.instance` package with RLock erroneously called inter-op cache fixing.



- [ ] **Step 2: Implement small strict reader without generic-load laundering**
Implement explicit `MISSING/VALID/UNSUPPORTED` read states and strict validator: current bytes from lstat-owned normal filepath; use bounded `os.open` with `O_NOFOLLOW` when available, post-open regular checks, no truncate/path metadata from API. Parse with duplicate/object-level key rejection and read limit exactly 128KiB (whitespace limit allowed on outer valid JSON) and canonical ASCII ID regex.
Preserve API structural filtering through dataset normalization of output only: only positive integer IDs/nonblank finite bounded names. No `bool is int` acceptance or floats-as-key substitution. Return detached stored documents only. Unexpected UnicodeDecodeError/OSError/jsonValueError/TypeError/RecursionError/Overflow return unavailable rather than overriding unknown raw bytes. Caller may receive getters for already present empty valid mapping, not `None`.

Then write a boolean `store(media, language, mapping)` using validated safe group/key value subset and `AtomicJsonCache.save_merged()`/transaction, retaining other allowed media/lang snapshots and the strict schema root, never replacing unsupported input or injecting selected personal metadata. Existing primitive must supervise its own complete file save and assembles deterministic flushed temp+ rename, after first valid merge. Logger accepts safe public key/state/namespace data without raw token/session. Missing normalized addon profile dir is allowed through existing primitive, not unknown handler data path.

- [ ] **Step 3: Establish genuine atomic commit and independent subsequent read**
Add regression: writer installs one profile/map via normal get/validated response then distinct reader repository/file helper gets it no new HTTP. Actual producer `response.close` occurs before store call; temp finish and os.replace are recorded before second helper is constructed. A second metadata/locale operation merge happens only when no illegal raw/untrusted initial document. Scenario returns exact metadata values on reload; current other allowed name isn't evicted. Read valid behavior from a failed save leads to `store=None` or `False`, never prefixing provenance “committed".

- [ ] **Step 4: Run focused RED→GREEN, review commit and full checks**
Run new focused tests against absent behavior first, retain actual failure evidence (lack helper/unsupported). Implement minimal source to pass without copying generic cache’s definitions hidden trust. Run entire focused tests, Python3.8 AST the new production/test modules and git diff check. Commit only those two files as `feat: add validated sequential public genre cache`.
Immediately run all discovery and release verifier after commit. Include dynamic inventory increment by helper in evidence rather than bake number assumption into code/tests. No owner/GenreRepository wiring until next independent review.

---

### Task 2: Wire optional GenreRepository file sharing with current memory fallback

**Files:**
- Modify: `yawsp_app/tmdb.py` (existing in-memory genre class only)
- Modify: `yawsp_app/router.py` (owner injects one profile helper)
- Test: `tests/test_tmdb.py` `GenreRepositoryTests` and `tests/test_entrypoint.py` only if production owner assertion requires it
- Perhaps modify discovery/production tests just to assert callbacks under sequential owner creation

**Interfaces:**
- Consumes: Task 1 `GenreFileCache.get()`/save contract and strict validated map. `AtomicJsonCache(profile/tmdb_genres.json)` owner injection.
- Produces: `GenreRepository(file_cache=None)` with default memory identical behavior. Optional path for supported `(media, language)` can delegate existing key after valid document read and store only after normal existing HTTP strict parsing, otherwise legacy memory _values/_pending notification path stays intact.

- [ ] **Step 1: Add failing fresh-owner/offline-client checks**
Create separately constructed real repository instances with distinct AtomicJsonCache and clean fake clients while sharing one profile:
- current behavior wants two lookups for two new owners. Record this as real baseline (if constructor optional option absent, that error/non-cache missing is absent required contract evidence).
- expect new client auto GET only once after first accepted public response, second repository reads saved localized genre without another. Field ids/name and `params={"language":"en-US"}` stay canonical. `client.get("/3/genre/movie/list")` validates ordinary server data and response closing.
- TV gets one first fetch without re-requesting movie; each is keyed independently.
- after true persisted owner commit+reload, own second-layer normal getter doesn't accidentally tun same key into an HTTP cache because primary genre data isn't list caching; no return alias allows a caller to rewrite stored representation.

Ensure test explicit first save+commit completed before next repository creation. Run against unmodified current `GenreRepository` and show its two new instances perform two network calls, establishing actual memory-only bug vs desired one. Avoid faking result by preloading same `_values` or putting originals single in-memory cache-store packaging behind own bang.

- [ ] **Step 2: Add opt-in lookup/write preserving old branch**
Extend constructor with optional `file_cache`, leaving main original loop/concurrent waits in nonfile mode. For supported file mode with document hit return value, don't trigger network. If miss only there (ENUM missing root key/frontmatter), use regular client get and existing genre array validations; validate sanitized values then store. Require store success to establish future persistence. If unsupported or write returns False, record safe warning but let current invoker return valid public mapping and `_values` stores current result. Always `_pending.discard`/`notify_all` on error, and do not mark invalid HTTP data/dropped names as an accepted negative cache hit. Any pending-local or storage error must leave exactly one local map and one notifiable waiter, never stuck pending set.

Owner constructs actual public helper once under same addon profile normalized file name and shared logger, not a new per-call generic lock/service/lifecycle. No other repositories write it.

- [ ] **Step 3: Cover compatibility, failures and invalid data paths**
Extend tests:
- non-enabled default current client+thread conditionally shared case preserved, existing two GenreRepository memory dedup tests still pass unchanged;
- unsupported other `language`/`media` stays process-only no file read/write with identical language param;
- positive/negative/string/boolean IDs and empty or whitespace names are only sanitized for storage, never poison a valid read. Valid rows aid success, malformed query `genres` preserves existing TMDBError and truthful no persistence;
- original raw file valid hash, then unknown data remains unchanged if input not accepted. Fresh cache previously loaded `load()` ordinary path must not revive it later incorrectly.
- simulate `.save_merged` failure or nonregular path. Current owner can render/use valid first payload and next owner refetches; _pending remains empty, not a silent error success; no failed save claimed commit, no restart negative-cache.

Run focused repo+entrypoint+discovery/public errors + complete current test. No rewrite worker shutdown behavior or public personal identity sessionlessness. Verify independent proof 401 snapshot and user-live bytes with helper inputs are unchanged other than intended new dedicated cache file on valid writes.

- [ ] **Step 4: Commit and independent regression review**
Commit limited production interfaces as `feat: reuse public genres across sequential invokers`. Run full discovery after commit, `verify_release.py`, Python3.8 AST/resource/diff gates. Review exact both-owner test and comparison to current in-memory handling; no release before dynamic package proof.

---

### Task 3: Align documentation/performance pack and final sequential real-Kodi matrix

**Files:**
- Modify: `README.md`, `tests/test_documentation.py` when behaviour changed requirements
- Modify if actual active coverage requires: `.superpowers/sdd/2026-11-25-performance-remediation/performance-coverage.json` and fresh actual-selector log
- Create only disposable `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-sequential-genre-validation` artifacts, and `.superpowers/sdd/2026-11-25-sequential-public-genres/task-3-report.md` repository ignored report

**Interfaces:**
- Consumes: Tasks 1–2 committed file+repo pipeline, no reader lock claim/service restoration. As before InfoWall styles/reversible no-client public selectors unaffected.
- Produces: explicit **sequential** performance evidence, proper module map/what-local cache contains (schema1 public only), legitimate latest hash/inventory, final four-category actual counter results.

- [ ] **Step 1: Document physically truthful bounded behavior**


Explain sidecar contains `schema { version genre id strings, localized genre names }` only in traversable form at present example correct `schema:1`, allowed six media/lang record groups, no TTL/expire schedule inferred. Don't disclose temporary/user credential cases, promise thread-safe dedup or call a simple `AtomicJsonCache` cross-invoker lock. Clarify unknown/unsafe data is unchanged/no trust and normal invocation falls back when write unavailable. No broadcasts/daemon fix.
Actual总量的 newcomer error continues separate: native UI original failure and import/shutdown unresolved before this fix. Do not write native UI PASS from an imagined commit, leaving final live gate open until following steps.

For active performance coverage add exact new fresh-owner sequential tests and execute all mapped selectors using real input from current schema; compute passed/mapped/unique after actual run (no extra unknown counter). Original current31 including full native root selectors should remain. Old native 2×matrix was separately failed and must not be represented as solved yet. News 2.0.0/setup remains same verbatim. Authenticate desired runtime code points `genre_file_cache` automatically selected by root pattern in verifier. Re-evaluate **dynamic inventory checksum only after current new commit**.


- [ ] **Step 2: Run exact source unit/package checks**
After doc/coverage commit clear any pods, run complete discovery, Python3.8 AST+PO/XML/diff, actual selector proof, verifier dynamic files and `git diff --check`. Get independent functional/security/perf reviews covering current/input contract and trust boundaries. No unsupported legacy service/concurrency dedup terminology allowed without measurement.

- [ ] **Step 3: Run current-code native sequence**
With new clone fresh `portable_data`, strict prereq stdout no sidecar file, exact expected binary hash and synthetic loopback handlers/no external network/password:
1. Six native root items; Objevit first leads four localized folders with zero API/background/getGenre.
2. First `popular_movies` ordinary callback performs movie list+movie genre. Capture response‑close before file replace/save true and strict reload outcome; exact directory end follows eventual commit.
3. Then select `popular_shows` after that commit. First TV list+TV genre because TV not yet persisted. Valid data/url group is independent of movie. This could use same or different unique invoker context, normal elapsed window no synthetic concurrent thread.
4. Subsequent `top_rated_movies` after movie commit requires ordinary top-rated movie list GET, but **zero additional movie genre**; require its actual selected row has current genre and real InfoTag metadata correct from first response, not generic root.
5. Following `top_rated_shows` after TV commit requires regular TV list GET, but **zero additional TV genre**. InfoTag/title/year/plot/media/art and Webshare sort largest stay conventional.
Counts per route: Root0; popular_movies1list+1genreMovie; popular_shows1list+1genreTV; top_rated_movies1list+0genre; top_rated_shows1list+0genre. Cumulative exactly4 categories/1 selected list each +1 movie+1TV genre. Any repeat opens additional list ordinarily but no further genre for valid sidecar.

At each transition record actual previous coroutine/commit complete time→new invoker callback timestamp from framework, no claiming timestamps that just match list rows. Record thread/PID inspect same invoker processing as before and precise per-handler running types. Check actual output file schema and response close before file replace/valid read. Prove no dubious unsupported user path/link or stale file substitution was loaded: fresh profile document hash/version map changed only expected rules of public genre ID/name records.

- [ ] **Step 4: Unchanged common paths and termination**
Check empty category still yields 30440 and 0 genre despite persistable empty map future case; error 503 follows 30402/no success; missing API key gets local error before HTTP; explicit unknown source does not open categories. Public401 doesn't modify watchlist identity bytes or session hidden fields. Handoff preserved for actual movie/tv localized title+sort largest and optional original context.


Capture normal process exit 0 without forced cleanup but keep disclaimer independent active-background HANG is not rerun or fixed. No pixels/readability/hardware performance claim nor long-absent stopped list update claim. Source/diff inventory/package check and raw artifact hash review prove final code's behavior. Any native product/test failure produces followup failing test and is not hidden by closing Kodi cleanly nor by file existence alone.

- [ ] **Step 5: Independent scoped review and ask next decision**
Return `SEQUENTIAL GENRES PASS/FAIL`, general native/other blocks PASS/FAIL, `PUBLIC PRIVACY PASS/FAIL`, `PACKAGE/OFFLINE PASS/FAIL`, `LEGACY ACTIVE SHUTDOWN RESULT` same unresolved. Review directory/root/list/public identity, store invalid byte preservation, constructor optionality, no concurrency claim from file presence + 1 first fetch and names normalized realistically. Ask only after those artifacts for user's later merge/release decision. No automatic finishes/merges per this plan.

## Self-review

- Task 1 deals current byte identity/strict schema and independent formatter rather than letting `AtomicJsonCache.load()` fake file-valid/hit status.
- Task 2 tests actual two separately-created GenreRepository and JSON instances, proving a final live owner starts only after its zero/first committed event versus intrinsic process-local cache handler count.
- Task 3 holds concrete first movie/tv mapping and conditional next script flags. Static next list API is legally uncached; it is the second genre response count which must stay zero after valid previous key, not all transport revalidation disabled or saves ignored.
- Existing negative proof for OS locks remains separate. No interpretation of section One label changes should weaken security privacy, error redaction or primary actual widget shapes.
- Main profile and dispatched clone are not blended. Main library versions remain unchanged globally, preserving ordinary user expected deletion WindowsX experimental to cleanup before delivery not reactivate through quiet settings/code.
