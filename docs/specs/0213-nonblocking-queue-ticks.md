---
id: SPEC-0213
title: "Non-blocking queue ticks: in-flight steps carry across ticks"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, queue, concurrency]
source_idea: IDEA-081
depends_on: [SPEC-0176, SPEC-0194, SPEC-0200, SPEC-0204]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-081`. `queue.tick()` ([SPEC-0176](./0176-queue-state-machine-with-lease-guarded-tick-run.md))
runs the agent steps (implementer, gates, gate triager, reviewers) in a `ThreadPoolExecutor`
and joins them all before it returns. The next tick, and with it admission and every other
campaign's next step, waits for the slowest step: gates take about 2.5 min, implementers
3–11 min. On 2026-10-25 a ready priority-1 item (SPEC-0201) waited minutes without being
admitted while 2 of 3 slots were free. Admission (`pick_admissions`) was correct. The tick
was blocked. Campaigns advanced in lockstep, so extra slots helped much less than they should.

## Goals / Non-goals

**Goals**
- A slow step in one campaign never delays admission, or another campaign's transitions.
- Keep the SPEC-0176 guarantees: Python decides every transition, results are applied on the
  main thread under the campaign lock, and there is at most one step per campaign.
- Keep the guarantees of SPEC-0194 (re-exec), SPEC-0200 (slots / WAITING) and SPEC-0204
  (heartbeat).

**Non-goals**
- Running OPENING / CLOSING steps concurrently. They touch the main repository and stay
  serial on the main thread, as today.
- Changing the events projection or watchdog (`events.py`).
- Changing the per-campaign step order or any transition rule.

## Decision

`run()` owns a persistent `StepPool`: one executor plus at most one *flight* (in-flight
step) per campaign. A tick:

1. applies the flights that finished;
2. does recovery, WAITING, admission and opening;
3. submits a step for every active campaign that has no flight;
4. closes serially;
5. returns without waiting.

Flights survive across ticks. They are joined only at shutdown (signal, `--once`,
`--max-ticks`) or before a code-change re-exec. `tick()` without a pool (`wt queue tick`,
tests) makes a throwaway pool and waits for its own steps, so single-shot behaviour is
unchanged.

## Design

### `StepPool` / flights (`src/wt/agentic/queue.py`)

- `_Flight(state, ctx, tasks=[(Step, Future)], group=_ReviewGroup|None, started_at)`: one
  campaign's in-flight unit. An implement, triage or gate step is one task. A reviewer
  fan-out (SPEC-0201) is one task per reviewer and one `_ReviewGroup`. The flight is done
  when every task is done. It keeps the `_Ctx` of the tick that submitted it, so its result is
  applied with the same profile and project config it was planned with.
- `StepPool`: a lazily created `ThreadPoolExecutor(max_workers=64, thread_name_prefix=
  "wt-step")` and `flights: dict[campaign id → _Flight]`. Choice: threads are created on
  demand. The real limit on concurrency is slots × reviewers per campaign, so a fixed high
  cap never queues a reviewer behind another campaign's step, and we never resize the pool.
  Methods: `submit`, `ids()`, `any_done()`, `wait_any(timeout)`, `shutdown()`.
- `_harvest(pool, report)` (main thread) removes each finished flight and applies it. A
  single task goes through `_apply`. A worker exception becomes the existing
  "implement error" park, plus a `report.errors` entry. A review group goes through
  `_finish_reviews`, which still applies it once, in profile order. Flights are applied in
  the order they were submitted.

### `tick(cfg, *, stop, runner, admit=True, pool=None)`

- `pool is None` ⇒ a throwaway pool. After submitting, the tick waits for every flight,
  harvests them, then runs the closes. This is today's order (agent steps applied, then
  closes), so `wt queue tick` and the existing tests stay deterministic.
- With a pool, the tick harvests first, then lists the campaigns, so a slot freed by a
  finished step is admitted in the same tick. A campaign whose result was just applied gets
  its next step planned in that same tick.
- **Slot accounting (SPEC-0200).** `busy` = the ids of campaigns in `ACTIVE_STATES` on disk
  ∪ `pool.ids()`. A campaign with a flight keeps its active state on disk until its result
  is applied, so it already counts. The union also keeps a flight counted if its on-disk
  state somehow left `ACTIVE_STATES`. WAITING promotion, config-recovery and
  `pick_admissions` all use this count.
- **No double step.** The planning loop skips every campaign in `pool.ids()` before
  `plan_step`. While a step is in flight, nothing about that campaign is planned, parked or
  prepared: no `_prepare_run`, no `run.start`. Its state dict belongs to the worker until the
  flight is harvested.
- **Park while in flight.** A human park requested while a step runs is deferred, as before
  (`plan_park` → `park_requested`). The campaign is not planned, so the request waits. When
  the flight is harvested, `_apply` → `finish()` records the step and then applies the park.
  This is the existing SPEC-0176 `park_requested` path.
- `TickReport` gains `started` (`[{campaign, action, state, run_ids}]` for the steps this
  tick submitted), `in_flight` (campaign ids still running when the tick returns) and
  `drain` (false for a normal tick). All three are additive in `wt.queue.tick.v1`. `idle`
  is true only with no non-parked campaign and no flight.

### `run()`

- **Heartbeat for the loop's whole life (SPEC-0204).** `_heartbeat_while(lease)` wraps the
  whole loop: ticks, inter-tick waits, the drain, and the run-up to re-exec. It no longer
  wraps each tick. One `wt-lease-heartbeat` thread lives from just after `acquire` until just
  before `release`/`handover`, and is joined before either. The between-tick
  `lease.heartbeat()` stays.
- **Inter-tick wait.** The loop sleeps `poll_interval_s`, but wakes early when a flight
  finishes (`pool.any_done()`), so a finished step is applied and its successor submitted
  within about 0.2 s. When flights are running and the interval is below 0.1 s, the wait is
  at least 0.1 s, so the loop does not busy-spin.
- **Drain.** When the loop leaves (signal, `--once`, `--max-ticks`, `--until-idle`, code
  change), `_drain(cfg, pool, ...)` waits for the flights and applies each one as it
  finishes, on the main thread. The drained rows go out as one more report with
  `drain: true`: journaled as a `tick` record and passed to `on_tick`. They are not counted
  as a tick. `--once` therefore still means "one tick, its steps recorded", as before.
- **Signals.** The SIGINT/SIGTERM handler is unchanged: it sets `stop` and forwards SIGTERM
  to the process group when we lead it. Workers see `stop` (`res.interrupted`), so the drain
  records the existing `interrupted` `run.end` and the cost. Only then are the lease and the
  pool released.
- **Re-exec drains first (SPEC-0194).** When `code_changes` reports changed wt code, the
  loop stops ticking, which means no admission and no new submissions. It drains every
  flight (results applied), journals `stop` with reason `code_changed`, and only then hands
  the lease over and calls `_EXEC`. If a signal arrives during the drain, the reason becomes
  `signal` and there is no exec. `_EXEC` is never called with a flight pending: `_drain`
  returns only when `pool.flights` is empty, and the pool is shut down before the exec.
- **Crash.** On an unexpected exception the pool is shut down with `cancel_futures=True`,
  waiting for running workers as the old `with ThreadPoolExecutor` did. As before, their
  results are not applied; the next loop re-plans from disk.

### Concurrency note

`Lease.heartbeat` is now also called by the main thread while the heartbeat thread runs.
Each write goes to its own `mkstemp` file and is `os.replace`d, so concurrent beats cannot
corrupt the lease file. The only shared mutation is reassigning an existing dict key.

A human park with **no** lease held is immediate, even mid-flight. This only happens to
tick-level callers; `run()` always holds the lease. The flight then keeps its slot until it
is harvested, and `_apply` records the step's facts with `superseded_by_human` (the
SPEC-0176 path), so it never moves a campaign a human parked.

Close steps (main repository: rebase, merge, re-gate) can now overlap other campaigns'
implementers and gates, which run in their own worktrees. Before, closes ran after the pool
had joined. Git worktrees share only the object store and refs, each worktree touches only
its own branch ref, and git serializes ref updates with lock files. This interleaving
already happened between the runner and a human's or the conductor's git commands, so
nothing new is serialized.

### Visible behaviour changes

- In `wt queue run` and the runner journal, a step's result row appears in a *later* tick
  than the one that started it (or in the final `drain` record). Ticks that only start steps
  now print `<campaign>: started <action> in <STATE> (run …)` lines and are journaled with
  `started`. `wt queue runner-log` shows `started …` and `drain` lines.
- `wt queue tick` output is unchanged.

## Alternatives considered

- **Shorter ticks with `wait(timeout)`.** Still blocks the tick for the timeout, and splits
  each step's result handling across ticks anyway.
- **One process per campaign.** Gives up the single lease-holding loop that owns every
  transition (SPEC-0176), and would need cross-process run accounting.
- **Draining lazily for re-exec (keep ticking without submitting until idle).** It does the
  same thing with more states. The blocking drain is simpler, and a re-exec is rare.

## Acceptance criteria

- [x] With campaign A's implementer sleeping, campaign B is admitted, opened, and moves
      through its own transitions across several ticks before A's run finishes.
- [x] A campaign with a flight is never planned again: exactly one `run.start` per step,
      however many ticks pass while it runs.
- [x] A code change detected while a flight runs leads to: drain (run.end recorded and
      transition applied) → `stop code_changed` journaled → `_EXEC` called once, with no
      flight pending at exec time.
- [x] The lease stays fresh (never stale, age < ttl) for a run that spans many ticks, and
      exactly one heartbeat thread exists during the loop and none after it.
- [x] SIGTERM in the middle of a run (real process, several ticks into the run) records an
      interrupted `run.end` with its cost, leaves the state unchanged, and releases the
      lease.
- [x] In-flight campaigns count as busy: with every slot taken by flights, a QUEUED item is
      not admitted and a WAITING item stays WAITING; a park requested mid-flight applies
      once the step is recorded.
- [x] `wt queue tick` stays single-shot and deterministic, and the existing suites pass.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_nonblocking.py`, all offline. The tick-level
  tests use `HeldRunner`, a FakeRunner wrapper that holds a named spec's runs on a
  `threading.Event`, so "A is still running" is deterministic rather than timing-based. The
  loop-level tests use FakeRunner `sleep_s`.
  - `test_slow_run_does_not_block_admission_or_other_campaigns`: two specs, slots 2, a pool
    passed to `tick`. While A's implementer is held, B is admitted (the tick returns in
    < 10 s), implements, gates green and reaches REVIEWING. A still has one `run.start`.
    After release, A → GATING.
  - `test_no_double_submission_while_in_flight`: 6 ticks during a held run ⇒ no rows, no
    `started`, `in_flight == [A]`, one `run.start`, `counters.runs == 1`. The harvest tick
    applies → GATING and submits the gate in the same tick.
  - `test_inflight_counts_as_busy_for_slots`: slots 1. A flies; a ready QUEUED B is not
    admitted and a WAITING C is not continued. A human then parks A with no lease
    (immediate), so A is PARKED on disk with its step still running: the slot is still
    held. On harvest: `superseded_by_human`, run.end kept; the slot goes to C (WAITING beats
    the backlog) and B stays QUEUED.
  - `test_park_requested_mid_flight_applies_after_record`: `park_requested` written
    mid-flight; 3 ticks leave A alone; the harvest records run 001 and parks
    (`resume_to: GATING`).
  - `test_reexec_drains_inflight_before_exec`: `run(reexec=True)`; a code commit lands in the
    tick that submits a 1.5 s implementer. The patched `_EXEC` checks, when it is called:
    run.end on disk, state GATING, no `wt-step` thread alive, journal ends
    `tick(drain) → stop code_changed`, the handover env is present; `ticks == 2`.
  - `test_heartbeat_spans_many_ticks`: `lease_ttl_s: 2`, a 3.5 s run, poll 0.05,
    `until_idle` to MERGED. More than 10 ticks; a side thread samples the lease every 0.2 s:
    never stale, age < 2 s, exactly one heartbeat thread ident seen, none after.
  - `test_sigterm_mid_flight_after_several_ticks`: real subprocess (`wt queue run
    --poll-interval 0.05 --no-reexec`). SIGTERM 1.5 s into a 4 s run: interrupted run.end,
    cost 0.5, state IMPLEMENTING, lease released; journal `stop` has `ticks > 3` and is
    preceded by the `drain` record (row reason `interrupted`); stdout shows `started
    implement in IMPLEMENTING`.
