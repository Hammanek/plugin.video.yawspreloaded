# Native Kodi Visual Acceptance and Release Gate Design

## Intent and current state

The shutdown-hardening implementation is merged locally into `main` at `6955e2aeac3e1e1d2ab53df98d316aa61661c530`. Offline, user-visible and lifecycle checks have 600 passing tests, three expected Windows symlink-privilege skips with deterministic fail-closed alternatives, Python 3.8 AST over 25 runtime files, and verified archive structure. Native proof covers a real bounded single consumer, 32 background plus 1 interactive reservation, collisions, snapshot ownership, identity replacement, callback fans and 12-second shutdown exits. No GitHub release, tag, deployment or main-profile installation has been authorized or performed.

The remaining gate is **external visual acceptance**, not another network protocol, executor, cache or WindowXML implementation. This phase proves that a real native Kodi user sees usable controls and understandable content, rather than merely receiving correctly shaped metadata and result documents.

## Goals

1. Capture the actual native UI in an isolated **Kodi 21.2 Omega, Windows Store x64** profile at the merged runtime source. Prove the six root entries in exact order and the four fixed Objevit categories with expected visual/text structure.
2. Inspect native InfoWall artwork, poster/thumbnail/fanart geometry, localized and original titles, years, genres, rating, plot and Unicode rendering, including diacritics and supported astral characters. No unpaired surrogates or replacement normalization is allowed.
3. Check interaction visibly: focus selection, context menu behavior, original-title Webshare handoff, refresh/settings actions, back behavior, and exactly one relevant container refresh after successful watched removal. A mocked URL by itself is not a substitute for visual focus/refresh evidence.
4. Check warm snapshot reopen, manual `refresh=1`, failed-refresh complete fallback, empty results and watched generations/revisions: revision 1→2→3, generation 0→1, exactly one preterminal forced follow-up and only ID 7102 at the latest visible stage.
5. Inspect seasons/episodes including dates/runtime, and prove failed directories do not trigger an extra metadata burst.
6. Produce actual screenshots and a machine-readable verdict for a fresh independent UI reviewer. A native metadata/InfoTag smoke test can supplement, never replace, image inspection.

Only after visual acceptance may release preparation update version, setup/install documentation and exact source archive. A release package may then be offered for independently reviewed release approval; no tag/push/publish is implied by this design.

## Non-goals and anti-goals

- No GUI redesign, WindowXML, `discovery_window.py`, `DiscoveryWindow.xml`, `discovery_script.py`, `discovery_native_wall`, alternative skin or Netflix-like UI.
- No new background executor, native service duplication, new application dependency, or test-only object module masquerading as production logic. Probe-only `sitecustomize` hooks, controller/controller-session helpers and loopback adapters are permitted only outside the final source.
- No automatic visual pass from native metadata readback, resource settings, root label order, successful HTTP codes, screenshot existence or promises of readability. Do not infer pixels from InfoTag values.
- No use of the real TMDB/Webshare accounts, credentials or remote service calls. All service inputs are synthetic, controlled offline/loopback fixtures.
- No main-profile mutation, host DNS/proxy/CAs/trust-store modifications, Windows registry changes, user display settings changes or remote debugging of an installed real add-on.
- No global site data/test hook changes in shipped add-on source. Hooks must be probe-specific, source-byte-preserving and outside the final artifact.

## Chosen approach: native GUI evidence in fresh clone

Run the current merged artifact from a fresh external source checkout and existing isolated runtime clone controlled by the proven RPC/quit helper pattern, but keep it manageable by bounded visual phases rather than creating a new generalized service harness.

Capture the actual native window contents using host-provided Windows GDI+/Win32-compatible screenshot APIs and focus only the clone window, while honoring any Windows Store packaged-session rendering limitation. Initialize UI settings locally, using the existing actual Profile addon storage with the smallest safe seeded synthetic fixture values. Do not print or log any raw credential-like query material.

If that window can be created, screenshot its visible contents (not only process output). Each required screenshot must be opened and inspected by a reviewer who can evaluate image detail. Record phase, PID, source HEAD/hash, fixture/phase timing and screen/window rectangle. Reject blank, stale or unrelated screenshots and lack of crop. If a native session cannot render, capture or be inspected, mark the visual gate `UNCHECKED` and stop the candidate pipeline; it is not passed and release metadata cannot be finalized as complete.

Existing process-local observation can map TMDB requests to loopback without changing production source literals or routes. Artwork uses local `special://temp` PNG resources under the disposable profile rather than unverified remote image downloads. A direct `xbmc.executebuiltin` probe observation is optional support, not a replacement for image proof.

## Alternative approaches rejected

