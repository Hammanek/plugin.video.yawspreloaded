# Performance Task 7 report — proof, documentation and whole-suite verification

## Scope and baseline

- Branch/worktree: `functional-remediation`.
- Base: `8eae2cb812b68ef47ca3303b3dc1624f6383a7c4` (`Reduce Webshare navigation cost and history overhead`).
- Baseline command: `python -m unittest discover -s tests -v`.
- Baseline result in `task-7-baseline-full.log`: **303 tests, OK**, 3.231 s. This reproduces the Task 6 approved total.
- Only documentation/evidence work was performed. No production implementation, release, tag, version, setting, localization or deferred-minor change was made. `addon.xml` and README remain **1.3.15**; the optimization is documented as not released.
- The machine-readable map is `performance-coverage.json`. It resolves all nine IDs to actual production AST definitions, actual asserting unittest methods, benchmark commands/raw evidence, comparisons and limits.

## Strict documentation/evidence RED

The four new documentation/evidence contract methods were added to `tests/test_documentation.py` before changing README or creating the coverage artifact. The existing three tests were retained.

Command:

```text
python -m unittest tests.test_documentation -v
```

`task-7-red-documentation.log` records:

```text
Ran 7 tests in 0.030s
FAILED (failures=7, errors=1)
```

Concrete causes against the old documentation were:

1. `test_readme_documents_snapshot_consistency_and_complete_cold_saves` failed because no snapshot scope/eventual-consistency/complete-cold-save section existed.
2. `test_readme_documents_bounded_jobs_and_truthful_completion` failed because `yawsp_app/download_jobs.py`, queue-versus-completion and post-promote refresh contracts were absent.
3. `test_readme_documents_db_streaming_and_offline_measurement_limits` failed on `mtime_ns`, `O(F)`, selected-file loading and 256 KiB, followed by the missing streaming/reproduction requirements.
4. `test_performance_coverage_resolves_real_symbols_and_test_methods` errored with `FileNotFoundError` for the deliberately not-yet-created `performance-coverage.json`.

The count of seven assertion failures includes the independent subTest failures; it is not seven test methods. The three pre-existing documentation tests passed. No performance production assertion was weakened or relabeled as RED/GREEN evidence.

## Documentation/evidence GREEN

README now names the snapshot and job modules and documents: complete account/media/language/schema-scoped warm snapshots, stale-while-revalidate eventual consistency, no partial cold snapshots, cache batching/post-complete work, bounded metadata drain, warm sidecar metadata checks, streamed extraction, lazy auth, truthful queued state, exactly one successful cold-DB refresh, bounded worker/session lifecycle, offline measurement boundaries and the exact standard test command. Existing QR/TMDB setup, latest-release installation and version were retained.

The coverage contract resolves every mapped production class/function/method through `ast` without importing Kodi, resolves each test selector to a real method with assertions, requires non-empty benchmark evidence and measured summaries/limits, and specifically retains the small-history and one-JSON-member caveats.

Focused GREEN command and output (`task-7-green-documentation.log`):

```text
python -m unittest tests.test_documentation -v
Ran 7 tests in 0.558s
OK
```

Two Czech phrase checks were corrected from nominative to the actual grammatical forms (`účtu TMDB`, `typu média`) before GREEN; no semantic requirement was removed.

## Actual mapped selector execution

Every selector named in `performance-coverage.json` was run. There are **31 mapped entries and 31 unique selectors; 31/31 passed** in two explicit `python -m unittest -v <selectors...>` batches.

| Batch | Evidence | Result |
|---|---|---:|
| TMDB snapshot/cache/metadata | `task-7-selectors-tmdb.log` | **12/12 OK**, 0.416 s |
| DB/media/auth/history/chunks/production owner | `task-7-selectors-io.log` | **19/19 OK**, 0.538 s |

The logs enumerate every method and its `ok`, not merely a module/class name. Coverage assignment is:

| ID | Selectors | Mutation-sensitive behavior actually asserted |
|---|---:|---|
| PERF-01 | 4 | warm zero account/watchlist calls + post-end refresh; failed cold page has no partial save; another account is not warm fallback; stale generation cannot overwrite local mutation |
| PERF-02 | 4 | one cache discovery read; post-complete composition; eight-success checkpoint; delayed merge preserves immediate unrelated data |
| PERF-03 | 3 | warm ensure zero full reads/target-only load; size/mtime mismatch full revalidation; corrupt/unsafe sidecar revalidation and regeneration |
| PERF-04 | 5 | media return before blocked release + actual completion; one cold reservation/no partial read/one refresh; failure no refresh/partial listing; real production VFS and worker-session composition |
| PERF-05 | 3 | menu no auth; installed DB root/group/detail no auth; installed-DB remote queue still auth-at-use |
| PERF-06 | 4 | 100-key drain with <=32 cap; fair family order; bounded retries with no negative cache; on-demand/worker one-request sharing |
| PERF-07 | 3 | canonical whole-content/first occurrence; no-ID response reuse; valid positive clear then one reload |
| PERF-08 | 2 | mixed compression + >chunk member streamed without `.content`; CRC detected by extraction with `testzip` forbidden |
| PERF-09 | 3 | default 4x write reduction; exact short writes at all chunk sizes; actual promotion then 100% completion |

The methods are therefore not comment-only or merely suggestively named; the focused runs demonstrate their assertions.

## Fresh benchmark execution and comparisons

All benchmark scripts completed with exit code 0 after the documentation changes. Raw outputs use the `task-7-*` names below; baselines are the original pre-change artifacts from Tasks 1–6. No live account, network transfer, actual Kodi invocation or external VFS was used.

### PERF-01 — warm snapshots and complete cold fallback

Command: `python .superpowers/sdd/2026-11-25-performance-remediation/task-2-benchmark.py`.
Evidence: `task-7-watchlist-benchmark.log`, `task-7-watchlist-counters.json`; baseline `task-2-baseline-counters.json`.

| Operation | Before | Current |
|---|---:|---:|
| Every-open legacy account identity resolutions | 1 | warm 0 |
| Legacy/warm watchlist HTTP requests | 1 | warm 0 |
| First warm shared-genre request | 1 cold-process baseline | 1 independently, if mapping absent |
| Second fresh warm account/watchlist/genre requests | N/A | 0 additional |
| Cold cached-identity two-page watchlist GETs | 2 | 2, still complete and no immediate duplicate refresh |
| Cold complete empty genres | N/A | 0 |
| Cold missing identity | N/A | 1 `/3/account` + 1 watchlist + 1 genre |

Fewer warm requests do not imply cross-account or partial behavior: the four PERF-01 regressions above passed, and cold saves still occur only after all pages succeed.

### PERF-02 — real cache fsync/replace counts

Command: `python .superpowers/sdd/2026-11-25-performance-remediation/task-1_operation_benchmark.py`.
Evidence: `task-7-cache-counters.json`; baseline `task-1-baseline-counters.json`. Three independent real-filesystem runs per cell; every range was invariant.

| Per trailer or show worker | Baseline fsync/replace median [range] | Current median [range] |
|---|---:|---:|
| N=1 | 1 [1,1] | 1 [1,1], intentionally immediate-sized |
| N=8 | 8 [8,8] | 1 [1,1] |
| N=32 | 32 [32,32] | **4 [4,4]**, 87.5% fewer |

The integrated listing regresses two discovery reads to one per cache, without bypassing reservation/dedup or deferred merge safety.

### PERF-03 / PERF-08 — warm DB and streamed validation

Command: `python .superpowers/sdd/2026-11-25-performance-remediation/benchmark_backup_db.py --output .superpowers/sdd/2026-11-25-performance-remediation/task-7-db-after.json`.
Evidence: `task-7-db-after.json`, `task-7-db-benchmark.log`; baseline `benchmark-baseline.json`. Dataset: 27 files x 500 items, seven repeats, Windows/CPython offline desktop.

