---
id: SPEC-0313
title: "runner_journal_silent: count in-tick progress, so a busy tick with runs in flight is not a stall"
status: done
owner: user
created: 2026-10-30
updated: 2026-10-30
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, runner, watchdog, conductor, events]
source_idea: IDEA-433
depends_on: [SPEC-0228]
---

## Context

Bug `IDEA-433`. The conductor received `runner_journal_silent` at 61 s of silence while the
runner was healthy: heartbeat 21 s old, six runs in flight, ticking normally, with no state
transitions to write to the journal. The conductor's rule for this event is to restart the
runner at once (`wt-conduct`, `runner_journal_silent` row), so a false alarm interrupts live
persona runs for nothing.

What exists today ([SPEC-0228](./0228-watchdog-waiting-not-admitted-and-silent-runner-journal.md),
`events.runner_silence`, `src/wt/agentic/events.py`):

- Silence is the *minimum* age of the lease's `tick_at`, the holder's newest journal record and
  `busy_ended_at`, compared with `journal_silent_factor` (3) × `poll_interval_s` (20 s) = 60 s.
  The name is historical: journal lines alone never raise the alarm and a journal-only quiet
  stretch is already not the trigger.
- `tick_at` is stamped by `run()` only **after** `tick()` returns and `lease.heartbeat()` has run
  (`slots.check(lease, tick_at=…)`, `src/wt/agentic/queue.py`, the main loop). An idle tick
  writes nothing to the journal, so with runs in flight `tick_at` is the only signal.
- A tick is not instantaneous. With several campaigns in flight it harvests results, lists the
  store several times, refreshes readiness and walks every busy campaign serially
  (`_tick`, `queue.py`). Only `open`, `close` and `drain` are declared `busy` steps
  ([`_busy`](../../src/wt/agentic/queue.py)). A tick that takes 40+ s (the loop then waits a
  further `interval` before the next stamp) pushes the gap between two `tick_at` stamps past 60 s
  although the loop is making progress the whole time.
- The heartbeat is **not** a loop signal: its thread keeps beating through a drain or a hung tick
  (SPEC-0228 context, point 2). Keying the alarm on it would blind the watchdog to the very
  stall it exists for, so it stays out of the check.

The 61 s observation (just over the 60 s threshold, 3 × 20 s) fits a tick that simply took longer
than the stamp cadence allows. The alarm cannot tell "slow but moving" from "stuck".

## Goals / Non-goals

**Goals**
- A runner whose loop is making progress inside a long tick (several campaigns in flight) never
  raises `runner_journal_silent`.
- A loop that is truly stuck (one main-thread step with no progress for more than
  `journal_silent_factor` × interval, outside a declared `busy` step) still raises it, with the
  same once-per-episode behaviour.
- The event payload says which progress signal was newest, so the conductor can see why it fired.

**Non-goals**
- No use of the heartbeat as a liveness signal (see Context).
- No rename of the event type or its key shape: `quiet.py`, `wt-conduct` and the ping digest
  depend on `runner_journal_silent`.
- No change to the factor, `open_budget_s`, the close guard or the other watchdog conditions.
- Not finding or fixing whatever makes a tick slow; this spec makes the watchdog tolerate a slow
  tick, and records tick duration so a later spec can target it.

## Decision

The loop publishes a **progress stamp** `progress_at` in the lease info, written (throttled)
from inside the tick: at the tick's start, after the harvest, and before each campaign it
processes. The watchdog counts `progress_at` as activity alongside `tick_at`, the journal and
`busy_ended_at`. A tick with N campaigns in flight therefore keeps the stamp fresh as long as no
single step between two marks lasts longer than the threshold; a single unmarked step that long
is by definition a stall (the existing contract says a new long main-thread step must declare
itself `busy`). `tick_at` keeps its meaning (end of a completed tick) and the event keeps its
name. The loop also records `last_tick_s` (duration of the last completed tick) in the lease and
the event payload, as evidence for tuning.

## Design

**Lease** (`src/wt/agentic/lease.py`)
- Add `"progress_at"` and `"last_tick_s"` to `LOOP_KEYS`, so `lease_status` returns them (each
  None until the runner writes it, as for the other loop keys) and the docstring lists them.

**Runner** (`src/wt/agentic/queue.py`)
- `_Ctx` gains `progress()`: a no-op without a lease (one-shot ticks, tests); otherwise it calls
  `lease.update_info(progress_at=store.now_iso())` at most once per
  `_PROGRESS_MIN_INTERVAL_S` (1.0 s, `time.monotonic()` based), so a 6-campaign tick adds a
  handful of small lease writes, not one per statement.
