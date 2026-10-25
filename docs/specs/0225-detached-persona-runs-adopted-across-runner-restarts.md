---
id: SPEC-0225
title: "Detached persona runs, adopted across runner restarts"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, queue, concurrency, reexec]
source_idea: IDEA-100
depends_on: [SPEC-0174, SPEC-0194, SPEC-0213, SPEC-0224]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md). This is
the durable fix for bug `IDEA-100`. The incident is described in
[SPEC-0224](./0224-honest-run-cost-on-interrupt-and-a-runner-draining.md), which also ships
honest cost on interrupt and the `runner_draining` signal.

Today a persona run is a child of the runner process that the runner waits on.
`ClaudeRunner.run` calls `subprocess.run(argv, capture_output=True, timeout=…)` inside a
`wt-step` worker thread (SPEC-0213 `StepPool`). The stdout exists only in memory until the
run ends. That has two consequences:

- **A code-change re-exec waits for every run.** The SPEC-0194 exec would kill the waiting
  threads and lose the result, so `_drain` joins every flight first. A 30-minute implementer
  holds admission for up to 30 minutes after every merge that touches `src/wt/agentic`, and
  on a busy queue most merges do.
- **Any runner stop kills the runs.** SIGTERM is forwarded to the process group (SPEC-0213
  `_handler`), so a restart costs every in-flight run, about $4 for an implementer. A runner
  crash leaves a `run.start` with no `run.end` forever. The next tick spawns a fresh run, and
  the watchdog's `_open_run` keeps reporting the dead one until `run_overdue` fires.

## Goals / Non-goals

**Goals**
- A persona run outlives the runner process: re-exec, signal stop, crash.
- A new runner process **adopts** the runs that are still open in the campaign ledgers
  (`run.start` with no `run.end`). It waits for the live ones and applies the finished ones.
  Each run is applied exactly once.
- A code-change drain waits only for in-process work (gate steps, and close steps once
  [SPEC-0220](./0220-close-regate-off-main-thread.md) moves them onto the pool), and
  admission continues while it waits.
- Run timeouts are enforced even when no runner is alive.

**Non-goals**
- Detaching gate or close steps. They run in-process. They are bounded by the gate time,
  and their subprocesses must die with the runner (SPEC-0220 discard rule).
- Killing runs on demand (a `wt queue kill-run`). The pgid is recorded, which is enough for
  a manual kill.
- Remote or other-host runs. Liveness is checked on the local host.
- FakeRunner detachment. FakeRunner stays in-thread, and its flights drain as today.

## Decision

1. **Detached spawn.** `ClaudeRunner` starts each run as a small wrapper process,
   `python -m wt.agentic.runproc <run_dir>`, in a new session
   (`Popen(start_new_session=True)`). The wrapper runs `claude -p` with stdin from
   `prompt.md` and stdout/stderr going directly to `result.json` / `stderr.log`. It enforces
   `timeout_s` (TERM to its group, 10 s grace, then KILL) and writes `exit.json` atomically
   when the child ends. The runner writes `request.json` and `launch.json` before the spawn,
   and `proc.json` (pid, pgid, process start time) durably right after it.
2. **Attach-or-spawn, and a runner protocol with hooks.** `PersonaRunner.run(req, *,
   hooks=None)`. `ClaudeRunner.run` is idempotent per run dir: if `run_dir/proc.json` exists
   it attaches, otherwise it spawns. Either way it then waits and collects, and collecting
   gives exactly today's `RunResult` and `run.json`. `hooks` (a `RunHooks`, defined below)
   lets the pool see the run's phase and release a waiting thread. `FakeRunner` accepts and
   ignores `hooks`. `_do_persona` changes in three ways only: it passes `hooks`, it handles
   the `DETACHED` outcome, and it takes `interrupted` from the run's outcome (Decision 6).