- **Metadata-only/native Input/readback:** useful for testing logic, data and SDK handoffs, but cannot prove focus paint, font readability, texture geometry, visual overflow or aesthetic acceptance.
- **New synthetic image of the imagined UI or HTML recreation:** it would not prove actual Kodi rendering and is forbidden as acceptance evidence. All accepted frames must be captured directly from the running native application.
- **Manual user interaction as mandatory fallback:** accepted only as an optional later route. The current user explicitly delegated visual review to this agent, so inability to inspect the actual native frame leaves the gate open rather than silently converting it into simulated approval.
- **Additional platform/API expansion:** Kodi 19, ARM, Linux/POSIX, remote VFS and true production credentials are outside this gate; they are documented as unproven and are not claimed.

## Controls and exact acceptance criteria

### Root/source gate

- Runtime source HEAD is `6955e2aeac3e1e1d2ab53df98d316aa61661c530`; any later code/docs commit must get a new exact SHA-256-checked archive and fresh relevant visual evidence.
- Menus native and ordinary root plugins or the real root path only: `Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist`, with exact localized text from the applicable catalog.
- Final `service.py` official `xbmc.service` starts exactly one owner with a clean ready/stopping lifecycle, no alternative producer thread and no parent executor.
- `addon.xml` version currently starts at 2.0.0 for source identification, not as authorization to publish.

### Objevit/public genre gate

- Categories exactly `popular_movies → /3/movie/popular`, `popular_shows → /3/tv/popular`, `top_rated_movies → /3/movie/top_rated`, `top_rated_shows → /3/tv/top_rated`. No account call, pagination, watched mutation, list cache masquerading as genre playlist, trailer/show metadata burst or autoplay.
- Across all four: 4 list GETs plus exactly one movie and one TV genre mapping request. Empty results get no genre request. A failed/incorrect list response triggers no extra genre work.
- Every row has the required detached data before the visual inspection: localized and original title, media type, integer ID/year, genres, rating, plot and poster/thumbnail/fanart. Actual visible text is Unicode-preserving, not cropped or garbled. No auto-watched or autoplay change.
- Native InfoWall is set only after the initial successful catalog directory, and later opens do not repeatedly force it. No alternate registered start script or custom WindowXML.
- Primary Webshare row targets include localized `action=search&what=<encoded localized>&sort=largest`; optional original title preserves exact original title in the same handoff form. Inspect actual route and, where possible, the visible context menu. Missing context-menu proof is reported instead of inferred.
- Public 401 closes a single response and leaves personal settings, genre bytes and identity intact; it does not clear session or produce parent transport fallback/settings loop.

### Watchlist gate

- Cold complete profiles fetch only service-side pages and one exact media/language genre mapping for nonempty rows; complete empty has a valid complete empty snapshot, no genre request and Refresh as last row.
- Warm reopen uses actual full compatible snapshot with zero account/list calls. Missing genre does not cause an uncached parent HTTP fetch/write, and no stale API key/session output is exposed.
- Manual refresh uses explicit `refresh=1` once with exact scope and returned revision/generation, and does not immediately submit an automatic duplicate refresh. A deliberately failed later page leaves the original complete snapshot byte-identical and visibly fallback/local-cache rendered instead of partial data.
- Watched mutation sends `media_type:movie`, `media_id:7101`, `watchlist:false`. Latest snapshot changes revision 1→2→3 and generation 0→1, exactly one forced preterminal follow-up executes, and ID 7102 alone remains. `completion_for` chooses ACTION and real `xbmc.executebuiltin("Container.Refresh")` occurs once after successful operation, never on failure.
- Empty/missing-complete watched attempts produce no false completed snapshot, implicit refresh, or new automatic continuation.

### Details gate

- `/3/tv/{show}` and `/3/tv/{show}/season/{season}` are the only detail list requests. Visible native season/episode counts, air date, runtime, artwork and expected search handoff values are checked, including optional/missing fields where supported. A controlled failed detail request closes once and creates no extra metadata prefetch/jobs.
- Focused native controls remain visible and operable during/after directory transitions tasks, rather than relying solely on background documentation of label text.

### Review and release gate

- Image inspections cover definitely readable text, artwork geometry, text clipping, focus visibility and meaningful hierarchy; absence of another indicator is not presumed equivalent to clear UI. Focus and disclosure alternatives must be observed through keyboard/native input if available.
- There are no domain-disabled errors hidden behind a plausible screenshot. If UI is wrong, preserve the screenshot/event role and stop before release metadata. Any fix requires a new scoped plan, never an ad-hoc skin or pending worker workaround.
- Main-profile credentials/tokens/userdata or generated binary Kodi profiles are not committed to the repo. JSON includes paths and SHA-256 of all evidence, never raw request credentials.
- Visual acceptance is independent of the current local main merge, and a final human partner review returns visual PASS/FAIL/UNCHECKED with recommendations.

