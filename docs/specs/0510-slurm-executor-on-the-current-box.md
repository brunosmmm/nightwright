---
id: SPEC-0510
title: "Slurm executor on the current box: verify the install, submit worker jobs with sbatch, map Slurm state, cancel with scancel"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, slurm, gates, personas]
depends_on: [SPEC-0503, SPEC-0504, SPEC-0509]
---

## Context

SPEC-0501 Phase 1 runs Slurm as the worker executor on the current box before the new box
arrives. Verified by the coordinator on 2026-11-09: `box` = `morais-Desktop`, Arch Linux,
`slurm-llnl 26.05.5.1-1`, `munge 0.5.18`, config dir `/etc/slurm-llnl` (only `*.example`
before setup), units `slurmctld`/`slurmd`/`slurmdbd`/`munge` installed but inactive+disabled,
cgroup v2 (`cpuset cpu io memory`), `slurmd -C`: `CPUs=12 CoresPerSocket=6 ThreadsPerCore=2
RealMemory=31955` (+ a GT 1030, ignored), users `nw` (uid 1001, `Linger=no`) and `slurm` exist,
sudo needs the human's password. The coordinator wrote the human's root setup script
(`setup-slurm-box.sh` in the 2026-11-09 session scratchpad). Its `slurm.conf`:
`ClusterName=nw`, `SlurmctldHost=morais-Desktop`, `AuthType=auth/munge`,
`CredType=cred/munge`, `SchedulerType=sched/backfill`, `SelectType=select/cons_tres`,
`SelectTypeParameters=CR_Core_Memory`, `ProctrackType=proctrack/cgroup`,
`TaskPlugin=task/cgroup,task/affinity`, `JobAcctGatherType=jobacct_gather/cgroup`
(`Frequency=30`), `AccountingStorageType=accounting_storage/none`,
`PriorityType=priority/multifactor`, `PreemptType=preempt/none`, `ReturnToService=2`,
`DefMemPerCPU=1024`, `NodeName=morais-Desktop CPUs=12 ... RealMemory=31955 CoreSpecCount=1
MemSpecLimit=6000`, `PartitionName=nw Nodes=morais-Desktop Default=YES MaxTime=04:00:00`;
`cgroup.conf`: `CgroupPlugin=cgroup/v2` (explicit: `autodetect` refuses the host's hybrid
mount, a PIA VPN `net_cls` v1 hierarchy), `ConstrainCores/RAMSpace/SwapSpace=yes`,
`AllowedSwapSpace=0`; `SLURM_CONF=/etc/slurm-llnl/slurm.conf` in `/etc/environment`. So the
schedulable node is 10 threads (one core kept for the desktop) and ~25.9 GB.

Without `slurmdbd` there is no QOS, no `--wckey` and no `sacct` history; SPEC-0511 and
SPEC-0514 handle that. The coordinator can reach the box both through the `nw` forced-command
key and as `bruno@morais-Desktop` with a normal shell (used for the real-binary checks).

Today's worker paths are in SPEC-0503's Context. The seam is SPEC-0503; requests are SPEC-0504.

## Goals / Non-goals

**Goals**
- Record what the real install is (verification checklist) before any code depends on it.
- With `agentic.workers.executor: slurm`, every async job (`submit`: campaign gates, persona
  runs) and every sync job (`gate-job`: sentinel, cadence, `nw gate run`) runs as a Slurm job
  named by its nw id, sized by SPEC-0504's request, inside the Slurm cgroup.
- Slurm state (incl. the pending reason) is visible through the existing protocol.
- Cancel is `scancel`. The worker-side host slot is skipped (the allocation is the admission).

**Non-goals**
- QOS (SPEC-0511), governance verbs (SPEC-0512), lease removal (SPEC-0513), accounting
  (SPEC-0514), OOM policy (SPEC-0515), more nodes (SPEC-0516).

## Decision

Implement `SlurmExecutor` in `src/nightwright/agentic/executor_slurm.py` behind SPEC-0503's seam.
`submit` claims and spools as today, then `sbatch --parsable` runs `nw worker run-job <jobdir>`.
`gate-job` under Slurm claims and spools the same way, submits, then follows `frames.jsonl` to
its stdout until `done` (scancel on stdin EOF / signal), so the sync callers need no change.
State is merged from `squeue --json`, then `scontrol show job --json`, then (with accounting)
`sacct --json`. The first implementation step is the verification checklist below.

## Design

**Step 1: verification checklist** (run on the box; results recorded verbatim in As shipped):

