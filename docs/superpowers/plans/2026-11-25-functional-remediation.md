# Functional Remediation and Modular Refactoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the Kodi add-on into testable Webshare, TMDB, persistence, download, and UI modules while fixing all 17 audit findings and four conditional risks without changing the public plugin URL contract.

**Architecture:** The legacy `yawsp.py` remains operational while services and tests are introduced. New service modules contain no `xbmc*` imports; Kodi adapters and UI handlers depend on explicit runtime interfaces. The final migration task replaces `yawsp.py` with a facade only after both Webshare and TMDB handlers and the exact action-completion matrix work together.

**Tech Stack:** Python 3 / Kodi 19+, Requests, Unidecode, bundled `md5crypt.py` and `qrcodegen.py`, standard-library `unittest`, XML/JSON/ZIP and VFS adapters.

**Spec:** `docs/superpowers/specs/2026-11-25-functional-remediation-design.md` (approved by the user on 2026-11-25).

## Global Constraints

- Preserve Kodi 19+ and all existing `plugin://plugin.video.yawspreloaded/` parameters and setting identifiers; add only hidden `duuid` and localized labels.
- Production runtime adds no third-party dependency. Bundled local modules need no `addon.xml` declaration.
- Never use real credentials, live APIs, or network in tests. Run `python -m unittest discover -s tests -v` after every task and require zero failures.
- Every handler has exactly one completion according to the spec matrix. Services never display Kodi notifications or return HTTP responses as `None`.
- Do not delete valid settings or cache data. Recover legacy negative TMDB cache failures and malformed DB/cache state according to the spec.
- No release, tag, version bump, or archive is part of this plan. README remains version `1.3.15` and gains architecture/test documentation.
- Every implementation task follows RED→GREEN: write its named tests, run them and confirm the expected import/behavior failures, implement, run full discovery, then commit.
- Prior code outside a task’s files is not reformatted or “improved” opportunistically.

## Review Focus

- **Malformed external data:** missing XML fields, partial ZIP/JSON, non-string Retry-After, unknown item metadata. Tasks 2–6 include valid minimal fixtures plus broken/missing values and assert safe operation rather than secondary exceptions.
- **Cancellation and re-entry:** Escape in search/auth, repeated Backup DB initialization, repeated trailer/account lookup, stale legacy state. Tasks 3, 4, 5, and 6 assert a valid completion and idempotent follow-up behavior.
- **Concurrency and lifetime:** Kodi abort during background work/retry, worker completes after invocation returns, duplicate prefetches, response/file closure. Tasks 3, 5, and 6 use fake clocks/workers and assert no notification after invalidation or blocking wait.
- **Unicode and collation:** non-Latin queries, diacritics, Czech `ch`, empty token sets, non-ASCII DB initials. Tasks 1 and 3 contain the explicit examples; Task 6 checks their routing into actual URLs/list labels.
- **Legacy compatibility:** existing tokens/settings, old trailer cache values and `None`, valid unmarked DB, every original action and entry point. Tasks 3, 5, 6, and 7 contain migration and resource/router contract tests.

---

### Task 1: Pure search, pagination, Czech collation, and test foundation

**Files:**
- Create: `yawsp_app/__init__.py`, `yawsp_app/search.py`
- Create: `tests/__init__.py`, `tests/support/__init__.py`, `tests/support/http_stubs.py`, `tests/support/kodi_stubs.py`
- Create: `tests/test_search.py`
- Add for the initial commit: `docs/superpowers/specs/2026-11-25-functional-remediation-design.md`, `docs/superpowers/plans/2026-11-25-functional-remediation.md`

**Interfaces:**
- Produces: `SearchQuery(api_text: str, tokens: tuple[str, ...], browsing: bool)`; `parse_query(text: str) -> SearchQuery`; `matches_filename(query: SearchQuery, filename: str) -> bool`; `page_offsets(offset: int, limit: int, raw_count: int) -> tuple[int | None, int | None]`; `czech_sort_key(text: str) -> tuple`.
- Produces for later tests: reusable fake response/session and Kodi recorder classes with no filesystem/network side effects.

