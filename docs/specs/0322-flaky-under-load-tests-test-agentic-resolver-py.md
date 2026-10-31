---
id: SPEC-0322
title: "Make test_interrupted_resolver_not_counted load-tolerant (stop only once the resolver run is in flight)"
status: accepted
owner: user
created: 2026-10-31
updated: 2026-10-31
kind: feature
source_idea: IDEA-512
tags: [tests, flaky, agentic]
---

## Context

`tests/test_agentic_resolver.py::test_interrupted_resolver_not_counted` (line ~482) fails on
`main` when the machine is heavily loaded (load average ~34 on 8 cores), with and without
SPEC-0320: `assert [] == [True]` at the `run.end` assertion (line ~498). The ledger holds no
`run.end` for `conflict_resolver` at all.

The test races two fixed delays. It starts `threading.Timer(0.15, stop.set)`, then calls
`w.tick(stop=stop)`; the scripted `FakeRunner` entry is `resolver(sleep_s=0.4)`, which sleeps 0.4 s
before it runs `pre_shell` / edits / `shell` (`src/wt/agentic/runner.py:569`). The test assumes the
resolver run starts within 0.15 s of the timer and is still sleeping when the timer fires. Under load
the tick's own startup (reading state, leases, worktree git calls) can take longer than 0.15 s, so
`stop` is already set when the tick reaches the step loop. The queue then does not dispatch the
resolver (`ctx.stop.is_set()` checks in `tick`, `src/wt/agentic/queue.py:3908-3922`), so no run
starts and no `run.end` with `interrupted=True` is written. The mirror hazard also exists: a very slow
resolver thread could finish its 0.4 s sleep and its git work before `stop` is set, giving
`interrupted=False`.

[SPEC-0269](./0269-load-tolerant-test-waits-fix-known-flakes-and-add.md) forbids this shape: a fixed
delay before a positive assertion. It provides `tests/waits.py` (`until`, `join`, `WAIT_S`, overridable
via `WT_TEST_WAIT_S`). Sibling flakes were fixed the same way by
[SPEC-0312](./0312-flaky-under-load-tests-test-agentic-adopt-py-test.md) (IDEA-434) and are tracked
for `test_agentic_adopt` as IDEA-511.

## Goals / Non-goals

**Goals**
- `test_interrupted_resolver_not_counted` no longer depends on wall-clock timing: `stop` is set only
  after the resolver run has demonstrably started, and the run is guaranteed to still be in flight
  when `stop` is set.
- The test asserts exactly what it asserts today (not counted as an attempt, branch restored,
  one `run.end` with `interrupted=True`, then the retry merges with two resolver runs).
- The flake is demonstrated before and after the change under artificial load.

**Non-goals**
- No change to product code (`runner.py`, `queue.py`); the stop handling under test is untouched.
- No sweep of other tests for flakiness; only this one test (the `threading.Timer` uses elsewhere in
  `tests/` are out of scope).
- No retry / `flaky` plugin, no global timeout knob.

## Decision

Replace the fixed 0.15 s timer and the fixed 0.4 s resolver sleep with two explicit synchronisation
points, both bounded by `waits.WAIT_S`. The test uses a small test-local `FakeRunner` subclass that,
on its first `conflict_resolver` run, announces it has started (`started` event) and then blocks until
the test's `stop` event is set, before delegating to the normal scripted entry. A watcher thread waits
(with `waits.until`) for `started`, then sets `stop`. The run is therefore always in flight when
`stop` is set, regardless of machine speed, and the queue's `res.interrupted = ctx.stop.is_set()` is
deterministically `True`. The second (retry) resolver run is not held.

## Design

All changes are in `tests/test_agentic_resolver.py`; nothing under `src/`.

1. **`_HoldingRunner(FakeRunner)`** (test-local, next to `World`): constructed with the same script
   path as `World.runner`, plus `stop: threading.Event`. It carries `started = threading.Event()` and
   a `_held = False` flag. `run(req, *, hooks=None)`: if `req.persona == "conflict_resolver"` and not
   `_held`, set `_held = True`, set `started`, then `waits.until(stop.is_set)`; if that returns
   `False` it raises `AssertionError` (the watcher never ran) rather than proceeding. Then
   `return super().run(req, hooks=hooks)`. Every other persona and every later resolver run goes
   straight through. `detachable` is inherited (`False`). It shares the script's file-backed cursor
   (`.fake-cursor`), so entry order is unchanged.
2. **The test** becomes:
   - `w = basic(tmp_path, monkeypatch, resolver(), resolver())` (the `sleep_s=0.4` is dropped; the
     first entry no longer needs it because the runner holds).
   - `w.open_and_conflict()`; `pre = w.dispatched()["closing"]["resolve"]["pre_head"]` (unchanged).
   - `stop = threading.Event()`; `w.runner = _HoldingRunner(w.script, stop)`; the watcher thread
     `t = threading.Thread(target=lambda: waits.until(lambda: w.runner.started.is_set() or stop.is_set()) and stop.set())`
     is started (`t.start()`).
   - `try: w.tick(stop=stop) finally: stop.set(); waits.join(t)`. The watcher also exits as soon as
     `stop` is set, so the `finally`'s `stop.set()` ends both the held runner and the watcher at
     once, and `waits.join(t)` returns immediately on a failing tick. A failure (for example a tick
     that raises before the resolver run starts) therefore never waits out `WAIT_S`.
   - `assert w.runner.started.is_set()` (the run really started), then all existing assertions
     (`CLOSING` and `pre_head` unchanged, no `resolve_attempts`, no `rebase_resolved`,
     `_assert_restored`, `[e["interrupted"] ... "conflict_resolver"] == [True]`).
   - `w.drive(...)` to `MERGED` and `len(w.run_ids()) == 2` are unchanged; the retry uses the
     un-held second scripted resolver.
