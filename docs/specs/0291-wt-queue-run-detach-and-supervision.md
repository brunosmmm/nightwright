---
id: SPEC-0291
title: "`wt queue run --detach` and `wt queue stop`: start and stop the runner without a shell recipe"
status: done
updated: 2026-10-30
owner: user
created: 2026-10-30
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-040
parent: SPEC-0169
depends_on: [SPEC-0228, SPEC-0229]
tags: [agentic, queue, runner, conductor]
---

## Context

Promoted from idea `IDEA-040` ("if the nohup launcher proves inadequate", epic SPEC-0169). It
has proved inadequate:

- The conductor starts the runner with a shell recipe, in `skills/wt-conduct/SKILL.md`
  step 2: `setsid nohup wt queue run >> ~/.local/share/wt/agentic/runner.log 2>&1 &`. The data
  dir is hard-coded, so it is wrong for any other `data_dir`. Nothing confirms that the child
  took the lease. A start that fails, for example on a lease that is already held or on a
  broken config, is silent unless someone reads the log.
- Stopping is worse. The decision-table rows `ready_not_admitted`, `waiting_not_admitted`,
  `runner_journal_silent` and `merged` say "SIGTERM the runner … subject to the close guard".
  Each conductor re-implements that by reading `wt queue list --json` `.lease.busy`, comparing
  `since + budget_s` with the clock, finding the pid and running `kill`, then polling.
- The building blocks exist:
  - `src/wt/agentic/lease.py:lease_status` reports `held`, `pid`, `host` and SPEC-0228's
    `busy {kind, campaign, since, budget_s}`.
  - `lease.acquire` refuses a second holder ("queue held by pid …").
  - `queue.run` handles SIGTERM gracefully: it stops admissions, ends gates, detaches persona
    runs for adoption (SPEC-0229) and releases the lease.
  - `config.py` has an unused-in-code `agentic.run_launcher: nohup` default.

## Goals / Non-goals

**Goals**
- `wt queue run --detach [--log PATH]` starts the loop as a detached session leader. It
  confirms that the child holds the lease, prints the pid and the log path, and exits 0. It
  refuses (exit 1) when a live runner already holds the lease. A child that dies or does not
  take the lease within 15 s gives exit 1 and the log tail.
- The lease records `log` and `detached: true`, so `wt queue list --json` /
  `conduct-status` show where the runner writes.
- `wt queue stop [--timeout S] [--wait-guard] [--yes]` sends SIGTERM to the lease holder and
  waits for the lease to be released. It honors the close guard: it refuses (or waits, with
  `--wait-guard`) while `busy.kind` is `open` or `close` within `since + budget_s`. It is a
  dry run without `--yes`.
- The wt-conduct skill uses these two commands instead of the nohup recipe and the
  hand-rolled guard.

**Non-goals**
- A persistent supervisor (systemd units, automatic restarts). The epic explicitly rules it
  out. The conductor's `runner_dead` row stays the restart policy.
- SIGKILL escalation. If the runner does not exit in time, `wt queue stop` reports and exits 1.
  Killing a stuck runner is a human or conductor decision with the process in view.
- `wt queue restart`. A restart is `stop` then `run --detach`, and keeping them separate keeps
  the guard semantics simple.
- tmux launchers. `agentic.run_launcher` is removed from the defaults (see Design).

## Decision

**Detach** re-executes the same `wt` entry point (`sys.argv[0]`, or `[sys.executable, "-m",
"wt"]` when argv[0] is not executable) through `subprocess.Popen` with:

- `start_new_session=True` (the `setsid` equivalent);
- `stdin=DEVNULL`, with stdout and stderr appended to the log;
- the original options minus `--detach` and `--log`;
- the environment variable `WT_RUNNER_LOG=<log>` set.

The parent then polls `lease.read_lease` / `is_held` every 0.2 s for up to 15 s, until the
lease is held with `pid == child.pid`. A fork-based daemon was rejected (see Alternatives).

**Stop** is a lease-level operation. Only the pid recorded in a held lease on this host is
signalled. The close guard is computed from `lease_status` by one shared helper, which the
skill rows can then reference instead of restating it.

## Design

- `src/wt/agentic/cli_queue.py:queue_run`: add `--detach` (flag) and `--log PATH` (default
  `<agentic_dir>/runner.log`, from `store.agentic_dir(cfg)`).
  - `--detach` with `--once` or `--max-ticks` is a usage error. Short runs need no detach.
  - With `--detach`, the command calls the new `Q.detach_runner(cfg, argv, log_path,
    timeout_s=15)` and prints `runner started: pid N, log PATH`. A `ValueError` becomes a
    `ClickException`.
