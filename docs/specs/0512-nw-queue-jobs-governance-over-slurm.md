---
id: SPEC-0512
title: "nw queue jobs: list, hold, release, top, bump, class, requeue and cancel nw's Slurm jobs by nw id"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, queue, slurm, governance, cli]
depends_on: [SPEC-0510, SPEC-0511]
---

## Context

The human: "I want to be able to govern jobs and reprioritize". Slurm has the verbs
(`scontrol hold|release|top|requeue|update`, `scancel`); the human can always run them on the
box, but they take Slurm ids and the box shell. nw's queue, conductor and TUI speak nw ids
(campaign ids, `GATEJOB-NNNN`, `<campaign>-gate-NNN-aN`, `<campaign>-run-NNN`). Permissions
(`scontrol(1)`): a job's owner may hold, release, requeue and cancel it and lower its priority;
`scontrol top` for a non-admin needs `SchedulerParameters=enable_user_top`; raising `Priority=`
or a negative `Nice=` needs an operator (`AdminLevel=Operator`, which needs slurmdbd). Moving a
job to another partition (`scontrol update JobId=N Partition=…`) is allowed to the owner while
it is pending.

## Goals / Non-goals

**Goals**
- `nw queue jobs` shows every nw Slurm job (pending reason, class, cpus/mem, node, age) and acts
  on one by nw id or Slurm id.
- Each action is recorded as an event the conductor sees.

**Non-goals**
- Acting on non-nw users' jobs (invariant 6 of SPEC-0501). TUI rendering (later Phase 2 item).

## Decision

Add a worker subcommand `nw worker slurm-jobs` (list) and `nw worker slurm-ctl <verb> <job>`
(forced-command grammar entries with a closed verb set), and a coordinator command group
`nw queue jobs [list|hold|release|top|bump|class|requeue|cancel]` that calls them over the
existing SSH. Every verb is filtered to jobs whose user is `nw` and whose name is a known nw job.

## Design

| nw command | worker runs (as nw) | needs |
|---|---|---|
| `nw queue jobs [--json] [--worker W]` | `squeue --json -u nw` joined with job dirs | — |
| `nw queue jobs hold JOB` | `scontrol hold <id>` | owner |
| `nw queue jobs release JOB` | `scontrol release <id>` | owner |
| `nw queue jobs top JOB` | `scontrol top <id>` | `SchedulerParameters=enable_user_top` (human adds; root) |
| `nw queue jobs bump JOB [--down N]` | `scontrol update JobId=<id> Nice=<N>` (down only for now) | owner; raising needs Operator (Phase 2 with slurmdbd) |
| `nw queue jobs class JOB CLASS` | `scontrol update JobId=<id> Partition=<mapped>` (QOS mode: `QOS=<mapped>`) | pending job |
| `nw queue jobs requeue JOB` | `scontrol requeue <id>` | owner; job has `--requeue` |
| `nw queue jobs cancel JOB` | the existing `nw worker cancel` (SPEC-0510: scancel + cancel file) | owner |

- **Id resolution**: `JOB` may be an nw job id, a campaign id (-> its live gate/persona job), a
  `GATEJOB-NNNN` (-> its worker job id from the gate job record) or a numeric Slurm id; the
  worker validates that the Slurm job's `user_name == nw` and `name` matches a job dir.
- **List row**: `nw id · class · state (reason) · cpus/mem · node · waited/ran · slurm id`;
  `--json` schema `nw.queue.jobs.v1`.
- **Events**: each successful verb appends `queue_job_governed {job, verb, by, slurm_id}` to the
  queue event stream (`nw queue events`), so the conductor sees human reprioritization.
- **Forced-command grammar** (`worker.check_ssh_command`): `slurm-jobs [--json]`;
  `slurm-ctl (hold|release|top|requeue) <job>`; `slurm-ctl nice <job> <0..10000>`;
  `slurm-ctl class <job> (campaign|gatejob|persona|bulk)`. Anything else is refused.
- A `local` executor worker answers `slurm-*` with `error: executor is local`.

## Alternatives considered

- **Run `scontrol` over a normal SSH shell.** The `nw` key is forced-command only; widening it
  would weaken the worker's security model.
- **Our own priority file the worker consults.** That is rebuilding the queue, which the human
  rejected.

## Acceptance criteria

- [ ] `nw queue jobs` lists the box's nw Slurm jobs with pending reasons.
- [ ] hold/release/top/bump/class/requeue/cancel act on the right Slurm job when given an nw id,
      a campaign id, a GATEJOB id or a Slurm id, and refuse a non-nw job.
- [ ] Each verb appends `queue_job_governed`.
- [ ] The forced-command grammar refuses any verb or argument outside the table.

## Test plan

- **Automated tests:** `tests/test_queue_jobs_governance.py` with `tests/slurm_fake.py`: argv per
  verb, id resolution table, non-nw refusal, event append; grammar tests in
  `tests/test_worker_ssh_command.py`.
- **Manual verification (real binary):** on the box with two pending nw jobs (fill the node
  first): `nw queue jobs` shows both with reason `Resources`/`Priority`; `hold` one ->
  `squeue` (as bruno) shows `JobHeldUser`; `release`; `top` the later one (after the human adds
  `enable_user_top`) -> it starts first; `class` a pending job to `bulk` -> partition changes;
  `requeue` a running bulk job. Record outputs.
- **Regression guard:** affected tests of `worker.py`, the queue CLI.

## Rollout / migration

Human adds `SchedulerParameters=enable_user_top` (root, `scontrol reconfigure`). Deploy worker
then coordinator. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- `scontrol top` reorders only within the user's own jobs in the same partition; documented in
  the command help.

## Open questions

- None.
