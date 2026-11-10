---
id: SPEC-0508
title: "Worker capacity and load: nw worker info reports cores, memory, load and per-job usage; nw workers list shows them"
status: accepted
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