- `src/wt/agentic/queue.py`:
  - New `detach_runner(cfg, argv, log_path, *, timeout_s=15, popen=subprocess.Popen,
    clock=time.monotonic)`:
    1. If `L.is_held(cfg)`, raise `ValueError("queue held by pid … (heartbeat …s ago); stop it
       with wt queue stop")`.
    2. Open the log for append and write a `--- wt queue run --detach <ts> ---` marker.
    3. Start the child.
    4. Poll. Success returns `{"pid", "log"}`. If the child exits first, or the timeout passes,
       raise `ValueError` with the last 20 log lines. On timeout the child is left running,
       and the message says so with its pid, because killing a slow but healthy start is
       worse.
  - In `run`, right after `L.acquire`: if `os.environ.get("WT_RUNNER_LOG")`, call
    `lease.update_info(log=…, detached=True)`. A re-exec (SPEC-0194) inherits the env, so the
    adopted lease keeps them. `lease_status` gains `log` and `detached` while held.
  - New `close_guard(status, now=None) -> dict | None`: returns `busy` when `busy.kind in
    ("open", "close")` and `age(since) <= budget_s`, else None. An unparseable `since` counts
    as guarded, which is the safe side.
  - New `stop_runner(cfg, *, timeout_s=60, wait_guard=False, guard_wait_s=None, kill=os.kill,
    clock=…, sleep=…)`:
    1. Not held: return `{"stopped": False, "reason": "no runner"}`. This is exit 0, because
       the desired state already holds.
    2. A host other than `socket.gethostname()`: raise.
    3. A close guard: raise `ValueError("close guard: <kind> of <campaign> started <since>,
       budget <n>s; retry later or pass --wait-guard")`. With `wait_guard`, poll until the
       guard clears or `guard_wait_s` (default: the remaining budget + 30 s) runs out, which
       raises.
    4. `kill(pid, SIGTERM)`, then poll `is_held` until it is False or `timeout_s` runs out,
       which raises `"pid N did not release the lease within S s (still running); not
       killed"`.
    5. Return `{"stopped": True, "pid", "waited_s"}`.
- `src/wt/agentic/cli_queue.py`: new `wt queue stop`, with options `--timeout` (60),
  `--wait-guard`, `--yes`, `--by` and `--json` (`wt.queue.stop.v1`).
  - Without `--yes` it prints what it would do: the pid, the guard state, and whether it would
    wait.
  - With `--yes` it calls `stop_runner`. It journals nothing itself: the runner's own `stop`
    record carries reason `signal`.
- `src/wt/config.py`: remove `agentic.run_launcher`. No code reads it, and the skill now names
  the command. A user config that still sets it loads fine and the key is ignored: nothing
  validates unknown `agentic` keys (verified in `config.py`).
- `skills/wt-conduct/SKILL.md`:
  - Step 2 becomes: "start it: `wt queue run --detach` (it confirms the lease or prints why
    not)".
  - The rows `runner_dead`, `ready_not_admitted`, `waiting_not_admitted`,
    `runner_journal_silent` and `merged` replace "SIGTERM the runner and restart it … close
    guard: never SIGTERM while …" with "`wt queue stop --yes` then `wt queue run --detach`
    (`stop` refuses inside the close guard: then wait for the next event)". The guard's
    definition stays in one place, the `runner_journal_silent` row.

## Alternatives considered

- **Double-fork daemon in-process (`os.fork` + `os.setsid`).** Rejected. Forking a process that
  has imported threads and click state is fragile. A fresh exec is what the nohup recipe did,
  and it re-reads config cleanly.
- **systemd user unit.** Rejected by the epic (no persistent supervisor). It is also not
  portable.
- **SIGKILL after the timeout.** Rejected. A runner that ignores SIGTERM past SPEC-0229's 30 s
  bound is in a state a human should look at. The close guard exists because a kill can
  leave the main checkout mid-merge.
- **Keep the recipe and only fix the path.** Rejected. It still gives no start confirmation and
  no shared guard.

## Acceptance criteria

- [x] `wt queue run --detach` starts a session-leader child with stdin closed and output
      appended to `--log` (default `<agentic_dir>/runner.log`). It returns 0 only after the
      lease is held by the child's pid, and prints the pid and the log path. The lease and
      `lease_status` carry `log` and `detached: true`, and keep them across a re-exec.
