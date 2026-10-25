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

1. **The other exit paths still end persona runs, or wait for them.** `run()` leaves its
   loop on `signal` (SIGINT/SIGTERM via `_handler`), `once`, `max_ticks`, `idle`
   (`--until-idle`) and `crash`.
   - On a stop, SPEC-0225's interim rule (its Decision 7) kills the runs, which costs them.
   - `once` / `max_ticks` call `_drain`, which joins every flight.
   - The crash path calls `pool.shutdown(cancel=True)`, which is
     `ThreadPoolExecutor.shutdown(wait=True)`. It still joins every running thread: a gate
     until it ends, a hung `Popen` forever.
   - Gate subprocesses run in their own sessions (`gates.py`, `start_new_session=True`), so
     `_handler`'s `killpg` of the runner's own group never reaches them. Today a stop or a
     crash waits for the whole gate.
2. **Admission stops during a code-change drain.** Under SPEC-0225 the exec waits only for
   in-process steps (a gate run can take ~3 min, a close more), plus seconds for `spawning`
   and `collect`. But the loop stops admitting the moment it detects the change, so a ready
   item still waits for that gate. SPEC-0224's `runner_draining` reports this but does not
   fix it.

## Goals / Non-goals

**Goals**
- Every exit path follows one rule: in-process work is ended or waited for within a bound,
  persona runs are detached and left to the next runner. The table below gives each path.
- During a code-change drain, admission, opening and detachable persona steps continue. The
  exec happens at the first 0.2 s check where nothing blocks it.
- A stop or a crash never blocks on a live persona run, and never waits unboundedly for a
  gate or a hung spawn.

**Non-goals**
- Changing adoption (SPEC-0225).
- Detaching non-detachable steps (FakeRunner, gates, closes). They are **in-process work**:
  steps whose thread must finish in this process.

## Decision

### Exit-path table

"In-process work" means gate steps, close steps once SPEC-0220 lands, and every persona task
whose runner is not detachable (FakeRunner).

| exit | admission after the trigger | in-process work | persona runs in `wait` | `spawning` / `collect` tasks | then |
|---|---|---|---|---|---|
| `code_changed` | **continues** (draining ticks): new admissions and **detachable** persona steps only; no gate, close or non-detachable step is planned | waited for (finite: nothing new of that kind starts) | detached by `try_detach_all` | waited for (seconds) | journal `stop code_changed detached=[…]`, exec |
| `signal` / `ctx.stop` | stops | `_terminate_children()` kills gate process groups; results discarded (SPEC-0220); other in-process threads joined with a 30 s bound | detached | waited for, 30 s bound | journal `stop signal detached=[…]`, release lease, return |
| `once` / `max_ticks` | stops (the loop has ended) | waited for, as today | detached | waited for | journal `stop once/max_ticks detached=[…]`, release, return |
| `idle` (`--until-idle`) | n/a | none: `idle` means no non-parked campaign and no flight | none | none | as today |
| `crash` | stops | `_terminate_children()`, then not waited for beyond the 30 s bound | detached by the crash sweep | bounded re-sweep, then left to adoption | journal `crash`, release, re-raise |

`detached` is empty when nothing was detached. SPEC-0225's interim kill-on-stop is removed:
a stop now detaches.

**Precedence.** If `once`, `max_ticks` or `until_idle` fires during draining ticks, that
exit wins and is a plain exit after detaching. There is no exec, and `stop` carries
`restart_pending: {to_sha}` so the next start runs the new code anyway. A signal during
draining ticks becomes a `signal` exit, as today.

### Ending in-process work: `_terminate_children`

- `gates.py` keeps a registry of live gate processes (`_LIVE: set[Popen]` under a lock).
  `run_all` adds each Popen before waiting on it and removes it after.
  `gates.terminate_all()` calls the existing `_kill_group` (TERM, grace, KILL) on each one.
- `queue._terminate_children()` = `gates.terminate_all()`. It is called by `_handler` and
  also by the main loop when it finds `stop` set, so a stop set through `ctx.stop` (tests,
  library callers) gets exactly the signal path. It is called on the crash path too. The
  killed gate fails, the stop set discards its result (SPEC-0220 rule), and the campaign
  stays in its state for the next runner.
