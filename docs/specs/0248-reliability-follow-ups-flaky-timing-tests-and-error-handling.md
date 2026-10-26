---
id: SPEC-0248
title: "Reliability follow-ups from review: flaky timing tests and error-handling gaps"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, tests, reliability, followups]
source_idea: IDEA-130
depends_on: [SPEC-0209, SPEC-0220, SPEC-0222, SPEC-0224, SPEC-0226]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md). It
gathers the review follow-ups (`wtfu:` ideas, SPEC-0207) about flaky timing and error
handling that were filed on SPEC-0209, 0220, 0222, 0224 and 0226:

- `IDEA-130` (`wtfu:c735d4ada362`, SPEC-0224) and `IDEA-135` (`wtfu:5f3848cd4392`, SPEC-0224):
  in `tests/test_agentic_drain_signal.py`, `test_drain_writes_and_clears_lease_draining` and
  `test_ready_not_admitted_fires_during_drain` coordinate a real `Q.run()` with an observer
  thread that polls (`_poll`, 20 s; `GatedRunner`, 30 s). In `tests/test_agentic_lease.py`,
  `test_heartbeat_races_info_updates` runs for 2 s of wall time.
- `IDEA-145` (`wtfu:de8fbefed3f7`, SPEC-0220): in `tests/test_agentic_close_inflight.py`,
  `Env.tick` asserts that each tick takes under 12 s, with a 15 s safety timer, and
  `Env.spin` / `Env.settle` poll with sleeps and 60–120 s timeouts.
- `IDEA-160` (`wtfu:b99b5b552f79`, SPEC-0226) and `IDEA-166` (`wtfu:3cd23f30de5a`, SPEC-0226):
  in `tests/test_agentic_config_reload.py`, `test_poll_interval_reload_respects_cli_override`
  asserts real `time.monotonic` gaps between ticks (`>= 0.55`, `< 0.3`). The upper bounds
  fail on a loaded host.
- `IDEA-139` (`wtfu:f591472925a9`, SPEC-0209): in `tests/test_agentic_idea_campaign.py`,
  `test_concurrent_apply_add_idea_creates_one_campaign` catches a missing `add_lock` only if
  the two threads happen to interleave.
- `IDEA-190` (`wtfu:e7c5bb6c2ae4`, SPEC-0222): a non-numeric `agentic.events.park_decide_s`
  crashes `wt queue list` and the dashboard. `queue_payload` calls `EV.settings(cfg)`, and
  `park_view` compares `parked_for > settings["park_decide_s"]` with no coercion. Before
  SPEC-0222, a bad value there could break only the watchdog.

Not included: `IDEA-189` (the watchdog re-parsing parked ledgers each poll; a performance
item, not a reliability one) and `IDEA-191` (the SPEC-0222 rendered-age assertions; their
reviewer judged the 60 s windows robust). The `wtfu:` ideas are not edited by this spec. They
close through the normal follow-up flow when it ships.

The rule applied throughout: **a test never asserts an upper bound on elapsed wall-clock
time, and never needs a particular thread interleaving to detect a defect.** Each test
synchronises on an explicit event: a `threading.Event` set from a hook, a `Future`, or a
fake clock. A timeout remains only as a hang guard, and it fails with a message that names
what never happened.

## Goals / Non-goals

**Goals**
- The listed tests are deterministic: they pass or fail the same way on an idle laptop and
  on a loaded CI host.
- Malformed numeric `agentic.events` settings cannot crash the listing, the dashboard or the
  watchdog.

**Non-goals**
- The held re-gate's shell loop (`while [ -e hold ]; do sleep 0.05; done` in `_gate_cmd`).
  It is an external process that polls a file, and no assertion depends on its timing.
- Any production behaviour change other than the settings coercion.
- The other untested-branch `wtfu:` ideas on these specs (coverage gaps, not reliability).

## Decision

Rewrite the synchronisation in the five listed tests around explicit events, a fake clock
and lock-held assertions. Make `events.settings(cfg)` coerce every numeric key, falling back
to its default.

## Design

- **Drain tests** (`test_agentic_drain_signal.py`):
  - Replace `_poll` with a `_Signal` helper. It wraps `L.Lease.update_info` and
    `L.Lease.pop_info` with `monkeypatch`: after the real call, it sets
    `threading.Event`s for "draining written" and "draining cleared".
  - The SPEC-0224 backlog refresh is observed the same way: `queue._refresh_backlog` (which
    `queue._drain` calls) is wrapped to set "B stored ready" once B's stored
    `backlog.ready` is true after the call.
  - The observer waits on these events (`Event.wait(60)` as a hang guard, which fails with
    "never saw: …"). It reads state only after the event is set.
  - `GatedRunner.run` keeps its `Event.wait`. Its timeout is a hang guard, raised to 60 s.
- **Heartbeat race** (`test_agentic_lease.py::test_heartbeat_races_info_updates`): replace
  the 2 s loop with a fixed 500 `update_info`/`pop_info` pairs. A `threading.Barrier(3)`
  starts the beat thread, the read thread and the main loop together. Each of the two
  threads must complete at least one iteration before the main loop ends (checked with
  per-thread counters after `join`), so the race is exercised at any machine speed.
