---
id: SPEC-0323
title: "Make test_adopt_orphan_group_waited_and_killed load-tolerant (timeout anchored to the run's started_at, not to test progress)"
status: proposed
owner: user
created: 2026-10-31
kind: feature
source_idea: IDEA-511
tags: [tests, flaky, agentic]
---

## Context

`tests/test_agentic_adopt.py::test_adopt_orphan_group_waited_and_killed` (line ~926) fails
three runs in a row at load average ~36 on an 8-core host, on `main` too (IDEA-511). It is the same
family as [SPEC-0312](./0312-flaky-under-load-tests-test-agentic-adopt-py-test.md)
(`test_fanout_detach_precondition`): real subprocesses plus assertions that assume the machine is fast.
[SPEC-0269](./0269-load-tolerant-test-waits-fix-known-flakes-and-add.md) set the policy:
`tests/waits.py` deadlines (`WT_TEST_WAIT_S`) instead of fixed timing before a positive assertion.

What the test does: start a real implementer run (`start_impl`), kill the pool and the wrapper
(`die`, `os.kill(wrapper_pid, 9)`) so the run is an `orphan_group` (the fake `claude` lives on), then
write `req["timeout_s"] = 3` into `request.json`, start a fresh `StepPool`, tick once (adoption),
assert the journal shows `orphan_group`, tick four more times with `time.sleep(0.2)` and assert the
group is still alive with no `run.end`, then `tick_until` a `run.end` and assert a synthesized
`exit.json` with `killed_by == "timeout"`.

Reading the product code gives the likely race. `ClaudeRunner._orphan_duties`
(`src/wt/agentic/runner.py:454`) computes the deadline as
`due = _epoch(request.json["started_at"]) + req.timeout_s` — an **absolute** instant anchored to
when the run *started* (`started_at` is written at launch, `runner.py:263`), not to when the test
rewrote `timeout_s`. The test's 3 s budget therefore has to cover everything between the run starting
and the "nothing happens" assertions: waiting for `child.json`/`child.pid`, `die`, the wrapper kill,
the `orphan_group` wait, adoption, and the four ticks. On an idle machine that is well under 3 s;
under load it is not, so the group is SIGTERMed during the "the group lives" phase and
`assert pid_alive(pid) and not run_ends(world)` fails (or the run is already closed before the
adoption journal check). The 0.2 s sleeps in that loop are a second, smaller fixed-timing hazard:
they encode "a few ticks happen inside the window" rather than a condition.

The product behavior (wait for an orphaned group, kill it at the timeout, synthesize `exit.json`,
record one `run.end`) is correct and stays untouched; the test must stop racing a product timeout.

## Goals / Non-goals

**Goals**
- `test_adopt_orphan_group_waited_and_killed` does not depend on elapsed wall time: the group cannot
  reach its timeout while the test is still setting up or asserting "nothing happens"; the timeout
  fires only when the test makes it due.
- The test asserts the same behavior as today (waited, not respawned, then killed at the timeout with
  a synthesized `exit.json` and exactly one `run.end`).
- The flake is reproduced before and shown fixed after under artificial load.

**Non-goals**
- No change to product code (`runner.py`, `runproc.py`, `queue.py`): `_orphan_duties` and the
  `started_at` anchor stay as they are.
- No sweep of other tests; only the one named test (and any local helper it needs).
- No retry/`flaky` plugin and no new global timeout knob.

## Decision

Keep the real-subprocess test. Take its deadline out of the race by splitting it into two controlled
steps. First, set `timeout_s` in `request.json` to a value far above anything the test can reach
(`int(waits.WAIT_S * 4)`, 240 s by default, tracking `WT_TEST_WAIT_S`) before adoption, so the group
cannot time out during setup or the "group lives" phase however slow the machine is. Second, once the
"nothing happens" assertions are done, make the timeout due deterministically by rewriting
`request.json`'s `started_at` to an instant older than `timeout_s` (`_orphan_duties` re-reads
`started_at` on every poll), then wait for the kill with `tick_until` (a deadline, `waits.WAIT_S`).
Replace the `time.sleep(0.2)` loop by a tick-count loop with no sleep dependence: ticks are
state-driven, so asserting liveness after N ticks is a condition check, not a timing guess.

## Design

All changes are in `tests/test_agentic_adopt.py`; nothing under `src/`.

1. **Long timeout before adoption.** Replace `req["timeout_s"] = 3` with
   `req["timeout_s"] = int(waits.WAIT_S * 4)`; keep the rewrite of `request.json` before the new pool's
   first tick, because adoption (`rebuild_request`, `runner.py:271`) reads `timeout_s` from that file.
   Keep the original `started_at` untouched here.
2. **"The group lives" phase.** Keep the first `Q.tick` and the `adopt_journal(...)[-1]["runs"][0]["status"]
   == "orphan_group"` assertion. Replace the `for _ in range(4): tick; time.sleep(0.2)` loop with
   `for _ in range(4): Q.tick(...)` (no sleep), then assert `pid_alive(pid)`, `not run_ends(world)`,
   and exactly one `run.start` — unchanged assertions, now independent of how long the loop takes.
   Because the deadline is 240 s past `started_at`, none of these can be preempted by the timeout.
