---
id: SPEC-0311
title: "Gate timeouts under machine load are re-gated, not triaged as code fixes"
status: proposed
owner: user
created: 2026-10-30
kind: feature
source_idea: IDEA-432
tags: [agentic, gates, queue]
---

## Context

A campaign's gates run in `G.run_all` (`src/wt/agentic/gates.py:383`); each gate has a
`timeout_s` (default 1200, `gates.py:177`) and a gate that exceeds it is killed and recorded
with `timed_out=True`. SPEC-0289's retry ladder deliberately never retries a timed-out gate
(`run_gate_with_retries`, `gates.py:289`), because rerunning a slow suite inside the same
busy budget would only double the damage.

The queue then treats a timeout like any other failure. In `_apply`'s gate branch
(`src/wt/agentic/queue.py`, the `kind == "gate"` block) a run that is not `ok` moves the
campaign to `TRIAGING`, the gate-triager persona runs, and `decide_triage`
(`queue.py:287`) maps its verdict to a transition. Anything but `retry_gate`, `waive` or
`escalate` becomes `fix`: `counters["loops"] += 1` and the implementer is sent back with a
fix brief. `retry_gate` is honoured once per loop (`GATE_RETRY_MAX = 1`) and then also
degrades to `fix`.

That is wrong when the timeout comes from machine load rather than from the code. With six
campaigns gating concurrently, the full `pytest` gate takes more than 1200 s on a healthy
tree and times out. Nothing in the branch is broken, but the campaign pays for a triager run
and (when the triager says `fix`) an implementer run that has nothing to fix. Each loop
burns one of `loop_max` (default 3), so a few load spikes end in `PARKED(loop_exhausted)`
(`queue.py:322`). That park reports "still failing gates" and sends a human looking for a
bug that does not exist.

## Goals / Non-goals

**Goals**
- A gate run whose only blocking failures are timeouts never reaches the triager and never
  consumes an implement loop.
- Such a run is re-gated automatically, a bounded number of times, with the re-gate
  serialized against other campaigns' gate steps so it is not run under the same load.
- When the re-gate budget is spent, the campaign parks with a reason that names the
  timeout (`gate_timeout`), not `loop_exhausted` or `triage_escalated`.
- The ledger shows each timeout re-gate, so a load-induced delay is visible and countable.

**Non-goals**
- Changing a gate's `timeout_s`, adding adaptive timeouts, or limiting how many campaigns
  gate at once in general (slot admission, SPEC-0180, is untouched).
- Changing SPEC-0289's in-gate retry ladder: a timed-out gate is still not retried inside
  `run_gate_with_retries`.
- Changing `decide_triage` for non-timeout failures, or the gate-triager persona/prompt.
- Detecting machine load directly (load average, memory); a timeout is the only signal.

## Decision

Handle gate timeouts deterministically in the queue, before triage. After a gate run, if it
failed, and every failing **required** gate result is `timed_out` and not `interrupted`, the
campaign goes straight back to `GATING` (reason `gate_timeout_regate`) instead of
`TRIAGING`, increments a new per-loop counter `timeout_regates`, and does not touch `loops`.
Re-gates after a timeout are serialized: at most one timeout re-gate step runs at a time, it
starts only when no gate step is in flight in the same runner, and while one is pending the
runner starts no new non-exclusive gate step so it cannot be starved. Once
`timeout_regates` reaches `TIMEOUT_REGATE_MAX` (2), the next timed-out run parks the
campaign `gate_timeout` with `resume_to="GATING"`. A run that mixes a timeout with a
genuine failure (a required gate that failed without timing out) goes to triage as today.

The thresholds (2 re-gates, serialization instead of a fixed sleep/backoff) are decided
here from the observed failure: concurrency is the cause, so removing concurrency is the
cure; a fixed delay would not.

## Design

**Classification (pure).** Add `timeout_only(run_payload: dict) -> bool` in `queue.py` next
to `decide_triage`. It takes the `last_gate` payload shape (`G.as_payload`) and returns true
iff there is at least one failing required result, every failing required result has
`timed_out` true, and none has `interrupted` true. Optional (non-required) failing gates are
ignored, as `GateRun.ok` ignores them.

**Planning/transition.** Add `decide_gate_timeout(state: dict) -> Step`, pure, called from
the gate branch of `_apply` when `run.ok` is false and `timeout_only(state["last_gate"])`:

