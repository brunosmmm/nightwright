---
id: SPEC-0224
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
depends_on: [SPEC-0174, SPEC-0194, SPEC-0213, SPEC-0223]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md). This is
the durable fix for bug `IDEA-100`. The incident is described in
[SPEC-0223](./0223-honest-run-cost-on-interrupt-and-a-runner-draining.md), which also ships
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
   when the child ends. The runner records `proc.json` (pid, pgid, process start time) and
   `request.json` (what collecting the result needs) before it starts waiting.
2. **Attach-or-spawn.** `ClaudeRunner.run(req)` is idempotent per run dir. If
   `run_dir/proc.json` exists, it attaches: it waits for `exit.json` or for the process to
   die, then collects. Otherwise it spawns. Collecting gives exactly today's `RunResult`
   and `run.json`. `_do_persona`, including the implementer's commit, is unchanged, so an
   adopted run goes through the same code as a fresh one.
3. **Two-phase flights.** A persona flight is in its *wait* phase until the run exits, and
   in its *collect* phase (parse, `run.json`, implementer commit) after that. A flight can
   be detached only in the wait phase. The switch happens under a per-flight lock, so a
   flight is never detached halfway through a commit.
4. **Draining ticks.** On `code_changed` the loop keeps ticking. It harvests, admits,
   opens, and plans and submits persona steps, but it plans no new gate or close step. Once
   no in-process flight and no collect-phase flight is left, it detaches the wait-phase
   persona flights, journals them, and execs. No new in-process work starts, so that moment
   always comes.
5. **Signal stop.** SIGINT/SIGTERM stops admission and drains in-process flights as today.
   SIGTERM is still forwarded to the runner's own process group, which kills gate
   subprocesses, but detached runs are in their own sessions and keep running. Wait-phase
   persona flights are detached. The `stop` record lists them.
6. **Adoption.** On its first tick, every runner process, whether fresh or re-exec'd, scans
   non-terminal and ABANDONED campaigns for open runs and handles each one by its files (see
   Design). Live runs become ordinary flights, so harvest, `_fresh`, a park request and
   abandon apply as for any step.

## Design

### Run directory contract (`runs/NNN-<persona>/`)

| file | written by | when | content |
|---|---|---|---|
| `prompt.md` | runner | before spawn | as today |
| `request.json` | runner | before spawn | `{schema: wt.run.request.v1, persona, model, effort, max_budget_usd, timeout_s, permission_mode, read_only, cwd, json_schema, started_at}` |
| `proc.json` | runner | right after `Popen` | `{pid, pgid, start_ticks, host, started_at}`; `start_ticks` = field 22 of `/proc/<pid>/stat`, or null off Linux |
| `result.json` | wrapper (child stdout) | streamed | raw vendor stdout, as today but written live |
| `stderr.log` | wrapper (child stderr) | streamed | as today |
| `exit.json` | wrapper | child ended | `{exit_code, timed_out, signal, ended_at, duration_s}`, temp + `os.replace` |
| `run.json` | collector | collect phase | `wt.run.v1` (SPEC-0192), unchanged + SPEC-0223's `cost_estimated` |

- `runproc` reads its argv, cwd and env from a `launch.json` that the runner writes next to
  `request.json`. The env holds only the `req.env` additions (`WT_*`); the wrapper merges
  them over its inherited environment. The wrapper ignores SIGHUP. On SIGTERM it forwards
  TERM to the child and still writes `exit.json` (`signal: 15`).
- **Liveness** (`runs.proc_alive(proc)`): `/proc/<pid>` exists, its state is not `Z`, and
  `start_ticks` matches. Off Linux: `os.kill(pid, 0)`. A start-time mismatch means the pid
  was reused, and the run counts as dead.
- **Zombies.** After an exec the wrappers are still children of the same pid. The collector
  calls `os.waitpid(pid, WNOHANG)` and ignores `ChildProcessError`. A zombie counts as dead,
  and `exit.json` already holds its outcome.

### Collect (`runner.py`)

`ClaudeRunner.collect(req_or_meta, run_dir) -> RunResult` reads `result.json` and runs
`parse_result` with the schema from `request.json`. It reads `exit.json` for `exit_code`,
`timed_out` and `duration_s`. With no `exit.json` and a dead process ("vanished"), it uses
`exit_code: -1`, `is_error: true`, and `result_text` = the stderr tail + "run process
vanished". The cost is whatever `result.json` carries, otherwise null, which SPEC-0223 turns
into an estimated charge. `write_run_record` then runs as today.

### Queue (`queue.py`)

- `_Flight` gains `lock`, `phase` (`wait` | `collect`) and `detached: bool`. `_do_persona`
  takes an `on_exit` callback that the runner calls between its wait and collect. Under
  `fl.lock`: if `fl.detached`, the worker returns a sentinel `DETACHED` and does nothing
  more; otherwise it sets `phase = "collect"`. `StepPool.detach_waiting()`, under each lock,
  marks every wait-phase persona flight `detached`, removes it from `flights` without
  harvesting, and returns `[{campaign, run_ids}]`. `_harvest` drops a `DETACHED` result.
  This is defensive, since detached flights are no longer in `flights`. FakeRunner flights
  have no `on_exit` and are never detachable. They drain as today.
