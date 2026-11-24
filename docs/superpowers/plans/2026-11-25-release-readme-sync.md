# Release README Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make README synchronization mandatory for every release and correct the currently stale version badge.

**Architecture:** Add a root `AGENTS.md`, which coding agents read automatically, containing a concise mandatory release checklist. Keep README as the user-facing source for current version, features, setup, and installation information.

**Tech Stack:** Markdown, Git, Python standard library for verification.

**Spec:** Approved short design in the 2026-11-25 conversation: create project-level release instructions, update the README badge to 1.3.15, commit, and push.

## Global Constraints

- README must be updated before every new release.
- The README version badge must match `addon.xml`.
- Feature descriptions and setup instructions must reflect changes since the previous release.
- Do not change plugin behavior or create another release as part of this documentation update.

## Review Focus

- A future version bump changes only `addon.xml`; verification must compare it with the README badge.
- A future release changes features or setup; the checklist must explicitly require reviewing those sections.
- Project instructions placed outside the repository root may be missed; use root `AGENTS.md`.
- A release prepared before documentation changes would retain stale details; the checklist must require README updates before tagging and publishing.

---

### Task 1: Add the release documentation rule and synchronize README

**Files:**
- Create: `AGENTS.md`
- Modify: `README.md:3`
- Create: `docs/superpowers/plans/2026-11-25-release-readme-sync.md`

**Interfaces:**
- Consumes: The `version` attribute from the root element of `addon.xml`.
- Produces: Root project instructions requiring README review before every release.

- [ ] **Step 1: Confirm the current mismatch**

Read `addon.xml` and `README.md`. Expected: `addon.xml` reports `1.3.15`, while the README badge reports `1.3.11`; root `AGENTS.md` does not exist.

- [ ] **Step 2: Create the mandatory release checklist in `AGENTS.md`**

Require, before every release: updating the README version badge from `addon.xml`, reviewing feature descriptions against changes since the previous release, reviewing setup and installation instructions, and publishing only after these checks. State that a release with stale README information is incomplete.

- [ ] **Step 3: Correct the current README badge**

Change the version badge from `verze-1.3.11-blue.svg` to `verze-1.3.15-blue.svg`; leave the installation link pointing to `releases/latest`.

- [ ] **Step 4: Verify documentation and diff**

Run:

```bash
python -c "import pathlib, xml.etree.ElementTree as ET; v=ET.parse('addon.xml').getroot().attrib['version']; r=pathlib.Path('README.md').read_text(encoding='utf-8'); a=pathlib.Path('AGENTS.md').read_text(encoding='utf-8'); assert f'verze-{v}-blue.svg' in r; assert 'README.md' in a and 'release' in a.lower(); assert 'feature' in a.lower() and 'setup' in a.lower() and 'installation' in a.lower(); print(f'README and release instructions OK: {v}')"
git diff --check
```

Expected: `README and release instructions OK: 1.3.15`; `git diff --check` produces no errors.

- [ ] **Step 5: Review, commit, and push**

Review the complete diff to ensure plugin code and release artifacts are unchanged. Commit the instructions, README correction, and this plan with message `Document release README update requirement`, then push `main` to `origin` and verify local and remote hashes match.
