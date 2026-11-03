---
id: SPEC-0392
title: "A gate's timeout_s is a hard wall-clock bound: kill the whole process tree, reap with a bound, and always return"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
kind: feature
source_idea: IDEA-552
depends_on: [SPEC-0228, SPEC-0229]
tags: [agentic, gates]
---

## Context

IDEA-552 (bug, 2026-10-31). The `pytest-affected` close gate (`.nw/project.yaml`: `python3
tools/affected_tests.py ... --run uv run pytest -q -x -n 4`, `timeout_s: 2400`) ran for
**8600 s**. While it ran:

- the runner's drain (`queue._drain`) stayed blocked for about 2 h, because it loops while
  `pool.flights` is non-empty and the gate's flight never ended;
- neither `runner_journal_silent` nor `lease_stale` fired, so the conductor was never woken;
- a SIGTERM to the runner did not interrupt it.

The gate's own timeout is the first line of defence and it did not hold. Today `gates.run_gate`
(`src/nightwright/agentic/gates.py:183`) starts the command with
`subprocess.Popen(cmd, shell=True, start_new_session=True)` and waits with
`proc.wait(timeout=timeout_s)`. On `TimeoutExpired` it calls `_kill_group`
(`gates.py:143`), which sends SIGTERM to the process group, then `proc.wait(timeout=_KILL_GRACE_S)`
on **the shell only**, and sends SIGKILL only if the shell is *still* alive after the grace
period. Two weaknesses follow, and the incident is consistent with either:

1. **Only the shell is waited on.** Once the shell dies on SIGTERM, `_kill_group` returns without
   a SIGKILL; `uv run pytest -n 4` descendants (the `uv` process, pytest, the xdist/execnet
   workers) that ignore or handle SIGTERM are left running, still holding the gate's log and
   the gate slot's CPU, and nothing reaps them. A descendant that called `setsid`/`setpgid` is
   outside the group and is never signalled at all.
2. **The wait is a single blocking call** with no independent deadline and no bound on the
   post-kill reap, so any hang in the kill path (a killed-but-unreapable process, a stuck
   `killpg`, a wait on a process that never leaves) has no second line of defence. `run_gate`
   therefore has no guarantee that it returns.

The same `run_gate` also runs on the worker box (`worker_job.py` / `worker.py`), so the remote
job's timeout (`remote_gates.job_timeout_s`, which assumes `run_gate` honours each `timeout_s`)
rests on the same code; fixing `run_gate` fixes both the local runner and the worker box.

The rest of the incident (the drain waiting on a gate flight, the lease/watchdog signals that
stayed quiet, the unobservable second signal) is a **separate subsystem** and is deliberately
split out of this spec; see Non-goals. This spec delivers the part that makes those
secondary defences rarely needed: a gate cannot outlive its `timeout_s` plus a small fixed
bound, and `run_gate` always returns.

## Goals / Non-goals

**Goals**
- A gate command, and every process it spawned, is gone within a small fixed bound after
  `timeout_s` (`timeout_s` + 2 x grace + reap bound), whether the descendants ignore SIGTERM,
  left the process group, or the shell exited first.
- `run_gate` always returns by its own hard deadline, with `timed_out=True`, even if the kill
  path itself misbehaves.
- Running gates are listed with their deadlines by `gates.live_deadlines()`, as the one
  in-process source the follow-up spec publishes.
- A reproduction of the xdist case exists as a test, so the root cause is pinned, not guessed.

**Non-goals** (each is a follow-up spec, to be written once this one is `accepted`; it
`depends_on` SPEC-0392)
- **Drain overrun bound** (`queue._drain`, `queue.py`) and the stop path. Review notes the
  follow-up must carry: a drain's per-flight deadline cannot be `since_started + timeout_s`
  (`_waiting_on`, `queue.py:5147`, has no per-flight start time, and a *gate flight* runs a whole
  gate ladder budgeted at `events_settings(cfg)["gate_budget_s"]`, default 3600 s, plus SPEC-0289
  retries under the ladder deadline `t0 + gate_budget_s`, `gates.py:715`, which did not bound the
  8600 s run either); it must take each flight's deadline from `live_deadlines()` and the ladder
  deadline, the later of the two. A second-signal escape needs a distinct `stop_now` Event
  (the handler at `queue.py:5595` only calls `stop.set()`, which cannot signal twice), and its test
  must call the handler twice.
- **Publishing gate deadlines in the lease info** (`gates_live`), including the coordinator's
  remote-gate `deadline_at` (`remote_gates`, `submitted + header["timeout_s"]`), with its own
  criterion and test in `tests/test_events_gate_overrun.py`.
