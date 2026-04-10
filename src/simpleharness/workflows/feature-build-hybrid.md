---
name: feature-build-hybrid
phases:
  - project-leader
  - brainstormer
  - plan-writer
  - loop:
      roles: [local-builder, local-reviewer, local-critic]
      max_cycles: 5
      max_critic_rounds: 2
      on_exhaust: skip_and_flag
  - project-leader
max_sessions: 40
---

Hybrid Opus/Local workflow. Opus handles architecture, design, and final review.
Local Ollama models execute plan steps in a quality-gated loop:
builder -> reviewer (pass/fail) -> critic (quality push) -> next step.

The plan-writer produces PLAN.md with explicit numbered steps, each containing:
- Interface contracts and signatures
- Acceptance criteria (specific tests)
- Quality wishlist (FP, complexity, patterns)

The local loop implements each step, with the reviewer checking acceptance criteria
and the critic pushing for quality improvements. Steps that exhaust retries are
flagged for the final project-leader review.
