---
id: SPEC-0225
title: "Detached persona runs, adopted across runner restarts"
status: done
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
honest cost on interrupt and the `runner_draining` signal. This spec is the core: detached
runs, detaching, and adoption, with the code-change exec as the first user.
[SPEC-0229](./0229-runner-exit-paths-and-draining-ticks-with-detached-runs.md) builds on it
and covers the other exit paths (signal, `--once`, `--max-ticks`, crash) and admission while
a drain is in progress.

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
- A persona run lives in its own process tree, which records its state in files, so it can
  outlive the runner process.
- A new runner process **adopts** the runs that are still open in the campaign ledgers
  (`run.start` with no `run.end`). It waits for the live ones and applies the finished ones.
  Each run is applied exactly once, and a live agent is never duplicated in a worktree.
- A code-change exec does not wait for persona runs: it detaches them and the new process
  adopts them.
- Run timeouts are enforced even when no runner is alive.

**Non-goals**
- The other exit paths (signal, `--once`, `--max-ticks`, crash) and admission during a drain:
  [SPEC-0229](./0229-runner-exit-paths-and-draining-ticks-with-detached-runs.md). Until it
  lands, a stop keeps today's behaviour: it kills the persona runs and returns promptly
  (Decision 7). Only the code-change exec detaches.
- Detaching gate or close steps. They run in-process. They are bounded by the gate time,
  and their subprocesses must die with the runner (SPEC-0220 discard rule).
- Killing runs on demand (a `wt queue kill-run`). The pgid is recorded, which is enough for
  a manual kill.
- Remote or other-host runs. Liveness is checked on the local host.
- FakeRunner detachment. FakeRunner stays in-thread, and its flights drain as today.

## Decision

1. **Detached spawn with a wrapper.** `ClaudeRunner` starts each run as a wrapper process,
   `python -m wt.agentic.runproc <run_dir>`, in a new session
   (`Popen(start_new_session=True)`). The wrapper first **claims** the run dir by creating
   `proc.json` exclusively (`O_CREAT|O_EXCL`). If the claim fails, it exits without starting
   anything. Then it starts `claude -p` in a process group of its own
   (`Popen(process_group=0)`), with stdin from `prompt.md` and stdout/stderr going directly to
   `result.json` / `stderr.log`, and records the child in `child.json`. It enforces
   `timeout_s` (TERM to the child's group, 10 s grace, then KILL), and writes `exit.json`
   atomically when the child ends.
2. **Attach-or-spawn, and a runner protocol with hooks.** `PersonaRunner.run(req, *,
   hooks=None)`. `ClaudeRunner.run` is idempotent per run dir: if `run_dir/proc.json` exists
   it attaches. Otherwise it spawns and waits until the wrapper's `proc.json` appears, for at
   most `agentic.spawn_timeout_s` (default 30 s). Past that the launch has failed: the runner
   claims `proc.json` itself (`{orphaned: true, by: "spawn_timeout"}`), so a late wrapper
   can never start `claude`, and returns an error `RunResult` (`exit_code: -1`, `is_error`,
   `result_text` = the `wrapper.log` tail), which becomes a normal `run.end` error.
   Once attached or spawned, it waits and collects. Collecting gives exactly today's `RunResult` and
   `run.json`. `hooks` (`RunHooks`, below) lets the pool see the run's phase and release a
   waiting thread. `FakeRunner` accepts and ignores `hooks`. `_do_persona` changes in three
   ways only: it passes `hooks`, it handles the `DETACHED` outcome, and it takes
   `interrupted` from the run's outcome (Decision 6).
3. **Task phases.** Each persona task is in one of these phases:
   - `spawning`: from submission until the wrapper's `proc.json` exists. Not detachable;
   - `wait`: the run's process tree is alive, or has exited and is not yet collected.
     Detachable;
   - `collect`: parse, `run.json`, the implementer's commit. Not detachable;
   - `done`.
   Every phase change, and every detach, happens under one `StepPool.lock`. Detachability is
   the runner's `PersonaRunner.detachable` attribute: True for `ClaudeRunner`, False for
   `FakeRunner`. A non-detachable task never leaves `spawning`, so it is never detachable
   and drains as today.
4. **Detach for the code-change exec.** On `code_changed` the loop stops admitting and
   submitting, as today. It then loops: `_harvest`, then `pool.try_detach_all()`, which
   atomically checks that nothing is in-process, `spawning` or `collect`, and only then
   detaches every remaining flight. When the call succeeds, `pool.flights` is empty, the
   waiting threads return within 0.2 s, and the loop journals the detached runs and execs.
5. **Liveness is the process group.** A run is alive while its wrapper or any process in
   the child's process group is alive. A run is `vanished` only when no process of either
   is left. If the wrapper is gone but the group lives, adoption waits on the group and
   enforces the timeout itself.
6. **`interrupted` is the run's own outcome.** A run is interrupted only when **we** killed
   it: the wrapper forwarded a SIGTERM it received (`exit.json.killed_by == "signal"`), or
   adoption found it orphaned. A child killed by a signal the wrapper did not send (an OOM
   kill, an external `kill`) gets `killed_by: "external"` and is an error, not an
   interruption (`is_error: true`, `interrupted: false`). The runner's `stop` flag no longer marks detachable runs as
   interrupted. For non-detachable runners (FakeRunner) the old rule stays,
   `interrupted = ctx.stop.is_set()`, because their runs share the runner's fate.
7. **Interim stop behaviour.** Today `_handler` forwards SIGTERM to the runner's own
   process group, which kills the `claude` children. With the wrapper in its own session,
   that signal no longer reaches them. To avoid shipping a 30-minute stop, this spec keeps
   today's outcome explicitly: when `stop` is set (signal, or any caller setting
   `ctx.stop`), the drain sends TERM to the **wrapper** of every ClaudeRunner task in
   `spawning` or `wait` whose run is still running, meaning no `exit.json` exists yet. A
   `spawning` task gets it as soon as its `proc.json` appears. The wrapper forwards TERM to
   the child's group and escalates to KILL after 10 s, then writes `exit.json` with
   `killed_by: "signal"`. As a backstop, the drain sends KILL to the child group 15 s after
   its TERM if the group is still alive. A run whose `exit.json` already exists at stop
   time is not signalled. It is collected normally, as a finished run, and is not
   interrupted. Signalled runs are recorded as interrupted, as today, and the drain returns
   within about 15 s. SPEC-0229 replaces this with
   detach-on-stop. Exception: runs already detached by a successful `try_detach_all` (the
   code-change exec) are no longer in `flights`. A stop that arrives between that call and
   `_EXEC` leaves them running, with no exec. They stay adoptable, the wrapper enforces
   their timeout, and the `stop signal` record lists them under `detached`.