3. **Task phases.** Each persona task is in one of these phases:
   - `spawning`: from submission until `proc.json` is durable. Not detachable; it counts as
     in-process work;
   - `wait`: the run process is alive, or has exited and is not yet collected. Detachable;
   - `collect`: parse, `run.json`, the implementer's commit. Not detachable;
   - `done`.
   Every phase change, and every detach, happens under one `StepPool.lock`. A FakeRunner task
   never leaves `spawning`, so it is never detachable and drains as today.
4. **Draining ticks.** On `code_changed` the loop keeps ticking. It harvests, admits,
   opens, and plans and submits persona steps, but it plans no new gate or close step. Each
   iteration calls `pool.try_detach_all()`, which atomically, under the pool lock, checks
   that no in-process flight is left and no task is in `spawning` or `collect`, and only then
   detaches every remaining flight. When the call succeeds, `pool.flights` is empty, and the
   loop journals the detached runs and execs. No new in-process work starts, so that moment
   always comes.
5. **Signal stop and crash.** SIGINT/SIGTERM stops admission. SIGTERM is still forwarded to
   the runner's own process group, which kills gate subprocesses. Detached runs are in their
   own sessions and are not affected. `_drain` repeats harvest + `try_detach_all()` until
   `pool.flights` is empty. A crash detaches wait-phase tasks before `pool.shutdown`. In both
   cases the waiting threads return within 0.2 s, so `shutdown(wait=True)`, `lease.release()`
   and process exit are never held by a live run.
6. **`interrupted` is the run's own outcome.** A run is interrupted only when its process was
   killed by a signal that was not the wrapper's timeout (`exit.json.killed_by == "signal"`),
   or when adoption finds it orphaned. The runner's `stop` flag does not mark detachable runs
   as interrupted any more: a run that exits normally after a stop and before it is detached
   is collected and committed like any other. For non-detachable runners (FakeRunner) the
   old rule stays, `interrupted = ctx.stop.is_set()`, because their runs share the runner's
   fate.
7. **Adoption.** On the first tick of every pool, whether in a fresh process, after a
   re-exec, or in a single-shot `wt queue tick`, the runner scans campaigns for open runs
   that are not in this pool's flights, and handles each one by its files (see Design). The
   caller holds the lease, so no other process owns those runs. Adopted runs become ordinary
   flights, so harvest, `_fresh`, a park request and abandon apply as for any step.

## Design

### Run directory contract (`runs/NNN-<persona>/`)

| file | written by | when | content |
|---|---|---|---|
| `prompt.md` | runner | before spawn | as today |
| `request.json` | runner | before spawn | `{schema: wt.run.request.v1, persona, model, effort, max_budget_usd, timeout_s, permission_mode, read_only, cwd, json_schema, started_at}` |
| `launch.json` | runner | before spawn | `{argv, cwd, env}`, where `env` holds only the `req.env` additions (`WT_*`) |
| `proc.json` | runner | right after `Popen` | `{pid, pgid, start_ticks, host, started_at}`; temp file, `fsync`, `os.replace`, `fsync` of the dir; `start_ticks` = field 22 of `/proc/<pid>/stat`, or null off Linux |
| `result.json` | wrapper (child stdout) | streamed | raw vendor stdout, as today but written live |
| `stderr.log` | wrapper (child stderr) | streamed | as today |
| `exit.json` | wrapper | child ended | `{exit_code, timed_out, killed_by: null \| "timeout" \| "signal", signal, ended_at, duration_s}`, temp + `os.replace` |
| `run.json` | collector | collect phase | `wt.run.v1` (SPEC-0192), unchanged + SPEC-0224's `cost_estimated` |

- The wrapper merges `launch.json.env` over its inherited environment and ignores SIGHUP.
  On SIGTERM it forwards TERM to the child and still writes `exit.json` (`killed_by:
  "signal"`, `signal: 15`).
- **Liveness** (`runs.proc_alive(proc)`): `/proc/<pid>` exists, its state is not `Z`, and
  `start_ticks` matches. Off Linux: `os.kill(pid, 0)`. A start-time mismatch means the pid
  was reused, and the run counts as dead.
