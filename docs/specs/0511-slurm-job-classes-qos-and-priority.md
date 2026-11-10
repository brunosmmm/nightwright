---
id: SPEC-0511
title: "Slurm job classes: campaign > gatejob > persona > bulk, as class partitions now and a QOS ladder once slurmdbd runs"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, slurm, priority]
depends_on: [SPEC-0510]
---

## Context

nw orders work today with its own mechanisms: campaigns win over standalone gate jobs
(`gate_jobs.campaign_waiting`, SPEC-0500), FIFO tokens per requester and a box share cap
(SPEC-0500), persona slots per worker (`remote_personas.acquire_slot`), heavy tiers on a cadence
(SPEC-0489..0493). The human wants Slurm to own priorities and to reprioritize jobs. The
research proposed a QOS ladder (`nw-campaign` 1000 > `nw-gatejob` 500 > `nw-persona` 300 >
`nw-bulk` 100, bulk REQUEUE-preemptible by campaign; `GrpTRES` caps for gatejob and persona).

QOS needs the accounting database (`slurmdbd`). The current box starts with
`AccountingStorageType=accounting_storage/none` (SPEC-0510 Context), and `slurmdbd` + MariaDB
is a later root step of the human. Partition `PriorityTier` and `preempt/partition_prio` work
without a database.

## Goals / Non-goals

**Goals**
- Every nw Slurm job carries a class; Slurm orders and caps classes.
- The class -> Slurm mapping is configuration, so moving from partitions to QOS is a config
  change, not code.

**Non-goals**
- Human-tenant policy (SPEC-0518). Governance verbs (SPEC-0512).

## Decision

The coordinator stamps `payload.job_class` in every job header; the worker maps class ->
`{partition, qos}` from `agentic.slurm.classes`. Phase 1 default: class partitions on the one
node, ordered by `PriorityTier`, bulk preemptible by REQUEUE. When `slurmdbd` runs (Phase 2 at
the latest), the mapping switches to one `nw` partition + the QOS ladder.

## Design

**Class assignment** (coordinator; `remote_gates._header`, `remote_personas._header`):

| class | jobs |
|---|---|
| `campaign` | campaign loop and close gates (SPEC-0338) |
| `gatejob` | `nw gate run` standalone jobs (SPEC-0381), cadence jobs (SPEC-0491/0493) except heavy tiers |
| `persona` | remote persona runs (SPEC-0344) |
| `bulk` | sentinel / full-suite runs (SPEC-0379), heavy-tier cadence jobs (SPEC-0489) |

A header without `job_class` (old coordinator) is `gatejob`.

**Worker mapping** (`executor_slurm.py`), defaults:

```yaml
agentic:
  slurm:
    classes:                       # Phase 1: no slurmdbd
      campaign: {partition: nw-campaign}
      gatejob:  {partition: nw-gatejob}
      persona:  {partition: nw-persona}
      bulk:     {partition: nw-bulk}
    # Phase 2 / once slurmdbd runs:
    # classes: {campaign: {partition: nw, qos: nw-campaign}, gatejob: {partition: nw, qos: nw-gatejob}, ...}
```

`sbatch` gets `-p <partition>` and, when set, `--qos=<qos>`. A partition or QOS that Slurm
rejects at submit falls back once to `-p nw` without `--qos`, with a `warning` frame, so a config
mistake never stops gating.

**Root config for the human (Phase 1, appended to `/etc/slurm-llnl/slurm.conf`, then
`scontrol reconfigure`)**:

```
PreemptType=preempt/partition_prio
PreemptMode=REQUEUE
PartitionName=nw-campaign Nodes=morais-Desktop PriorityTier=4 MaxTime=04:00:00 PreemptMode=OFF State=UP
PartitionName=nw-gatejob  Nodes=morais-Desktop PriorityTier=3 MaxTime=04:00:00 PreemptMode=OFF MaxCPUsPerNode=8 State=UP
PartitionName=nw-persona  Nodes=morais-Desktop PriorityTier=2 MaxTime=04:00:00 PreemptMode=OFF MaxCPUsPerNode=4 State=UP
PartitionName=nw-bulk     Nodes=morais-Desktop PriorityTier=1 MaxTime=04:00:00 PreemptMode=REQUEUE State=UP
```

(`nw` stays the default partition.) Inside a tier, order is submit time (FIFO), which is
SPEC-0500's first-come-first-served.

**QOS mode (once slurmdbd runs)**, the research ladder, created by the human with `sacctmgr`:

```
sacctmgr -i add qos nw-campaign Priority=1000 Preempt=nw-bulk PreemptMode=requeue MaxTRESPerUser=cpu=60,mem=60G
sacctmgr -i add qos nw-gatejob  Priority=500  GrpTRES=cpu=40,mem=40G
sacctmgr -i add qos nw-persona  Priority=300  GrpTRES=cpu=8,mem=16G
sacctmgr -i add qos nw-bulk     Priority=100
sacctmgr -i add account nw ; sacctmgr -i add user nw Account=nw QOS=nw-campaign,nw-gatejob,nw-persona,nw-bulk DefaultQOS=nw-gatejob
```

with `PriorityWeightQOS=100000`, `PriorityWeightAge=100`, `PreemptType=preempt/qos` (limits are
for the 112-thread box; on the 12-thread box they are halved by the human). SPEC-0516 owns the
switch.

`nw worker slurm-check` (SPEC-0510) reports which partitions/QOS of the mapping exist.

## Alternatives considered

- **`--nice` offsets without a database.** With all priority weights at 0 every job's priority
  is 1 and positive nice cannot order them; class partitions order strictly with no weights.
- **Waiting for slurmdbd.** Leaves Phase 1 without priorities, which is the human's main ask.

## Acceptance criteria

- [ ] Every remote job header carries `job_class` per the table.
- [ ] The worker submits with the mapped partition (and QOS when configured); a rejected
      mapping falls back to `-p nw` with a warning frame.
- [ ] On the box, with a campaign gate and a gatejob both pending for cpus, the campaign gate
      starts first; a `bulk` job running when a campaign gate needs its cpus is requeued.

## Test plan

- **Automated tests:** `tests/test_slurm_job_classes.py`: class assignment per caller (campaign
  gate, gate run, cadence heavy tier, sentinel, persona, old header); mapping -> sbatch argv for
  partition and QOS modes; rejection fallback with the fake shim.
- **Manual verification (real binary):** on the box after the human adds the partitions: submit
  (via `nw gate run` and a campaign gate) enough jobs to fill the 10 schedulable cpus with a
  `bulk` job running; `squeue -u nw -o '%i %j %P %T %r %Q'` as bruno shows the campaign job
  started first and the bulk job `REQUEUED`/`PENDING (BeginTime)`. Record in As shipped.
- **Regression guard:** affected tests of `remote_gates.py`, `remote_personas.py`,
  `executor_slurm.py`.

## Rollout / migration

Human adds the partitions; deploy worker, then coordinator (an old coordinator yields `gatejob`).
Supersedes nothing now; SPEC-0519 supersedes SPEC-0500's ordering when nw's tokens go.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- A REQUEUE'd bulk job restarts from scratch (SPEC-0503 restart safety); sentinel runs are
  idempotent re-runs of a tree, so only time is lost.
- Partition caps (`MaxCPUsPerNode`) are coarser than `GrpTRES`; acceptable on one node.

## Open questions

- None.
