---
id: SPEC-0388
title: Worker disk guard - auto-reap dead jobs, in-run free-space abort, job sizes, update headroom
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
tags: [workers, remote-gates]
depends_on: [SPEC-0387]
---

## Context

IDEA-807. On 2026-11-02 the remote worker box (ext4: the `nw` user gets ENOSPC once the fs passes
~95% because of root's 5% reserve) ran out of space: two big `cargo build` standalone gate jobs plus
`lost` / `transport_error` jobs whose tree and build output stay until a manual `nw worker gc`.
A wheel update then failed at receive with ENOSPC. SPEC-0387 made the error legible and added
`min_free_gb` admission, but nothing frees space by itself, nothing stops a job mid-run, job sizes
are invisible, and the update does not check for room.

## Goals / Non-goals

**Goals**: a dead job gives its disk back by itself; one job cannot wedge the host; sizes are
visible; an update fails clearly when the worker has no room.

**Non-goals**: shared repo caches (`repos/`) and build/uv/cargo caches are never touched (the
Rust cache is valuable); per-job size caps; changing `gc`'s `--keep-days` semantics.

## Decision

1. **Auto-reap.** `worker.reap_dead_scratch(cfg)` removes `tree`, `tmp`, `out` (and a leftover
   `bundle`/`job.bundle`) of every job dir whose process is gone: `job.lock` free (the `gc` test) and
   no process group named in `proc.json`/`spawn.json` still alive; and quiet for
   `agentic.workers.reap_lost_after_min` (default 10, 0 = at once; newest mtime of `job.json`,
   `result.json`, `heartbeat.json`, `frames.jsonl`, `cancel`, `lease.json`). This covers `lost` jobs,
   jobs the coordinator gave up on (transport error, cancel) whose process died, and failed cleanups.
   `job.json`, `frames.jsonl`, `result.json` stay; a `reaped_scratch.json` marker is written. It
   runs from `nw worker gc` (so after every job) and from a running job's disk watch (every 60 s),
   so a lost job's tree goes while other jobs run.
2. **In-run watch.** `worker.DiskGuard` runs in `_run_gates` (async and sync jobs). Every 5 s it
   checks free space of the data dir fs; below `agentic.workers.abort_free_gb` (default 2, 0 = off)
   it first reaps dead scratch with no grace, and if still below, the live job with the most
   scratch bytes is killed: only that job's own guard acts (`cancel` + gate termination), and the
   job ends with an `error` frame `stage: "disk"`, `job_error: true`, the disk detail
   (SPEC-0387 `error_detail`: `disk`, `job_bytes`) and a message naming the floor. The coordinator
   treats it as a job-level error (retry elsewhere, no cooldown).
3. **Sizes.** `WJ.jobs` rows (and so `nw worker jobs --json`) gain `size_bytes` (whole job dir)
   and `scratch_bytes` (tree+tmp+out); the text listing adds a human size column.
4. **Update headroom.** `nw workers update` (and the queue's scheduled path) probes the worker's
   `info.free_bytes` after building the wheel and, below 3 x wheel size, raises `UpdateError`
   before uploading. A probe failure or a missing figure lets the upload proceed. `--dry-run`
   sends nothing and skips the probe.

Config (`docs/product/reference/index.md`): `agentic.workers.reap_lost_after_min`,
`agentic.workers.abort_free_gb`. Invalid values fall back to the defaults.

## Design

`worker.py`: `abort_free_gb`, `reap_lost_after_min`, `job_scratch_bytes`, `job_disk_bytes`,
`reap_dead_scratch`, `DiskGuard`, `DiskAbort(OSError ENOSPC)`; `gc` calls `reap_dead_scratch`
first. `worker_job.jobs`, `cli_worker.worker_jobs`, `workers_update._preflight`.

## Alternatives considered

- **Reaping from the coordinator over ssh** - needs a verb and a reachable worker; the case at
  hand is a worker that cannot be reached or served.
- **Killing the newest job on low disk** - the largest frees the most and is the likeliest cause.
- **Also pruning build caches** - rejected: losing the Rust cache costs more than it saves.

## Acceptance criteria

- [x] A dead job (lock free, no live group) quiet past the grace loses tree/tmp/out; metadata stays.
- [x] A job with a live lock, or a younger one, is untouched; `repos/` and other dirs are untouched.
- [x] `reap_lost_after_min` and `abort_free_gb` configure grace and floor; bad values default.
- [x] Below the floor the largest live job is killed with a `job_error` frame naming the disk;
      a smaller one is not.
- [x] `nw worker jobs --json` has `size_bytes` / `scratch_bytes`; the text lists a size.
- [x] `nw workers update` fails before uploading when free_bytes < 3 x wheel; passes otherwise.
- [x] Config documented.

## Test plan

- **Automated:** `tests/test_worker_disk_guard.py` (tmp_path, monkeypatched free space and
  config load): reap grace / live lock / live group / caches untouched / config parsing; guard
  tick kills the largest job only and the job ends with the disk error frame; jobs sizes in
  JSON and text; update preflight blocks and allows (stub ssh from `test_workers_update_cmd`).
- **Manual:** none against the real box (not touched in this change).
- **Regression guard:** `tests/test_worker_infra_error.py`, `tests/test_workers_update_cmd.py`.

## Rollout / migration

Workers pick it up with `nw workers update`. No data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions (affected tests, per AGENTS.md).
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` updated.

## Open questions

None.
