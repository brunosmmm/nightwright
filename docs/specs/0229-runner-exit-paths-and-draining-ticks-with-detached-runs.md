---
id: SPEC-0229
title: "Runner exit paths and draining ticks with detached runs"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, queue, concurrency, reexec]
source_idea: IDEA-100
depends_on: [SPEC-0224, SPEC-0225]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), and the
last part of bug `IDEA-100`.
[SPEC-0225](./0225-detached-persona-runs-adopted-across-runner-restarts.md) makes persona
runs detachable. It adds the run-dir contract, task phases, `StepPool.try_detach_all()` and
adoption, and uses them for one exit path: the code-change exec. Two gaps remain:

1. **The other exit paths still wait for, or kill, persona runs.** `run()` (queue.py) leaves
   its loop on `signal` (SIGINT/SIGTERM via `_handler`), `once`, `max_ticks`, `idle`
   (`--until-idle`) and `crash`. Every non-crash exit calls `_drain`, which joins every
   flight. The crash path calls `pool.shutdown(cancel=True)`, which still waits for running
   threads. With detachable runs, none of these needs to wait for a persona run.
2. **Admission stops during a code-change drain.** Under SPEC-0225 the exec waits only for
   in-process steps (a gate run can take ~3 min, a close more), plus seconds for `spawning`
   and `collect`. But the loop stops admitting the moment it detects the change, so a ready
   item still waits for that gate. SPEC-0224's `runner_draining` reports it, without fixing
   it.

## Goals / Non-goals

**Goals**
- Every exit path follows one rule: wait for in-process work only, detach persona runs, and
  leave them to the next runner. The table below states each path.
- During a code-change drain, admission, opening and persona steps continue. The exec
  happens at the first loop check where nothing is in-process, `spawning` or `collect`. The
  extra lag after that moment is bounded by one 0.2 s check.
- A crash never blocks on a live run, including one that is still spawning.

**Non-goals**
- Changing what SIGTERM forwarding kills. Gate subprocesses share the runner's process group
  and die with it, as SPEC-0213/0220 require. Detached runs are in their own sessions.
- Changing adoption (SPEC-0225).

## Decision

### Exit-path table

| exit | admission after the trigger | in-process work (gate, close) | persona runs in `wait` | `spawning` / `collect` tasks | then |
|---|---|---|---|---|---|
| `code_changed` | **continues** (draining ticks); no new gate/close is planned | waited for | detached by `try_detach_all` | waited for (seconds) | journal `stop code_changed detached=[…]`, exec |
| `signal` | stops | waited for; gate subprocesses get the forwarded SIGTERM (SPEC-0220 discard) | detached | waited for | journal `stop signal detached=[…]`, release lease, return |
| `once` / `max_ticks` | stops (the loop has ended) | waited for | detached | waited for | journal `stop once/max_ticks detached=[…]`, release, return |
| `idle` (`--until-idle`) | n/a | none: `idle` means no non-parked campaign and no flight | none | none | as today |
| `crash` | stops | **not** waited for (`cancel=True`) | detached by the crash sweep | bounded re-sweep, then left to adoption | journal `crash`, release, re-raise |

`detached` is empty when nothing was detached. FakeRunner tasks never leave `spawning`, so
for them every path waits exactly as today.

### Draining ticks

On `code_changed`, `run()` no longer breaks out of the loop. It records the detection
(SPEC-0224 `lease.update_info(draining=…)`, journal), and from then on calls
`tick(..., inprocess=False)`. In `_tick` planning, `gate` and `close` steps are skipped when
`inprocess` is false. They stay in their state and the new process plans them. Admission,
opening, harvest, reviewer fan-outs and the other persona steps run as normal. Admitted
campaigns' implementers are detachable as soon as their wrappers claim `proc.json`.
After each tick, the inter-tick wait polls every 0.2 s. On each poll it runs `_harvest`
when a flight is done, then `pool.try_detach_all()`. When that returns a list, the loop
journals and execs at once, without waiting out `poll_interval_s`.