## Release metadata transition (only after PASS)

A candidate **2.1.0** bump is proposed because bounded background TMDB lifecycle and user-visible snapshots/browsing/handoffs have materially changed since main's release 2.0.0. Choose it explicitly only after the visual proof and partner agreement.

At that point:

1. Make `addon.xml` version and news accurately reflect shipped user-visible behavior; don't claim unrun platform or visual review.
2. Update README version badge to exactly `2.1.0`, features, module responsibilities and every setup/latest-download/versioned link. Preserve the existing minimal known-working setup, install-from-ZIP and TMDB API-key instructions.
3. Run the complete `python -B -m unittest discover -s tests -v` gate, all 25 runtime Python 3.8 AST source files, resource/XML/settings checks, `git diff --check`, unchanged read-only/pinned CI and release verifier. CI must not gain publish permissions or credentials.
4. Keep correct archive root `plugin.video.yawspreloaded/`, ZIP CRC and exact merged source bytes. No `userdata`, raw credentials, symlinks, probe-only adapters, temp sites, screenshot/profile artifacts or old executor files enter it. Keep 32 runtime names only if a file inventory check confirms those exact names; do not hardcode a count without evidence.
5. The release commit includes the metadata updates and the evidence report with the accepted final head/archive SHA-256, then has an independent doc-quality/release-metadata review.
6. Only then request final explicit authorization for any push, Git tag `v2.1.0` and GitHub release. If authorized, attach the exact validated ZIP and verify its target commit/content. Post-publishing identity and attachment must be rechecked; approval here covers no future other-platform claim.

## Evidence and retention policy

- Path for probe media/screenshots: `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-visual-acceptance`.
- SDD reports current plan: `.superpowers/sdd/2026-11-26-kodi-native-visual-acceptance` (ignored/plugins).
- Public validated JSON `visual-verification.json` includes action evidence, actual image paths/hashes, native PID/thread invocations, generated timestamps, source identity and phase outcomes, with all platform and screenshot limitations explicit.
- Preserve previous native reports as historical source-specific evidence; do not assign a newer artifact to an older capture by scaling names or hashes. Every reported capture is a discrete named phase with provenance.
- Failed captures and harness errors are preserved but are never counted as passing candidates. No natural-language verdict is accepted when an actual inspected pixel stage is missing.

## Required independent reviews

- **Builder/design review** reports only material unclear requirements, risky unaddressed tradeoffs and builder-required choices before implementation; it checks this design, not its own assumptions.
- **Visual review** uses actual screenshot contents to evaluate controls, text/layout/focus/artwork and state fidelity. It has authority to reject the native images.
- **Release metadata and source review** compares merged source, exact archive, versioned setup guidance and any newly prepared tag/archive target. It cannot infer visual or platform acceptance from offline tests alone.

## Builder-review rulings

The user's existing delegation covers visual execution, local version/docs preparation and local commits. It does not cover push/tag/publication or installation. A fresh image-review subagent provides independent advice, not human certification; the executing agent must also actually open and inspect the PNGs.

The binding visual runtime identity is an exact inventory/hash of those bytes that affect native runtime behavior. A later docs/source provenance commit does not recursively require recapture; a version/news-only change in `addon.xml` likewise does not require recapture when every runtime Python/resource byte and the actual native addon code are unchanged. The immutable evidence records original and candidate runtime inventory plus that documented exclusion. Candidate HEAD and archive are recorded externally with separate preparation timestamps; a commit cannot contain its own hash. Any behavior/resource change invalidates the relevant captures.

Native list/detail disclosure may be used to verify fields that are not simultaneously visible in InfoWall. Representative primary fixtures must show native rendering and each row's full semantic/InfoTag evidence remains required. Focus/navigation and the Webshare original-title route are required core checks; an RPC route without visible native focus is insufficient to claim that GUI interaction passed. A stock-skin limitation is documented narrowly only where it does not substitute for missing core evidence. Unread, blank or unavailable required native frames leave their related gate UNCHECKED.

## Evidence limits

PASS in this design covers only actual Kodi 21.2 Omega Windows Store x64 isolated clone inputs and checked steps. It does not prove other Kodi/OS/GPU/platform rendering, screen reader compliance or universal aesthetic quality. No main-profile installation, real account, Linux/POSIX power-loss recovery, remote service access or unmeasured UI claim is made. Native screenshot review alone does not prove touch gesture compliance on other devices; keyboard-visible/accessible native controls are still necessary.

PNG frame captures are explicitly allowed evidence for the delegated review, not AI-generated visual substitutes and not an interface redesign. Allowed mechanical bitwise difference across checkouts must be explained rather than concealed; accepted source root/version/CRC and correct attachment identity remain mandatory.
