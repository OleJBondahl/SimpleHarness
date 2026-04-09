---
title: Design dev container for safe bypass-permissions execution
workflow: universal
worksite: .
depends_on: []
deliverables:
  - path: docs/dev-container.md
    description: "Refined, implementation-ready dev container spec"
refine_on_deps_complete: false
references:
  - docs/dev-container.md
  - docs/intent.md
---

# Goal

Validate and refine the draft design in `docs/dev-container.md` into a concrete, implementation-ready specification. The draft was written speculatively — review it against the current state of the harness codebase, identify gaps or incorrect assumptions, resolve ambiguities, and produce an updated document that a downstream implementation task can follow without open questions.

The end state: `docs/dev-container.md` is a reliable spec, not a rough draft.

## Success criteria

- [ ] Every file path, line number, and function name referenced in the doc has been verified against the current source — stale references are corrected or removed
- [ ] Any design decisions that have multiple valid approaches are resolved with a clear rationale (not left as "could do X or Y")
- [ ] The doc explicitly lists what is deferred to v0.1 vs what is in scope
- [ ] A reviewer reading only the updated doc could implement the container artifacts without needing to ask clarifying questions
- [ ] No changes to any source code — this task produces documentation only

## Boundaries

- Do not modify any Python source files, shell scripts, or configuration files
- Do not create any container artifacts (Dockerfile, compose.yml, etc.) — that is a downstream task
- Stay on the `feature/dev-container` branch — do not create new branches
- Do not remove sections from the draft wholesale — refine them or mark them as deferred with rationale

## Autonomy

**Pre-authorized (decide and proceed):**
- Restructuring sections of the doc for clarity
- Correcting factual errors (wrong line numbers, renamed functions, etc.)
- Adding missing details discovered during codebase review
- Resolving ambiguous design choices where one option is clearly better given the current codebase

**Must block (stop and write BLOCKED.md):**
- Proposing to drop a major feature area from the spec (e.g., removing multi-worksite parallelism)
- Discovering that the sandbox gating mechanism has fundamentally changed from what the draft assumes
- Any change that would affect the scope of downstream implementation tasks

## Handoff

Downstream tasks (002, 003) consume the updated `docs/dev-container.md` as their primary reference. The doc should be self-contained — no "see conversation notes" or "TBD" sections.

## Notes

The draft doc was written before some recent harness changes. Pay special attention to verifying the sandbox check mechanism and the `config.yaml` structure still match what the doc describes. Also review `docs/intent.md` for alignment on the project's design philosophy around isolation and safety.
