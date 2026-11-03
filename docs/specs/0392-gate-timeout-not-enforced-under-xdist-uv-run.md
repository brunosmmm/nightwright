---
id: SPEC-0392
title: "A gate's timeout_s is a hard wall-clock bound: kill the whole process tree, bound the drain, and alarm on an overrun"
status: proposed
owner: user
created: 2026-11-02
kind: feature
source_idea: IDEA-552
depends_on: [SPEC-0228, SPEC-0229, SPEC-0262]
tags: [agentic, gates, queue, watchdog]
---

## Context

IDEA-552 (bug, 2026-10-31). The `pytest-affected` close gate (`.nw/project.yaml`: `python3
tools/affected_tests.py ... --run uv run pytest -q -x -n 4`, `timeout_s: 2400`) ran for
**8600 s**. While it ran:

- the runner's drain (`queue._drain`) stayed blocked for about 2 h, because it loops while
  `pool.flights` is non-empty and the gate's flight never ended;
- neither `runner_journal_silent` nor `lease_stale` fired, so the conductor was never woken;
- a SIGTERM to the runner did not interrupt it.

Today `gates.run_gate` (`src/nightwright/agentic/gates.py`) starts the command with
`subprocess.Popen(cmd, shell=True, start_new_session=True)` and waits with
`proc.wait(timeout=timeout_s)`. On `TimeoutExpired` it calls `_kill_group`, which sends SIGTERM
to the process group, then `proc.wait(timeout=_KILL_GRACE_S)` on **the shell only**, and sends
SIGKILL only if the shell is *still* alive after the grace period. Four weaknesses follow, and
the incident is consistent with any of them:

1. **Only the shell is waited on.** Once the shell dies on SIGTERM, `_kill_group` returns without
   a SIGKILL; `uv run pytest -n 4` descendants (the `uv` process, pytest, the xdist/execnet
   workers) that ignore or handle SIGTERM are left running, still holding the gate's log and the
   gate slot's CPU, and nothing reaps them. A descendant that called `setsid`/`setpgid` is outside
   the group and is never signalled at all.
2. **The wait is a single blocking call** with no independent deadline and no bound on the
   post-kill reap, so any hang in the kill path (a killed-but-unreapable process, a stuck
   `killpg`, a wait on a process that never leaves) has no second line of defence.
3. **The drain has no overrun cap of its own.** `_drain` takes its `busy` budget from the largest
   `timeout_s` in `waiting_on`, but a flight that outlives that budget is simply waited on. It
   only gets bounded (`_STOP_BOUND_S`) after `stop` is set, and the stop path calls
   `G.terminate_all()` once, which is the same `_kill_group` and has the same weaknesses.
4. **The overrun is invisible.** `runner_silence` (`events.py`) is quiet while a declared `busy`
   step is within `budget_s + overdue_grace_s`, and the runner's heartbeat thread keeps the lease
   fresh, so a drain that is stuck past its budget raised no event the conductor acts on in the
   incident. (Why `runner_journal_silent` stayed quiet past 2400 s + grace is part of what the
   reproduction step below must establish; the fix must make it fire regardless.)

The same `run_gate` also runs on the worker box (`worker_job.py` / `worker.py`), so the remote
job's timeout (`remote_gates.job_timeout_s`, which assumes `run_gate` honours each `timeout_s`)
rests on the same code.

## Goals / Non-goals

**Goals**
- A gate command, and every process it spawned, is gone within a small fixed bound after
  `timeout_s` (default bound: `timeout_s` + 2 x grace + reap bound), whether the descendants
  ignore SIGTERM, left the process group, or the shell exited first.
- `run_gate` always returns by its own hard deadline, with `timed_out=True`, even if the kill
  path itself misbehaves.
- The runner's drain cannot wait on a gate flight past its deadline plus a bound: past it, the
  flight is force-ended and abandoned/recorded as today's stop path does (SPEC-0262).
- A gate (or drain) running past its deadline raises a conductor-visible event, independent of
  the lease heartbeat, so a ~2 h overrun cannot be silent again.
