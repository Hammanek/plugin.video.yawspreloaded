# Native visual acceptance, release preparation and gated publication

**Goal:** practically inspect the merged native Kodi UI, prepare an evidence-backed 2.1.0 candidate if it passes, and stop before any public action unless separately authorized.

**Source:** `main` `6955e2aeac3e1e1d2ab53df98d316aa61661c530` in isolated `visual-acceptance`.

**Gate:** `python -B -m unittest discover -s tests -v`, `python -B scripts/verify_release.py`, exact source-byte archive, native Kodi 21.2 x64 screenshot review. No code redesign is planned.

**User decisions already made:** local merge is complete; the user delegated remaining work to this agent; publication/tag/push/install remain independently gated.

---

### Task 1: New source-identical isolated visual environment

**External files:** `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-visual-acceptance\`

- Copy the previously proven Kodi 21.2 executable/dependencies to a new disposable application directory; never reuse its mutable profile as validation input.
- Deploy exactly the currently selected runtime files from this checkout, compile each runtime Python file with Python 3.8 `ast.parse(feature_version=(3,8))`, check XML/settings and retain exact source/deployed hashes.
- Retain screenshots, redacted synthetic event logs and machine summary outside the repository archive. All TMDB outputs are loopback synthetic fixture values; `special://temp` artwork is local.
- The only addon production source remains the ordinary plugin/service pair. The environment `sitecustomize` records observations/redirects and adds no new production thread or behavior.

**Verify:** exact HEAD and complete runtime hash inventory match; executable identity matches Kodi 21.2 archive already held locally; empty fresh profile and ordinary clean quit.

### Task 2: Root and four native Objevit catalogs

**Synthetic cases:** localized/original titles, years, genres, rating, Unicode plots, local PNG poster/thumb/fanart. Empty, public401 and all four nonempty categories use named phase fixtures.

- Activate the normal root through `GUI.ActivateWindow` and inspect its six-entry exact order.
- Capture root and category screens using actual rendered Win32 pixels; inspect each image with the image-reading tool, not just metadata. Hash and map every accepted screenshot to one PID/route/time.
- Verify four category folders and 4 list + 1 movie-genre + 1 TV-genre requests. Exercise focus/input and Webshare handoff in its native context menu where available; record any unobservable feature as UNCHECKED.
- Verify InfoWall native artwork and Unicode fields, no clipping/focus/texture issues, and InfoWall view default exactly once.

**Accept:** actual images prove readable text, visible focus and geometry. Screenshot existence alone or RPC InfoTags alone is no pass. A defect stops release and gets a new failing regression before any production fix.

### Task 3: Watchlist and details visual flows

**Synthetic fixtures:** two-page movies with IDs 7101/7102, movie 7301-style show details, changing watched response, later-page and detail failures, local artwork.

- Cold complete fetch: screenshot and inspect initial watchlist; warm reopen: zero account/list calls and warm snapshot image.
- Manual explicit refresh and failed later page: compare original complete snapshot bytes, rendered fallback state and no duplicate.
- Watched ID 7101: POST false; revision 1→2→3/generation 0→1; one forced continuation, latest only 7102, one `Container.Refresh`, final rendered result.
- Seasons/episodes and failed detail: check text/date/runtime/artwork and no subsequent metadata jobs; inspect empty results with Refresh last.
- Keep current exact route/query assertions already covered by the 600-test/native contract evidence; image proof is supplemental, not a replacement.

**Accept:** each actual step and screenshot is independently mapped and rejected/accepted; no claims for other skins/platforms/hardware.

### Task 4: Independent image review and candidate documentation

- Let a fresh reviewer read actual PNG files with their rendered contents and route/source evidence. It must distinguish native pass/fail/unchecked and flag readability/focus/artwork defects.
- If any required scene is not actually rendered/captured, leave gate UNCHECKED and do not declare visual PASS.
- Once PASS: set addon news/version to 2.1.0 and update README badge, feature/setup/install instructions and current source-specific evidence. Remove stale statements saying the local merge/this visual gate are still open; retain scoped limitations for untested platforms and real-account behavior.
- Commit release metadata, keep screenshots/private profile evidence external, and document final hashes. Build only `plugin.video.yawspreloaded-2.1.0.zip` from tracked runtime files through `scripts.verify_release.py`; check root, members, CRC and exact bytes. Do not alter CI permissions/dependencies.

**Verify:** rerun entire 600-test suite, 3.8 AST/XML/settings, `git diff --check`, release verifier and archive inventory. Independent docs/artifact review must find no mismatched version/link/candidate identity.

### Task 5: Separate gated publication (not currently authorized)

Only on a separate explicit approval specifying push/tag/release:

1. Merge the prepared candidate branch into local main without altering unrelated user work.
2. Push intended commit, tag `v2.1.0`, and create the GitHub release using available authenticated tooling; no secrets in logs.
3. Attach exactly the validated ZIP; verify tag/release target, archive SHA-256 and download attachment after publication.
4. No main-profile deployment or broader compatibility claim follows automatically. Installation remains another separate action.

If no explicit authorization arrives, finish with candidate and evidence paths, list publication as pending. No tools may perform a remote irreversible action in this phase.
