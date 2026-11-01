---
id: SPEC-0342
title: "Lost-job retry across workers and escalation"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, reliability, conductor]
depends_on: [SPEC-0341]
---

## Context

[SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md) detects a lost job or
worker, appends `worker_job_lost` and runs the gate locally. With more than one worker
registered, a better answer is to try another worker first. A worker that keeps losing jobs
should also reach the human.

This is the third part of the split of
[SPEC-0332](./0332-worker-owned-async-jobs.md).

## Goals / Non-goals

**Goals**
- A lost job is retried with a new attempt on another eligible worker, and runs locally once
  the attempts are used up.
- Several workers are used at once.
- A worker that loses jobs repeatedly raises one escalation to the human through the
  conductor.

**Non-goals**
- Load-aware scheduling. "First free slot in registry order" is enough at this scale
  (affinity comes with SPEC-0345).
- Persona jobs (SPEC-0344).
- Bootstrap and drain (SPEC-0343).

## Decision

On give-up (SPEC-0341), the step writes a new record with `attempt + 1`, adds the lost worker
to `excluded_workers`, and acquires a slot in the next eligible pool. After
`agentic.workers.max_remote_attempts` (default 2), the job runs locally. Three
`worker_job_lost` for one worker within 3600 s raise one `escalation`.

## Design

**Retry.**
1. Giving up appends `worker_job_lost {worker, job_id, attempt, reason}` (SPEC-0341), sends
   a best-effort `cancel`, and retries with `attempt + 1` on the next eligible worker that
   is not in `excluded_workers`.
2. The retry releases the current worker's slot and acquires one in the next eligible pool.
3. After `max_remote_attempts`, or when every eligible worker is excluded, the job runs
   locally. Retries append `worker_job_retried {job_id, from_worker, to_worker, attempt}`
   (a new event in `EVENT_TYPES`).
4. The `gates/` files of an abandoned attempt are deleted before the next attempt writes, as
   in SPEC-0338's fallback.
5. `job_id` carries the attempt (`...-a<attempt>`), one record per attempt. The lost record
   is set to `lost` before the next attempt's record is written, so the `(campaign, seq)`
   lookup (SPEC-0341) returns the single non-final record with the highest attempt.
6. Adoption (SPEC-0341) of a record whose attempt is above 1 respects its
   `excluded_workers`.

**Several workers.** The slot pools of SPEC-0337 already cover N workers. The only change is
that `excluded_workers` removes a worker from one job's pool list.

**Events and escalation.** `worker_job_retried` joins `EVENT_TYPES` (`worker_job_lost` is
SPEC-0341's). Both are informational for the conductor, except that three `worker_job_lost`
for one worker within 3600 s raise one `escalation` (raised by the queue, as other
escalations are) with options `off` (sets the worker `enabled: false`) and `keep` (no
change). One escalation per worker per window: further losses inside the window do not raise
another.

## Alternatives considered

- **Always fall back to local on the first loss.** Simple, but it ignores an idle second
  worker.
- **Unbounded retries.** A job that kills every worker would loop. Two remote attempts then
  local bounds the waste.

## Acceptance criteria

- [ ] Killing the worker's job process yields `lost`, then `worker_job_lost`, then a retry
      with `attempt + 1` on a second worker. After `max_remote_attempts`, the job runs
      locally.
- [ ] `worker_job_retried` is appended with `from_worker`, `to_worker` and `attempt`; the
      abandoned attempt's `gates/` files are deleted before the next attempt writes.
- [ ] With two workers registered, concurrent campaigns use both.
- [ ] A retry never picks a worker in `excluded_workers`; with all eligible workers
      excluded, the run is local.
- [ ] Three `worker_job_lost` for one worker within 3600 s raise one `escalation` with
      options `off` and `keep`; a fourth in the same window does not raise another.
- [ ] With no workers registered, behaviour is unchanged.

## Test plan

- **Automated tests** (`tests/test_worker_job_retry.py`, offline). They use SPEC-0338's
  loopback transport with two loopback workers (two temp data dirs):
  - **lost:** the job's pid is killed, which gives `worker_job_lost` and a retry on worker 2;
    with both excluded, the run is local;
  - `max_remote_attempts` is honoured, and the abandoned attempt's files are gone;
  - two campaigns against two workers place one job on each;
  - adoption of an attempt-2 record keeps `excluded_workers`;
  - three `worker_job_lost` in an hour give one `escalation`, and the `off` option disables
    the worker.
- **Manual verification:**
  1. With two workers registered, reboot the first mid-gate. Expect `worker_job_lost`, then
     `worker_job_retried` to the second worker, and a completed gate.
- **Regression guard:** affected tests while iterating; SPEC-0341's tests run unchanged.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. Nothing to migrate. With one worker registered, the retry has no second pool and the job
   runs locally, as after SPEC-0341.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0332:
- A job gets 2 remote attempts before it runs locally.
- The escalation threshold is three losses for one worker in 3600 s.