- **Zombies.** After an exec the wrappers are still children of the same pid. The collector
  calls `os.waitpid(pid, WNOHANG)` and ignores `ChildProcessError`. A zombie counts as dead,
  and `exit.json` already holds its outcome.
- **Spawn window.** The task stays in `spawning` from `_save_inputs` until `proc.json` is
  durable, and `hooks.spawned()` moves it to `wait` only after that. Neither an exec nor a
  stop can happen while a task is spawning, so every wrapper that exists has a `proc.json`,
  and adoption never sees a live run without one.

### Runner hooks and collect (`runner.py`)

- `RunHooks` (a small class owned by the pool, one per task):
  - `spawned()`: after `proc.json` is durable. Under the pool lock, phase `spawning` → `wait`;
  - `detached() -> bool`: reads the task's detach flag. No lock needed, since it is a set-once
    `threading.Event`;
  - `enter_collect() -> bool`: under the pool lock, returns False if the task is detached,
    otherwise sets phase `collect` and returns True.
- **Wait loop** (`ClaudeRunner._wait`): every 0.2 s it checks, in order:
  1. `hooks.detached()` → return `DETACHED`;
  2. `exit.json` exists, or the process is dead → if `hooks.enter_collect()` collect, else
     return `DETACHED`.
  A detached thread therefore returns within 0.2 s and never touches the run's files. With
  `hooks=None` (callers outside the queue, such as `spec_review`), the loop just waits and
  collects.
- `collect(run_dir) -> RunResult` reads `result.json` and runs `parse_result` with the schema
  from `request.json`. It reads `exit.json` for `exit_code`, `timed_out`, `duration_s` and
  `interrupted = killed_by == "signal"`. With no `exit.json` and a dead process ("vanished"),
  it uses `exit_code: -1`, `is_error: true`, `interrupted: false`, and `result_text` = the
  stderr tail + "run process vanished". The cost is whatever `result.json` carries,
  otherwise null, which SPEC-0224 turns into an estimated charge. `write_run_record` then
  runs as today. `collect` is idempotent, so collecting the same run dir twice gives the same
  result, which adoption relies on.

### Queue (`queue.py`)

- `_Flight.tasks` entries become `(Step, Future, RunHooks | None)`. Gate tasks have no hooks
  and count as in-process.
- `StepPool.lock` guards phases and `flights` membership. `StepPool.try_detach_all() ->
  list | None`, under the lock:
  - if any flight is in-process (a gate, or a close once SPEC-0220 lands), or any task is in
    `spawning` or `collect`, return None;
  - otherwise, for every flight, set each `wait` task's detach flag and drop the flight from
    `flights`. A flight's `done` tasks (finished reviewers whose group was not yet harvested)
    are dropped with it: their results are on disk (`exit.json`, `result.json`, `run.json`),
    and adoption re-collects them. Return `[{campaign, run_ids}]`.
  Because the check and the detach happen under the same lock as `enter_collect`, a task
  cannot enter `collect` between them. After a successful call `pool.flights` is empty.
- `_harvest` runs before `try_detach_all` in every loop iteration. A flight whose tasks are
  all done is therefore harvested, never detached. `_harvest` drops a `DETACHED` result. This
  is defensive, since detached flights are no longer in `flights`.
- **Fan-out flights.** A reviewer group is detachable only when each of its tasks is in
  `wait` or `done`. Adoption groups the campaign's open reviewer runs, whether finished or
  running, into one `_ReviewGroup`, so the group applies with every verdict.