- [ ] **Step 1: Write failing search tests**
  Cover the literal sentinel `%#NONE#%` (API empty plus unfiltered browsing), `Show S1E2` matching both `Show.S01E02` and `Show.S1E2`, `Ž` and mixed Czech names without `all([])` acceptance, punctuation/Unicode token matching, empty input, previous/next offsets around 0/500/501, and exact `['Adam', 'Ábel', 'Helena', 'Chata', 'Iva', 'Žluťoučký']` collation.
- [ ] **Step 2: Run `python -m unittest tests.test_search -v` and confirm failure**
  Expected: import failure because `yawsp_app.search` does not exist; no production code has been written yet.
- [ ] **Step 3: Implement only the declared pure interfaces and reusable test doubles**
  Normalize episode notation before tokenization on both query and filename, share the same Unidecode-based normalization, keep browsing explicit, derive next offset only from raw results, and make Czech `ch` sort after `h` without host locale.
- [ ] **Step 4: Run full discovery and inspect the zero-failure result**
  Run: `python -m unittest discover -s tests -v`.
- [ ] **Step 5: Commit Task 1 and the approved design/plan**
  `git add yawsp_app tests docs/superpowers/specs/2026-11-25-functional-remediation-design.md docs/superpowers/plans/2026-11-25-functional-remediation.md && git commit -m "Add search contracts and test foundation"`.

### Task 2: Webshare transport, XML validation, and authentication domain

**Files:**
- Create: `yawsp_app/webshare.py`
- Create: `tests/test_webshare.py`
- Reuse: `tests/support/http_stubs.py`

**Interfaces:**
- Consumes: Task 1 HTTP fakes.
- Produces: `WebshareError(RuntimeError)`; `WebshareClient(session, settings, api_url='https://webshare.cz/api/')`; `login() -> str | None`; `revalidate() -> str | None`; `search(token, query: SearchQuery, category: str, sort: str, limit: int, offset: int) -> XmlSearchPage`; `get_info(ident, token) -> Element`; `get_link(ident, token, dtype='video_stream') -> str`.
- `XmlSearchPage` exposes raw XML elements and raw result count. Settings exposes `get(key) -> str` and `set(key, value)`, implemented against Kodi later.

- [ ] **Step 1: Write failing domain tests**
  Cover timeout and HTTP 500 raising `WebshareError` (never `None`), malformed XML and missing token/vip/link/name/ident, valid bundled-md5 login vectors, rejected login returning `None`, invalid existing token clearing followed by failed login (no fallback token), normalized `S1E2` sent to API, raw file count retention, `maybe_removed` info retry, and independent link/info behavior.
- [ ] **Step 2: Run `python -m unittest tests.test_webshare -v` and confirm missing-module failures**
- [ ] **Step 3: Implement the client without Kodi imports**
  Let transport/parse failures raise a chained domain exception. Validate required XML fields with helper accessors; business rejection returns explicit values only where the interface specifies them.
- [ ] **Step 4: Run full discovery and require all search/Webshare tests green**
- [ ] **Step 5: Commit**
  `git add yawsp_app/webshare.py tests/test_webshare.py tests/support/http_stubs.py && git commit -m "Harden Webshare API and authentication"`.

### Task 3: Downloads and recoverable Backup DB installation

**Files:**
- Create: `yawsp_app/downloads.py`, `yawsp_app/backup_db.py`
- Create: `tests/test_downloads.py`, `tests/test_backup_db.py`
- Extend: `tests/support/http_stubs.py`

**Interfaces:**
- Produces: `DownloadReceipt(destination, bytes_written)`; `stream_download(response, destination, display_name, file_ops, progress=None) -> DownloadReceipt`; file adapter operations `open_write/open_read/replace/remove/exists` used only through injection.
- Produces: `BackupDBStore(profile).is_valid() -> bool`; `ensure_installed(archive_loader: Callable[[], bytes]) -> bool`; `group_for_query(text: str) -> str`, returning `A.txt`–`Z.txt` or `0.txt`; `load(filename) -> list[dict]`.