- A reproduction of the xdist case exists as a test, so the root cause is pinned, not guessed.

**Non-goals**
- Changing any gate's configured `timeout_s`, the gate ladder/retry policy (SPEC-0289), or how a
  timeout is triaged (SPEC-0311).
- Persona (`claude -p`) run timeouts (`runproc`), which have their own wrapper.
- Making pytest itself faster or changing `-n 4`.

## Decision

`gates.py` owns timeout enforcement end to end. `run_gate` waits with a polling loop against a
monotonic deadline, and on expiry runs `_kill_tree`: snapshot the process tree of the shell
(the process group plus `/proc` descendants by parent links, so setsid'd children are found),
SIGTERM it, wait for **all** snapshot members (not just the shell), then SIGKILL every survivor,
then reap with a bounded wait. A last-resort hard deadline guarantees `run_gate` returns even if
reaping fails. The runner publishes the running gates' deadlines in its lease info, `_drain`
force-ends any flight that is `OVERRUN_BOUND_S` past its deadline, and the events watchdog emits
a new `gate_overrun` event when a gate or drain is past deadline + grace. The first
implementation step is a reproduction test for the xdist case; if it shows a cause not listed
above, the fix for that cause is added to this spec's Design (As shipped) before `done`.

## Design

### 1. Kill the tree, not the shell (`gates.py`)

- `_tree_pids(proc)`: the set of pids in the shell's process group (`os.getpgid`, scanning
  `/proc/*/stat` field 5) united with every descendant reachable through the ppid links
  (`/proc/*/stat` field 4), taken as a snapshot immediately before signalling and re-taken once
  between TERM and KILL (new children spawned during the grace are caught). Off Linux (no
  `/proc`) it degrades to the group alone, as today.
- `_kill_tree(proc)` replaces `_kill_group`'s body (`_kill_group` stays as the name callers and
  tests use, with the new behaviour): `killpg(TERM)` plus `kill(TERM)` for each snapshot pid
  outside the group; poll until every snapshot pid is gone or `_KILL_GRACE_S` elapses; then
  `killpg(KILL)` plus `kill(KILL)` for every pid still present, **even if the shell already
  exited**; then poll up to `REAP_BOUND_S` (default 10 s) for the pids to disappear. A pid
  that outlives the reap bound is logged in the sidecar `error` (`"survivors: <pids>"`) and
  never waited on again. Pid reuse is guarded by comparing field 22 (start ticks) from the
  snapshot (`runproc.start_ticks`) before every signal.
- The shell `proc` is reaped with `proc.wait(timeout=...)` after the kill; a `TimeoutExpired`
  there is swallowed (recorded, not raised).

### 2. A deadline that does not depend on one blocking call

- `run_gate` replaces `proc.wait(timeout=timeout_s)` with a loop of `proc.wait(timeout=slice)`
  (`slice` = `min(1.0, remaining)`) against `deadline = started + timeout_s` on
  `time.monotonic()`, also checking `_stopping`. On expiry it calls `_kill_tree`.
- The whole kill-and-reap sequence runs under `HARD_DEADLINE_S = 2 * _KILL_GRACE_S +
  REAP_BOUND_S + 5` (default 25 s) enforced by running `_kill_tree` in a daemon thread and
  joining with that timeout. If the thread is still alive, `run_gate` proceeds: it returns
  `GateResult(timed_out=True, exit_code=None)` with `error = "timed out after Ns; kill did not
  complete within Ms"`. The gate therefore returns by `timeout_s + HARD_DEADLINE_S` in every
  case.
- Both knobs read `NW_GATE_GRACE_S` / `NW_GATE_REAP_S` env overrides so tests can shorten them
  (the same pattern as `NW_RUNPROC_GRACE_S`).

### 3. The gate publishes its deadline

- `_LIVE` entries carry `(proc, deadline_epoch_s, name, campaign)`; a function
  `live_deadlines() -> list[dict]` returns `{name, campaign, started_at, deadline_at}`.
