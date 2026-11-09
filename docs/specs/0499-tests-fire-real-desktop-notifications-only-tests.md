---
id: SPEC-0499
title: Tests must not fire real desktop notifications
status: proposed
owner: user
created: 2026-11-09
updated: 2026-11-09
kind: feature
tags: [tests, agentic, escalations]
source_idea: IDEA-1070
---

## Context

Escalation notify defaults to `auto` (`DEFAULTS` in
`src/nightwright/agentic/escalations.py`). In that mode `notify` resolves
`shutil.which("notify-send")` and, when the binary is on `PATH`, runs it through
`escalations._run_notify`, which is `subprocess.run` with a 5-second timeout.
`apply_escalate` (`_announce`), auto-apply (`_announce_auto`), and `remind_due` all
call `notify`. A config that omits `agentic.escalations.notify` therefore launches
a real desktop notification whenever a test writes an escalation and this machine
has `notify-send`.

The notifier unit tests stub that call. `tests/test_agentic_escalations.py` has an
autouse `calls` fixture that replaces `ESC._run_notify` and `shutil.which`.
`tests/test_agentic_escalation_timeout.py` imports `calls`. A few other modules
stub `_run_notify` for the tests that need a recorded argv or a forced failure
(`tests/test_agentic_queue.py`, `tests/test_agentic_queue_autopark.py`,
`tests/test_agentic_queue_finish_race.py`, `tests/test_agentic_conductor_brief.py`,
`tests/test_cost_policy_e2e.py`). Everything else does not. Raising
`backend_unavailable` in `tests/test_runner_selfheal.py`
(`test_gate_holds_backend_unavailable_and_escalates_once` and
`test_do_persona_turns_cursor_startup_fault_into_interrupted`) writes `ESC-0001`
into a tmp store under the default and runs the real `notify-send`. Any new test
that escalates without remembering the stub does the same.

The production notifier is specified by
[SPEC-0232](./0232-escalation-records-wt-queue-escalate-and-notify.md) and stays
as it is. This spec only makes the pytest process hermetic.

## Goals / Non-goals

**Goals**

- Every test starts with `ESC._run_notify` stubbed, so the default `notify=auto`
  path records a successful notify outcome and does not spawn `notify-send`.
- A test that still reaches `notify-send` through `subprocess.run` or
  `subprocess.Popen` fails before the process starts.
- Tests that already replace `_run_notify` to record argv or inject a failure
  keep doing so.

**Non-goals**

- Changing `DEFAULTS`, the `notify-send` argv, the timeout, or the runner's
  desktop notifications.
- Stubbing `shutil.which` for the whole suite (that would rewrite `notified.via`
  on machines where `notify-send` is installed).
- Deleting the per-module `_run_notify` stubs that record argv or force errors.
- Intercepting `notify-send` launched through a `subprocess.run` / `Popen`
  function object that a test captured with `from subprocess import run` before
  the fixture ran. Production code looks up `subprocess.run` on the module at
  call time.

## Decision

Add one repo-wide autouse fixture in `tests/conftest.py` that stubs
`nightwright.agentic.escalations._run_notify` with a success result. The same
fixture replaces `subprocess.run` with a function and `subprocess.Popen` with a
subclass of the saved `Popen` (a function assigned to `subprocess.Popen` is
forbidden) so an argv whose program basename is `notify-send` raises
`AssertionError` before the real subprocess call. Lock both behaviors in with
`tests/test_no_desktop_notify.py`. No production code changes.

## Design

Add `_no_desktop_notify` to `tests/conftest.py`, next to the other autouse
hermetic fixtures (`_no_runner_unit`, `_no_ambient_herdr_env`). It is
function-scoped, `autouse=True`, and takes `monkeypatch`.

**Stub.** `monkeypatch.setattr` on `nightwright.agentic.escalations._run_notify`.
The replacement ignores its argv and returns an object with `returncode == 0`,
`stdout == ""`, and `stderr == ""` (the attributes `notify` reads). It does not
call `subprocess`.

`notify` still builds argv. Under the default `auto`, `shutil.which("notify-send")`
is unchanged, so a machine with the binary records `via == "notify-send"` and
`ok is True`, and a machine without it records `via == "none"` and
`error == "notify-send not found"` without calling the stub. `notify=none` and a
custom command list are unchanged except that the command list hits the stub
instead of a real process.

