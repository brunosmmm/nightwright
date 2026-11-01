---
id: SPEC-0365
title: "herdr watcher auto-start: the conductor follower supervises nw herdr watch"
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-661
parent: SPEC-0360
depends_on: [SPEC-0363, SPEC-0364]
tags: [agentic, observability, herdr, conductor]
---

## Context

Promoted from idea `IDEA-661`. Child of epic
[SPEC-0360](./0360-look-inside-running-agents-herdr-status-reporting.md).

`nw herdr watch [--run-panes]` (SPEC-0361/0363/0364) pushes the queue's display agents,
workspace tokens, notifications and run panes into herdr. Today the human starts it by hand in
a spare herdr pane and restarts it after every upgrade (`docs/herdr.md`: "The watcher does not
restart when nightwright's code changes"). The human asked for no manual step.

What exists, checked against the code on 2026-11-01:

- The conductor's event follower, `nw queue events --follow --for conductor`
  (`events.follow`, `cli_conduct.queue_events`), runs inside the conductor's herdr pane for as
  long as the queue is conducted. It holds the `conductor` reader flock for its lifetime,
  records its herdr placement (`conductor_cost.record_herdr_pane`), polls every `poll_s` (2 s),
  and **its stdout is the conductor's Monitor event stream**: every line wakes the conductor.
- The follower exits 0 with one `code_changed` line when nightwright's code changes
  (`codewatch.Watcher`, SPEC-0231); the conductor then re-arms the Monitor, which starts a
  fresh follower on the new code. That is the codebase's existing upgrade mechanism.
- The watcher holds the `herdr` reader flock (`events.reader_lock`) for its lifetime, so two
  watchers already cannot run at once; the second exits 1 naming the holder.
- Liveness by pid + `/proc/<pid>/stat` start ticks is the codebase's pattern
  (`conductor_cost.placement_live`, `runproc.start_ticks`).
- Epic invariant 2: the queue loop, the runner, `runproc` and the event stream never import
  `agentic/herdr.py` (`test_queue_runner_runproc_never_import_herdr`).

## Goals / Non-goals

**Goals**
- When the conductor follower runs inside herdr, `nw herdr watch` runs too, with no manual
  step, and stops with it.
- Exactly one watcher; a manually started one is neither doubled nor killed.
- The watcher can never write to, break, block or slow the conductor's event stream.
- Crashes restart with backoff; upgrades restart it; status is visible.

**Non-goals**
- Auto-starting from steward followers (one queue, one watcher: the conductor's follower is
  the natural owner).
- Changing what the watcher pushes (SPEC-0363/0364 unchanged).
- A SIGTERM handler in the follower (see Decision).

## Decision

The conductor follower (`nw queue events --follow --for conductor`) owns a best-effort
**supervisor** (`agentic/herdr_autowatch.py`) that it ticks once per poll loop. When the
follower runs with `HERDR_ENV=1`, `herdr` on `PATH` and `agentic.herdr.auto_watch` not false
(default **on**), the supervisor starts `python -m nightwright herdr watch --parent
PID:TICKS` as a detached child (own session, stdin `/dev/null`, stdout+stderr appended to
`<data_dir>/agentic/conductor/herdr_watch.log`, no inherited fds). Run panes follow
`agentic.herdr.run_panes` (the watcher reads it, as before).

- **Single instance**: the `herdr` reader flock stays the authority. The watcher additionally
  records `<data_dir>/agentic/conductor/herdr_watch.json` (`pid`, `start_ticks`, `mode`
  auto|manual, `parent_pid`, `parent_start_ticks`, `started_at`, `log`) after it takes the
  lock and removes it (only if still its own) on exit. Before each start the supervisor skips
  if that record is live (pid alive, start ticks match) or the flock is held; it then rechecks
  every 5 s and starts one as soon as the other is gone. It never signals a process it did not
  start.
- **Stops with the follower**: on any normal follower exit (`code_changed`, Ctrl-C, broken
  pipe, `max_events`) a `finally` sends SIGTERM to the child (the watcher clears its labels and
  closes its run panes). It does not wait for it.
- **Hard follower death** (SIGKILL, SIGTERM without a handler, Monitor teardown): the watcher
  checks its `--parent PID:TICKS` every poll (2 s, below one push interval) and exits cleanly
  when the parent is gone or its start ticks differ.
- **Crash restart**: when the child exits, the supervisor logs it and restarts after a backoff
  of 5 s doubling to 300 s; a child that ran 300 s resets the backoff.
- **Upgrade**: reuse SPEC-0231. After a code change the follower exits with `code_changed`,
  which stops its watcher; the re-armed follower starts a new watcher on the new code (once
  the old one has released the lock). With `--no-code-watch` neither restarts (as today for
  the follower).
- **Best effort**: every supervisor step is in `try/except`; five consecutive failures turn
  the supervisor off for the follower's lifetime (one log line). The supervisor writes only to
  the log, never to the follower's stdout or stderr. It does not import `agentic/herdr.py`
  (it repeats the two-line `HERDR_ENV` / `which herdr` gate) and is imported lazily only by the
  conductor's `--follow`.
- **Status**: `nw queue conduct-status` (text and `herdr_watch` in its JSON) and a new
  `nw herdr status [--json]` (`nw.herdr.watch.v1`) show running/pid/mode/log path/auto_watch.

No SIGTERM handler is added to the follower: the conductor's most critical process keeps its
exact signal behaviour; the parent check covers that path within 2 s.

## Design

New `src/nightwright/agentic/herdr_autowatch.py` (no herdr import):

- `log_path(cfg)`, `state_path(cfg)` under `conductor_cost.conductor_dir(cfg)`.
- `auto_watch_enabled(cfg)` (`agentic.herdr.auto_watch`, default True; false/0/"false"/"off"
  disable); `gate(cfg, env)` → one-line reason or None (config off, `HERDR_ENV` != 1, no
  `herdr` on `PATH`).
- `write_state(cfg, mode, parent)` / `clear_state(cfg)` (atomic replace; clear only if the
  file's pid is ours), `read_state(cfg)`, `alive(pid, ticks)`, `status(cfg)` →
  `{schema, auto_watch, running, pid, mode, parent_pid, started_at, log}` from the state file
  only (no flock probe).
- `parse_parent("PID:TICKS")`, `watcher_argv(cfg, pid, ticks)`, `child_env(cfg, env)` (adds
  `NW_CONFIG_DIR`/`NW_DATA_DIR` like SPEC-0364's typed tail, `PYTHONUNBUFFERED=1`).
- `Supervisor(cfg, env=, clock=, popen=, holder=)`: `start()` evaluates the gate once (a
  gated supervisor is inert, logs nothing, creates no file); `tick()` never raises; `stop()`
  SIGTERMs only its own live child. A failed step also backs off (5 s doubling); the
  "5 in a row" error count resets only on a successful spawn. Constants `BACKOFF_MIN_S=5`, `BACKOFF_MAX_S=300`,
  `STABLE_S=300`, `RECHECK_S=5`, `ERRORS_MAX=5`, `LOG_MAX_BYTES=5 MiB` (rotated to `.log.1`
  before a spawn).

`cli_conduct.queue_events`: only for `--follow --for conductor`, builds the supervisor, passes
`stop=` a callable that ticks it and returns False, and stops it in `finally`. `events.follow`
is unchanged; its `stop` hook runs inside the reader lock once per loop, so a second conductor
follower (lock refused) never starts a watcher.

`cli_herdr.herdr_watch`: hidden `--parent PID:TICKS`; mode `auto` with it, else `manual`.
`herdr_watch.Watcher.run(..., on_locked=None)` calls `on_locked()` right after taking the
lock (the CLI writes the state there, not for `--once`) and the CLI clears it on exit. With
`--parent` the run's `stop` returns True once the parent is gone. New `nw herdr status`.

`conduct_status` JSON gains `herdr_watch` (status dict); the text view prints one line.

## Alternatives considered

- **Thread inside the follower running the watcher** — shares the conductor's process: a hung
  herdr call, a crash or a print would land in the event stream. Rejected.
- **A systemd user unit / herdr config hook** — outside nightwright, needs human setup.
- **SIGTERM handler in the follower** — prompter cleanup, but changes the conductor's signal
  semantics; the 2 s parent check suffices.
- **Supervisor in `events.follow`** — every reader would carry it; keeping it in the CLI
  layer, conductor only, leaves `events.py` untouched.
- **Own codewatch in the supervisor** — duplicates SPEC-0231's exit + re-arm; extra `git` per
  10 s in the conductor's process.

## Acceptance criteria

- [x] A conductor follower with `HERDR_ENV=1` and `herdr` on PATH starts exactly one
      `nw herdr watch` child (mode auto), whose stdout/stderr go to
      `<data_dir>/agentic/conductor/herdr_watch.log`; the follower's stdout carries no watcher
      or supervisor output, and the child holds no fd of the follower's stdout.
- [x] `agentic.herdr.auto_watch: false`, `HERDR_ENV` unset, or no `herdr` on PATH: no child,
      no log, no state file. Steward/other readers never start one.
- [x] A live watcher (manual or another follower's) is not doubled and never signalled; when
      it goes away the supervisor starts one within the recheck interval.
- [x] Normal follower exit SIGTERMs the child, which clears what it pushed.
- [x] Follower killed with SIGKILL: the child exits within one push interval and clears.
- [x] A crashed child is restarted with backoff (5 s doubling, cap 300 s).
- [x] Supervisor failures (spawn error, unwritable log, exceptions) never raise into the
      follower, never print to it, and turn the supervisor off after 5 in a row.
- [x] `nw herdr status [--json]` and `nw queue conduct-status [--json]` show running, pid,
      mode and log path, robust to a stale pid (start ticks).
- [x] The import-isolation test still passes (no `agentic.herdr*` imported by the queue,
      runner, runproc, events, mailbox, cli).
- [x] `docs/herdr.md` says there is no manual step, how to turn it off, where the log is.

## Test plan

- **Automated** (`tests/test_herdr_autowatch.py`, fake herdr from `tests/fixtures`):
  gate/config parsing; state write/clear/stale (dead pid, wrong ticks); supervisor with an
  injected fake popen + clock: one spawn, no respawn while running, backoff sequence on
  crashes and reset after a stable run, skip + recheck while a live holder exists, never
  signals a foreign pid, stop SIGTERMs only its own child, errors swallowed and supervisor off
  after 5; a real `nw queue events --follow --for conductor` subprocess under a fake data dir
  with the fake herdr on PATH: exactly one watcher child, its log has content, follower stdout
  empty of watcher output, child fds exclude the follower's stdout pipe, SIGINT to the
  follower stops the child and clears its state, SIGKILL to the follower → child exits; auto
  off/no herdr → no child; `nw herdr status --json`; conduct-status JSON key. Waits via
  `tests/waits.py`.
- **Manual / real**: an isolated `herdr --session nw-test-<rand> server`, the human's
  `HERDR_*` stripped, a real follower against a fake data dir inside the test session's pane
  env: watcher starts once, writes only to its log, the follower's stdout has no watcher
  output; the conductor pane's display agent appears; kill -9 the watcher → restarted; kill
  the follower → watcher exits and the display agent is cleared. Teardown: `server stop`,
  `session delete`.
- **Regression guard**: `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_herdr.py`, `tests/test_agentic_events*.py`, conduct/queue CLI tests.

## As shipped (2026-11-01)

- **Files:** new `src/nightwright/agentic/herdr_autowatch.py` (gate, state file, status,
  `Supervisor`), `cli_conduct.py` (`_herdr_supervisor` for `--follow --for conductor` via the
  `stop` hook + `finally: stop()`; `herdr_watch` in `nw.conduct.status.v1` and one text line),
  `cli_herdr.py` (hidden `--parent PID:TICKS`, state write/clear, `nw herdr status [--json]`),
  `herdr_watch.py` (`Watcher.run(on_locked=)`), `tests/conftest.py` (autouse: strip ambient
  `HERDR_*` for every test, so a follower under test can never auto-start a watcher against the
  human's herdr when pytest runs from a herdr pane), new `tests/test_herdr_autowatch.py`,
  `docs/herdr.md` ("It starts by itself", how to turn it off, log path),
  `docs/product/reference/index.md`.
- **Design as written.** One refinement found by the tests: the error counter resets only on a
  successful spawn (resetting on any quiet tick meant it never reached 5), and a failed step
  backs off like a crash.
- **Real herdr 0.9.5, isolated session only** (`herdr --session nw-test-4df43aa1 server`, the
  human's `HERDR_*`, `CLAUDE_CODE_SESSION_ID` stripped, `HERDR_SOCKET_PATH` = the test
  session's socket; a workspace `w1` with pane `w1:p1` given a `claude` agent). A real
  follower (`python -m nightwright queue events --follow --for conductor`, the worktree's
  code) ran with `HERDR_ENV=1 HERDR_PANE_ID=w1:p1 HERDR_WORKSPACE_ID=w1` against a fake
  config/data dir, stdout and stderr to files:
  - within a second: one `nw herdr watch --parent <follower>:<ticks>` (own session id), state
    `mode: auto`; `agent get w1:p1` → `display_agent: "claude · conductor"`; the watcher's fds
    were `/dev/null`, the log twice and `cursors/herdr.lock` (none of the follower's); the
    follower's stdout and stderr stayed empty.
  - `nw herdr status [--json]` and `nw queue conduct-status [--json]` showed pid, `auto`,
    parent and the log path.
  - `kill -9` the watcher → log `exited -9 after 35s; next start in 5s`, a new watcher 6 s
    later, display agent back; still exactly one.
  - a manual `nw herdr watch` meanwhile → `Error: reader 'herdr' is already followed by pid
    …`, exit 1; the auto watcher untouched.
  - committing this change in the worktree moved HEAD: the follower printed exactly one line,
    `code_changed`, exited 0, and the log says `follower exiting; stopped watcher pid …`; the
    watcher exited, its state file was removed and the display agent was cleared (the upgrade
    path, SPEC-0231).
  - a re-armed follower started a new watcher; `kill -9` of the follower → the watcher logged
    `supervising follower pid … is gone; exiting` and exited 1.8 s later; state file gone,
    display agent cleared; the follower's stdout empty.
  - Teardown: `herdr --session nw-test-4df43aa1 server stop`, `herdr session delete
    nw-test-4df43aa1` (`deleted session`); `herdr session list` shows only `default`;
    `~/.config/herdr/sessions/` empty; the fake dirs removed. The human's `default` session
    and live conductor follower were never addressed.
- **Tests executed:** `tests/test_herdr_autowatch.py` 15 passed (unit: gate, state/stale
  ticks, backoff 5→300 and reset, wait-for-other-watcher, stop only own child, errors swallowed
  and off after 5; real processes with the fake herdr: one watcher, fds, crash restart, SIGINT
  stop + clear, SIGKILL of the follower → watcher exits < 10 s, a manual watcher not doubled
  nor killed, no watcher when gated / auto off / non-conductor reader). The conftest change
  makes `tools/affected_tests.py` fall back to the full suite: run by hand with `-n 8`
  (textual-dependent tab tests ignored, `textual` is not installed here): 4854 passed, 87
  skipped, 6 failed — 2 `test_tui_queue_model` (no `textual`, pre-existing), 3 load flakes
  that pass when rerun alone (`test_agentic_outbound_e2e`, `test_agentic_queue` gate timeout,
  `test_worker_jobs` renew), and `test_rich_output_guard`, fixed after rebasing on main
  (`parent_gone` allow-listed: its line goes to the watcher's log; `nw herdr status` prints
  through the Rich console). After the rebase: herdr, autowatch, rich guard, conduct CLI,
  events, followers, quiet, codewatch: 205 passed, 2 skipped (the opt-in contract tests).

## Rollout / migration

Merge; the running conductor follower picks it up on its next start (the merge itself moves
HEAD, so its codewatch makes it exit with `code_changed` and the conductor re-arms it). Stop
any manually started `nw herdr watch` (or leave it: the supervisor waits for it).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
