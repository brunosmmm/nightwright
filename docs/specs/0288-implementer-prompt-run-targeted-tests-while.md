---
id: SPEC-0288
title: "Implementer prompt: targeted tests while iterating, full suite left to gating"
status: accepted
owner: user
created: 2026-10-27
kind: feature
source_idea: IDEA-382
updated: 2026-10-27
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
- Dropping the cheap gates (for wt itself: spec-lint) from the implementer's own checks.

## Decision

Rewrite step 5 of the implementer persona template: while iterating, run targeted tests only; the
full suite at most once at the end (optional); gating is the authority for the full run, so the
gate's full-suite run satisfies the "no regressions" / "tests passing" part of the AGENTS.md
Definition of done. An implementer may therefore set `status: done` after targeted tests pass and
leave the full-suite confirmation to gating (a gate failure comes back through the triager). Cheap
gates (for wt itself: spec-lint) may still be run freely. Prompt-only change, no code.

## Design

- `src/wt/agentic/personas/implementer.md`, "Procedure" step 5: replace with text that
  (a) says to run the test files touched plus tests of related modules while iterating,
  (b) says to run the full test suite at most once, at the end, or skip it because gating runs
  it after the implementer returns and the triager reports failures, and
  (c) keeps "run the cheap gates yourself" in generic wording (the persona serves every project
  that uses wt campaigns), naming spec-lint only in the "for wt itself:" style step 7 already
  uses.
  Step 7 gets one added sentence: the gate's full-suite run counts as the no-regressions check,
  so skipping your own full run does not block `status: done`; the `### Test record` should say
  the full suite was left to gating. This settles the AGENTS.md DoD conflict: the persona
  tells implementers to read AGENTS.md as the contract, and that contract requires passing tests
  and no regressions before `done`; the prompt now says gating supplies that evidence.
  Placeholder-free text, so `personas.py:render` (`render(p.template, ctx)`, called from
  `build_prompt`) passes it through unchanged. `Persona.template` reads the file from
  `_TEMPLATES_DIR`, and `PERSONAS["implementer"]` points at `implementer.md`.
- `tests/test_agentic_personas.py`: new test next to `test_personas_state_spec_change_guard_rules`.

**Measuring the effect (post-merge follow-up, not a gate for `done`).** Compare implementer run
durations before and after merge with `wt campaign runs` (per-run duration) or the campaign
export: take the implementer-persona runs from the 20 campaigns before and after, compare median
and p90 duration and the share of runs that ended by timeout. Expect fewer timeouts and a lower
p90. The implementer records the baseline numbers (20 campaigns before the change) in an
`### As shipped` note; the after-numbers are filled in by a later follow-up once enough campaigns
have run.

## Alternatives considered

- **Block full-suite commands via the allowlist** - brittle and hides legitimate use; a prompt
  nudge is cheap and reversible.
- **Raise the implementer timeout** - treats the symptom and costs more wall time.

## Acceptance criteria

- [ ] The assembled implementer prompt (`build_prompt("implementer", ctx)`) tells the
      implementer to run targeted tests (touched files plus related modules) while iterating.
- [ ] It tells the implementer to run the full suite at most once, at the end, or not at all.
- [ ] It says gating runs the full suite and returns failures.
- [ ] It says the gate's full-suite run satisfies the no-regressions check, so `status: done` may
      be set after targeted tests pass without a full run of the implementer's own.
- [ ] The cheap-gates sentence is project-generic: spec-lint appears only as a "for wt itself"
      example, not as the instruction.
- [ ] The guard rules and placeholders of the template are unchanged (existing persona tests
      pass).
- [ ] The `### As shipped` note records the baseline implementer run durations (median, p90,
      timeout share) from the 20 campaigns before the change, taken with `wt campaign runs` /
      export.

## Test plan

- **Automated tests:** `tests/test_agentic_personas.py::test_implementer_prompt_targeted_tests`
  builds the context with `PS.build_context(_cfg(tmp_path), _state(tmp_path), _project_cfg())`
  (as `test_build_prompt_is_deterministic_and_complete` does), then `PS.build_prompt("implementer",
  ctx)`, and asserts longer phrases pinned from the new text, not single words: one for AC1
  naming targeted tests on the touched files plus related modules; one for AC2 ("at most once"
  with "at the end"); one clause for AC3 naming gating plus the full suite and returned
  failures; one for the DoD AC saying the gate's full-suite run satisfies the no-regressions
  check. It also asserts the cheap-gates line does not instruct on `spec_lint` outside a "for wt
  itself" clause. The exact phrases are fixed when the step-5 text is written and quoted in the
  test. Sabotage: remove the new step-5 text, confirm the test fails, restore it.
- **Manual verification:** `uv run wt campaign prompt` for an implementer run shows the new
  step 5.
- **Regression guard:** `uv run pytest tests/test_agentic_personas.py`, then the full suite once.

## Clock Log

## Rollout / migration

Takes effect on the next implementer run after merge. Revert is a one-file change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).
