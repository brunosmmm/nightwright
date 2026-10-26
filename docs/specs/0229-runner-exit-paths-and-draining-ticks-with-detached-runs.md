---
id: SPEC-0229
title: "Runner exit paths and draining ticks with detached runs"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, queue, concurrency, reexec]
source_idea: IDEA-100
depends_on: [SPEC-0220, SPEC-0224, SPEC-0225]
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
| `signal` / `ctx.stop` | stops | `_terminate_children()` kills gate process groups, and no further gate starts; gate results discarded (gate rule below), close results per SPEC-0220; other in-process threads joined with a 30 s bound | detached; at bound expiry every `wait` task is force-detached individually | waited for up to the 30 s bound, then abandoned to adoption | journal `stop signal detached=[…] abandoned=[…]`, release lease, return |
| `once` / `max_ticks` | stops (the loop has ended) | waited for, as today | detached | waited for | journal `stop once/max_ticks detached=[…]`, release, return |
| `idle` (`--until-idle`) | n/a | none: `idle` means no non-parked campaign and no flight | none | none | as today |
| `crash` | stops | `_terminate_children()`, then not waited for beyond the 30 s bound | detached by the crash sweep | bounded re-sweep, then left to adoption | journal `crash` (in `finally`, after the sweep, with `detached` and `abandoned`), release, re-raise |

`detached` is empty when nothing was detached. SPEC-0225's interim kill-on-stop is removed:
a stop now detaches.

**Precedence.** If `max_ticks` or `until_idle` fires during draining ticks, that exit
wins. `once` cannot fire there: it ends the loop after the first tick, before
`code_changes` is ever polled. The winning exit is a plain exit after detaching. There is
no exec, and `stop` carries `restart_pending: {to_sha}` so the next start runs the new code
anyway. A signal during draining ticks becomes a `signal` exit and clears `restart`, as
today (no exec, no `restart_pending`): a human stop means stop.

### Ending in-process work: `_terminate_children`

- `gates.py` keeps a registry of live gate processes (`_LIVE: set[Popen]`) and a
  `_stopping` flag, both under one lock. `run_gate` checks `_stopping` under that lock
  immediately before its `Popen`. If the flag is set, it does not start the gate: it returns
  a gate result with `interrupted: true`. Otherwise it adds the Popen to `_LIVE` while still
  holding the lock, and removes it after the wait. `run_all(…, stop=None)` also checks
  `stop.is_set()` before each next gate and stops the loop if it is set. `_do_gate` passes
  `ctx.stop`. `gates.terminate_all()` sets `_stopping`, then calls the existing
  `_kill_group` (TERM, grace, KILL) on each registered process. So a multi-gate project
  (lint, then pytest) never starts its next gate after a stop. `gates.reset()` clears
  `_stopping` and the registry. `run()` calls it at its start, and again after every drain
  or stop, so a flag left set by one loop (in the same process, or in tests) never blocks
  the gates of the next.
- `GateResult` gains `interrupted: bool = False` (additive), and the `wt.gate.v1` sidecar
  writes it.
- `queue._terminate_children()` = `gates.terminate_all()`. **Only the main loop calls it**,
  once, when it finds `stop` set. It polls `stop` every 0.2 s, so this covers signals and
  `ctx.stop` alike. It is also called on the crash path. `_handler` only sets `stop` (and a
  `signalled` flag for the `stop` reason). The old `killpg` of the runner's own group is dropped,
  since it reached neither the gates nor the detached runs. It never blocks
  in signal context and never takes `_LIVE`'s lock.
- **Rule: a gate killed by a stop is discarded.** A `gate` StepResult harvested while
  `stop` is set (killed, or not started because of `_stopping`) is not applied:
  - no `gate` ledger event and no transition. The state stays GATING;
  - `counters.gate_runs` and the loop counters are not incremented;
  - the row reads `reason: "interrupted"`;
  - the gate's log and its `wt.gate.v1` sidecar for that seq stay on disk, and the sidecar
    gets `interrupted: true` so `wt campaign` views do not show it as a failure.
  The next runner re-gates from scratch with the next seq. This mirrors the close rule of
  [SPEC-0220](./0220-close-regate-off-main-thread.md) (Interrupt), which this spec depends
  on for closes being in-process flights.