3. **Make the timeout due on purpose.** After step 2, rewrite `request.json` with
   `started_at = (now - timeout_s - 1)` as an ISO timestamp in the same format the runner writes
   (`dt.datetime.now().astimezone().isoformat(timespec="seconds")` shifted by
   `dt.timedelta(seconds=timeout_s + 1)`; use the same helper/format as `_now_iso`,
   `runner.py`, so `_epoch` parses it). The next `_orphan_duties` poll sends TERM, waits
   `RP.GRACE_S` (1 s in this fixture), then KILL, then synthesizes `exit.json`.
4. **Wait on a condition.** `assert tick_until(world, new, lambda: run_ends(world))` — `tick_until`
   is already deadline-bound by `waits.WAIT_S` (SPEC-0312); drop the legacy `n=150` argument. Then
   the existing assertions: `exit.json` has `synthesized` and `killed_by == "timeout"`, the child pid
   is no longer alive, and `len(run_ends(world)) == 1`.
5. **No bare `time.sleep` remains** in the test body, and the test imports/uses `waits` as the other
   tests in the module do.

Root-cause confirmation comes before the fix is trusted: the implementer first reproduces the
failure under load (Test plan, manual step 1) and records which assertion fired. If it is not the
`pid_alive(pid) and not run_ends(world)` / adoption-journal assertion pre-empted by the 3 s timeout
anchored at `started_at`, the Decision and Design are amended with what was found (as an `As shipped`
note, per AGENTS.md) before the change ships. Any other timing hazard the reproduction exposes in
this one test is fixed the same way (a condition or a `waits` deadline, never a longer fixed sleep).

## Alternatives considered

- **Raise the fixed 3 s to e.g. 30 s** — still a wall-clock race (just a slower machine away from
  failing) and it makes the green path wait that long for the kill; a due-on-purpose `started_at`
  makes it both deterministic and fast.
- **Change `_orphan_duties` to anchor the deadline to adoption time** — product change to suit a test
  and it alters real timeout semantics (a run's budget counts from its start).
- **Mark the test `flaky` / rerun on failure** — hides real regressions in the adopt path.
- **Delete the test** — it is the only end-to-end proof that adoption waits for, kills and closes an
  orphaned `claude` group exactly once.

## Acceptance criteria

- [ ] The test writes `timeout_s == int(waits.WAIT_S * 4)` into `request.json` before adoption and no
      longer uses the literal `3`.
- [ ] The "group lives" phase has no `time.sleep`; it asserts `pid_alive(pid)`, no `run.end`, and a
      single `run.start` after four ticks.
- [ ] The timeout is made due by rewriting `request.json`'s `started_at` to older than `timeout_s`, and
      the test then asserts a `run.end` via `tick_until` (deadline `waits.WAIT_S`), a synthesized
      `exit.json` with `killed_by == "timeout"`, the child dead, and exactly one `run.end`.
- [ ] No file under `src/` changes; the diff is confined to `tests/test_agentic_adopt.py`
      (plus this spec and the regenerated ledger).
- [ ] The reproduction before the change and the 20-run result after it are recorded in the spec under
      `As shipped`; the test passes 20 consecutive runs under saturating CPU load.
- [ ] `uv run pytest` passes in full and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests** (`tests/test_agentic_adopt.py`):
  - `test_adopt_orphan_group_waited_and_killed` (revised): asserts the sequence in Design §1–§4 —
    adoption reports `orphan_group`; after four ticks the group is alive with one `run.start` and no
    `run.end`; once the timeout is made due, one `run.end`, synthesized `exit.json` with
    `killed_by == "timeout"`, child dead.
  - Other adopt tests (`test_adopt_orphan_group_finishing_on_its_own`, the rest of the module) and
    `tests/test_test_waits.py` (SPEC-0269 guard) must keep passing unchanged.
- **Manual verification**:
  1. Reproduce before the change: saturate CPU
     (`for i in $(seq $(( $(nproc) * 2 ))); do (while :; do :; done) & done`, or
     `stress-ng --cpu $(( $(nproc) * 2 ))`) and run
     `for i in $(seq 20); do uv run pytest tests/test_agentic_adopt.py::test_adopt_orphan_group_waited_and_killed -q || echo FAIL; done`;
     record the failure rate and the assertion that fired.
  2. Repeat after the change under the same load: 20 consecutive passes; kill the load generators.
  3. Idle-machine sanity: the revised test passes alone and its runtime is not materially longer than
     before (the kill happens as soon as the timeout is due, plus the 1 s grace).
  Record 1–3 in the spec's `As shipped`.
- **Regression guard**: `uv run pytest` (full suite) passes; `python3 tools/spec_lint.py` exits 0;
  `git diff --stat` shows only `tests/test_agentic_adopt.py` besides the spec and ledger.

## Rollout / migration

1. Reproduce under load (manual step 1) and note the result.
2. Apply the test changes (Design §1–§5).
3. Run the targeted test, the load loop (manual step 2), then the full suite.
4. Update the spec body with what shipped and regenerate the ledger.

No data migration; test-only change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
