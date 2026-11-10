---
id: SPEC-0520
title: "Retire nw's placement, worker cooldown, cross-worker retry, drain internals, lease and lost-job kill paths, and the sync gate-job path"
status: draft
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, slurm, cleanup]
depends_on: [SPEC-0516, SPEC-0519, SPEC-0513, SPEC-0515]
---

## Context

Draft until the two-node cluster runs (SPEC-0516) and nw's admission is gone (SPEC-0519). With
one submit host and Slurm placing jobs, nw's own placement and recovery code is redundant:
`remote_gates.py` `pools_for/_affine_first/retry_pools/set_cooldown` (~200 lines, SPEC-0338
cooldown, SPEC-0342 cross-worker retry, SPEC-0374 no-local-overflow), `workers.py` cooldown /
`placeable` (~100) and drain (~150, `finish_drains`, `draining_jobs`; SPEC-0343, SPEC-0386's
`--when-idle` drain), `worker_job.py` lease-cancel, heartbeat, lost detection and reap/kill paths
(~300 of 1045; SPEC-0340 lost semantics), and the synchronous `nw worker gate-job` path used by
`remote_gates.run_detached`. Slurm equivalents: node selection, `scontrol update NodeName=…
State=DRAIN|RESUME`, `--requeue`, cgroup process ownership, `sacct`/`scontrol` state.

## Goals / Non-goals

**Goals**
- One remote path: async submit + attach to the Slurm submit host for every caller.
- `nw workers drain NAME` maps to `scontrol update NodeName=NAME State=DRAIN Reason="nw drain"`;
  `nw workers update --when-idle` waits for the node's `AllocCPUs == 0` in Slurm.

**Non-goals**
- Removing the coordinator-side watchdog/deadline (kept, SPEC-0513) or the local fallback.

## Decision

At ship this spec supersedes SPEC-0342 (lost-job retry across workers), SPEC-0374 (no local
overflow), the drain parts of SPEC-0343 and SPEC-0386, the cooldown part of SPEC-0338 and the
lease/lost parts of SPEC-0340 (each superseded spec gets `superseded_by: SPEC-0520`; specs only
partly replaced get a successor note and stay `done`, per SPEC-0000's amend-by-supersede rule,
with this spec stating which sections it replaces).

## Design

- `run_detached` (sentinel, gate jobs) moves to `_submit` + `_attach`; `nw worker gate-job`
  becomes a thin alias kept one release for old coordinators, then removed.
- Delete lease renewal on Slurm workers, `reap`, `reap_lost`, `_kill_group`, `_heartbeat`,
  `_watch`'s lease branch; `jobs`/`state` come from the executor only.
- Drain/resume via `scontrol` through `nw worker slurm-ctl drain|resume <node>` (grammar
  addition; nw needs Operator, SPEC-0516).

## Alternatives considered

- **Keep cooldown as a second opinion.** Two health models disagree; a bad node is drained in
  Slurm where the human sees it.

## Acceptance criteria

- [ ] No call site of the deleted functions remains; all remote gate callers use async submit.
- [ ] `nw workers drain/resume` change the Slurm node state and back.
- [ ] Superseded specs marked; ledger regenerated.

## Test plan

- **Automated tests:** removed tests deleted; new tests for drain mapping and for
  `run_detached` over async submit with `tests/slurm_fake.py`.
- **Manual verification (real binary):** `nw workers drain morais-Desktop` -> `sinfo` shows
  `drain`; jobs go to `big`; `resume` restores it. A sentinel run attaches and survives a
  coordinator restart.
- **Regression guard:** affected tests of `remote_gates.py`, `workers.py`, `worker_job.py`.

## Rollout / migration

After SPEC-0519 and two-node operation for 2-4 weeks. Supersedes as listed in Decision at ship.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- Large deletion: done in small commits per area, each with its affected tests.

## Open questions

- Whether any non-Slurm remote worker should remain supported (today none is planned).