- **Draining ticks.** `run()` no longer breaks out of the loop on `code_changed`. It sets
  the SPEC-0224 lease field (`lease.update_info(draining=…)`) and from then on calls
  `tick(..., inprocess=False)`. In `_tick` planning, `gate` / `close` steps are skipped when
  `inprocess` is false. They stay in their state and are planned by the new process. Each
  iteration, after the tick: `detached = pool.try_detach_all()`. If that is not None, the
  loop journals `tick(drain)` and `stop code_changed` with `detached`, then execs as today.
  A signal during draining ticks makes it a signal stop (no exec), as today. SPEC-0224's
  `runner_draining` still fires if this takes longer than 60 s (a long gate), and its
  `waiting_on` lists only in-process work.
- **Signal stop.** After `stop` is set, `_drain` loops: `wait_any(0.5)`, `_harvest`,
  `try_detach_all()`, until `pool.flights` is empty. `stop` records `detached`.
- **Crash path.** Before `pool.shutdown(cancel=True)`, the `finally` block sets the detach
  flag on every `wait` task, without the in-process check, so their threads return.
  `spawning` / `collect` tasks finish as today. The runs survive and the next runner adopts
  them.
- **Adoption** (`_adopt(cfg, ctx, pool, report)`, called by `tick` when `pool.adopted` is
  False, before `_harvest`). For each campaign not in MERGED/HANDED_OFF and not in
  `pool.ids()`, it finds every open run in the ledger (the same pairing as
  `events._open_run`, but returning all open runs). Each open run is classified by persona:
  - `implementer` → `Step("implement", persona="implementer")`;
  - `gate_triager` → `Step("triage", persona="gate_triager")`;
  - a persona in `personas.REVIEWER_PERSONAS` (today `conformance_reviewer` and
    `test_reviewer`, plus any reviewer added to that tuple later) → part of the campaign's
    reviewer group;
  - any other persona (`spec_reviewer`, a future resolver persona from SPEC-0221) →
    `unknown`: a synthetic `run.end` (`is_error: true`, `interrupted: false`, result text
    "open run of persona <p> cannot be adopted"), with the cost from `result.json` if
    present, otherwise estimated. State unchanged. It never raises out of the tick.

  and by its files:
  - **no `proc.json`**: spawned by pre-SPEC-0225 code, or the process died before spawning
    finished. The spawn-window rule above guarantees no live wrapper exists. It is
    `orphaned`, applied at once as a synthetic interrupted `RunResult` (`interrupted: true`,
    `cost_usd: null`, which SPEC-0224 marks estimated). The state is unchanged and the next
    plan respawns the step;
  - **`exit.json` present** → `finished`; **process alive** → `running`; **dead without
    `exit.json`** → `vanished`. For each, rebuild the `RunRequest` from `request.json` +
    `prompt.md`, take `budget` from `run.start.max_budget_usd`, and submit `_do_persona`
    as a flight with fresh hooks in phase `wait`. `ClaudeRunner.run` attaches because
    `proc.json` exists. A finished or vanished run collects at once;
  - **reviewer group:** the campaign's open reviewer runs (any of the three statuses) form
    one `_ReviewGroup` with `Step("review", personas=[those personas in profile order])` and
    `meta` from their `run.start`s. A reviewer whose run was never started is planned again
    by `_pending_reviewers` after the group applies.
  - Adoption runs before admission in the same tick, so adopted campaigns count as busy.
    It journals `adopt` with `runs: [{campaign, run_id, persona, status: running | finished
    | vanished | orphaned | unknown}]` and sets `pool.adopted = True`. A single-shot
    `wt queue tick` applies finished, vanished, orphaned and unknown runs, and waits for a
    live one, since a throwaway pool always waits.
- Applying is unchanged. An adopted flight's result goes through `_apply`. A campaign that
  was parked, abandoned or resumed meanwhile records `run.end` and cost, then skips the
  transition (`_fresh` → `superseded_by_human`). `park_requested` is honoured as today.