| Operation | Baseline data reads | Current data reads | Baseline ms median [min,max] | Current ms median [min,max] |
|---|---:|---:|---:|---:|
| Warm ensure + target load | 55 | **1** (plus 2 small sidecar reads) | 29.7032 [29.2003,34.7425] | **9.8964 [8.7212,10.9755]** |
| Cold install | 27 | 27 | 326.3027 [316.1323,336.0895] | 324.4793 [318.1278,333.4657] |
| Legacy migration | 27 | 27 | 290.7382 [286.2646,319.0699] | 296.1355 [289.5765,303.4216] |
| Detected mtime change | 27 | 27 | 15.5362 [15.2463,19.9481] | 28.3850 [25.4171,31.8927] (slower; full revalidation) |
| Detected size change + repair | 54 | 54 | 355.8483 [349.9342,371.0068] | 371.4777 [363.2618,379.5831] (slower; validated repair) |

Warm `ensure_installed()` separately reads zero data JSON documents; `load()` reads only the target. Streaming uses 262144-byte requested chunks, no production whole-archive `.content`/`BytesIO`, and no separate `testzip()` pass. Current fixture memory facts: archive 70600 bytes, largest uncompressed member 25290 bytes, sequential full-validation `tracemalloc` peak 242447 bytes. This does **not** imply constant memory independent of a JSON member.

### PERF-04 — event handoff and genuine completion

Command: `python .superpowers/sdd/2026-11-25-performance-remediation/task-5-event-benchmark.py`.
Evidence: `task-7-event-benchmark.log`; legacy sequence `task-5-legacy-handoff.log`.

Legacy media: `stream-entered -> observed-blocked-invocation -> stream-released -> invocation-returned`; return-before-release was false.

Current media:

```text
invocation-entered -> resolve -> invocation-returned -> stream-entered
-> observed-return-before-release -> stream-released -> backend-complete-notification
accepted=True; completed-before-release=False; response-closed=True
```

Current cold DB:

```text
db-invocation-entered -> end -> db-invocation-returned -> archive-stream-entered
-> observed-db-return-before-release -> duplicate-open-status-without-second-install
-> archive-stream-released -> validated-promote-success-and-one-refresh
one-reservation=True; partial-reader=False; refreshes=1; warm-new-auth=0
```

The improvement is invocation handoff, not a timing or remote-throughput promise. Acceptance remains distinct from success.

### PERF-05 / PERF-07 — auth/history request counters

Command: `python .superpowers/sdd/2026-11-25-performance-remediation/task-6-benchmark.py --output .superpowers/sdd/2026-11-25-performance-remediation/task-7-chunks-current.json`.
Evidence: `task-7-chunks-current.json`, `task-7-chunks-benchmark.log`; baseline `task-6-before-benchmarks.json`.

| Actual path | Before | Current | Preserved behavior |
|---|---:|---:|---|
| Main menu auth/revalidation | 1 | **0** | same five items; 0 explicit remote `_post` calls |
| Installed DB root/group/detail auth | 0 each | 0 each | same local rendered item counts |
| Installed-DB queue auth | 1 | 1 | still auth-at-use |
| Blank/missing/unknown-ID history removal | 2 history GET / 0 clear / 2 rows | **1 / 0 / 2** | response reuse |
| Positive valid-ID removal | history -> clear -> history | unchanged | still clear then one reload |

### PERF-06 — bounded metadata coverage and exact per-key totals

Command: `python .superpowers/sdd/2026-11-25-performance-remediation/task-3-benchmark.py .superpowers/sdd/2026-11-25-performance-remediation/task-7-metadata-counters.json`.
Evidence: `task-7-metadata-counters.json`, `task-7-metadata-benchmark.log`; baseline `task-3-baseline-counters.json`.

