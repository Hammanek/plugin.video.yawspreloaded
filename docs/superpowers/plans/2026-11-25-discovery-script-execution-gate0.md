# Objevit Standalone Script Execution Gate 0 Implementation Plan

> **Final outcome — FEASIBILITY FAIL:** ordinary root selection routed literal `RunScript` to VideoPlayer instead of script activation. Task 1 acceptance failed; Task 2 was never run. Task 3 independently verified 14 PASS / 2 FAIL / 15 UNCHECKED. Project production remains unimplemented. Do not treat this plan as an authorization to retry the launcher or advance an offline partial result to production.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking only if the experiment is evaluated; never override FAIL/UNCHECKED to claim feasibility.

**Goal:** Prove in a disposable isolated Kodi 21.2 profile that a standalone `xbmc.python.script` invocation can open one custom WindowXML outside the failed plugin-directory lifecycle, close exactly once per explicit activation, and preserve the canonical Webshare URL contract.

**Architecture:** A temporary `discovery_script.py` executes through the existing add-on ID using documented `RunScript`. The first root item activates it only for this staged fixture; a temporary mandatory-argument forwarding fix lets the real WindowXML code reach `onInit`. No production implementation is included in this plan; two serialized activation scenarios and a null-cloud one-item record separate GUI feasibility from metadata/cache work.

**Tech Stack:** Kodi 21.2 Windows Store x64 cloned runtime with embedded CPython 3.8.15, Python stdlib, `xbmc.python.script`, `RunScript`, `WindowXML`, `ActionRunner`, existing add-on modules and loopback JSON-RPC only

**Spec:** `docs/superpowers/specs/2026-11-25-discovery-script-execution-design.md` (user approved for this Gate 0 test, read together with this plan)

## Global Constraints

- Only disposable files under `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-discovery-script-gate0` and ignored task reports may be created/modified. Repository production/tests/README/addon files are not changed. Preserve the existing intentional constructor RED in `tests/test_discovery_window.py`, including its before/after hash; do not convert it into GREEN without a later implementation plan.
- Copy only the proven Kodi clone minus `portable_data`; new executable SHA-256 must be `c6b1a566a1b4185ed9f6392889600a0bef08f86babd5b21744f67daee9dc3664`. Create fresh profile data per scenario, with real addon discovery/enablement and loopback RPC—not main Store executable/profile/port 9090.
- No cloud API, personal credentials, fixture credentials in logs, service/thread workers, timeouts/daemon changes, additional architecture/MRO/WindowXMLDialog variants, or alternative launcher chains. Any failed gate ends this approach rather than producing a production patch.
- Temporary mandatory forwarding is exactly `NativeDiscoveryWindow("DiscoveryWindow.xml", addon_path, "default", "720p")` and forwarding those exact `*args, **kwargs` through the current MRO. Preserve current `<views>1200</views>` and onInit/base-first callback logic.
- Launch is the actual static root item using `RunScript(plugin.video.yawspreloaded,source=popular_movies)`. No direct constructor-only result may substitute for a real root user activation. Unknown source/core public URI must not use `sys.argv[0]` as plugin base.
- Canonical Webshare destination is `plugin://plugin.video.yawspreloaded/?action=search&what=<encoded localized title>&sort=largest`; no credentials in query/data. One-item records are offline only.
- Every wait has a deadline plus process-liveness predicate. Before cleanup or process stop verify exact executable path; RPC quit first, then stop only that spawned clone PID if needed. No forced stop counts as PASS.
- One modal means one modal per explicit activation. `single` tolerates zero second activation after its one Back. `repeat` creates two separately identified explicit events and requires their real Kodi behavior to be serialized with no overlapping windows. If Kodi instead produces overlap, a Busy rejection or lost identity, mark FAIL; never invent a singleton lock/service to compensate.
- Existing active-background shutdown HANG and visual texture/layout/poster evidence are outside this gate. Do not claim either from it.

## Review Focus

- Binding admission: `window_created` must use all four documented arguments before any control access; reject early controls and the historical zero-argument `TypeError` (Task 1).
- Busy/lifecycle admission: actual root activation must reach `onInit`, control title and one-result panel without a modal refusal (Task 1).
- Completion/idempotency: one Back causes one native close/`doModal` exit and no unobserved second invocation (Tasks 1 and 3).
- Intentional concurrent activation: separate launcher events must be distinguishable and serialized, never silently converted into an overlapping or automatic second modal (Tasks 2 and 3).
- URL/source boundary: recorded one-item destination uses the explicit plugin base, encoded title and `sort=largest`, never the script path or credentials (Tasks 1 and 3).

---

