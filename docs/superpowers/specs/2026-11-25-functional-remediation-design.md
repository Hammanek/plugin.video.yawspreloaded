# Functional Remediation and Modular Refactoring Design

## Intent and why

The 2026-11-25 read-only functional audit found 17 functional/localization findings and four conditional risks in YAWsP Reloaded. The main failure pattern is inconsistent responsibility: HTTP helpers swallow errors and return `None`, UI actions continue after authentication failures, some Kodi invocations are never finalized, downloads and database initialization expose partial results as successful, and TMDB caches persist failures.

The user explicitly chose a **large refactoring** over targeted patches. This project will therefore separate transport/business logic from Kodi presentation while fixing every audited item and adding regression tests. Existing user-facing functionality and navigation are to remain recognizable.

## Goals

1. Separate Webshare, TMDB, downloading, persistent cache, text/search rules, and Kodi UI into focused modules.
2. Make service failures propagate in one direction with no `None`-as-response contract and no duplicate error notifications.
3. Guarantee exactly one appropriate Kodi completion for every directory, playback, and fire-and-forget action, including exceptions and cancelled input.
4. Make downloads, Backup DB initialization, and JSON cache persistence safe against HTTP errors, interruptions, and partial writes.
5. Correct search, paging, TMDB account/cache behavior, QR cleanup, localization, and optional YouTube integration.
6. Add deterministic unit/integration-style tests using mocked HTTP and Kodi interfaces; no tests may require real credentials or network access.

## Non-goals and anti-goals

- Do not create a release, tag, version bump, or release archive in this project.
- Do not add a new UI/skin, redesign menus, or change successful user flows merely for aesthetics.
- Do not add a new third-party runtime dependency.
- Do not split modules without preserving behavior; every extraction must retain or improve an audited contract and be covered by tests.
- Do not use live Webshare/TMDB accounts in automated tests or claim Kodi runtime verification without running Kodi.
- Do not silently retain defects because the code moves to a new file.

## Approaches considered

### A. Targeted patches in the existing monolith

This has the smallest immediate diff and could fix the findings with helpers inside `yawsp.py`. It was initially recommended for regression safety. The user rejected this option in favor of the structural refactor.

### B. Modular refactor with test-defined contracts — selected

Extract transport and business rules into testable modules, keep Kodi-specific calls in UI/runtime modules, and make `yawsp.py` a thin compatibility entry point. This directly addresses the architectural causes found by the audit while remaining within one Kodi add-on and the existing Python standard library plus already declared dependencies.

### C. Full external framework / asynchronous application rewrite

Rejected. Kodi 19+ plugin entry points, local XML/JSON persistence, Requests, and the installed dependency set are fixed by the product. Introducing a framework would add platform risk and unnecessary dependencies.

## Constraints

- Kodi 19+ / `xbmc.python` 3.0.0, Requests, and Unidecode remain supported as declared in `addon.xml`.
- Python tests run with the standard-library `unittest` runner: `python -m unittest discover -s tests -v`.
- Production runtime uses the standard library, declared dependencies, and the already bundled local modules `md5crypt.py` and `qrcodegen.py`. These local modules are packaged with the add-on; they are not external Kodi dependencies and require no `addon.xml` import declaration.
- `main.py` remains the add-on entry point and `plugin://plugin.video.yawspreloaded/` URL compatibility is preserved.
- Existing setting identifiers remain compatible, including stored Webshare and TMDB credentials/tokens.
- User-visible changes that previously had localized text retain that behavior. New TMDB visible strings must be added to all three PO files.
- No real secrets may appear in tests, logs in the design, or committed fixtures.
- Work is separated into small red-green commits. The full test discovery command must pass after each task.

## Target file structure