| Scenario | Before | Current |
|---|---:|---:|
| 100 successful trailer IDs | 32 covered, 32 HTTP, 68 silently dropped | **100 covered, 100 HTTP, 0 dropped**, exactly one attempt/key |
| Reservation samples | 32,0 (incomplete) | 32,32,32,4,0; max 32, final 0; four closed sessions |
| Mixed 50 trailers + 50 shows | no combined fair scheduler | 100 calls, one/key, deterministic alternating family order |
| First 32 of 100 shows persistently fail | N/A | 132 attempts: IDs 1–32 exactly 2 each, IDs 33–100 exactly 1 each; 68 successful rows, 0 failed rows persisted |

This is deliberately **more** HTTP work than the broken incomplete baseline, not false evidence of fewer requests. It proves complete deduplicated coverage and bounded ownership.

### PERF-07 — repeated actual-helper history timing/tradeoff

Command: `python .superpowers/sdd/2026-11-25-performance-remediation/task-6-dedup-benchmark.py --output .superpowers/sdd/2026-11-25-performance-remediation/task-7-history.json`.
Evidence: `task-7-history.json`, `task-7-history-benchmark.log`; the exact legacy loop and actual canonical helper produce equal output before seven runs per H.

| H | Legacy median [min,max] ms | Canonical median [min,max] ms | Outcome |
|---:|---:|---:|---|
| 20 | 0.0155 [0.0146,0.0223] | 0.1130 [0.1115,0.1395] | measured slower allocation |
| 200 | 0.3569 [0.3544,0.3795] | 0.9629 [0.9395,0.9761] | measured slower allocation |
| 1000 | 8.1065 [8.0732,8.2020] | **4.6235 [4.5977,4.7889]** | 1.753x median, non-overlapping ranges |

H=1000 separate one-run traced peaks were 4296 -> 451336 bytes. Task 6's instrumented whole-handler medians (117.8195 -> 127.2205 ms at H=1000) are retained in the coverage limits; no faster end-to-end render is claimed.

### PERF-09 — repeated 64/256/1024 KiB chunk evidence

The same Task 6 benchmark used actual `stream_download`, synthetic local files and bounded VFS double, 1/8/64 MiB, three runs/cell, exact byte counts and SHA-256. Compare current-session 64 KiB (baseline column) with 256 KiB (current/default column):

| Backend/payload | 64 KiB ms median [range]; writes | 256 KiB ms median [range]; writes |
|---|---:|---:|
| local 1 MiB | 4.0230 [3.8224,4.2763]; 16 | 4.1846 [3.8225,4.4488]; 4 — measured slower timing |
| local 8 MiB | 17.1042 [16.0344,20.7110]; 128 | 15.1606 [15.1346,15.1670]; 32 |
| local 64 MiB | 119.3466 [115.8044,122.4045]; 1024 | **103.9829 [103.9121,104.8718]; 256** |
| VFS double 1 MiB | 1.7764 [1.7592,1.8456]; 16 | 1.6804 [1.6659,1.7480]; 4 |
| VFS double 8 MiB | 9.9638 [9.8885,10.0159]; 128 | 8.8944 [8.8691,8.9843]; 32 |
| VFS double 64 MiB | 76.3969 [76.1721,76.4787]; 1024 | **66.6470 [66.5167,67.4335]; 256** |

The deterministic write reduction is 4x and hashes match in every cell. One of six current timing comparisons favors 64 KiB, so no universal timing improvement is claimed. At a 32 KiB adapter write cap all chunk sizes require exactly 32 writes and the same hash. Partial-write traced peaks increase with larger chunks; 256 KiB remains the conservative approved default, not a claim that synthetic 1024 KiB could never be faster.

## Complete regression and static/resource verification

### Full discovery

```text
python -m unittest discover -s tests -v
```

`task-7-full-precommit.log`: **Ran 307 tests in 3.785s / OK**. There are no failures, errors or skips; every prior 303 test remains and four documentation/evidence tests were added. This output includes `tests.test_resources` as well.