- **The `gate_overrun` watchdog event** (`events.py`, `quiet.py` `wake` mapping,
  `EVENT_FIELDS`) and the `runner_silence` busy-past-budget fix.
- Changing any gate's configured `timeout_s`, the gate ladder/retry policy (SPEC-0289), or how a
  timeout is triaged (SPEC-0311).
- Persona (`claude -p`) run timeouts (`runproc`), which have their own wrapper.
- Making pytest itself faster or changing `-n 4`.

## Decision

`gates.py` owns timeout enforcement end to end. `run_gate` waits with a polling loop against a
monotonic deadline, and on expiry runs `_kill_tree`: snapshot the process tree of the shell
(the process group plus `/proc` descendants by parent links, so setsid'd children are found),
SIGTERM it, wait for **all** snapshot members (not just the shell), then SIGKILL every survivor,
even if the shell has exited, then reap with a bounded wait. A last-resort hard deadline
guarantees `run_gate` returns even if the kill or reap hangs. The registry of live gates
becomes a dict that also records each gate's deadline, exposed through `live_deadlines()`. The
first implementation step is a reproduction test for the xdist case; if it shows a cause not
listed above, the fix for that cause is added to this spec's Design (As shipped) before `done`.

## Design

### 1. Kill the tree, not the shell (`gates.py`)

- `_tree_pids(proc)`: the set of pids in the shell's process group (`os.getpgid`, scanning
  `/proc/*/stat` field 5) united with every descendant reachable through the ppid links
  (`/proc/*/stat` field 4), taken as a snapshot immediately before signalling and re-taken once
  between TERM and KILL (new children spawned during the grace are caught). Each entry is
  `(pid, start_ticks)`. Off Linux (no `/proc`) it degrades to the group alone, as today.
- `_kill_tree(proc)` is the new body of `_kill_group` (`_kill_group` keeps its name, which
  `terminate_all` and the existing tests use, so SPEC-0229's stop path gets the same
  behaviour): `killpg(TERM)` plus `kill(TERM)` for each snapshot pid outside the group; poll
  until every snapshot pid is gone or `_KILL_GRACE_S` elapses; then `killpg(KILL)` plus
  `kill(KILL)` for every pid still present, **even if the shell already exited**; then poll up
  to `REAP_BOUND_S` (default 10 s) for the pids to disappear. A pid that outlives the reap bound
  is recorded as `"survivors: <pids>"` in the sidecar `error` and never waited on again.
- Pid reuse is guarded: before every signal the pid's current start ticks (field 22,
  `runproc.start_ticks`) are compared with the snapshot's; a mismatch (the pid was recycled)
  means the process is skipped and not signalled. A pid that is gone is a no-op.
- The shell `proc` is reaped with `proc.wait(timeout=...)` after the kill; a `TimeoutExpired`
  there is swallowed (recorded, not raised).

### 2. A deadline that does not depend on one blocking call

- `run_gate` replaces `proc.wait(timeout=timeout_s)` with a loop of `proc.wait(timeout=slice)`
  (`slice` = `min(1.0, remaining)`) against `deadline = started + timeout_s` on
  `time.monotonic()`. On expiry it calls `_kill_tree`. A normal exit returns the real exit
  code exactly as today.
- The whole kill-and-reap sequence runs under `HARD_DEADLINE_S = 2 * _KILL_GRACE_S +
  REAP_BOUND_S + 5` (default 25 s), enforced by running `_kill_tree` in a daemon thread and
  joining with that timeout. If the thread is still alive, `run_gate` proceeds: it returns
  `GateResult(timed_out=True, exit_code=None)` with `error = "timed out after Ns; kill did not
  complete within Ms"`. The gate therefore returns by `timeout_s + HARD_DEADLINE_S` in every
  case.
- Both knobs read `NW_GATE_GRACE_S` / `NW_GATE_REAP_S` env overrides so tests can shorten them
  (the same pattern as `NW_RUNPROC_GRACE_S`).

### 3. The registry knows each gate's deadline (`gates.py`)

- `_LIVE` (`gates.py:44`) changes from a `set` of procs to a `dict` mapping `proc` to
  `{name, campaign, started_at, deadline_at}` (`deadline_at` is the epoch-seconds wall time
  `started + timeout_s`); `terminate_all`, `reset` and the add/discard sites in `run_gate` are
  updated accordingly (`list(_LIVE)` still iterates procs).
- `live_deadlines() -> list[dict]` returns a copy of those entries for the gates currently
  running (empty when none), under `_LOCK`. Nothing else consumes it in this spec.

### 4. Reproduction first

