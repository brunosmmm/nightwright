---
id: SPEC-0528
title: "CLI signal handlers scoped to the command: herdr watch, gate run, campaign tail restore the previous handler; bounded in-process watch loop"
status: accepted
owner: user
created: 2026-11-10
updated: 2026-11-10
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, herdr, gates, tests, reliability]
source_idea: IDEA-1136
---

## Context

Two box gates hung at ~98% until the 2400 s gate timeout (GATEJOB-1485; campaign
SPEC-0476-20261109-ca46 gate 010 retry r2, `gates/010-pytest-affected.r2.log`). When the
timeout's SIGTERM reached the stuck xdist worker it printed

```
File ".../src/nightwright/agentic/cli_herdr.py", line 66, in _term
    raise KeyboardInterrupt
```

so `nw herdr watch`'s SIGTERM -> KeyboardInterrupt handler was still installed in a pytest
worker long after the test that ran it. `herdr_watch` (cli_herdr.py) installed it with a bare
`signal.signal` that was skipped only off the main thread; an xdist worker runs tests *in* its
main thread, so every in-process `CliRunner().invoke(cli, ["herdr", "watch", "--once"])` in
`tests/test_herdr.py` left it behind for the rest of the worker. The same pattern exists in
`nw gate run` / `gate _exec` (`_stop_on_signals`: SIGTERM *and SIGINT* routed to a handler that
sets an event nobody reads any more, i.e. the worker silently ignores both afterwards) and in
`nw campaign tail --follow-campaign` (SIGTERM/SIGHUP -> KeyboardInterrupt). A leaked handler
turns any later SIGTERM/SIGINT to the worker into a KeyboardInterrupt raised in arbitrary code,
or into nothing, instead of the default termination, which is how a gate's kill turns into a
wedged worker. Earlier the same day `tests/test_herdr_autowatch.py::
test_manual_watcher_is_not_doubled_nor_killed` also flaked on the box.

The in-process watch *loop* itself is used by one test (`test_watch_loop_pushes_and_stops`),
which stops it with an event; nothing bounded it if the stop were missed, and its thread was
non-daemon, so a missed stop would keep the worker from ever exiting.

## Goals / Non-goals

**Goals**
- A command's signal handler lives exactly as long as the command; afterwards the previous
  handler is back (in-process callers, tests above all, are left untouched).
- The watch loop has a bounded stop path that in-process callers (tests) use.
- A stuck test names itself in a gate log instead of a silent stall at 98%.

**Non-goals**
- Production behaviour: SIGTERM still ends `herdr watch` cleanly (clears what it pushed),
  still cancels `gate run`/`_exec` as `cancelled`, still exits `campaign tail
  --follow-campaign` with 130.
- Other handler installers (`worker.py`, `queue.py`, `cli_worker.py`) already restore theirs;
  `runproc`/`worker_job`/`cursor_exec`/`executor_slurm` install them in their own dedicated
  processes. Unchanged.

## Decision

One helper, `agentic/scoped_signals.py::scoped({sig: handler})`, a context manager that
installs the handlers (skipping a signal `signal.signal` refuses off the main thread) and
restores each previous handler in `finally` (a previous handler not installed from Python,
`None`, is restored as `SIG_DFL`). `herdr watch`, `gate run`/`gate _exec` and `campaign tail
--follow-campaign` use it around exactly the work the handler is for. `Watcher.run` gains
`max_s` (a clean exit once it passes; `None`, the CLI default, runs until stopped). pytest gets
`faulthandler_timeout = 300` so a test stuck for 5 minutes dumps every thread's stack into the
gate log (it does not fail or kill the test; no pytest-timeout dependency).

## Design

- `src/nightwright/agentic/scoped_signals.py`: `scoped(handlers)` yields the set of signals it
  actually installed.
- `cli_herdr.herdr_watch`: `with scoped({SIGTERM: _term})` wraps the `w.run(...)` try/except/
  finally (KeyboardInterrupt -> clean exit, `clear_state` unless `--once`), so the handler is
  back to the previous one when the command returns, also when it raises.
- `cli_gate._stop_on_signals` becomes a context manager yielding the stop event, with SIGTERM/
  SIGINT scoped to the `J.follow` / `J.execute` call; `_report` and `sys.exit` run after.
- `cli_campaign` `tail --follow-campaign`: SIGTERM/SIGHUP scoped to `TL.follow_campaign`;
  KeyboardInterrupt -> rc 130; `sys.exit(rc)` after the scope.
- `herdr_watch.Watcher.run(max_s=None)`: the loop also returns (through the same `finally`:
  clear + close run panes) once `max_s` has passed; its sleep never overshoots the bound.
- `tests/test_herdr.py::test_watch_loop_pushes_and_stops` runs the loop with
  `max_s=waits.WAIT_S` on a daemon thread.
- `pyproject.toml` `[tool.pytest.ini_options]`: `faulthandler_timeout = 300` (not a gate key in
  `.nw/project.yaml`; a new gate key there would park every campaign under a stale runner).

## Alternatives considered

- **Install the handler only when not `--once`** — fixes the tests' `--once` calls but not the
  loop form, nor `gate run`/`campaign tail`; restore-on-return is the general rule.