- **In-flight close suite** (`test_agentic_close_inflight.py`):
  - `Env.tick` drops the `< 12 s` assertion. The safety timer's callback sets
    `self.guard_fired` before releasing the holds. The tick then asserts `not
    self.guard_fired`, and that every hold present before the tick still exists after it.
    A tick that blocked on a held re-gate can only have returned because the guard fired,
    so the check is exact. The guard stays at 15 s.
  - `Env.settle` waits on the flight's futures with `concurrent.futures.wait(..., timeout=60)`
    instead of sleep-polling `done()`.
  - `Env.spin` is bounded by a tick count (`max_ticks=400`) instead of seconds. Between ticks
    it waits on `pool.wait_any(0.2)` (`StepPool`), not `time.sleep`.
- **Poll interval** (`test_agentic_config_reload.py`): the test installs a `FakeTime` in place
  of `queue.time` (`monkeypatch.setattr(Q, "time", fake)`). `fake.sleep(s)` advances
  `fake.monotonic()` by `s` and returns at once. `_spy_ticks` records `fake.monotonic()`.
  The assertions become exact within the fake clock's arithmetic: the gaps equal the
  intervals (0.6 after the reload, the old value before it, 0.01 with `--poll-interval`),
  compared with `pytest.approx`.
- **Add-lock coverage** (`test_agentic_idea_campaign.py`): add
  `test_apply_add_idea_holds_add_lock`, modelled on `test_apply_add_holds_add_lock`. Spies on
  `Q._open_idea_campaign` (the in-lock re-check) and on `S.create_campaign` assert that both
  run with the `add.lock` process lock at depth 1. The threaded test stays as a smoke test.
- **Settings coercion** (`src/wt/agentic/events.py`): `settings(cfg)` merges as today, then,
  for each key whose default in `DEFAULTS` is an int or float (including `park_decide_s`,
  added by SPEC-0222), replaces the value with the default when it is a bool, is not
  coercible with `float()`, or is negative. Otherwise it stores `float(v)`, or `int(v)` when
  the default is an int and the value is integral. Unknown keys pass through unchanged.

## Alternatives considered

- **Raise the wall-clock bounds** — it only moves the flake threshold, and slows the failure
  when a regression does block.
- **Mark the tests `flaky` and retry** — hides real races. These tests exist to catch races.
- **Validate `agentic.events` in `validate_runner_cfg` and refuse bad config** — the listing
  and the dashboard do not run that validation, and refusing to render a queue over a
  display threshold is worse than falling back.

## Acceptance criteria

- [ ] The two drain tests synchronise only on events set from wrapped lease and backlog
      calls. They contain no `time.sleep`, and no assertion on `time.monotonic()`.
- [ ] `test_heartbeat_races_info_updates` runs a fixed 500 iterations behind a
      `Barrier(3)`, and asserts that each thread iterated at least once. No wall-clock
      deadline remains.
- [ ] `Env.tick` asserts non-blocking through `guard_fired` and the surviving holds, with no
      elapsed-time assertion. Mutation check: making `prepare_close` re-gate on the main
      thread fails `test_agentic_close_inflight.py` through `guard_fired`.
- [ ] `Env.settle` waits on futures and `Env.spin` is tick-bounded. Neither calls
      `time.sleep`.
- [ ] `test_poll_interval_reload_respects_cli_override` uses `FakeTime` and asserts the gaps
      with `pytest.approx` against the configured intervals. It finishes without real
      sleeping (the test's own duration is under the old 0.6 s interval).
- [ ] `test_apply_add_idea_holds_add_lock` fails when the `with store.add_lock(cfg):` in
      `apply_add_idea` is removed (mutation check), and passes with it.
- [ ] With `agentic.events.park_decide_s: "soon"` (and separately `true`, `-5`),
      `wt queue list`, `wt queue list --json`, `wt queue watch --once` and
      `events.watchdog_checks` run without error, using the default threshold. `settings`
      keeps valid numbers, including numeric strings such as `"30"` → `30`.
- [ ] No test in the five touched test files asserts an upper bound on elapsed wall-clock
      time. Check with `grep -nE 'monotonic\(\) *- *[a-z0-9_]+ *<'`, which matches nothing.
- [ ] `uv run pytest` passes three times in a row, the last with `pytest -n 4` when
      `pytest-xdist` is available (otherwise noted). `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** the rewritten tests above, plus a new
  `tests/test_agentic_events_settings.py` for the coercion (the bad and good values for one
  int key and one float key, and `park_decide_s` through `queue_payload` and
  `dashboard_payload`).
- **Manual verification:** run the two mutation checks (main-thread re-gate; `add_lock`
  removed from `apply_add_idea`) locally, confirm each fails the named test, and revert.
  Run the touched files under CPU load (`stress -c $(nproc)` or a parallel `pytest -n`
  run). Note the results here.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Tests only, plus one defensive change to settings parsing. Nothing to migrate.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