- [ ] **Step 1: Write failing lifecycle tests**
  Download: 403 creates no destination; missing content length and disabled notifications still stream chunks; interrupted stream closes/removes `.part` and never announces success; zero-length content and zero progress interval never divide by zero; success atomically replaces the target and reports exact bytes.
  Backup DB: valid ZIP installs `data`-list JSON without requiring all A–Z files; CRC failure, unsafe member, empty archive, missing `data`, malformed JSON and HTTP failure all permit a subsequent successful retry; valid legacy unmarked DB remains; invalid empty/partial legacy DB is replaced; whitespace/diacritic initials route to `0.txt` and ASCII initials uppercase correctly.
- [ ] **Step 2: Run both new test modules and confirm missing-module failures**
- [ ] **Step 3: Implement temporary output, structural validation, cleanup and adapters**
  Use same-directory temporary files/directories and `os.replace` for local profile/DB writes. Isolate actual local/VFS operations behind the injected adapter; never call `response.content` for media transfer.
- [ ] **Step 4: Run full discovery and require all previous suites green**
- [ ] **Step 5: Commit**
  `git add yawsp_app/downloads.py yawsp_app/backup_db.py tests/test_downloads.py tests/test_backup_db.py tests/support/http_stubs.py && git commit -m "Make downloads and backup database recoverable"`.

### Task 4: Kodi action adapters and Webshare UI migration (not yet activated)

**Files:**
- Create: `yawsp_app/runtime.py`, `yawsp_app/webshare_ui.py`, `yawsp_app/router.py`
- Create: `tests/test_runtime.py`, `tests/test_webshare_ui.py`, `tests/test_router.py`
- Modify: `resources/settings.xml` only to add hidden `duuid`.

**Interfaces:**
- Consumes: Tasks 1–3 service interfaces.
- Produces: `CompletionKind.DIRECTORY | PLAYBACK | ACTION`; `completion_for(action, params) -> CompletionKind`; `ActionRunner.run_directory/play/action`, each accepting a handler and completing exactly one recorder call.
- Produces: `build_webshare_handlers(runtime, client_factory) -> dict[str, Callable]`; handlers accept parsed params and do not finalize Kodi themselves.
- Produces: `dispatch(paramstring, handlers, runner)`, retaining original query parsing and dynamic `tmdb_watchlist` completion selection even before TMDB handler migration.

- [ ] **Step 1: Write failing runtime/router tests**
  Assert the spec matrix for empty/unknown actions, every current action, `watched`, playback success/failure, exceptions, cancelled input, and exactly one `endOfDirectory` or `setResolvedUrl`. Include external-trailer `PlayMedia` ordering defined in Task 6 without duplicating its handler implementation.
- [ ] **Step 2: Write failing Webshare UI tests**
  Cover failed token causing no API request, sentinel/latest display, previous/next URLs, loss-free `toqueue` state, queue/history removal, play invalid link, info textviewer followed by action completion, download error filename fallback, cancelled/empty searchdb and repaired DB retry.
- [ ] **Step 3: Run new tests and verify they fail for missing adapters/handlers**
- [ ] **Step 4: Implement runtime adapters and move Webshare presentation without activating it**
  Reuse service modules; remove inner completion calls. Keep `main.py` and legacy `yawsp.py` untouched so an intermediate commit does not break a working plugin. Route display/settings through injected runtime only and keep service modules Kodi-free.
- [ ] **Step 5: Run full discovery, verify old entry point syntax, and commit**
  Run full unittest discovery plus read-only AST parsing of existing Python files. Commit as `Add Kodi action contracts and Webshare UI`.

### Task 5: TMDB client, retry semantics, account identity, cache and background work

**Files:**
- Create: `yawsp_app/json_cache.py`, `yawsp_app/tmdb.py`
- Create: `tests/test_json_cache.py`, `tests/test_tmdb.py`

