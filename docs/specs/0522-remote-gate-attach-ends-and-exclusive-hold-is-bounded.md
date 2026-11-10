---
id: SPEC-0522
title: "A finished remote gate job always ends its attach, and an exclusive gate step cannot hold other gates forever"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, gates, remote, bug]
depends_on: [SPEC-0311, SPEC-0341]
source_idea: IDEA-1115
---

## Context

Observed 2026-11-09 (IDEA-1115). At 21:15:06 campaign SPEC-0476-20261109-ca46 had a
`pytest-affected` timeout, so SPEC-0311 re-gated it **exclusively** (`state.gate_exclusive`
set, `timeout_regates 1/2`). Its gate step started at 21:15:07 on the worker box
(`ssh nw@morais-Desktop nw worker attach SPEC-0476-...-gate-NNN-aN --from 0`). The job's last gate
finished on the box at 21:15:27 (`gates/006-spec-lint.json`, ok), yet the attach SSH process
never returned (5+ minutes later it was still open), and during that time no other campaign's
gate started: 7 campaigns sat in GATING.

What the code does today:

- Gate steps already run on the runner's `StepPool` threads (`queue._submit` ->
  `pool.submit(_do_gate, ...)`); the tick itself is not blocked by the attach (the runner kept
  ticking, `runner.jsonl` shows 20 ticks before the human stopped it).
- What stalled everyone is SPEC-0311's exclusivity: `select_gate_starts` starts nothing else
  while an exclusive gate step is in flight (`_gates_in_flight` returns `exclusive=True` for any
  not-done gate future whose campaign carries `gate_exclusive`). The in-flight hold has no
  bound, so a step that never returns holds every other campaign's gate forever. (SPEC-0507
  bounds the *pending* exclusive wait; it does not bound the *in-flight* hold.)
