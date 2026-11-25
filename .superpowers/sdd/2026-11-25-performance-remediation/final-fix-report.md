# Final performance remediation fix report

## Scope and acceptance

- Branch/worktree: `functional-remediation`; implementation base `32093b9` (`Document performance remediation evidence`).
- Scope is exactly I-1…I-4 and M-1…M-4. No release, tag, version, README structure, deferred-minor behavior, public route, setting, localization, live API, credential, network, or architecture change was made.
- Original suite: **307 tests, OK** (fresh baseline before edits, 3.880 s). Final suite: **316 tests, OK**. All 307 previous tests remain; nine regressions were added, and the existing installed-notification regression was made deterministic.
- Every production defect had a failing focused regression before its minimal fix. The unknown-history test was intentionally GREEN before production changes because that behavior was already correct; it now prevents recurrence. M-3 and the evidence-label repairs were test/evidence-only.

## I-1 — initially missing account now reuses the resolved snapshot

**Root cause and RED.** `list_watchlist` kept the early `snapshot = None` after resolving a missing account ID. The two-opening production test resolved the second account successfully, then attempted an unexpected second watchlist GET against the network-disabled fake and failed. This reproduced duplicate cold work without relaxing identity rules.

**Fix.** `yawsp_app/tmdb_ui.py` now loads the newly resolved account/media/language scope immediately after successful staged identity resolution. Warm completion passes the invocation-captured `allow_missing_account=True` to watchlist refresh. No speculative global account-ID publication was added.

**GREEN and exact counters.** `tests.test_entrypoint.EntrypointTests.test_production_missing_account_second_open_reuses_complete_scoped_snapshot` performs two real production-router openings with an initially missing ID:

| Counter across both openings | Before | After |
|---|---:|---:|
| `/3/account` | 2 | 2 |
| Full watchlist GET | 2 | **1** |
| Shared genre GET | 1 | 1 |
| Second-opening watchlist/genre GETs | watchlist repeated | **0 / 0** |

Both openings render the same complete Czech global order `Áčko`, `Chata`; the saved record remains `SnapshotScope("account-1", "movies", "en-US")`; the setting remains empty; and the queued refresh carries `allow_missing_account=True`. Existing other-account rejection, generation, invalidation, and cold-save tests remain in the 40-selector proof.

## I-2 — refresh language is bound to the request scope

**Root cause and RED.** The worker client used live `self.locale_mapper`. The deterministic switch-away-and-back test observed query `language=en-US` instead of `cs-CZ`, even though the final identity check returned to `cs-CZ` and allowed the save.

**Fix.** `yawsp_app/tmdb.py` constructs the watchlist worker client with `lambda: scope.language`. Account/session/language identity validation and expected-generation saves are unchanged.

**GREEN.** `tests.test_tmdb.WatchlistBackgroundTests.test_switch_away_and_back_around_http_uses_scope_language_and_save` proves exactly one HTTP request with `language=cs-CZ`, saved results under the original `cs-CZ` scope, and worker-session closure.

## I-3 — worker start failure rolls back only the unaccepted job

**Root cause and RED.** Forced `Thread.start()` `RuntimeError` escaped after pending/status/reservation insertion, leaving the key occupied and preventing retry.

**Fix.** `yawsp_app/download_jobs.py` catches the start failure, removes the specific `_Job` from pending, conditionally removes only its active reservation, removes its queued status, restores the prior worker reference, notifies waiters, logs, and returns rejection. It invokes no operation callback and does not close the manager or disturb accepted jobs.

**GREEN.** `tests.test_download_jobs.DownloadJobManagerTests.test_worker_start_failure_rolls_back_reservation_and_retry_succeeds` proves rejection, absent status/reservation/pending entry, no rejected callbacks, a successful same-key retry with exact `run -> succeeded` events, then clean `close(wait=True)`. Existing duplicate/quota/failure/abort/rekey/close/concurrent handoff tests all passed in the related and full suites.