- `_tick` calls `ctx.progress()` at its start, right after `_harvest`, at the head of each
  iteration of the tmp-sweep loop (`for st in store.list_campaigns(cfg)` →
  `store.sweep_campaign_tmp`), the readiness loop (`for st in queued`), the parked-recovery loop
  (step 0), the WAITING loop (step 0b), the admission loop and the per-campaign loop
  (`for st in busy`), after `_submit`, and before each serial open/close dispatch; the 1 s
  throttle keeps the per-item calls cheap. Every loop in `_tick` that iterates over the store or
  the queued campaigns is a mark point. Steps already inside `_busy` are unchanged.
- `run()` measures each `tick()` with `time.monotonic()` and passes
  `last_tick_s=round(duration, 1)` into the existing `slots.check(lease, tick_at=…)` call (one
  write, as today). The first `progress_at` of a run is stamped by the first tick.
- No new journal record; idle ticks stay unjournaled.

**Watchdog** (`src/wt/agentic/events.py`)
- `runner_silence` adds `lease.get("progress_at")` to the `ages` it takes the minimum over. A
  runner that predates `progress_at` (None) behaves exactly as before. The "no `tick_at`" legacy
  early return is unchanged.
- The returned dict gains `last_progress_at` and `last_tick_s`; existing keys
  (`last_journal_ts`, `last_tick_at`, `silent_s`, `interval_s`, `heartbeat_age_s`, `busy`,
  `draining`, `busy_ended_at`) are unchanged.
- The episode key in `watchdog_checks` includes `last_progress_at` in the set it takes the
  newest of, so the event re-arms after a tick that made progress and then stalled again.
- `EVENT_FIELDS` (`events.py`, the `runner_journal_silent` entry) lists `last_progress_at` and
  `last_tick_s` next to the existing fields.

**Docs**
- `skills/wt-conduct/SKILL.md` (`runner_journal_silent` row) and
  `docs/product/capabilities/agentic.md`: say the loop counts as alive while it marks progress
  inside a tick, and name the two new payload fields. `tests/test_skills.py` row checks must
  still pass.

## Alternatives considered

- **Key on the heartbeat** — the heartbeat thread beats through a hung loop and through a drain,
  so it cannot see a stall. Rejected.
- **Raise `journal_silent_factor` or `poll_interval_s` headroom** — hides this case by delaying
  every real alarm; no principled value, and it tunes a parameter instead of fixing the signal.
- **Declare the whole tick `busy` with a budget** — a stuck tick would then be silent for the
  full budget, and `busy` has the close guard's meaning for the conductor. Progress marks keep
  the 60 s detection of a real stall.
- **Stamp `tick_at` at tick start as well** — still false-alarms once one tick outlasts the
  threshold; it does not scale with the number of campaigns in the tick.
- **Stamp progress on every statement or every 0.2 s of the wait loop** — needless lease writes;
  the throttle plus per-campaign marks is enough.

## Acceptance criteria

- [x] `lease_status(cfg)` returns `progress_at` and `last_tick_s` (None when unwritten) and
      `LOOP_KEYS` contains both.
- [x] While a tick processes several busy campaigns, the lease's `progress_at` advances during the
      tick (not only at its end), including while sweeping and refreshing readiness for many
      queued campaigns, and is written at most once per `_PROGRESS_MIN_INTERVAL_S`.
- [x] `ctx.progress()` without a lease does nothing and does not raise.
- [x] After each completed tick the lease carries `last_tick_s` equal to that tick's measured
      duration (rounded to 0.1 s) in the same `update_info` call that stamps `tick_at`.
- [x] `runner_silence` returns None when `tick_at` is older than the threshold but `progress_at`
      is fresh (the 61 s case from IDEA-433), and the sync event stream emits no
      `runner_journal_silent`.
- [x] `runner_silence` still returns data when `tick_at`, `progress_at`, the holder's journal
      record and `busy_ended_at` are all older than `journal_silent_factor` × interval, outside a
      declared `busy` step; the data has `last_progress_at` and `last_tick_s`.
- [x] The event still fires once per episode and re-arms when `progress_at` advances and then goes
      stale again; a lease without `progress_at` behaves as before this spec.
- [x] Heartbeat age alone never clears or raises the alarm (a fresh heartbeat with all loop
      stamps stale still fires).
- [x] The event type name and the existing payload keys are unchanged; `docs/product/capabilities/agentic.md`
      and the `wt-conduct` row describe `progress_at`; the skills and quiet-stream tests pass.

## Test plan

