---
id: SPEC-0312
title: "Make test_fanout_detach_precondition load-tolerant (no wall-clock races with real subprocesses)"
status: accepted
owner: user
created: 2026-10-30
updated: 2026-10-30
kind: feature
source_idea: IDEA-434
tags: [tests, flaky, agentic]
---

## Context

`tests/test_agentic_adopt.py::test_fanout_detach_precondition` (line ~825) passes alone and
fails intermittently in full-suite gates while other campaigns load the machine. It is the one
adopt test that *holds a real wrapper subprocess back on purpose*: `paused()` replaces the
`test_reviewer` wrapper's `Popen` with a Python stub that spins until a `go` file exists, then
`runpy`s `wt.agentic.runproc`. While the stub waits, `ClaudeRunner._await_claim`
(`src/wt/agentic/runner.py:389`) polls for `proc.json` and gives up after `spawn_timeout_s`
(default `30.0`, `runner.py:355`), after which the runner claims the run itself and returns a
`launch failed` result. `World.runner` is a bare `R.ClaudeRunner()` (`tests/test_agentic_adopt.py:44`),
so the test inherits that 30 s wall-clock deadline.

The test therefore races a fixed product timeout against everything the test does before it
writes `go`: bootstrapping the pool through `reviewing_flight` (several real runs, git
operations, ticks), waiting for the conformance reviewer, and the asserts. Under load that
sequence can exceed the deadline, so the paused reviewer's launch is reported as failed, its hook
never reaches `phase == "wait"`, and the test fails (or the detach precondition assertions
see a different state than the test intends to freeze).

A second, related weakness is the shared `tick_until` helper
(`tests/test_agentic_adopt.py:101`): it is bounded by an *iteration count* (`n=60`, so roughly 6 s
plus tick time) rather than by a time bound. It sits on the critical path of `reviewing_flight`
(`GATING` reached, then both reviewer tasks registered), so a slow machine can exhaust the
iterations even though nothing is wrong. [SPEC-0269](./0269-load-tolerant-test-waits-fix-known-flakes-and-add.md)-style
waits (`tests/waits.py`, `WT_TEST_WAIT_S`) exist for exactly this; `until` is already used by this
test, but `tick_until` does not follow it.

The pure-logic part of the same behavior is already covered deterministically, with no
subprocesses, by `test_fanout_detach_precondition_phases` (line ~799). What the subprocess test
adds is the end-to-end proof that a really-unclaimed wrapper keeps the fan-out attached until it
claims — that stays, but it must not depend on how fast the machine is.

## Goals / Non-goals

**Goals**
- `test_fanout_detach_precondition` no longer depends on wall-clock timing: its paused wrapper
  cannot be timed out by the runner while the test is still arranging the scenario.
- `tick_until` in `tests/test_agentic_adopt.py` is bounded by time (`waits.WAIT_S`), not by an
  iteration count, so it tolerates a slow machine the same way `until` does.
- The test asserts the same behavior it asserts today (attached while the wrapper is unclaimed;
  detached as one group of both run ids after the claim).
- The flake is demonstrated before and after the change under artificial load.

**Non-goals**
- No change to product code (`runner.py`, `runproc.py`, `queue.py`): `spawn_timeout_s` stays at
  its production default of 30 s and the detach logic is untouched.
- No sweep of every other test in the repo for flakiness; only the adopt test module's shared
  helper and the one named test.
- No new global timeout knob or retry/`flaky` plugin: retries would hide a real regression.

## Decision

Keep the real-subprocess test, but remove its dependence on elapsed time in two ways. First,
build the test's runner with a spawn timeout that is far above any bound the test can reach
(`spawn_timeout_s=waits.WAIT_S * 4`, i.e. 240 s by default, tracking `WT_TEST_WAIT_S`), so the paused wrapper
can only be released by the test's own `go` file, never by the runner's give-up path. Second,
make the paused stub announce that it is up (a `ready` marker file) and have the test wait on that
marker with `until` before asserting `phase == "spawning"`, so the "unclaimed wrapper" state is
observed, not assumed. Finally, convert `tick_until` from an iteration bound to a deadline bound
using `waits.WAIT_S`, keeping `n` accepted for the two existing callers
(`n=150`) as a documented floor-compatible argument.

## Design

All changes are in `tests/test_agentic_adopt.py`; nothing under `src/`.