- **Draining ticks.** `run()` no longer breaks out of the loop on `code_changed`. It sets
  `draining = "code_changed"` (SPEC-0223 lease field), and from then on calls
  `tick(..., inprocess=False)`. In `_tick` planning, `gate` / `close` steps are skipped when
  `inprocess` is false. They stay in their state and are planned by the new process.
  Everything else runs as normal. Each loop iteration checks
  `pool.inprocess_or_collecting()`. When it is empty, the loop calls `detach_waiting()`,
  journals `tick(drain)` and `stop code_changed` with `detached: [...]`, and execs as
  today. A signal during draining ticks makes it a signal stop (no exec), as today.
  SPEC-0223's `runner_draining` still fires if this takes longer than 60 s (a long gate). Its
  `waiting_on` then lists only in-process flights.
- **Signal stop.** After `stop` is set, the existing `_drain` waits for in-process and
  collect-phase flights only, then calls `detach_waiting()`. `stop` records `detached`. The
  forwarded SIGTERM still reaches gate subprocesses (same process group) and never the runs
  (own sessions).
- **Crash path.** `pool.shutdown(cancel=True)` is unchanged. The runs survive, and the next
  runner adopts them.
- **Adoption** (`_adopt(cfg, ctx, pool, report)`, called by `tick` when
  `pool.adopted is False`, before `_harvest`). For each campaign not in
  MERGED/HANDED_OFF, open runs are found in its ledger, the same pairing as
  `events._open_run` but returning all open runs. For each open run:
  - **no `proc.json`** (spawned by pre-SPEC-0224 code, or a crash before spawn): orphaned.
    It is applied at once as a synthetic interrupted `RunResult` (`interrupted: true`,
    `cost_usd: null`, which SPEC-0223 marks estimated). The state is unchanged and the next
    plan respawns the step;
  - **live or finished** (`exit.json` exists, or the process is alive, or it vanished):
    rebuild the `RunRequest` from `request.json` + `prompt.md`, take `budget` from
    `run.start.max_budget_usd`, and the `Step` from the persona: `implementer` → implement,
    triager → triage, reviewer → review. Then submit `_do_persona` as a flight. For a
    finished run it returns at once, and `ClaudeRunner.run` attaches because `proc.json`
    exists;
  - **reviewer fan-out:** the campaign's open reviewer runs are grouped into one
    `_ReviewGroup` with `Step("review", personas=[open personas in profile order])` and
    `meta` from their `run.start`s. A reviewer whose run was never started is planned again
    by `_pending_reviewers` after the group applies.
  - Adoption runs before admission in the same tick, so adopted campaigns count as busy.
    It journals `adopt` with `runs: [{campaign, run_id, status: running | finished |
    vanished | orphaned}]` and sets `pool.adopted = True`. `wt queue tick` (single shot)
    adopts too: a finished or orphaned run is applied, and a live one is waited for, as a
    throwaway pool always waits.
- Applying is unchanged. An adopted flight's result goes through `_apply`. A campaign that
  was parked, abandoned or resumed meanwhile records `run.end` and cost, then skips the
  transition (`_fresh` → `superseded_by_human`). `park_requested` is honoured as today.
- **Implementer commit on adoption.** If `commit_all` finds nothing to commit but the
  worktree HEAD differs from `state.repo.head`, the commit was made but never recorded (a
  crash during the collect phase). `_do_persona` then records HEAD as `commit_sha`, so the
  `commit` event is never lost and never doubled.

### Watchdog compatibility

`run.start.ts` is the true start of an adopted run, so `run_overdue`, `stuck_items` and
`runner_busy` work unchanged. The wrapper's timeout makes `run_overdue` a real anomaly again
(a wrapper that did not enforce its timeout).

## Alternatives considered

- **(a) only: keep ticking on the old code until no flight is left, with a cap.** With
  several slots there may never be a moment with no flight. The cap brings back the full
  wait, and a signal stop still kills every run. It is part of the chosen design (draining
  ticks), but only for in-process work, which always runs out.
- **Pass the runs' pipes across `execve` (inheritable fds + a fd map in the env).** It only
  covers the exec case, not a signal stop or a crash. Threads die on exec, so waiting and
  timeouts would have to be rebuilt anyway. Files on disk cover every case.
- **Detach with `nohup claude … &` and no wrapper.** Nothing would write the exit status or
  enforce the timeout while the runner is gone. Rejected.
- **A separate long-lived run supervisor daemon.** A second process with its own lifecycle
  and its own liveness problem. The per-run wrapper dies with its run. Rejected.
- **Detach FakeRunner too.** That would need a subprocess fake for every test. The real
  spawn path is tested with a fake `claude` executable instead (Test plan).

## Acceptance criteria

- [ ] `ClaudeRunner.run` spawns the wrapper in its own session and writes `request.json`,
      `proc.json`, a streamed `result.json`/`stderr.log` and an atomic `exit.json`. For a run
      that completes, the `RunResult` and `run.json` equal today's. The wrapper enforces
      `timeout_s` (TERM, 10 s, KILL) and records `timed_out: true` with no runner alive.
