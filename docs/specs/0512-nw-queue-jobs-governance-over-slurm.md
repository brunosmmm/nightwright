---
id: SPEC-0512
title: "nw queue jobs: list, hold, release, top, bump, class, requeue and cancel nw's Slurm jobs by nw id"
status: in-progress
owner: user
created: 2026-11-09
updated: 2026-11-10
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
| `nw queue jobs [--json] [--worker W]` | `nw worker slurm-jobs --json`: `squeue --json -u <its user>` joined with job dirs | — |
| `nw queue jobs hold JOB` | `scontrol hold <id>` | owner |
| `nw queue jobs release JOB` | `scontrol release <id>` | owner |
| `nw queue jobs top JOB` | `scontrol top <id>` | `SchedulerParameters=enable_user_top` (human adds; root) |
| `nw queue jobs bump JOB [--down N]` | `scontrol update JobId=<id> Nice=<N>` (down only; N 0..10000, default 1000) | owner; raising needs Operator (Phase 2 with slurmdbd) |
| `nw queue jobs class JOB CLASS` | `scontrol update JobId=<id> Partition=<mapped>` (QOS mode: `QOS=<mapped>`) | pending job |
| `nw queue jobs requeue JOB` | `scontrol requeue <id>` | owner; job has `--requeue` |
| `nw queue jobs cancel JOB` | the existing `nw worker cancel` (SPEC-0510: scancel + cancel file); when nw already finished the job but Slurm still queues it (a requeue), `slurm-ctl cancel` (`scancel <id>` + cancel file) | owner |

- **Id resolution** (as shipped, `queue_jobs.resolve`): the coordinator asks the workers'
  live lists and takes an exact nw id or Slurm id, else the one live job whose nw id starts
  with `<JOB>-` (so a campaign id finds `<cid>-gate-NNN-aN` / `<cid>-persona-...`, and
  `GATEJOB-NNNN` its `GATEJOB-NNNN-gate-000-tT` job; the gate job record only picks the worker,
  since it does not store the worker job id). More than one match is refused as ambiguous
  (name the nw id). The worker (`slurm_govern.resolve`) checks again that the Slurm job is live,
  its `user_name` is the worker's own user (`nw` on the box) and its `name` is the job dir whose
  `job.json` records that Slurm id. Workers asked: `--worker W`, else every SSH worker whose
  cached info says `executor:slurm`.
- **List row**: `nw id · class · state (reason) · cpus/mem · node · waited/ran · slurm id`;
  `--json` schema `nw.queue.jobs.v1`.
- **Events**: each successful verb appends `queue_job_governed {job, verb, by, slurm_id}` (+
  `worker` and the verb's detail: `nice`, `class`/`from_class`, `via`) to the queue event
  stream (`nw queue events`); the event's campaign is set for a campaign job (not for
  `GATEJOB-`/`sentinel-`). The quiet conductor holds it (`quiet.RULES`, the nw-conduct skill's
  *Always held* list), so it reaches the conductor in its digest without a wake.
- **Forced-command grammar** (`worker.check_ssh_command`): `slurm-jobs [--json]`;
  `slurm-ctl (hold|release|top|requeue|cancel) <job>`; `slurm-ctl nice <job> <0..10000>`;
  `slurm-ctl class <job> (campaign|gatejob|persona|bulk)`. Anything else is refused.
  `slurm-ctl` prints one `nw.worker.slurm_ctl.v1` line (exit 1 refused, 3 Slurm refused),
  `slurm-jobs --json` one `nw.worker.slurm_jobs.v1` line.
- A worker whose executor is not `slurm` answers `slurm-jobs`/`slurm-ctl` with `ok: false,
  error: "executor is local"` (exit 1). `slurm-check` still runs there (it is how a box is
  checked before opting in).
- `class` only for a pending job; it records the new class in `job.json`. Seen on the box
  (26.05): a user's `scontrol update JobId=N Partition=...` is refused "Resource temporarily
  unavailable" for up to ~20 s after the job was submitted or last updated, then accepted;
  the worker retries any `scontrol` verb on that answer for up to 40 s (inside the
  coordinator's 60 s call).

## Alternatives considered

- **Run `scontrol` over a normal SSH shell.** The `nw` key is forced-command only; widening it
  would weaken the worker's security model.
- **Our own priority file the worker consults.** That is rebuilding the queue, which the human
  rejected.

## Acceptance criteria

- [x] `nw queue jobs` lists the box's nw Slurm jobs with pending reasons. *(Real box, through
      the forced-command grammar, as bruno; through the `nw` user: pending the deploy.)*