### Task 1: Disposable one-activation script launcher validation

**Files:**
- Create only under `C:\Users\jkrat\AppData\Local\Temp\opencode\kodi-discovery-script-gate0`:
  - `stage_probe.py` — provenance/cloned source preparation and fresh additive temporary script registration, not repository construction
  - `run_single.py` — spawn new clone, drive actual root activation/Back and kill only on bounded unwinding failure
  - temporary `staging/addons/plugin.video.yawspreloaded/discovery_script.py` — one fixed offline record, source allowlist, canonical public base URI, guarded `doModal` entry
  - temporary `staging/.../yawsp_app/discovery_window.py` — mandatory-argument forwarding candidate only
  - `runs/single/summary.json` — machine assertion results, source hashes and events

**Interfaces:**
- Consumes: user-approved spec; proven `kodi-task9-windowxml-validation` harness/provenance; current `discovery_window.py`, `DiscoveryRecord`, `DiscoveryWindow`; current normal root `ActionRunner` path and addon enable/disable APIs.
- Produces: actual `single` evidence with callable `discovery_script.main()`, observed script activation ID, onInit/control/panel events, canonical `search_url`, one close and no second activation.

- [ ] **Step 1: Verify provenance and baseline before writing a launchable candidate**

Run `git rev-parse HEAD` and `git status --short`; expected current production HEAD `0624bda84892b855d2cbb1dad359456f497de969` with only intentional constructor RED (and approved documentation commits may follow). Source repo must have no production change from this plan. Verify the new clone hash and embedded CPython 3.8 property via immutable clone provenance; do not start it yet. Reject if any executable differs.

- [ ] **Step 2: Create the disposable registration and controller**

Register both the original `xbmc.python.pluginsource` and temporary `xbmc.python.script` extension in staged `addon.xml`, leaving main source/version semantics untouched otherwise. Create `discovery_script.main()` that validates source equals `popular_movies`, constructs one detached synthetic record with public base `plugin://plugin.video.yawspreloaded/`, queues `popular_movies` and invokes exactly one `doModal`. Its item loader supplies one offline record directly—no HTTP/account/cache/session or hidden background work.

Modify the temporary factory invocation as specified in Global Constraints, no other class changes. Expose real onInit/control/registered-view state and `getCurrentListPosition`. No plugin directory or `post_complete` is allowed to call the window or modal; the scripted event starts only from the root command after user selection. Before launch, inspect that temporary script has no generic `exec`, request forwarding, `execv`, shell or credential argv.

- [ ] **Step 3: Run the ordinary root one-activation probe**

Use real addon discovery/enable and actual root. Activate Objevit through supported Input selection. Wait for actual script activation and `onInit`, checking `endOfDirectory` for root before that script event; no `ActionRunner`/plugin directory `doModal` order may be involved for the custom window.

Require `window_created` with the four exact arguments, no original binding `TypeError`, actual custom current window, no `13000`/modal refusal, title value and panel/list-size 1. Validate the offline record’s canonical `search_url` before stopping: encoded title, `action=search`, `sort=largest`, no session_id. Drive one `Input.Back`, then wait for `doModal` exit and script finally.

Expected: one modal entry/one close/one exit/no later `RunScript` or onInit from this activation. A second activation/frame event without explicit identity is failure, not supported “navigation.”

- [ ] **Step 4: Exit the normally unwound clone and verify individual predicates**

Call `Application.Quit`, require exit code 0 without forced cleanup and no active script/background HTTP. Store exact events/process state in `runs/single/summary.json`. If onInit cannot be reached, Busy remains, Back cannot close, route is automatic, or cleanup needs forced stop, mark the failed predicates and stop before Task 2.

- [ ] **Step 5: Preserved-diff review, not a production commit**

Verify repository diff/hash is unchanged apart from intentional documentation commits. Record the temporary source replacement separately from original deployed/current HEAD. No repository source commit is allowed for this proof.

---

### Task 2: Repeated activation serialization, after single PASS

**Files:**
- Create: `run_repeat.py` under the same disposable root
- Create: `runs/repeat/summary.json` and per-run preserved logs/profile
- Reuse: disposable Task 1 registration/controller and cloned binary only, never an old task's live profile

**Interfaces:**
- Consumes: proven one-activation capability (`runs/single/summary.json` single/close predicates PASS).
- Produces: actual second explicit activation protocol showing no overlap or a FAIL stop.

- [ ] **Step 1: Create two deliberately identified activation events**

Start from a newly initialized root/profile. Record the first explicit root `RunScript` command as `activation-A`. Before the first modal clears, issue a second documented launcher event as a distinct controller activation `activation-B`; do not claim it is merely a repeat keyboard callback. Preserve command/event ID/timestamps in logs. This is an input stressor, not an accepted second modal by default.