**Guard.** In the same fixture, capture the original `subprocess.run` and
`subprocess.Popen`, then `monkeypatch.setattr` both. The `run` replacement is a
function. The `Popen` replacement is a subclass of the saved `subprocess.Popen`,
not a function. Assigning a function to `subprocess.Popen` is forbidden:
`isinstance(proc, subprocess.Popen)` raises `TypeError` when `subprocess.Popen`
is not a type. `runner_jobs.service` does that check after spawn, and the
`except Exception` around it writes `result.json` `ok: false` (`the runner
could not start the job`) even when spawn returned a stand-in process.
`tests/test_runner_jobs.py::test_submit_service_wait_round_trip` is that path.

The subclass's `__init__` inspects the program, raises `AssertionError` before
`super().__init__` when the basename or first token is `notify-send`, and
otherwise calls `super().__init__` with the same arguments. Processes it starts
still satisfy `isinstance(proc, subprocess.Popen)` looked up on the subprocess
module during the test. The `run` function uses the same inspection rules:

- A sequence argv uses `os.path.basename` of element 0 (`/usr/bin/notify-send`
  and a PATH entry both count).
- A string argv uses the first whitespace-delimited token, so
  `shell=True` commands are covered too.

When that basename or token is `notify-send`, the replacement raises
`AssertionError` with `notify-send` and the argv in the message and does not
start a process (`run` does not call the saved `run`; `Popen.__init__` returns
before `super().__init__`). Every other command, including `git`, is forwarded
with the same arguments. `subprocess.call`, `check_call`, `check_output`, and
`run` all spawn through `Popen`, so the `Popen` subclass is the backstop; the
`run` function fails the production `_run_notify` path at the call site
`notify` uses.

**Override.** A test that later `monkeypatch.setattr`s `ESC._run_notify` still
wins for that test: pytest undoes `monkeypatch` last-in, first-out, and the
test's fixture or body runs after the autouse setup. The existing recorders and
failure injections stay correct, and they never call the real `_run_notify`, so
they do not trip the guard. A test that replaces `subprocess.run` itself for
the duration of the test likewise wins for that name; the `Popen` subclass
remains unless the test replaces `Popen` too.

**Guard tests.** New module `tests/test_no_desktop_notify.py`. It prepends a
directory to `PATH`, via `monkeypatch.setenv`, containing an executable named
`notify-send` (a script that writes a marker file and exits 0) so a regression
spawns that script instead of the session's real notifier. Every test that
treats an absent marker as proof first asserts `shutil.which("notify-send")` is
that prepended script. An absent marker proves nothing ran only when that script
is the binary `which` would have executed.

## Alternatives considered

- **Stub `_run_notify` at each new call site.** This is the current practice.
  Tests that forget, including `tests/test_runner_selfheal.py`, page the desktop.
- **Force `agentic.escalations.notify` to `none` from a shared cfg helper.**
  Most tests build their own cfg dict and never pass through one helper.
  `settings` then fills in `auto` from `DEFAULTS`.
- **Repo-wide `shutil.which("notify-send")` returning `None`.** `notify` would
  record `via: none` and skip `_run_notify`, which changes assertions that
  expect `via == "notify-send"` under `auto`, and a direct `subprocess.run` of
  `notify-send` would still succeed.
- **Stub only `_run_notify`, with no subprocess guard.** The known call path
  goes quiet, and any future inline `subprocess.run(["notify-send", ...])` or a
  test that restores the real `_run_notify` pages the desktop again with a
  green suite.

## Acceptance criteria

- [ ] With the suite's autouse fixture in place, `ESC.notify` under default
      `notify=auto`, when `PATH` starts with a fake `notify-send` that writes a
      marker file, returns `via == "notify-send"` and `ok is True`, and the
      marker file is absent.
- [ ] Applying an escalation into a tmp store under that same default (the
      `tests/test_runner_selfheal.py` shape, an open `ESC-0001`) leaves the
      fake `notify-send` marker absent.
- [ ] `subprocess.run` and `subprocess.Popen` whose program basename is
      `notify-send` (bare name or absolute path), and `subprocess.run` of a
      string whose first token is `notify-send`, raise `AssertionError`
      mentioning `notify-send` before the fake executable runs (marker absent).
- [ ] `subprocess.run` of an argv whose basename is something else still runs
      that program and returns its result.
- [ ] `subprocess.Popen([sys.executable, "-c", "print(1)"], stdout=subprocess.PIPE, text=True)`
      returns a process for which `isinstance(proc, subprocess.Popen)` is true
      and `communicate()` stdout is `1\n`.
- [ ] A test that replaces `ESC._run_notify` with a recorder still receives the
      argv `notify` would have run, and the fake `notify-send` marker stays
      absent.
