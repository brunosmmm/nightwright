---
id: SPEC-0510
title: "Slurm executor on the current box: verify the install, submit worker jobs with sbatch, map Slurm state, cancel with scancel"
status: in-progress
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

The smoke subcommand: `nw worker slurm-check [--json]` (added to the forced-command grammar;
`--json` is its only argument) runs checks 1, 3-5, 7, 8 and 10 as the calling user (on the box
over SSH: the `nw` user) and prints pass/fail per check (`nw.worker.slurm_check.v1`, exit 1 on a
failure); `nw workers check box --slurm` calls it. It is what proves "the nw user can submit".
As shipped, check 7 reads the smallest numeric `memory.max` walking up from the task's cgroup:
Slurm 26.05 names the job cgroup `slurmstepd.scope/<token>/step_batch/user/task_0` (not
`job_<id>`), and the limit sits on the job and `user` levels, not on the task's own.

**Opt-in.** `local` stays the default on every host; only a worker host whose own config sets
`agentic.workers.executor: slurm` submits to Slurm (the coordinator never chooses; a job keeps
the executor that started it, SPEC-0503).

**Worker config** (worker host's `config.yaml`):

```yaml
agentic:
  workers:
    executor: slurm
  slurm:
    conf: /etc/slurm-llnl/slurm.conf   # exported as SLURM_CONF to every Slurm CLI call (null: inherit)
    partition: null                     # null: Slurm's default partition (the box: nw-gatejob)
    bin_dir: null                       # null: PATH
    time_margin_s: 300
    max_time_s: 14400                   # also capped by the partition's MaxTime; clamped + warned
    cache_s: 2                          # one squeue/scontrol answer serves readers this long
    call_timeout_s: 30                  # every Slurm CLI call is bounded
    cancel_grace_s: 60                  # a cancelled job still RUNNING after this is scancel'ed
```

**Submit** (`SlurmExecutor.start`): after SPEC-0503's claim/spool, run

```
sbatch --parsable --job-name=<job_id> --comment='{"campaign":…,"seq":…,"attempt":…,"kind":…,"requester":…}'
  [--partition=<p>] --cpus-per-task=<cpus> --mem=<mem_mb>M --time=<ceil((time_s+margin)/60)>
  [--licenses=jerome_target:1]    # only licenses `scontrol show licenses` defines; others dropped + warning
  --chdir=<jobdir> --output=<jobdir>/slurm.out --open-mode=append
  --requeue --signal=B:TERM@60 --export=ALL
  --wrap 'export NW_JOB_ATTEMPT=$((${SLURM_RESTART_COUNT:-0}+1)); exec <own_nw> worker run-job <jobdir>'
```

`NW_DATA_DIR`, `NW_CONFIG_DIR`, `SLURM_CONF` and `NW_WORKER_FINISH_GC=0` are set in sbatch's own
environment, which `--export=ALL` hands the job (comma-safe, unlike values inside `--export`).
The job's detached finish gc is off (Slurm would kill it with the job's cgroup); `start` starts
that gc instead, outside any job.