**Interfaces:**
- Produces: `AtomicJsonCache.load() -> dict`; `AtomicJsonCache.save(mapping)` using same-directory temporary output and `os.replace`.
- Produces: `TrailerLookup(status: Literal['hit', 'missing', 'error'], youtube_id: str | None)`; `TrailerRepository.lookup(media, media_id) -> TrailerLookup`; `ShowRepository.next_episode(show_id) -> dict | None`.
- Produces: `TMDBClient`, whose account/session identity can be invalidated; request language is derived from an injected locale mapper. Retry After supports seconds and HTTP date with injected clock/sleeper.
- Produces: `BackgroundPrefetch.submit_trailers(...)` and `.submit_shows(...)` with bounded deduplicated work, worker-owned HTTP session, abort predicate, and injectable synchronous worker for tests.

- [ ] **Step 1: Write failing persistence and TMDB tests**
  Cover malformed/legacy JSON, discarded legacy trailer `None`, atomic replacement, seven-day trailer/six-hour genuine-negative/24-hour show TTL, no error entry on timeout/401/429, stale show fallback with retry eligibility, successful identity reset on account switch, both Retry-After formats/invalid fallback/abort, and deduplicated nonblocking submission with saved results and no automatic container refresh.
- [ ] **Step 2: Run new tests and confirm missing-module/interface failures**
- [ ] **Step 3: Implement repositories, client and bounded worker without Kodi imports**
  Persist only successful API responses; preserve prior valid data on load/write failure and chain/report errors through injected loggers only.
- [ ] **Step 4: Run full discovery and require all service suites green**
- [ ] **Step 5: Commit**
  `git add yawsp_app/json_cache.py yawsp_app/tmdb.py tests/test_json_cache.py tests/test_tmdb.py && git commit -m "Harden TMDB identity caching and retries"`.

### Task 6: TMDB UI, complete router migration and compatibility facade

**Files:**
- Create: `yawsp_app/tmdb_ui.py`
- Modify: `yawsp_app/router.py`, `yawsp_app/runtime.py`, `tests/test_router.py`, `tests/test_runtime.py`
- Replace: `yawsp.py` with a thin facade exporting `router`; preserve executable guard where applicable.
- Create: `tests/test_tmdb_ui.py`, `tests/test_entrypoint.py`
- Modify only if needed for adapter implementation: `main.py` while preserving its public behavior.

**Interfaces:**
- Consumes: every earlier service/UI/runtime interface. No new public setting or plugin base URL.
- Produces: `build_tmdb_handlers(runtime, client_factory, repositories, background) -> dict[str, Callable]` and unified production `router(paramstring)` used by `main.py`.
- Public completion matrix remains exactly the spec table, including dynamic watched handling and false-action-then-`PlayMedia` trailer ordering.

- [ ] **Step 1: Write failing TMDB UI and entry-point tests**
  Watchlist: cached rendering returns without waiting for all API lookups; background data is used on later open; invalid session/account recovery; watched POST/resolution/refresh order; Czech sorting/filtering; seasons/episodes content types; queued and successful/failed metadata failures.
  Auth/trailer: QR success, timeout, abort, closed viewer and exception cleanup; account reset; uncached on-demand trailer `hit/missing/error`; no YouTube avoids `PlayMedia`; trailer action resolves before optional `PlayMedia`.
  Compatibility: with Kodi stubs `import yawsp` exposes callable `router`, every legacy URL reaches a handler, and all normal/error/cancel routes complete once.
- [ ] **Step 2: Run the new tests and verify failure before migration**
- [ ] **Step 3: Move TMDB presentation and activate the unified router/facade**
  Schedule background work without waiting, use cached values immediately, and do not automatically refresh a container from workers. Apply Kodi locale mapping and Task 1 Czech collation. Remove the legacy monolith only after the complete action test matrix passes with the new handlers.
- [ ] **Step 4: Run full discovery and AST/XML checks**
  Require zero test failures, all four existing bundled Python files plus new modules syntactically readable (UTF-8 BOM acceptance), and valid `addon.xml`/settings XML.
