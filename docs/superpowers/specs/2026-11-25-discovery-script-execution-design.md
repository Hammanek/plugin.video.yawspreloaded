# Objevit — standalone script execution design (proposal)

**Status:** proposed for review, **not approved for implementation yet**. User selected “Nový script execution design” after both plugin-modal approaches failed in actual Kodi. This document does not authorize merging service code, releasing or provisioning a new profile.

## Why a different execution model is needed

The user still wants the reversible, Netflix-inspired native Objevit presentation. The current branch already proves root/catalog data, sessionless public TMDB calls, InfoWall, native InfoTagVideo, manual refresh/fallback and safe CI packaging. The remaining blocker is specifically custom `WindowXML`, not the catalog model.

Actual isolated Kodi 21.2 evidence:

1. `NativeDiscoveryWindow()` fails without the mandatory `xmlFilename` and `scriptPath` constructor arguments.
2. Forwarding all four documented arguments in a deployed candidate removes that binding error, but `doModal()` during a plugin directory never reaches `onInit`; Kodi refuses window 13000 while Busy 10138 is active.
3. Reversing MRO does not help. There was no production constructor patch.
4. An approved post-completion spike does reach `onInit` and controls after `endOfDirectory`, but Back causes the plugin route to reopen: **2 modal entries / 2 exits / 1 onInit / 1 Back**. It therefore fails the one-modal contract.
5. InfoWall and real native InfoTag read-back, manual refresh/fallback and the corrected sessionless public list/genre calls have independent live PASS evidence. Active legacy background shutdown separately reproduced a HANG and is outside this design.

Relevant retained evidence: `kodi-task9-windowxml-validation/verification.json`, `constructor-fix-verification.json`, `post-completion-spike/post-completion-verification.json`, and `task-9-live-report.md`.

## Decided approach

Run the custom presenter in a **separate, on-demand `xbmc.python.script` invocation** of the existing add-on, never as a background service and never inside a plugin `ActionRunner` directory or `post_complete` callback.

A new root `discovery_script.py` starts only when Kodi executes the official built-in `RunScript(add-on id[, args])`. It uses the same add-on ID `plugin.video.yawspreloaded` and a new `xbmc.python.script` extension point. It is not registered as `xbmc.service`, spawned by a custom thread/process, auto-started, or left resident.

Official constraints used:

- `WindowXML(xmlFilename, scriptPath, defaultSkin, defaultRes)` has mandatory `xmlFilename` and `scriptPath` at binding construction.
- `RunScript(script[,args*])` uses an add-on ID; extra parameters are available through `sys.argv`.
- The script’s own `xbmcaddon.Addon()` reads API key, locale and presentation settings in its own interpreter. No credential, session, callable or transport object crosses the invocation boundary.

The candidate first-open trigger is exactly `RunScript(plugin.video.yawspreloaded,source=popular_movies)`. That registration/launch combination is what the phase-0 live proof must establish; it is not treated as already proven merely because documentation mentions the built-in.

## Presentation and launch behavior

### Root

Keep exactly: **Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist**. Root remains static and makes no discovery request.

For `discovery_native_wall=false` or an unset value, first entry is the script command above. For `discovery_native_wall=true`, first entry remains an ordinary plugin discovery URL and renders the already-proven InfoWall. The setting is evaluated at the next root/Objevit opening, never by a worker and without reinstall or changing watchlist views.

For an externally invoked custom `action=tmdb_discovery` route, the plugin must not open a window itself. After validation it may present only a lightweight native launcher item using the same `RunScript` command. Legacy/bookmarked plugin custom URLs do not again become hidden modal invocations. The actual modal begins only in the dedicated script. This explicit launcher boundary prevents the failed recursive plugin/modal loop.

### One script invocation