**A request that can never be scheduled is clamped, not submitted as is** (decided with the
jerome steward's input: jerome's whole-box wish of 12 cpus / 9G+ can never fit nw-gatejob's
`MaxCPUsPerNode=8` or the node's 10 usable cpus, and would stay PENDING forever with reason
`PartitionConfig`/`Resources`). `limits()` reads `scontrol show partition --json` and
`scontrol show node --json` once per process: cpus are capped at min(partition
`MaxCPUsPerNode`, the largest partition node's `effective_cpus`, i.e. CoreSpecCount taken off),
memory at min(partition `MaxMemPerNode` when set, the node's `RealMemory - MemSpecLimit`), time
at min(`max_time_s`, partition `MaxTime`). Each clamp is a warning on stderr and in `job.json`
`slurm_warnings`; the gates size from the clamped allocation (SPEC-0509). Clamp over refusal:
a smaller allocation still gates (slower) where a refusal would fall back to a local gate that
competes with the box anyway; the warning keeps the request's owner informed. Unreadable limits
clamp nothing.

`<own_nw>` is `worker.own_nw()` (the `nw` user's uv tool install, sudo-free). The Slurm job id
goes to `spawn.json` (`{"slurm_job_id": N}`) and `job.json` (`executor: "slurm"`,
`slurm_job_id`). `sbatch` failure -> `_unclaim` with `stage: "spawn"`, its stderr as error,
`retryable: true` when it says the controller is unreachable. `run-job` takes `job.lock` itself
(`worker_job._take_lock`; inside a Slurm job it waits up to 60 s for it, since submit or a reader
may hold it for a moment, and it never re-execs into its own session: the cgroup tracks it and
Slurm's signals must reach it), and its attempt is `SLURM_RESTART_COUNT + 1` (SPEC-0503).
Inside a Slurm job SIGTERM is a cancel (gates ended, `done` interrupted), like the `cancel` file.

**State** (`SlurmExecutor.state`), cached per call:

| Slurm | nw state | extra |
|---|---|---|
| PENDING (reason R) | `accepted` | any reader (attach, poll, jobs, the sync follower) appends a numbered `slot_wait` frame `{waited_s, reason: R, slurm_job_id}` to `frames.jsonl`: the first at once, then every `NW_WORKER_SLOT_WAIT_EVENT_S` (30 s) or on a new reason; only while it holds `job.lock` (no job process writes frames then) |
| CONFIGURING, RUNNING, COMPLETING, SUSPENDED | `running` | heartbeat age is not used for lost detection |
| COMPLETED / FAILED / CANCELLED with `result.json` | from `result.json` (today) | |
| any end state without `result.json` (OUT_OF_MEMORY, TIMEOUT, NODE_FAIL, PREEMPTED, FAILED, BOOT_FAIL, DEADLINE, CANCELLED, COMPLETED) | `error` | the reader writes one final `error` frame `{stage: "slurm", slurm_state, slurm_job_id, exit_code, signal, infra: true}` and `result.json` (`slurm_state`), once, under `job.lock` taken without waiting (a holder is the job process still finishing: the job reads `running` until it lets go), so `attach` ends normally; SPEC-0515 decides retries. A CANCELLED job with a `cancel` file gets `done` `{ok: false, interrupted: true}` instead |
| unknown to squeue and scontrol (past `MinJobAge`), no result | `lost` | as today |
| a Slurm CLI that cannot answer (controller down, timeout) | `running` (has `proc.json`) / `accepted` | never `lost`: gc would reap a job Slurm still runs |

Lookup order: `squeue --json -j N` -> `scontrol show job N --json` -> `sacct --json -j N`
(tried whenever the first two do not know the job; without slurmdbd it says accounting is
disabled and the job is unknown — simpler than reading `AccountingStorageType`, same result).

**Cancel**: write the `cancel` file (run-job's `_watch` ends gates gracefully). A PENDING job is
`scancel N` (nothing runs yet); a running one gets `scancel --batch --signal=TERM N` (TERM to
`run-job` only, which cancels like the file), not a plain `scancel`: that TERMs every process of
the cgroup at once, and a gate killed before `run-job` sets its cancel flag would be recorded as
a red gate rather than an interrupted one. Backstop: a job still RUNNING `cancel_grace_s` after
its `cancel` file is `scancel N`ed once by the next reader. **Jobs**: per job dir, through
`state` (a finished job never calls Slurm); no bulk `squeue -u` join (not needed at this size).

**What the coordinator sees.** A Slurm job's `started` frame carries `slot_wait_s` = its time in
Slurm's queue (sbatch to `run-job` start), so the gate record's worker wait is the Slurm wait.
The last `slot_wait.reason` is stored live on the remote job record (`wait_reason`) and in the
gate record (`wait_reason`, only when a reason was seen; sync and async paths).

**Sync `gate-job` under Slurm**: read header + bundle exactly as today into a claimed job dir
(same validation: it runs `worker_job.submit` on the header and the rest of stdin), `start` it
as above, then follow `frames.jsonl` to stdout with the `attach` reader; stdin EOF, a closed
stdout or a signal -> `worker_job.cancel`, then follow the interrupted ending for at most
150 s; exit code as today. A persona header or an existing job id is refused as the local
`gate-job` refuses it. The coordinator's sync `started` deadline (`slot_wait_s`, 300 s) still
applies: a job Slurm keeps pending longer is given up by the coordinator (stdin closes, the
job is scancel'ed) and the gate falls back as today.

**Admission**: `host_slot` is a no-op under Slurm. On the coordinator, the box's registry
`slots` is raised to 20 at rollout (`nw workers set box --slots 20`) so jobs queue in Slurm,
where `squeue` shows them, not in nw's slot wait. The queue does not set SPEC-0311's
`gate_exclusive` marker when the campaign's gate pool is a worker whose cached info reports
`executor:slurm` (a timeout re-gate is a normal gate; Slurm guarantees its allocation). As
shipped: the check is on the worker the timed-out run ran on (`GateRun.worker`,
`workers.runs_slurm`).

**Test fixture** `tests/slurm_fake.py`: writes executable fakes `sbatch`, `squeue`, `scontrol`,
`scancel`, `sacct`, `sinfo`, `sstat` into a tmp dir put first on `PATH`, backed by a JSON state
file (`scontrol show partition|node --json` serve the box's recorded output); the
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

- [x] The verification checklist was run on the box and its output is recorded in As shipped.
- [ ] `nw worker slurm-check --json` passes on the box as the `nw` user (submit, cgroup cpus,
      memory.max, OOM enforcement). *Passes as `bruno` from this branch (As shipped); as `nw`
      it needs the deployed build (pending).*
- [ ] With `executor: slurm`, a campaign gate, a persona run and an `nw gate run` job each
      appear in `squeue -u nw` with name = nw job id, the requested cpus/mem, and finish with
      the same frames and gate records as under `local`. *Worker side proven as `bruno` with the
      opt-in config (async gate, sync `gate-job`); the `nw`-user campaign gate / persona /
      `nw gate run` runs need the deployed build and the box opted in (pending).*
- [x] A PENDING job's reason reaches the coordinator as `slot_wait.reason`.
- [x] A job that ends in Slurm without `result.json` yields one `error` frame with
      `stage: "slurm"` and `slurm_state`.
- [ ] `nw gate cancel` / campaign cancel ends the Slurm job (`scancel`), and the gate reports
      interrupted. *`nw worker cancel` (what both call) proven on the real box; the coordinator
      run through the `nw` user is pending with the deploy.*
- [x] Inside the job, `NW_GATE_CPUS` equals `SLURM_CPUS_PER_TASK`.

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

## As shipped

**Code.** `src/nightwright/agentic/executor_slurm.py` (`SlurmExecutor`, `limits`, `lookup`,
`sbatch_argv`, the sync `gate_job`/`follow`, `slurm_check`, `remote_check`), registered as
`slurm` in `executors.py`. `worker_job.py`: `_unclaim` passes `retryable`; `run_job` turns
SIGTERM into a cancel when the executor says `term_cancels`; `main` inside a Slurm job neither
re-execs into its own session nor fails on a briefly held `job.lock` (waits up to 60 s).
`worker.py`: `slurm-check [--json]` in the forced-command grammar. `cli_worker.py`:
`nw worker slurm-check`; `cli_workers.py`: `nw workers check NAME --slurm`. `gates.py`:
`GateRun.wait_reason` / payload `wait_reason`; `remote_gates.py`: the last `slot_wait.reason`
on the job record and the gate record (async and sync); `workers.runs_slurm` +
`queue.py`: no `gate_exclusive` after a timeout on a Slurm worker. Tests:
`tests/slurm_fake.py`, `tests/test_executor_slurm.py`, `tests/test_worker_slurm_check.py`,
fixture `tests/fixtures/slurm/scontrol_show_partition.json` (the box's real output).

**Deviations from the accepted Design** (all recorded in Design above): no `-p nw` (the box
has `nw-campaign/nw-gatejob/nw-persona/nw-bulk`; `partition: null` = Slurm's default,
nw-gatejob; SPEC-0511 maps job classes to partitions); requests clamped to partition/node
limits (steward input); a running job is cancelled with a TERM to the batch step plus a
`scancel` backstop, so a real cancel ends `COMPLETED` with `done` interrupted, not
`CANCELLED` (a pending one is `CANCELLED`); `sacct` is tried whenever the job is unknown;
no bulk `squeue -u` join for `jobs`; the gate record's worker wait is the Slurm queue time;
check 7 walks the cgroup tree (26.05 has no `job_<id>` dir).

**Verification checklist (box, 2026-11-09/10, as bruno):**
1. `sinfo --version` -> `slurm 26.05.5`.
2. `systemctl is-active munge slurmctld slurmd` -> `active` x3; `is-enabled` -> `enabled` x3.
3. `sinfo -N -o '%N %c %m %T'` -> `morais-Desktop 12 31955 idle` (once per partition, 4 partitions).
4. `scontrol show config`: `AuthType=auth/munge`, `SelectType=select/cons_tres`,
   `SelectTypeParameters=CR_CORE_MEMORY`, `ProctrackType=proctrack/cgroup`,
   `TaskPlugin=task/cgroup,task/affinity`, `JobAcctGatherType=jobacct_gather/cgroup`,
   `AccountingStorageType=(null)`, `PriorityType=priority/multifactor`,
   `PreemptType=preempt/partition_prio` (`PreemptMode=REQUEUE`), `MinJobAge=300 sec`,
   `KillWait=30 sec`, `SchedulerParameters=enable_user_top`, `Licenses=jerome_target:3`;
   partitions nw-campaign (tier 4) / nw-gatejob (3, Default, MaxCPUsPerNode=8) / nw-persona (2,
   MaxCPUsPerNode=4) / nw-bulk (1, PreemptMode=REQUEUE), each `MaxTime=04:00:00`.
5. `scontrol show node`: `CfgTRES=cpu=10,mem=31955M,billing=10`, `CoreSpecCount=1
   CPUSpecList=10-11 MemSpecLimit=6000` (cpu=10 usable, not 12: one core kept for the desktop).
6. `/sys/fs/cgroup/cgroup.controllers` -> `cpuset cpu io memory hugetlb pids rdma misc dmem`.
7. job 11 (`-c 2 --mem=1G`): `nproc` 2; cgroup
   `/system.slice/slurmstepd.scope/s0HNM71X1XSR00/step_batch/user/task_0` (`memory.max` max),
   `.../step_batch/user` and the job level `1073741824`; `SLURM_CPUS_PER_TASK=2
   SLURM_MEM_PER_NODE=1024`. Through `slurm-check`: job 19 COMPLETED, output `2 / 1073741824`.
8. job 12 (`--mem=200M`, 400 MiB bytearray): `OUT_OF_MEMORY`, reason `OutOfMemory`, "Detected 1
   oom_kill event in StepId=12.batch". Through `slurm-check`: job 20 `OUT_OF_MEMORY`.
9. `sacct -j 9` -> exit 1 "Slurm accounting storage is disabled" (SPEC-0514 needs slurmdbd).
10. `scontrol show licenses` -> `LicenseName=jerome_target Total=3 Used=0 Free=3`.
Also: `squeue --json -j` of an ended job -> `{"jobs": []}` exit 0; of a purged/unknown id -> exit
1 "Invalid job id specified"; `scontrol show job <unknown> --json` -> empty list, exit 0;
`SLURM_RESTART_COUNT` is unset on a first run (the wrap defaults it to 0).

**Real-binary run of this branch (box, as bruno, `git archive` of the branch in
`/tmp/nw0510-e2e`, temp data dir with `executor: slurm`; never the `nw` user's install or the
live worker config):** `nw worker slurm-check` -> every check ok (jobs 19, 20).
- async submit + attach, request 2 cpus / 1024M: Slurm job 13, `squeue` `13 e2e-run-000902 2 1G
  RUNNING None`; frames accepted, started (`slot_wait_s` 0.926), gate ok, done ok; the gate log
  `NW_GATE_CPUS=2 NW_GATE_MEM_MB=1024 nproc=2`, cgroup `slurmstepd.scope/.../task_0`; time limit
  15 min; `COMPLETED`.
- pending on a license (a blocker job 14 held `jerome_target:3`): job 15 `PENDING Licenses`;
  `poll` printed `slot_wait {waited_s 4.5, reason "Licenses", slurm_job_id 15}`; after the
  blocker's scancel it ran: `started` `slot_wait_s` 5.004, done ok. (A first `slot_wait` with
  reason `None`, before the scheduler's first pass, is no longer written: fixed after the run,
  covered by the fake.)
- cancel of a running job (`sleep 300` gate): job 16, `nw worker cancel` -> `cancel requested`;
  gate exit -15, `done {ok: false, interrupted: true}`; `scontrol` `COMPLETED`.
- sync `gate-job`, stdin held open: job 17, frames accepted/started/gate/done ok, rc 0.
- sync `gate-job`, stdin EOF right after the upload: job 18 scancel'ed while pending,
  `done {interrupted: true, slurm_state: CANCELLED}`, rc 0; `scontrol` `CANCELLED`.

**Pending (needs the deployed worker build; not run in this lane):** `nw workers update box`,
then `nw workers check box --slurm` (the `nw` user's `slurm-check`); set `executor: slurm`
(+ `agentic.slurm.conf`) on the box's worker config; one nightwright campaign gate, one persona
run and one `nw gate run` through the `nw` user with `squeue -u nw -o '%i %j %C %m %T %r'`
during the run; `nw gate cancel` of a running one. Until then the spec stays `in-progress`.

**Box gate:** `GATEJOB-1556`, loop tier on `52a5e8e9` (the code-final commit), worker `box`,
passed (667.6 s). Local runs: only this lane's new/touched test files
(`tests/test_executor_slurm.py`, `tests/test_worker_slurm_check.py`,
`tests/test_rich_output_guard.py`, `tests/test_worker_executor_seam.py`,
`tests/test_worker_ssh_command.py`), green.

**For SPEC-0511..0515.** The `attempt` frame of a requeued job (attempt 2) is still not in the
coordinator's `FRAME_TYPES`: a requeue makes the coordinator fail the job as a protocol error and
fall back (SPEC-0515). Partition choice per job class is SPEC-0511's (`partition` is one value
here). `limits()` and `licenses()` are read once per worker process. `tests/slurm_fake.py`
supports `pending`, `start`/`finish`/`run`, `end`, `requeue`, `forget`, `fail`/`heal`, `auto`
(an sbatch that ends at once with an output) and `fixture` (partition/node JSON).

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