- **Shutdown.** After `_terminate_children()` and the detach sweep: `pool.shutdown(wait=False,
  cancel_futures=True)`, then join the pool's threads with a total bound of 30 s.
  Thread-to-task bookkeeping is in `_Flight`, so the join covers every thread still
  running. When the bound expires, the lease is released anyway. A thread left behind
  holds no lease and has no subprocess left: its gate group is killed, and its run is
  detached or claimed by its wrapper. It is not daemonic, and interpreter exit would join
  it. So `wt queue run` (the CLI) ends with `os._exit(70)` after flushing the journal when
  `Q.leftover_threads()` is non-empty, and no stale thread outlives the lease. The library
  `run()` only re-raises or returns.

### Draining ticks

- On `code_changed`, `run()` no longer breaks out of the loop. It records the detection
  (SPEC-0224 `lease.update_info(draining=…)`, journal), and from then on calls
  `tick(..., draining=True)`. In planning, a draining tick skips:
  - gate and close steps;
  - every persona step when the runner is not detachable (`ctx.runner.detachable` is
    False).
  Skipped steps stay in their state, and the new process plans them. Admission, opening,
  harvest, and detachable persona steps (including reviewer fan-outs) run as normal.
- **Lag.** An item admitted in a draining tick is planned on the next one (`_tick` skips
  `admitted_ids`), so its implementer starts at most one `poll_interval_s` later. If the
  exec point comes first, the item is simply opened, and the new process plans it.
- **The inter-tick wait while draining.** It polls every 0.2 s. On each poll it runs
  `_harvest` when a flight is done, and `emit`s the results as a `tick(drain)` record, as
  `_drain` does today. Then it calls `pool.try_detach_all()`. When that returns a list, the
  loop journals and execs at once. `code_changes` is polled again on every draining tick,
  and `restart.to_sha` / `paths` follow the latest merge.
- **Convergence.** Once draining starts, no new in-process work starts, and the existing work
  finishes. That includes FakeRunner steps: the runner is non-detachable, so no new
  FakeRunner step is planned. `spawning` and `collect` last seconds per task, and new
  detachable submissions are bounded by free slots, so a moment with nothing blocking
  arrives, and the 0.2 s poll catches it. A FakeRunner queue therefore drains exactly like
  today: it stops starting persona steps and waits for the current ones.

### Crash sweep

In `run()`'s `finally`, when `crashed`:
1. `_terminate_children()`.
2. Under the pool lock, set `pool.detach_all = True` and the detach flag of every `wait`
   task. `RunHooks.spawned()` honours `detach_all`: a task that finishes spawning after this
   point is marked detached instead of entering `wait`. `enter_collect()` returns False under
   `detach_all`, so a run that exits now is left for adoption to collect.
3. Re-sweep every 0.2 s until no task is in `spawning`, then shut down as above. The 30 s
   bound covers steps 2–3 together. A thread still stuck in `spawning` after it (a hung
   `Popen`) is left behind. The wrapper claims `proc.json` by itself (SPEC-0225), so the
   next runner adopts the run whatever this thread does.

### Interplay with SPEC-0224

During draining ticks, `draining.waiting_on` lists only what blocks the exec: in-process
work and tasks in `spawning` / `collect`. Detachable runs in `wait` do not block it and are
not listed. `ready_not_admitted` rows keep `runner_draining: true`, but admission continues
while draining, so a `ready_not_admitted` during draining ticks is a real admission problem.
The conductor skill rows change accordingly:
- `runner_draining`: "waiting on a gate, close or FakeRunner step, so wait. Past the largest
  `timeout_s` + 5 min, treat as `lease_stale`."
- `ready_not_admitted` with `runner_draining: true`: "admission should be running, so handle
  it as an ordinary `ready_not_admitted`."

## Design