- [ ] **Step 5: Review file inventory and commit**
  Confirm no duplicate legacy router remains and no service module imports `xbmc*`. Commit as `Migrate TMDB UI to unified modular router`.

### Task 7: Resource localization and optional dependency contracts

**Files:**
- Modify: `resources/settings.xml`, all three `resources/language/*/strings.po`
- Modify only where visible labels are assigned: `yawsp_app/tmdb_ui.py`
- Create: `tests/test_resources.py`

**Interfaces:**
- Consumes: runtime locale/localization methods from Task 6.
- Produces: required legacy IDs `30007` and `30216`, corrected English `30043`, sequential unused IDs beginning at `30400` for every TMDB-visible hardcoded string, and unchanged English fallback via `msgid`/empty `msgstr` where intentional.

- [ ] **Step 1: Write failing resource contract tests**
  Extract all static `getLocalizedString` numeric IDs and settings `label`/`lvalues`; require each numeric reference in all three catalogs, strict UTF-8 decode, unique msgctxt IDs per catalog, no U+FFFD, matching settings/lvalue counts, no replacement hardcoded TMDB labels, and exact availability of `30007`, `30216`, valid `30043` and the new IDs.
- [ ] **Step 2: Run `python -m unittest tests.test_resources -v` and confirm the current catalog failures**
- [ ] **Step 3: Repair catalogs and wire every TMDB visible label to the new IDs**
  Preserve Czech/Slovak wording, use English source/fallback strings, map TMDB labels/settings consistently, and do not renumber older IDs.
- [ ] **Step 4: Run full discovery and resource validation; require no warnings/errors**
- [ ] **Step 5: Commit**
  `git add resources yawsp_app/tmdb_ui.py tests/test_resources.py && git commit -m "Repair localization and resource contracts"`.

### Task 8: Documentation and whole-remediation verification

**Files:**
- Modify: `README.md`
- Modify only to record discovered contract corrections: relevant production/tests files with accompanying RED→GREEN tests
- Update checkboxes: this plan and the design only if the final commit needs execution status; do not alter requirements to match incomplete code.

**Interfaces:**
- Consumes: the complete modular add-on and test suite.
- Produces: documented module responsibilities and reproducible test command; final published status remains **not released**.

- [ ] **Step 1: Add README verification expectations before implementation**
  Define a documentation/resource check requiring the module table, test command `python -m unittest discover -s tests -v`, version badge still matching `addon.xml`, and no claim that live Kodi/API tests ran. Run it against the old README and observe the missing-content failure.
- [ ] **Step 2: Update README concisely**
  Add architecture/testing instructions and limitations while preserving installation, QR/TMDB setup, release links and version `1.3.15`.
- [ ] **Step 3: Run the complete verification suite**
  Run `python -m unittest discover -s tests -v`, strict AST syntax checks, `addon.xml`/settings XML parse, resource checks, `git diff --check`, and a grep proving legacy swallowed `api()` contract and service `xbmc*` imports are absent. Capture exact totals rather than assuming coverage.
- [ ] **Step 4: Perform an audit-ID coverage review**
  In the final review packet, map F-01…F-14, L-01…L-03 and R-01…R-04 each to at least one passing test and production location. Any unmapped item requires a new failing regression test and fix before completion; do not mark the task complete by prose alone.
- [ ] **Step 5: Commit documentation and verified residuals**
  Commit as `Document modular architecture and regression verification`.

---

## Execution invariants for every task

1. A red test must fail for the documented missing behavior/module, then the same test must pass without changing intended assertions to fit implementation.
2. Full discovery after every task includes all earlier tests. A regression stops the next task.
3. Commits are scoped to the task. Existing unrelated user work is never reset or overwritten.
4. `main`/`yawsp.py` remains on legacy behavior until Task 6 passes locally. After Task 6, no fallback to the monolith is used to hide failures.
5. Reviewers compare base..head behavior, exact completion-call counts, and production file locations, not just green test totals.
6. No completion claim may assert real Kodi/API compatibility; only the tests and checks actually run can be reported.