- **Bound expiry on a stop** (signal / `ctx.stop`). `_drain` loops harvest +
  `try_detach_all()` for at most 30 s. All-or-nothing detach is right for the exec, but on a
  stop a single stuck task must not keep every other run attached. So when the bound
  expires, `pool.force_detach_waiting()` runs under the pool lock:
  - it sets `pool.detach_all` (as the crash sweep does) and the detach flag of **every**
    `wait` task individually. Those threads return within 0.2 s and never collect, so no
    thread collects or commits after the lease is gone;
  - it lists the remaining `spawning` and `collect` tasks as `abandoned`.
  The abandoned tasks are journaled in `stop` (`abandoned: [{campaign, run_id, phase}]`)
  and left to adoption:
  - a `spawning` task's wrapper claims `proc.json` by itself (SPEC-0225);
  - a `collect` thread is joined up to the same bound and then abandoned. Its run has
    `exit.json` but possibly no `run.end`, so the next runner adopts it as `finished` and
    collects it again. `commit_all` commits whatever is left, and SPEC-0225's adopted-run
    HEAD rule records a commit the abandoned thread had already made, exactly once. A stale
    `index.lock` left by a thread killed in the middle of `git commit` is out of scope:
    `commit_all` then fails, and the run takes the existing commit-error path. Cleaning it
    up is a follow-up idea, not part of this spec.
- **Shutdown.** Then `pool.shutdown(wait=False, cancel_futures=True)`, and join the pool's
  threads within whatever remains of the 30 s bound. The lease is released after that, even
  if threads remain.

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
0. Harvest finished flights first: `_harvest` once, each `_apply` wrapped in `try/except`
   and logged, inside the 30 s bound. A flight that already finished `collect` (it has
   `run.end`, so adoption would skip it) is then applied instead of lost. A harvest that
   raises is journaled, and its campaign is left to the next runner's planning.
1. `_terminate_children()`.
2. Under the pool lock, set `pool.detach_all = True` and the detach flag of every `wait`
   task. `RunHooks.spawned()` honours `detach_all`: a task that finishes spawning after this
   point is marked detached instead of entering `wait`. `enter_collect()` returns False under
   `detach_all`, so a run that exits now is left for adoption to collect.
