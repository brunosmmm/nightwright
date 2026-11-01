---
id: SPEC-0341
title: "Coordinator re-attach, job records and adoption after a restart"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, lease, reliability]
depends_on: [SPEC-0338, SPEC-0340]
---

## Context

[SPEC-0340](./0340-worker-async-job-protocol.md) makes remote jobs outlive the SSH
connection: the worker owns them. The coordinator does not use that yet. When the connection
drops, or the coordinator restarts (a code-change restart, SPEC-0231, or a crash), the
remote gates still die in the coordinator's eyes and are re-run locally. That is correct,
but it wastes the run.

This is the second part of the split of
[SPEC-0332](./0332-worker-owned-async-jobs.md). It is the **coordinator side** for gate
jobs: durable job records, idempotent frame application, re-attach with backoff, adoption
after a restart, and cancel. Retry on another worker is
[SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md); here a job that is
given up on runs locally, as in SPEC-0338.

Local persona runs are already adopted by the next tick (SPEC-0225); this mirrors that for
remote gate jobs.

## Goals / Non-goals

**Goals**
- The coordinator re-attaches to a remote gate job from where it left off, after a dropped
  connection or a coordinator restart.
- Applying frames is idempotent. Re-reading a frame never appends a duplicate event or log.
- The coordinator detects a lost worker or job and gives up on it (the gate then runs
  locally).
- A stop or an abandon cancels the remote job.
- A worker without the `async` feature keeps working through `gate-job`.

**Non-goals**
- Retry on a second worker, several workers at once, escalation (SPEC-0342).
- Bootstrap and drain (SPEC-0343).
- Persona jobs (SPEC-0344).

## Decision

**Amendment 2026-11-01:** `depends_on` changed from `[SPEC-0340]` to `[SPEC-0338, SPEC-0340]`
because Design, Gate step 1 rewrites `remote_gates.run` (SPEC-0338) and the Test plan uses
SPEC-0338's loopback transport, so SPEC-0338 is a hard prerequisite. The edge was missing
because SPEC-0340 depended on SPEC-0338 transitively; SPEC-0340 no longer does.

The coordinator records each remote job in a durable **job record**: the worker, the attempt
and the last applied `n`. It applies frames strictly in `n` order, so a re-attach after a
drop or a restart resumes at `n + 1`. The coordinator renews the job lease on the worker
(SPEC-0340) and gives up on a worker after a TTL.

## Design

**Job records.** Each remote job has a record at
`<agentic_dir>/remote-jobs/<job_id>.json`:

```
{schema: "nw.remote_job.v1", job_id, kind, campaign, worker, attempt, seq_or_run,
 cursor, state, submitted_at, last_seen_at, excluded_workers}
```

- `job_id` is `<campaign>-gate-<seq:03d>-a<attempt>` (one record per attempt; this spec only
  ever writes attempt 1, SPEC-0342 adds later attempts). Giving up sets that record's
  `state` to `lost` atomically **before** anything else is written for the gate, so the
  `(campaign, seq)` lookup returns the single non-final record with the highest attempt.
- It is written atomically after every applied batch of frames.
- Applying a frame and advancing `cursor` are ordered: write the frame's files and events
  first, then the record. A crash between the two re-applies the frame, so every
  application is idempotent:
  - files are rewritten with the same bytes;
  - events carry `event_uid = "<job_id>:<n>"`, and `store.append_event` skips an append
    whose `event_uid` already appears in the campaign ledger's last 200 entries. The final
    `gate` event carries `event_uid = "<job_id>:<n of done>"`; `finding`, `run_*` and
    `progress` events derived from frames carry their frame's `n`.
  - `cursor` guards side effects only. The in-memory `GateResult`s are rebuilt from every
    frame (see Adoption).

**Gate step.**
1. `remote_gates.run` (SPEC-0338) becomes `submit`, then `attach --from cursor`, in a loop.
2. When `attach` ends without `done` (the connection dropped), the step waits with backoff
   (2, 4, 8, ... up to 60 s) and re-attaches. Each re-attach is preceded by `poll --renew`,
   which also reports the job's state.
3. The step gives up when one of these holds:
   - the worker is unreachable for longer than `agentic.workers.worker_ttl_s` (default
     300 s);
   - the worker reports `lost`;
   - the overall deadline passes.