- [ ] On a code change with a persona run in flight and no in-process step, the runner execs
      within one poll interval and does not wait for the run. The run keeps going. The `stop
      code_changed` record lists it under `detached`. The new process adopts it, applies it
      once (exactly one `run.end`) when it exits, and the campaign moves on.
- [ ] While a gate step is in flight after a code change, ticks continue: a ready QUEUED
      item is admitted and opened, and its implementer is submitted. No new gate or close is
      submitted. The exec happens once the gate is harvested.
- [ ] SIGTERM to the runner leaves detached runs running (process still alive after the
      runner exits), kills in-process gate subprocesses as before, and the `stop` record
      lists the detached runs. A later `wt queue run` adopts them.
- [ ] Adoption handles every open run by its files. Live → waited for and applied.
      `exit.json` present → applied on the first tick. Dead without `exit.json` (vanished),
      or `start_ticks` mismatch (pid reuse) → `run.end` with `exit_code: -1`, `is_error`, and
      cost from `result.json` or estimated. No `proc.json` → `run.end` interrupted and
      estimated, state unchanged, step respawned. One `adopt` journal record lists each run
      with its status.
- [ ] An adopted reviewer fan-out is applied once as one group, with every adopted reviewer's
      verdict.
- [ ] A campaign parked or abandoned while its run was detached gets `run.end` + cost and no
      transition (`superseded_by_human`). A `park_requested` campaign parks `human` after
      the adopted run applies.
- [ ] A flight in its collect phase is never detached: an exec or stop waits for the
      implementer's commit. An adopted finished implementer run whose commit was made but
      not recorded records HEAD as its commit exactly once.
- [ ] FakeRunner-based suites behave as before (FakeRunner flights drain and are never
      detached), and `wt queue tick` adopts and applies finished runs.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

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
      still writes `timed_out: true`, and the stubborn child is killed after the grace.
    - `test_attach_existing_proc`: a second `run(req)` on the same run dir attaches and does
      not spawn (spawn counter).
    - `test_proc_alive_pid_reuse`: wrong `start_ticks` → dead.
    - `test_vanished_run_collects_error`: the wrapper is killed with -9 → `exit_code -1`,
      `is_error`, and an estimated cost.
  - `tests/test_agentic_adopt.py` (runner loop, tmp git repos, helpers from
    `tests/test_agentic_nonblocking.py`):
    - `test_reexec_does_not_wait_for_persona_run`: patched `_EXEC` records the call time. The
      implementer is held by its `release` file. `_EXEC` is called while the fake claude pid
      is alive. Then `run()` is called again (simulating the exec'd process): `adopt` lists
      `running`. After the release, exactly one `run.end`, and the campaign reaches GATING.
    - `test_draining_ticks_admit_but_no_gate`: A is GATING with a held gate command, and the
      code change is detected. B is ready. B is admitted, opened and its implementer
      submitted during the drain. No `started gate` appears after detection. `_EXEC` is
      called after A's gate is harvested.
    - `test_signal_stop_leaves_runs_alive`: SIGTERM to the loop (test thread calls
      `_handler`). The fake claude pid is alive after `run()` returns, and the `stop` record
      lists `detached`. A new `run()` adopts and applies the run.
    - `test_adopt_finished_vanished_orphaned`: three campaigns are pre-seeded with ledgers
      and run dirs: `exit.json` present, dead pid without `exit.json`, and no `proc.json`.
      One tick: the statuses in `adopt` are right, the `run.end` payloads are right, and the
      orphan's state is unchanged and respawned on the next tick.
    - `test_adopt_review_group`: two open reviewer runs (two-reviewer profile) are applied as
      one group, with one `review` transition decision.
    - `test_adopt_after_abandon_and_park_request`: `superseded_by_human`, and a `human`
      park, respectively.
    - `test_collect_phase_not_detached`: `on_exit` blocks inside the collect phase (patched
      `git.commit_all` waits on an event) while the code change is detected. `_EXEC` is not
      called until the commit returns, and the `commit` event is recorded once.
    - `test_adopt_records_unrecorded_commit`: the worktree has a commit beyond
      `state.repo.head` and `exit.json` is present → the `commit` event has that sha.
    - `test_queue_tick_adopts_finished`: single-shot `tick()` applies a finished open run.
- **Existing suites unchanged:** `tests/test_agentic_runner.py` (argv/parse tests adapted
  to the wrapper only where they patched `subprocess.run`, with the same assertions),
  `tests/test_agentic_reexec.py`, `tests/test_agentic_nonblocking.py`,
  `tests/test_agentic_heartbeat.py`, `tests/test_agentic_events.py`,
  `tests/test_agentic_e2e.py`.
- **Sabotage checks:** run the adopt suite after each of these (restoring in between) and
  confirm it fails. Spawn without `start_new_session`. Allow detach in the collect phase.
  Skip adoption. Plan gates during draining ticks. Record the outcomes in the spec.
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
on, every run is detached. No state migration is needed. Ledger additions: `adopt` journal
records, and the `detached` field on `stop`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
