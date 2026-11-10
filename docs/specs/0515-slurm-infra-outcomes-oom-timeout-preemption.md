---
id: SPEC-0515
title: "OOM kills, Slurm time limits, preemption and node failures are infra outcomes: retried by policy, never triaged as test failures"
status: in-progress
owner: user
created: 2026-11-09
updated: 2026-11-10
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

As shipped (decided while implementing; details in As shipped below):

- **Worker OOM detection** lives in its own module `cgroup_oom.py` (not SPEC-0514's accounting
  path, a parallel lane): inside a Slurm job (`SLURM_JOB_ID`) it reads `memory.events`
  `oom_kill` of the process's own cgroup and each ancestor up to the job level (the directory
  right under `slurmstepd.scope`), taking the largest; a red, not interrupted gate with exit -9
  or 137 while it rose is `infra: "oom"`.
- **A TERM from Slurm is not a cancel.** SPEC-0510 made every SIGTERM inside a job a cancel
  (`done` interrupted). Then Slurm's time limit (`--signal=B:TERM@60`) or a preemption ended
  the job with `done` interrupted (a coordinator fallback), and a requeued job's attempt 2 came
  after a `done` the coordinator had already acted on. Now a TERM **with** the job's `cancel`
  file (nw's own cancel writes it first) is still a cancel; one **without** ends the gates,
  mutes the final frame (`_Frames.mute_final`), writes no `result.json`, and waits (bounded,
  `NW_WORKER_SLURM_END_WAIT_S`, 150 s) for Slurm's KILL, so Slurm's own end state stands
  (`TIMEOUT`; a requeue reruns as attempt 2) and the reader writes the `error` frame for it.
- **Coordinator mapping** (`remote_gates.infra_code` / `infra_run`), async (`_Job.feed`) and sync
  (`_dispatch`): received results stand; a received red or interrupted one and every gate that
  never reported get the code. PREEMPTED maps to `preempted` (a requeue disabled or failing
  could still end so); it follows the `node_fail` policy. A job whose gates all reported green
  is not blamed on them (falls back as before). FAILED / CANCELLED / COMPLETED / DEADLINE stay
  fallbacks. The `attempt` frame is now in `FRAME_TYPES`: it drops the earlier attempt's
  results (and, past the cursor, its `gates/<seq>-*` files).
- **Queue**: `mem_boost` is sticky for the campaign (the gate needed it); `oom_retries` is not
  reset by a green run (one boost per campaign, then a human decision) and both reset on resume
  from a `gate_oom` park. The boost is applied to `payload.resources.mem_mb` in the header
  (`suggested_mem_mb`: x 1.5, ceil 256 MB); the node cap is the worker's existing clamp
  (SPEC-0510 `limits()`). The park detail reads "gate X used > N MB (OOM-killed again after a
  1.5x memory re-gate); raise resources.mem in .nw/project.yaml (suggested: M; `nw gate stats`
  shows measured peaks)" (`nw gate stats` is SPEC-0514's; N is the boosted request). The local
  run after a second node failure is `state.gate_local` -> `run_all(local=True,
  local_reason="node_fail")`; `node_fail` joined `gates.LOCAL_REASONS`.

## Alternatives considered

- **Let triage judge OOM.** Costs a run and invites code changes for a config problem.
- **Unbounded memory escalation.** Hides a real leak; one boost, then a human decision.

## Acceptance criteria

- [x] A gate OOM-killed inside a Slurm job is reported `infra: "oom"`; the run is not triaged.
- [x] The first OOM re-gates with 1.5x memory; the second parks `gate_oom` with the suggested
      value. *Queue path by test; on the box the boosted header's request reached `sbatch` as
      `--mem=1536M` and OOMed again (As shipped); the campaign run through the `nw` user is
      pending with the deploy.*
- [x] A job ending `TIMEOUT` follows the gate-timeout path; `NODE_FAIL` re-gates once then falls
      back locally with reason `node_fail`. *TIMEOUT proven on the box; NODE_FAIL by test only
      (failing the box's node on purpose is not on the table).*
- [x] Non-infra failures are unchanged.

## Test plan

- **Automated tests:** `tests/test_gate_infra_outcomes.py`: worker OOM detection with a fake
  cgroup `memory.events`; coordinator mapping of each `slurm_state`; queue decisions (boost,
  park, timeout, node_fail) as pure tests; triage not started for `infra_only`.
- **Manual verification (real binary):** on the box, a throwaway project gate
  `python3 -c "b=bytearray(3<<30)"` with `resources: {mem: 1G}` under Slurm: the campaign shows
  `oom`, re-gates with 1536 MB, parks `gate_oom` on the second OOM; `scontrol show job` (as
  bruno) shows `OUT_OF_MEMORY` for the job-level case. Record in As shipped.
- **Regression guard:** affected tests of `queue.py`, `remote_gates.py`, `worker.py`.

## As shipped

**Code.** `cgroup_oom.py` (new): `oom_kills()` / `classify()`. `gates.py`: `GateResult.infra`,
`INFRA_CODES`, `node_fail` in `LOCAL_REASONS`, `run_all(local_reason=, local_detail=)`, payload
`infra` only when set. `worker.py`: `_run_gates` classifies each gate; `attempt` in
`FRAME_TYPES`; `_Frames.mute_final`. `worker_job.py`: a TERM without the `cancel` file inside a
Slurm job mutes the final frame and waits for Slurm's end (`_await_slurm_end`,
`NW_WORKER_SLURM_END_WAIT_S`). `remote_gates.py`: `SLURM_INFRA`, `infra_code`, `infra_run`,
`_Job._infra`, the `attempt` frame (async and sync), `infra` validated in a gate result, the
`mem_boost` header request. `remote_personas.py`: `attempt` is a persona frame (ignored).
`queue.py`: `infra_only`, `decide_gate_infra`, `suggested_mem_mb`, `_gate_mem_mb`, the
`gate_oom` park reason, `oom_retries` / `node_fail_regates` counters, `mem_boost` /
`gate_local` state (carried by `_apply`), the resume reset. `timeline.py` / `journey.py`:
`INFRA_LABELS` next to a red gate (`oom`, `slurm timeout`, `node failure`, `preempted`).
`docs/product/loop/operate.md`: the `gate_oom` park row. Tests:
`tests/test_gate_infra_outcomes.py` (integration tier, 30 tests); `tests/test_executor_slurm.py`'s
SIGTERM test now writes the `cancel` file first (nw's own cancel).

**Deviations from the accepted Design** (recorded in Design above): own cgroup module instead of
SPEC-0514's path; a Slurm TERM without a `cancel` file is no longer a cancel (needed for TIMEOUT
and requeue to reach the coordinator at all); PREEMPTED is mapped (`preempted`, node-failure
policy) rather than assumed unreachable; the park detail points at `nw gate stats` for measured
peaks and carries the suggested value itself; a run whose gates all reported green before Slurm
ended the job still falls back.

