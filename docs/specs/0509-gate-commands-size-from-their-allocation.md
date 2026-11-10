---
id: SPEC-0509
title: "Gate commands size their parallelism from the job's allocation (NW_GATE_CPUS / NW_GATE_MEM_MB), with a config warning for nproc and -n auto"
status: in-progress
owner: user
created: 2026-11-09
updated: 2026-11-10
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, gates, slurm, config]
depends_on: [SPEC-0504]
---

## Context

Inside a Slurm job the cgroup grants N cpus and M MB, but tools size themselves from the whole
machine: `pytest -n auto`, `nproc`, `/proc/meminfo`. Over-sizing inside a cgroup does not
oversubscribe the box, but it thrashes the job (12 xdist workers on 3 cpus) and inflates memory
toward an OOM kill. jerome's `tools/gate/cargo.sh` (read 2026-11-09) sizes `-j min(nproc/4, 6)`
and `MemoryMax` from total RAM, wraps builds in `systemd-run --user` (a user-manager scope
**outside** the Slurm job's cgroup, so the build escapes the limit and the accounting; with
`Linger=no` for the `nw` user on the current box there is no user manager inside a Slurm job
at all, and the script falls back to `ulimit -v`, which breaks rustc's large virtual
reservations), and serializes on 3 cargo target dirs with an in-job `flock`
(`JEROME_GATE_SLOTS`), a hidden queue the research counted as a cause of timeouts of the same
order as load.

## Goals / Non-goals

**Goals**
- Every gate process, local or remote, sees `NW_GATE_CPUS` and `NW_GATE_MEM_MB` = its job's
  request (SPEC-0504), equal to the granted cpus / the job's memory under Slurm.
- nightwright's own gates size from them.
- Project config validation warns about machine-sized parallelism.
- The jerome changes are specified for export to the jerome repo.

**Non-goals**
- Editing jerome here (another repo; outbound work, see Design).

## Decision

Export the two variables from `gates.gate_env` (local) and the worker's gate environment
(remote); change nightwright's pytest gates to `-n "$(( ${NW_GATE_CPUS:-3} - 1 ))"` floored at 1;
add a non-fatal config warning for `-n auto`, `nproc`, `$(getconf _NPROCESSORS_ONLN)` and
`os.cpu_count` in a gate `cmd`; record the jerome requirements as an outbound item.

## Design

- `gates.gate_env` (`gates.py:142`) and the remote gate env builder in `worker.py` add
  `NW_GATE_CPUS=<cpus>`, `NW_GATE_MEM_MB=<mem_mb>` from `job_request` (SPEC-0504). Under the
  Slurm executor the worker uses the allocation when set (they win: they are what the cgroup
  grants): cpus `SLURM_CPUS_ON_NODE` (the grant), else `SLURM_CPUS_PER_TASK` (the request);
  memory `SLURM_MEM_PER_NODE`. The worker also passes a fixed allowlist of the job's Slurm keys
  (`SLURM_JOB_ID` first) into each gate command, so scripts can tell they run inside Slurm
  (see *As shipped*, 2026-11-10).
- nightwright `.nw/project.yaml`: `pytest-affected`, `docs-tests` and `pytest` pass
  `-n $(( NW_GATE_CPUS > 1 ? NW_GATE_CPUS - 1 : 1 ))` (xdist workers + 1 controller ~= cpus),
  replacing the fixed `-n 2`.
- `project.py` validation: `warnings` (printed by `nw projects config`, not failing) for the
  patterns above, text: "gate <name> sizes from the machine; use $NW_GATE_CPUS".