| # | Command (as) | Expected |
|---|---|---|
| 1 | `sinfo --version` (bruno, nw) | `slurm 26.05.5` |
| 2 | `systemctl is-active munge slurmctld slurmd` (bruno) | `active` x3; `is-enabled` -> `enabled` |
| 3 | `sinfo -N -o '%N %c %m %T'` (bruno) | `morais-Desktop 12 31955 idle` |
| 4 | `scontrol show config \| grep -E 'SelectType\|ProctrackType\|TaskPlugin\|JobAcctGather\|AuthType\|AccountingStorageType\|PriorityType\|MaxTime'` | values of the setup script above |
| 5 | `scontrol show node morais-Desktop \| grep -E 'CfgTRES\|CoreSpec\|MemSpec'` | `cpu=12`, `CoreSpecCount=1`, `MemSpecLimit=6000` |
| 6 | `cat /sys/fs/cgroup/cgroup.controllers` | contains `cpuset cpu io memory` |
| 7 | as nw (through a one-off `nw worker` smoke subcommand added by this spec, see below): `sbatch --parsable -p nw -c 2 --mem=1G --time=2 --wrap 'nproc; cat /sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.max'` | job id; `slurm-<id>.out` shows `2` and `1073741824` |
| 8 | as nw: `--mem=200M --wrap 'python3 -c "b=bytearray(400<<20)"'` | job state `OUT_OF_MEMORY` (`scontrol show job <id>`), proving memory enforcement |
| 9 | `sacct -j <id>` | with `accounting_storage/none`: an error saying accounting is disabled (recorded; SPEC-0514 depends on slurmdbd) |
| 10 | `scontrol show licenses` | `jerome_target` Total=3 once the human adds `Licenses=jerome_target:3` (root, one line in `slurm.conf`, `scontrol reconfigure`) |

The smoke subcommand: `nw worker slurm-check [--json]` (added to the forced-command grammar,
no arguments) runs checks 1, 3-5, 7 and 8 as the `nw` user and prints pass/fail per check;
`nw workers check box --slurm` calls it. It is what proves "the nw user can submit".