- **A conftest fixture that resets SIGTERM after every test** — hides the leak instead of
  fixing the commands; any non-test in-process caller would still inherit it.
- **pytest-timeout** — a new dependency that kills the test; the faulthandler dump is enough to
  name a stall, and the gate's own `timeout_s` already bounds the run.

## Acceptance criteria

- [x] After `nw herdr watch` returns in-process (`--once`, the loop ended by a gone `--parent`,
      and the loop ended by a SIGTERM), `signal.getsignal(SIGTERM)` is the handler from before;
      during the watch it is the command's handler.
- [x] After `nw campaign tail --follow-campaign` returns in-process (exit 130), SIGTERM and
      SIGHUP handlers are the ones from before.
- [x] `gate`'s `_stop_on_signals` sets its stop event on SIGTERM inside the block and restores
      SIGTERM/SIGINT after it.
- [x] `scoped` restores on an exception, leaves handlers alone off the main thread, and restores
      a non-Python (`None`) previous handler as `SIG_DFL`.
- [x] `Watcher.run(max_s=...)` ends on its own; the in-process loop test uses it on a daemon
      thread.
- [x] `faulthandler_timeout = 300` is in pytest's ini options and the herdr/gate tests pass
      with it.
- [x] Production SIGTERM paths unchanged: the existing subprocess tests (`herdr watch`
      SIGTERM'd by `test_manual_watcher_is_not_doubled_nor_killed`, `campaign tail` viewer
      `proc.terminate()` -> 130, gate-job cancel) pass.

## Test plan

- **Automated tests:**
  - `tests/test_herdr.py::test_cli_watch_handler_lives_only_while_the_command_runs` (new):
    the three `herdr watch` shapes above, handler captured inside `Watcher.run` and after.
  - `tests/test_herdr.py::test_follow_campaign_cli_and_restored_pane_shows_the_last_run`:
    asserts SIGTERM/SIGHUP restored after the in-process `campaign tail --follow-campaign`.
  - `tests/test_scoped_signals.py` (new, `fast`): `scoped` restore-on-error, off-main-thread,
    `None` -> `SIG_DFL`; `cli_gate._stop_on_signals` set + restore.
  - `tests/test_herdr.py::test_watch_loop_pushes_and_stops` with `max_s` + daemon thread.
- **Manual verification:** repeat the herdr test files under xdist
  (`for i in $(seq 20); do timeout 180 uv run pytest -q -n 4 -p no:cacheprovider
  tests/test_herdr*.py tests/test_scoped_signals.py || break; done`), plus a run of the
  signal-touching test files with an audit-hook plugin that logs any SIGTERM handler still
  changed after a test (leak) and any kill aimed at the worker's own pid/group.
- **Regression guard:** affected tests (`tools/affected_tests.py`) on the box via
  `nw gate run --tier loop`; `spec_lint` and `tools/test_tiers.py --check`.

## As shipped

- Shipped as designed (`scoped_signals.scoped`; `herdr watch`, `gate run`/`_exec`,
  `campaign tail --follow-campaign` use it; `Watcher.run(max_s)`; `faulthandler_timeout = 300`).
- Leak audit (an audit-hook plugin logging a SIGTERM handler still changed after a test and any
  kill aimed at the worker's own pid/group), over the 34 signal-touching test files, before the
  fix: 10 leaking tests: `test_gate_jobs.py` x6 (`_stop_on_signals`: SIGTERM+SIGINT became
  no-ops for the rest of the worker), `test_herdr.py` x4 (`herdr_watch._term`,
  `campaign_tail._bye`). After the fix, the same plugin over `test_gate_jobs`, `test_herdr*`,
  `test_scoped_signals`, `test_remote_workers_integration`: 0 leaks. The only self-kills are
  `test_worker.py`'s three deliberate SIGTERM tests, under their own (restored) handler.
- No in-process loop hang reproduced: every in-process `herdr watch` in the tests is `--once`
  (bounded), and the one in-process loop test stops on an event. 8 x pre-fix and 20 x post-fix
  `pytest -n 4 tests/test_herdr*.py (+ test_scoped_signals.py)`: all green. The confirmed defect
  is the leaked handler that changes how a worker reacts to SIGTERM/SIGINT; the exact test that
  stalled the two gates is not named by the logs, which is what `faulthandler_timeout = 300` is
  for (a stall > 5 min now dumps every thread's stack into the gate log).
- `faulthandler_timeout` works with the existing setup (no pytest-timeout): local runs pass with
  it; it only dumps, never fails a test.
- `tests/test_remote_workers_integration.py::test_no_workers_run_matches_the_pre_epic_golden`
  fails on main too (verified with this change stashed); unrelated.
- Box gate (`--tier loop`): not run yet: the box was stuck `updating (draining, 1 jobs)` with no
  jobs for > 40 min (`nw gate run` exit 3, "no placeable gate worker"). Status stays `accepted`
  until that gate is green.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions (box gate, loop tier).
- [ ] Spec body updated to match what shipped (As shipped).
- [ ] `docs/LEDGER.md` updated (status + date).