8. **Only queue runs detach.** Detaching applies only to runs started with `hooks` (the
   queue runner). With `hooks=None` (callers outside the queue, such as `wt spec review`),
   the wrapper is still used, so the timeout and the files are the same, but the run is
   attached. `ClaudeRunner.run` waits in a `try/finally`, and on any exception or
   `KeyboardInterrupt` raised while it waits, it kills the run's group and the wrapper
   (TERM, 10 s grace, KILL) before re-raising. A Ctrl-C on `wt spec review` therefore stops
   the agent, as today.
9. **Adoption.** On the first tick of every pool, whether in a fresh process, after a
   re-exec, or in a single-shot `wt queue tick`, the runner scans campaigns for open runs not
   in this pool's flights, and handles each one by its files. The caller holds the lease, so
   no other runner owns those runs. Adopted runs become ordinary flights, so harvest,
   `_fresh`, a park request and abandon apply as for any step.

## Design

### Run directory contract (`runs/NNN-<persona>/`)

| file | written by | when | content |
|---|---|---|---|
| `prompt.md` | runner | before spawn | as today |
| `request.json` | runner | before spawn | `{schema: wt.run.request.v1, persona, model, effort, max_budget_usd, timeout_s, permission_mode, read_only, cwd, json_schema, started_at}` |
| `launch.json` | runner | before spawn | `{argv, cwd, env}`, where `env` holds only the `req.env` additions (`WT_*`) |
| `proc.json` | wrapper, as its first act | before the child exists | `{wrapper_pid, wrapper_start_ticks, host, started_at}`, created `O_EXCL`, then written, `fsync`ed, and the dir `fsync`ed. Or `{orphaned: true, by: "adopt", ts}` when adoption claims it first |
| `child.json` | wrapper | right after starting `claude` | `{pid, pgid, start_ticks}` (temp + `os.replace`) |
| `result.json` | `claude` stdout | streamed | raw vendor stdout, as today but written live |
| `stderr.log` | `claude` stderr | streamed | as today |
| `exit.json` | wrapper, or adoption (synthesized) | child group ended | `{exit_code, timed_out, killed_by: null \| "timeout" \| "signal", signal, ended_at, duration_s, synthesized: bool}`, temp + `os.replace` |
| `run.json` | collector | collect phase | `wt.run.v1` (SPEC-0192), unchanged + SPEC-0224's `cost_estimated` |

`start_ticks` is field 22 of `/proc/<pid>/stat`, or null off Linux. The wrapper's own
stdout and stderr go to `wrapper.log` in the run dir, so a wrapper failure (a bad
`launch.json`, a failed claim, an exception) can be diagnosed. The wrapper is `python -m
wt.agentic.runproc` from the package that is installed when it is spawned. A later re-exec
of the runner does not touch a running wrapper, so the wrapper and the runner can be on
different code versions. `request.json`, `launch.json`, `proc.json`, `child.json` and
`exit.json` therefore only gain optional keys, and readers ignore unknown ones. The wrapper merges
`launch.json.env` over its inherited environment and ignores SIGHUP. On SIGTERM it forwards
TERM to the child's group and still writes `exit.json` (`killed_by: "signal"`, `signal: 15`).
`killed_by` is decided by the wrapper:
- `"timeout"`: its own timeout killed the child;
- `"signal"`: the child died after the wrapper forwarded a SIGTERM it received (the interim
  stop, a manual stop of the run);
- `"external"`: the child died from a signal the wrapper did not send (OOM, an external
  `kill`).
The child's signal number is always in `signal`. If starting `claude` fails (`Popen`
raises, for example because `claude` is not installed), the wrapper writes `exit.json =
{exit_code: -1, killed_by: null, error: str(e)}` and exits. The run collects as an error with
that text, as today's `OSError` path does.

### The claim closes the spawn window

The wrapper writes `proc.json` before `claude` exists, and refuses to start when the claim is
taken. So:
- every live `claude` has a `proc.json`, even if the runner was SIGKILLed or OOM-killed
  right after `Popen`. The next runner then sees a `running` (or `finished`) run, not an
  orphan;
- adoption declares a run `orphaned` only by winning the same claim, creating
  `proc.json = {orphaned: true}` with `O_EXCL`. A wrapper that starts later loses the claim
  and exits without starting `claude`. Before claiming, adoption waits until 10 s have passed
  since `launch.json`'s mtime, so a wrapper that was just started can still claim first.
  When `launch.json` does not exist, the 10 s run from the `run.start` ts instead. That
  covers a legacy run and a run whose worker never got as far as writing the file.
  Whichever side wins, there is never more than one agent per run dir.