**Real-binary run (box, as bruno, `git archive` of `a46ab2e9` in `/tmp/nw0513-e2e`, temp data and
config dirs with `executor: slurm`; never the `nw` user's install or the live worker config):**
- OOM inside the job: nw job `e2e-oom-89816`, Slurm job 27, `--mem=1G`, gate `pytest` =
  `python3 -c "b=bytearray(3<<30)"`, then gate `lint`. Frames: `gate pytest {ok: false,
  exit_code: -9, infra: "oom"}`, `gate lint ok`, `done ok: false`; `scontrol` `JobState=
  OUT_OF_MEMORY Reason=OutOfMemory` (the kernel killed the gate only; `run-job` finished and
  wrote `result.json`). The coordinator's `_Job` over the same frames: `infra_only` true,
  `[(pytest, False, oom), (lint, True, None)]`.
- The re-gate's request: the header built with `mem_boost` (`remote_gates._header`) asked
  1536 MB; nw job `e2e-oomboost-89987`, Slurm job 34, `SubmitLine ... --mem=1536M`,
  `MinMemoryNode=1.50G`; the 3 GiB gate OOMed again (`infra: "oom"`, `OUT_OF_MEMORY`), the run
  `infra_only` (what parks `gate_oom` on the second OOM).
- Slurm time limit: nw job `e2e-timeout-89816`, Slurm job 28, `--time=1` (`time_margin_s: 0`),
  gate `sleep 600`. Slurm's early TERM (`B:TERM@60`) reached `run-job` 19 s in: gate `slow`
  `{exit_code: -15, interrupted: true}`, no `done`, no `result.json`; Slurm killed it at the
  limit: `JobState=TIMEOUT Reason=TimeLimit RunTime=00:01:20`. The next reader wrote `error
  {stage: slurm, slurm_state: TIMEOUT, signal: 9, infra: true}`; coordinator: `infra_only`,
  `[(slow, False, slurm_timeout)]` (the gate-timeout path).
- Preemption: nw job `e2e-bulk-90246` on `nw-bulk` (`partition: nw-bulk`), Slurm job 54,
  `--mem=16000M`, gate `sleep 40`; a `nw-campaign` blocker (job 55, `--mem=16000M`, no room for
  both) preempted it: `slurm.out` "CANCELLED AT ... DUE TO PREEMPTION", then "SIGTERM from Slurm;
  ending the gates, leaving the end to Slurm"; `Restarts=1`, PENDING `BeginTime`. Attempt 1's
  frames end at `gate bulk {exit_code: -15, interrupted: true}` (no `done`); attempt 2 wrote
  `attempt {attempt: 2}`, `accepted`, `started`, `gate bulk ok`, `done ok`; `result.json`
  `attempt: 2`; `scontrol` `COMPLETED Restarts=1`. The coordinator's `_Job` over all nine
  frames: one green run `[(bulk, True)]` (attempt 1's red result dropped), no protocol fallback.
  The other lane's concurrent Slurm job (53) was not preempted (`Restarts=0`).
- Every job above ended; the temp dirs were removed afterwards.

**Pending (needs the deployed worker build and the box opted in to Slurm: the human's
decision):** the Test plan's manual step through a real campaign (a throwaway project gate with
`resources: {mem: 1G}`: `nw campaign show` shows `oom`, the re-gate at 1536 MB, the `gate_oom`
park). Until then the spec stays `in-progress`.

**Box gate: not run yet.** `nw gate run --project Meta-Tools --worktree <this worktree> --tier
loop --json` on `add38a6e` exited 3 ("box unavailable: no placeable gate worker"): the box worker
was `updating (draining, 1 jobs)` for 50+ minutes (01:03 to 01:32 at least), an update this lane
did not start and may not touch. Local runs: only this lane's new/touched test files
(`tests/test_slurm_lease.py`, `tests/test_gate_infra_outcomes.py`, `tests/test_executor_slurm.py`,
`tests/test_worker_slurm_check.py`), all green; `tools/test_tiers.py --check` and `spec_lint` pass.

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
