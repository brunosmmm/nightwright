---
id: SPEC-0288
title: "Implementer prompt: targeted tests while iterating, full suite left to gating"
status: accepted
owner: user
created: 2026-10-27
kind: feature
source_idea: IDEA-382
---

## Context

Promoted from IDEA-382. On 2026-10-27 four implementer runs timed out (SPEC-0263 and SPEC-0267
at 30 min, SPEC-0269 and SPEC-0272 at 45 min) with machine load about 4.5 of 8 cores, so it was
not CPU starvation. The full pytest suite is 3000+ tests (5-10 min under parallel campaigns) and
implementers re-ran it several times. The gates already run the full suite after the implementer
returns, and the gate triager feeds failures back, so the implementer's own full runs duplicate
that work.

Today `src/wt/agentic/personas/implementer.md` procedure step 5 says only "Run the project's gate
commands yourself ... until they pass", which invites repeated full runs.

## Goals / Non-goals

**Goals**
- The implementer prompt tells implementers to iterate with targeted tests (the test files they
  touched plus tests of directly related modules).
- It tells them to run the full suite at most once, at the end, or not at all, since gating runs
  it and returns failures.
- The guidance is measurable through implementer run durations.

**Non-goals**
- Enforcing it (no test-command filtering, no timeout changes).
- Changing gates, other personas, or the gate-triager flow.
- Dropping the cheap gates (spec-lint) from the implementer's own checks.

## Decision

Rewrite step 5 of the implementer persona template: while iterating, run targeted tests only; the
full suite at most once at the end (optional); gating is the authority for the full run. Cheap
gates (spec-lint) may still be run freely. Prompt-only change, no code.

## Design

- `src/wt/agentic/personas/implementer.md`, "Procedure" step 5: replace with text that
  (a) says to run the test files touched plus tests of related modules while iterating,
  (b) says to run the full test suite at most once, at the end, or skip it because gating runs
  it after the implementer returns and the triager reports failures, and
  (c) keeps "run the cheap gates (spec lint) yourself".
  Placeholder-free text, so `personas.py:render` (`render(p.template, ctx)`, called from
  `build_prompt`) passes it through unchanged. `Persona.template` reads the file from
  `_TEMPLATES_DIR`, and `PERSONAS["implementer"]` points at `implementer.md`.
- `tests/test_agentic_personas.py`: new test next to `test_personas_state_spec_change_guard_rules`.

**Measuring the effect.** Compare implementer run durations before and after merge with
`wt campaign runs` (per-run duration) or the campaign export: take the implementer-persona runs
from the 20 campaigns before and after, compare median and p90 duration and the share of runs
that ended by timeout. Expect fewer timeouts and a lower p90.

## Alternatives considered

- **Block full-suite commands via the allowlist** - brittle and hides legitimate use; a prompt
  nudge is cheap and reversible.
- **Raise the implementer timeout** - treats the symptom and costs more wall time.

## Acceptance criteria

- [ ] The assembled implementer prompt (`build_prompt("implementer", ctx)`) tells the
      implementer to run targeted tests (touched files plus related modules) while iterating.
- [ ] It tells the implementer to run the full suite at most once, at the end, or not at all.
- [ ] It says gating runs the full suite and returns failures.
- [ ] The guard rules and placeholders of the template are unchanged (existing persona tests
      pass).
- [ ] The measurement method (`wt campaign runs` / export, median and p90) is recorded in this
      spec.

## Test plan

- **Automated tests:** `tests/test_agentic_personas.py::test_implementer_prompt_targeted_tests`
  builds the implementer prompt with `PS.build_prompt` using the existing `_cfg`/`_state` fixtures
  and asserts the three phrases from the ACs are present ("targeted tests", "at most once",
  "gating"). Sabotage: remove the new step-5 text, confirm the test fails, restore it.
- **Manual verification:** `uv run wt campaign prompt` for an implementer run shows the new
  step 5.
- **Regression guard:** `uv run pytest tests/test_agentic_personas.py`, then the full suite once.

## Clock Log

CLOCK-IN: [timestamp]
CLOCK-OUT: [timestamp]

## Rollout / migration

Takes effect on the next implementer run after merge. Revert is a one-file change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).