1. **`tick_until`** (line 101): signature becomes
   `tick_until(w, pool, cond, n=None, steps=None, timeout=None)`. It loops
   `Q.tick(...)`, extends `steps`, returns `True` as soon as `cond()` holds, sleeps 0.1 s between
   ticks, and gives up only when `time.monotonic()` passes `end`, where
   `end = start + (timeout if timeout is not None else waits.WAIT_S)`. A passed `n` is
   interpreted as a *minimum* tick count the loop still performs before it may give up
   (it never shortens the time bound); the two existing `n=150` call sites (lines 347 and 901)
   keep working unchanged. After the deadline it makes one final `cond()` check, like `waits.until`.
   `until` and `waits` are imported from `waits` (`until` is already re-exported through
   `test_agentic_runproc`; the new code imports `WAIT_S` from `waits` directly).
2. **Paused wrapper stub** (the `code` string in the test): after the `while not exists(go)` loop is
   entered the stub first writes `ready` (`argv[1]`'s sibling path passed as a third argument,
   `world.tmp / "ready"`), so the test can see the stub is alive and unclaimed. Python startup of
   the stub is the only unbounded-by-test step, and it is waited with `until`.
3. **Spawn timeout**: the test sets `world.runner = R.ClaudeRunner(spawn_timeout_s=waits.WAIT_S * 4)`
   before calling `reviewing_flight` (the `world` fixture builds `ClaudeRunner()` with the default;
   the tests that need a different runner already replace `world.runner`). Only this test is affected.
4. **Order of assertions** in the test becomes:
   `until(h_conf.phase == "wait")` → `until(ready.exists())` →
   `h_test.phase == "spawning"` and `try_detach_all() is None` and `world.cid in pool.flights` →
   write `go` → `until(h_test.phase == "wait" and child.json)` → the grouped detach assertions
   (unchanged). The `h_test.phase == "spawning"` assertion is meaningful only after `ready`, since
   before it the stub may not even have started.

   **Release in a `finally`.** Because the spawn timeout is now 240 s, a failure before `go` is
   written would otherwise leave the worker thread parked in `_await_claim` (it starts its spawn
   clock on entering `ClaudeRunner.run`, `runner.py:364`), and the `world` teardown
   (`pool.detach_force()` only releases threads in phase `wait`; `pool.shutdown()` joins with
   `wait=True`, `queue.py:3190`) would block for up to `WAIT_S * 4` while the paused stub (started
   with `start_new_session`, no `proc.json`, so `RP.terminate_run` cannot kill it) spins forever.
   So everything after `reviewing_flight` runs inside `try: ... finally: go.write_text("")`: on
   any failure the stub is released and claims the run, the worker thread leaves `_await_claim`,
   `detach_force` releases it, and the stub never outlives the test. On the success path the
   `finally` write is an idempotent no-op (the file already exists).
5. **Guard**: a unit test (`test_tick_until_is_time_bounded`) in `tests/test_agentic_adopt.py`
   monkeypatches `Q.tick` to a no-op returning an object with `steps = []` (and counts its
   calls), then calls `tick_until(fake_w, None, lambda: False, n=1, timeout=0.3)` and asserts:
   it returns `False`; `time.monotonic()` elapsed is `>= 0.3` s; and the fake tick ran more than
   once (so `n=1` does not bound the loop). It also asserts an immediate `True` after exactly one
   tick when `cond` is already true, and that a passed `steps` list is extended. No assertion
   depends on how many ticks fit in a wall-clock window: only elapsed `>= timeout`, the return
   value, and ticks `> 1` with `n=1`.
   A second, tiny test asserts `tick_until` does not contain a bare `for _ in range(n)` bound, by
   AST inspection of the function, so the iteration bound cannot come back unnoticed.

Root-cause confirmation comes before the fix is trusted: the implementer first reproduces the
failure under load (see Test plan, manual step 1) and records which assertion fired. If the
reproduction shows a different failing assertion than the spawn-timeout / iteration-bound races above,
the Design and Decision are amended with what was found before the change is shipped (the spec is
past `draft`, so by superseding or an `As shipped` note, per AGENTS.md).

## Alternatives considered

- **Mark the test `@pytest.mark.flaky` / rerun on failure** — hides genuine regressions in the
  detach precondition and adds a dependency; the failure is a test-design race, not noise to retry.
- **Delete the subprocess test and rely on `test_fanout_detach_precondition_phases`** — loses the
  end-to-end proof that an unclaimed real wrapper (no `proc.json`) is what holds the fan-out
  attached; the phase test fakes the hooks.