## I-4 — Backup DB signature regeneration and promotion lifecycle

**Root cause and RED.** Drifted or corrupt sidecars caused full structural validation on every `is_valid()` because the refreshed signature was never written. Two `is_valid()` calls after mtime-only drift or sidecar corruption each parsed both test documents (**4 data reads total**) and wrote no replacement. New-install sidecar replacement also preceded DB promotion, so forced promotion failure changed the marker from the previous `A.txt` identity to uncommitted `B.txt` metadata.

**Fix.** `yawsp_app/backup_db.py`:

- UI-facing `is_valid()` now runs `_validate_and_record()` after any signature mismatch; malformed data or a failed signature write is not reported valid.
- A new installation writes its signature only after final DB promotion. Marker-write failure participates in rollback; rollback removes the newly promoted directory when necessary, restores the previous DB, and leaves the previous atomic marker intact. Full stream extraction, CRC reads, manifest and JSON validation are unchanged.

**GREEN and exact counters.** Three tests cover the lifecycle:

| Scenario | Before | After |
|---|---|---|
| Two `is_valid()` calls after mtime-only drift | 4 data reads, 0 regenerated sidecars | **2 reads on first, 0 on second, 1 regenerated sidecar** |
| Two `is_valid()` calls with corrupt JSON sidecar | 4 data reads, 0 valid replacements | **2 reads on first, 0 on second, schema-v1 sidecar restored** |
| Forced final promotion failure | marker replaced with uncommitted `B.txt` data | **previous marker bytes and previous damaged DB bytes preserved exactly** |

Tests: `test_is_valid_regenerates_signature_after_mtime_only_drift`, `test_is_valid_recreates_corrupt_sidecar_then_uses_warm_metadata`, and `test_failed_promotion_preserves_previous_validation_marker`. Existing tamper, rescue, double-replacement, CRC, unsafe-member, and migration tests remain green. The confirmed `benchmark-baseline.json` was not modified.

## M-1 — independent post-completion responsibilities

**Root cause and RED.** A throwing default-view hook aborted the shared callback; the focused test observed zero trailer submissions and no snapshot refresh.

**Fix.** `yawsp_app/tmdb_ui.py` isolates and logs default-view, snapshot-refresh, and metadata-submission failures independently. The whole callback remains a `DirectoryResult.post_complete` callback run only after the runner's sole `endOfDirectory`.

**GREEN.** `tests.test_tmdb_ui.TMDBUITests.test_throwing_view_hook_does_not_block_warm_refresh_or_metadata_submission` changes submissions from **0 refresh / 0 trailer** to **1 refresh / 1 trailer** after the thrown view error. Exact event order is `add -> end -> SetViewMode attempt -> watchlist -> trailers`, and the view failure is logged. Existing callback ordering and cache-save tests remain green.

## M-2 — worker sessions clone headers but never share the main session

**Root cause and RED.** Both raw `requests.Session` worker factories produced the default `python-requests/2.32.5` User-Agent rather than the configured main Firefox User-Agent and had no matching Referer.

**Fix.** `yawsp_app/router.py` has one factory definition used independently by TMDB and download managers. Each call constructs a new `requests.Session` and copies the main session's complete current headers into its own header mapping. No main or worker session object is shared; per-job close behavior is unchanged.

**GREEN.** `tests.test_entrypoint.EntrypointTests.test_production_worker_session_factories_clone_main_headers_without_sharing` asserts three distinct worker instances from three factory calls, all distinct from the UI session, equal full header mappings and exact equal `User-Agent`/`Referer`, independent header objects, and explicit cleanup. Existing tests prove separate per-job ownership and closure.

## M-3 — installed-notification test waits for the real log event

`tests/test_webshare_ui.WebshareUITests.test_installed_notification_error_cannot_suppress_refresh_or_change_job_outcome` previously used notifier entry (`attempted`) as the gate before reading the production logger. It now uses an event set by the actual `Backup DB installed notification failed` exception-log call. There is no timing sleep or notifier-attempt gate.

