---
id: SPEC-0193
title: "Persona prompts state the spec-change guard rules"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, personas, safety]
source_idea: IDEA-060
depends_on: [SPEC-0186]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-060`). On campaign SPEC-0187-20261025-b735 the implementer put a deviation
into the spec's Design section; the spec-change guard (SPEC-0186) correctly refused it. The
gate-triager persona then told the implementer to revert the whole spec file, wiping the allowed
edits (status, ticks, test record), and the next triage misread the resulting `spec-dod`
failure as a guard-vs-DoD conflict and escalated. Neither persona prompt states the guard's rules.

## Goals / Non-goals

**Goals**
- The implementer prompt states exactly what may change in the spec file and that deviations go
  in `### As shipped`.
- The gate-triager prompt directs a `spec-changed` fix to revert only the flagged hunks, and a
  `spec-dod` fix to be the allowed edits.

**Non-goals**
- Changing the guard itself (SPEC-0186) or the conformance reviewer persona.

## Decision

Document the guard's rules in the two persona templates, and pin them with a test.

## Design

- `src/wt/agentic/personas/implementer.md`, Procedure step 6: the allowed edits are the
  `status:`/`updated:` lines, checkbox ticks anywhere, `### Test record …` and `### As shipped …`
  subsections, and `## Clock Log`; any other change makes close refuse the merge; Design
  deviations go in `### As shipped`.
- `src/wt/agentic/personas/gate_triager.md`, before "Be specific": for `spec-changed`, the
  `fix_brief` reverts ONLY the flagged hunks and keeps the allowed edits, never a whole-file
  revert; for `spec-dod`, the fix is the allowed edits, never "the rules conflict".
- `tests/test_agentic_personas.py::test_personas_state_spec_change_guard_rules`.

## Alternatives considered

- **Enforce in code (auto-restore allowed edits)** — larger change to SPEC-0186; prompt fix is
  the smallest remedy for the observed failure.

## Acceptance criteria

- [x] The implementer template lists the allowed spec edits and says other changes make close refuse the merge.
- [x] The implementer template says deviations go in `### As shipped`, not Design.
- [x] The triager template says to revert only flagged hunks for `spec-changed` and never the whole spec.
- [x] The triager template says `spec-dod` is fixed by the allowed edits, not by "the rules conflict".
- [x] A test pins these statements.

## Test plan

- **Automated tests:** `tests/test_agentic_personas.py::test_personas_state_spec_change_guard_rules`
  asserts the key phrases in both templates.
- **Manual verification:** read the rendered prompts (`wt campaign prompt`) for both personas.
- **Regression guard:** full `uv run pytest -q`.

### Test record (2026-10-25)

Ran `uv run pytest -q` (all passed, 0 failed), `python3 tools/spec_lint.py` (exit 0) and
`uv run wt spec verify SPEC-0193` (ok).

## Rollout / migration

None. Takes effect on the next runner restart (src/wt/agentic changed).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).
