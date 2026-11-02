---
id: SPEC-0376
title: "Close-tier gates with reuse_from stay remote-eligible (the job header drops reuse_from)"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, workers]
depends_on: [SPEC-0338, SPEC-0359]
source_idea: IDEA-782
---

## Context

IDEA-782: campaign IDEA-715-20261102-14b5 entered CLOSING at 12:08 and recorded the
`remote-eligibility` finding "the worker would refuse this job; gates run locally". The
detail was
`payload.gates: invalid project config: gates[2].reuse_from: 'pytest-affected' is not a
`when: loop` gate of this config`. The box was updated at ~12:20 and the runner restarted at
12:27 on e0d15796, and the close gate still took a local slot. The idea guessed at a sticky
verdict, a stale worker build or a cached `worker info`.

None of those is the cause. `pools_for` re-runs eligibility at every gate start, and nothing
persists a verdict or caches worker features. The refusal comes from the **coordinator's own**
dry `header_refusal` check, and it repeats on every close-tier run:

1. `gates.run_all(..., tier="close")` narrows the gate list to `P.gates_for(cfg, "close")`,
   which holds the close gates and the `always` gates. The loop gate `pytest-affected` is
   dropped.
2. `_header` copies each gate except the `remote` key. The close gate `pytest` keeps
   `reuse_from: pytest-affected`.
3. `worker.validate_header` validates `payload.gates` with the project validator. Its
   SPEC-0359 rule says `reuse_from` must name a `when: loop` gate *in the same list*. That
   gate was dropped in step 1, so the job is refused.

Any `nw` build fails this way, on both the coordinator and the worker. Updating the box could
not fix it. Every close-tier gate of a project whose close gate uses `reuse_from` (this
repo's own config) ran locally after SPEC-0359. The finding is recorded once per campaign, so
the later refusals left no trace, and that made the verdict *look* sticky.

## Goals / Non-goals

**Goals**
- A close-tier gate run whose gates use `reuse_from` is remote-eligible, and runs on the worker.
- Eligibility keeps being decided fresh at every gate start, with no persisted verdict.

**Non-goals**
- Changing the one-finding-per-campaign rule (SPEC-0338).
- Probing `nw worker info` per gate start. Eligibility is a coordinator-side dry check by
  design, and a real worker refusal is handled by SPEC-0367's `worker-rejected` path.

## Decision

`reuse_from` is a coordinator-only key, like `remote`. The close decides on reuse
(`close._reuse_loop_green`) before dispatch, and the worker never uses the key. `_header`
drops both keys (`remote_gates._COORDINATOR_ONLY`) from the gate list it sends. The dry
`header_refusal` check uses the same `_header`, so eligibility and the real job agree.

## Design

- `src/nightwright/agentic/remote_gates.py`: `_COORDINATOR_ONLY = {"remote", "reuse_from"}`.
  `_header` filters each gate dict by that set.
- Compatibility: a worker on any build accepts the slimmer list. The `reuse_from` key, and so
  its cross-reference check, is simply absent.

## Alternatives considered

- **Validate the untrimmed config on the worker.** That would send the loop gates the worker
  must not run. Rejected.
- **Relax the validator's cross-reference rule for `payload.gates`.** That needs a worker
  update to take effect, and the coordinator's dry check would still need the same change.
  Dropping the key fixes the problem on the coordinator alone.

## Acceptance criteria

- [x] With a loop gate and a close gate that `reuse_from`s it, `header_refusal` of the
      close-tier list is None, and `pools_for` returns the worker pools.
- [x] `run_all(..., tier="close")` runs on the worker (`run.worker == "box"`). The job header
      has no gate with `reuse_from` or `remote`, and no `remote-eligibility` finding is
      written.
- [x] A run that was ineligible once is eligible at the next gate start once the cause is
      gone. No verdict persists.
- [x] This repo's real `.nw/project.yaml` has no header refusal for either tier.

## Test plan

- **Automated tests** (`tests/test_remote_gates.py`):
  `test_a_close_tier_run_with_reuse_from_is_remote_eligible` (AC1, AC2). It fails without
  the fix, with the exact refusal from IDEA-782. `test_eligibility_is_re_evaluated_at_every_gate_start` (AC3).
- **Manual verification:** `header_refusal` on the repo's real config, for each tier, gives
  `None` for both (AC4). `ssh nw@morais-Desktop 'nw worker info --json'` returns build
  b1:15ce1fa2 with protocol `nw.job.v1`. That build validates the trimmed list the same way.
- **Regression guard:** the existing `tests/test_remote_gates.py` suite, including the
  golden record, plus affected tests.

## Rollout / migration

Coordinator-only change. Restart the runner on a build that includes it. No worker update is
needed.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass.
- [x] No regressions in the affected tests (the full suite runs at the queue's close gate).
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## As shipped

As designed. Red/green: without the `_header` change, the new close-tier test fails in
`header_refusal`. With it, both new tests pass.