- [x] `--detach` refuses with exit 1, naming the holder, when the lease is held. A child that
      exits early, or a start that does not take the lease within 15 s, gives exit 1 with the
      log tail. `--detach` with `--once` or `--max-ticks` is a usage error.
- [x] `wt queue stop --yes` sends SIGTERM to the local lease holder and returns once the lease
      is released. It returns exit 0 with "no runner" when none holds it, and exit 1 when the
      runner does not release the lease within `--timeout` (it never sends SIGKILL).
- [x] `wt queue stop` refuses inside the close guard (`busy.kind` `open`/`close` within
      `since + budget_s`, or an unparseable `since`). With `--wait-guard` it waits for the
      guard to clear first. Without `--yes` it is a dry run.
- [x] The wt-conduct skill contains no `nohup` recipe. Its runner start and restart steps use
      `wt queue run --detach` and `wt queue stop --yes`. `agentic.run_launcher` is gone from
      the defaults.

## Test plan

- `tests/test_agentic_runner.py` (or a new `tests/test_agentic_detach.py`):
  - `test_detach_refuses_when_held`: hold the lease with `L.acquire` in the test. The command
    raises and names the pid.
  - `test_detach_waits_for_child_lease`: a fake `popen` starts a real child, `python -c` that
    takes the lease through `L.acquire` with its own pid and sleeps. The call returns that
    pid. The child is terminated in teardown.
  - `test_detach_child_dies_early`: the child writes `boom` to stdout and exits 3. You get a
    `ValueError` containing `boom`.
  - `test_detach_timeout_leaves_child`: the child never takes the lease. With the timeout set
    to 0.5 s, the call raises and the message has the pid. The child is still alive, then
    killed in teardown.
  - `test_detach_popen_args`: there is a recording fake `popen`. It gets
    `start_new_session=True`, `stdin=DEVNULL`, the argv without `--detach`/`--log`, and
    `WT_RUNNER_LOG` in the env.
  - `test_run_records_log_in_lease`: `WT_RUNNER_LOG` is set, then `Q.run(once=True)` with
    the fake runner runs. During the tick the lease info has `log` and `detached`, observed
    through `on_tick`.
- `tests/test_agentic_lease.py` / queue tests:
  - `test_close_guard_open_within_budget`;
  - `test_close_guard_expired`;
  - `test_close_guard_drain_not_guarded`;
  - `test_close_guard_bad_since_guarded`.
- Stop tests (child process holding the lease, as above):
  - `test_stop_no_runner_ok`;
  - `test_stop_sigterm_releases`: the child installs a SIGTERM handler that releases and
    exits;
  - `test_stop_timeout_no_sigkill`: the child ignores SIGTERM. The call raises and the child
    is still alive;
  - `test_stop_refuses_in_guard`: the lease's `busy` is written as `close`, now, with a 600 s
    budget;
  - `test_stop_wait_guard_then_stops`: `busy` is cleared by a thread after 0.3 s;
  - `test_stop_other_host_refused`.
- `tests/test_agentic_queue_cli.py`:
  - `test_queue_run_detach_once_usage_error`;
  - `test_queue_stop_dry_run_no_signal` (a monkeypatched `os.kill` is never called);
  - `test_queue_stop_json_schema`.
- `tests/test_skills.py::test_conduct_skill_has_no_nohup_recipe`: the wt-conduct text has no
  `nohup`, and has both `wt queue run --detach` and `wt queue stop`.
- **Sabotage check:**
  - Make `detach_runner` return right after `Popen` without polling the lease.
    `test_detach_child_dies_early` must fail.
  - Make `close_guard` ignore `budget_s`, so it is always guarded.
    `test_close_guard_expired` must fail.
- **Manual:** run `wt queue run --detach` on this machine. `wt queue list --json` shows the
  `.lease.log` path, `wt queue stop --yes` returns within ~30 s, and `wt queue runner-log
  --tail 2` shows `stop signal`.
- **Regression guard:** full `uv run pytest`. A foreground `wt queue run` is unchanged
  (existing runner tests).

### Test record (2026-10-30)