- **Unrecorded commit, for adopted runs only.** An adopted implementer flight carries
  `adopted=True` and `exited_before_start` (true when `exit.json.ended_at` is earlier than
  this process's start). Only for such a flight: if `commit_all` finds nothing to commit and
  the worktree HEAD differs from `state.repo.head`, `_do_persona` records HEAD as
  `commit_sha`. That is the crash-during-collect case, where the commit was made but its
  event was not. Fresh runs keep today's behaviour exactly, so an implementer that commits
  by itself, or a HEAD moved for another reason, emits no new `commit` event.

### Watchdog compatibility

`run.start.ts` is the true start of an adopted run, so `run_overdue`, `stuck_items` and
`runner_busy` work unchanged. The wrapper's timeout makes `run_overdue` a real anomaly again
(a wrapper that did not enforce its timeout).

### Existing tests

`tests/test_agentic_events.py`, `test_agentic_ping.py`, `test_agentic_dashboard.py` and
`test_agentic_campaign_runs.py` seed open `run.start` events, but none of them calls
`tick()` or `run()`, so adoption never sees those fixtures. The suites that do tick
(`test_agentic_nonblocking.py`, `test_agentic_reviewers.py`, `test_agentic_e2e.py`, …) use
FakeRunner and only read `run.start`. Their open runs are always in the pool's flights,
which adoption skips. A single-shot tick waits for its own runs, so it never leaves open
runs behind. No fixture changes are expected. If one turns up during implementation, it
gets recorded in the spec.

## Alternatives considered

- **(a) only: keep ticking on the old code until no flight is left, with a cap.** With
  several slots there may never be a moment with no flight. The cap brings back the full
  wait, and a signal stop still kills every run. It is part of the chosen design (draining
  ticks), but only for in-process work, which always runs out.
- **Pass the runs' pipes across `execve` (inheritable fds + a fd map in the env).** It only
  covers the exec case, not a signal stop or a crash. Threads die on exec, so waiting and
  timeouts would have to be rebuilt anyway. Files on disk cover every case.
- **Detect a live orphan by scanning for `wt.agentic.runproc <run_dir>` processes,** instead
  of the non-detachable spawning phase. It is a process-table scan on every adoption and
  fragile across platforms. The spawning phase removes the window instead. Rejected.
- **`shutdown(wait=False)` for detached flights** instead of releasing the waiters. Leaked
  threads would still block interpreter exit, and they could still write files after an
  exec. Rejected.
- **Detach with `nohup claude … &` and no wrapper.** Nothing would write the exit status or
  enforce the timeout while the runner is gone. Rejected.
- **A separate long-lived run supervisor daemon.** A second process with its own lifecycle
  and its own liveness problem. The per-run wrapper dies with its run. Rejected.
- **Detach FakeRunner too.** That would need a subprocess fake for every test. The real
  spawn path is tested with a fake `claude` executable instead (Test plan).

## Acceptance criteria

- [ ] `ClaudeRunner.run` spawns the wrapper in its own session and writes `request.json`,
      `launch.json`, a durable `proc.json` (before the task enters `wait`), a streamed
      `result.json`/`stderr.log` and an atomic `exit.json`. For a run that completes, the
      `RunResult` and `run.json` equal today's. The wrapper enforces `timeout_s` (TERM, 10 s,
      KILL) and records `timed_out: true`, `killed_by: "timeout"` with no runner alive.
- [ ] On a code change with a persona run in `wait` and no in-process work, the runner execs
      within one poll interval while the run's process is alive. At `_EXEC` no `wt-step`
      thread for that run is alive, and `stop code_changed` lists it under `detached`. The
      new process adopts it and applies it exactly once (one `run.end`) when it exits.
- [ ] While a gate step is in flight after a code change, ticks continue: a ready QUEUED
      item is admitted and opened, and its implementer is submitted. No new gate or close is
      submitted. The exec happens only after the gate is harvested and no task is in
      `spawning` or `collect`. A task entering `collect` concurrently delays the exec, and
      its commit is recorded once.
- [ ] A signal stop, and a crash raised inside `tick`, both return from `run()` (lease
      released) while a detached run's process is still alive, kill in-process gate
      subprocesses as before, and leave the run to a later `wt queue run`, which adopts it.