- `counters["timeout_regates"] < TIMEOUT_REGATE_MAX` ⇒ `Step("gate", reason="gate_timeout_regate")`;
  `_apply` increments `counters["timeout_regates"]` and `finish("GATING", "gate_timeout_regate")`.
  The `gate` ledger event for the timed-out run is still appended first (unchanged), and a
  `note` event (author `queue`) records the gate names, their `timeout_s`, and
  `timeout_regates` after the increment.
- otherwise ⇒ `Step("park", reason="gate_timeout", resume_to="GATING", detail=…)` where the
  detail names the timed-out gates and the budget (`"<n> gate timeout re-gates used; <gate>
  still times out after <timeout_s>s; likely machine load or a gate whose timeout_s is too
  low"`). `gate_timeout` is added to the known park reasons (`queue.py:68`) and to any
  place that renders or classifies park reasons.

All other failing runs keep the current path (`finish("TRIAGING", "gates_failed")`).

**Counters.** `timeout_regates` is a campaign counter, defaulted to 0 wherever
`gate_retries` is initialised (the `counters` literals and the resume paths around
`queue.py:800`, `2998` and `6029`). It resets to 0 on a `fix` triage (the new loop), on
resume from a park, and whenever a gate run is green or fails for a non-timeout reason, so
the budget is "consecutive timeouts", not "timeouts ever". `loops`, `gate_retries` and
`triage_retries` are not modified by this path.

**Serialization.** A campaign whose last gate event was a timeout re-gate (state
`GATING` with `timeout_regates > 0` and the previous run `timeout_only`) is marked
`exclusive` for its next gate step. While any campaign has a pending exclusive gate step
(planned but deferred), the planner starts no new non-exclusive gate step for any
campaign; gate steps already in flight run to completion, and the exclusive step starts on
the first tick with no gate step in flight. Exclusive steps run one at a time, ordered by
`(entered_at, id)`. Gate steps planned earlier in the same tick count as in flight. A
deferred step is simply retried on a later tick (no state change, no event spam: at most
one `gate_timeout_deferred` note per campaign per timeout). Blocking new non-exclusive
starts while an exclusive step is pending (not only while it is in flight) is what keeps the
re-gate from starving when other campaigns keep entering `GATING`. Only the gate step is
serialized; implementer, reviewer and triager runs of other campaigns proceed.
`G.run_all` and the per-gate timeout are unchanged. The deferred step was never submitted,
so no busy budget (`gate_budget_s`) covers the wait; the wait is bounded instead by the
drain of the gate steps already in flight.

**Interaction with other paths.** `retry_gate` (triager) and `timeout_regates` are
independent budgets. The review/close re-gate (`_close_regates`, `queue.py:1644`) is out of
scope: its failure still goes through `TRIAGING`.

**Surfacing.** `wt queue show`/status output that prints park reasons shows
`gate_timeout` like any other reason; the campaign export (`campaign_export.py:183`) may
add `gate_timeouts` (the count of `gate_timeout_regate` transitions) next to
`loops_exhausted`.

## Alternatives considered

- **Teach the triager prompt to answer `retry_gate` for timeouts** — nondeterministic, costs
  a triager run each time, and `retry_gate` is capped at once per loop then degrades to
  `fix`; the cap is exactly what turns load into `loop_exhausted`.
- **Retry the timed-out gate inside `run_gate_with_retries`** — contradicts SPEC-0289 and
  blows the busy budget (`gate_budget_s`); and the retry would run under the same load.
- **Raise `timeout_s` or add a fixed backoff sleep** — hides the symptom, makes real hangs
  slower to detect, and still runs under contention.
- **Global limit on concurrent gates** — fixes more than the bug asks and changes slot
  semantics (SPEC-0180); serializing only re-gates after a timeout is the minimal fix.

## Acceptance criteria

- [ ] `timeout_only` is true for a failed `last_gate` whose failing required results are all
      `timed_out` and not `interrupted`; false when any failing required result failed
      without timing out, when a failing result is `interrupted`, when only optional gates
      failed, and for a green run.
- [ ] A gate run that fails with only timeouts moves the campaign to `GATING` (not
      `TRIAGING`) with transition reason `gate_timeout_regate`; `counters["loops"]`,
      `gate_retries` and `triage_retries` are unchanged and `timeout_regates` is
      incremented by 1; no gate-triager run is started.