- `uv run pytest tests/test_agentic_detach.py` (new, 19 tests: every detach, close-guard and
  stop test named above, plus `test_reexec_adoption_keeps_log_and_detached`,
  `test_lease_status_without_detach_has_null_detached` and
  `test_stop_wait_guard_times_out`), `tests/test_agentic_queue_cli.py` (the three new CLI
  tests), `tests/test_skills.py` (`test_conduct_skill_has_no_nohup_recipe`), and the related
  `test_agentic_lease`, `test_agentic_drain_signal`, `test_agentic_reexec`,
  `test_agentic_adopt`, `test_agentic_events*`, `test_agentic_config_reload`,
  `test_agentic_dogfood_config`: all pass (198 + 110).
- **Sabotage:** `detach_runner` returning right after `Popen` ⇒
  `test_detach_child_dies_early` fails; `close_guard` ignoring `budget_s` ⇒
  `test_close_guard_expired` fails. Both restored.
- **Manual** (isolated `WT_DATA_DIR` / `WT_CONFIG_DIR` under `/tmp`, so the live runner's
  lease was never touched): `wt queue run --detach --poll-interval 2` printed `runner started:
  pid N, log …/agentic/runner.log` in 0.4 s; a second `--detach` exited 1 with `queue held by
  pid N (heartbeat 0s ago); stop it with wt queue stop`; `ps` showed SID = PGID = pid,
  `/proc/N/fd/0` → `/dev/null`, fds 1/2 → the log; `wt queue list --json` showed `.lease.log`
  and `detached: true`; `wt queue stop` (dry run) printed the plan; `wt queue stop --yes`
  returned in 0.2 s; `wt queue runner-log --tail 2` showed `start` then `stop signal after 3
  tick(s)`; a further `stop --yes` printed `no runner` (exit 0).
- Full `uv run pytest` left to gating (the no-regressions check).

### Test record (2026-10-30, review round 1)

- Added `test_module_start_reexec_and_detach_argv_are_runnable` (a `python -m wt` start's
  re-exec/detach argv is `python -m wt …` and actually runs; the old `python …/wt/__main__.py`
  form fails with ImportError), `test_loop_reexec_keeps_dash_m_wt` (the SPEC-0194 loop
  re-exec under a module start), `test_script_start_argv`,
  `test_detach_relative_log_is_made_absolute`, `test_stop_holder_gone_is_no_runner`,
  `test_stop_permission_error_is_refused`, `test_stop_refuses_a_one_shot_tick`
  (`tests/test_agentic_detach.py`) and `test_queue_run_detach_cli_wiring`
  (`tests/test_agentic_queue_cli.py`: argv, default log, `--log`, output, ValueError ⇒ exit 1).
  `test_agentic_detach`, `test_agentic_queue_cli`, `test_agentic_reexec`,
  `test_agentic_drain_signal`, `test_agentic_adopt`, `test_agentic_config_reload`,
  `test_agentic_exit_paths`, `test_agentic_close_inflight`, `test_agentic_lease`,
  `test_skills`: all pass.