The only gap left is accepted and documented. If the wrapper itself is SIGKILLed in the
microseconds between starting `claude` and renaming `child.json` into place, the child's
pgid is unknown. Adoption then sees a dead wrapper and no `child.json`, and reports the run
`vanished`, and the stray `claude` runs until its own `--max-budget-usd` stops it. That needs
a SIGKILL aimed at the wrapper inside that window. It is recorded in the `run.end` result
text ("wrapper died before recording its child") so that it is visible if it ever happens.

### Liveness and orphaned groups (`runs.py`)

- `wrapper_alive(proc)`: `/proc/<wrapper_pid>` exists, its state is not `Z`, and
  `wrapper_start_ticks` matches. Off Linux: `os.kill(pid, 0)`.
- `group_alive(child)`: `os.killpg(pgid, 0)` succeeds. When the leader `pid` still exists,
  its `start_ticks` must also match. A mismatch means the pid was reused, and that process is
  ignored.
- **Status of a run that has `proc.json` but no `exit.json`:**
  - wrapper alive → `running`;
  - wrapper dead and group alive → `orphan_group`. The wrapper died, but `claude` is still
    working. The attach-wait polls `group_alive` and enforces the timeout itself: at
    `run.start.ts + timeout_s` it sends `killpg` TERM, waits 10 s, then sends KILL. When the
    group is empty, it synthesizes `exit.json` (`synthesized: true`, `exit_code` 0 if
    `result.json` parses to a non-error result, else -1, and `killed_by: "timeout"` if it
    killed the group). It then collects as usual. This means the next plan never respawns an
    agent while an old one is still editing the worktree;
  - neither alive → `vanished`.
- **Zombies.** After an exec the wrappers are still children of the same pid. The collector
  calls `os.waitpid(pid, WNOHANG)` and ignores `ChildProcessError`. A zombie counts as dead.

### Runner hooks and collect (`runner.py`)

- `RunHooks` (owned by the pool, one per task):
  - `spawned()`: called once `proc.json` exists. Under the pool lock, phase `spawning` →
    `wait`;
  - `detached() -> bool`: reads the task's set-once detach `threading.Event`;
  - `enter_collect() -> bool`: under the pool lock, returns False if the task is detached,
    otherwise sets phase `collect` and returns True.
- **Wait loop** (`ClaudeRunner._wait`): every 0.2 s it checks, in order:
  1. `hooks.detached()` → return `DETACHED`;
  2. `exit.json` exists, or the run is `vanished` → if `hooks.enter_collect()` collect, else
     return `DETACHED`;
  3. otherwise the `orphan_group` duties above (timeout kill and exit synthesis).
  A detached thread therefore returns within 0.2 s and never touches the run's files. With
  `hooks=None` the loop waits and collects, inside the kill-on-exception `try/finally` of
  Decision 8.
- `collect(run_dir) -> RunResult` reads `result.json` and runs `parse_result` with the schema
  from `request.json`. It reads `exit.json` for `exit_code`, `timed_out`, `duration_s` and
  `interrupted = killed_by == "signal"`. A vanished run gives `exit_code: -1`, `is_error:
  true`, `interrupted: false`, and `result_text` = the stderr tail + "run process vanished".
  The cost is whatever `result.json` carries, otherwise null, which SPEC-0224 turns into an
  estimated charge. Then `write_run_record` runs as today. `collect` is idempotent.

### Queue (`queue.py`)

- `_Flight.tasks` entries become `(Step, Future, RunHooks | None)`. Gate tasks have no hooks
  and count as in-process.
- `StepPool.lock` guards phases and `flights` membership. `StepPool.try_detach_all() ->
  list | None`, under the lock:
  - if any flight is in-process, or any task is in `spawning` or `collect`, return None;
  - otherwise, for every flight, set each `wait` task's detach flag and drop the flight from
    `flights`. A flight's `done` tasks (finished reviewers whose group was not yet harvested)
    are dropped with it: their results are on disk, and adoption re-collects them. Return
    `[{campaign, run_ids}]`.
  `enter_collect` takes the same lock, so no task can enter `collect` between the check and
  the detach. After a successful call `pool.flights` is empty.
- `_harvest` runs before `try_detach_all` in every loop iteration. A flight whose tasks are
  all done is therefore harvested, never detached. `_harvest` drops a `DETACHED` result. This
  is defensive, since detached flights are no longer in `flights`.
- **Fan-out flights.** A reviewer group is detachable only when each of its tasks is in
  `wait` or `done`. If one reviewer is still `spawning` or in `collect`, `try_detach_all`
  returns None and is retried on the next iteration.
- **The code-change exec.** On `code_changed`, `run()` journals the detection and stops
  admitting and submitting, as today. The SPEC-0224 drain then loops: `wait_any(0.2)`,
  `_harvest`, `try_detach_all()`. When the call returns a list, the loop journals
  `tick(drain)` and `stop code_changed` with `detached`, then execs. Persona runs never hold
  the exec. Only in-process steps, `spawning` (seconds) and `collect` (seconds) do.