```text
main.py                              # unchanged Kodi entry point
yawsp.py                             # compatibility facade; delegates router
yawsp_app/
    __init__.py
    runtime.py                       # addon/Kodi context, URL building, action wrappers, common errors
    webshare.py                      # HTTP session, XML parsing, authentication and domain operations
    search.py                        # query normalization, Unicode matching, pagination state, Czech sort key
    downloads.py                     # checked streaming download and temporary-file lifecycle
    json_cache.py                    # defensive loading, atomic writes, TTL records and legacy conversion
    tmdb.py                          # TMDB HTTP client, account state, trailer/show cache and prefetch jobs
    webshare_ui.py                   # search/queue/history/info/play/download/Backup DB presentation
    tmdb_ui.py                       # TMDB auth/QR/watchlist/seasons/episodes/trailer presentation
    router.py                        # URL action dispatch and top-level failure boundary
resources/
    settings.xml                     # retained ids; hidden duuid declaration if persistence requires it
    language/.../strings.po           # repaired and newly required visible strings
 tests/
    support/                          # reusable fake HTTP responses/sessions, filesystem and Kodi doubles
    test_search.py
    test_webshare.py
    test_downloads.py
    test_json_cache.py
    test_tmdb.py
    test_router.py
    test_webshare_ui.py
    test_tmdb_ui.py
    test_resources.py
README.md                            # short architecture and test instructions
```

Tests and fixtures use only synthetic data. Production service modules must not import `xbmc*`; runtime/UI adapters inject all Kodi operations, clocks, sleepers, paths, and file operations needed for deterministic tests.

`duuid` will be declared as a hidden text setting alongside other internally persisted settings, eliminating reliance on undeclared dynamic setting persistence.

## Runtime and Kodi action contracts

### Service boundary

`WebshareClient` and `TMDBClient` return parsed data/valid responses or raise a domain-specific exception caused by the original HTTP/parsing error. They never display Kodi notifications and never convert transport failures into `None`.

Runtime code logs the original exception with chained traceback context and presents one suitable error. Business-level responses such as invalid credentials or unavailable content use explicit result objects/values and at most one user-facing message.

Every caller requiring a Webshare token stops when revalidation/login fails. No request is made with `wst=None`. Playback failure resolves `False`; directory failure completes with `succeeded=False`; fire-and-forget actions resolve/finalize according to their action adapter.

### Kodi action adapters

The router assigns every action one of three completion kinds:

- **Directory:** handlers add items but do not call `endOfDirectory`; the adapter calls it exactly once in `finally`, setting `succeeded=False` on failure. Presentation helpers may call each other but may not independently finalize.
- **Playback:** handlers return a success flag and a `ListItem`; the adapter calls `setResolvedUrl` exactly once, including invalid token, missing link, bad metadata, or exception.
- **Action/settings:** handlers return a result and the adapter completes the invocation consistently (`setResolvedUrl(False, ...)` where a plugin action requires it) before optional refresh.

This covers search, queue, history, DB, watchlist, season/episode lists, watched removal, trailer actions, settings, cancelled `searchdb`, and unexpected router failures. Normal behavior remains unchanged on successful paths.

### Exact completion matrix

| Invocation | Kind | Completion behavior |
| --- | --- | --- |
| Empty parameters/default menu, unknown action | Directory | Exactly one `endOfDirectory` from the directory adapter. |
| `search`, `queue`, `history`, `db`, `searchdb` | Directory | Exactly one `endOfDirectory`; cancelled input returns through the same adapter. |
| `tmdb_watchlist` without `watched`, including seasons and episodes | Directory | Exactly one `endOfDirectory`; nested presentation helpers never finalize. |
| `tmdb_watchlist` with `watched` | Action | Resolve the RunPlugin action false once, then request `Container.Refresh` only after completion and a handled API result. Completion kind is selected from both action and parameters. |
| `play` | Playback | Exactly one `setResolvedUrl`, true only with a validated playable link/list item. |
| `settings`, `download`, `info`, `tmdb_auth` | Action | Exactly one `setResolvedUrl(False, ...)` after the handled UI operation or its error path. `info` opens its textviewer before completing; downloads never resolve a media URL. |
| On-demand `tmdb_trailer` | Action followed by external media action | Resolve the lookup action false once. Only after a valid YouTube lookup and availability check, issue `PlayMedia` for the YouTube plugin; failure never calls `PlayMedia`. It does not pass one plugin URL through another plugin’s `setResolvedUrl`. |

The router may implement this with explicit per-action adapters or an adapter that selects kind using `(action, params)`. Tests assert both the completion kind and exact call count for every row. New actions may not be added without assigning a matrix row.

## Functional contracts by domain

### 1. Webshare HTTP and authentication — F-01, F-06

