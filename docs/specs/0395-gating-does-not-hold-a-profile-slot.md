---
id: SPEC-0395
title: "GATING campaigns do not count against the profile slots"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M10: agentic MVP"
kind: feature
depends_on: [SPEC-0200]
tags: [agentic, queue, slots, gates]
---

## Context

`ACTIVE_STATES` (queue.py) includes `GATING`, and `_active_count`, the tick's `n_busy`, the
watchdog's `_occupied` and `_SlotWatch.count` all count GATING campaigns against the profile
`slots` (balanced: 4). A campaign that is only waiting on or running box gates spends no Claude
tokens, yet it holds a slot and throttles admission of campaigns that would. Box gate
concurrency already has its own bound: the gate slot pools (`gate_slots.py`) and worker slots.

## Goals / Non-goals

**Goals**
- A campaign in `GATING` does not occupy a profile slot for admission, resume or waiting.
- Claude-consuming states (OPENING, SPEC_WRITING, SPEC_REVIEW, IMPLEMENTING, TRIAGING,
  REVIEWING, CLOSING) keep counting.

**Non-goals**
- No new `slots` number and no new gate-concurrency setting (gate pools already bound it).
- `ACTIVE_STATES` itself is unchanged: GATING is still "in flight" for status, brief,
  restart, herdr, export and the stuck/alarm detectors.

## Decision

Add `SLOT_STATES = ACTIVE_STATES - {"GATING"}` and `slot_ids(rows, flying)` in queue.py, and use
them wherever the profile `slots` cap is compared: `_active_count`, `_enter_slot_or_wait`,
`plan_resume`, the tick's `n_busy` and auto-resume `taken`, `queue_payload`'s `busy`,
`events._occupied` and `_SlotWatch.count`. In-flight ids whose campaign is GATING are not
counted either (a box gate flight is not a Claude run).

## Design

- A campaign leaving GATING for a slot state (fix loop back to IMPLEMENTING, TRIAGING,
  REVIEWING, CLOSING) is a normal in-state transition, not an admission, so it may push the
  occupied count above `slots` transiently; it is never sent to WAITING (that would strand a
  started campaign mid-loop). New admissions then wait until the count drops. This matches
  how in-flight campaigns already "always count".
- Resume/recover into GATING never waits for a slot (`to in SLOT_STATES` is false).
- Gate concurrency stays bounded by `gate_slots.acquire` / worker slots; unchanged.

### Gate job failure reasons (GATEJOB-0135/0136, investigated read-only)

Not a recording bug. Both jobs failed because gates failed; `job.json` carries `exit: 1`,
`failing: [...]`, and per-gate `result[].exit_code` / `ok` / `log`; the top-level `error` is
null by design (it is for infrastructure errors). The confusing part is jerome's gate config
using `results.format: pytest` for cargo gates, so `summary.failing` is empty and the summary
says "no pytest summary line". That is a project gate-config issue, outside this spec.

## Alternatives considered

- **Raise `slots`** — rejected by the task: no new number, and it would also admit more
  Claude work than intended.
- **Count GATING at a fraction** — extra knob for no gain; gate pools already bound it.

## Acceptance criteria

- [x] With `slots=1` and one campaign GATING, a second QUEUED campaign is admitted.
- [x] With `slots=1` and one campaign IMPLEMENTING, admission still waits.
- [x] A resume targeting GATING does not go to WAITING when slots are full.
- [x] `events._occupied` / `_SlotWatch.count` ignore GATING rows and GATING in-flight ids.
- [x] `ACTIVE_STATES` still contains GATING.

## Test plan

- **Automated tests:** `tests/test_agentic_gating_slots.py` covers each criterion using the
  fixtures pattern of `tests/test_agentic_waiting.py`.
- **Regression guard:** `tests/test_agentic_waiting.py`, `tests/test_agentic_queue.py` (affected).
- **Manual verification:** `nw queue status` on a box with a GATING campaign shows it active
  but not counted in slots busy.

### As shipped

Existing tests that encoded the old behavior were updated: `test_agentic_waiting.py` (a
WAITING item resuming into GATING no longer blocks backlog admission) and
`test_agentic_queue_restart.py` (restart resumes a GATING campaign directly, never WAITING).

## Rollout / migration

None; behavior changes on the next runner tick.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

## Open questions

None.
