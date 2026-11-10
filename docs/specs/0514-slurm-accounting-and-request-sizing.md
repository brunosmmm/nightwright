---
id: SPEC-0514
title: "Slurm job accounting into gate records, and nw gate stats suggests per-gate requests from measured peak memory and CPU"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, gates, slurm, measurement, sizing]
depends_on: [SPEC-0504, SPEC-0510]
---

## Context

SPEC-0504 records requested resources and a `wait4` approximation of use (largest single
process RSS, `rss_scope: max_process`). Inside a Slurm job the cgroup knows the truth: the job
cgroup's `memory.peak` and `cpu.stat usage_usec` cover every process of the job (pytest-xdist
workers included). Slurm's own accounting (`sacct -o JobID,State,Elapsed,TotalCPU,MaxRSS,ReqTRES,
NodeList`) needs `slurmdbd`; the current box starts with `accounting_storage/none`, and slurmdbd
+ MariaDB is a later root step of the human (SPEC-0501). The SPEC-0501 defaults (3 cpus / 3G for
`pytest-affected`, 5 / 5G full `pytest`, 1 / 1G small gates, 6 / 8G jerome cargo, 1 / 2G
persona) are estimates; the research proposed re-deriving them as p95 MaxRSS x 1.25 and cpus
from TotalCPU/Elapsed. The Phase 1 exit criterion needs a measured MaxRSS table.

## Goals / Non-goals

**Goals**
- Per-gate measured CPU seconds and peak memory of the whole job cgroup in every gate record of
  a Slurm job, without needing slurmdbd.
- With slurmdbd: per-job `sacct` fields merged into the records, `--wckey=<project>` on submit.
- `nw gate stats` shows p50/p95 per (project, gate) and a suggested request, and flags drift.

**Non-goals**
- Auto-editing project yaml (the human edits it; SPEC-0501 invariant 4).

## Decision

`run-job` reads its own job cgroup around each gate: it resets `memory.peak` (cgroup v2 supports
a per-fd reset by writing to it; kernel 7.2 on the box) and reads `cpu.stat usage_usec` before
and after, writing `resources.used = {cpu_s, max_rss_mb, wall_s, rss_scope: "job_cgroup"}`.
When `AccountingStorageType` is not `none`, the coordinator's poll after `done` merges
`sacct --json -j <id>` (`Elapsed`, `TotalCPU`, `MaxRSS`, `State`, `ExitCode`, `NodeList`) into a
job-level `slurm` block, and submits carry `--wckey=<project>`. A new `nw gate stats` reads the
gate records.

## Design

- **Cgroup path**: `/sys/fs/cgroup` + the `0::` line of `/proc/self/cgroup` of the run-job
  process, walked up to the `job_<SLURM_JOB_ID>` directory. Missing files (old kernel, local
  executor) -> keep SPEC-0504's `wait4` numbers.
- **Per-gate measurement** in `worker._run_gates` (the gate loop): before a gate, open
  `memory.peak` and write `0` to reset, read `usage_usec`; after, read both. Concurrent gates do
  not exist inside one job, so the numbers are the gate's.
- **sacct merge** (coordinator, `remote_gates` on `done`, best effort, 1 retry after 30 s since
  sacct lags job end): `nw worker poll` gains an optional `slurm` block that the worker fills by
  running `sacct --json -j <id>` (the coordinator never runs Slurm commands itself). Stored at
  `gates/<seq>-*.json.resources.slurm` and in gate-job rows.
- **`nw gate stats [--project P] [--since 14d] [--json]`**: groups gate records by (project,
  gate name) over the window; for each: n, wall p50/p95, `cpu_s/wall_s` p50/p95 (effective
  cpus), `max_rss_mb` p50/p95, current request, suggested request = `cpus: ceil(p95
  effective cpus)`, `mem: ceil(p95 max_rss_mb x 1.25 / 256) x 256 MB`; marks `over` when the
  request exceeds the suggestion by > 50 % and `under` when p95 exceeds the request (OOM risk).
  It also prints the OUT_OF_MEMORY count per gate (SPEC-0515).
- This command also closes the research's missing-CLI gap ("no nw command exports per-gate
  timing across campaign and standalone records"): `--json` rows carry start, end, worker,
  test count and resources.

## Alternatives considered

- **`sstat` polling during the run.** Samples every 30 s (`JobAcctGatherFrequency=30`) and
  misses short peaks; `memory.peak` is exact.
- **Wait for slurmdbd before measuring.** Leaves Phase 1 without the table it must produce.

## Acceptance criteria

- [ ] A gate run in a Slurm job records `resources.used.rss_scope = "job_cgroup"` with
      `max_rss_mb` >= the sum of its xdist workers' RSS (spot-checked).
- [ ] With accounting on, gate records carry a `slurm` block with `MaxRSS`, `TotalCPU`, `State`,
      `NodeList`; submits carry `--wckey`.
- [ ] `nw gate stats` prints p50/p95 and a suggested request per gate, and `over`/`under` flags.
- [ ] Without accounting, nothing calls `sacct`.

## Test plan

- **Automated tests:** `tests/test_gate_cgroup_measure.py` with a fake cgroup dir (files
  `memory.peak`, `cpu.stat`) verifying reset/read order and the fallback; `tests/test_gate_stats.py`
  on synthetic records (percentiles, suggestion rounding, flags, `--json`); sacct merge with
  `tests/slurm_fake.py` in both accounting modes.
- **Manual verification (real binary):** on the box under Slurm, one nightwright campaign gate:
  `jq .resources` on its record shows `job_cgroup` numbers; compare `max_rss_mb` with
  `sstat -j <id> -o MaxRSS` taken as bruno during the run. After a week: `nw gate stats --since
  7d` is the Phase 1 MaxRSS table, pasted into SPEC-0501's As shipped. Once the human installs
  slurmdbd: one `sacct -j <id>` merge checked against the record.
- **Regression guard:** affected tests of `worker.py`, `remote_gates.py`, `gate_jobs.py`.

## Rollout / migration

Ships after SPEC-0510. The sacct half switches on by itself when `scontrol show config` reports
an accounting storage other than `none`. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- Writing `memory.peak` needs the job to own its cgroup files; slurmstepd delegates the job
  cgroup to the user on cgroup v2. If the write is refused, record per-job peak only
  (`rss_scope: "job_cgroup_cumulative"`) and say so in the stats.

## Open questions

- None.
