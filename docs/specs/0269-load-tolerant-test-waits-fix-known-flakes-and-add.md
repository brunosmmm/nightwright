---
id: SPEC-0269
title: "Load-tolerant test waits: fix known flakes and add a shared wait helper"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-317
tags: [tests, flaky, gates]
---

## Context

IDEA-317 collects tests that failed once and then passed on a rerun. Each failure happened
while 6 campaigns ran `pytest` at the same time on 8 cores. A flake in the gate makes a close
re-gate fail for no real reason: the campaign goes TRIAGING or PARKED `gates_failed`, and a
fix loop gets burned on it. MERGE-20261027-0d7d parked `gates_failed` at 02:17 on
2026-10-27 because of exactly this, and the conductor had to resume it by hand.

These are the known flakes, each checked against the code on 2026-10-27:

1. **`tests/test_agentic_runproc.py::test_vanished_when_group_gone`**, failure
   `res.result_text == ''`. This is an ordering race, not a short timeout. The test
   SIGKILLs the wrapper first and the child's process group second. If the runner thread's
   `ClaudeRunner._wait` poll (`src/wt/agentic/runner.py`) lands between the two kills,
   `RP.run_status` returns `orphan_group`, `seen_orphan` is set, and the vanish takes the
   `_orphan_duties` path. That path synthesizes `exit.json`, so `collect` reads a finished run
   with no "vanished" text. The product behaviour is correct, because an orphan group that
   ended *is* a finished run. The test is wrong to assume that no poll lands between two
   syscalls. Load widens that window.
2. **`tests/test_agentic_adopt.py::test_adopt_review_group_applied_once`**. Already fixed
   (SPEC-0235 campaign commit 356d123): it now counts only adopted run ids.
3. **Fixed `pilot.pause(x)` followed by a *positive* assertion in TUI tests.** One of these is
   "a TUI test failed once then passed", from the SPEC-0237 ledger fix at about 22:00 on
   2026-10-25. Under load, Textual's timer may not fire within `x`. The sites:
   - `tests/test_tui_mailbox_tab.py::test_timer_pauses_when_the_tab_is_left`: `pause(0.3)`,
     then `assert calls`.
   - `tests/test_tui_events_tab.py::test_timers_pause_when_the_tab_is_left`: `pause(0.2)`,
     then `assert len(calls) > n`.
   - `tests/test_tui_queue_tab.py::test_periodic_reload_runs_without_a_write`: `pause(0.5)`,
     then `assert len(calls) >= 3`.
   - `tests/test_tui_campaign_screen.py::test_a_ledger_append_shows_within_a_poll_and_never_syncs`:
     `pause(0.5)` + `settle`, then a count assertion.
   - `tests/test_tui_mailbox_tab.py::test_the_tab_never_touches_followers`: after `ask(...)`,
     `pause(0.3)`, then `settle` and the count assertion.

   A fixed pause followed by a *negative* assertion (`assert not calls`, `len(calls) == n`)
   is load-safe, because load only makes the absence more likely. Those stay.
4. **A numeric `.join(<n>)` followed by a read of what the thread produced.** The tree has 15
   numeric joins. These 12 are followed by a read of thread-produced results or state, and
   they change:
   - `test_agentic_runproc.py`: 9 sites (lines 98, 136, 154, 172, 187, 203, 217, 239, 248),
     each followed by `out["res"]`;
   - `test_agentic_exit_paths.py:69`, where `t.join(5)` is followed by `return n, …`;
   - `test_agentic_store.py:196`, where `t.join(5)` is followed by `assert order == [...]`;
   - `test_agentic_nonblocking.py:261`, where `side.join(5)` is followed by assertions on
     `samples`, which the side thread collected.

   When the thread overruns the bound under load, the test fails with a `KeyError` or a
   misleading assertion instead of a timeout message. Three sites stay unchanged:
   `test_agentic_drain_signal.py:137` and `:268`, and `test_agentic_lease.py:128`. Each is a
   60 s join that is already followed by `assert not <t>.is_alive()`, which is the explicit
   form of the same check.

   `runproc.py:136-137` also asserts the wall time, `time.monotonic() - t0 < 0.5`: a detach
   must be prompt. That bound is a load flake of the same class. It is loosened to `< 5.0`,
   which is still far below the 60 s fake run, so a non-prompt detach still fails.
5. **`until(cond, timeout=10.0)`** in `test_agentic_runproc.py` is imported by
   `test_agentic_adopt.py`, `test_agentic_index_lock.py` and `test_agentic_exit_paths.py`.
   Its 10 s default is the bound for subprocess spawns, which are the slowest thing under
   load. The SPEC-0225 adopt-test flake (2026-10-25 23:50) fits this pattern:
   `test_agentic_adopt.py` uses no joins, only this `until` and the iteration-bounded
   `tick_until`.