- `run()`:
  - the `code_changed` branch sets `draining` and continues the loop, instead of `break`;
  - the inter-tick wait loop, while draining, does the 0.2 s harvest/emit + detach poll. When
    not draining it keeps today's `any_done` early break;
  - on `stop` set (any source), the loop calls `_terminate_children()` once, then `_drain`
    loops `wait_any(0.2)`, `_harvest`, `try_detach_all()` until `pool.flights` is empty or
    the 30 s bound expires, then shuts down as above. `once` / `max_ticks` use the same
    `_drain` without terminating anything and without the bound (as today);
  - `stop` gains `detached: [{campaign, run_ids}]` on every path, and `restart_pending` when
    a code change was pending at a non-exec exit.
- `_tick(..., draining=False)`: planning skips as described. Single-shot `tick()` and
  `wt queue tick` never drain.
- `StepPool.detach_all: bool` (under `lock`), honoured by `RunHooks.spawned()` /
  `enter_collect()`. `PersonaRunner.detachable` attribute: True for `ClaudeRunner`, False
  for `FakeRunner`.
- **CLI help.** `wt queue run`: "SIGINT/SIGTERM stop admissions, end running gates, and
  return. Persona runs keep running and the next `wt queue run` adopts them (SPEC-0225/0229)."
  `--once` / `--max-ticks`: "persona runs still running at exit are left running; the next
  `wt queue run` collects them."
- **`wt queue list` / watch.** A campaign whose open run has a live `proc.json` while no
  runner holds the lease shows `run: detached (pid N)`. It is read from the run dir, so
  scripted `--once` users can see what is still running. The watchdog's `runner_dead`
  (active campaigns, no lease) already fires in that situation.
- The conductor skill's restart guidance: "a runner restart no longer costs persona runs".

## Alternatives considered

- **`until_idle` waits for persona runs.** It never has to. `idle` is defined as "no
  non-parked campaign and no flight", so there is nothing to detach when it triggers.