- [ ] **Step 2: Observe without guessing serialization**

Require the first modal to reach onInit/controls. Observe whether Kodi serializes B, queues it after A or overlaps it. Use condition waits rather than arbitrary sleeps. For each event preserve script entry/exit, modal entry/exit, native current-window and any Busy refusal.

Accept only actual serialized behavior: each event has one script modal, B cannot overlap A, and one Back for A reaches A’s exit before B (if supported by Kodi) proceeds. If B executes concurrently, opens overlapping GUI or triggers Busy rejection so cannot proceed safely, mark fail and stop.

- [ ] **Step 3: Close only the identified active windows and evaluate repeat predicates**

Drive one Back per actual serialized activation; no more than one per explicit event. Require each close causes that invocation’s one `doModal` exit. No Back itself may create another run. Every per-activation count must be exactly 1/1; no anonymous extra invocation is allowed.

Expected: `activation-A` and `activation-B` are both explicitly accounted; their pmids/script events are serialized without UI overlap. Failure/UNCHECKED counts are retained individually, not averaged into overall success.

- [ ] **Step 4: Verify clean process unloading and source isolation**

After RPC quit require exit 0/unforced cleanup. Record whether cloning cleanup required anything beyond ordinary controller close (if so verdict fails). Confirm temporary offline fixture made zero cloud calls and no user profile/source mutation occurred.

---

### Task 3: Independent saved-evidence verification and gate decision

**Files:**
- Create: `verify_gate0.py` and `gate0-verification.json` under disposable root
- Modify ignored only: `.superpowers/sdd/2026-11-25-kodi-usability-improvements/task-9-live-report.md` with a separate Task 9D result

**Interfaces:**
- Consumes: Task 1 `single` and Task 2 `repeat` actual artifacts; source/temporary diff hashes.
- Produces: explicit `FEASIBILITY PASS` or `FEASIBILITY FAIL`, without implementation/release authority.

- [ ] **Step 1: Write a verifier that inspects each required predicate separately**

Machine-check source/binary provenance; actual root activation, registered script file called; four mandatory constructor args; onInit/controls/panel; canonical public URL (parse query), one Back close, `doModal` returned, no anonymous new invocation; two explicit rapid activations serialized, expected per-event cardinality; no cloud requests; normal process exit and credentials markers absent. Unsupported absent artifacts are UNCHECKED/FAIL, never inferred PASS.

- [ ] **Step 2: Run the verifier, retain failures and stopped branches**

Run `python -B verify_gate0.py` from the disposable root. Expected PASS output only if all Task 1 and Task 2 predicates are actually PASS. Otherwise copy exact first failing predicate/evidence path to the report and judge `FEASIBILITY FAIL`. Preserve setup-discard attempts separately; no old constructor or post-completion partial PASS may substitute for current proof.

- [ ] **Step 3: Confirm the null implementation boundary and report outcome**

Run `git status --short --branch`; it must show no new production/test/CI change from this plan. Only approved documentation commits and the preexisting constructor RED may be present. There is no factory argument commit, script file, addon extension or workflow change yet. No green full-suite claim is made while intentional RED is pending.

Append to Task 9 report: actual one-activation sequence, duplicate activation outcome, exact `FEASIBILITY PASS`/`FEASIBILITY FAIL`, artifact paths, known active-shutdown blocker and the unproven visual limits.

- [ ] **Step 4: Decision gate, not an implementation commit**

If and only if verification is `FEASIBILITY PASS`, request approval for a new detailed implementation plan covering the real data/script owner, root/launcher/config behavior, canonical handoff, package docs and live four-category/InfoWall regression. On FAIL, stop this design and return to the earlier InfoWall-or-pause alternatives. No implementation by inertia.

---

## Self-review recorded design decisions

- Two tasks have separate PASS gates; no repeat run can manufacture evidence to compensate for single failure. A verifier is scoped to measurable actual events/hashes, not textual “success” narration.
- Script entry and mandatory constructor argument count are both proven through real root execution; a constructor-only fake cannot satisfy Task 1.
- Each explicit launcher activation is distinguishable in repeat evidence. Back cannot produce a new anonymous activation; a second valid event has its own explicit ID.
- Search URL is pinned to `plugin://plugin.video.yawspreloaded/` with encoded title and `sort=largest`; canonical URI is not obtained from script args.
- The plan intentionally leaves script-owned client construction, full four-category behavior, InfoWall re-integration, documentation/packaging and general production refactor for a later plan after feasible execution is demonstrated.
