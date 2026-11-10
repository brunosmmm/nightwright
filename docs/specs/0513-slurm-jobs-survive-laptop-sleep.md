---
id: SPEC-0513
title: "Slurm jobs survive a sleeping coordinator: no lease self-cancel, pending time does not count against the deadline, re-attach on wake"
status: in-progress
owner: user
created: 2026-11-09
updated: 2026-11-10
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

As shipped (decided while implementing; details in As shipped below):

- `lease_cancels = False` and `_watch(lease_cancels)` were already in place from SPEC-0503 /
  SPEC-0510; `poll --renew` and `attach` still rewrite `lease.json` under Slurm (nothing reads
  it there; skipping the write would only fork the file shapes), so "no-op" means "has no effect".
- The coordinator knows a lease-free worker from its cached `info` (`workers.lease_free`) at
  submit and writes it on the job record (`lease: "none"`, `timeout_s`), so an adopted record
  keeps the rule without re-reading the registry.
- The pre-start deadline is a fixed `submitted_at + max_pending_s` (`remote_jobs.max_pending_s`,
  `agentic.slurm.max_pending_s`, default 6 h). It is **not** re-armed per `slot_wait` frame: a
  re-arm would make a forever-PENDING job unbounded, and the fixed bound already gives every
  pending job `max_pending_s` (AC 3). The first `started` frame (`n` past the record's cursor)
  re-arms it to `started.at + timeout_s + worker_ttl_s` and persists it (`deadline_at`); a
  replayed `started` (adoption from frame 0) does not move it again.
- A deadline that passed is checked against the worker first (existing for `done`): a job that
  ended there, `done` **or `error`** (an infra end, SPEC-0515), is replayed however late. The
  persona path got the same check (`remote_personas._past_deadline`); before, a persona job
  past its deadline was given up even when its result sat on the worker.
- **Sleep does not count against the worker TTL** (`remote_jobs.SleepClock`): a coordinator
  that wakes finds its attach SSH dead, and `_follow` gave up `unreachable` (TTL 300 s) before
  polling once. Linux's monotonic clock stops during suspend, the wall clock does not; the lead
  of the wall clock over the monotonic one since the last check (over 30 s) moves `last_seen`
  forward. Checked every attach tick (`_Job.past_deadline`) and in the persona attach/follow
  loops. Without this AC 2 failed for any sleep longer than `worker_ttl_s`.

## Alternatives considered

- **Raise `job_lease_s` to hours.** Still kills on a long sleep and keeps the kill path that
  Slurm makes redundant.

## Acceptance criteria

- [x] Under Slurm, a job whose lease expired keeps running and writes `result.json`.
- [ ] A coordinator that resumes after > 900 s re-attaches and applies the result with no re-run.
      *Proven with fake clocks (a 20 min suspend) and on the box by re-attaching to a finished
      Slurm job whose lease had expired (As shipped); the real laptop suspend needs the deployed
      build with the box opted in to Slurm (pending).*
- [x] A job PENDING longer than `job_timeout_s` is not given up while it reports a pending reason
      and is younger than `max_pending_s`.
- [x] Under `local`, lease behaviour is unchanged.

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

## As shipped

**Code.** `worker.py`: `info` features gain `lease:none` when the configured executor has
`lease_cancels = False`. `workers.lease_free(cfg, name)` reads it from the cached `info`.
`remote_jobs.py`: `max_pending_s(cfg)` (`agentic.slurm.max_pending_s`, default 6 h),
`SleepClock` (wall-clock lead over the monotonic clock = time suspended, counted over 30 s).
`remote_gates.py`: `_run_async` sets a lease-free job's `deadline_at` to submit +
`max_pending_s` and records `lease: "none"` and `timeout_s`; `_Job.feed` re-arms the deadline on
the first `started` (`at` + `timeout_s` + `worker_ttl_s`, persisted); `_Job.past_deadline`
credits sleep to `last_seen` and replays a job that ended on the worker (`done` or `error`).
`remote_personas.py`: the same deadline rule, sleep credit and `_past_deadline` replay check.
Tests: `tests/test_slurm_lease.py` (fast tier, 14 tests).

**Deviations from the accepted Design** (recorded in Design above): no per-`slot_wait` re-arm (a
fixed `max_pending_s` bound from the submit); `poll --renew` still writes `lease.json` (it has no
effect under Slurm); the sleep credit on the worker TTL and the persona deadline replay were
added because without them a coordinator waking after `worker_ttl_s` gave up before polling.

**Real-binary run (box, as bruno, `git archive` of `a46ab2e9` in `/tmp/nw0513-e2e`, a temp data
and config dir with `executor: slurm`, `job_lease_s: 10`; never the `nw` user's install or the
live worker config; driver `e2e0513.py`):**
- Lease expiry while running, nobody polling (the sleeping coordinator): nw job
  `e2e-lease-89816`, Slurm job 29, a 45 s gate. `lease.json` `lease_until` passed 10 s after
  submit; the job ran on: frames `slot_wait {reason: Priority}`, `accepted`, `started`, `gate
  long ok`, `done ok` (duration 45.0 s); `result.json` `done`; `scontrol` `COMPLETED`, RunTime
  00:00:46. About 3 minutes after the lease expired, the coordinator's real `_follow` (through a
  local stand-in ssh that runs this build's `nw worker attach/poll`) re-attached to the lease-free
  record and applied the run (`ok`, gate `long` exit 0) in 0.2 s; no second Slurm job.
- Every job ended on its own (none left to `scancel`); the temp dirs were removed (`rm -rf
  /tmp/nw0513-run /tmp/nw0513-e2e`). SPEC-0515's runs (OOM, time limit, preemption) used the
  same temp build.

**Pending (needs the deployed worker build and the box opted in to Slurm: the human's decision):**
the Test plan's manual step: a nightwright campaign gate on the box under Slurm, the laptop
suspended 20 min (`systemctl suspend`), then `nw campaign show` shows the result applied with no
local re-run and one Slurm job. Until then the spec stays `in-progress`.

**Box gate: not run yet.** `nw gate run --project Meta-Tools --worktree <this worktree> --tier
loop --json` on `add38a6e` exited 3 ("box unavailable: no placeable gate worker"): the box worker
was `updating (draining, 1 jobs)` for 50+ minutes (01:03 to 01:32 at least), an update this lane
did not start and may not touch. Local runs: only this lane's new/touched test files
(`tests/test_slurm_lease.py`, `tests/test_gate_infra_outcomes.py`, `tests/test_executor_slurm.py`,
`tests/test_worker_slurm_check.py`), all green; `tools/test_tiers.py --check` and `spec_lint` pass.

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