- [ ] An exec or stop requested while a task is in `spawning` waits until its `proc.json` is
      durable. A run dir never gets a second wrapper, and adoption never reports `orphaned`
      for a run whose wrapper is alive.
- [ ] Adoption classifies every open run: `running` (waited for and applied), `finished`
      (applied on the first tick), `vanished` or pid reused (`run.end` with `exit_code: -1`,
      `is_error`, cost from `result.json` or estimated), `orphaned` (`run.end` interrupted and
      estimated, state unchanged, step respawned), and `unknown` persona (`run.end` error, no
      crash). One `adopt` journal record lists each run with its persona and status.
- [ ] A reviewer fan-out is detachable only when every task is in `wait` or `done`. After
      adoption it is applied once as one group, with every reviewer's verdict, including
      reviewers that had finished before the detach.
- [ ] A campaign parked or abandoned while its run was detached gets `run.end` + cost and no
      transition (`superseded_by_human`). A `park_requested` campaign parks `human` after
      the adopted run applies.
- [ ] A ClaudeRunner run that exits normally after `stop` is set, but before it is detached,
      is collected with `interrupted: false` and its implementer work is committed. Only an
      adopted run that exited before the process started records an unrecorded HEAD as its
      commit, exactly once. Fresh runs never do.
- [ ] FakeRunner-based suites behave as before (FakeRunner tasks never leave `spawning`), `wt
      queue tick` adopts and applies finished runs, `uv run pytest` passes and
      `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Fixture:** `tests/fixtures/fake_claude.py`, an executable put on `PATH` as `claude` via
  `monkeypatch.setenv("PATH", …)`. It reads stdin and sleeps for `FAKE_CLAUDE_SLEEP_S`
  (env, set through `req.env`) or until a `release` file exists in its cwd. It prints a
  result JSON (`structured_output` from `FAKE_CLAUDE_VERDICT`, `total_cost_usd`) and can
  ignore SIGTERM (`FAKE_CLAUDE_STUBBORN`) to exercise the KILL step. The runner is
  `ClaudeRunner`, with no network involved.
- **Automated tests:**
  - `tests/test_agentic_runproc.py`:
    - `test_spawn_writes_contract_files`: all files exist, `exit.json` is valid, and the
      `RunResult` equals the parse of the printed JSON.
    - `test_wrapper_timeout_without_runner`: the runner thread is abandoned. The wrapper
      still writes `timed_out: true` / `killed_by: "timeout"`, and the stubborn child is
      killed after the grace.
    - `test_attach_existing_proc`: a second `run(req)` on the same run dir attaches and does
      not spawn (spawn counter).
    - `test_detached_waiter_returns_fast`: setting the detach flag makes `run()` return
      `DETACHED` within 0.5 s while the child is alive, and no files are written after that.
    - `test_proc_alive_pid_reuse`: wrong `start_ticks` → dead.
    - `test_vanished_run_collects_error`: the wrapper is killed with -9 → `exit_code -1`,
      `is_error`, `interrupted: false`, and an estimated cost.
    - `test_killed_by_signal_is_interrupted`: SIGTERM to the wrapper → `interrupted: true`.
  - `tests/test_agentic_adopt.py` (runner loop, tmp git repos, helpers from
    `tests/test_agentic_nonblocking.py`):
    - `test_reexec_does_not_wait_for_persona_run`: patched `_EXEC` records the call time, the
      live fake-claude pid and `threading.enumerate()`. The implementer is held by its
      `release` file. `_EXEC` is called while the pid is alive, and no `wt-step` thread is
      waiting on that run. Then `run()` is called again (simulating the exec'd process):
      `adopt` lists `running`. After the release there is exactly one `run.end`, and the
      campaign reaches GATING.
    - `test_draining_ticks_admit_but_no_gate`: A is GATING with a held gate command, and the
      code change is detected. B is ready. B is admitted, opened and its implementer
      submitted during the drain. No `started gate` appears after detection. `_EXEC` is
      called after A's gate is harvested.
    - `test_spawn_window_blocks_exec`: `Popen` is patched to pause (on an event) between the
      spawn and the `proc.json` write, while the code change is detected. `_EXEC` is not
      called until `proc.json` exists, and the adopting process reports `running`, not
      `orphaned`, with a single wrapper for the run dir.
    - `test_collect_race_delays_exec`: `git.commit_all` is patched to block, and the run
      exits just before `try_detach_all`. `_EXEC` waits for the commit, and the `commit`
      event is recorded once.
    - `test_signal_stop_leaves_runs_alive`: SIGTERM to the loop (test thread calls
      `_handler`). `run()` returns while the fake-claude pid is alive, and the `stop` record
      lists `detached`. A new `run()` adopts and applies the run.
    - `test_crash_leaves_runs_alive`: `tick` is patched to raise on the second call while a
      run is live. `run()` re-raises promptly, the lease is released and the pid is alive.
      A new `run()` adopts the run.
    - `test_run_finishing_after_stop_is_not_interrupted`: the run exits normally after `stop`
      is set, while `_drain` waits on a gate. `run.end.interrupted` is false and the
      implementer commit is recorded.
    - `test_adopt_statuses`: campaigns are pre-seeded with ledgers and run dirs for
      finished, vanished, orphaned, and unknown persona (`spec_reviewer`). One tick gives the
      right `adopt` statuses and `run.end` payloads. The orphan is respawned on the next tick,
      and the tick does not raise.
    - `test_adopt_review_group_with_finished_member`: a two-reviewer profile. One reviewer
      finished before the detach, one is still running. They are applied as one group with
      both verdicts.
    - `test_adopt_after_abandon_and_park_request`: `superseded_by_human`, and a `human`
      park, respectively.
    - `test_unrecorded_commit_only_for_adopted`: an adopted finished implementer with a
      worktree commit beyond `state.repo.head` records that sha once. A fresh run whose
      implementer committed by itself emits no extra `commit` event.
    - `test_queue_tick_adopts_finished`: single-shot `tick()` applies a finished open run.
- **Existing suites unchanged:** `tests/test_agentic_runner.py` (argv/parse tests adapted
  to the wrapper only where they patched `subprocess.run`, with the same assertions),
  `tests/test_agentic_reexec.py`, `tests/test_agentic_nonblocking.py`,
  `tests/test_agentic_heartbeat.py`, `tests/test_agentic_events.py`,
  `tests/test_agentic_ping.py`, `tests/test_agentic_dashboard.py`,
  `tests/test_agentic_campaign_runs.py`, `tests/test_agentic_e2e.py` (see *Existing tests*).
- **Sabotage checks:** run the adopt suite after each of these (restoring in between) and
  confirm it fails:
  - spawn without `start_new_session`;
  - allow detach in `spawning`;
  - allow detach in `collect`;
  - split `try_detach_all`'s check from its detach;
  - ignore the detach flag in the wait loop;
  - skip adoption;
  - plan gates during draining ticks.
  Record the outcomes in the spec.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** with a live implementer running, merge a docs-plus-code change.
  `wt queue runner-log --tail 5` shows `stop code_changed detached=[…]` within one poll
  interval, then `start` and `adopt … running`. `ps -o pid,sid,cmd` shows the
  `wt.agentic.runproc` wrapper in its own session. The run's `run.end` appears once, after
  it finishes.

## Rollout / migration

The first runner on this code (after the SPEC-0194 re-exec from the previous code) finds
no `proc.json` for runs the old code started. The old code's drain waited for all of them,
so normally there are none. Any left by a crash are applied as orphaned (Design). From then
on, every run is detached. No state migration is needed. Ledger and journal additions: the
`adopt` journal records, and the `detached` field on `stop`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
