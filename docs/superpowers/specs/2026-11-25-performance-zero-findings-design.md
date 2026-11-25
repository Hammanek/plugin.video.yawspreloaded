# Performance zero findings — approved design

**Date:** 2026-11-25
**Approval:** User approved the four corrective areas and the conservative design in the preceding conversation.

## Intent

Close the four independently evidenced performance findings without changing the add-on's public routes, settings IDs, authentication/snapshot correctness, external dependencies, or completion semantics. Offline measurements count blocking waits, HTTP requests and JSON loads; no arbitrary latency threshold or new latency promise is introduced.

## Approach considered and selected

The selected approach keeps the existing single executor and session ownership:

1. An on-demand lookup for an already deferred metadata key claims that item before waiting for background capacity. An existing active flight is still shared, and the remainder of the background queue remains bounded.
2. A validated TMDB `/account` result is published only if the captured session, language and current account state still match its guarded resolution. No speculative ID is published before the response or during an obsolete account/language switch.
3. Webshare performs local search-history display, keyboard cancellation and history removal before lazy authentication. Remote search and queue actions still authenticate exactly once per invocation.
4. A single snapshot refresh-state load supplies the warm snapshot, its generation and refresh-due decision to the invocation and post-completion submission. Complete saves use one transactional read/check/update followed by the existing atomic save, not a nested second load.

Adding another executor or unbounded interactive HTTP lane was rejected because it would change ownership, create more simultaneous HTTP and complicate shutdown. A generic time-based cache of Kodi settings was rejected because it could publish stale identity. Byte-buffer replacement was separately rejected: real Kodi 19/21 VFS does not accept `memoryview`.

## Boundaries

- Keep exact account/media/language scoping, generation checks, snapshot completeness and local watched-update behavior.
- Keep response/session closing and queue/terminal completion paths unchanged.
- Public `main.py`, action URLs and resource settings remain untouched.
- Use only the Python standard library and existing requests dependency.
- Every production change follows a failing test; after each task run its focused tests and the complete suite with `python -B -m unittest discover -s tests -v`.
- No release, tag, remote push or merge into local `main` without separate approval.
