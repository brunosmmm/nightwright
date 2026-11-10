---
id: SPEC-0509
title: "Gate commands size their parallelism from the job's allocation (NW_GATE_CPUS / NW_GATE_MEM_MB), with a config warning for nproc and -n auto"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
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
  request (SPEC-0504), equal to `SLURM_CPUS_PER_TASK` / the job's memory under Slurm.
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
  Slurm executor the worker uses `SLURM_CPUS_PER_TASK` and `SLURM_MEM_PER_NODE` when set (they
  win: they are what the cgroup grants).
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

- [ ] A local and a remote gate both see `NW_GATE_CPUS`/`NW_GATE_MEM_MB` equal to the job request.
- [ ] Under a Slurm job the values equal `SLURM_CPUS_PER_TASK` / `SLURM_MEM_PER_NODE`.
- [ ] nightwright's pytest gates size `-n` from `NW_GATE_CPUS`.
- [ ] A gate cmd containing `-n auto` or `nproc` yields a config warning, not an error.
- [ ] The jerome outbound requirements are recorded (this Design) and exported when the jerome
      spec is written.

## Test plan

- **Automated tests:** `tests/test_gate_env_sizing.py` (local env, remote env, Slurm env wins);
  config warning tests in `tests/test_agentic_project.py`;
  `tests/test_agentic_dogfood_config.py` asserts nightwright's gates no longer hard-code `-n 2`.
- **Manual verification (real binary):** after SPEC-0510, one nightwright gate on the box under
  Slurm: its log's xdist header shows `N workers` with N = requested cpus - 1.
- **Regression guard:** affected tests of `gates.py`, `worker.py`, `project.py`.

## Rollout / migration

Ships with the default requests of SPEC-0504 (3 cpus -> `-n 2`, today's value), so the local
executor behaves as before. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- jerome cargo gates escape the cgroup until the jerome change lands: keep jerome's gates on the
  local executor's admission (or pin them to `executor: local`) until then; SPEC-0510's rollout
  says so.

## Open questions

- None.