4. Giving up appends `worker_job_lost {worker, job_id, attempt, reason}` (a new event in
   `EVENT_TYPES`, informational for the conductor), sends a best-effort `cancel`, and runs
   the gates locally, as SPEC-0338's fallback does.
5. The `gates/` files of the abandoned attempt are deleted before the local run writes, as
   in SPEC-0338's fallback.

**Slot hold.** The step holds its worker slot across re-attaches. After a restart, adoption
re-acquires a slot in the record's worker pool (waiting if needed) before attaching.

**Adoption after a restart.** At startup, and on each tick, the queue reads
`remote-jobs/*.json` with `state` not final. A record whose campaign is in a gate step is
re-entered through the same gate step:
- `_do_gate` first looks up a live record for `(campaign, seq)`;
- if there is one, it re-acquires a slot in the record's worker pool, then attaches
  `--from 0` and rebuilds its `GateResult`s from every frame; side effects (file writes,
  events) for frames with `n <= cursor` are skipped.

**Cancel.** A SPEC-0229 stop sends `nw worker cancel` and then attaches until `done` (10 s
grace). An abandon (SPEC-0229) cancels all of the campaign's records.

**Old workers.** `nw workers check` warns (does not fail) when `info` lacks the `async`
feature, and the coordinator then uses `gate-job` for that worker, without re-attach or
adoption.

## Alternatives considered

- **Coordinator-side dedupe only, without `n`.** Matching on payloads is fragile. A
  monotonically increasing frame number per job is simple and total.
- **Resume at `cursor + 1` without rebuilding earlier results.** That loses the
  `GateResult`s of frames applied before a restart, so the adopted `GateRun` would be
  incomplete. The step re-reads every frame and skips only the side effects.

## Acceptance criteria

- [ ] Killing the coordinator's SSH process mid-job does not stop the job. The coordinator
      re-attaches and the final `gates/` files and events equal those of an uninterrupted
      run, with no duplicate events.
- [ ] A coordinator restart (a new process) during a remote gate job adopts the job from its
      record and completes the gate step without re-running it.
- [ ] An adopted gate step's final `GateRun` includes the results of frames applied before
      the restart.
- [ ] `store.append_event` skips an append whose `event_uid` is already in the last 200
      ledger entries.
- [ ] Killing the worker's job process yields `lost`, then `worker_job_lost`, then a local
      run of the gate step.
- [ ] A stop or an abandon cancels the remote job, and its processes end.
- [ ] A worker whose `info` lacks `async` gets a `check` warning and its gates run through
      `gate-job`.
- [ ] With no workers registered, behaviour is unchanged (SPEC-0338's golden test still
      passes).

## Test plan

- **Automated tests** (`tests/test_remote_job_records.py`, offline). They use SPEC-0338's
  loopback transport:
  - **drop:** the transport script is killed after k frames, then re-attach; assert no
    duplicate `event_uid` and the files are byte-equal to a clean run;
  - **restart:** the gate step is run in a subprocess and killed after `accepted`. A fresh
    tick adopts the job from `remote-jobs/` and finishes it;
  - **adoption replay:** kill after two of three gate frames; the adopted `GateRun` has three
    results and no duplicate events;
  - **lost:** the job's pid is killed, which gives `worker_job_lost` and a local run;
  - stop and abandon cancel the job;
  - `store.append_event` skips a duplicate `event_uid`;
  - an old worker (fake `info` without `async`) gates through `gate-job`, with a `check`
    warning.
- **Manual verification:**
  1. With the second machine registered, start a campaign gate on it.
  2. Restart the coordinator (`nw queue stop` / `run`) mid-gate. The gate completes without
     re-running; check the frames count in `nw worker jobs` on the worker.
  3. Reboot the worker mid-gate. Expect `worker_job_lost` and a local run.
- **Regression guard:** affected tests while iterating; SPEC-0338's tests run unchanged.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. Upgrade workers when convenient. `nw workers check` warns when a worker's `info` lacks
   the `async` feature.
2. Such a worker keeps working through `gate-job` (synchronous).
3. No data migration. `remote-jobs/` starts empty.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0332:
- `worker_ttl_s` defaults to 300 s.
- Frames are deduplicated by `n` and `event_uid`.
- Until SPEC-0342 lands, a given-up job runs locally at once.