- The runner includes it in lease info as `gates_live` on every `lease.update_info` it already
  does during a tick and in `_drain` (the drain refresh path), so the out-of-process watchdog
  sees deadlines without importing the runner. For gates run on a worker box the coordinator
  already tracks `deadline_at` (`remote_gates`, `submitted + header["timeout_s"]`); that value
  is published the same way.

### 4. The drain cannot wait forever (`queue._drain`)

- `_drain` computes, per flight in `waiting_on`, `deadline = since_started + timeout_s`. Once
  `now >= max(deadline) + OVERRUN_BOUND_S` (default 60 s) it behaves exactly as if `stop` were
  set: `_terminate_children()` once, then after `_STOP_BOUND_S` `force_detach_waiting`, record
  `unfinished_in_process()` as `abandoned`, clear the flights and break (SPEC-0262: recorded,
  not killed, and left to adoption). It journals `drain_overrun` with the campaigns and
  deadlines.
- A second SIGTERM/SIGINT to the runner while `terminated` is already true skips the remaining
  wait: the loop breaks at the next 0.2 s poll with the same abandon path. The first signal's
  behaviour is unchanged.

### 5. The overrun is a conductor event (`events.py`, `quiet.py`)

- New event `gate_overrun` (fields `campaign`, `gate`, `deadline_at`, `overdue_s`, `runner_pid`),
  raised by the watchdog scan when any `gates_live` entry in a held lease has
  `now > deadline_at + overdue_grace_s` (the existing setting), once per
  `(gate, deadline_at)` key. It is also raised when a `draining` info is older than
  `max(waiting_on.timeout_s) + overdue_grace_s + OVERRUN_BOUND_S`. It does not depend on
  `tick_at`/`progress_at`, so it fires while `runner_silence` is quiet.
- `quiet.py` maps `gate_overrun` to `"wake"`, and `events.EVENT_FIELDS` lists its fields.
- `runner_silence` is also fixed so a `busy` step past `budget_s + overdue_grace_s` is never
  hidden by a fresher `progress_at` / `busy_ended_at` stamped from inside the drain's refresh
  loop. If the reproduction shows that stamping is why `runner_journal_silent` stayed quiet in
  the incident, this is the fix for it; if it shows another cause, that cause is recorded here.

### 6. Reproduction first

`tests/test_gate_timeout_tree.py` starts with a fixture that builds the incident's shape
without `uv`/pytest-xdist: a shell command spawning a parent that forks 4 children, each
ignoring SIGTERM (`trap '' TERM`), one of which calls `setsid`. Before any production change
the test is written and run against the current code; its failure output is pasted in the
spec's As shipped section as the pinned cause. One additional marked test
(`@pytest.mark.slow`, skipped when `uv`/`pytest-xdist` is not importable) runs a real
`pytest -n 2` over a sleeping test with `timeout_s=3`.

## Alternatives considered

- **Raise `timeout_s` or lower `-n`** — hides the symptom; the unbounded wait stays.
- **A cgroup/systemd scope per gate (`systemd-run --scope`)** — kills everything reliably but is
  Linux-with-systemd only, needs the worker boxes to permit user scopes, and cannot be tested
  portably. The `/proc` snapshot plus group kill covers the realistic cases with no new
  dependency; a cgroup backend can be a later spec.
- **`psutil`** — new dependency for what `/proc` parsing (already used by `runproc`) gives.
- **Only fix the drain** — leaves gates (and worker jobs) with unkillable children and leaks
  CPU into the next gate.
- **Only add the alarm** — tells the conductor but still leaves a runner wedged for hours.

## Acceptance criteria

- [ ] A gate whose command spawns descendants that ignore SIGTERM (including one that called
      `setsid`) is gone entirely (no surviving pid from the snapshot) within
      `timeout_s + 2 x grace + reap bound`, and the result has `timed_out=True`.
- [ ] `_kill_group` sends SIGKILL to surviving descendants even when the shell has already
      exited after SIGTERM.
- [ ] `run_gate` returns within `timeout_s + HARD_DEADLINE_S` even if the kill thread never
      completes (simulated by a kill function that blocks), with an `error` that says the kill
      did not complete.
