---
id: SPEC-0462
title: Triage a diverging campaign before another implement round
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [agentic, queue]
depends_on: [SPEC-0247]
source_idea: IDEA-1027
---

## Context

[SPEC-0247](./0247-divergence-detection-in-the-implementation-loop.md) detects a fix
loop that is not converging and parks the campaign `diverging`. The conductor skill
then dispatches its own reader to pick continue, rework, or escalate. That reader is
not a queue step, it has no cap, and it is not the same comparison twice.

IDEA-1019 parked `diverging` on 2026-11-07. The latest blockers were new requirements
the spec does not state. Another implement round would not have fixed that. The
detector was right to stop. The next step should be a comparison, not a guess.

## Goals / Non-goals

**Goals**
- A trip goes to one read-only persona before anyone spends another implement round.
- That persona can grant one extra round, recommend a spec rework, or escalate.
- A second extra round, and a second unusable verdict, escalate.
- The escalation does not answer itself.

**Non-goals**
- Replacing the detector. SPEC-0247 still decides that the loop tripped.
- Triaging the spec-review loop. SPEC-0247 left that out on purpose.
- Migrating campaigns already parked `diverging`. They stay on the old conductor row.
- Having the queue edit or supersede a spec. Rework is a recommendation.

## Decision

`DIVERGENCE_TRIAGE` is a new active state. It holds a slot. It runs
`divergence_triager` once. `TRIAGING` stays the gate triager. `PARKED` still runs
no persona.

The detector is unchanged. `_finish_fix` appends `loop_divergence`, keeps
`pending_brief`, stores the payload on `state["divergence"]`, and enters
`DIVERGENCE_TRIAGE`.

The verdict labels each latest blocker `impl_miss`, `spec_silent`, or
`spec_contradicts`, and picks one of:

- `extra_round`, only when `counters.divergence_extras` is 0. The campaign returns
  to `IMPLEMENTING` with the held brief and the counter increments. That transition
  (`divergence_extra`) cuts the detector's window, the same way a resume of a
  `diverging` park does, so the extra round is judged on what it produces.
- `rework`. Park `diverging_rework`. The conductor abandons and the project's
  steward supersedes the spec. The queue does not write it.
- `escalate`, also the result of a second extra-round pick and of a second unusable
  verdict. Park `human_decision` with `escalated_from: diverging` (so a later resume
  still resets the detector's rounds) and file an escalation on topic `scope` with
  no recommendation. `scope` is human-only, so it does not time out.

`--reset-loops` does not refund `divergence_extras`. A new campaign starts at 0.
One malformed verdict retries in the same state (`divergence_retries`, cap 1).

## Design

The persona is read-only, same file tools as the other read-only Cursor personas
(SPEC-0456). Builtin profiles give it a small budget (`balanced` and `thorough`:
sonnet, medium, $0.50; `frugal`: haiku, medium, $0.25), `dontAsk`. The mixed model
set sends it to Cursor Grok 4.7, with the reviewers.

The prompt gets the spec, the diff, the pending brief, and a JSON block of the
trip (`signals`, `loop`, `rounds`, `extra_rounds_used`, `extra_rounds_cap`).

A cost cap during the state parks `cost_cap` and resumes back to it. A failed run
resumes to it. The step kind is `divergence`, gated like the other persona steps.

The conductor row `parked.diverging` is now the leftover park (a campaign parked
before this spec, or an escalation that failed to file). `parked.diverging_rework`
is abandon, then `/nw-rework`, with the triager's items as constraints.

## Alternatives considered

- Reuse `TRIAGING`. That state means the gate triager. A diverging review would be
  sent to the wrong persona.
- Stay in `PARKED` and let the conductor dispatch a reader. That is the rung this
  spec replaces, and a park holds no slot and runs no persona.
- Auto-apply rework. The queue would be writing a spec. The steward already owns
  that, with the findings as constraints.

## Acceptance criteria

- [x] A detector trip enters `DIVERGENCE_TRIAGE` with the brief still held and a
      `loop_divergence` event. It does not park `diverging`.
- [x] `extra_round` on the first trip returns to `IMPLEMENTING`, increments
      `divergence_extras`, and the following rounds are a fresh detector window.
- [x] A second `extra_round` escalates.
- [x] `rework` parks `diverging_rework` and files no escalation.
- [x] `escalate`, and a second unusable verdict, park `human_decision` with
      `escalated_from: diverging` and an open escalation on topic `scope` with no
      recommendation and no auto-answer.
- [x] Campaigns already parked `diverging` are not rewritten.

## Test plan

- `tests/test_agentic_divergence.py`: the round cut, `decide_divergence`, the
  recurrence end-to-end (one extra round, then merge), the gate-repeat rework park,
  an escalate verdict, and two bad verdicts.
- `tests/test_skills.py::test_conduct_skill_has_diverging_row_spec_0247` still
  matches the leftover row.
- `tests/test_model_sets.py` covers the mixed-set binding.
- `tests/test_docs_two_pillars.py` covers the new state and park reason in the
  product pages.
- Affected tests via `python3 tools/affected_tests.py --run uv run pytest -q`, plus
  `python3 tools/spec_lint.py`.

## Rollout / migration

No data move. A campaign already parked `diverging` (including IDEA-1019) stays
parked. The new state applies to the next trip. The queue runner loads this code
when it starts, so a runner that is already up keeps the old park until it is
restarted.

## Definition of done

- [x] Acceptance criteria checked and true.
- [x] Test plan executed. `tests/test_agentic_divergence.py` (66), the persona
      registry, the profile golden, the spec-phase stepper, the docs pillar
      pages, and the conductor-skill row. `spec_lint` after the ledger write.
      Twelve unrelated failures also fail on main (dashboard width, campaign
      runs, herdr follow, spec-review list).
- [x] Spec body matches what shipped.
- [x] Ledger regenerated and `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
