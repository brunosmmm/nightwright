---
id: SPEC-0518
title: "Shared-box policy: a compute partition and human QOS for other heavy work; nw never preempts humans and is capped"
status: draft
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, slurm, policy, multi-tenant]
depends_on: [SPEC-0516]
---

## Context

Draft: whether the new box is nw-only or shared with other heavy compute (simulations,
SystemC/Renode, builds) is undecided (SPEC-0501). The research's shared branch: add a `compute`
partition on `big`, a `human` QOS (priority 700) that nw never preempts, fair-share between the
`nw` and human accounts, and a cap on all nw QOS (e.g. 70 % of `big`'s memory). Unmanaged heavy
processes outside Slurm would be invisible to the scheduler and oversubscribe the box, so heavy
human work must go through `sbatch` too, or the node must reserve cores/memory for it.

## Goals / Non-goals

**Goals**
- Humans and nw share `big` with predictable arbitration; nw never takes a human's running job.
- nw's governance wrappers stay limited to `--user nw`.

**Non-goals**
- Policing processes outside Slurm (documented, not enforced).

## Decision

If the human declares the box shared: `PartitionName=compute Nodes=big` (same `PriorityTier` as
`nw`), `sacctmgr add qos human Priority=700` (not in any nw QOS's `Preempt` list; may preempt
`nw-bulk`), `GrpTRES=cpu=78,mem=63G` across nw's QOS via a parent account limit
(`sacctmgr modify account nw set GrpTRES=...`), `PriorityWeightFairshare` on, a short guide
`docs/slurm-for-humans.md` (sbatch/srun examples for their workloads). If heavy work will still
run outside Slurm, raise `big`'s `CoreSpecCount`/`MemSpecLimit` to reserve it.

## Design

- Root plan block in `nw workers bootstrap --slurm --role controller --shared`.
- nw wrappers (SPEC-0512) already filter `--user nw`; `nw queue jobs --all` shows human jobs
  read-only for context.

## Alternatives considered

- **One QOS ladder for everyone.** Chosen instead only if the human wants to reprioritize
  human runs against nw directly (research §7 deciding signal).

## Acceptance criteria

- [ ] A human `sbatch -p compute --qos=human` job is never preempted by an nw job.
- [ ] nw's total allocation on `big` never exceeds the account cap.
- [ ] `nw queue jobs` acts only on nw jobs.

## Test plan

- **Automated tests:** plan rendering for `--shared`; wrapper filter tests (SPEC-0512 fixture).
- **Manual verification (real binary):** a human job filling half of `big` plus a burst of nw
  jobs: the human job keeps running, nw stops at the cap (`squeue` reason
  `AssocGrpCpuLimit`/`AssocGrpMemLimit`). Record.
- **Regression guard:** SPEC-0512 tests.

## Rollout / migration

Only on the human's decision that the box is shared. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- Human work outside Slurm defeats admission; mitigated only by node-level reservations.

## Open questions

- Is the box shared? (The human decides; this spec stays draft until then.)
- Cap value (70 % proposed).