3. **Imports**: `import waits` (tests import helpers as top-level modules, as `test_agentic_adopt.py`
   does) and `from wt.agentic.runner import FakeRunner` (already imported). `threading.Timer` is
   no longer used in this test.
4. **Guard**: `test_interrupted_resolver_test_has_no_fixed_timer` parses `tests/test_agentic_resolver.py`
   with `ast` and asserts the function `test_interrupted_resolver_not_counted` contains no call to
   `threading.Timer` / `Timer` and no `sleep_s` constant, so the fixed-delay race cannot return
   unnoticed.

Root-cause confirmation comes first: the implementer reproduces the failure under load (Test plan,
manual step 1) and records which assertion fired. If it is not the `run.end` assertion described in
Context, the Context and Decision are amended with what was found (an `As shipped` note, since the
spec is past `draft`) before the fix is trusted.

## Alternatives considered

- **Raise the timer delay / sleep (for example 1 s / 3 s)** — still a fixed-delay race, only rarer;
  SPEC-0269 forbids it.
- **Set `stop` from inside the runner (no watcher thread)** — also deterministic, but it bakes the
  stop into the runner and no longer models a stop arriving from another thread while a run is in
  flight, which is what a signal handler does in production.
- **Use `sleep_s` as the hold and only fix the start race** — the run could still finish its sleep
  before `stop` is set under extreme load; holding on the `stop` event removes that side as well.
- **`@pytest.mark.flaky` / reruns** — hides genuine regressions in the interrupted-resolver
  accounting.
- **A product-code hook ("run started" callback)** — product change to fit a test; a runner
  subclass in the test achieves the same.

## Acceptance criteria

- [ ] `test_interrupted_resolver_not_counted` no longer uses `threading.Timer` or a `sleep_s`
      entry; `stop` is set only after `_HoldingRunner.started` is set (observed via `waits.until`).
- [ ] The first `conflict_resolver` run blocks inside the runner until `stop` is set, so the
      `run.end` for it has `interrupted=True` whatever the machine speed; the retry resolver run is
      not held and the campaign still reaches `MERGED` with two resolver runs.
- [ ] A failing tick cannot hang the test for `WAIT_S`: the watcher waits on `started or stop`, the
      `finally` sets `stop` and joins the watcher with `waits.join`, so with a failure raised inside
      the `try` before `w.tick` the test fails within about a second (manual step 3).
- [ ] All assertions the test made before are still made (not counted as an attempt, no
      `rebase_resolved`, `_assert_restored`, interrupted `run.end`, final `MERGED`, two run ids).
- [ ] `test_interrupted_resolver_test_has_no_fixed_timer` exists and passes.
- [ ] No file under `src/` is changed; the diff is confined to `tests/test_agentic_resolver.py`
      (plus this spec and the regenerated ledger).
- [ ] `test_interrupted_resolver_not_counted` passes 20 consecutive runs while the machine is
      saturated (manual step 2), and `uv run pytest` passes in full.
- [ ] `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests** (`tests/test_agentic_resolver.py`):
  - `test_interrupted_resolver_not_counted` (revised): asserts the sequence in Design §2.
  - `test_interrupted_resolver_test_has_no_fixed_timer` (new): AST guard from Design §4.
  - All other tests in the module (which use `World.runner = FakeRunner`) and
    `tests/test_test_waits.py` (SPEC-0269 guard) must keep passing unchanged.
- **Manual verification**:
  1. Reproduce before the change: saturate the CPU (for example
     `for i in $(seq $(( $(nproc) * 2 ))); do (while :; do :; done) & done`, or
     `stress-ng --cpu $(( $(nproc) * 2 ))`) and run
     `for i in $(seq 20); do uv run pytest tests/test_agentic_resolver.py::test_interrupted_resolver_not_counted -q || echo FAIL; done`.
     Record the failure rate and the assertion that fired.
  2. Repeat after the change under the same load: 20 consecutive passes. Kill the load generators
     afterwards. Note both results in the spec's `As shipped` section.
  3. Failure-path release: temporarily make the tick fail before the resolver starts, by inserting
     `raise RuntimeError("injected")` as the first statement inside the `try:`, before
     `w.tick(stop=stop)`, after the watcher thread has been started. Run the test with
     `WT_TEST_WAIT_S` left at its default and confirm it fails with `RuntimeError: injected` in well
     under `WAIT_S` (not with `thread still running`). Then remove the line.
- **Regression guard**: `uv run pytest` (full suite) passes; `python3 tools/spec_lint.py` exits 0;
  `git diff --stat` shows only `tests/test_agentic_resolver.py`, this spec and the regenerated ledger.

## Rollout / migration

1. Reproduce under load (manual step 1) and record the result.
2. Apply the change in `tests/test_agentic_resolver.py` (Design §1-§4).
3. Run the targeted tests, then the load loop (manual step 2), then the full suite.
4. Update this spec with what shipped and regenerate the ledger.

No data migration; test-only change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