Convergence: once draining starts, no new in-process step starts and the existing ones
finish. `spawning` and `collect` last seconds per task, and new persona submissions are
bounded by the free slots, so a moment with nothing blocking arrives, and the 0.2 s poll
catches it. A signal during draining ticks turns the exit into `signal` (no exec), as today.

### Crash sweep

In `run()`'s `finally`, when `crashed`:
1. Under the pool lock, set `pool.detach_all = True` and the detach flag of every `wait`
   task. `RunHooks.spawned()` honours `detach_all`: a task that finishes spawning after this
   point is marked detached instead of entering `wait`, so its thread returns at once.
   `enter_collect()` returns False under `detach_all`, so a run that exits now is left for
   adoption to collect.
2. Re-sweep every 0.2 s, for at most 30 s, until no task is in `spawning`. Tasks in
   `collect` that started before step 1 are let finish within the same 30 s bound.
3. Then `pool.shutdown(cancel=True)`. A thread still stuck in `spawning` after 30 s (for
   example a hung `Popen`) is left behind. The wrapper claims `proc.json` by itself
   (SPEC-0225), so the next runner adopts the run whatever this thread does.

The lease is released after step 3, as today, so the 30 s bound is the most a crash can
delay the release.

## Design

- `run()`:
  - the `code_changed` branch sets `draining` and continues the loop with
    `inprocess=False`, instead of `break`;
  - the inter-tick wait loop, while draining, calls `_harvest` + `try_detach_all()` on each
    0.2 s step. Not draining, it keeps today's `any_done` early break;
  - after the loop, for `signal` / `once` / `max_ticks`, `_drain` loops `wait_any(0.2)`,
    `_harvest`, `try_detach_all()` until `pool.flights` is empty. `idle` has no flights;
  - `stop` gains `detached: [{campaign, run_ids}]` on every path.
- `_tick(..., inprocess=True)`: when false, `gate` and `close` steps are `continue`d in
  planning. Single-shot `tick()` and `wt queue tick` always pass True.
- `StepPool.detach_all: bool` (under `lock`), honoured by `RunHooks.spawned()` /
  `enter_collect()` as above.
- `queue run` CLI docstring: "SIGINT/SIGTERM stop admissions and wait for in-process steps;
  persona runs keep running and the next `wt queue run` adopts them (SPEC-0225/0229)."
  The same sentence goes into `skills/wt-conduct/SKILL.md`, replacing the advice to wait for
  runs before restarting the runner. A restart no longer costs a run.

## Alternatives considered

- **`until_idle` waits for persona runs.** It never has to. `idle` is defined as "no
  non-parked campaign and no flight", so there is nothing to detach when it triggers. No
  special case needed.
