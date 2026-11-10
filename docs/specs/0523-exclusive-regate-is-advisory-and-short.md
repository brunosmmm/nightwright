---
id: SPEC-0523
title: "An exclusive re-gate is advisory: it holds other campaigns' gates for at most 300 s"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, gates, bug]
depends_on: [SPEC-0311, SPEC-0507, SPEC-0522]
---

## Context

SPEC-0311 re-gates a campaign whose gate timed out (load, not a code fault) **exclusively**:
`state.gate_exclusive = {entered_at}` is set (`queue.py`, timeout branch of the gate result),
and `select_gate_starts` / `_gate_starts` then start no other campaign's gate while that
marker is pending (once anything else is in flight) or while its gate step runs. Two later
specs bounded this, both too loosely:

- SPEC-0507: a **pending** marker older than `agentic.gates.exclusive_wait_max_s`
  (default `EXCLUSIVE_WAIT_MAX_S = 2700` s) is cleared and the gate starts normally.
- SPEC-0522: a gate step **in flight** longer than the project's longest gate timeout +
  `GATE_HOLD_SLACK_S` (2400 + 600 = 3000 s) stops holding other starts.

Observed 2026-11-09: SPEC-0476-20261109-ca46 timed out on `pytest-affected` (2400 s) at
21:15:06 and got the marker; its exclusive gate step started at 21:37 and was restarted alone
by the runner re-exec at 21:48 (merge of SPEC-0507), so the SPEC-0522 bound (counted from the
flight start) began again. Six GATING campaigns (IDEA-1070, IDEA-1040, IDEA-1039, IDEA-1020,
SPEC-0335, IDEA-1015) made no progress the whole time. The worst-case block was ~45 min
pending + ~50 min running, restarted by every runner re-exec.

The premise is also wrong for this setup: the worker box runs other jobs (other projects,
CLI gate jobs) anyway, so holding *our* other campaigns back does not make the re-gate run
alone; it only stalls the queue.

## Goals / Non-goals

**Goals**
- No campaign's exclusive re-gate blocks other campaigns' gate starts for more than a short,
  fixed bound (300 s), pending and running combined, across runner re-execs.
- Keep the ordering SPEC-0311 still buys (a fresh exclusive re-gate starts first, closes that
  re-gate yield to it per SPEC-0507) within that bound.
- Reuse the SPEC-0507 / SPEC-0522 mechanisms; no new state fields.

**Non-goals**
- Detecting foreign load on the box before setting the marker (the short bound makes the
  marker harmless; detection would be a new moving part).
- Changing SPEC-0522's non-exclusive hold bound or the remote attach fix.
- Changing `TIMEOUT_REGATE_MAX` / the `gate_timeout` park.

## Decision

Exclusivity becomes advisory with one bound, measured from the marker's `entered_at` (which
survives runner re-execs), not from the flight start:

1. `EXCLUSIVE_WAIT_MAX_S` drops from 2700 to **300** s. `agentic.gates.exclusive_wait_max_s`
   may shorten it but never lengthen it (values above 300 are clamped).
2. A pending marker older than the bound is cleared and its gate starts normally (SPEC-0507's
   `_expire_exclusive`, unchanged, now with the 300 s bound).
3. A gate step **in flight** counts as exclusive only while its marker is at most the bound
   old (`_gate_flights`). Past it the step is a normal gate step: other campaigns' gates start
   beside it, closes that re-gate no longer yield to it (`_exclusive_held` reads the same
   flag), and the runner journals `gate_exclusive_lapsed {campaign, age_s, bound_s}` once per
   flight. An unreadable `entered_at` counts as infinitely old (holds nobody).

So a marker can hold the others for at most 300 s in total, whether it waits, runs, or the
runner re-execs in between.

## Design

- `src/nightwright/agentic/queue.py`
  - `EXCLUSIVE_WAIT_MAX_S = 300` (comment cites SPEC-0507 + SPEC-0523).
  - `exclusive_wait_max_s(cfg)`: `min(int(raw), EXCLUSIVE_WAIT_MAX_S)` for a valid positive
    setting, else the constant.
  - `_gate_flights`: after the SPEC-0522 hold check, `exclusive` is
    `step.kind == "gate" and gate_exclusive(fl.state) and exclusive_waited_s(fl.state, now) <=
    exclusive_wait_max_s(cfg)`; when the age check flips it to False, journal
    `gate_exclusive_lapsed` once (key `("gate_exclusive_lapsed", campaign, fl.started_at)` in
    `pool.deferred_noted`). The step still counts in `in_flight`, so a *different* pending
    exclusive marker waits for it, but only up to its own 300 s.
- `src/nightwright/agentic/schemas.py`: register
  `"gate_exclusive_lapsed": ("campaign", "age_s", "bound_s")` in `RUNNER_KINDS`.
- Text of earlier specs, updated here (they are not edited): SPEC-0311 "the exclusive re-gate
  runs alone" now reads "runs first, holding others at most 300 s from its marker"; SPEC-0507's
  2700 s default is now 300 s and the setting can only shorten it; SPEC-0522's in-flight hold
  bound still applies to every gate step, but an exclusive one stops being exclusive at 300 s
  from its marker, well before it.

