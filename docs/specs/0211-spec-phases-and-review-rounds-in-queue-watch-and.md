---
id: SPEC-0211
title: "Spec phases and review rounds in queue watch and listing"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, watch, review]
source_idea: IDEA-078
depends_on: [SPEC-0214]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md). It
covers the minimum display for idea campaigns, from `IDEA-078` (user, 2026-10-25: "review
work status should show up in the wt queue watch activity"). After SPEC-0209, SPEC-0215 and
SPEC-0214, an idea campaign spends part of its life in SPEC_WRITING and SPEC_REVIEW. Today
the dashboard (SPEC-0185) and `wt queue list` show nothing about where it came from, its
spec, or its review round.

Reworked on 2026-10-25 down to this minimum. Per-reviewer chips with several statuses are
deferred: `IDEA-078` stays open for them.

## Goals / Non-goals

**Goals**
- An idea campaign shows where it came from (`from IDEA-NNN`), its spec (or "writing…"),
  the spec phases on the stepper, and its review round with the last spec-review verdict, in
  both `wt queue watch` and `wt queue list`.

**Non-goals**
- Per-reviewer chips and implementation-review status (IDEA-078 remainder).
- Standalone spec reviews, queue events and conductor rows (SPEC-0217).

## Decision

Derive everything from the campaign state and ledger (SPEC-0214's `spec_review` events and
`counters`). No new stored state.

## Design

- **Phases:**
  - `queue.PHASES` = OPENING, SPEC_WRITING, SPEC_REVIEW, IMPLEMENTING, GATING, TRIAGING,
    REVIEWING, CLOSING.
  - `queue.phases_for(state)` drops SPEC_WRITING and SPEC_REVIEW unless `state.kind ==
    "idea"`.
  - The payload's `phase_index` indexes `phases_for(state)` (None for states not in the
    list), and the payload adds `phases`.
  - `test_agentic_dashboard.py`'s `phase_index` assertion moves to `phases_for`.
- **Round:** `queue.spec_round(state, ledger)` = the latest `spec_review` event's `round`
  (R, 0 if none):
  - SPEC_REVIEW → R + 1;
  - SPEC_WRITING → None when no spec exists yet, else R (revising round R);
  - PARKED or WAITING → the rule for `park.resume_to` / `waiting.resume_to`;
  - other states → R if R > 0, else None.
  `queue_payload` and `dashboard_payload` items carry `spec_round`, and idea campaigns also
  carry `idea: {id, heading}` and `last_spec_review: {round, verdict}` (or None).
- **Title:** for `state.kind == "idea"`, `queue._spec_title` reads the spec from
  `state.repo.worktree / state.spec.rel_path` first, then from `state.repo.root /
  state.spec.rel_path` (once merged, the worktree is gone). Other campaigns are unchanged.
- **Rendering:**
  - The active panel title appends `· from IDEA-NNN`.
  - The spec row shows `SPEC-NNNN <title>`, or before a spec exists, `writing… <heading>`,
    cut to 60 characters with `…`.
  - A `spec review` row for idea campaigns shows `r2 · last: revise`, or `r1 · writing`
    before any review.
  - The stepper uses `phases`.
  - The `wt queue list` state cell shows `SPEC_REVIEW r2`, `SPEC_WRITING r1` (revising) or
    plain `SPEC_WRITING`.
  - `render.STATE_STYLES` has entries for SPEC_WRITING (cyan, from SPEC-0209) and
    SPEC_REVIEW (magenta, from SPEC-0214), used here.

## Alternatives considered

- **Per-reviewer chips now** — they diverged in review; deferred to the IDEA-078 remainder.

## Acceptance criteria

- [ ] Idea campaign in SPEC_REVIEW after one revise review (fixture: one `spec_review` event,
      round 1, verdict revise):
  - `spec_round == 2`, and `last_spec_review == {round: 1, verdict: "revise"}`;
  - `wt queue list` shows `SPEC_REVIEW r2`.
- [ ] Idea campaign in SPEC_WRITING:
  - with no spec yet, plain `SPEC_WRITING`, and a spec row of `writing… ` plus the heading cut
    at 60 characters with `…`;
  - revising after round 1, `SPEC_WRITING r1`.
  - When parked with `spec_review_exhausted` (resume_to SPEC_WRITING), `spec_round` is R.
- [ ] Rendering at 80 and 160 columns shows `from IDEA-`, the spec phases on the stepper, the
      `spec review` row, and the spec title read from the worktree file.
- [ ] A spec campaign's `phases` omit the spec phases, and its `phase_index` is unchanged
      from today.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_dashboard_spec_phase.py`, with fixtures built
  through `store` / `schemas.with_defaults` (as in `test_agentic_dashboard.py`). At least one
  test per AC.
- **Manual verification:** none.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Ships through the agentic queue after SPEC-0214. The changes are additive.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
