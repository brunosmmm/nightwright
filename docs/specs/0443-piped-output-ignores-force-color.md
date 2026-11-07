---
id: SPEC-0443
title: Piped output ignores FORCE_COLOR when deciding to wrap
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [cli, rich]
depends_on: [SPEC-0197]
---

## Context

[SPEC-0197](./0197-piped-human-output-never-hard-wraps.md) keeps one record per line when
stdout is not a terminal, by setting `soft_wrap` from Rich's `is_terminal`. Rich treats
any set `FORCE_COLOR`, including `0`, as a terminal. In that environment a pipe hard-wraps
at the console width, and `nw queue watch` takes the alternate screen, so a captured run
prints nothing. `NO_COLOR` is a separate switch: Rich drops color codes even when a caller
passes `color_system`.

## Goals / Non-goals

**Goals**
- Line-shaped piped output stays one line when `FORCE_COLOR` is set.
- `nw queue watch` uses the alternate screen only when stdout is a real tty (or
  `force_terminal` was set explicitly).
- An explicit `force_terminal=True` still wraps, and `soft_wrap=False` still wraps.

**Non-goals**
- Changing Rich's own `is_terminal`, or disabling color when `FORCE_COLOR` is set.
- Ignoring `NO_COLOR` for real commands. A test that asserts a color code opts out
  with `no_color=False`.

## Decision

`stdout_is_tty` is the wrap and watch-screen check. It follows an explicit
`force_terminal` and otherwise calls `file.isatty()`. `NwConsole.print` uses that
instead of `is_terminal`. `queue watch` uses it for the frame height, fill, and
`Live(screen=…)`.

## Design

`src/nightwright/console.py` defines `stdout_is_tty`. `cli_queue.queue_watch` calls it
for the live view. The queue-list color test constructs its console with `no_color=False`
so an ambient `NO_COLOR` does not strip the red overdue marker it asserts.

## Alternatives considered

- **Unset `FORCE_COLOR` in the test process.** Hides the pipe-wrap bug from the suite
  and leaves `runner.log` broken wherever the variable is set.
- **Override `Console.is_terminal`.** That would also drop colors that `FORCE_COLOR`
  is meant to keep on a pipe.

## Acceptance criteria

- [x] With `FORCE_COLOR=0` and stdout not a tty, a 200-character line printed on
      `NwConsole` is one line.
- [x] `nw queue watch` captured by CliRunner contains the dashboard text when
      `FORCE_COLOR` is set.
- [x] Piped campaign log, launch dry-run, runner log, campaign show, and the worker
      busy message stay single lines under `FORCE_COLOR`.
- [x] The overdue `!` test still sees red when `NO_COLOR` is set in the environment.

## Test plan

- **Automated tests:** `tests/test_console_piped.py` (including
  `test_force_color_does_not_wrap_a_pipe`), plus the eight tests that failed under
  `FORCE_COLOR=0` / `NO_COLOR=1`: `test_busy_exit_5_message`,
  `test_cli_campaign_log_note_one_line`, `test_cli_mailbox_thread_message_one_line`,
  `test_list_marks_overdue_and_colours_parks_by_owner`, `test_watch_renders`,
  `test_runner_log_renders_drain_tick_and_admission_blocked`,
  `test_default_plan_argv_and_env`, `test_campaign_show_prints_the_exact_remote_line`.
  17 passed with those variables still set. The full suite was not re-run.
- **Manual verification:** N/A — the failing commands are the tests above.
- **Regression guard:** `test_terminal_still_wraps` and `test_explicit_soft_wrap_false_is_respected`.

## Rollout / migration

No data move. The next process that imports `nightwright.console` picks it up.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the listed tests pass.
- [x] No regressions in that set.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

None.