Focused resources + real production owner/facade:

```text
python -m unittest tests.test_resources tests.test_entrypoint -v
```

`task-7-resources-owner.log`: **25/25 OK** (7 resource tests + 18 entrypoint/composition/completion tests). Production cold DB uses its worker session and one post-promote refresh; VFS success/failure, owner close/rebuild, unified facade and action completion remain green.

### Recursive AST, XML, UTF-8-SIG and owner/job boundaries

Command:

```text
python .superpowers/sdd/2026-11-25-performance-remediation/task-7-static-checks.py .superpowers/sdd/2026-11-25-performance-remediation/task-7-static-checks.json
```

`task-7-static-checks.log` / `.json`: **OK**.

- 38 tracked Python files recursively selected by Git parsed with `ast` after `utf-8-sig` decoding.
- `addon.xml` and `resources/settings.xml` parsed; addon version 1.3.15.
- Every `yawsp_app` module except the intentional runtime/router Kodi adapters has zero `xbmc*` imports (11 files enumerated).
- Media and cold-DB invocation handlers contain queue `submit` and no direct `stream_download`/`get_link`/`get_info`/`iter_content` or direct `ensure_installed` respectively.
- `_ProductionOwner` constructs `BackgroundPrefetch` + `DownloadJobManager`; close calls both with `wait=False`.
- Webshare `.content` accesses 0, Webshare `BytesIO` calls 0, Backup DB `testzip()` calls 0.

`git diff --check` completed with exit code 0. Its only output was Git's existing Windows LF->CRLF advisory for `tests/test_documentation.py`, not a whitespace error.

## Remaining measured and runtime limits

1. Offline desktop counts/events/repeated timings are not live Kodi, ARM, Webshare/TMDB, CDN, NFS/SMB or remote VFS measurements. No real media transfer or Kodi invocation was performed.
2. Warm snapshots intentionally provide eventual visibility on a later open; cold validation and identity/generation safety take precedence over saving partial/faster output. Cross-process dedup/locking is not claimed.
3. More metadata requests than the broken admission-capped baseline means required complete coverage, not reduced traffic. Retries remain bounded and failures do not become negative cache entries.
4. Queue acceptance is not remote success. Cancellation is cooperative at operation/chunk boundaries; blocked OS/socket calls cannot be forcibly interrupted. Remote VFS rename atomicity remains backend-dependent.
5. Warm DB metadata cannot detect content edits preserving both size and `mtime_ns`. Detected mismatches perform full validation, and cold/migration/tamper operations are not claimed faster.
6. ZIP network streaming removes whole-archive copies but standard `json.load()` retains one complete JSON member and its object graph. `tracemalloc` is not RSS/native memory.
7. Small histories and whole-handler measurements do not support a blanket history speedup. Chunk selection guarantees fewer writes/identical data, not universal latency improvement.
8. Deferred minor findings from earlier reviews were not changed because none was needed for a mapped proof.

## File scope

Tracked source/documentation changes:

- `README.md` — architecture, truthful behavior, performance measurement limits and reproduction commands; existing version/install/QR/setup retained.
- `tests/test_documentation.py` — four documentation/evidence contracts plus AST mapping helpers.
- `.superpowers/sdd/2026-11-25-performance-remediation/performance-coverage.json` — machine-readable PERF-01…PERF-09 map, actual selectors, comparisons and limits (explicitly added despite the SDD ignore rule so the committed test has its input).
- `.superpowers/sdd/2026-11-25-performance-remediation/task-7-report.md` — this report.

Local SDD evidence additionally includes `task-7-baseline-full.log`, RED/GREEN logs, two selector logs, all seven benchmark outputs/raw JSON files named above, `task-7-static-checks.py/.log/.json`, and `task-7-full-precommit.log`. No production file was modified and no release/tag/version change was made.