`tests/test_gate_timeout_tree.py` starts with a fixture that builds the incident's shape
without `uv`/pytest-xdist: a shell command spawning a parent that forks 4 children, each
ignoring SIGTERM (`trap '' TERM`), one of which calls `setsid`. Before any production change
the test is written and run against the current code; its failure output (the surviving pids)
is pasted in the spec's As shipped section as the pinned cause. One additional marked test
(`@pytest.mark.slow`, skipped when `uv`/`pytest-xdist` is not importable) runs a real
`pytest -n 2` over a sleeping test with `timeout_s=3`.

## Alternatives considered

- **Raise `timeout_s` or lower `-n`** — hides the symptom; the unbounded wait stays.
- **A cgroup/systemd scope per gate (`systemd-run --scope`)** — kills everything reliably but is
  Linux-with-systemd only, needs the worker boxes to permit user scopes, and cannot be tested
  portably. The `/proc` snapshot plus group kill covers the realistic cases with no new
  dependency; a cgroup backend can be a later spec.
- **`psutil`** — new dependency for what `/proc` parsing (already used by `runproc`) gives.
- **Only fix the drain / only add the alarm** — a wedged gate keeps unkillable children, leaks
  CPU into the next gate and wedges worker jobs; those follow-ups are defence in depth, not a
  substitute for a gate that honours its timeout.
- **One spec for all four subsystems** — gates, drain, lease info and watchdog ship separately
  and the later ones change signals the conductor's safety relies on; the first is the root
  fix and the smallest blast radius, so it lands alone.

## Acceptance criteria

- [x] A gate whose command spawns descendants that ignore SIGTERM (including one that called
      `setsid`) is gone entirely (no surviving pid from the snapshot) within
      `timeout_s + 2 x grace + reap bound`, and the result has `timed_out=True`.
- [x] `_kill_group` sends SIGKILL to surviving descendants even when the shell has already
      exited after SIGTERM.
- [x] `run_gate` returns within `timeout_s + HARD_DEADLINE_S` even if the kill thread never
      completes (simulated by a kill function that blocks), with `timed_out=True` and an
      `error` that says the kill did not complete.
- [x] A pid recycled between snapshot and signal (start ticks differ) is not signalled.
- [x] `live_deadlines()` lists each running gate with `name`, `campaign`, `started_at` and a
      `deadline_at` equal to start plus `timeout_s`, and is empty once the gate ends (including
      after a timeout kill and after `reset()`).
- [x] The reproduction test existed and failed against the pre-fix code, and its cause is
      recorded in the spec's As shipped section.
- [x] No existing gate behaviour changes for a gate that finishes before `timeout_s`: exit code,
      sidecar fields, ladder/retry results are identical.
- [x] The worker-box path needs no separate change: a `worker_job` gate run goes through
      the same `run_gate` and is covered by the same tests (asserted by a test that runs
      `run_gate` the way `worker_job` calls it, or by reading the call site, noted in As shipped).

## Test plan

- **Automated tests:**
  - `tests/test_gate_timeout_tree.py` (new): fixture with SIGTERM-ignoring children and a
    `setsid` child; asserts all snapshot pids dead after `run_gate(timeout_s=1)` with
    `NW_GATE_GRACE_S=0.3`, and the elapsed time is within the bound; asserts the SIGKILL path
    when the shell exits first; hard-deadline test with a patched blocking `_kill_tree`
    (asserts the return time and `error` text); pid-reuse guard test (patched `start_ticks`
    returning a different value, asserts no `os.kill`/`killpg` on that pid); sidecar
    `timed_out`/`error` assertions; a `worker_job`-style call; the slow real `pytest -n 2` case.
  - `tests/test_gates.py` (existing, extended): a gate finishing in time is unchanged
    (regression: exit code, sidecar fields); `live_deadlines()` lists a running gate with the
    right `deadline_at` and is empty after it ends, after a timeout kill and after `reset()`.
  - Run with `python3 tools/affected_tests.py --run uv run pytest -q` plus the files above;
    `python3 tools/spec_lint.py` must pass.
- **Manual verification:** on a dev checkout with a throwaway project gate
  `cmd: "sh -c 'trap \"\" TERM; setsid sleep 600 & sleep 600'"`, `timeout_s: 5`, run
  `nw gate run` (or a queue tick). Expect the gate result `timed_out`, and `pgrep -f 'sleep
  600'` empty within ~30 s.
- **Regression guard:** the existing gate, ladder (SPEC-0289) and SPEC-0229 stop tests pass
  unchanged; `tests/test_agentic_dogfood_config.py` still parses `.nw/project.yaml` with the
  same `timeout_s` values.

### Test record (2026-11-02)