- **Raise the product `spawn_timeout_s` default** — changes production behavior for a test problem
  and delays real launch-failure detection.
- **Add a test-only hook in `_await_claim`** — product code change to fit a test; passing the
  existing constructor parameter achieves the same result.

## Acceptance criteria

- [ ] `test_fanout_detach_precondition` constructs its runner with
      `spawn_timeout_s == waits.WAIT_S * 4` and never relies on the 30 s default.
- [ ] The paused wrapper stub writes a `ready` marker, and the test waits for it with `until`
      before asserting `h_test.phase == "spawning"` and that `try_detach_all()` returns `None`.
- [ ] After `go` is written the test still asserts a single grouped detach of both reviewer run
      ids and that both hooks are detached and `pool.flights` is empty (assertions unchanged in meaning).
- [ ] `tick_until` gives up only after `waits.WAIT_S` (or an explicit `timeout`) seconds, not after a
      fixed number of iterations; the `n=150` call sites still work unchanged.
- [ ] `test_tick_until_is_time_bounded` exists and passes; it asserts only the return value,
      elapsed `>= timeout`, and ticks `> 1` with `n=1` (no tick count per wall-clock window), and the
      AST guard fails if `tick_until` reintroduces a `range(n)` iteration bound.
- [ ] `go` is written in a `finally`, so a failing `test_fanout_detach_precondition` tears down
      within the normal fixture bounds and leaves no stub process (manual step 3).
- [ ] No file under `src/` is changed; the diff is confined to `tests/test_agentic_adopt.py`.
- [ ] `test_fanout_detach_precondition` passes 20 consecutive runs while the machine is saturated
      (manual step 2), and `uv run pytest` passes in full.
- [ ] `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests** (`tests/test_agentic_adopt.py`):
  - `test_fanout_detach_precondition` (revised): asserts the sequence in Design §4 — the fan-out stays
    attached while the wrapper is unclaimed, then detaches as one group after the claim.
  - `test_tick_until_is_time_bounded` (new): monkeypatches `Q.tick` to a counting no-op returning an
    object with `steps = []`; calls `tick_until(fake_w, None, lambda: False, n=1, timeout=0.3)` and
    asserts it returns `False`, elapsed `time.monotonic()` is `>= 0.3` s, and the fake tick ran more
    than once (so `n` does not bound the loop); asserts an immediate `True` (exactly one tick) when
    `cond` is already true, and that `steps` is extended when passed. No assertion depends on how
    many ticks fit in a wall-clock window.
  - Failure-path release (covered by Design §4's `finally`): verified in manual step 3.
  - `test_tick_until_has_no_iteration_bound` (new): AST check that `tick_until` has no
    `for ... in range(...)` loop.
  - Existing `test_fanout_detach_precondition_phases` and all other `test_agentic_adopt.py` tests that
    call `tick_until` (19 call sites) must keep passing unchanged.
  - `tests/test_test_waits.py` (SPEC-0269 guard) must still pass.
- **Manual verification**:
  1. Reproduce before the change: saturate CPU (for example `for i in $(seq $(nproc)); do (while :; do :; done) & done`, or
     `stress-ng --cpu $(( $(nproc) * 2 ))`) and run
     `for i in $(seq 20); do uv run pytest tests/test_agentic_adopt.py::test_fanout_detach_precondition -q || echo FAIL; done`
     Record the failure rate and the assertion that fired.
  2. Repeat after the change under the same load: 20 consecutive passes. Kill the load generators
     afterwards. Note both results in the spec's `As shipped` section.
  3. Failure-path teardown: temporarily insert `assert False` right after the `ready` wait in the
     test, run it, and confirm it fails in well under a minute (not ~240 s) and that no paused stub
     process remains (`pgrep -f runproc` shows none from the test); then remove the `assert False`.
- **Regression guard**: `uv run pytest` (full suite) passes; `python3 tools/spec_lint.py` exits 0;
  `git diff --stat` shows only `tests/test_agentic_adopt.py` (plus this spec and the regenerated ledger).

## Rollout / migration

1. Reproduce under load (manual step 1) and record the result.
2. Apply the changes in `tests/test_agentic_adopt.py` (Design §1–§5).
3. Run the targeted tests, then the load loop (manual step 2), then the full suite.
4. Update this spec's body with what shipped and regenerate the ledger.

No data migration; test-only change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