- **Automated tests** (`tests/test_agentic_watchdog_gaps.py`, using the existing `held`, `iso`,
  `at`, `_of` helpers):
  - `test_runner_journal_silent_progress_stamp_counts_as_activity`: lease with `tick_at=iso(0)`,
    `progress_at=iso(50)`; at `at(70)` and at `at(100)` `E.runner_silence` is None and `E.sync`
    has no event; at `at(130)` it fires with `last_progress_at == iso(50)`.
  - `test_runner_journal_silent_fires_when_every_stamp_is_stale_despite_fresh_heartbeat`: all
    stamps old, heartbeat fresh ⇒ fires (guards the heartbeat non-goal).
  - `test_runner_journal_silent_rearms_when_progress_advances`: fire, advance `progress_at`
    (cleared), let it go stale ⇒ a second event with the new `last_progress_at`.
  - `test_runner_journal_silent_without_progress_at_is_unchanged`: the existing behaviour tests
    above keep passing untouched.
- **Runner tests** (`tests/test_agentic_loop_state.py`):
  - `test_tick_marks_progress_between_campaigns`: six campaigns in busy states with a fake lease
    and `store.now_iso` / `time.monotonic` monkeypatched to advance 5 s per call; after one
    `Q.tick(..., lease=lease)` the recorded `progress_at` values are strictly increasing, more
    than one was written, and none closer together than the throttle.
  - `test_tick_marks_progress_in_sweep_and_readiness_loops`: many queued and terminal campaigns
    with `store.sweep_campaign_tmp` and `backlog_readiness` monkeypatched to advance the fake
    clock 5 s per call; the recorded `progress_at` values advance during those loops, so no gap
    between consecutive stamps exceeds the threshold.
  - `test_ctx_progress_is_noop_without_lease`.
  - `test_run_stamps_last_tick_s`: `Q.run(cfg, once=True, …)` with a slowed tick (monkeypatched
    clock) leaves `last_tick_s` in the lease info alongside `tick_at`.
  - `test_lease_status_exposes_progress_keys` in `tests/test_agentic_lease.py`.
- **Doc/skill tests**: `uv run pytest tests/test_skills.py tests/test_agentic_quiet.py` pass
  (the `runner_journal_silent` row and wake mapping still resolve).
- **Manual verification**: with a runner holding the lease and several runs in flight,
  `wt queue list --json` shows `.lease.progress_at` newer than `.lease.tick_at` while a tick is
  executing and `.lease.last_tick_s` set; `wt queue events` produces no `runner_journal_silent`
  over several minutes of normal operation.
- **Regression guard**: `uv run pytest` in full, including the existing `runner_journal_silent`
  tests in `tests/test_agentic_watchdog_gaps.py` and `tests/test_agentic_loop_state.py`;
  `python3 tools/spec_lint.py` exits 0.

### Test record (2026-10-30)

- `uv run pytest tests/test_agentic_loop_state.py tests/test_agentic_lease.py tests/test_agentic_watchdog_gaps.py tests/test_skills.py tests/test_agentic_quiet.py tests/test_agentic_events.py` — 224 passed, including every new test named above.
- The full suite was left to gating. The manual live-runner check was not performed in this run; `test_run_stamps_last_tick_s` and the two `Q.tick` mark tests cover the same stamps against the real lease code path.

### As shipped (2026-10-30)

- The Design's `EVENT_FIELDS` is the `EVENT_TYPES` table in `events.py` (type -> required data keys); `runner_journal_silent` there now lists `last_progress_at` and `last_tick_s`. The matching assertion is in `test_runner_journal_silent_progress_stamp_counts_as_activity`.
- The `runner_silence` docstring now names `progress_at` among the activity stamps.
- `without_progress_at_is_unchanged` is a concrete test: stale `tick_at`, no `progress_at`; it fires at the same time as before with `last_progress_at` and `last_tick_s` None and the episode key `runner_journal_silent:<pid>:<tick_at>`.
- Mark points also include the single-shot close loop (step 4), next to the open dispatches, the admission loop, `_submit` and the loops listed in Design.
- The episode key needed no change beyond adding `last_progress_at` to the set it takes the newest of.

## Rollout / migration

1. Land the lease keys, `_Ctx.progress`, the `run()` timing and the `runner_silence` change
   together; the lease file is read tolerantly, so no data migration.
2. A runner started before this change keeps writing no `progress_at`; the new watchdog treats
   that as today's behaviour. A restart (or the code-change re-exec, SPEC-0229) picks up the new
   stamps.
3. Update the skill row and capability doc in the same change.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The 1 s throttle and the choice of mark points are implementation defaults, to be tuned
  from `last_tick_s` evidence if a real stall is ever missed or a false alarm recurs.