The preserved regression still proves a failing notification does not alter the real job outcome: terminal state `succeeded`, valid installed DB, exactly **one** `Container.Refresh`, one matching exception log, and a successful warm detail open. `final-focused-tests.log` includes this existing regression alongside the nine new methods.

## M-4 — evidence labels and permanent unknown-ID coverage

- `performance-coverage.json` and `task-7-report.md` now label the fresh-second-warm and cold-page-2 historical baseline cells **unmeasured** where no matching legacy artifact exists; neither number is fabricated from `task-2-baseline-counters.json`. The cold-DB baseline was untouched.
- `task-7-cache-benchmark.log` changed from **0 bytes** to a genuine **1690-byte, 110-line JSON transcript** created by rerunning `task-1_operation_benchmark.py`. It matches all six full result cells: N=1/8/32 medians are **1/1/4** fsyncs and replacements per trailer/show family, three invariant runs per cell.
- New `tests.test_webshare_ui.WebshareUITests.test_unknown_history_removal_does_not_clear_other_valid_id_or_reload` supplies the missing hard assertion for an unrelated remove ID with another valid `download_id`: exactly **1 history / 0 clear_history** requests, no reload, and both original rows rendered from the reused response.
- Coverage now maps **40 unique selectors (40 passed)** rather than 31; test totals no longer make an unsupported broad unknown-ID claim.

## Final verification evidence

All commands were offline. Logs are in this SDD directory.

| Check | Result | Evidence |
|---|---:|---|
| Focused final regressions (9 new + deterministic M-3) | **10/10 OK**, 0.204 s | `final-focused-tests.log` |
| Related production/test modules | **213/213 OK**, 2.889 s | command output reviewed during GREEN |
| All mapped PERF selectors | **40/40 OK**, 1.043 s | `final-proof-selectors.log` |
| `python -m unittest discover -s tests -v` | **316/316 OK**, 4.270 s | `final-full-tests.log` |
| `python -m unittest tests.test_resources tests.test_entrypoint -v` | **27/27 OK**, 0.395 s | fresh reviewed command output |
| Recursive AST/UTF-8-SIG, XML, service/queue/owner boundaries | **OK: 38 Python files; addon.xml + settings.xml; version 1.3.15; zero forbidden imports/calls** | `final-static-checks.json`, `task-7-static-checks.py` |
| `git diff --check` | exit 0; only existing Git LF→CRLF advisories, no whitespace errors | reviewed command output |

Static boundary facts remain: zero service `xbmc*` imports, media/cold-DB direct forbidden calls empty, owner constructs and closes both managers with `wait=False`, zero Webshare `.content` accesses, zero Webshare `BytesIO` calls, and zero Backup DB `testzip()` calls.

## Changed files and retained limits

- Production: `yawsp_app/tmdb_ui.py`, `yawsp_app/tmdb.py`, `yawsp_app/download_jobs.py`, `yawsp_app/backup_db.py`, `yawsp_app/router.py`.
- Tests: `tests/test_entrypoint.py`, `tests/test_tmdb.py`, `tests/test_tmdb_ui.py`, `tests/test_download_jobs.py`, `tests/test_backup_db.py`, `tests/test_webshare_ui.py`.
- Evidence/docs only: `performance-coverage.json`, `task-7-report.md`, this report, final test logs/static JSON, and the regenerated cache transcript. No README/addon version/install content changed.

Limits are unchanged: synthetic test doubles and desktop evidence are not live Kodi, TMDB/Webshare, network, ARM, SMB/NFS, or remote-VFS claims. Snapshot sharing/reservations, thread safety and account ownership are process-local. Full cold downloads/extraction/validation still occur where required; ZIP memory still includes one JSON member and object graph. Warm metadata cannot detect content edits preserving both size and `mtime_ns`. No new timing, cross-process, live completion, or VFS-atomicity claim is made.
