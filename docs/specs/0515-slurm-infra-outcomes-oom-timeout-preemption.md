---
id: SPEC-0515
title: "OOM kills, Slurm time limits, preemption and node failures are infra outcomes: retried by policy, never triaged as test failures"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, gates, slurm, triage, reliability]
depends_on: [SPEC-0510, SPEC-0504]
---

## Context

Under Slurm's cgroup a job that exceeds `--mem` is killed (`OUT_OF_MEMORY`); one that exceeds
`--time` ends `TIMEOUT`; a `bulk` job may be `PREEMPTED` and requeued (SPEC-0511); a node may
fail (`NODE_FAIL`). SPEC-0510 turns an end without `result.json` into an `error` frame
`{stage: "slurm", slurm_state, infra: true}`. Memory has never been measured (SPEC-0501), so OOM
kills are likely in the first weeks. If a gate killed by OOM inside a running job is read as a
test failure, the gate triager (SPEC-0176 family) spends an LLM run on a non-bug and may send an
implementer to "fix" it. Today's timeout handling (SPEC-0311 `timeout_only`, re-gate) is for
nw's own `timeout_s`, which stays.

## Goals / Non-goals

**Goals**
- An OOM kill of a gate (whole job or a gate process inside it) is detected and reported as
  `oom`, not `failed`.
- Policy: OOM -> one retry with 1.5x memory (capped by the node), then park with a clear reason;
  Slurm TIMEOUT -> same as nw's gate timeout; PREEMPTED -> requeue (Slurm does it), not counted;
  NODE_FAIL -> one retry, then local fallback (SPEC-0338) with reason.

**Non-goals**
- Changing nw's own `timeout_s` semantics.

## Decision

Add an `infra` outcome to gate results: `GateResult.infra = "oom"|"slurm_timeout"|"node_fail"|
"preempted"|None`. The worker sets it per gate (a gate whose process was SIGKILLed while the job
cgroup's `memory.events` `oom_kill` counter rose is `oom`); the coordinator maps job-level
`slurm_state` without a result to the same codes. The queue's gate decision treats a run whose
failing gates are all `infra` as an infra run (like `timeout_only`), applying the policy above.

## Design

- Worker (`_run_gates`): read `memory.events` `oom_kill` from the job cgroup (SPEC-0514's path)
  before/after each gate; if it rose and the gate's return code is -9/137, set `infra: "oom"` on
  the `gate` frame.
- Coordinator (`remote_gates`): an `error` frame with `stage: "slurm"` becomes a GateRun whose
  unrun gates are marked `infra` with the mapped code (OUT_OF_MEMORY -> `oom`, TIMEOUT ->
  `slurm_timeout`, NODE_FAIL/BOOT_FAIL -> `node_fail`; PREEMPTED never reaches here because
  `--requeue` restarts it, SPEC-0503 attempts).
- Queue (`queue.py`, next to `timeout_only`/`decide_gate_timeout`): `infra_only(payload)`;
  `decide_gate_infra(state)`: `oom` with `counters.oom_retries < 1` -> re-gate with
  `state.mem_boost = 1.5` (the header's request x 1.5, ceil to 256 MB, capped at the node's
  schedulable memory) ; else park `gate_oom` with detail "gate X used > N MB; raise
  resources.mem in .nw/project.yaml (nw gate stats suggests M)". `slurm_timeout` follows
  `decide_gate_timeout`. `node_fail` -> one re-gate, then SPEC-0338 fallback with SPEC-0505
  reason `node_fail`.
- Triage: the triager is never started for an `infra_only` run (the decision above runs first).
- `nw campaign show` renders `oom` / `slurm timeout` next to the gate.

## Alternatives considered

- **Let triage judge OOM.** Costs a run and invites code changes for a config problem.
- **Unbounded memory escalation.** Hides a real leak; one boost, then a human decision.

## Acceptance criteria

- [ ] A gate OOM-killed inside a Slurm job is reported `infra: "oom"`; the run is not triaged.
- [ ] The first OOM re-gates with 1.5x memory; the second parks `gate_oom` with the suggested
      value.
- [ ] A job ending `TIMEOUT` follows the gate-timeout path; `NODE_FAIL` re-gates once then falls
      back locally with reason `node_fail`.
- [ ] Non-infra failures are unchanged.

## Test plan

- **Automated tests:** `tests/test_gate_infra_outcomes.py`: worker OOM detection with a fake
  cgroup `memory.events`; coordinator mapping of each `slurm_state`; queue decisions (boost,
  park, timeout, node_fail) as pure tests; triage not started for `infra_only`.
- **Manual verification (real binary):** on the box, a throwaway project gate
  `python3 -c "b=bytearray(3<<30)"` with `resources: {mem: 1G}` under Slurm: the campaign shows
  `oom`, re-gates with 1536 MB, parks `gate_oom` on the second OOM; `scontrol show job` (as
  bruno) shows `OUT_OF_MEMORY` for the job-level case. Record in As shipped.
- **Regression guard:** affected tests of `queue.py`, `remote_gates.py`, `worker.py`.

## Rollout / migration

Ships with or right after SPEC-0510. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- A test that legitimately expects a SIGKILL could be misread as OOM; the `oom_kill` counter
  check makes that require a real OOM in the same window.

## Open questions

- None.
