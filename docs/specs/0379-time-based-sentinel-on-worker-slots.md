---
id: SPEC-0379
title: "Clock-triggered full-suite sentinel on main, placed on worker-box gate slots"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, sentinel, workers]
depends_on: [SPEC-0359, SPEC-0374, SPEC-0378]
---

## Context

[SPEC-0378](./0378-affected-only-close-gate.md) makes a campaign close run only the affected
tests. After that, the full suite has exactly one place to run: the SPEC-0359 sentinel, which runs
the project's close gates at the base HEAD.

The SPEC-0359 sentinel is due only after reused merges, on a count or age threshold. Merges whose
close gate ran the affected selection without reuse never make it due. With affected-only closes,
main could go days without a full run. Interactions between modules that the static selection
cannot see would then go unnoticed.

The sentinel also ran only in the local gate pool. A 28-minute full suite on the host competes
with the coordinator and with campaigns' local gates. SPEC-0374 already prefers the worker box for
campaign gates.

## Goals / Non-goals

**Goals**
- The full suite runs on main periodically, on a clock: only when main moved since the last green
  run, and not more often than every N hours.
- N is adjustable, can be disabled, and has a reasoned default.
- The clock run reuses the existing sentinel machinery: worktree, flock, `last.json` retry and
  backoff, red handling (`force_full_close`, now meaning reuse off per SPEC-0378, plus the
  escalation).
- It is low priority. It starts only when no campaign waits for a gate slot (the existing
  `_sentinel_step` rule). It takes a worker-box gate slot whenever a worker is placeable, and it
  never overflows to the local pool while one is (SPEC-0374).

**Non-goals**
- Bisecting a red run.
- A per-project interval. The key is global for now; a per-project key can come later if the
  evidence asks for one.
- A local fallback after a remote transport failure.

## Decision

- **Config.** `agentic.sentinel.interval_hours` in the nw config.
  - Absent, or `true`: **6 hours**.
  - `0`, `false`, `null` or a negative number: off.
  - Any other non-number: a `ValueError`, which the runner reports as a sentinel failure for that
    tick.
- **Why 6 hours by default.** It gives a few full runs per working day: at most 4 per day, each
  about 28 minutes of one worker gate slot, and only when main actually moved. That keeps the
  window in which an undetected break can spread to a few hours, close to the old per-merge
  cadence of the busy hours. It also stays far from the cost of a full suite per close. Tune it
  from evidence: the sentinel's red rate and the gate-slot queue wait.
- **The clock.** `SN.time_due(cfg, project, base_sha)` is true when all of these hold:
  - the interval is on;
  - the project's sentinel is enabled;
  - `base_sha` is not the last green sha;
  - the last green run (`last_green_at`) is older than the interval, or there never was one.

  An unreadable state counts as due, because a run rewrites the state.
- **Scheduling.** The runner's `_sentinel_step` keeps its candidates: projects with sentinel state
  (every merge records it).
  - A project that is not due by SPEC-0359's `check_due` is still considered when the clock is on
    and the project has close gates.
  - The base sha is resolved, and the project is scheduled if `time_due` holds.
  - `SN.backoff` then applies unchanged. A failed or red run at the same sha waits `RETRY_S`; a
    new base retries at once.
- **Placement.** `SN._placement` gives the slot pools.
  - The pools are `workers.gate_pools(cfg)`: worker pools while a worker is placeable, and local
    only when none is (SPEC-0374).
  - It falls back to local only when the project's close gates are not remote-eligible
    (`remote_gates.ineligible`, `header_refusal`).
  - On a worker slot the run calls `remote_gates.run_detached`. That is a campaign-less dispatch:
    no RJ record and no ledger, job id `sentinel-<project>-gate-NNN-t<time>`, with the bundle cut
    at the detached base HEAD.
  - A `RemoteFailure` sets the worker cooldown and becomes a failed sentinel run, noted in
    `last.json` and retried by the backoff. There is no local fallback, so a flapping worker box
    does not drag a 28-minute suite onto the host.
- **Forcing the full suite remotely.** A worker forwards only `NW_CAMPAIGN`, `NW_SPEC_ID` and
  `NW_BASE`. So the remote job's campaign id `sentinel-<project>` reaches the gate as
  `NW_CAMPAIGN=sentinel-<project>`, and SPEC-0378's `affected_tests.py` treats that as forced
  full. A local run sets `NW_SENTINEL=<project>`.