- **`once` / `max_ticks` keep waiting for runs** (today's behaviour). Those flags exist for
  bounded runs, and blocking for up to 30 min on a run the next runner would adopt anyway
  works against them. Rejected. The `stop` record lists what
  was left.
- **Exec immediately on `code_changed`, abandoning in-process work.** A gate or close cut
  off by the exec is discarded and redone (SPEC-0220 makes that safe), but merges and pushes
  would be cut off as well. Rejected. In-process work is bounded and short next to a
  persona run.
- **Unbounded crash wait for `spawning` tasks.** A hung spawn would hold the lease forever.
  The claim-by-wrapper rule makes the bound safe. Rejected.

## Acceptance criteria

- [ ] After a code change, while a gate is in flight: a ready QUEUED item is admitted and
      opened, and its implementer is submitted. No `started gate` or `started close`
      follows the detection. `_EXEC` is called within 0.5 s of the gate being harvested
      (with `poll_interval_s: 20`), the implementer is listed under `detached`, and the new
      process adopts it.
- [ ] A stop triggered through `ctx.stop`, the same path `_handler` takes, with a persona
      run in `wait` and a gate in flight: the gate's subprocess is killed, and the gate
      result is discarded per SPEC-0220. `run()` returns while the run's process is alive,
      `stop signal` lists it, and a new `run()` adopts and applies it.
- [ ] `--once` and `--max-ticks` exits detach wait-phase runs the same way, and `run()`
      returns while they are alive. `--until-idle` behaves as today. FakeRunner runs are
      waited for on every path.
- [ ] A crash raised inside `tick` while one task is in `wait` and another in `spawning`
      (paused wrapper start): `run()` re-raises within the 30 s bound, the lease is
      released, both runs' processes are alive, and the next `run()` adopts both (`running`).
      A run that exits during the sweep is collected by the next runner, not by the
      crashing one.
- [ ] Every `stop` record carries `detached` (possibly empty). The CLI help and the conductor
      skill state that a stop no longer costs persona runs.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Fixture:** SPEC-0225's `tests/fixtures/fake_claude.py` on `PATH`, plus its paused-wrapper
  helper. Stops are driven by setting `ctx.stop` (the event `_handler` sets) or through a
  `run(..., stop_event=…)` test hook, and code changes by the patched `code_changes`.
  `_handler` is never called directly.
- **Automated tests:** new `tests/test_agentic_exit_paths.py`.
  - `test_draining_ticks_admit_and_exec_promptly`: A is GATING with a held gate, a code
    change is detected, and B is ready. B is admitted, opened and its implementer started
    during the drain, with no gate or close started after detection. The gate is released.
    `_EXEC` is called within 0.5 s of the harvest, with B's run under `detached`.
  - `test_signal_stop_detaches_and_kills_gate`: the gate's subprocess pid is gone after the
    stop, the run's `child.pid` is alive, and the `stop signal detached` record is written. A
    second `run()` journals `adopt … running`.
  - `test_once_and_max_ticks_detach` (parametrized): `run()` returns in < 2 s with the run
    alive, and `stop` lists it.
  - `test_until_idle_unchanged`: FakeRunner e2e reaching idle gives the same journal as
    before.
  - `test_fakerunner_waits_on_every_path`: FakeRunner `sleep_s` runs are harvested before
    `run()` returns, for signal, once and max_ticks.
  - `test_crash_with_spawning_task`: `tick` raises on its second call while one task is in
    `wait` and one is paused in `spawning`. The pause is released after 1 s, so the task
    becomes detached through `detach_all`. `run()` re-raises within 30 s, the lease file is
    released, both children are alive, and the next `run()` adopts both.
  - `test_crash_sweep_bound`: a `spawning` task paused for longer than the bound (the bound
    is patched to 1 s) → `run()` re-raises after about 1 s, and the next runner adopts the
    run once the wrapper claims it.
  - `test_stop_record_has_detached_on_every_path`.
- **Existing suites unchanged:** `tests/test_agentic_reexec.py`,
  `tests/test_agentic_nonblocking.py` (including its subprocess SIGTERM test),
  `tests/test_agentic_close.py`, `tests/test_agentic_heartbeat.py`,
  `tests/test_agentic_adopt.py` (SPEC-0225).
- **Sabotage checks:** run the suite after each of these (restoring in between) and confirm
  it fails:
  - plan gates while draining;
  - drop the 0.2 s detach poll (exec waits for `poll_interval_s`);
  - ignore `detach_all` in `spawned()`;
  - wait unbounded in the crash sweep.
  Record the outcomes in the spec.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** with a gate running and an item ready, merge a code change.
  `wt queue runner-log --tail 8` shows the ready item admitted after the detection, then
  `stop code_changed detached=[…]` right after the gate result. A `kill -TERM` of the runner
  during a persona run leaves `ps` showing the run alive, and `wt queue run` adopts it.

## Rollout / migration

Lands after SPEC-0225. No data migration. Journal addition: `detached` on every `stop`
record. The conductor skill's restart guidance changes with it.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