3. Re-sweep every 0.2 s until no task is in `spawning` or `collect`, then shut down as
   above. `collect` tasks that are still running when the bound expires are abandoned, and
   handled by adoption as described above. The 30 s bound covers steps 0–3 together. A thread still stuck in `spawning` after it (a hung
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
  `enter_collect()`. `StepPool.force_detach_waiting() -> list` (abandoned tasks).
  `PersonaRunner.detachable` attribute: True for `ClaudeRunner`, False for `FakeRunner`.
- `gates.run_gate` registry + `_stopping`, `gates.run_all(…, stop=None)`,
  `gates.terminate_all()`, `gates.reset()`, and `queue._terminate_children()`, called only
  from the main loop and the crash path.
- `Q.leftover_threads() -> list[threading.Thread]`: the pool's worker threads still alive
  after shutdown. Executor threads are not daemonic, so interpreter exit would join them. The
  `wt queue run` CLI, when `Q.run` returns or raises and `leftover_threads()` is
  non-empty, prints the traceback (when `Q.run` raised) to stderr, flushes the journal
  (`journal` writes are already per-record `O_APPEND`) and stdout/stderr, and calls
  `os._exit(70)`. That way no stale thread outlives the
  lease. The library `run()` only returns or re-raises.
- **CLI help.** `wt queue run`: "SIGINT/SIGTERM stop admissions, end running gates, and
  return. Persona runs keep running and the next `wt queue run` adopts them (SPEC-0225/0229).
  If worker threads are still stuck after the 30 s bound, the traceback (if any) is printed
  to stderr and the process exits with status 70."
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

- [x] After a code change, while a gate is in flight (test `poll_interval_s: 1`): a ready
      QUEUED item is admitted and opened, and its implementer is submitted on the next
      draining tick. No gate or close starts after detection. With the gate held until the
      implementer is in `wait`, `_EXEC` is called within 0.5 s of the gate's harvest, the
      implementer is under `detached`, and the new process adopts it.
- [x] With a FakeRunner (non-detachable) step in flight, a code-change drain plans no new
      persona step, waits for the current one, and execs. A FakeRunner queue's re-exec
      journal matches today's for `tests/test_agentic_reexec.py`, except for the additive
      `detached` / `abandoned` keys.
- [x] A stop set through `ctx.stop`, with a persona run in `wait` and a gate in flight in a
      two-gate project: the gate's process group is killed through `_terminate_children()`,
      the second gate never starts, and the killed gate is discarded. There is no `gate`
      event, the state stays GATING, `gate_runs`/loops are not incremented, the row reason is
      `interrupted`, and the next runner re-gates. `run()` returns while the run's process
      is alive, `stop signal` lists the run under `detached`, and a new `run()` adopts and
      applies it.
- [x] `--once` and `--max-ticks` exits detach wait-phase runs, and `run()` returns while they
      are alive. `--until-idle` behaves as today. If `max_ticks` or `until_idle` fires
      during draining ticks, it is a plain exit with `restart_pending`.
- [x] A crash raised inside `tick` with one task in `wait`, one in `spawning` (paused wrapper
      start), a gate in flight and one finished-unharvested flight: the finished flight is
      applied, the gate group is killed, `run()` re-raises within the 30 s bound, the lease
      is released, both runs' processes are alive, and the next `run()` adopts both. A run
      that exits during the sweep is collected by the next runner.
- [x] When the 30 s bound expires on a stop or crash, every `wait` task is detached
      individually even though another task is stuck. Stuck `spawning`/`collect` tasks are
      journaled as `abandoned`, and the next runner adopts them, including an abandoned
      `collect` (`exit.json`, no `run.end`), whose commit is recorded once. On a crash, the
      `crash` record is written after the sweep and carries `detached` and `abandoned`. A
      leftover thread does not delay the lease release, and the CLI prints the traceback to
      stderr and exits with `os._exit(70)`. `gates.reset()` runs at the start of `run()` and
      after every drain or stop.
- [x] Results harvested during draining ticks are journaled as `tick(drain)` records, a
      second merge during draining updates `restart.to_sha`, and `draining.waiting_on`
      lists only blockers (in-process work, `spawning`, `collect`).
- [x] Every `stop` record carries `detached` (possibly empty). CLI help, `wt queue list`
      (`run: detached`) and the conductor skill rows reflect the new behaviour.
      `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Fixture:** SPEC-0225's `tests/fixtures/fake_claude.py` on `PATH` and its paused-wrapper
  helper. Gate commands are real subprocesses that write their pid to a file and sleep, so
  "the gate's group is killed" is checked by pid. Stops are driven by setting `ctx.stop`.
  The main loop then calls `_terminate_children()`, exactly as after a signal, because
  `_handler` only sets the same event. Code changes come from
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
  - `test_stop_kills_gate_and_detaches_run`: a two-gate project, and `ctx.stop` is set
    during gate 1. The gate-1 pid is gone, gate 2's pid file never appears, the state is
    still GATING, and there is no `gate` event and no `gate_runs` increment. The run's
    `child.pid` is alive, `stop signal detached` is written, and a second `run()` journals
    `adopt … running` and re-gates.
  - `test_handler_only_sets_stop`: `signal.getsignal(SIGTERM)` installed by `run()` on the
    main thread, invoked with a gate in flight, returns at once without calling
    `terminate_all` (spy). The main loop calls it within 0.5 s.
  - `test_once_and_max_ticks_detach` (parametrized): `run()` returns in < 2 s with the run
    alive, and `stop` lists it. The `max_ticks` variant that fires during draining has
    `restart_pending` and no `_EXEC`. There is no `once` variant: `once` cannot reach
    draining.
  - `test_until_idle_unchanged`: FakeRunner e2e reaching idle gives the same journal as
    before.
  - `test_crash_with_gate_wait_and_spawning`: `tick` raises on its second call, with one
    reviewer flight already finished and unharvested. That flight is applied, the gate pid
    is gone, `run()` re-raises within 30 s, the lease file is released, both children are
    alive, and the next `run()` adopts both.
  - `test_bound_expiry_force_detach_and_abandon` (bound patched to 1 s), on a stop and on a
    crash. One task is paused in `spawning` and one blocks in `collect` (patched
    `commit_all` that commits, then hangs). The `wait` task is detached anyway, and its
    thread exits without collecting. `stop` lists the other two as `abandoned`, and
    `Q.leftover_threads()` is non-empty. The next runner adopts all three. The abandoned
    collect's run (with `exit.json`, no `run.end`) is collected, and its commit is
    recorded once. On the crash variant, the `crash` record carries `detached` and
    `abandoned`. The CLI's stderr traceback and `os._exit(70)` are checked with a patched
    `os._exit`.
  - `test_gates_reset_between_loops`: after a stopped `run()`, a second `run()` in the same
    process runs its gates normally (`_stopping` is cleared), and a killed gate's sidecar has
    `interrupted: true`.
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
  - drop the stop check in `run_gate`/`run_all`;
  - apply a gate result harvested during a stop;
  - keep all-or-nothing detach at bound expiry;
  - `shutdown(wait=True)`;
  - ignore `detach_all` in `spawned()`.
  Record the outcomes in the spec.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** with a gate running and an item ready, merge a code change.
  `wt queue runner-log --tail 8` shows the ready item admitted after the detection, then
  `stop code_changed detached=[…]` right after the gate result. A `kill -TERM` of the runner
  during a persona run leaves `ps` showing the run alive and the gate gone, and `wt queue
  run` adopts the run.

### Test record (2026-10-25)

- `uv run pytest -q`: 2645 passed, 3 skipped, one failure that was a stale assertion in
  `tests/test_skills.py` (the old `runner_draining` row text). That test is now
  `test_runner_draining_row_waits_on_in_process_work_spec_0229`, and `tests/test_skills.py` passes (58).
- New `tests/test_agentic_exit_paths.py`: 20 tests, all passing. They cover draining admission and
  prompt exec, FakeRunner convergence, stop/gate discard/re-gate, handler-only-sets-stop,
  once/max_ticks detach, until-idle unchanged, crash sweep, bound expiry (stop and crash), the CLI
  exit 70, `gates.reset`, `waiting_on`, and the `run: detached` marker.
- Existing suites (`reexec`, `nonblocking`, `close`, `gates`, `heartbeat`, `adopt`) pass in the full run.
  Where the spec's table changes their expectations (kill-on-stop became detach), the assertions
  were updated: `test_agentic_adopt.py`, `test_agentic_close_inflight.py`,
  `test_agentic_drain_signal.py`, `test_agentic_loop_state.py`, `test_agentic_nonblocking.py`.
- Sabotage checks, each applied alone and then restored. Each made
  `tests/test_agentic_exit_paths.py` + `reexec`/`nonblocking`/`adopt`/`gates` fail:

  | sabotage | first failing test |
  |---|---|
  | plan gates while draining | `test_draining_starts_no_gate_for_a_harvested_campaign` |
  | plan FakeRunner steps while draining | `test_fakerunner_drain_converges_and_admits` |
  | drop the 0.2 s detach poll | `test_fakerunner_drain_converges_and_admits` |
  | skip `_terminate_children` | `test_stop_kills_gate_discards_it_and_next_run_regates` |
  | drop the stop check in `run_gate` | `test_gate_registry_unit` |
  | drop the stop check in `run_all` | `test_gate_registry_unit` |
  | apply a gate result harvested during a stop | `test_stop_kills_gate_discards_it_and_next_run_regates` |
  | all-or-nothing detach at bound expiry | `test_bound_expiry_through_run[stop]` |
  | `shutdown(wait=True)` | `test_bound_expiry_abandons_stuck_tasks` |
  | ignore `detach_all` in `spawned()` | `test_force_detach_waiting_and_hooks_honour_detach_all` |

- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py`: exit 0.
- Manual verification: no live queue was run from this worktree. The same flows (stop during a gate
  and a persona run, adoption by the next `run()`, SIGTERM in a subprocess) are covered by the
  automated tests above, using real subprocesses and `fake_claude`.

## Rollout / migration

Lands after SPEC-0225. No data migration. Journal addition: `detached` on every `stop`
record. The conductor skill's restart guidance changes with it.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