**Worker config** (worker host's `config.yaml`):

```yaml
agentic:
  workers:
    executor: slurm
  slurm:
    conf: /etc/slurm-llnl/slurm.conf   # exported as SLURM_CONF to every Slurm CLI call
    partition: nw
    bin_dir: null                       # null: PATH
    time_margin_s: 300
    max_time_s: 14400                   # the partition MaxTime; longer requests are clamped + warned
```

**Submit** (`SlurmExecutor.start`): after SPEC-0503's claim/spool, run

```
sbatch --parsable --job-name=<job_id> --comment='{"campaign":…,"seq":…,"attempt":…,"kind":…,"requester":…}'
  -p nw --cpus-per-task=<cpus> --mem=<mem_mb>M --time=<ceil((time_s+margin)/60)>
  [-L jerome_target:1]            # only licenses `scontrol show licenses` defines; others dropped + warning
  --chdir=<jobdir> --output=<jobdir>/slurm.out --open-mode=append
  --requeue --signal=B:TERM@60 --export=ALL,NW_DATA_DIR=…,NW_CONFIG_DIR=…
  --wrap 'exec <own_nw> worker run-job <jobdir>'
```

`<own_nw>` is `worker.own_nw()` (the `nw` user's uv tool install, sudo-free). The Slurm job id
goes to `spawn.json` (`{"slurm_job_id": N}`) and `job.json` (`executor: "slurm"`,
`slurm_job_id`). `sbatch` failure -> `_unclaim` with `stage: "spawn"`, its stderr as error,
`retryable: true` when it says the controller is unreachable. `run-job` takes `job.lock` itself
(`worker_job._take_lock`), and its attempt is `SLURM_RESTART_COUNT + 1` (SPEC-0503).

**State** (`SlurmExecutor.state`), cached per call:

| Slurm | nw state | extra |
|---|---|---|
| PENDING (reason R) | `accepted` | `pending_reason: R`; attach/poll emit `slot_wait` frames `{waited_s, reason: R}` (an existing frame type; the coordinator already counts it as worker wait) |
| CONFIGURING, RUNNING, COMPLETING, SUSPENDED | `running` | heartbeat age is not used for lost detection |
| COMPLETED / FAILED / CANCELLED with `result.json` | from `result.json` (today) | |
| any end state without `result.json` (OUT_OF_MEMORY, TIMEOUT, NODE_FAIL, PREEMPTED, FAILED, BOOT_FAIL, DEADLINE, CANCELLED) | `error` | the reader writes one final `error` frame `{stage: "slurm", slurm_state, exit_code, infra: true}` into `frames.jsonl` (under `jobs_lock`, once), so `attach` ends normally; SPEC-0515 decides retries |
| unknown to squeue and scontrol (past `MinJobAge`), no result | `lost` | as today |

Lookup order: `squeue --json -j N` -> `scontrol show job N --json` -> `sacct --json -j N` only
when `AccountingStorageType` is not `none`.

**Cancel**: write the `cancel` file (run-job's `_watch` ends gates gracefully) and run
`scancel N` (Slurm sends TERM, then KILL after `KillWait`). **Jobs**: `squeue --json -u nw`
joined with job dirs by `slurm_job_id`.

**Sync `gate-job` under Slurm**: read header + bundle exactly as today into a claimed job dir
(same validation), `start` it as above, then follow `frames.jsonl` to stdout with the
`attach` reader; stdin EOF or a signal -> cancel; exit code as today. The coordinator side
(`remote_gates.run_detached`, `_dispatch`) is unchanged.

**Admission**: `host_slot` is a no-op under Slurm. On the coordinator, the box's registry
`slots` is raised to 20 at rollout (`nw workers set box --slots 20`) so jobs queue in Slurm,
where `squeue` shows them, not in nw's slot wait. The queue does not set SPEC-0311's
`gate_exclusive` marker when the campaign's gate pool is a worker whose cached info reports
`executor:slurm` (a timeout re-gate is a normal gate; Slurm guarantees its allocation).

**Test fixture** `tests/slurm_fake.py`: writes executable fakes `sbatch`, `squeue`, `scontrol`,
`scancel`, `sacct`, `sstat` into a tmp dir put first on `PATH`, backed by a JSON state file; the
test drives transitions (`fake.pending(id, "Resources")`, `fake.run(id)` which executes the
`--wrap` command, `fake.end(id, "OUT_OF_MEMORY")`); it records every argv for assertions. Used
by SPEC-0508 and SPEC-0511..0515.

## Alternatives considered

- **`srun` for the sync `gate-job`.** Ties the job to the SSH session's lifetime and needs a
  different code path for streaming; submit-and-follow reuses the async reader.
- **slurmrestd.** Needs JWT and another daemon; the CLI with `--json` is enough on one host.
- **Waiting for slurmdbd before Phase 1.** Delays the admission win; state works without it
  (`squeue` + `scontrol` within `MinJobAge`).

## Acceptance criteria

- [ ] The verification checklist was run on the box and its output is recorded in As shipped.
- [ ] `nw worker slurm-check --json` passes on the box as the `nw` user (submit, cgroup cpus,
      memory.max, OOM enforcement).
- [ ] With `executor: slurm`, a campaign gate, a persona run and an `nw gate run` job each
      appear in `squeue -u nw` with name = nw job id, the requested cpus/mem, and finish with
      the same frames and gate records as under `local`.
- [ ] A PENDING job's reason reaches the coordinator as `slot_wait.reason`.
- [ ] A job that ends in Slurm without `result.json` yields one `error` frame with
      `stage: "slurm"` and `slurm_state`.
- [ ] `nw gate cancel` / campaign cancel ends the Slurm job (`scancel`), and the gate reports
      interrupted.
- [ ] Inside the job, `NW_GATE_CPUS` equals `SLURM_CPUS_PER_TASK`.

## Test plan

- **Automated tests:** `tests/test_executor_slurm.py` with `tests/slurm_fake.py`: sbatch argv
  (name, comment JSON, cpus, mem, time clamp, licenses dropped when undefined, SLURM_CONF in env);
  state table row by row; final `error` frame written once; cancel calls scancel and writes
  `cancel`; sync gate-job submit-and-follow incl. stdin EOF -> scancel; sbatch failure ->
  `stage: "spawn"`. `tests/test_worker_slurm_check.py` for the smoke subcommand's parser and
  grammar entry.
- **Manual verification (real binary, the human's rule):** after the human ran
  `setup-slurm-box.sh`: (1) run the checklist; (2) `nw workers check box --slurm`; (3) set
  `executor: slurm` on the box, run one nightwright campaign gate and one `nw gate run` of
  nightwright; as `bruno@morais-Desktop` run `squeue -u nw -o '%i %j %C %m %T %r'` during the run
  and `cat /sys/fs/cgroup/system.slice/slurmstepd.scope/job_<id>/memory.max`; (4) `nw gate cancel`
  a running job and confirm `scontrol show job <id>` says CANCELLED. Record outputs in As shipped.
- **Regression guard:** affected tests of `worker_job.py`, `worker.py`, `remote_gates.py`,
  `gate_jobs.py`; the local executor's tests run unchanged.

## Rollout / migration

1. Human runs `setup-slurm-box.sh` (root) and adds `Licenses=jerome_target:3`.
2. Ship; `nw workers update`; run the checklist and `slurm-check`.
3. Set `executor: slurm` on the box and raise its registry slots to 20. jerome cargo gates stay
   safe only after the jerome `cargo.sh` change (SPEC-0509); until then the human may keep
   jerome jobs off the box or accept that their builds escape the cgroup.
4. Rollback: `executor: local` on the box and slots back to 10.

Supersedes nothing at ship time; SPEC-0519/0520 supersede the slot/drain specs later.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped (As shipped: checklist output).
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- `auth/munge` keys and the controller on a desktop that may reboot: `ReturnToService=2` and
  `--requeue` cover a reboot; jobs pending during it simply wait.
- `MaxTime=04:00:00`: a job longer than 4 h is clamped and warned; none today exceeds 2400 s
  per gate, but a sum of gate timeouts could; the clamp logs it.
- jerome's `systemd-run --user` escapes the cgroup until the jerome change (SPEC-0509).

## Open questions

- None.