- **`once` / `max_ticks` keep waiting for runs** (today's behaviour). Those flags exist for
  bounded runs, and blocking for up to 30 min on a run the next runner would adopt anyway
  works against them. Rejected. `stop` lists what was left, and `wt queue list` shows it.
- **A pending exec wins over `once` / `max_ticks`.** An exec'd `--once` runner would run
  another tick, which breaks the flag's promise. Rejected. The next start runs the new code
  anyway.
- **Keep planning FakeRunner steps while draining, with a drain cap.** A cap is an
  arbitrary number, and it still delays the exec. Not planning non-detachable steps
  converges by construction. Rejected.
- **Exec immediately on `code_changed`, abandoning in-process work.** That would cut off
  merges and pushes in the middle. Rejected. In-process work is bounded and short next to a
  persona run.
- **Unbounded joins on stop or crash.** A hung gate or spawn would hold the lease forever.
  Rejected.

## Acceptance criteria

- [ ] After a code change, while a gate is in flight (test `poll_interval_s: 1`): a ready
      QUEUED item is admitted and opened, and its implementer is submitted on the next
      draining tick. No gate or close starts after detection. With the gate held until the
      implementer is in `wait`, `_EXEC` is called within 0.5 s of the gate's harvest, the
      implementer is under `detached`, and the new process adopts it.
- [ ] With a FakeRunner (non-detachable) step in flight, a code-change drain plans no new
      persona step, waits for the current one, and execs. A FakeRunner queue's re-exec
      journal matches today's for `tests/test_agentic_reexec.py`.
- [ ] A stop set through `ctx.stop`, with a persona run in `wait` and a gate in flight: the
      gate's process group is killed through `_terminate_children()`, and the gate result is
      discarded. `run()` returns while the run's process is alive, `stop signal` lists the
      run under `detached`, and a new `run()` adopts and applies it.
- [ ] `--once` and `--max-ticks` exits detach wait-phase runs, and `run()` returns while they
      are alive. `--until-idle` behaves as today. If one of these fires during draining
      ticks, it is a plain exit with `restart_pending`.
- [ ] A crash raised inside `tick` with one task in `wait`, one in `spawning` (paused wrapper
      start) and a gate in flight: the gate group is killed, `run()` re-raises within the
      30 s bound, the lease is released, both runs' processes are alive, and the next
      `run()` adopts both. A run that exits during the sweep is collected by the next runner.
- [ ] A thread still stuck after the 30 s bound does not delay the lease release, and the
      CLI process exits (`os._exit(70)`) instead of hanging.
- [ ] Results harvested during draining ticks are journaled as `tick(drain)` records, a
      second merge during draining updates `restart.to_sha`, and `draining.waiting_on`
      lists only blockers (in-process work, `spawning`, `collect`).
- [ ] Every `stop` record carries `detached` (possibly empty). CLI help, `wt queue list`
      (`run: detached`) and the conductor skill rows reflect the new behaviour.
      `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Fixture:** SPEC-0225's `tests/fixtures/fake_claude.py` on `PATH` and its paused-wrapper
  helper. Gate commands are real subprocesses that write their pid to a file and sleep, so
  "the gate's group is killed" is checked by pid. Stops are driven by setting `ctx.stop`,
  which takes the same `_terminate_children()` path as `_handler`. Code changes come from
  the patched `code_changes`.
- **Automated tests:** new `tests/test_agentic_exit_paths.py`.
  - `test_draining_ticks_admit_and_exec_promptly` (`poll_interval_s: 1`): A is GATING with
    a held gate, a code change is detected, and B is ready. B is admitted, then its
    implementer starts on the next tick. No gate or close is started after detection. The
    gate is released once B's task is in `wait`, and `_EXEC` is called within 0.5 s of the
    harvest, with B's run under `detached`.
  - `test_fakerunner_drain_converges`: FakeRunner, a `sleep_s: 1` implementer in flight
    and a second campaign ready. After detection, no new persona step is planned (the
    second campaign may be admitted and opened), and `_EXEC` follows the harvest.
  - `test_stop_kills_gate_and_detaches_run`: `ctx.stop` is set. The gate pid is gone, the
    run's `child.pid` is alive, `stop signal detached` is written, and a second `run()`
    journals `adopt … running`.
  - `test_once_and_max_ticks_detach` (parametrized): `run()` returns in < 2 s with the run
    alive, and `stop` lists it. The variant fired during draining has `restart_pending` and
    no `_EXEC`.
  - `test_until_idle_unchanged`: FakeRunner e2e reaching idle gives the same journal as
    before.
  - `test_crash_with_gate_wait_and_spawning`: `tick` raises on its second call. The gate
    pid is gone, `run()` re-raises within 30 s, the lease file is released, both children
    are alive, and the next `run()` adopts both.
  - `test_crash_sweep_bound`: a `spawning` task paused past the bound (patched to 1 s).
    `run()` re-raises after about 1 s, `Q.leftover_threads()` is non-empty, and the next
    runner adopts the run once the wrapper claims it. The CLI's `os._exit` is checked with
    a patched `os._exit`.
  - `test_drain_journal_redetect_and_waiting_on`: the drain's harvests appear as
    `tick(drain)` records, a second patched merge updates `restart.to_sha`, and
    `lease.draining.waiting_on` never lists a `wait`-phase run.
  - `test_stop_record_detached_and_list_marker`: `detached` appears on every path, and
    `queue_payload` shows `run: detached` for a live run with no lease held.
- **Existing suites unchanged:** `tests/test_agentic_reexec.py`,
  `tests/test_agentic_nonblocking.py` (including its subprocess SIGTERM test),
  `tests/test_agentic_close.py`, `tests/test_agentic_gates.py`,
  `tests/test_agentic_heartbeat.py`, `tests/test_agentic_adopt.py` (SPEC-0225).
- **Sabotage checks:** run the suite after each of these (restoring in between) and confirm
  it fails:
  - plan gates while draining;
  - plan FakeRunner steps while draining;
  - drop the 0.2 s detach poll;
  - skip `_terminate_children` on a `ctx.stop`;
  - `shutdown(wait=True)`;
  - ignore `detach_all` in `spawned()`.
  Record the outcomes in the spec.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** with a gate running and an item ready, merge a code change.
  `wt queue runner-log --tail 8` shows the ready item admitted after the detection, then
  `stop code_changed detached=[…]` right after the gate result. A `kill -TERM` of the runner
  during a persona run leaves `ps` showing the run alive and the gate gone, and `wt queue
  run` adopts the run.

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
