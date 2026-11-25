# Transport credential redaction (Task 1B)

**Scope:** `yawsp_app/tmdb.py`, `tests/test_tmdb.py` only. No service/resurrection, no scheduler, no lowering strictness or retry caps and no public error result change.

**Trace observed behavior:** `HTTPError.from raise_for_status` may precompute an error message including URL with `api_key` and/or `session_id`; they can pass into `logger.exception`/traceback chain. Current exception type must remain `requests.HTTPError` for HTTP failures and `requests.Timeout` (or original RequestException) for transport, but constructor argument values are not a valid logging redaction boundary.

**Approach:** sanitize the original requests exception's message arguments immediately before raising the existing domain `TMDBError` from it. Preserve direct original cause and response/status context objects; zero the `args` tuple as docs guarantee no filename URL parts can be reconstructed by formatted exception output. Helper is local and handles exceptions with ordinary args as well without changing callable signature/public requests parameters. All current `response.close()`/unauthorized cleanup remains. This intentionally does not reconstruct a raw ad-hoc URL regex with escaped variable edge cases.

**Required tests (before production changes):**
1. Use actual production TMDBClient/Request with fake non-secret credentials in api_key and session_id. Exercise direct `client.get` (or existing production public method) with HTTP 404/500. Build `traceback.format_exception(type(error), error, error.__traceback__)` and include own outer exception. Assert no fake credential strings. Assert `isinstance(cause, requests.HTTPError)` and existing message carries endpoint+status only.
2. Equivalent transport `requests.Timeout` synthetic (e.g. URL query and arbitrary exception transport message), assert synthetic secret not disclosed and preserved domain cause class. Don't mistake unrelated Api type message for safe log simulation; exact check is rendered __cause__ trace frame.
3. Existing full `ClientTests` must keep 429/retry-after, retries, response closing, 401 invalidation, non-directory response no hardcoded status caster, unknown media JSON errors and existing operation semantics.

**TDD:** run new focused tests on unmodified base and observe both credential-string failures. Implement only the minimal helper and exact two transport/raise branches, then run focused tests and complete `python -B -m unittest discover -s tests -v`.

## Review follow-up — nested adapter context

The independent security review reproduced an additional real path: `requests` wraps an underlying `urllib3 MaxRetryError` in a direct transport error, leaving the URL-bearing retry error in its implicit `__context__`. Clearing only the direct exception's `args` is insufficient because Python's standard traceback renders that nested context.

The integrated follow-up therefore uses `_sanitize_requests_error()` in both existing transport branches to clear the direct `args`, detach its `__context__` and `__cause__`, and set `__suppress_context__` before the same domain `raise ... from original_error`. The direct original request exception object and type, plus its `response`/`request` attributes, remain available to programmatic callers; the standard rendered exception chain no longer follows the credential-bearing nested adapter error. This is deliberately not recursive exception traversal or ad-hoc URL redaction.

A regression using a real `requests.Session` and `HTTPAdapter` demonstrates the original `MaxRetryError` context before the domain wrap, then asserts its absence from both complete `traceback.format_exception()` output and `logger.exception()` output. Retry, HTTP status, 401 invalidation, abort, return and response-close behavior are unchanged. Fix: `011e1b6c7ca01ba32d05432d8a8ba5596ddecb91`. Independent re-review marked the original P1 **ADDRESSED**, with no scoped follow-up finding; fresh `tests.test_tmdb.ClientTests` verification: **17/17 OK**. A retained request/response or separately retained original retry object can still contain its URL if explicitly serialized; ordinary standard traceback/logger rendering does not serialize those attributes.