- HTTP status, timeout, malformed XML, and missing required XML fields become explicit failures at the service boundary.
- `revalidate` returns a token only after successful validation. Empty credentials and rejected login leave no token and stop the current UI action cleanly.
- Required elements such as `token`, `vip`, `link`, `name`, `ident`, and `download_id` are validated before use. Invalid individual result items are skipped with a diagnostic where safe; required global values fail the operation.

### 2. Search and listing — F-02, F-07, F-08

- `NONE_WHAT` is a browsing sentinel, not a search word. API receives `what=''` and no local title filter is applied.
- `S1E2` normalization occurs before API request construction. Both query and candidate filename receive equivalent season/episode normalization before matching so valid `S1E2` names are not discarded merely because the query became `S01E02`.
- Query and filename use the same deterministic Unicode-insensitive tokenization/normalization (Unidecode is already available). A query with no meaningful tokens never turns into `all([]) == True` for ordinary search; explicit browsing is the only unfiltered mode.
- “Previous” and “next” are generated from request offset/limit and the raw API result count. Local filtering never decides whether another API page exists.
- Internal result/queue URLs preserve `what`, `category`, `sort`, `limit`, and `offset`.

### 3. Download lifecycle — F-03, F-09, R-03

- `file_link`/`file_info` and the download GET are fully validated before a target file is created. GET calls `raise_for_status()`.
- All responses stream through chunks, with or without `Content-Length` and regardless of notification settings. Responses are closed.
- Data is written to a unique `.part` file in the destination. It is closed in `finally` and promoted with the appropriate local/VFS atomic replacement only after complete success. Failed/incomplete files are removed when supported by the VFS adapter.
- The display filename has a safe fallback to the requested name or ident, initialized before the `try` block. Partial failures never announce download completion.
- Progress notification intervals are clamped to at least one; zero-length content never causes division errors. Notification checks use integer floor progress and emit only changed values.

### 4. Backup DB — F-04, F-11, R-01

- Download, status validation, ZIP validation, and extraction happen into a unique temporary directory under the add-on profile. `ZipFile.testzip()` must pass and every archive member must remain under that directory before extraction.
- A structurally complete DB installation contains at least one regular `.txt` data file. Every `.txt` file must parse as a JSON object with a top-level `data` list. Item-level fields are validated when read because individual database categories may have different optional metadata. There is no requirement that every `A.txt`–`Z.txt` or `0.txt` member be present; the external archive has no documented manifest allowing such an assumption.
- Only after the complete download, CRC checks, safe extraction, and all JSON validation succeed is the validated DB switched into place. On any failure temporary files/directories are cleaned and the next open retries initialization. This proves local structural completeness, not the external server’s undocumented editorial completeness.
- An existing legacy installation without a validity marker is accepted if it passes the same structural validation. A leftover empty directory, no-data-file directory, malformed JSON, or other partial final `db` directory is quarantined/removed only after the replacement is ready, then repaired rather than treated as initialized. New installations record a small local manifest of filenames and sizes so later missing/truncated files can be detected without imposing an A–Z schema.
- `searchdb` trims input. Cancel/empty input returns to a valid DB listing or completes cancelled without a pending directory.
- Group selection uses only explicit ASCII `A`–`Z`; other initial characters route to `0.txt`, matching the documented schema.

### 5. JSON cache persistence — R-04

- Cache writes serialize to a temporary file in the same directory, flush/close it, then use `os.replace`. A failed old cache load produces an empty usable cache and a diagnostic, not a crash.
- Concurrent invocation safety is documented: atomic replacement protects file integrity; process-level write merging is not claimed without a test proving it.
- Legacy trailer dictionaries are accepted. Legacy `None` trailer values are discarded so previously persisted network failures can recover.

### 6. TMDB HTTP, identity, and caching — F-05, F-10, F-13, R-02