- **Adoption** (`_adopt(cfg, ctx, pool, report)`, called by `tick` when `pool.adopted` is
  False, before `_harvest`). For each campaign not in MERGED/HANDED_OFF and not in
  `pool.ids()`, it finds every open run in the ledger (the same pairing as
  `events._open_run`, but returning all open runs). Each open run is classified **by the
  step recorded with it, not by hardcoded persona names**:
  - `_prepare_run` adds `step: {kind, personas}` to `run.start` (additive, optional in
    `schemas.EVENT_KEYS`). `kind` is the planned `Step.kind` (`implement` / `triage` /
    `review`), and `personas` is the `Step.personas` of a review step, for example
    `["review_triager"]` or `["conformance_reviewer", "test_reviewer"]`. Adoption rebuilds
    `Step(kind, persona=p, personas=tuple(personas))` from it;
  - for a legacy `run.start` without `step`, kind and personas come from
    `personas.ADOPT_REGISTRY`, a mapping defined next to `PERSONAS` that covers every
    campaign persona:
    - `implementer` → `implement`;
    - `gate_triager` → `triage`;
    - `review_triager` → `review`, personas `("review_triager",)`;
    - each persona in `REVIEWER_PERSONAS` (including `adversarial_reviewer` when it is
      added) → `review` in the campaign's reviewer group.
    A test asserts that every persona in `PERSONAS` except `spec_reviewer` (which has no
    campaign ledger) has an entry, so a new persona cannot be forgotten;
  - **grouping:** runs whose `step.personas` are the same tuple form one `_ReviewGroup`,
    applied through `_finish_reviews` as `_submit`/`_harvest` do today. `review_triager`
    (personas `("review_triager",)`) is therefore always its own single-member group and
    never grouped with the regular reviewers;
  - **unknown:** a persona with no `step` and no registry entry is adopted and collected,
    never killed. Adoption waits for it like any run, then records `run.end` + cost and a
    `note` ("adopted run of unregistered persona <p>; result recorded, no transition"). The
    state is unchanged, and the next plan decides the step afresh. It never raises out of
    the tick.

  and by its files:
  - **legacy survivor** (no `proc.json`, no `launch.json`, and a `run.start` without `step`,
    so it was started by pre-SPEC-0225 code): that `claude` may still be alive in no
    group adoption knows about. Adoption kills nothing. It holds the campaign with a waiting
    flight, so no step is respawned, until `run.start.ts + timeout_s` + 60 s. Then it writes
    `run.end` (`is_error: true`, "legacy orphan", cost estimated), with state unchanged;
  - **no `proc.json`** after the 10 s grace (new-code run): adoption claims it (`{orphaned: true}`). The
    status is `orphaned`, applied at once as a synthetic interrupted `RunResult`
    (`interrupted: true`, `cost_usd: null`, which SPEC-0224 marks estimated). The state is
    unchanged and the next plan respawns the step. This covers runs from pre-SPEC-0225 code
    and a runner that died before the wrapper claimed. If the claim fails because the
    wrapper won, the run is handled as `running`;
  - **`exit.json` present** → `finished`; otherwise `running`, `orphan_group` or
    `vanished`, as in *Liveness*. For each, rebuild the `RunRequest` from `request.json` +
    `prompt.md`, take `budget` from `run.start.max_budget_usd`, and submit `_do_persona` as
    a flight with fresh hooks in phase `wait`. `ClaudeRunner.run` attaches because
    `proc.json` exists;
  - **reviewer group:** the campaign's open reviewer runs (any status) form one
    `_ReviewGroup` with `Step("review", personas=[those personas in profile order])` and
    `meta` from their `run.start`s. A reviewer whose run was never started is planned again
    by `_pending_reviewers` after the group applies.
  - Adoption runs before admission in the same tick, so adopted campaigns count as busy.
    It journals `adopt` with `runs: [{campaign, run_id, persona, status: running | finished
    | orphan_group | vanished | orphaned | unknown}]` and sets `pool.adopted = True`. A
    single-shot `wt queue tick` applies finished, vanished, orphaned and unknown runs, and
    waits for live ones, since a throwaway pool always waits.
- **Snapshot check for adopted runs.** `_fresh` compares only the state name, and a human
  park-then-resume while no runner was alive would leave the name unchanged. So
  `run.start` records `state` (the campaign state at planning time) and its own ledger `seq`
  is the snapshot point. An adopted flight is superseded when the on-disk state differs from
  `run.start.state`, **or** the campaign ledger has a `transition`, `park`, `resumed` or
  `abandoned` event with `by != "tick"` and a `seq` after the `run.start`. The run's
  `run.end` and cost are recorded, and the transition is skipped (`superseded_by_human`).
  Otherwise applying is unchanged: the result goes through `_apply`, and `park_requested`
  is honoured as today.
- **Unrecorded commit, for adopted runs only.** An adopted implementer flight carries
  `adopted=True` and `exited_before_start`. The flag is true when `exit.json.ended_at` is
  earlier than the lease's `started_at`. That timestamp is the runner's logical start: it is
  kept across a SPEC-0194 `execve` (`acquire` keeps `started_at` on the handover path), and
  it is never taken from `/proc`. Only for such a flight: if `commit_all` finds nothing to
  commit and the worktree HEAD differs from `state.repo.head`, `_do_persona` records HEAD as
  `commit_sha`. That is the crash-during-collect case. Fresh runs keep today's behaviour
  exactly.

### Watchdog compatibility

`run.start.ts` is the true start of an adopted run, so `run_overdue`, `stuck_items` and
`runner_busy` work unchanged.

### Existing tests

`tests/test_agentic_events.py`, `test_agentic_ping.py`, `test_agentic_dashboard.py` and
`test_agentic_campaign_runs.py` seed open `run.start` events, but none of them calls
`tick()` or `run()`, so adoption never sees those fixtures. The suites that do tick use
FakeRunner and only read `run.start`. Their open runs are always in the pool's flights,
which adoption skips. A single-shot tick waits for its own runs, so it never leaves open
runs behind. No fixture changes are expected. If one turns up during implementation, it
gets recorded in the spec.

