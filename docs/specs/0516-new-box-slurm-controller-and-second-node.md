---
id: SPEC-0516
title: "New box as Slurm controller and big node; the current box joins as a configless slurmd; nw workers bootstrap --slurm"
status: draft
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, slurm, infra, bootstrap]
depends_on: [SPEC-0510, SPEC-0511, SPEC-0354]
---

## Context

Draft until the new box (112 threads, 96 GB) arrives (SPEC-0501). In Phase 1 the current box
(`morais-Desktop`, 12 threads / 31 GB, the human's desktop) is controller, node and submit host
at once, with `auth/munge` and no accounting DB. The target topology (research report): the new
box (`big`) runs `slurmctld`, `slurmdbd` + MariaDB and `slurmd`; `box` runs only `slurmd` in
configless mode (`slurmd --conf-server big`), fills last (`Weight`), keeps `CoreSpecCount=1
MemSpecLimit=6000`. `big` is the single submit host in `workers.json`; the laptop keeps talking
`nw worker` over SSH. `nw workers bootstrap` (SPEC-0354) installs nw on a worker sudo-free; it
does not touch Slurm.

## Goals / Non-goals

**Goals**
- One cluster, two nodes; jobs from `big` land on either node.
- Accounting on (`slurmdbd`), QOS mode of SPEC-0511, `AdminLevel=Operator` for `nw`.
- A repeatable, printed root plan plus an idempotent check, so the human's root steps are
  exact.

**Non-goals**
- nw running root commands (the human runs the plan; nw upgrades stay sudo-free).
- The job spool (SPEC-0517), shared-box policy (SPEC-0518).

## Decision

`nw workers bootstrap --slurm --role controller|node NAME` prints the root plan for that host
(packages pinned to Slurm 26.05.x to match the current box's `26.05.5`; config files; units;
`sacctmgr` lines) and `--check` verifies it as the `nw` user (via `nw worker slurm-check`).
Auth stays `auth/munge` on both nodes (the current box already runs it; one key copied once),
unless the new box's distribution lacks munge, in which case both switch to `auth/slurm` with
one shared `slurm.key`.

## Design

- `slurm.conf` on `big` (sketch): `SlurmctldHost=big`, `SlurmctldParameters=enable_configless`,
  `AccountingStorageType=accounting_storage/slurmdbd`, `TrackWCKey=yes`,
  `SelectType=select/cons_tres`, `SelectTypeParameters=CR_Core_Memory`, `DefMemPerCPU=1024`,
  `PriorityType=priority/multifactor`, `PriorityWeightQOS=100000`, `PriorityWeightAge=100`,
  `PreemptType=preempt/qos`, `PreemptMode=REQUEUE`,
  `SchedulerParameters=enable_user_top`, `Licenses=jerome_target:3`,
  `NodeName=big CPUs=112 RealMemory=<slurmd -C> MemSpecLimit=6000 Weight=1`,
  `NodeName=morais-Desktop CPUs=12 RealMemory=31955 CoreSpecCount=1 MemSpecLimit=6000 Weight=10`,
  `PartitionName=nw Nodes=big,morais-Desktop Default=YES MaxTime=04:00:00`.
- `box`: stop `slurmctld`, set `slurmd` `SLURMD_OPTIONS=--conf-server big`, remove the local
  `slurm.conf` (configless).
- `slurmdbd.conf` + MariaDB on `big`; `sacctmgr` lines of SPEC-0511 QOS mode plus
  `sacctmgr -i modify user nw set AdminLevel=Operator`.
- nw: `workers.json` gains `big` (forced-command key, SPEC-0354) with `executor: slurm` on its
  host config; `box` is no longer a submit target (its registry entry is removed or `off`), but
  its node still runs jobs. Class mapping switches to QOS mode on `big`.
- `nw queue jobs bump` may now raise priority (Operator).

## Alternatives considered

- **Controller stays on the current box.** It is a desktop that may reboot; `big` is the
  always-on server.
- **Backup controller.** Not worth it for two LAN boxes; the local fallback covers an outage.

## Acceptance criteria

- [ ] `sinfo -N` on `big` lists both nodes idle; a job pinned with `-w morais-Desktop` runs there.
- [ ] `nw workers bootstrap --slurm --check` passes on both hosts.
- [ ] `sacct` returns history; QOS ladder in force; `nw queue jobs bump` raises priority.
- [ ] Gates submitted through `big` run on both nodes (after SPEC-0517).

## Test plan

- **Automated tests:** `tests/test_workers_bootstrap_slurm.py`: plan rendering per role (files,
  units, sacctmgr lines), idempotent `--check` parsing with `tests/slurm_fake.py`.
- **Manual verification (real binary):** the human runs the plan on both hosts; then
  `nw workers bootstrap --slurm --check big` and `... morais-Desktop`; one job per node via
  `nw gate run`; `sacct -j` shows both. Record in As shipped.
- **Regression guard:** affected tests of `workers_bootstrap.py`, `cli_workers.py`.

## Rollout / migration

Arrival -> human runs the controller plan on `big`, then the node plan on `box` -> add `big` to
`workers.json` -> switch class mapping to QOS -> retire `box` as a submit target. Rollback: point
`box` back at its own `slurm.conf`. Extends SPEC-0354 (no supersede).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- Version skew: both nodes must run the same Slurm release line (26.05.x); the plan pins it.
- Controller outage stops scheduling for both nodes; the coordinator's local fallback remains
  (SPEC-0505 records `transport`).

## Open questions

- The new box's distribution (decides package source and munge availability).
- Whether the box is shared (SPEC-0518).