- [ ] `tests/test_agentic_escalations.py` still records the `notify-send` argv
      and still covers a raising notifier and a non-zero return code, through
      its own `_run_notify` replacement.
- [ ] No production module changes. `escalations._run_notify` outside pytest is
      still `subprocess.run` of the configured argv.

## Test plan

- **Automated tests:** add `tests/test_no_desktop_notify.py`. The module
  prepends a directory to `PATH`, via `monkeypatch.setenv`, containing an
  executable named `notify-send` (a script that writes a marker file and exits
  0), as described in Design. Every test that treats an absent marker as proof
  first asserts `shutil.which("notify-send")` is that prepended script. An
  absent marker proves nothing ran only when that script is the binary `which`
  would have executed.
  - `test_notify_auto_does_not_spawn_notify_send` calls `ESC.notify` on a
    minimal record (`id`, `question`) and a cfg that leaves `notify` at the
    default. First asserts `shutil.which("notify-send")` is the prepended
    script. Then asserts `via == "notify-send"`, `ok is True`, no exception,
    marker absent.
  - `test_apply_escalate_in_tmp_store_does_not_spawn_notify_send` applies one
    escalation in a tmp data dir (open `ESC-0001`, default notify). First
    asserts `shutil.which("notify-send")` is the prepended script, then asserts
    the record exists and the marker is absent.
  - `test_subprocess_notify_send_fails_closed` first asserts
    `shutil.which("notify-send")` is the prepended script, then asserts
    `pytest.raises(AssertionError, match="notify-send")` for
    `subprocess.run([fake, ...])`, `subprocess.Popen([fake, ...])`, and
    `subprocess.run("notify-send t b", shell=True)`, and asserts the marker is
    absent in each case.
  - `test_other_subprocess_commands_still_run` runs
    `subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True)`
    and asserts return code 0 and stdout `1\n`.
  - `test_popen_of_other_commands_stays_a_popen` runs
    `subprocess.Popen([sys.executable, "-c", "print(1)"], stdout=subprocess.PIPE, text=True)`
    and asserts `isinstance(proc, subprocess.Popen)` is true and `communicate()`
    stdout is `1\n`.
  - `test_module_stub_still_observes_argv` replaces `ESC._run_notify` with a
    recorder that returns the success object and calls `ESC.notify`. First
    asserts `shutil.which("notify-send")` is the prepended script, then asserts
    the recorded argv's program basename is `notify-send` and the marker is
    absent.
- **Manual verification:** none. The marker file is the spawn check, including
  on a host with no session bus. A desktop session that runs the new module
  shows no nightwright toast; that observation is optional.
- **Regression guard:** while iterating, run
  `uv run pytest -q tests/test_no_desktop_notify.py tests/test_agentic_escalations.py tests/test_agentic_escalation_timeout.py tests/test_runner_selfheal.py tests/test_runner_jobs.py tests/test_agentic_queue.py tests/test_agentic_queue_autopark.py tests/test_agentic_queue_finish_race.py tests/test_agentic_conductor_brief.py tests/test_cost_policy_e2e.py`.
  The escalation module must still assert the critical `notify-send` argv and
  the failure paths. The self-heal module must still pass
  `test_gate_holds_backend_unavailable_and_escalates_once` and
  `test_do_persona_turns_cursor_startup_fault_into_interrupted`.
  `tests/test_runner_jobs.py` must still pass, including
  `test_submit_service_wait_round_trip`, which checks
  `isinstance(proc, subprocess.Popen)` after spawn. A root
  `tests/conftest.py` edit selects the full suite in
  [SPEC-0328](./0328-tiered-gates-affected-test-selection.md) (`conftest.py
  changed`). That run is the queue close gate
  ([SPEC-0378](./0378-affected-only-close-gate.md)). The implementer does not
  run `uv run pytest` on the whole tree
  ([SPEC-0379](./0379-time-based-sentinel-on-worker-slots.md)).

## Clock Log

No implementation session yet. Clock `IDEA-1070` with `nw idea clock-in` and
`nw idea clock-out` when implementation starts and stops.

## Rollout / migration

Test-only. Land the autouse fixture and `tests/test_no_desktop_notify.py`
together. There is no data migration and no change to stored escalation JSON.
Existing per-module stubs stay. After the fixture is in, a pytest run on a
desktop session does not pop `notify-send` for escalations the tests raise.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the new module and the regression modules above pass.
      The close gate owns the full-suite selection caused by `tests/conftest.py`.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date) via `python3 tools/spec_lint.py --write-ledger`.

## Open questions

None.