## Alternatives considered

- **Runner writes `proc.json` after `Popen`** (the previous revision). A runner SIGKILL
  between the two leaves a live wrapper that adoption cannot see. Having the wrapper claim
  the run dir itself closes that window. Rejected.
- **Keep `claude` in the wrapper's process group.** Then the wrapper's timeout TERM to the
  group hits the wrapper too, and it has to survive its own signal. A separate group for the
  child keeps "kill the run" one `killpg`. Rejected.
- **Pass the runs' pipes across `execve` (inheritable fds + a fd map in the env).** It only
  covers the exec case, not a stop or a crash. Threads die on exec anyway. Rejected.
- **Scan the process table for `wt.agentic.runproc <run_dir>`.** Fragile across platforms.
  The claim and the group checks make it unnecessary. Rejected.
- **`shutdown(wait=False)` for detached flights** instead of releasing the waiters. Leaked
  threads would still block interpreter exit and could write files after an exec. Rejected.
- **A separate long-lived run supervisor daemon.** A second process with its own lifecycle
  and liveness problem. The per-run wrapper dies with its run. Rejected.
- **Detach FakeRunner too.** That would need a subprocess fake for every test. The real
  spawn path is tested with a fake `claude` executable instead. Rejected.

## Acceptance criteria

- [x] `ClaudeRunner.run` starts the wrapper in its own session. The wrapper claims
      `proc.json` with `O_EXCL` before starting `claude` in its own process group, records
      `child.json`, streams `result.json`/`stderr.log` and writes `exit.json` atomically.
      For a run that completes, the `RunResult` and `run.json` equal today's. The wrapper
      enforces `timeout_s` (TERM, 10 s, KILL) and records `timed_out: true`, `killed_by:
      "timeout"` with no runner alive.
- [x] A wrapper whose claim is already taken (`{orphaned: true}`) exits without starting
      `claude`, and adoption never claims a run dir whose wrapper claimed it first. A run dir
      never has two agents. A runner SIGKILLed right after `Popen` leaves a run that the next
      runner reports `running` or `finished`, not `orphaned`. A wrapper that never claims
      within `spawn_timeout_s` gives a `run.end` error carrying the `wrapper.log` tail, and a
      late wrapper cannot start. A `claude` that fails to start gives `exit.json` with
      `error`.
- [x] On a code change with a persona run in `wait` and no in-process work, the runner execs
      while the run's process is alive. At `_EXEC` no `wt-step` thread for that run is alive,
      and `stop code_changed` lists it under `detached`. The new process adopts it and applies
      it exactly once (one `run.end`) when it exits.
- [x] `try_detach_all` is atomic with `enter_collect`. A task in `spawning` or `collect`
      blocks it, a task entering `collect` concurrently delays the exec, and the
      implementer's commit is recorded once. A reviewer group with one task in `spawning` or
      `collect` is not detached until that task reaches `wait` or `done`.
- [x] A run whose wrapper died while `claude` still runs (`orphan_group`) is never classified
      `vanished` and never respawned while the group lives. Adoption waits for the group,
      kills it at `run.start.ts + timeout_s` (TERM, 10 s, KILL), synthesizes `exit.json`,
      and applies the run once. A run whose wrapper and group are both gone, or whose pids
      were reused, is `vanished`: `run.end` with `exit_code: -1`, `is_error`, and the cost
      from `result.json` or estimated.
- [x] Adoption classifies every open run and journals one `adopt` record listing each run's
      persona and status. `finished` is applied on the first tick. `orphaned` (no claim
      after 10 s) gives `run.end` interrupted and estimated, state unchanged, step
      respawned. A legacy survivor is never killed and is closed as "legacy orphan" after
      its timeout. Classification uses `run.start.step`, falling back to
      `personas.ADOPT_REGISTRY` for legacy events, which covers every campaign persona. An
      unregistered persona is adopted and collected (never killed), with a `note` and no
      transition, without crashing the tick.
- [x] An adopted reviewer fan-out is applied once as one group, with every reviewer's verdict,
      including reviewers that had finished before the detach. An adopted `review_triager`
      run is applied as its own single-member group, never merged with the reviewers. A
      campaign parked or abandoned while its run was detached, including a park-then-resume
      done while no runner was alive (same state name), gets `run.end` + cost and no
      transition (`superseded_by_human`), and a `park_requested` one parks `human`.
- [x] Interim stop: with `ctx.stop` set during a live ClaudeRunner run, `run()` returns
      within 20 s, the run's process group is gone, and `run.end` is `interrupted: true`. A
      run whose `exit.json` already existed at stop time is collected as finished, not
      interrupted. A child killed by an external signal is `killed_by: "external"`, an
      error and not interrupted.
      With `hooks=None`, a `KeyboardInterrupt` raised while waiting kills the group before
      it propagates.
- [x] A ClaudeRunner run that exits normally while the loop is stopping is collected with
      `interrupted: false`, and its implementer work is committed. Only an adopted run whose
      `exit.json.ended_at` predates the lease `started_at` records an unrecorded HEAD as its
      commit, exactly once. Fresh runs never do.