- `429 Retry-After` accepts integer seconds and HTTP-date values. Invalid values use bounded exponential fallback. Server-requested positive waits are not capped to five seconds. Retry sleeps are abort-aware and finite in tests through injected clock/sleeper.
- A new successful authorization clears `tmdb_account_id` before storing/using the new session. Session invalidation also clears account identity. The next authenticated request resolves and persists the account for the new session.
- Trailer cache stores successful API responses only. Valid trailer IDs have a seven-day TTL; a successful “no trailer” response has a six-hour negative TTL. Transport/API failures return no new value and do not create a negative entry.
- Show next-episode successes, including a genuine `next_episode_to_air=None`, use the existing 24-hour TTL. Failures do not refresh the timestamp. An expired previous result may be displayed as stale fallback while still being eligible for retry.
- QR display cleanup lives in `finally`. Timeout, abort, polling errors, and user cancellation close only a still-active QR viewer; action code does not navigate the underlying container away.
- UI auth success returns a clear success/failure result and the router finishes its action exactly once.

### 7. Watchlist workload and optional trailers — F-12, F-14

- Initial directory rendering never waits for one videos/details request per watchlist item. Cached values are used immediately.
- Missing metadata is scheduled through a small bounded background worker after directory rendering is initiated. Work is deduplicated, has its own HTTP session, can stop when Kodi aborts, and persists results atomically. There is no automatic concurrent container refresh; newly fetched data appears on a later open/refresh.
- Trailer context action remains available for uncached items. Selecting it performs an on-demand lookup and either plays through YouTube or reports that no trailer exists/failed. This avoids hiding a trailer merely because it was not prefetched.
- YouTube availability is checked through Kodi. If unavailable, the UI reports the missing optional add-on instead of issuing a broken `PlayMedia` URL. `plugin.video.youtube` is not made a mandatory runtime dependency.

### 8. Presentation, localization, and sorting — L-01, L-02, L-03

- Add IDs `30007` and `30216` to all PO files and replace the corrupted English `30043` value with valid English/fallback UTF-8 text.
- Move all TMDB-visible hardcoded strings, including settings labels, authentication states, watchlist actions, empty states, seasons, and episodes, into all three PO catalogs. Existing Czech meaning is preserved.
- TMDB API language derives from Kodi language: Czech `cs-CZ`, Slovak `sk-SK`, otherwise `en-US`. Explicit per-request overrides such as trailer lookup may still override it.
- Czech ordering uses a deterministic local collation key: normalized base letters plus explicit `ch`-after-`h` handling and stable tie-breaking. It must not depend on the host OS locale.
- Season content remains `seasons`; episode containers explicitly set `episodes`.

## Testing and migration strategy

1. Introduce the test doubles and service contracts with failing tests first.
2. Extract and test pure search/text and JSON/download lifecycle logic before moving UI behavior.
3. Extract Webshare/TMDB clients with fake HTTP sessions, then move UI handlers into action adapters.
4. Migrate placeholders/routing incrementally. A public entry-point smoke test proves `main.py` and `yawsp.router` remain importable with Kodi stubs.
5. Existing local JSON/settings remain readable. Network-failure cache entries are intentionally invalidated; no destructive deletion of valid user data is allowed.
6. Each functional task includes tests for happy path and audited failure paths. The last task runs the entire discovery suite, resource contract tests, XML parsing, Python syntax parsing, and a full diff review.
7. README documents module responsibilities and `python -m unittest discover -s tests -v`; it retains version `1.3.15` because no release is being prepared.

Minimum regression matrix:

- Token failure/HTTP timeout/malformed required XML for every service boundary type.
- Browse sentinel, `S1E2` and `S01E02`, diacritics, Unicode-only input, paging and preservation of listing state.
- HTTP error download, missing content length, interrupted stream, zero/invalid progress settings, cleanup and promotion.
- Failed DB initialization followed by successful retry; cancellation and non-ASCII grouping.
- Malformed/old cache files, atomic replacement, TTL expiry and no caching of transport failures.
- TMDB account switch, both Retry-After formats, aborts, QR cleanup and no blocking prefetch.
- Every router action’s success, business failure, cancellation and exception completion contract.
- Catalog IDs/settings/valid UTF-8 and Kodi content types.

## What remains to the builder

Exact internal class/function names, exception hierarchies, result objects, and small helpers are builder choices consistent with this design. Exact TTL implementation classes and bounded-worker mechanics may change if tests can make the same guarantees more simply. Any change that weakens an explicit contract or drops an audit item is not allowed.

Live Kodi 19/20/21 testing, real Webshare/TMDB smoke testing, and release preparation are follow-up activities and require separate user direction. Their absence must be reported as a verification limitation, not described as a passing runtime test.
