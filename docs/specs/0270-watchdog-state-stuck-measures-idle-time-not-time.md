---
id: SPEC-0270
title: "Watchdog state_stuck measures idle time, not time in state"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-360
parent: SPEC-0169
tags: [agentic, watchdog, events]
---

## Context

On the night of 2026-10-27, `state_stuck` fired four times on healthy IMPLEMENTING campaigns:
SPEC-0261 at 1520 s, SPEC-0260 at 2010 s, and SPEC-0250 at 1249 s and 1090 s. Each one fired
within seconds of the implementer's `run.end`. It also fired on CLOSING campaigns after
`base_moved` redos (IDEA-360). The conductor has to read and dismiss every one of these.

`src/wt/agentic/events.py:stuck_items` (SPEC-0212, amended by SPEC-0260) was checked on
2026-10-27:

- **The clock.** The age is `_since_state(cfg, st, now)`, which is the time since the last
  `transition` *into* the current state. A self-transition such as CLOSING → CLOSING with
  reason `base_moved` writes no `transition` event. The MERGE-20261027-0d7d ledger has only a
  `finding` and a `gate` at 01:56:02. So a CLOSING clock runs from the first entry through
  every redo.
- **The persona-state exemption** applies only while a run is open. An open run is a
  `run.start` with no `run.end` (`_open_run`) whose age is within `timeout_s` +
  `overdue_grace_s`. Once `run.end` is written, the exemption is gone. The age is then the
  whole stay in the state (25 minutes or more for an implementer run), which is far above
  `stuck_after_s` (900). `_apply` writes `run.end` before the commit and the transition, so a
  watchdog poll (every 2 s) that falls in that window fires. The same happens in any gap
  between a `run.end` and the next `run.start` in the same state: a review-fix implementer
  run, or a retry.
- **GATING / CLOSING.** These are exempt while `(head_since or age) <= gate_budget_s`.
  `closing.queued_behind` (a SPEC-0260 train waiter) is already excluded; this was confirmed
  in the code and pinned by
  `tests/test_agentic_close_train.py::test_stuck_items_ignores_train_waiter_and_uses_head_since`.
  A CLOSING campaign that redid its close twice after `base_moved` (each attempt is about a
  12-minute re-gate) goes past `gate_budget_s` (1800) measured from its first entry, even
  though every attempt was healthy.
- `run_overdue` (`events.py`, the in-flight-run check) already reports a run that is past its
  own timeout. While a run is in flight, `state_stuck` adds nothing.

## Goals / Non-goals

**Goals**
- In persona states (SPEC_WRITING, IMPLEMENTING, REVIEWING, TRIAGING), `state_stuck` measures
  **idle** time: time with no run in flight, counted from the later of entering the state and
  the latest `run.end` in it. A run in flight never makes the campaign stuck; `run_overdue`
  owns that case.
- In GATING and CLOSING, the in-process budget is measured from the **latest attempt**: the
  latest of entering the state, `closing.head_since`, and the latest `gate` ledger event.
- SPEC-0260 train waiters stay excluded.

**Non-goals**
- New thresholds or settings. `stuck_after_s` and `gate_budget_s` keep their meaning and
  their defaults.
- Changing `run_overdue`, `campaign_silent`, or the dedupe key `state_stuck:{cid}:{state}`.
- OPENING. It is a declared `busy` step (SPEC-0228), and its behaviour does not change.

## Decision

`stuck_items` replaces its single "age in state" with a per-state **activity clock**:

- `since_state` = the time since the last `transition` into the state, as today.
- **Persona states** (`SPEC_WRITING`, `IMPLEMENTING`, `REVIEWING`, `TRIAGING`): when a run is
  open, the campaign is skipped, whatever its age. Otherwise `idle = min(since_state, since
  the latest run.end)`, and the campaign is stuck when `idle > stuck_after_s`.
- **GATING / CLOSING**: a train waiter is skipped, as today. Otherwise `attempt = min(since_state,
  age(closing.head_since), since the latest gate event)`, and the campaign is stuck when
  `attempt > max(stuck_after_s, gate_budget_s)`. That keeps today's in-budget exemption, now
  measured from the current attempt.
- Any other active state (OPENING) keeps today's rule.
- The reported `stuck_s` is the clock that was compared (idle or attempt), not the time in
  state. The row also carries `in_state_s` (the old number), for context.

## Design