- [x] FakeRunner-based suites behave as before (FakeRunner tasks never leave `spawning`), `wt
      queue tick` adopts and applies finished runs, `uv run pytest` passes and
      `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Fixture:** `tests/fixtures/fake_claude.py`, an executable put on `PATH` as `claude` via
  `monkeypatch.setenv("PATH", …)`. It reads stdin and sleeps for `FAKE_CLAUDE_SLEEP_S`
  (env, set through `req.env`) or until a `release` file exists in its cwd. It prints a
  result JSON (`structured_output` from `FAKE_CLAUDE_VERDICT`, `total_cost_usd`), can ignore
  SIGTERM (`FAKE_CLAUDE_STUBBORN`), and writes its pid to `child.pid` for assertions. The
  runner is `ClaudeRunner`. The loop is stopped through `ctx.stop` / the patched code-change
  check, never by calling the `run()`-local `_handler`.
- **Automated tests:**
  - `tests/test_agentic_runproc.py`:
    - `test_spawn_writes_contract_files`: all files exist, `proc.json` is created before
      `child.json`, `exit.json` is valid, and the `RunResult` equals the parse of the printed
      JSON.
    - `test_wrapper_timeout_without_runner`: the runner thread is abandoned. The wrapper
      still writes `timed_out: true` / `killed_by: "timeout"`, and the stubborn child is
      killed after the grace.
    - `test_claim_taken_wrapper_exits`: pre-create `proc.json = {orphaned: true}`. The
      wrapper exits non-zero, and no `child.pid` appears.
    - `test_attach_existing_proc`: a second `run(req)` attaches and does not spawn.
    - `test_detached_waiter_returns_fast`: setting the detach flag returns `DETACHED` within
      0.5 s while the child is alive, and no files are written afterwards.
    - `test_orphan_group_waited_and_killed_at_timeout`: SIGKILL the wrapper only. `group_alive`
      is true, and the wait loop does not collect. With a short `timeout_s` it kills the group
      (the `child.pid` process is gone), synthesizes `exit.json` (`synthesized`,
      `killed_by: "timeout"`), and collects once.
    - `test_vanished_when_group_gone`: SIGKILL the wrapper and the child group → `vanished`,
      `exit_code -1`, `is_error`, an estimated cost, and no process left.
    - `test_pid_reuse_ignored`: wrong `start_ticks` for the wrapper and the leader → dead.
    - `test_killed_by_signal_is_interrupted`: SIGTERM to the wrapper → `killed_by: "signal"`,
      `interrupted: true`. SIGKILL sent straight to the child → `killed_by: "external"`,
      `is_error`, `interrupted: false`.
    - `test_spawn_timeout_failed_launch`: the wrapper start is patched to hang before
      claiming, with `spawn_timeout_s: 1`. The result is a `run.end` error with the
      `wrapper.log` tail, `proc.json = {orphaned: true, by: "spawn_timeout"}`, and a
      released wrapper exits without starting `claude`.
    - `test_claude_popen_failure`: `claude` is missing from `PATH` → `exit.json` has
      `exit_code: -1` and `error`, and the run collects as an error.
    - `test_attached_run_killed_on_keyboard_interrupt`: `hooks=None`. A `KeyboardInterrupt`
      is raised in the waiting thread (the wait poll is patched to raise). The `child.pid`
      process is gone and the wrapper has exited before the exception reaches the test.
    - `test_wrapper_log_and_unknown_keys`: a `launch.json` with an extra key runs normally,
      and a broken `launch.json` leaves a traceback in `wrapper.log`.
  - `tests/test_agentic_adopt.py` (runner loop, tmp git repos, helpers from
    `tests/test_agentic_nonblocking.py`):
    - `test_reexec_does_not_wait_for_persona_run`: patched `_EXEC` records the call, the live
      `child.pid` and `threading.enumerate()`. `_EXEC` is called while the child is alive,
      and no `wt-step` thread waits on it. A second `run()` (the exec'd process) journals
      `adopt … running`. After the release there is exactly one `run.end`, and the campaign
      reaches GATING.
    - `test_runner_killed_after_popen_is_adopted_running`: `ClaudeRunner` is patched to
      return right after `Popen` (the runner "dies" before observing `proc.json`), and the
      pool is discarded. A new `run()` reports `running`, not `orphaned`, and there is one
      `child.pid`.
    - `test_orphan_claim_after_grace`: `run.start` + `launch.json` older than 10 s, with no
      `proc.json` → `orphaned`, claim file written, step respawned next tick. A variant with
      no `launch.json` uses the `run.start` ts: not claimed at 5 s, claimed at 11 s.
    - `test_interim_stop_kills_runs`: `ctx.stop` is set while an implementer is in `wait`.
      `run()` returns in < 20 s, the child group is gone, and `run.end.interrupted` is true.
      A `FAKE_CLAUDE_STUBBORN` variant (TERM ignored) still meets the bound through the
      wrapper's KILL escalation, with `killed_by: "signal"` and `signal: 9`. In a variant
      where the run's `exit.json` exists before the stop, it is not signalled and is
      collected with `interrupted: false`.
    - `test_legacy_survivor_not_killed`: a `run.start` without `step`, with no `proc.json`
      or `launch.json`, and a live dummy process. Adoption kills nothing, and the campaign
      gets no new step before the deadline. After `timeout_s` + 60 s (clock patched), there
      is a `run.end` "legacy orphan" and the state is unchanged.
    - `test_stop_after_detach_leaves_runs`: `ctx.stop` is set after `try_detach_all`
      returned but before `_EXEC` (patched hook). No exec, the run is alive, and `stop
      signal` lists it under `detached`.
    - `test_collect_race_delays_exec`: `git.commit_all` is patched to block, and the run
      exits just before `try_detach_all`. `_EXEC` waits for the commit, and the `commit`
      event is recorded once.
    - `test_fanout_detach_precondition`: two reviewers, one held in `spawning` (paused
      wrapper start). `try_detach_all()` returns None. Once it reaches `wait`, the call
      returns both run ids.
    - `test_adopt_statuses`: pre-seeded finished, vanished, orphan_group, orphaned and
      unknown-persona (a made-up `resolver` name with no `step`, live) runs. One tick gives
      the right statuses and `run.end` payloads. The unknown run is waited for, not killed,
      then recorded with a `note` and no transition, and the tick does not raise.
    - `test_adopt_review_group_with_finished_member`: one reviewer finished before the
      detach, one running. Applied as one group with both verdicts.
    - `test_adopt_review_triager`: an open `review_triager` run alongside open reviewer runs
      of another campaign. The triager is applied through `_finish_reviews` as a
      single-member group. A legacy `run.start` without `step` gives the same result through
      the registry.
    - `test_adopt_registry_complete`: every `PERSONAS` key except `spec_reviewer` is in
      `ADOPT_REGISTRY`.
    - `test_adopt_after_abandon_and_park_request`: `superseded_by_human`, and a `human`
      park. A variant parks and resumes the campaign while no runner is alive (the state
      name is the same again). The adopted run is `superseded_by_human` through the
      ledger-seq check.
    - `test_run_finishing_while_stopping_is_not_interrupted`: `ctx.stop` is set while
      `_drain` waits on a gate, and the run exits normally. `interrupted` is false, and the
      commit is recorded.
    - `test_unrecorded_commit_only_for_adopted`: an adopted finished implementer
      (`ended_at` < lease `started_at`, including after a simulated handover) records the
      extra worktree commit once. A fresh run whose implementer committed by itself emits no
      extra `commit`.
    - `test_queue_tick_adopts_finished`: single-shot `tick()` applies a finished open run.
- **Existing suites unchanged:** `tests/test_agentic_runner.py` (argv/parse tests adapted
  to the wrapper only where they patched `subprocess.run`, with the same assertions),
  `tests/test_agentic_reexec.py`, `tests/test_agentic_nonblocking.py`,
  `tests/test_agentic_heartbeat.py`, `tests/test_agentic_events.py`,
  `tests/test_agentic_ping.py`, `tests/test_agentic_dashboard.py`,
  `tests/test_agentic_campaign_runs.py`, `tests/test_agentic_e2e.py` (see *Existing tests*).
- **Sabotage checks:** run the suites after each of these (restoring in between) and
  confirm they fail:
  - spawn without `start_new_session`;
  - drop the `O_EXCL` claim;
  - classify by persona name only (ignore `run.start.step`);
  - liveness from the wrapper pid only;
  - allow detach in `spawning` or in `collect`;
  - split `try_detach_all`'s check from its detach;
  - ignore the detach flag in the wait loop;
  - skip adoption;
  - drop the snapshot (ledger-seq) check on adoption;
  - signal every task at an interim stop, including those that have already exited.
  Record the outcomes in the spec.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** with a live implementer running, merge a code change. `wt queue
  runner-log --tail 5` shows `stop code_changed detached=[…]` shortly after detection, then
  `start` and `adopt … running`. `ps -o pid,pgid,sid,cmd` shows the wrapper in its own
  session and `claude` in its own group. The run's `run.end` appears once.

### Test record (2026-10-25)

Ran `uv run pytest -q` (whole suite: 2218 passed, 1 skipped) and `python3 tools/spec_lint.py`. New: `tests/fixtures/fake_claude.py`,
`tests/test_agentic_runproc.py` (15 tests), `tests/test_agentic_adopt.py` (23 tests; runproc + adopt together: 37 passed).
`test_agentic_runner.py` and `test_agentic_cost_estimate.py` run the fake `claude` on PATH
instead of patching `subprocess.run` (same assertions); two `_prepare_run` monkeypatches in
`test_agentic_reviewers.py` take the new optional `step` argument.

Sabotage checks (source restored after each; each run failed the named test):
spawn without `start_new_session` (`test_detached_waiter_returns_fast`), drop `O_EXCL`
(`test_claim_taken_wrapper_exits`), liveness from the wrapper pid only
(`test_orphan_group_*`/`test_vanished_*`), allow detach in `spawning`/`collect`
(`test_try_detach_all_phases`), ignore the detach flag (`test_detached_waiter_returns_fast`),
skip adoption (`test_adopt_running_then_finished_once` and others), drop the ledger-seq check
(`test_adopt_after_park_resume_while_no_runner`), `interrupted` from the stop flag
(`test_run_finishing_while_stopping_is_not_interrupted`), drop the interim-stop TERM/KILL
(`test_interim_stop_kills_runs`), drop the kill-on-exception `finally`
(`test_attached_run_killed_on_keyboard_interrupt`). Not run as a separate sabotage: "split
`try_detach_all`'s check from its detach" (`test_collect_race_delays_exec` covers the race it guards,
but the sabotage itself was not run). "Classify by persona name only" was run in review round 2.

Review round 2 added (all pass; `tests/test_agentic_adopt.py` + `test_agentic_runproc.py`: 49 passed):
`test_fanout_detach_precondition` (end to end, one reviewer wrapper held in `spawning` by a paused
start) and `test_fanout_detach_precondition_phases` (either task held in `spawning`/`collect`, a
finished reviewer next to a waiting one, all-done and unharvested-implementer flights),
`test_adopt_review_group_with_finished_member` (real `try_detach_all`; one `review_fix` transition
whose brief carries both reviewers), `test_adopt_orphan_group_waited_and_killed` (SIGKILL the
wrapper only; `adopt` says `orphan_group`, no second `run.start`, killed at the timeout, one
`run.end`), `test_adopt_exited_before_start_from_ended_at` (earlier / later / real lease handover),
`test_adopt_classification_recorded_step_beats_registry` + `test_adopt_uses_recorded_step_end_to_end`,
the respawn / `cost_estimated` asserts and the no-`launch.json` variant of the orphan claim tests,
the commit assert in `test_run_finishing_while_stopping_is_not_interrupted`, and the minor
tightenings. `tests/fixtures/fake_claude.py` gained `FAKE_CLAUDE_BY_ARGV` (per-reviewer env).
Sabotage runs added, each failing the named test: no `fl.done()` check in `try_detach_all`
(`test_try_detach_all_never_drops_a_finished_implementer`, `..._phases`), `exited_before_start`
inverted (`test_adopt_exited_before_start_from_ended_at[earlier-True]`), classify by persona only
(`test_adopt_classification_recorded_step_beats_registry`, `test_adopt_uses_recorded_step_end_to_end`),
checking only the first task of a flight in `try_detach_all` (`test_fanout_detach_precondition_phases`).

Manual verification (live implementer + a real merge) was not performed on a live runner; the
same scenario is covered by `test_reexec_does_not_wait_for_persona_run` (exec while the child
lives, no `wt-step` thread, `stop code_changed` lists `detached`, the adopting process
journals `adopt … running` and records one `run.end`) and by the session/group assertions in
`test_detached_waiter_returns_fast`.

### As shipped

- `claim()` writes the payload to `.proc.json.<pid>.tmp` (fsync) and `os.link`s it to `proc.json`, so the file
  is exclusive (FileExistsError -> False) but never visible empty or partial; found by a race in
  `test_orphan_claim_after_grace`. Pinned by `test_claim_never_exposes_partial_proc_json`. Readers go
  through `runproc.read_json`, which treats undecodable content as absent (`unclaimed`).
- Liveness helpers (`wrapper_alive`, `group_alive`, `run_status`, `claim`, `terminate_run`)
  live in `runproc.py` beside the wrapper, not in `runs.py` (`runs.py` is the `wt campaign
  runs` data module and would have imported the runner for nothing). `collect(req)` takes the
  `RunRequest` (rebuilt from `request.json` + `prompt.md` by `rebuild_request` for adopted runs)
  instead of a bare run dir.
- `run.start` also records `state` (the campaign state at planning time) next to `step`; the
  adoption snapshot is built from it (legacy events: derived from the step kind). Adoption
  skips MERGED and HANDED_OFF campaigns; an ABANDONED one is adopted and its run recorded
  (`run.end` + cost, no transition, no commit).
- Orphan-group timeouts are measured from `request.json.started_at` (written by the runner just
  before it spawns), not from the `run.start` ts; they differ by milliseconds.
- Adoption of a run with no `proc.json` is done in the waiting thread (`hooks.adopt =
  ("claim" | "legacy", epoch)`): it waits for a wrapper claim until the grace has passed, then
  claims the run as orphaned (synthetic interrupted result) or, for a legacy survivor, closes it
  as "legacy orphan" (also interrupted: state unchanged) at `run.start.ts + timeout_s + 60 s`.
  Adoption is skipped for runners that are not `detachable` (FakeRunner).
- The interim stop sends TERM to the wrapper (which forwards it and escalates to KILL after
  `WT_RUNPROC_GRACE_S`, default 10 s) from `_drain`, and a backstop KILL to the group 15 s later.
  On a crash the pool releases (`detach_force`) the waiting threads instead of waiting for them;
  the runs stay adoptable (SPEC-0229 owns the other exit paths).
- `spawn_timeout_s` is read from `agentic.spawn_timeout_s`. A wrapper that exits before claiming
  returns an error result at once (`wrapper.log` tail), without waiting for the timeout.
- `interrupted` for detachable runners is `exit.json.killed_by == "signal"`; non-detachable
  runners keep `ctx.stop.is_set()`. The mechanism is the runner's `detachable` attribute.
- `try_detach_all` also returns None while any flight is `done()` (all its tasks finished, not yet
  harvested), so an implementer whose future completed after `_harvest` built its list is harvested
  on the next loop iteration (its `commit` recorded) instead of being dropped; only `wait` tasks
  and finished reviewers of a still-running group are detached. Pinned by
  `test_try_detach_all_never_drops_a_finished_implementer` (a patched `_harvest` finishes the
  implementer after its scan) and `test_fanout_detach_precondition_phases`.
- The adoption snapshot check ignores `transition` events whose `from` equals `to` (such as
  `prioritized`). `ClaudeRunner.run` does not rewrite `prompt.md` when it attaches (`proc.json`
  exists or `hooks.adopt` is set), and `_await_claim` is inside the kill-on-exception `try`.
- The legacy `claude` left by a SIGKILLed pre-SPEC-0225 runner is held for `timeout_s` + 60 s and
  never killed, so it is not duplicated in the worktree either.
- Rollout edge (legacy `claude` that survived an old runner): handled by the legacy-survivor rule
  above, not by a worktree-cwd process scan.

## Rollout / migration

The first runner on this code (after the SPEC-0194 re-exec from the previous code) finds
no `proc.json` for runs the old code started. The old code's drain waited for all of them,
so normally there are none. Any left by a crash are claimed as orphaned after the grace.
From then on, every ClaudeRunner run is detached. No state migration is needed. Journal
additions: `adopt` records, and the `detached` field on `stop`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
