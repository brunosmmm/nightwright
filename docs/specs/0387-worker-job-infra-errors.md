---
id: SPEC-0387
title: Worker job-level infra errors (ENOSPC) - real stage, no worker cooldown, admission guard
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
tags: [workers, remote-gates]
---

## Context

On 2026-11-02 16:00:59 a remote gate job on worker `box` failed with `worker_failed stage=slot
error=OSError: [Errno 28] No space left on device`. The job ended `lost` and the whole 6-slot box
cooled down for 10 min, so gating fell back to local slots. Causes: (1) `worker_job.run_job`
set `stage="slot"` after checkout and never advanced it, so every exception inside
`W._run_gates` said `slot`, with no traceback or path; (2) `gates.run_gate` let an `OSError`
from the log open or `_write_sidecar` kill the job; (3) the coordinator cooled the whole worker
down for any `worker_failed`, including a job-level error from a live worker.

## Goals / Non-goals

**Goals**: diagnosable error frames; a full disk yields a failed gate result, not a dead job;
a job-level error costs only that job a retry elsewhere; a nearly full worker refuses jobs.

**Non-goals**: cleaning the disk (gc exists); changing transport-failure cooldown.

## Decision

- **Error frame (a).** The generic-exception `error` frame (async `run_job` and sync `gate-job`)
  carries the real stage (`checkout`, `gate:<name>`, `frame`, else the old `slot`/`run`),
  `traceback` (last 4096 bytes), `filename` for an OSError, `job_error: true`, and on ENOSPC
  `disk` (`shutil.disk_usage(data_dir)`) and `job_bytes` (sizes of `job/{tmp,out,tree}`).
  `W._Frames.stage` is set by `_run_gates` around each gate and each frame emit.
- **run_gate (b).** An OSError creating the gate log yields a failed `GateResult` with new field
  `infra_error` (sidecar written best effort); an OSError writing the sidecar marks the finished
  result `ok=False` + `infra_error`. The job still emits `gate` and `done` frames.
  `infra_error` is in `as_payload` only when set, so existing payloads are unchanged.
- **Cooldown (c).** An `error` frame with `job_error: true` gives a `RemoteFailure` with
  `cooldown=False, job_error=True`. For a recorded (async) job `_failed` records `worker_failed`
  (no cooldown) then `_give_up(..., "job_error")`: the job record goes `lost`, and the existing
  SPEC-0342 retry runs it on another worker (this worker is excluded for that job only, which is
  the per-job back-off) or locally when none is left; repeated ones count toward the worker-loss
  escalation. A sync `gate-job` has no record and falls back locally, still without cooldown.
  Transport/unreachable/worker-dead failures and `WorkerError` stages (checkout, admission) keep
  the 10 min cooldown. Not configurable: the existing retry limit (`max_remote_attempts`) bounds it.
- **Admission (d).** `W.admission_refusal(cfg)`: when free space at the worker dir is below
  `agentic.workers.min_free_gb` (default 5; invalid -> default), `submit` and `gate-job` refuse
  with an `error` frame, stage `admission` and a clear message. This is a host condition, so the
  coordinator's normal cooldown applies. A failure to measure admits the job.
- **(e)** `nw workers clear-cooldown NAME` clears only `cooldown_until` (enabled/drain untouched).

## Design

`worker.py`: `min_free_gb`, `admission_refusal`, `error_detail`, `_Frames.stage`, `_fail(detail=)`.
`worker_job.py`: run_job / submit use them. `gates.py`: `GateResult.infra_error`, `_infra_result`.
`remote_gates.py`: `_error_failure`, `RemoteFailure.job_error`, `_failed`. `cli_workers.py`.

## Alternatives considered

- **Cool down only the failing slot** - slots are interchangeable and the cause is disk, not slot.
- **A new config for the back-off** - per-job exclusion already exists; no knob needed.

## Acceptance criteria

- [x] Error frame reports `gate:<name>`, traceback, filename and ENOSPC disk/dir sizes.
- [x] OSError on gate log/sidecar yields a failed result with `infra_error`.
- [x] A `job_error` frame sets no cooldown and the job is given up for retry.
- [x] Below `min_free_gb` a new job is refused with stage `admission`.
- [x] `nw workers clear-cooldown NAME` clears only the cooldown.

## Test plan

- **Automated:** `tests/test_worker_infra_error.py` (a-e, offline, tmp_path, config load patched).
- **Regression:** `tests/test_remote_gates.py`, `tests/test_worker.py`, `tests/test_worker_jobs.py`,
  `tests/test_workers_update_schedule.py`.
- **Manual:** none (never against the real box).

## Rollout / migration

Workers must be updated (`nw workers update`) to emit the new frame fields; an old worker's
errors keep today's behaviour (cooldown). Docs: `docs/product/reference/index.md`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions (affected files above).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.