1. Validate only the four immutable source identifiers: `popular_movies`, `popular_shows`, `top_rated_movies`, `top_rated_shows`. Unknown input fails locally with no HTTP/window.
2. Check API key before the first request. No personal account discovery or watchlist snapshot operation is allowed.
3. Construct a script-owned `requests.Session` and existing `TMDBClient`, then apply the existing `public_tmdb_client()` sessionless view. Close the session in `finally`.
4. Use one local `GenreRepository`, the same fixed source mapping and normalized `DiscoveryRecord` model as InfoWall. Selected data title/original title/year/genre/rating/plot/art remain shared with native InfoWall semantics.
5. Queue one category into `DiscoveryWindow`, which constructs using mandatory `("DiscoveryWindow.xml", addon_path, "default", "720p")` forwarded through the existing class hierarchy. No MRO, timeout, daemon or worker redesign.
6. Call `doModal()` **once** in that script invocation. Controls are initialized in guarded `onInit`, with the existing `<views>1200</views>` registration and base-first navigation behavior retained.
7. On Back, close once and return without another discovery or script invocation. Repeated Back/Enter/category callbacks after closure remain ignored.
8. On Enter, retain the selected record’s existing Webshare URL. Close once; after `doModal()` returns, perform exactly one ordinary `Container.Update` to that URL using `action=search&what=<localized title>&sort=largest`. Multiple/repeated callbacks cannot cause a second handoff.
9. Return from the script and release its transport/window objects. No generic queue or parent-executor handoff exists.

**Important:** the script returning to the current listing must not automatically re-execute `RunScript`. Actual one-run/Back behavior is the key acceptance condition, not just successful `onInit`.

## Execution cardinality and public URL contract

“One modal” means **one modal per explicit accepted launcher activation**, not a process-global singleton. One `RunScript` event must create one script invocation, one modal and at most one handoff. `_closed` suppresses later callbacks of that window. Back must never create a new activation automatically.

Two explicit separate launcher events are two activations, not a special singleton exception. Gate 0 includes rapid repeated activation to discover actual Kodi behavior. If Kodi serializes them safely, record the sequence and require one clean modal per event with no overlap. If they can overlap, create another Busy conflict, or lose invocation identity, this design fails at Gate 0 rather than introducing an unreviewed global lock/service/singleton. A second modal is **not** evidence of a bug only when it has a separate observed explicit activation ID; the earlier one-event/two-modal failure remains prohibited.

The script must never derive a plugin URL from its script argv/path or an assumed invocation handle. In the script-owned presenter, use the explicit existing public base **`plugin://plugin.video.yawspreloaded/`** and shared `urlencode` behavior. The selected destination is exactly **`plugin://plugin.video.yawspreloaded/?action=search&what=<encoded localized title>&sort=largest`**. Original-title commands use the same base. No API key, account, access token or transport object is part of these URLs.

Native InfoWall continues using its plugin runtime base URL and produces the same canonical search target. Gate 0 verifies this public URL/base is available before any implementation refactor; Gate 2 verifies actual Webshare handoff to that route.

## Network, identity and privacy invariants

Preserve the existing four immutable catalog APIs and custom/property response source semantics. Per selected category: one list request and at most one shared movie/TV genre request. No pagination, `/account`, watchlist snapshot, trailer/show metadata fetch, unknown endpoint interpolation, autoplay, watched mutation or metadata prefetch occurs.

Public request query retains only normal locale and required API key. `public_tmdb_client()` continues hiding `tmdb_access_token`, sanitizes inherited `HTTPError`/timeout/nested adapter chains, and denies public 401 any authority over personal watchlist identity. Its existence in this design does not transport a parent session or request client into another interpreter; a distinct script-owned transport session is created and closed there.

API key failure produces the normal localized settings/error flow before any request or modal. Error display/retry remains within the one window and makes one retry per activation; no extra modal or settings navigation loop. Loading uses core 1040; public empty result uses 30440. `DiscoveryWindow.xml` remains a bundled local addon resource, never user-controlled path input.

No cache writes, cross-interpreter locks, mutable snapshot generation, shared in-memory reservations, daemon conversion, socket timeout changes or general `XBMC.service` state reconciliation are proposed.

## Approaches considered

- **Plugin directory `doModal`:** rejected by the real Busy refusal and unreachable `onInit`.
- **Post-completion plugin `doModal`:** reaches `onInit` but re-invokes the route on Back; two modals violate completion bounds.
- **Native InfoWall only:** already live-proven and remains an available user-selected fallback, but the user selected one more separate execution investigation rather than abandoning custom presentation.
- **Full `xbmc.service` queue:** rejected earlier by independent cache/queue/security/performance reviews and not reintroduced here.
- **On-demand `xbmc.python.script`:** selected for a bounded proof because it targets the failed GUI invocation/lifecycle, not transport threading.