- The step never returned because `remote_gates._attach` waits only for frames on the SSH
  stdout pipe. Its other exits are a stop, the record's `deadline_at` (`timeout_s +
  worker_ttl_s`, about an hour here) and EOF. If the SSH session stays open without delivering
  the `done` frame (an SSH client kept alive by the `ControlMaster=auto` / `ControlPersist=120`
  multiplexing, a wedged mux channel, or a worker-side attach that stopped writing), the step
  waits until the deadline even though the job is finished on the worker.

The earlier `KeyError: 'sha'` in `_apply_spec_review` is already fixed on main (SPEC-0482:
`elif "sha" not in sr` turns an adopted review without a start snapshot into a reviewer failure);
nothing to change for it here.

## Goals / Non-goals

**Goals**
- A remote gate step whose job has ended on the worker (done, error, lost, unknown) leaves its
  attach within a bounded time, independent of the attach SSH stream.
- An exclusive gate step that runs past a bound no longer blocks other campaigns' gate starts.

**Non-goals**
- Changing SSH multiplexing options (ControlMaster/ControlPersist stay; they save a handshake
  per call and are shared by every worker call).
- Changing SPEC-0311's exclusivity semantics for a step within its bound, or SPEC-0507's
  pending-wait bound (separate spec).
- Cancelling or killing the long step: it keeps running and is harvested as usual when it ends.

## Decision

1. **Quiet-attach watchdog.** While attached, when no line has arrived for
   `remote_gates.QUIET_POLL_S` (30 s), the coordinator asks the worker directly with a separate
   short SSH call, `poll JOB --from <seen> --renew` (the existing `CALL_TIMEOUT_S` bound applies):
   - `accepted` / `running`: the job is alive, just quiet; keep attached (the worker answered, so
     `last_seen` is refreshed).
   - `done` / `error`: the poll's frames after `seen` are validated (`_frame`) and fed to the
     job exactly as attach frames are; the `done` frame ends the step (`error` raises as today).
     If the poll carried no `done` frame, the attach is ended as `dropped` and `_follow`
     re-attaches.
   - `lost` / `unknown`: the attach ends as `lost` (the existing give-up path).
   - unreachable (poll returned nothing): keep waiting; once `worker_ttl_s` has passed since the
     worker last answered, the attach ends as `dropped` so `_follow` gives up `unreachable`.
   The attach SSH process is then ended by the existing `_end(proc, 0)` (TERM/KILL of its own
   process group), so a hung client never outlives the step.
2. **Bounded exclusive hold.** A gate step (or a re-gating close step) that has been in flight
   longer than its hold bound stops counting in `_gates_in_flight` (neither in `n` nor as
   `exclusive`), so other gate starts, including a pending exclusive one, proceed. The bound is
   the project's longest gate `timeout_s` plus `GATE_HOLD_SLACK_S` (600 s): every healthy run of
   the gate that timed out is killed by its own timeout before then. The first time a step is
   past its bound the runner journals `gate_hold_expired` (campaign, step, age_s, bound_s, exclusive) once
   per flight. The step itself keeps running.

## Design

- `src/nightwright/agentic/remote_gates.py`
  - `QUIET_POLL_S = 30.0`.
  - `_quiet_check(job) -> tuple | None`: one `_call(worker, "poll", job_id, "--from", seen,
    "--renew")`; returns `None` (stay attached) or an `_attach` result tuple, as in Decision 1.
  - `_attach`: tracks `quiet_since` (monotonic, reset on each line); on `queue.Empty` past
    `QUIET_POLL_S` it calls `_quiet_check` and returns its tuple when not `None`.
- `src/nightwright/agentic/queue.py`
  - `GATE_HOLD_SLACK_S = 600`; `_gate_hold_s(pcfg) = max(timeout_s of pcfg["gates"]) +
    GATE_HOLD_SLACK_S` (default gate timeout when the list is empty).
  - `_gates_in_flight(ctx, pool, now=None)`: skips a not-done gate/close step whose flight
    `started_at` is older than `_gate_hold_s(ctx.project(fl.state))`; journals
    `gate_hold_expired` once per `(campaign, started_at)` via `pool.deferred_noted`.

## Alternatives considered

- **Drop ControlMaster for `attach`.** Removes one possible cause of the hang, but not a wedged
  worker-side attach or a stuck channel; the poll watchdog covers every cause and keeps the
  shared master.
- **Shorter `deadline_at`.** The deadline also bounds a legitimately slow job; shortening it
  would fail real runs. The watchdog ends only finished jobs early.
- **Run gate steps outside the pool / detach them.** They already run on pool threads; the tick
  was not blocked. The stall was the unbounded exclusive hold.
- **Bound the hold with a fixed constant.** A project with long gates would lose exclusivity
  while its re-gate is still legitimately running; tying it to the project's own gate timeouts
  keeps SPEC-0311's intent.

## Acceptance criteria

- [x] An attach whose SSH stream goes silent without the `done` frame, for a job that is `done` on
      the worker, ends with the job's `GateRun` (all gate results, ok) within about
      `QUIET_POLL_S`, without waiting for `deadline_at`, and the attach SSH process is ended.
- [x] A quiet attach for a job the worker reports `running` stays attached (no give-up, no
      fallback).
- [x] A quiet attach for a job the worker reports `lost` ends through the existing lost path.
- [x] An exclusive gate step in flight within its bound still blocks other gate starts
      (SPEC-0311 unchanged); past `_gate_hold_s` it no longer does, and `gate_hold_expired` is
      journaled once.
- [x] `python3 tools/spec_lint.py` exits 0 with the ledger regenerated.

## Test plan

- **Automated tests:**
  - `tests/test_remote_job_records.py::test_quiet_attach_of_a_finished_job_ends_by_poll`: the
    loopback worker (SPEC-0338) behind an `ssh` wrapper that, for `attach`, forwards frames but
    withholds `done` and then hangs; `QUIET_POLL_S` patched small. The gate step returns the
    run (g1..g3 ok, worker `box`), no `worker_failed` / `worker_job_lost`, and the record is
    `done`.
  - `tests/test_remote_job_records.py::test_quiet_check_states` (unit, fakes for `_call`):
    `running` -> `None` and `last_seen` refreshed; `lost` -> `("lost", "lost")`; unreachable
    within TTL -> `None`, past TTL -> `("dropped", False)`.
  - `tests/test_agentic_queue.py::test_exclusive_hold_expires_past_gate_timeout`: an exclusive
    gate flight younger than the bound gives `(1, True)` and blocks a non-exclusive candidate;
    with its `started_at` older than the bound it gives `(0, False)`, the candidate starts, and
    `gate_hold_expired` is journaled once across two calls.
- **Manual verification:** none locally (the runner is stopped and the box is not exercised
  from this change); the loopback test drives the real `nw worker` attach/poll code paths.
- **Regression guard:** the existing tests in `tests/test_remote_job_records.py` and the
  SPEC-0311 tests in `tests/test_agentic_queue.py` (`test_timeout_regate_is_serialized`,
  `test_exclusive_regate_not_starved`, `test_gate_starts_counts_flights_and_close_regates`).

## As shipped

- Implemented as designed (Decision 1 and 2). The journal field for the step kind is `step`,
  not `kind`, because `journal(cfg, kind, **fields)` already uses `kind` as the event kind.
- `_quiet_check` reads `done`/`error` frames straight from the `poll` reply; a `done` poll
  without a `done` frame ends the attach as `dropped` so `_follow` re-attaches from `seen`.
- Verification: the three named tests pass locally, with the regression guards listed above.
  The worker box was not exercised (the runner is stopped); the loopback test drives the real
  `nw worker attach`/`poll` code.

## Rollout / migration

None: no data change. Takes effect on the next runner start.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the named tests pass.
- [ ] No regressions in the regression-guard tests.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
