---
id: SPEC-0508
title: "Worker capacity and load: nw worker info reports cores, memory, load and per-job usage; nw workers list shows them"
status: in-progress
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, observability]
depends_on: [SPEC-0503]
source_idea: IDEA-1112
---

## Context

IDEA-1112: there is no way to see a worker's capacity and load. The forced-command SSH
(`worker.check_ssh_command`, `worker.py:1455`) allows only `nw worker` subcommands, and
`nw worker info` (`worker.py:1359`) returns versions, `data_dir` and `free_bytes` only. The
SPEC-0501 research had to read the box's size (12 cores / 31 GB) from a comment in jerome's
`cargo.sh`. Sizing slots, judging a timeout and later checking Slurm's view all need these
numbers. Under Slurm the authoritative capacity is the node's (`scontrol show node`), so the
view must read Slurm when the executor is `slurm`.

## Goals / Non-goals

**Goals**
- `nw worker info --json` adds a `capacity` block; `nw workers list` / `check` render it from the
  cached `info` (SPEC-0341 caches `info` in the registry).
- Same shape for `local` and `slurm` executors.

**Non-goals**
- A TUI view (a later Phase 2 item in SPEC-0501). History (SPEC-0514 covers usage history).

## Decision

Extend `worker.info` with `capacity` read from `/proc` (local) or from `scontrol show node
--json` (slurm), plus per running job usage, and render one line per worker.

## Design

`capacity` (schema `nw.worker.capacity.v1`):

```json
{"source": "proc" | "slurm",
 "cpus": 12, "mem_total_mb": 31955, "mem_available_mb": 18000,
 "loadavg": [6.1, 5.8, 5.2],
 "alloc_cpus": null, "alloc_mem_mb": null,          // slurm: CPUAlloc / AllocMem
 "jobs": [{"job_id": "...", "state": "running", "cpu_pct": 210.0, "rss_mb": 1400}]}
```

- `source: proc`: `os.cpu_count()`, `/proc/meminfo` (`MemTotal`, `MemAvailable`),
  `os.getloadavg()`; per job: sum over the pids in the job's `proc.json` gate groups of
  `/proc/<pid>/stat` utime+stime sampled twice 0.5 s apart and `/proc/<pid>/status` `VmRSS`.
- `source: slurm`: `scontrol show node <host> --json` (`cpus`, `real_memory`,
  `alloc_cpus`, `alloc_memory`, `free_mem`, `cpu_load`); per job `sstat -j <id> --json`
  (`TRESUsageInAve`/`MaxRSS`) when the job is running; on any Slurm CLI error, fall back to
  `proc` and set `"error"`.
- `nw workers list` gains a column `load` = `loadavg[0]/cpus` and `mem` =
  `(total-available)/total` (or `alloc_*` under Slurm), `--json` carries the block.
- No change to the forced-command grammar (`info` and `info --json` are already allowed).

## Alternatives considered

- **A new `nw worker load` subcommand.** `info` is already allowed and cached; one call suffices.

## Acceptance criteria

- [ ] `nw worker info --json` has `capacity` with `cpus`, `mem_total_mb`, `mem_available_mb`,
      `loadavg`, `jobs[]` on a local-executor host.
- [ ] With `executor: slurm`, the block comes from `scontrol show node` and fills `alloc_*`.
- [ ] `nw workers list` shows load and memory per worker from the cached info.

## Test plan

- **Automated tests:** `tests/test_worker_capacity.py` with a fake `/proc` root (fixture path
  injection) and the fake Slurm shim (`tests/slurm_fake.py`, SPEC-0510; until it exists, a
  local fake returning recorded `scontrol show node --json` output) for both sources; a render
  test for `nw workers list`.
- **Manual verification (real binary):** `nw workers check box` then `nw workers list` on the
  laptop shows `cpus 12`, `mem_total_mb ~31955`; after SPEC-0510, the same with
  `source: slurm` and `alloc_cpus` matching `scontrol show node morais-Desktop` run as
  `bruno@morais-Desktop`.
- **Regression guard:** affected tests of `worker.py`, `cli_workers.py`, `workers.py`.

## As shipped

- `src/nightwright/agentic/worker_capacity.py`; `worker.info` adds `capacity` (never raises:
  a failure is `error` in the block).
- `source: proc`: `os.cpu_count()`, `/proc/meminfo`, `/proc/loadavg` (the same numbers as
  `os.getloadavg()`, read from the file so a fake root can stand in).
- `source: slurm` (executor `slurm`): `scontrol show node <short hostname> --json` (5 s
  timeout). Slurm numbers may be `{"set", "number"}` objects (24.05+) or plain; both are read.
  `loadavg` is `[cpu_load / 100]` (Slurm's 1-minute load x 100). **Decision:**
  `mem_available_mb` = `real_memory - alloc_memory` (what Slurm can still allocate); the node's
  `free_mem` is the kernel's MemFree (page cache counts as used: 4783 MB free vs 15729 MB
  available on the box, 2026-11-09) and is kept as `free_mem_mb`. Any CLI or parse failure
  falls back to `proc` with `error`.
- **Deviation: per-job usage always comes from `/proc`, also under Slurm.** A running job
  (`job.lock` held, no `result.json`) contributes every process whose process group is the job
  process's or one its `proc.json` records for a gate (so pytest-xdist workers count), sampled
  twice `SAMPLE_S` (0.5 s) apart: `cpu_pct` (100 = one core) and `rss_mb` (sum of `VmRSS`).
  The jobs run on this host under either executor, so `sstat` adds nothing for one node; a
  multi-node view (SPEC-0514 / Phase 2) is where it belongs. The sample runs only when a job
  runs.
- `nw workers list` gains one column `cpus load mem` (so an 80-column table still fits: `cpus`, Slurm `alloc/total`; `load`, 1-minute load per cpu; and
  `mem` (in use, Slurm: allocated, of the total) from the cached `info` (`-` for a worker whose
  info has no block); `--json` rows carry `capacity`. `nw workers check` prints one
  `capacity (...)` line per worker.
- Tests: `tests/test_worker_capacity.py`: a fake `/proc` root; the recorded real
  `scontrol show node morais-Desktop --json` output (`tests/fixtures/slurm/scontrol_show_node.json`,
  Slurm 26.05.5) through a fake runner, its failure fallbacks; a real busy process group
  sampled from the real `/proc`; `info`'s block; the list render (proc, slurm, old worker).
- **Real binary, done:** the readers ran on the box (`bruno@morais-Desktop`, read-only, this
  branch's module piped to `python3`): `proc` -> `cpus 12, mem_total_mb 31955`; `slurm` ->
  `cpus 12, mem_total_mb 31955, alloc_cpus 0, alloc_mem_mb 0`, matching
  `scontrol show node morais-Desktop`. This laptop's `uv run nw worker info --json` reports
  its own block. **Pending deploy:** `nw workers check box` + `nw workers list` showing the box
  needs the worker updated (`nw workers update`, not run in this lane); until then the box's
  cached info has no block and the list prints `-`.

## Rollout / migration

Additive; an old worker's info lacks `capacity` and the list prints `-`. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- The 0.5 s sampling makes `info` slower; it runs only when `jobs` is non-empty and is bounded
  to one sample pass.

## Open questions

- None.
