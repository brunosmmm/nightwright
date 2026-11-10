---
id: SPEC-0517
title: "Multi-node job spool: job dirs and repo caches on a shared /srv/nw, checkouts on node-local scratch"
status: draft
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, slurm, storage]
depends_on: [SPEC-0516, SPEC-0336, SPEC-0388]
---

## Context

Draft until two nodes exist (SPEC-0516). Today a worker's job dirs and repo caches live under
the `nw` user's data dir (`worker.jobs_dir`, `worker.repos_dir`, `worker.py:103-107`), and the
checkout (`tree/`), gate output and tmp sit inside the job dir. With two Slurm nodes, `submit`
runs on `big` but the job may run on `morais-Desktop`: the header, bundle, `frames.jsonl` and
`result.json` must be visible on both, while the checkout and build trees must stay local (NFS
for cargo targets and pytest tmp is slow and a stalled mount would hang jobs). `DiskGuard`
(SPEC-0388) watches one disk.

## Goals / Non-goals

**Goals**
- A job runs on either node with no change to the coordinator.
- Heavy I/O stays node-local; per-node disk guard.

**Non-goals**
- A distributed filesystem; HA storage.

## Decision

`big` exports `/srv/nw` over NFS (same path on both nodes). `agentic.workers.spool_dir:
/srv/nw` moves `jobs/` and `repos/` there. `run-job` clones from `/srv/nw/repos/<key>` into
`/var/tmp/nw/<SLURM_JOB_ID>/tree` (node-local, removed at job end), and writes only frames, gate
logs and results to the NFS job dir. `DiskGuard` and `admission_refusal` check the node-local
scratch filesystem on the node that runs the job. Until the spool is proven, jobs may be pinned
to `big` (`agentic.slurm.nodelist: big`).

## Design

- `worker.jobs_dir/repos_dir` read `spool_dir` when set (default: today's paths).
- `run-job`: `scratch = Path(agentic.slurm.scratch_root or "/var/tmp/nw") / SLURM_JOB_ID`;
  `tree`, `out` tmp files in scratch; gate logs copied into the job dir on gate end.
- Locks: `repo_lock` uses `fcntl` locks, which NFSv4 supports; job-dir locks are taken only by
  processes on the node running the job, plus readers on `big`.
- Root plan additions in SPEC-0516's bootstrap: `/etc/exports` on `big`, an fstab/automount
  entry on `box` with `hard,timeo=600`.

## Alternatives considered

- **Per-node spools with the coordinator asking the right node.** Needs the coordinator to know
  where Slurm placed the job and SSH to both nodes; one submit host is simpler.

## Acceptance criteria

- [ ] A job placed on `morais-Desktop` is attachable from `big` and its result reaches the
      coordinator.
- [ ] No checkout or build tree is written to NFS.
- [ ] A full node-local scratch disk refuses or aborts only jobs on that node.

## Test plan

- **Automated tests:** `tests/test_worker_spool.py`: spool path resolution, scratch layout,
  scratch cleanup on success, failure and requeue.
- **Manual verification (real binary):** `-w morais-Desktop` pinned `nw gate run` from `big`;
  `ls /srv/nw/jobs/<id>` on both hosts; `df /var/tmp/nw` shows the tree on the node. Record.
- **Regression guard:** affected tests of `worker.py`, `worker_job.py`.

## Rollout / migration

After SPEC-0516; first with `nodelist: big`, then unpinned. Supersedes nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- A stalled NFS mount on `box` hangs its jobs; `hard` mounts with timeouts and Slurm `--time`
  bound the damage, and `scontrol update NodeName=morais-Desktop State=DRAIN` takes the node out.

## Open questions

- NFS vs a simpler rsync-back of results from node scratch (decide with the new box's disks).
