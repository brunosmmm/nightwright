---
id: SPEC-0200
title: "WAITING state: started campaigns wait for a slot ahead of the backlog"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, slots]
source_idea: IDEA-061
depends_on: [SPEC-0194]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-061`. On 2026-10-25, with slots=1, SPEC-0187 parked and its slot went
to SPEC-0188 from the backlog. Then `wt queue resume` put SPEC-0187 straight back into
IMPLEMENTING, and both ran at once. `apply_resume` ignored slots, and SPEC-0180's admission
covers only QUEUED.

The user's definition: slots are for campaigns that are open and delivering (OPENING …
CLOSING). The backlog (QUEUED) is work that has not started and never holds a slot. A started
campaign (worktree, branch, commits, counters, cost, pending brief) that is ready to continue
but has no slot is **WAITING**, first in line for the next slot. PARKED means it needs a
decision and holds no slot.

## Goals / Non-goals

**Goals**
- Resuming (and recovering a `config_invalid` park, SPEC-0194) never takes the queue past
  `slots`: the campaign enters WAITING when no slot is free.
- Admission order: WAITING (priority, then age), then the backlog. A WAITING campaign
  continues at its recorded state; it never re-opens.
- WAITING is visible: `wt queue list` gets its own section, the dashboard gets a section and
  a chip, the event stream gets `waiting` / `admitted`, and the conductor knows it needs no
  action.

**Non-goals**
- Pre-empting a running campaign to free a slot.
- Changing slot counts or profiles.

## Decision

Add state `WAITING` (not active, not terminal) with `state.waiting = {resume_to, since,
reason}`. `_enter_slot_or_wait(cfg, st, to, reason)` is the one way a started campaign
re-enters an active state from outside the loop: it goes to `to` when a slot is free, else
to WAITING. Tick step 0b moves WAITING campaigns into free slots before step 1 admits from
the backlog.

## Design

- **States:** `STATES` gains `WAITING` after `QUEUED`; `ACTIVE_STATES` is unchanged, so
  WAITING holds no slot. `completion.complete_campaign_state` and `render.STATE_STYLES`
  (`bright_blue`) include it.
- **`_enter_slot_or_wait`:** when `to ∈ ACTIVE_STATES` and active ≥ slots, it records
  `waiting` and transitions to WAITING with reason `<reason>_waiting`. Otherwise it drops
  `waiting` and transitions to `to` with `reason`. Callers:
  - `apply_resume` (reason `resumed`); `plan_resume` reports `waits_for_slot`.
  - SPEC-0194's `config_recovered` step 0 (it passes the tick's `busy`/`slots`). A recovery
    that finds no slot now lands in WAITING instead of staying parked.
- **Tick step 0b:** it sorts WAITING by `admission_key` (priority, then age) and, while
  `busy < slots` (and `admit` is set and no stop is requested), transitions each to
  `waiting.resume_to` with reason `slot_free`, row action `continue`. The campaign joins
  `busy` and is planned in the same tick. Park-requested WAITING items are skipped. Step 1
  (backlog admission) sees the updated `busy`.
- **Human commands:** `plan_park` on WAITING is always immediate (nothing is in flight),
  with `resume_to` = its waiting target. `prioritize` and `abandon` accept WAITING.
- **Events (`wt.event.v1`):** a transition to WAITING projects `waiting {from}`; a transition
  out of WAITING projects `admitted {from: WAITING, to}`.
- **Listing / dashboard:** `wt.queue.v1` items carry `waiting`. `wt queue list` shows Active,
  then "Waiting for a slot (N)" (campaign, project, continues at, pri, loops, waiting), then
  Backlog. The dashboard payload gains `waiting` (with `resume_to`, `waiting_s`), plus
  `totals.waiting` and `by_project[*].waiting`; the frame adds a `waiting` chip and a
  "Waiting for a slot" table before the backlog.
- **Conduct skill:** a decision-table row: `waiting` needs no action.

## Alternatives considered

- **Put the resumed campaign back in QUEUED** — it would re-open (a new worktree) or need
  special cases. The user explicitly separated not-started from started work.
- **Refuse `resume` when slots are full** — pushes slot bookkeeping onto the human or the
  conductor.

## Acceptance criteria

- [x] With the only slot busy, resuming a started campaign lands it in WAITING with its
      worktree, counters, cost and pending brief intact; active count stays ≤ slots.
- [x] With a free slot, resume goes straight to the target; resume to QUEUED never waits.
- [x] The next free slot goes to WAITING before QUEUED; WAITING continues at `resume_to`
      without OPENING; ordering is priority then age; no continue when admissions are off.
- [x] WAITING can be parked (immediately), prioritized and abandoned.
- [x] `waiting` / `admitted{from: WAITING}` events; `wt queue list` and the dashboard show
      the Waiting section; SPEC-0194 recovery without a slot goes to WAITING.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_waiting.py` (slots patched to 1):
  - resume under a full slot → WAITING with work intact and the `resumed_waiting` transition;
  - resume with a free slot; resume to QUEUED;
  - WAITING beats an older QUEUED item and continues at GATING (no OPENING);
  - priority order; `admit=False`;
  - park, prioritize and abandon on WAITING;
  - events (`waiting`, `admitted` from WAITING), `wt queue list` and the dashboard section.

  `tests/test_agentic_reexec.py::test_recovery_waits_for_a_free_slot` now expects WAITING.
  `test_render` covers the new state style.
- **Sabotage check:** with the old `apply_resume` and no step 0b, 4 of the 8 tests fail.
- **Regression guard:** full `uv run pytest`.

### Executed (2026-10-25)

- `tests/test_agentic_waiting.py`: 8 passed. Under sabotage, 4 failed; the code was then
  restored.
- Full `uv run pytest`: 1666 passed, 1 skipped; spec_lint exits 0.
- Updated assertions: the prioritize error text (now lists WAITING) and the dashboard
  `by_project` dict (new `waiting` key).

## Rollout / migration

Built on branch `idea-061-waiting` (worktree `/home/bruno/work/wt-idea-061`, rebased onto
SPEC-0194), merged through the conductor. With SPEC-0194 merged, the runner re-execs itself
on this merge. No existing campaign is in WAITING, so there's no data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