## Alternatives considered

- **Do not set the marker when the box has other holders** — needs a reliable view of foreign
  load on the box at decision time (slots, other runners' jobs); more code, more failure
  modes, and still needs a bound for the case where the box fills up after the marker is set.
- **Remove exclusivity entirely** — simplest, but loses the one useful effect (a timed-out
  re-gate goes first and closes yield to it briefly). With a 300 s bound the cost is small.
- **Bound the in-flight hold from the flight start** (SPEC-0522 style) — resets on every
  runner re-exec, which is exactly how ca46 kept holding the queue; the marker's
  `entered_at` does not.

## Acceptance criteria

- [x] `EXCLUSIVE_WAIT_MAX_S == 300`; `exclusive_wait_max_s` returns at most 300 for any
      setting (600 -> 300, 120 -> 120, invalid -> 300).
- [x] An exclusive gate step in flight whose marker is older than the bound does not block
      other GATING campaigns: at the next tick all of them start their gate
      (tick-level test with six waiting campaigns).
- [x] `gate_exclusive_lapsed` is journaled once per flight, not per tick, and is registered in
      `schemas.RUNNER_KINDS`.
- [x] After a runner re-exec (nothing in flight) a pending marker older than the bound is
      cleared and the exclusive campaign plus all waiting campaigns start in the same tick.
- [x] A fresh exclusive gate step still holds the others (ordering kept), and stops holding
      them once its marker passes the bound.
- [x] SPEC-0311 / SPEC-0507 / SPEC-0522 tests still pass (adjusted only where they encoded the
      old 2700 s value or an unreadable marker holding forever).
- [ ] Live: after the runner re-execs onto this merge, the six GATING campaigns waiting
      behind SPEC-0476-20261109-ca46 start their gates at the first tick.

## Test plan

- **Automated tests** (`tests/test_queue_exclusive_regate.py`):
  - `test_in_flight_exclusive_past_bound_releases_waiting_gates`: real `Q.tick` with a
    `StepPool` holding CA's never-finishing exclusive gate step (marker 35 min old, the ca46
    state) and six GATING campaigns with a faked `_do_gate`; asserts all six start, CA is not
    restarted, `gate_exclusive_lapsed` journaled once over two ticks.
  - `test_reexec_releases_pending_exclusive_and_waiting_gates`: same states, empty pool (the
    post-re-exec situation); one tick starts all seven, CA's marker is cleared with the
    SPEC-0507 note and `gate_exclusive_expired`.
  - `test_fresh_exclusive_still_runs_first_but_holds_at_most_the_bound`: `_gates_in_flight`
    is `(1, True)` at 270 s and `(1, False)` 60 s later; `select_gate_starts` then admits C.
  - `test_exclusive_wait_max_setting`: the clamp.
- **Regression guard:** `tests/test_queue_exclusive_regate.py`, `tests/test_agentic_queue.py`
  (SPEC-0311 serialization, SPEC-0522 hold expiry), `tests/test_remote_job_records.py`
  (SPEC-0341/0522 attach and detach); `python3 tools/spec_lint.py`.
- **Proof the tests catch the bug:** the two tick-level tests run with
  `EXCLUSIVE_WAIT_MAX_S` patched back to 2700 must fail.
- **Manual verification:** read-only replay of the live states under
  `~/.local/share/nightwright/agentic/campaigns` (ca46 marker `entered_at` vs. the bound);
  after merge, `runner.jsonl` shows `gate_exclusive_expired` for ca46 and gate starts for the
  six campaigns at the first tick after the re-exec.

## As shipped

Code exactly as in Design. Run (2026-11-09, worktree `excl-gate-bounded`):

- `uv run pytest -q tests/test_queue_exclusive_regate.py tests/test_agentic_queue.py`:
  208 passed (17 in the exclusive file, three new).
- `uv run pytest -q tests/test_remote_job_records.py`: 51 passed (the file has no tests
  named for exclusive/hold; the whole file was run).
- The two new tick-level tests with `EXCLUSIVE_WAIT_MAX_S` patched to 2700 (a pytest plugin
  in the scratchpad): both fail, i.e. they reproduce the stall.
- Read-only check of the live queue at 21:50: ca46 is GATING with marker
  `entered_at 2026-11-09T21:15:06-05:00` (~35 min, > 300 s); the six others are GATING with no
  marker; the runner re-exec'd at 21:48:08 onto 311382e5 and re-started only ca46's gate. On
  the re-exec onto this change nothing is in flight at the first tick, so SPEC-0507's expiry
  (now 300 s) clears ca46's marker and all seven gates are admitted in that tick; had ca46's
  step stayed in flight, the new in-flight check would have released the others too.
- **Not run, waived by the human for speed:** the full suite (`uv run pytest`) and the
  worker-box gates / queue close gate. Status stays `accepted` until the live criterion above
  is observed in `runner.jsonl`.

## Rollout / migration

Merge to main; the runner follows the merge by itself (drain + re-exec). No state migration:
existing markers are judged by their `entered_at`.

## Definition of done

- [ ] Acceptance criteria all met (live criterion pending).
- [x] Test plan executed except the waived full suite / box gates.
- [x] No regressions in the focused files.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
