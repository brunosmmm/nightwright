---
id: SPEC-0513
title: "Slurm jobs survive a sleeping coordinator: no lease self-cancel, pending time does not count against the deadline, re-attach on wake"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, slurm, reliability]
depends_on: [SPEC-0510, SPEC-0341]
---

## Context

A worker job cancels itself when its lease expires: `worker_job._watch`
(`worker_job.py:907`) reads `lease.json` and, past `lease_until`, sets cancel and
`G.terminate_all()`. The lease is `agentic.workers.job_lease_s` (default 900 s,
`worker_job.DEFAULTS`), renewed by the coordinator's `poll --renew`/`attach`. A laptop
coordinator asleep > 15 min kills its own gates and pays them again on wake. On the coordinator,
`remote_jobs` records carry `deadline_at` (`remote_jobs.py:119`); `remote_gates._give_up` ends a
job past it. Under Slurm, a job may also sit PENDING for a long time, which is queueing, not a
hang. SPEC-0341 adoption already re-attaches to a recorded job after a coordinator restart.

## Goals / Non-goals

**Goals**
- A Slurm job runs to completion whether or not the coordinator renews its lease.
- PENDING time does not consume the coordinator's deadline.
- Abandoned jobs stay bounded (Slurm `--time`, `nw worker gc`).

**Non-goals**
- The local executor's lease (kept; SPEC-0520 removes it with the local kill paths if ever).

## Decision

`SlurmExecutor.lease_cancels = False` (SPEC-0503): the `_watch` lease branch is skipped and
`poll --renew` is accepted but a no-op. The coordinator computes a Slurm job's deadline from its
**start** (first `running` / `started` frame), not its submission, and extends it while the job
reports PENDING. On wake, the existing adoption/re-attach path picks up finished results.

## Design

- Worker: `run_job` passes `executor.lease_cancels` to `_watch`; `submit` still writes the first
  lease (harmless, keeps file shapes). `nw worker info` features gain `lease:none` under Slurm.
- Coordinator (`remote_gates._Job`, `remote_personas._follow`): when the worker's cached info
  has `lease:none`, `deadline_at` = `started_at + job_timeout_s + margin`; before a start it is
  `submitted_at + agentic.slurm.max_pending_s` (default 6 h), re-armed each time a `slot_wait`
  frame with a Slurm `reason` arrives. Past it, `_give_up` as today (and `scancel` through
  `cancel`).
- `nw worker gc --keep-days N` (exists) already reaps old job dirs; Slurm's `--time` bounds
  runtime. A result left by an un-collected job stays until gc (default keep days unchanged).

## Alternatives considered

- **Raise `job_lease_s` to hours.** Still kills on a long sleep and keeps the kill path that
  Slurm makes redundant.

## Acceptance criteria

- [ ] Under Slurm, a job whose lease expired keeps running and writes `result.json`.
- [ ] A coordinator that resumes after > 900 s re-attaches and applies the result with no re-run.
- [ ] A job PENDING longer than `job_timeout_s` is not given up while it reports a pending reason
      and is younger than `max_pending_s`.
- [ ] Under `local`, lease behaviour is unchanged.

## Test plan

- **Automated tests:** `tests/test_slurm_lease.py`: `_watch` with an expired lease under a
  `lease_cancels=False` executor does not cancel; coordinator deadline arithmetic (pending
  re-arm, start-based deadline, `max_pending_s` give-up) with fake clocks; local executor test
  still cancels on lease expiry.
- **Manual verification (real binary):** start a nightwright campaign gate on the box under
  Slurm, then suspend the laptop (`systemctl suspend`) for 20 min; on wake, `nw campaign show`
  shows the gate result applied from the box with no local re-run and no second job in
  `squeue`/the job dir. Record in As shipped.
- **Regression guard:** affected tests of `worker_job.py`, `remote_gates.py`,
  `remote_personas.py`, `remote_jobs.py`.

## Rollout / migration

Ships with or after SPEC-0510. Supersedes nothing now; SPEC-0520 supersedes the lease/lost
parts of SPEC-0340 when the local kill paths are removed.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- A job whose campaign was abandoned while the laptop slept runs to completion and wastes its
  allocation; bounded by `--time`, and campaign cancel still scancels on wake.

## Open questions

- None.