6. **A private `async def until(app, pilot, cond, what=…, timeout=5.0)`** in
   `tests/test_tui_events_tab.py:59`, with 35 call sites. It polls `pilot.pause(0.05)`, calls
   `settle(app, pilot, 1)` on success, and raises a message that names `what`. Its fixed 5 s
   bound is the same kind of load flake.

SPEC-0248 already covered the agentic drain and config-reload timing tests. They are out of
scope here.

## Goals / Non-goals

**Goals**
- Make the six known flake classes above deterministic or load-tolerant.
- Add one shared, load-tolerant wait module for tests, with a default bound that is generous
  and can be set from the environment.
- Add a cheap guard that stops new fixed-short waits of the same shape from coming back.

**Non-goals**
- Retries or `pytest-rerunfailures`. A flake must be fixed, not hidden.
- Running the suite under artificial load in CI, or adding `pytest-repeat` / `stress-ng`
  dependencies.
- Speeding up the suite.

## Decision

1. A new module `tests/waits.py` (importable as `from waits import …`; tests already import
   sibling modules, as in `from test_agentic_runproc import until`). It holds:
   - `WAIT_S = float(os.environ.get("WT_TEST_WAIT_S", "60"))`, the default bound.
   - `until(cond, timeout=None, interval=0.05) -> bool`. It polls until `cond()` is true or
     `timeout or WAIT_S` passes, and returns at once on success, so a generous bound costs
     nothing on the green path.
   - `join(thread, timeout=None)`. It joins, then raises `AssertionError("thread still running
     after Ns")` if the thread is still alive.
   - `async pilot_until(pilot, cond, timeout=None, step=0.05) -> bool`, the same loop using
     `await pilot.pause(step)`.
   - Thin wrappers with another name are allowed. `test_tui_events_tab.py`'s `until` is
     renamed `ui_until(app, pilot, cond, what=…, timeout=None)`, with its 35 call sites. It
     keeps its signature and its `what` message, delegates the wait to
     `waits.pilot_until`, calls `settle(app, pilot, 1)` on success, and otherwise raises
     `AssertionError(f"timed out waiting for {what}")`. Its 5 s default goes away; the bound
     is `WAIT_S`.
2. `test_agentic_runproc.until` becomes a re-export of `waits.until`, so the three importers
   keep working unchanged, and its default changes from 10 s to `WAIT_S`.
3. `test_vanished_when_group_gone` is made deterministic. It SIGSTOPs the wrapper, SIGKILLs
   the child group, waits with `waits.until(lambda: not RP.group_alive(child))`, and only
   then SIGKILLs the wrapper. A stopped wrapper is alive (`wrapper_alive` treats state `T`
   as alive), so the runner sees `running` the whole time and then `vanished`, never
   `orphan_group`. The test keeps asserting "vanished". The stopped wrapper cannot reap the
   killed child, so the group stays a zombie. `RP.group_alive` already returns False for an
   all-zombie group (`_group_all_zombies`), so the wait ends. Do not "fix" it by reaping.
4. The 12 join sites listed in Context item 4 become `waits.join(t)`. The three joins that
   are already followed by an `is_alive()` assertion stay. Every fixed pause listed in Context item 3 that is followed by a
   positive assertion becomes `assert await pilot_until(pilot, <the asserted condition>)`.
5. The guard is a test, `tests/test_test_waits.py`, with two checks:
   - No test module other than `tests/waits.py` defines a top-level `until`, `_until`,
     `wait_for` or `_wait_for` function. All of them use the shared helper.
   - Every numeric join (`\.join\(\s*\d+(\.\d+)?\s*\)`, found by a regex scan of
     `tests/test_*.py`) must be followed on its next non-blank line by an assertion that
     contains `is_alive()`. So `side.join(60)` followed by `assert not side.is_alive()` is
     allowed, and any other numeric join, including `t.join(10)` followed by
     `out["res"]`, fails. String joins (`"".join(...)`) do not match the pattern.

   This is the cheap guard. A marker or a conftest fixture was considered and rejected; see
   Alternatives.

## Design

- **`tests/waits.py`** is new, about 40 lines, standard library only, with `import asyncio,
  os, time` at the top.
- **`tests/test_agentic_runproc.py`**: delete the local `until` and add `from waits import
  until, join` (re-exported). In `test_vanished_when_group_gone`, replace the two kills with
  `os.kill(wrapper, SIGSTOP)`, `os.killpg(pgid, SIGKILL)`, `assert until(lambda: not
  RP.group_alive(child))` and `os.kill(wrapper, SIGKILL)`. Replace its 9 numeric joins with
  `join(t)`, and loosen the detach wall-time bound at line 137 from 0.5 s to 5.0 s.