## Non-goals and anti-goals

- No claim that this resolves the independently reproduced active-background shutdown HANG.
- No claim that script invocation alone cancels requests faster or supplies cross-interpreter cache transactions.
- No alternate generic closure transport, service installer, daemon/thread process, new network client implementation, global setting rewrite, hidden native wall fallback or user-data migration.
- No merging, deploying to the main profile, version increase, Git tag, release or GitHub upload in this design.
- No “visual PASS” inferred from events: texture decoding, layout contrast, focus paint and poster appearance need explicit actual GUI review after the functional script proof.

## Evidence gates and stop conditions

### Gate 0 — disposable script-execution proof only

Before changing production code, use a new isolated clone/staging profile to prove the exact registration and launch from an ordinary root invocation:

- the `xbmc.python.script` extension is enabled with the plugin;
- exactly one `RunScript(plugin.video.yawspreloaded,source=popular_movies)` starts `discovery_script.py`;
- the explicit public search base above is used, not script argv/path or a synthetic handle;
- mandatory four-argument `WindowXML` constructs, `onInit` runs, registered controls/panel become usable without Busy refusal;
- one Back closes one modal and `doModal()` exits; **no second script execution or modal** follows from that one activation;
- rapid repeated activation has an observed, serialized invocation identity and safely closes one modal per activation without overlapping UI; otherwise stop;
- no cloud/API call or account activity is needed for this proof. Current logging must identify actual invocation/controls/exit and the source hash.

If this fails (unsupported coexistence, wrong file invoked, no `onInit`, Busy rejection, a second launch or unclosable modal), stop this approach and leave production unchanged. Do not turn it into a queue, MRO retry sequence or another hidden architecture change. Offer the already-proven InfoWall alternative again.

### Gate 1 — tests and bounded implementation

Only after Gate 0 PASS:

- Turn the intentional SIP-constructor RED regression green by forwarding the four documented arguments in production; preserve lifecycle/base-first tests.
- Extract only the reusable catalog/data-controller boundary required by the script; existing native InfoWall and sessionless transport continue through their tests. No metadata/cache rewrite.
- Add `discovery_script.py`, extension registration, script-owned owner/close and the direct launcher boundary. Script source arguments are allowlisted enums, never serialized credentials or objects.
- Add tests for root URL/command choice, no static HTTP, setting reversal at next open, one modal per script, Back zero handoff, Enter once, retry/error/empty states and no re-invocation.
- Add archive/package coverage for the new root script and local XML. Dynamic verifier selection must choose them with no fixed count/duplicate payload.

### Gate 2 — isolated current-code functional proof

Repeat the formerly blocked cases on the exact produced commit:

- custom first open plus actual Right/Left/Up/Down focus/hero consistency and one handoff after return;
- all four categories with four list calls and at most one shared genre call per media type; no personal/background request;
- error 503 → visible retry → content with one modal;
- empty → 30440 and reachable category with no genres;
- native InfoWall next-open mode remains functional and switching false restores one script modal;
- missing key/unknown source and public 401 retain non-network/local identity guarantees.

Keep results per condition and cache/source hash. A live failure is not hidden behind offline doubles.

### Gate 3 — final quality/release checks

Run the full unittest discovery, Python 3.8 AST, all XML/PO gates, active performance/security/functional reviews, `git diff --check` and the release verifier. Record a distinct **UI execution result** and retain the independent **legacy shutdown result**; do not merge them into a blanket “all lifecycle solved” statement.

No integration/release is authorized automatically after these gates; final user approval remains separately required.

## Documentation changes if adopted

README will state the actual on-demand script execution model and its launcher behavior instead of implying a plugin-directory modal. Version/release 2.0.0 setup and `<news>` stay untouched. Pending live verification remains explicit until Gate 2 passes. The existing 2.0.0 backup records and all user database/text/backups remain unchanged.

## Builder latitude

The builder may choose small internal names and whether a separate catalog reader/helper or a thin controller best reuses the already-bounded `DiscoveryRecord` model, avoiding duplicated transport logic. They may not change the invocation model, immutable categories, public/client identity isolation, modal counts, settings behavior, InfoWall semantics, media-only download worker, timeouts or error sanitization.
