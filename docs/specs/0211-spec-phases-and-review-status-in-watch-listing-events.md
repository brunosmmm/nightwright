---
id: SPEC-0211
title: "Spec phases and review status in queue watch, listing and events"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, watch, review]
source_idea: IDEA-078
depends_on: [SPEC-0210]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), the last
of the four specs for "unwritten specs are part of the queue" (IDEA-063). It also delivers
`IDEA-078` (user, 2026-10-25: "review work status should show up in the wt queue watch
activity").

After SPEC-0209 and SPEC-0210, idea campaigns spend part of their life in SPEC_WRITING,
SPEC_REVIEW and SPEC_REVISION. The dashboard (SPEC-0185, `queue.dashboard_payload` /
`cli_queue._dashboard`) shows a running persona as one generic activity line. It keeps only
the last verdict of whichever persona ran last, with no findings counts. `wt spec review` runs
outside campaigns are not shown at all.

## Goals / Non-goals

**Goals**
- Idea campaigns read clearly in `wt queue watch` and `wt queue list`:
  - which idea they came from, and the spec they are writing;
  - the spec phases on the phase stepper;
  - the review round and its result.
- Every active panel has a **reviews** row: one chip per review in the current campaign
  (spec_reviewer rounds, conformance_reviewer and future reviewers), each running (with
  elapsed time), passed or failed, with must_fix/major counts.
- A **Spec reviews** dashboard section shows `wt spec review` runs that are in flight or
  happened in the last 24 hours.
- Queue events for the spec phases.

**Non-goals**
- A TUI tab (IDEA-037); notifications (IDEA-059).

## Decision

Derive everything from what SPEC-0209/0210 already record (ledger events `spec_written`,
`spec_review`, persona `run.start`/`run.end`, and the review store), plus a small journal for
standalone spec reviews. No new state is kept for display.

## Design

- **Phases:** `queue.PHASES` becomes OPENING, SPEC_WRITING, SPEC_REVIEW, SPEC_REVISION,
  IMPLEMENTING, GATING, TRIAGING, REVIEWING, CLOSING. `_stepper` omits the three spec phases
  for campaigns whose `kind != "idea"`. `render.STATE_STYLES` gives SPEC_REVIEW and
  SPEC_REVISION styles (SPEC_WRITING was styled in 0209).
- **Payload (`wt.queue.dashboard.v1`, additive):**
  - each active, waiting and attention item gains `idea: {id, heading}` for idea campaigns,
    and `spec_review: {round, verdict, counts}` from the latest `spec_review` event;
  - each active item gains `reviews`: a list of `{persona, run_id, round, status:
    running|pass|fail|invalid, counts: {must_fix, major}, elapsed_s}`, built from the
    campaign's ledger. That covers spec_reviewer runs (verdict from the `spec_review` event:
    approve → pass, revise/split → fail) and every `*_reviewer` persona run (verdict
    `pass`/`approve` → pass, else fail), in run order;
  - a top-level `spec_reviews` list: standalone review runs from
    `<agentic>/spec-reviews.jsonl` (below) that are in flight, or ended within
    `recent_hours`. Fields: `{spec_id, status: running|approve|revise|split|failed, counts,
    started_at, duration_s, cost_usd}`;
  - `totals.reviews_running`.
- **Standalone review journal:** `spec_review.run_spec_review`, when called from
  `wt spec review`, appends `start` and `end` records (`wt.spec.review.run.v1`: ts, spec_id,
  run_dir, and on end verdict, counts, cost, duration, or error) to
  `<agentic>/spec-reviews.jsonl` with one O_APPEND write each. A `start` with no `end` after
  the run timeout counts as `failed`.
- **Rendering (`cli_queue`):**
  - The active panel title appends `· from IDEA-NNN` for idea campaigns. The spec row shows
    `SPEC-NNNN <title>`, or `writing… (IDEA heading)` before the spec exists.
  - A new **reviews** row shows chips such as `spec r1 ✗ 2 must_fix · spec r2 ✓ ·
    conformance ▶ 1m12s`; failures are red, passes green, running cyan.
  - The **Spec reviews** table (spec, status, findings, cost, when) appears after Needs
    attention when non-empty.
  - The header gains a `reviews` chip.
  - In `wt queue list`, the state cell shows `SPEC_REVIEW r2` for spec phases.
- **Events (`wt.event.v1`):** `spec_written {spec_id}` from the ledger event of the same name;
  `spec_reviewed {spec_id, verdict, round}` from `spec_review`; `spec_accepted {spec_id}` from
  the `spec_accepted` transition. All three are added to `events.EVENT_TYPES` and the
  projection.
- **Conduct skill:** the decision table gets rows for `spec_split`, `spec_review_exhausted`
  and `spec_write_blocked` parks:
  - split → rework or split the idea through the steward;
  - exhausted → read the reviews and resume with a note, or escalate;
  - blocked → ask the project's steward.
  `spec_written`, `spec_reviewed` and `spec_accepted` need no action.

## Alternatives considered

- **Store display state on the campaign** — duplicates the ledger and drifts.
- **Show only the latest review** — the user asked for review work status, and fan-out
  (SPEC-0201) runs several reviewers per loop.

## Acceptance criteria

- [ ] For an idea campaign in SPEC_REVIEW round 2 (fixture: a written spec, one revise review
      and one running reviewer), the payload has `idea`, `spec_review.round == 2` and
      `reviews` with two spec_reviewer entries (fail with counts, then running with elapsed
      time). The rendered panel at 80 and 160 columns shows `from IDEA-`, the spec id and the
      chips, and the stepper includes the spec phases.
- [ ] A spec campaign's stepper omits the spec phases. Its reviews row lists its
      conformance_reviewer run with pass/fail and counts.
- [ ] `wt spec review` (FakeRunner) appends start/end records. While it runs, the dashboard's
      Spec reviews section shows it as running; after it ends, it shows the verdict and
      counts; the header's reviews chip counts the running ones.
- [ ] `wt queue list` shows `SPEC_REVIEW r2`.
- [ ] `spec_written`, `spec_reviewed` and `spec_accepted` queue events are projected exactly
      once from an idea campaign's ledger.
- [ ] The conduct skill names the three new park reasons.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_dashboard_spec_phase.py`, with fixtures written
  through `store` / `schemas.with_defaults` (as in `test_agentic_dashboard.py`): payload and
  rendering for the idea campaign and the spec campaign; the standalone-review journal via
  `wt spec review` with a slow FakeRunner (dashboard sampled during the run and after);
  listing; event projection (`events.sync` twice → no duplicates); the skill grep.
- **Regression guard:** full `uv run pytest`; `test_agentic_dashboard.py` keeps passing.

## Rollout / migration

Ships through the agentic queue after SPEC-0210. All display changes are additive; no data
migration.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