## Design

- `sentinel.py`:
  - `DEFAULT_INTERVAL_HOURS = 6.0`, `interval_hours(cfg)`, `time_due(...)`, `_placement(...)`.
  - `run(...)` acquires `gate_slots.acquire(..., pools=, repool=)`. When `hold.pool` is not
    `None` it runs `RG.run_detached`; otherwise it runs the local gates as before.
  - Settling (`settle`), red escalation and `last.json` are unchanged.
- `remote_gates.py`:
  - `run_detached(worker, project_cfg, state, cfg, seq, gates_dir, stop=None) -> GateRun` shares
    `_dispatch` (now taking `gates_dir`).
  - It writes no campaign records.
- `queue.py` `_sentinel_step`: the clock branch described under Decision.

## Alternatives considered

- **Fixed wall-clock schedule (cron-like).** Rejected: it runs even when main did not move. The
  "moved and older than N" rule costs nothing on idle days.
- **Run after every N merges.** That is already SPEC-0359's count threshold, and it counts only
  reused merges. The clock covers the rest without a second counter.
- **Fall back to local on a transport failure.** Rejected: it brings back the host contention this
  spec avoids. The backoff retries within `RETRY_S`, or immediately on a new base.
- **Widen the candidates to every busy project.** Rejected for now: it would create sentinel state
  for projects that never merged, and it would hit `project_config` errors on the tick. Every merge
  already records state.

## Acceptance criteria

- [x] `interval_hours` defaults to 6. `0`, `false`, `null` and negative values turn it off. A
  non-number raises.
- [x] `time_due` is true only when the base moved since the last green run and that run is older
  than the interval (or absent). It is false when the base is unmoved, the clock is off, or the
  project's sentinel is disabled. Changing the interval changes the answer.
- [x] The runner schedules a clock-due project. It does not schedule when the base is unmoved,
  when the interval is 0, or when the project has no close gates.
- [x] With a placeable worker the run takes a worker pool and dispatches via `run_detached` with
  the close gates. It settles green or red as a local run would.
- [x] A remote transport failure fails the run and never runs the gates locally.
- [x] With no placeable worker, or remote-ineligible gates, the run is local.
- [x] A remote sentinel's gate sees `NW_CAMPAIGN=sentinel-<project>` and selects the full suite.

## Test plan

- `tests/test_agentic_sentinel.py`:
  - `interval_hours`: parametrized values, a non-number, and `{}` gives the default.
  - `time_due`: moved base and older last green, or none; unmoved base; disabled by
    0/False/None/-1; project sentinel disabled; interval change 6/12/0.5; unreadable state.
  - The runner clock trigger: scheduled on a moved base; not scheduled when the base is unmoved,
    the interval is 0, or there are no close gates.
  - `_placement`: worker pools versus local or ineligible.
  - A run on a worker slot calls `run_detached` with the close gates and settles green.
  - A transport failure raises and never runs the local gates.
- `tests/test_affected_tests.py`: `NW_CAMPAIGN=sentinel-x` and `NW_SENTINEL` force full; a
  campaign id does not (SPEC-0378).
- Run: the touched test files (the affected selection runs at the queue's close gate).

## Rollout / migration

- The clock takes effect on the next runner start after merge. Use `nw queue restart` (SPEC-0375).
- Existing `<project>.json` state is read as-is. A project that never had a green run is
  clock-due on the first tick where its base resolves.
- Set `agentic.sentinel.interval_hours: 0` to keep the SPEC-0359-only behaviour.

## Definition of done

- [x] Acceptance criteria true, and the test plan executed (see *As shipped*).
- [x] Ledger regenerated; `spec_lint` exits 0.

## As shipped

- Executed: `tests/test_agentic_sentinel.py` (50 passed), `tests/test_affected_tests.py`,
  `tests/test_agentic_close_reuse.py`, `tests/test_remote_gates.py`. The branch's full affected
  selection was stopped on the human's request to expedite.
- Not verified here: a real remote sentinel run on the worker box, and a real runner tick. The live
  queue and the worker were not touched. The remote path is covered by the tests with a faked
  `run_detached`.
- Per the repo memory, validate one real `run_detached` dispatch against the live worker after
  merge, before relying on it.

## Open questions

None.