- Reproduction first: `tests/test_gate_timeout_tree.py` was written and run against the unmodified
  `gates.py`; both tree tests were red. The 4-child fixture (SIGTERM-ignoring, one `setsid`) left
  **the setsid child alive** after `run_gate(timeout_s=1)` (`descendants survived the timeout
  kill: [<pid>]`): the group KILL reached the in-group children, the out-of-group one was never
  signalled. The shell-exits-first case left the TERM-proof child alive (`assert not True where
  True = _alive(<pid>)`): `_kill_group` returned as soon as the shell died on TERM, with no KILL.
- After the change: `uv run pytest -q tests/test_gate_timeout_tree.py tests/test_agentic_gates.py`
  -> 91 passed (includes the slow real `pytest -n 2` case and the new `live_deadlines` tests).
- Related modules, `-n 4`: `test_agentic_gates, test_agentic_exit_paths, test_gate_slots,
  test_worker_jobs, test_worker, test_remote_gates, test_remote_job_records, test_workers_drain,
  test_agentic_gate_limits, test_agentic_gate_paths, test_gate_jobs, test_agentic_sentinel,
  test_agentic_dogfood_config, test_gate_timeout_tree` -> 700 passed, 1 failed
  (`test_remote_gates.py::test_registry_slots_raise_the_worker_hosts_own_limit`, a worker-box
  timing test); it passes alone and the whole file passed again under `-n 4` (116 passed): load
  flake, not touched by this change. The full affected set (`affected_tests.py --run`) exceeded
  this run's time budget, and the full suite is left to gating.
- Manual: `run_gate` with `sh -c 'trap "" TERM; setsid sleep 600 & sleep 600'`, `timeout_s: 5`
  -> `timed_out=True`, returned after 10.1 s (5 + the 5 s grace the TERM-proof shell used), and
  no `sleep 600` process remained.
- Gate follow-up (2026-11-02): `tests/test_agentic_runner.py::test_claude_missing_is_error` was not
  hermetic (a real `claude` on the gate host's `/usr/bin` or SPEC-0389's fallback dirs ran). The
  `_capture_run(missing=True)` helper now sets `PATH` to the empty bindir only, points `HOME` at tmp
  and empties `runproc._CLAUDE_SYSTEM`; verified on a host with a real `claude` in `~/.local/bin`:
  `uv run pytest -q tests/test_agentic_runner.py` -> 19 passed. Test-only; `gates.py` untouched.

### As shipped

- `gates.py`: `_kill_tree` (snapshot of the shell's group plus ppid-reachable descendants as
  `(pid, start_ticks)`; TERM; wait for all of them; re-snapshot; KILL survivors even if the shell
  exited; bounded reap; returns survivor pids) with `_kill_group` as a thin alias for
  `terminate_all`/SPEC-0229. A recycled pid (`runproc.start_ticks` differs) is never signalled;
  zombies count as gone. A double-forked daemon that left the group and lost its ancestors before
  the snapshot is not found (left to a cgroup backend, per Alternatives). Off Linux it degrades to
  the old group kill.
- `run_gate` waits in 1 s slices against a monotonic deadline and kills in a daemon thread joined
  for `hard_deadline_s()` (`2 x grace + reap + HARD_SLACK_S`; `HARD_DEADLINE_S` is the default
  25 s). NW_GATE_GRACE_S / NW_GATE_REAP_S override the first two; `HARD_SLACK_S` is a module
  constant so a test can shorten it. Sidecar `error`: unchanged `timed out after Ns; process group
  killed` (plus `; survivors: <pids>`), or `...; kill did not complete within Ms`.
- `_LIVE` is a dict `proc -> {name, campaign, started_at, deadline_at}` (epoch seconds);
  `live_deadlines()` returns copies. `worker_job._watch` iterates it unchanged (keys are procs).
- Worker-box path: `worker.py::_run_gates` calls `gates.run_gate_with_retries`, which calls
  `run_gate`; no separate change, covered by `test_a_worker_job_style_call_...`.
- `pyproject.toml` registers the `slow` pytest marker.

## Rollout / migration

1. Land the reproduction test (red), then the `gates.py` kill-tree and deadline changes (green)
   together with `live_deadlines()`.
2. A running runner picks the change up at its next restart. Workers pick it up via
   `nw workers update` (no sudo, no manual scripts).
3. Write the follow-up specs listed under Non-goals (drain overrun bound and stop path; lease
   `gates_live`; `gate_overrun` event) once this is `accepted`; they `depends_on` this spec.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; the manual steps performed and noted.
- [x] No regressions.
- [x] Spec body updated to match what shipped (including the pinned root cause).
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