- **Manual** (isolated `WT_DATA_DIR`/`WT_CONFIG_DIR`): `python -m wt queue run --detach
  --log rel.log` from `/tmp/…` printed the absolute log path; the lease showed it with
  `detached: true`; the child's `/proc/N/cmdline` was `python -m wt queue run
  --poll-interval 2`; `python -m wt queue stop --yes` stopped it in 0.2 s.
- Full `uv run pytest` left to gating.

### Test record (2026-10-30, review round 2)

- Added to `tests/test_agentic_detach.py`: `test_nested_run_under_a_detached_runner_is_not_detached`,
  `test_loop_reexec_passes_the_runner_log_on`, `test_loop_reexec_without_detach_sets_no_runner_log`,
  `test_detach_log_oserror_is_a_clean_error`, `test_detach_popen_oserror_is_a_clean_error`,
  `test_detach_poll_reads_the_lease_file_before_probing_the_flock`,
  `test_stop_waits_for_a_missing_lease_file`, `test_stop_never_signals_a_stale_record`,
  `test_stop_refuses_when_the_lease_file_never_settles`, `test_run_launcher_gone_from_defaults`
  (AC5: no default, a config that still sets it loads). Extended: the log is pre-seeded and
  kept (`test_detach_waits_for_child_lease`), `stdout.mode == "a"` (`test_detach_popen_args`),
  `os.environ` has no `WT_RUNNER_LOG` after `Q.run` (`test_run_records_log_in_lease`).
  `test_queue_stop_dry_run_no_signal` now patches `Q.stop_runner` to fail (the `os.kill`
  patch could not reach its early-bound default); `test_queue_stop_json_schema` checks that a
  `--yes --json` refusal still prints `wt.queue.stop.v1` with `error` and exits 1.
  `test_agentic_detach`, `test_agentic_queue_cli`, `test_agentic_reexec`, `test_skills`,
  `test_agentic_lease`, `test_agentic_drain_signal`, `test_agentic_adopt`: 216 passed.
- **Sabotage:** log opened with `"w"`; flock probed before the file read; `stop_runner` on a
  single `lease_status`; `WT_RUNNER_LOG` read instead of popped; re-exec without passing the
  log on. Each makes at least one of the new tests fail; all restored.
- **Manual** (isolated `WT_DATA_DIR`/`WT_CONFIG_DIR`): `--detach --log /proc/x.log` printed
  `Error: cannot start the runner (…/wt, log /proc/x.log): [Errno 2] …` (exit 1, no
  traceback); `--detach` started, the lease showed `log` and `detached: true`; `wt queue stop
  --yes --json` stopped it (0.2 s, `error: null`); again gave `no runner`.
- Full `uv run pytest` left to gating.

### As shipped (2026-10-30)

- Review round 2: `run` pops `WT_RUNNER_LOG` from `os.environ` once it is recorded, so persona
  runs, gates and nested runs do not inherit it; the SPEC-0194 re-exec passes it on explicitly
  (from the lease's `log`). `detach_runner` reads the lease file before it probes the flock
  (the child writes the file right after taking the flock, so the probe cannot race the
  child's non-blocking acquire) and turns an `OSError` (log, `Popen`) into a `ValueError`.
  `stop_runner` acts only on a *settled* holder (`_settled_holder`: held, `pid` and `host`
  present, the same `(pid, host, started_at)` on two reads 0.05 s apart, up to 20 tries, else
  a refusal), so a missing or stale lease file is never signalled; it re-checks after a
  `--wait-guard` wait. `wt queue stop --yes --json` prints the JSON with `error` (null on
  success) and exits 1 on a refusal. `tests/conftest.py` also scrubs `WT_RUNNER_LOG`.
- Review round 1: the entry argv lives in `queue.py` and is shared with the SPEC-0194
  re-exec. `_started_as_module()` (`__main__.__spec__.name == "wt.__main__"`) makes both
  `self_argv()` (the re-exec: `python -m wt …`, else `python argv[0] …` as before) and
  `entry_argv()` (detach: `argv[0]` when executable, else `python -m wt …`) keep `-m wt`, since
  `python …/wt/__main__.py` cannot import its package. `detach_runner` stores the log path as
  an absolute path. `stop_runner` returns `no runner` when `kill` finds no process, turns a
  `PermissionError` into a refusal, and refuses a lease held by a one-shot `wt queue tick`
  (no `busy`, no SIGTERM handler; it ends by itself).

- `python -m wt` did not exist, so the Decision's fallback entry point would have failed:
  added `src/wt/__main__.py` (two lines). `cli_queue._entry_argv()` picks `sys.argv[0]`
  when it is an executable file, else `[sys.executable, "-m", "wt"]`; the child argv is that
  plus `sys.argv[1:]`, and `Q.detach_runner` strips `--detach`, `--log PATH` and `--log=PATH`
  itself (so the stripping is unit-tested). `WT_RUNNER_LEASE_FD` is dropped from the child env.
- `wt queue stop --by` is resolved like the other human commands (`_actor`) and only shown in
  the plan / JSON (`by`); nothing is journaled by the command. The JSON is
  `{schema: wt.queue.stop.v1, held, pid, host, guard, wait_guard, timeout_s, by, written,
  result}` (`result` = the `stop_runner` return, null on a dry run).
- `stop_runner` with `--wait-guard` returns `no runner` if the runner exits while it waits.
- Skill: besides step 2 and the five rows, the *Never block* list now names `wt queue run
  --detach` / `wt queue stop --yes` as quick commands (starting the loop is no longer a
  background-shell job), and the re-exec-drain guardrail uses the two commands. The close
  guard's definition is only in `runner_journal_silent`; the SPEC-0228 skill tests
  (`test_new_watchdog_rows_present`, `test_close_guard_in_rows`) were updated to check the
  new wording instead of "never SIGTERM" in every row.
- The idea clock-in/out was skipped: wt refuses org writes from a campaign agent
  (`WT_PERSONA_RUN=1`).

## Rollout / migration

The skill update ships with the code. A runner started the old way works with `wt queue stop`,
since only the lease is read. Its lease has no `log` and reports `detached: null`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