- [ ] After `TIMEOUT_REGATE_MAX` (2) timeout re-gates, a further timeout-only failure parks
      the campaign with reason `gate_timeout`, `resume_to="GATING"` and a detail naming the
      timed-out gate(s); the park is not `loop_exhausted` and not `triage_escalated`.
- [ ] A mixed failure (one gate timed out, another required gate failed normally) goes to
      `TRIAGING` exactly as before.
- [ ] `timeout_regates` resets to 0 on a `fix` triage, on a resume from park, and after a
      gate run that is green or fails for a non-timeout reason.
- [ ] The ledger records the timed-out `gate` event plus a `note` event naming the gates and
      the new `timeout_regates` value for each timeout re-gate.
- [ ] While a timeout re-gate is in flight, the runner starts no other campaign's gate step;
      a timeout re-gate does not start while another campaign's gate step is in flight; both
      defer to a later tick without changing campaign state. Non-gate steps are not delayed.
- [ ] While an exclusive gate step is deferred, no new non-exclusive gate step starts; the
      exclusive step starts once the in-flight gate steps finish. Multiple pending exclusive
      steps start one at a time, ordered by `(entered_at, id)`.
- [ ] With six campaigns where each full-suite gate times out once under concurrency and
      passes when run alone, all six reach `REVIEWING` and none parks `loop_exhausted`.
- [ ] `gate_timeout` is accepted wherever park reasons are validated or rendered, and
      `python3 tools/spec_lint.py` and `uv run pytest` pass.

## Test plan

- **Automated tests** (`tests/test_agentic_queue.py`, pure functions, mirroring
  `test_triage_retry_gate_once_per_loop_then_fix`):
  - `test_timeout_only_classification`: parametrised over the cases in the first
    acceptance criterion.
  - `test_gate_timeout_regates_then_parks`: `decide_gate_timeout` returns `gate` with
    reason `gate_timeout_regate` at `timeout_regates` 0 and 1, and `park` with reason
    `gate_timeout`, `resume_to="GATING"` at 2.
  - `test_mixed_timeout_and_failure_goes_to_triage`: `_apply` of a mixed run lands in
    `TRIAGING`.
- **Queue-level tests** (`tests/test_agentic_queue.py`, using the fake runner and gate
  fixtures already used by the gate tests):
  - `test_timeout_only_run_skips_triager`: a gate run faked as timed out ends in `GATING`,
    `loops == 0`, no `triage` run recorded, `timeout_regates == 1`, ledger carries the
    `gate` and `note` events.
  - `test_timeout_budget_exhausted_parks_gate_timeout`: three consecutive faked timeouts
    park `gate_timeout`, with `loops` still 0.
  - `test_timeout_regates_reset`: reset on `fix`, resume, green, and non-timeout failure.
  - `test_timeout_regate_is_serialized`: two campaigns in `GATING`, one flagged for
    timeout re-gate; assert the planner never has two gate steps in flight with the
    exclusive one, and that the deferred step runs on a later tick with no state change.
  - `test_exclusive_regate_not_starved`: campaign A is deferred exclusive while B's gate is
    in flight and C enters `GATING`; C's gate does not start before A's, A starts on the
    first tick with no gate step in flight, and two pending exclusive steps start in
    `(entered_at, id)` order, one at a time.
  - `test_six_campaigns_timeout_under_concurrency`: six campaigns, a fake gate that times
    out when more than one gate runs at once and passes alone; all reach `REVIEWING`.
- **Gate tests** (`tests/test_agentic_gates.py`): the existing
  `test_timed_out_attempt_zero_not_retried` and the retry-timeout tests stay green
  unchanged, proving the in-gate ladder is untouched.
- **Manual verification:** in a scratch project with a single gate `sleep 3` and
  `timeout_s: 1`, queue one campaign with a fake implementer; observe with
  `wt queue show` that it re-gates twice (`gate_timeout_regate` in the ledger) and then
  parks `gate_timeout` with `loops` at 0; `wt queue resume` re-gates once more.
- **Regression guard:** `uv run pytest` (full suite) passes, including all existing
  `decide_triage`, gate-ladder and park-reason tests; `python3 tools/spec_lint.py` exits 0.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None; the re-gate budget (2) and serialization approach are decided above and can be
  tuned from `gate_timeout_regate` counts in the ledger.