- **Outbound (project: jerome)**, to be authored as a portable spec in the jerome outbox and
  exported with `nw spec export` when this spec is accepted (listed in SPEC-0501's breakdown).
  Requirements for `tools/gate/cargo.sh`:
  1. `-j` = `${NW_GATE_CPUS:-${SLURM_CPUS_PER_TASK:-<today's rule>}}`.
  2. When `SLURM_JOB_ID` is set: no `systemd-run --user` and no `ulimit -v`; the Slurm cgroup is
     the cap.
  3. When `SLURM_JOB_ID` is set: no target-dir `flock` wait; nw submits jerome cargo gates with
     `-L jerome_target:1` (license count 3 = `JEROME_GATE_SLOTS`, defined in `slurm.conf`
     `Licenses=jerome_target:3`, SPEC-0510), and the script takes the target dir by
     `SLURM_JOB_ID`-keyed lock that never waits (a free dir is guaranteed by the license).
  4. jerome's `.nw/project.yaml` declares `resources: {cpus: 6, mem: 8G, licenses:
     [jerome_target]}` on its cargo gates (9G for `affected.py gate`).

## Alternatives considered

- **Rely on `SLURM_*` only.** The local executor would then have no sizing signal; one nw
  variable works under both.

## Acceptance criteria

- [x] A local and a remote gate both see `NW_GATE_CPUS`/`NW_GATE_MEM_MB` equal to the job request.
- [x] Under a Slurm job the values equal the grant: `SLURM_CPUS_ON_NODE` (else
      `SLURM_CPUS_PER_TASK`) / `SLURM_MEM_PER_NODE`; a worker gate sees `SLURM_JOB_ID` and the
      fixed Slurm allowlist (2026-11-10; real check after landing pending).
- [x] nightwright's pytest gates size `-n` from `NW_GATE_CPUS`.
- [x] A gate cmd containing `-n auto` or `nproc` yields a config warning, not an error.
- [ ] The jerome outbound requirements are recorded (this Design) and exported when the jerome
      spec is written.

## Test plan

- **Automated tests:** `tests/test_gate_env_sizing.py` (local env, remote env, Slurm env wins);
  config warning tests in `tests/test_agentic_project.py`;
  `tests/test_agentic_dogfood_config.py` asserts nightwright's gates no longer hard-code `-n 2`.
- **Manual verification (real binary):** after SPEC-0510, one nightwright gate on the box under
  Slurm: its log's xdist header shows `N workers` with N = requested cpus - 1.
- **Regression guard:** affected tests of `gates.py`, `worker.py`, `project.py`.

## As shipped

- `gate_resources.sizing_env(req, environ)` gives `NW_GATE_CPUS` / `NW_GATE_MEM_MB` from a
  request; `SLURM_CPUS_PER_TASK` / `SLURM_MEM_PER_NODE` in the process environment win
  whenever they are set (in a Slurm job they are: no executor check needed).
- **Local:** `gates._run_all_held` adds them to the gate env from the job request of the run's
  gate list (not `gate_env` itself, which has no gate list; every local campaign/sentinel/
  cadence run goes through `_run_all_held`). **Remote:** `worker._run_gates` adds them from
  `payload.resources`; a header from an older coordinator (no block) gets the gates' own
  defaults (`job_request` on the payload's gates).
- **Decision: the shell expression keeps today's value without the variable.**
  `pytest-affected` and `docs-tests` use `-n $(( ${NW_GATE_CPUS:-3} > 1 ? ${NW_GATE_CPUS:-3} - 1 : 1 ))`,
  `pytest` the same with default 5 (its old `-n 4`). A worker or coordinator not yet on this
  code sets no variable and runs exactly the old `-n`; POSIX arithmetic, so `dash` runs it
  too. With the SPEC-0504 requests the values are unchanged (3 -> 2, 5 -> 4).
- **Warnings:** `project.config_warnings(doc)` (gate and cadence commands matching `-n auto`,
  `--numprocesses auto`, `nproc`, `getconf _NPROCESSORS_ONLN`, `os.cpu_count`,
  `multiprocessing.cpu_count`) -> `gate <name> sizes from the machine; use $NW_GATE_CPUS`.
  `nw projects config` prints them on stderr (yellow) and puts them in `--json` as `warnings`;
  validation never fails on them.
- Not changed: the cadence jobs' fixed `-n` (outside this spec; cadence entries take no
  `resources` key yet).
- **jerome outbound:** the requirements stay recorded in *Design*. The portable jerome spec is
  not authored or exported in this change (another repo's outbox; listed in SPEC-0501's
  breakdown as its own item).
- Tests: `tests/test_gate_env_sizing.py` (sizing unit; a local run's gate sees the job's max
  request; a remote in-process worker job with and without `payload.resources`; Slurm values
  win locally and remotely), `tests/test_agentic_project.py` (warning table, `nw projects
  config --json` shows them), `tests/test_agentic_dogfood_config.py` (no fixed `-n <digit>`
  in the three pytest gates, `NW_GATE_CPUS` used, no warnings).
- **Manual verification (real binary): pending SPEC-0510** as the Test plan says (an xdist
  header under Slurm). Done meanwhile: the box run of this branch (old worker build, no
  variable set) gated it green, but `nw gate run` takes the gate list from main's
  `.nw/project.yaml`, so the new expression first runs on the box after merge. The expression
  itself was run in `bash` and `dash` (unset -> 2, `NW_GATE_CPUS=1` -> 1, 5 -> 4).
- Open: the jerome portable spec (AC 5's export) and the Slurm xdist check (after SPEC-0510).

- **Box runs (one lane, SPEC-0503..0509 together):** loop tier `GATEJOB-1461` (0503, green),
  `GATEJOB-1466` (0504, red: a pinned payload key set, fixed), `GATEJOB-1470` (0509, green),
  `GATEJOB-1476` (0508, red: the list table wrapped at 80 columns, fixed); integration+remote
  affected (`--cmd`, `-m 'integration or remote'`) `GATEJOB-1465` (10 pinned shapes/goldens,
  fixed) and `GATEJOB-1475` (1 env allowlist, fixed). The lane's final loop gate on its last
  commit is recorded below. **Status stays `in-progress`** until the real-binary step that
  needs the deployed worker build is done (not run in this lane).
  **Final lane gate:** `GATEJOB-1504`, loop tier on `83a7894e`, green (3382 passed). An earlier
  run on the same commit, `GATEJOB-1485`, hung near the end of pytest and was cancelled; the
  lane's touched and new test files ran serially with `faulthandler_timeout=60` without a hang,
  and the rerun passed, so no test of this lane is identified as the cause (the box holds 18
  orphaned xdist worker processes from older jobs, days old, a pre-existing pattern).

- **Slurm keys reach a worker gate; the grant sizes it (2026-11-10).** The first real Slurm run,
  `GATEJOB-1701` (Slurm job 179, box `morais-Desktop`, Slurm 26.05, `CR_CORE_MEMORY`,
  `task/cgroup,task/affinity`), printed `SLURM_JOB_ID=none NW_GATE_CPUS=3` and `nproc` 4. Two
  causes: the worker's SPEC-0346 allowset (`worker._run_gates`) passed no `SLURM_*` key, and
  `SLURM_CPUS_PER_TASK` is the *request* (3), while CR_Core grants whole 2-thread cores (4;
  `scontrol show job`: `CPUs/Task=3`, `AllocTRES=cpu=4`). jerome's `cargo.sh` tells "inside
  Slurm" by `SLURM_JOB_ID`, so it escaped the job's cgroup via `systemd-run --user`. Fix:
  - `gate_resources.SLURM_GATE_ENV`, an explicit allowlist (no `SLURM_*` wildcard, SPEC-0346):
    `SLURM_JOB_ID`, `SLURM_JOB_NAME`, `SLURM_JOB_PARTITION`, `SLURM_JOB_QOS` (identity and
    class), `SLURM_CPUS_PER_TASK`, `SLURM_CPUS_ON_NODE`, `SLURM_JOB_CPUS_PER_NODE`,
    `SLURM_MEM_PER_NODE`, `SLURM_JOB_LICENSES` (allocation), each only when set. Left out:
    step/task/launch keys (`SLURM_STEP_*`, `SLURM_NODELIST`, `SLURM_TASK_*`, `SLURM_CONF`: a
    gate is not meant to `srun` a step), `SLURM_SUBMIT_DIR` (the spawner's cwd, meaningless to a
    gate), `SLURM_RESTART_COUNT` (nw's own attempt variable covers it).
  - `slurm_env(environ)` returns them only when `SLURM_JOB_ID` is set; `worker._run_gates` adds
    them only for a job of the slurm executor (`executors.for_job(...).name == "slurm"`), so a
    local-executor job's gate env is unchanged.
  - `sizing_env` now reads Slurm keys only when `SLURM_JOB_ID` is set, and cpus are
    `SLURM_CPUS_ON_NODE` (the grant), else `SLURM_CPUS_PER_TASK`. The worker sizes from the
    `slurm_env` dict, the local gate path (`_run_all_held`, e.g. a campaign run inside a Slurm
    job) from its own process env as before. No cgroup/affinity read: the env is Slurm's own
    statement of the grant, and with `task/affinity` it equals the cpuset. **Probed on the box
    (as bruno, 2026-11-10, Slurm job 184, `sbatch --cpus-per-task=3 --mem=512M --time=1`):**
    `SLURM_CPUS_PER_TASK=3`, `SLURM_CPUS_ON_NODE=4`, `SLURM_JOB_CPUS_PER_NODE=4`,
    `SLURM_MEM_PER_NODE=512`, `SLURM_JOB_PARTITION=nw-gatejob`; `nproc` and
    `sched_getaffinity` 4, job cgroup `cpuset.cpus.effective` `0-1,6-7`, `memory.max` 512M.
    No `SLURM_JOB_QOS` / `SLURM_JOB_LICENSES` without a QOS / license (passed when set).
  - Tests: `tests/test_gate_env_sizing.py` (allowlist unit; grant over per-task; no
    `SLURM_JOB_ID` -> request; a local-executor worker gate sees `SLURM_JOB_ID=none` and the
    request despite stray keys), `tests/test_executor_slurm.py` (a gate inside a fake Slurm job,
    3 asked / 4 granted, sees exactly the allowlisted keys and `NW_GATE_CPUS=4`);
    `tests/slurm_fake.py` exports the batch step's keys (`SLURM_CPUS_ON_NODE`, partition, plus
    `SLURM_SUBMIT_DIR`/`SLURM_NODELIST` that must not pass).
  - **Post-landing check (pending, needs `nw workers update box --when-idle`):** the same
    `nw gate run --cmd 'echo SLURM_JOB_ID=... NW_GATE_CPUS=...; nproc'` must print a job id
    and `NW_GATE_CPUS` = `nproc` (4 for a 3-cpu request).

## Rollout / migration

Ships with the default requests of SPEC-0504 (3 cpus -> `-n 2`, today's value), so the local
executor behaves as before. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and noted.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Risks

- jerome cargo gates escape the cgroup until the jerome change lands: keep jerome's gates on the
  local executor's admission (or pin them to `executor: local`) until then; SPEC-0510's rollout
  says so.

## Open questions

- None.