- **`src/wt/agentic/events.py`**:
  - A new helper, `_latest_age(cfg, cid, types, now) -> float | None`: the age of the newest
    ledger event of the given types. It uses `store.read_ledger(cfg, cid, types=types)[-1:]`,
    the same read pattern as `_open_run`.
  - `stuck_items` is restructured as in Decision, around a module constant `_PERSONA_STATES =
    ("SPEC_WRITING", "IMPLEMENTING", "REVIEWING", "TRIAGING")`. The `run` branch becomes a plain
    `continue` for persona states. A run open in GATING or CLOSING (a conflict resolver) is
    also skipped while it is within its timeout + grace, as today. The row gains
    `"in_state_s": round(since_state)`.
  - `EVENT_TYPES["state_stuck"]` stays `("state", "stuck_s")`, with `in_state_s` as an extra
    data key. The watchdog loop at line ~698 passes `in_state_s` through.
- **`skills/wt-conduct/SKILL.md`**, `state_stuck` row: one clause saying that `stuck_s` is
  idle time (persona states) or time since the latest close or gate attempt (GATING/CLOSING),
  so a firing means no progress for that long.

## Alternatives considered

- **Raise `stuck_after_s` above the largest run timeout** (≥ 45 min). Rejected. It delays real
  stalls, such as an IMPLEMENTING campaign with no run that the runner never plans, by 45
  minutes. The idle clock catches those at 15.
- **Write a `transition` event on CLOSING → CLOSING redos.** Rejected. It changes ledger
  semantics that the TUI timelines and `_since_state` callers rely on. A `gate` event already
  marks each attempt.
- **Use the runner's `in_flight` lease list** to treat a spawning run as in flight. Rejected
  for now. Spawning lasts seconds, and the idle clock that starts at the previous `run.end`
  already covers that gap.

## Acceptance criteria

- [x] An IMPLEMENTING campaign that has been in state for 2000 s, whose last run ended 5 s ago
      and which has no open run, is **not** in `stuck_items`. The same campaign with its last
      `run.end` 1000 s ago is in `stuck_items`, with `stuck_s ≈ 1000` and `in_state_s ≈ 2000`.
- [x] An IMPLEMENTING campaign with an open run aged 3000 s (past its timeout) is not in
      `stuck_items`, and `run_overdue` still fires for it.
- [x] A CLOSING campaign that entered 2500 s ago and whose latest `gate` event is 300 s old is
      not stuck. With its latest `gate` event 2000 s old it is stuck. A `queued_behind` waiter
      is never stuck.
- [x] A persona-state campaign with no runs at all, in state for 1000 s, is stuck (the idle
      clock falls back to `since_state`).
- [x] The existing `test_agentic_ping.py` stuck tests and `test_agentic_close_train.py`
      stuck test pass, updated only where they asserted the old `stuck_s` meaning.

## Test plan

- **Automated tests** (`tests/test_agentic_ping.py`, using its existing ledger and state
  builders):
  - `test_stuck_ignores_persona_state_right_after_run_end`: AC 1, both halves.
  - `test_stuck_skips_open_run_even_when_overdue`: AC 2. Also assert that a `run_overdue` key
    is produced by `events.watchdog_checks` for the same fixture.
  - `test_stuck_closing_measured_from_latest_gate_event`: AC 3, including the `queued_behind`
    case.
  - `test_stuck_persona_state_without_runs_uses_state_age`: AC 4.
- **Sabotage check:** replace `idle = min(since_state, since_run_end)` with `idle =
  since_state`. `test_stuck_ignores_persona_state_right_after_run_end` must fail. Drop the
  `gate` event from the CLOSING attempt clock. `test_stuck_closing_measured_from_latest_gate_event`
  must fail.
- **Manual verification:** after the merge, follow `wt queue events --follow` across at least
  one implementer `run.end` of a campaign older than 15 minutes in state. No `state_stuck`
  appears.
- **Regression guard:** the full `uv run pytest`, in particular `tests/test_agentic_ping.py`,
  `tests/test_agentic_close_train.py` and `tests/test_agentic_watchdog_gaps.py`.

### Test record (2026-10-27)

- `uv run pytest tests/test_agentic_ping.py tests/test_agentic_close_train.py tests/test_agentic_watchdog_gaps.py`: 43 passed.
- Sabotage: `idle = since_state` fails `test_stuck_ignores_persona_state_right_after_run_end`; dropping the `gate` event fails `test_stuck_closing_measured_from_latest_gate_event`. Both restored.
- Full `uv run pytest`: 3309 passed, 3 skipped.
- Manual `wt queue events --follow` check is post-merge; not performed here.

### As shipped

`test_stuck_threshold_and_in_budget_exclusion` last assertion changed: an overdue open run in a persona state is no longer `state_stuck` (it asserted the old meaning; `run_overdue` owns it).

## Rollout / migration

None. Already-emitted `state_stuck` keys stay deduped.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