- [x] hold/release/top/bump/class/requeue/cancel act on the right Slurm job when given an nw id,
      a campaign id, a GATEJOB id or a Slurm id, and refuse a non-nw job. *(`top` is accepted
      on the right job, but on the box it reorders nothing yet: see As shipped.)*
- [x] Each verb appends `queue_job_governed`.
- [x] The forced-command grammar refuses any verb or argument outside the table.

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

## As shipped

**Code.** Worker: `slurm_govern.py` (`listing`, `rows`, `resolve`, `control`, the EAGAIN
retry), `nw worker slurm-jobs` / `slurm-ctl` in `cli_worker.py`, the grammar
(`worker._slurm_ctl_args`, `SSH_SUBCOMMANDS`). Coordinator: `queue_jobs.py` (`listing`,
`match`, `resolve`, `govern`, the cancel paths: `nw worker cancel`; `slurm-ctl cancel` for a
job nw finished that Slurm requeued; a `GATEJOB-` whose worker refuses the cancel is stopped
through its process, as `nw gate cancel`), `cli_queue_jobs.py` (`nw queue jobs ...`),
`events.EVENT_TYPES["queue_job_governed"]`, `quiet.RULES` hold, the nw-conduct skill's held
list. Tests: `tests/test_queue_jobs_governance.py` (new; worker list/argv per verb/refusals/
local executor/EAGAIN retry/worker CLI, the grammar table both ways, and the coordinator's
resolution table, events, cancel paths and CLI with its SSH calls run in-process through
`check_ssh_command`); `tests/slurm_fake.py` gained `squeue -u`, `scontrol
hold|release|top|requeue|update`, `deny(verb, err, times)`, `as_user`. The grammar tests live
in the new module rather than `test_worker_ssh_command.py` (less overlap with other lanes).

**Real-binary run (box, 2026-11-10, as bruno):** this branch's tree in `/tmp/nw0511-e2e` with a
temp worker data dir (`executor: slurm`); the coordinator was this worktree with a temp
registry whose worker `e2e` is `ssh morais-Desktop` running a wrapper that sets
`SSH_ORIGINAL_COMMAND` and execs `nw worker ssh-command`, so every call went through the
forced-command grammar (a `slurm-ctl hold 1 2` was refused: "do not match the `slurm-ctl`
grammar"). Jobs were real nw jobs (`nw worker submit`, bundles, `sleep` gates):
- `nw queue jobs` listed B1 `bulk PENDING (BeginTime) 10/512M slurm 30`, G1 `gatejob RUNNING`,
  G2 `gatejob PENDING (Resources) slurm 32`, C1 `campaign PENDING (Resources) slurm 33`.
- `hold E2EG-gate-002-a1` -> `squeue` `32 PENDING JobHeldUser` (priority 0); `release E2EG`
  -> refused as ambiguous (G1, G2); `bump 32 --down 50` -> `NICE 50`; `release 32` ok.
- `class 32 bulk` -> "Resource temporarily unavailable" (seen repeatedly for ~20 s on probe
  jobs, then accepted; the retry was added after this); `class E2EG-gate-001-a1 bulk` on the
  running G1 -> refused "only a pending job changes class". After the retry: `class E2EZ
  campaign` on a pending bulk job (57) -> partition nw-campaign, started at once and requeued
  the running bulk job 56.
- `top E2ET-gate-002-a1` (52, the later of two pending nw-bulk jobs) -> accepted, but job 51
  started first: with every priority weight 0 all jobs have priority 1, and `scontrol top`
  has nothing to reorder. A visible `top` needs a non-zero priority factor on the box (e.g.
  `PriorityWeightAge`, root: the human's call; or the Phase 2 QOS ladder).
- `requeue E2EB` (running 30) -> back to `PENDING BeginTime`. Its run got TERM and ended
  `done` interrupted (SPEC-0510's TERM-as-cancel) while Slurm queued it again; `cancel 30` then
  went through `slurm-ctl cancel` -> `CANCELLED` (the first cancel, before that path existed,
  was a no-op "requested" on the done job: the reason for it).
- `nw queue events --type queue_job_governed --text` showed one event per verb (hold, bump,
  release, top x2, requeue, cancel x3, class), `by=bruno`. All jobs ended; the temp dir was
  removed.

**Pending:** the same through the `nw` user (deployed worker + box opted into `executor:
slurm`, the human's call); a visible `top` (needs a priority weight on the box). Until then
`in-progress`.

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