- **Other thread-driven tests**: replace the joins at `test_agentic_exit_paths.py:69`,
  `test_agentic_store.py:196` and `test_agentic_nonblocking.py:261` with `join(t)`. The
  joins at `test_agentic_drain_signal.py:137/268` and `test_agentic_lease.py:128` stay.
- **`tests/test_tui_events_tab.py`**: the `until` → `ui_until` wrapper and its 35 call sites
  (Decision 1).
- **TUI tests** (the five named in Context item 3): `from waits import pilot_until`, and
  replace `await pilot.pause(x); assert P` with `assert await pilot_until(pilot, lambda: P)`.
  The `settle` helpers stay as they are; they already wait for workers with a bound of 10 s
  per round, which is fine.
- **`tests/test_test_waits.py`** holds the two guard checks from Decision 5, done with
  `ast` for the function definitions and a regex for the joins, plus a unit test of
  `waits.until` and `waits.join`: a thread that sleeps 0.1 s joins; a stuck thread with
  `timeout=0.2` raises `AssertionError`.
- **Docs.** One line in `docs/README.md`'s testing section, if it has one, or else in
  `AGENTS.md` Tooling: "wait in tests with `tests/waits.py` (`WT_TEST_WAIT_S`), never with a
  fixed sleep before a positive assertion".

## Alternatives considered

- **`pytest-rerunfailures` / retry markers.** Rejected. They hide real races, and the IDEA-317
  races include a real ordering assumption (item 1).
- **A `@pytest.mark.timing` marker that skips under load.** Rejected. It hides the same tests
  that the gate needs.
- **A conftest fixture (`wait_until`) instead of a module.** Rejected. Helpers that test
  modules import directly are the repo's existing convention (`from test_agentic_runproc
  import until`), and a fixture cannot be used inside helper functions that sit outside a
  test.
- **Raising every fixed sleep to a large value.** Rejected. That slows the green path, while
  polling with a generous bound costs only the real wait.

## Acceptance criteria

- [ ] `tests/waits.py` exists with `WAIT_S` (env `WT_TEST_WAIT_S`, default 60), `until`,
      `join` and `pilot_until`. `test_agentic_runproc.until` is the same object as
      `waits.until`.
- [ ] `test_vanished_when_group_gone` uses the SIGSTOP → killpg → wait → SIGKILL order and
      passes 20/20 runs while 4 copies of `tests/test_agentic_runproc.py` run in parallel (the
      manual load check below).
- [ ] The 12 join sites in Context item 4 use `waits.join`, and the runproc detach bound is
      5.0 s. None of the five TUI sites listed makes a positive assertion after a fixed
      `pilot.pause`. `test_tui_events_tab.py` uses `ui_until` on top of `waits.pilot_until`,
      and has no private `until`.
- [ ] `tests/test_test_waits.py` passes on the tree, and fails when a private `def until(` or
      a `t.join(10)` + `out["res"]` pair is added to any test module.
- [ ] The full `uv run pytest` passes.

## Test plan

- **Automated tests:**
  - `tests/test_test_waits.py::test_no_private_wait_helpers`: an `ast` scan of
    `tests/test_*.py`.
  - `tests/test_test_waits.py::test_no_bare_join_before_result_read`: the regex scan.
  - `tests/test_test_waits.py::test_until_and_join_bounds`: `until(lambda: True)` returns at
    once. `until(lambda: False, timeout=0.2)` returns False in less than 1 s. `join` on a
    thread blocked on an `Event` with `timeout=0.2` raises `AssertionError`.
  - The changed tests themselves (`test_vanished_when_group_gone` and the five TUI tests)
    pass.
- **Sabotage check:**
  - Put the kill order in `test_vanished_when_group_gone` back to wrapper → group, and insert
    `time.sleep(0.5)` between the two kills. The test must fail, showing the race was real.
    Restore the new order with the same sleep: the test must pass.
  - Add `def until(c): ...` to any test module. `test_no_private_wait_helpers` must fail.
  - Add `t.join(10)` followed by `res = out["res"]` to any test module.
    `test_no_bare_join_before_result_read` must fail. Add `side.join(60)` followed by
    `assert not side.is_alive()`: the guard must still pass.
- **Manual verification (load):** `for i in 1 2 3 4; do uv run pytest -q -p no:randomly
  tests/test_agentic_runproc.py tests/test_tui_mailbox_tab.py tests/test_tui_events_tab.py &
  done; wait`, repeated 5 times. Zero failures. Record the counts in As shipped.
- **Regression guard:** the full `uv run pytest`. Record the wall time as data, not as a
  pass/fail criterion: the median of 3 runs before and 3 runs after, on the same idle
  machine, in As shipped.

## Rollout / migration

None. Tests only.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
