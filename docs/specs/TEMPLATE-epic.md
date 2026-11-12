---
id: SPEC-NNNN
title: <epic title>
status: draft
owner: user
created: YYYY-MM-DD
kind: epic
# milestone: "M?: <phase>"
# tags: []
# depends_on: []
---

## Context

<The large problem/opportunity this epic addresses. Why it needs many specs, not one.>

## Goals / Non-goals

**Goals**
- …

**Non-goals**
- …

## Decision

<The overall approach at the epic level. The shape of the solution, not the line-by-line.>

## Architecture / cross-cutting design

<Shared data shapes, module boundaries, interfaces, and invariants the child specs must
respect. This is the contract the sub-specs fill in.>

## Breakdown / sub-specs

<The child specs that compose this epic. Each child sets `parent: SPEC-NNNN`. Keep this
checklist current as children are created and completed (the ledger also rolls it up).>

Multi-project fan-out (SPEC-0095/0097): annotate a bullet with `(project: Name)` so
`nw spec seed-children` stamps the right `:PROJECT:` (omit → epic `target_project`). Name must
be a known project (`nw projects --json`). A bullet worded `existing IDEA-NNN — …` (SPEC-0576)
maps to that already-captured idea in any org file: no new idea is seeded (an unknown id fails).

- [ ] Child A (project: Meta-Tools) — <one-line scope>
- [ ] Child B (project: Example) — <one-line scope> (depends_on A)
- [ ] Child C — <same project as epic target_project when annotation omitted>
- [ ] SPEC-???? — <sub-spec C>

Sequencing / dependencies: <the order and why; what can go in parallel.>

## Acceptance criteria

<Feature-level outcomes true when the whole epic is delivered.>

- [ ] …

## Test plan

<INTEGRATION-level: how the assembled feature is verified end-to-end once children land.
Unit-level testing belongs in each child spec.>

- **Integration/e2e tests:** …
- **Manual verification:** …

## Rollout / sequencing

<Order children land in; any phased rollout; how partial delivery behaves.>

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; `uv run pytest` green.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

- <resolve before `accepted`>