- **Sabotage checks:** each invariant was removed in turn, and the suite above failed each
  time (see the executed record).
- **Regression guard:** full `uv run pytest` (all `test_agentic_*` suites included).
- **Manual verification:** none beyond the suite (the live runner on main picks this up via
  SPEC-0194 re-exec once merged).

### Executed (2026-10-25)

- `tests/test_agentic_nonblocking.py`: 7 passed.
- Sabotage, `queue.py` restored after each:
  - removing the in-flight skip in planning ⇒ 6 failed;
  - removing `| flying` from slot accounting ⇒ `test_inflight_counts_as_busy_for_slots`
    failed;
  - removing the drain ⇒ the re-exec and SIGTERM tests failed;
  - `run()` calling a blocking `tick()` (the old behaviour) ⇒ 3 failed (re-exec,
    heartbeat, SIGTERM);
  - heartbeat scoped per tick (as in SPEC-0204) ⇒ `test_heartbeat_spans_many_ticks` failed.
- Every pre-existing `test_agentic_*` suite passed unchanged (422 tests). For the full
  `uv run pytest` and `spec_lint` results, see the final record below.
- Final record: full `uv run pytest` gave 1777 passed, 1 skipped, 0 failed.
  `python3 tools/spec_lint.py --write-ledger`, then `python3 tools/spec_lint.py`, exited 0.
  `wt spec verify SPEC-0213` reported ok.

## Rollout / migration

Built on branch `nonblocking-ticks` (worktree `/home/bruno/work/wt-nonblocking-ticks`). No
data migration. Once merged, the live runner re-execs into it (SPEC-0194): it drains, then
execs.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