- [ ] A pid recycled between snapshot and signal (start ticks differ) is not signalled.
- [ ] The lease info carries `gates_live` with each running gate's `deadline_at` while a gate
      runs, and drops it when the gate ends.
- [ ] `_drain` with a flight that never ends breaks out no later than its largest deadline +
      `OVERRUN_BOUND_S` + `_STOP_BOUND_S` (+ the poll slack), calling `_terminate_children`
      once and recording the flight as abandoned, with a `drain_overrun` journal record.
- [ ] A second stop signal during a drain that is already terminating breaks the drain at the
      next poll.
- [ ] A held lease whose `gates_live` entry is past `deadline_at + overdue_grace_s` yields one
      `gate_overrun` event (deduplicated by gate + deadline), `quiet.py` classifies it `wake`,
      and it fires while `runner_silence` returns None.
- [ ] `runner_silence` reports a `busy` step past budget + grace even when `progress_at` was
      refreshed more recently.
- [ ] The reproduction test existed and failed against the pre-fix code, and its cause is
      recorded in the spec's As shipped section.
- [ ] No existing gate behaviour changes for a gate that finishes before `timeout_s`: exit code,
      sidecar fields, ladder/retry results are identical.

## Test plan

- **Automated tests:**
  - `tests/test_gate_timeout_tree.py` (new): fixture with SIGTERM-ignoring children and a
    `setsid` child; asserts all snapshot pids dead after `run_gate(timeout_s=1)` with
    `NW_GATE_GRACE_S=0.3`; asserts SIGKILL path when the shell exits first; hard-deadline test
    with a patched blocking `_kill_tree`; pid-reuse guard test (patched `start_ticks`);
    sidecar `timed_out`/`error` assertions; the slow real `pytest -n 2` case.
  - `tests/test_gates.py` (existing, extended): a gate finishing in time is unchanged
    (regression); `live_deadlines()` lists a running gate and empties after.
  - `tests/test_queue_drain_overrun.py` (new): `_drain` with a stub pool whose flight never
    ends and `OVERRUN_BOUND_S`/`_STOP_BOUND_S` shortened; asserts the break, `abandoned`, the
    single `_terminate_children` call and the `drain_overrun` journal record; second-signal
    test using `stop` re-set.
  - `tests/test_events_gate_overrun.py` (new): a lease fixture with `gates_live` past deadline
    gives one `gate_overrun`; no duplicate on the next scan; absent when within grace; fires
    with a fresh `tick_at`; `runner_silence` busy-past-budget case with fresh `progress_at`;
    `quiet.py` classification.
  - Run with `python3 tools/affected_tests.py --run uv run pytest -q` plus the files above;
    `python3 tools/spec_lint.py` must pass.
- **Manual verification:** on a dev checkout with a throwaway project gate
  `cmd: "sh -c 'trap \"\" TERM; setsid sleep 600 & sleep 600'"`, `timeout_s: 5`, run
  `nw gate run` (or a queue tick). Expect the gate result `timed_out`, and `pgrep -f 'sleep
  600'` empty within ~30 s. Then start a runner, begin a drain with that gate live, and confirm
  `nw queue events` shows `gate_overrun` after the deadline plus grace and the drain exits
  within the bound.
- **Regression guard:** the existing gate, ladder (SPEC-0289), SPEC-0229 stop and SPEC-0262
  drain tests pass unchanged; `tests/test_agentic_dogfood_config.py` still parses
  `.nw/project.yaml` with the same `timeout_s` values.

## Rollout / migration

1. Land the reproduction test (red), then the `gates.py` kill-tree and deadline changes (green).
2. Land the lease `gates_live` publication, the drain overrun bound, and the `gate_overrun`
   event together (events and quiet maps are read by the same running conductor).
3. A running runner picks the change up at its next restart; the `gates_live` info is optional
   in the lease, so an old runner simply never raises `gate_overrun`. Workers pick up the
   `gates.py` change via `nw workers update` (no sudo, no manual scripts).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; the manual steps performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped (including the pinned root cause).
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
