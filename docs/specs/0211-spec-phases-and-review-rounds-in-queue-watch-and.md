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

Reworked on 2026-10-25 down to this minimum. Of `IDEA-078`, this spec delivers the spec-phase
part: the origin idea, the spec title, the spec phases on the stepper, and the spec-review
round with its last verdict. SPEC-0217 delivers standalone spec reviews in watch. Deferred,
with `IDEA-078` left open for them: per-reviewer chips with several statuses, and
implementation-review (REVIEWING) status per reviewer.

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
  - The payload's `phase_index` is `phases_for(st).index(st["state"])` when the state is in
    that list, else None (QUEUED, WAITING, PARKED, terminal), and the payload adds
    `phases` (the list itself).
  - The dashboard's `_stepper` draws `it["phases"]`, not the module constant, so a spec
    campaign's stepper is unchanged and an idea campaign's shows the spec phases.
  - `test_agentic_dashboard.py`'s `phase_index` assertion moves to `phases_for`.
- **Round:** `queue.spec_round(state, ledger)` = the latest `spec_review` event's `round`
  (R, 0 if none):
  - SPEC_REVIEW → R + 1 (the round under review; 1 for the first review);
  - SPEC_WRITING → R if R > 0 (revising after round R), else None (first write);
  - PARKED or WAITING → the rule for `park.resume_to` / `waiting.resume_to`. So a
    `spec_review_exhausted` park (resume_to SPEC_WRITING) gives R, and a park after accept
    (resume_to IMPLEMENTING or later) falls to the next rule;
  - other states (IMPLEMENTING onwards, terminal) → R if R > 0, else None. A spec campaign
    has no `spec_review` events, so it is always None.
  `queue_payload` and `dashboard_payload` items carry `spec_round`, and idea campaigns also
  carry `idea: {id, heading}` and `last_spec_review: {round, verdict}` (or None).
- **Title:** for `state.kind == "idea"`, `queue._spec_title` reads the spec from
  `state.repo.worktree / state.spec.rel_path` first, then from `state.repo.root /
  state.spec.rel_path` (once merged, the worktree is gone). Other campaigns are unchanged.
- **Rendering:**
  - The active panel title appends `· from IDEA-NNN`.
  - The spec row shows `SPEC-NNNN <title>`, or before a spec exists, `writing… <heading>`.
    The whole cell, prefix included, is at most 60 characters: longer text is cut to 59
    plus `…`.
  - A `spec review` row, for idea campaigns only, is built from the state, `spec_round` and
    `last_spec_review` (so it never disagrees with `spec_round`):
    - SPEC_WRITING, `spec_round` None → `writing`;
    - SPEC_WRITING, `spec_round` R → `r<R> · revising · last: <verdict>`;
    - SPEC_REVIEW → `r<spec_round> · reviewing`, plus ` · last: <verdict>` when a previous
      review exists (so the first review is `r1 · reviewing`);
    - any other state with `spec_round` R → `r<R> · last: <verdict>`;
    - otherwise the row is omitted.
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
- [ ] Idea campaign in its first SPEC_REVIEW (no `spec_review` event): `spec_round == 1`,
      `last_spec_review` is None, the row reads `r1 · reviewing`, and the list shows
      `SPEC_REVIEW r1`.
- [ ] Other states: an idea campaign in IMPLEMENTING after accept at round 2 has
      `spec_round == 2` and the row `r2 · last: approve`; the same campaign parked with
      resume_to IMPLEMENTING has `spec_round == 2`; a spec campaign in any state has
      `spec_round` None and no `spec review` row.
- [ ] Title: with the spec file present in the worktree, the title is read from it; with the
      worktree removed (merged), it is read from `repo.root`; with neither, the row falls
      back to the spec id alone.
- [ ] The spec row cell, prefix included, is at most 60 characters for a 100-character
      heading, and ends in `…`.
- [ ] Rendering at 80 and 160 columns shows `from IDEA-`, the spec phases on the stepper
      (drawn from `it["phases"]`), and the `spec review` row.
- [ ] A spec campaign's `phases` omit the spec phases, its `phase_index` is unchanged
      from today, and its stepper renders as today.
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
- [ ] Test plan executed; automated tests pass (`uv run pytest`). (No manual steps.)
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
