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
- Every runner exit (stop, code change, once/max_ticks, crash) leaves the remote job running
  for the next start to adopt; an abandon or a park cancels it.
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

The coordinator records each remote job in a durable **job record**: the worker, the attempt,
the gate seq, the last applied `n` and a deadline. It applies frames strictly in `n` order,
so a re-attach after a drop or a restart resumes at `n + 1`. The coordinator renews the job
lease on the worker (SPEC-0340) and gives up on a worker after a TTL. Runner exits detach
from remote gate jobs instead of killing them; only an abandon or a park cancels them.

## Design

**Job records.** Each remote job has a record at
`<agentic_dir>/remote-jobs/<job_id>.json`:

```
{schema: "nw.remote_job.v1", job_id, kind, campaign, worker, attempt, seq_or_run,
 cursor, state, submitted_at, deadline_at, last_seen_at, excluded_workers}
```

- `state` is one of `submitted`, `attached`, `detached` (non-final) or `done`, `lost`,
  `cancelled`, `local` (final; `local` means the gate fell back to a local run).
- `deadline_at` is `submitted_at` + the sum of the gates' `timeout_s` + `worker_ttl_s`,
  written at submit so it survives a restart.
- `job_id` is `<campaign>-gate-<seq:03d>-a<attempt>` (one record per attempt; this spec only
  ever writes attempt 1, SPEC-0342 adds later attempts). Giving up sets that record's
  `state` to `lost` atomically **before** anything else is written for the gate, so the
  campaign's live-record lookup (see Adoption) returns the single non-final record with the
  highest seq and attempt.
- The record is written atomically (temp file + rename) after every applied batch of frames.
- **Who writes what, and when.** Gate steps run on a worker thread and return a
  `StepResult`; `_apply` runs on the main thread.
  - The step (worker thread) writes the `gates/` files and advances `cursor` in the record
    as frames arrive: files first, then the record.
  - Ledger events derived from frames (`gate`, `finding`, `note`) are not written by the
    step. They are returned in the `StepResult` (`events_before`, `gate_run`) and written
    by `_apply` (main thread), each with its `event_uid`.
  - The record's `state` becomes `done` only in `_apply`, after the `gate` event is
    appended. A crash anywhere before that leaves the record non-final; adoption replays
    it, and `event_uid` dedupe drops anything already written.
- Every application is therefore idempotent:
  - files are rewritten with the same bytes;
  - events carry `event_uid = "<job_id>:<n>"`. The final `gate` event carries
    `event_uid = "<job_id>:<n of done>"`; `finding` and `note` events derived from frames
    carry `event_uid = "<job_id>:<n>"` of their frame.
  - `event_uid` is an optional ledger key (`schemas.EVENT_KEYS` allows extra keys, so no
    schema change is needed for it). `store.append_event` runs its dedupe scan only when
    the payload carries `event_uid`: it then skips the append if that `event_uid` already
    appears in the campaign ledger's last 200 entries. Payloads without `event_uid` are
    appended as today, with no scan.
  - `cursor` guards file side effects only. The in-memory `GateResult`s are rebuilt from
    every frame (see Adoption).

**Gate step.**
1. `remote_gates.run` (SPEC-0338) becomes `submit`, then `attach --from cursor`, in a loop.
2. When `attach` ends without `done` (the connection dropped), the step waits with backoff
   (2, 4, 8, ... doubling, capped at 60 s) and re-attaches. Each re-attach is preceded by
   `poll --renew`, which also renews the job lease and reports the job's state.
3. The step gives up when one of these holds:
   - the worker is unreachable (every call fails) for longer than
     `agentic.workers.worker_ttl_s` (default 300 s), measured from the record's
     `last_seen_at` (`reason: "unreachable"`);
   - the worker reports `lost` (`reason: "lost"`);
   - the gate step's deadline passes: the record's `deadline_at`, i.e. `submitted_at` + the
     sum of the gates' `timeout_s` + `worker_ttl_s` (`reason: "deadline"`).
4. Giving up appends a ledger event `worker_job_lost {worker, job_id, attempt, reason}`
   (added to `schemas.EVENT_KEYS`). `events.sync` projects it to a `worker_job_lost`
   stream event (added to `events.EVENT_TYPES`), informational for the conductor. The step
   then sends a best-effort `cancel` and runs the gates locally, as SPEC-0338's fallback
   does; the record's `state` goes `lost`, then `local` once the local run starts.
5. The `gates/` files of the abandoned attempt are deleted before the local run writes, as
   in SPEC-0338's fallback.

**Slot hold.** The step holds its worker slot across re-attaches. After a restart, adoption
re-acquires a slot in the record's worker pool (waiting if needed) before attaching. While
waiting for a slot, adoption sends `poll --renew` every `job_lease_s / 3` (SPEC-0340's
`agentic.workers.job_lease_s`, default 900 s). If the poll reports `lost` or `done`, it stops
waiting and proceeds without a slot: on `done` it only replays the frames (`attach --from 0`,
which needs no slot because nothing new runs); on `lost` it gives up as in Gate step 4.

**Adoption after a restart.** At startup, and on each tick, the queue reads
`remote-jobs/*.json` with a non-final `state`. A record whose campaign is in a gate step is
re-entered through the same gate step:
- `_do_gate` first looks up the campaign's live record (the non-final
  `remote-jobs/<campaign>-gate-*` record with the highest seq and attempt) **before**
  calling `next_gate_seq`. If there is one, the step reuses the record's `seq_or_run` as
  the gate seq; otherwise it allocates a new seq as today. (After a crash, `gates/NNN-*`
  files from applied frames already exist, so `next_gate_seq` would return N+1 and the job
  would be missed.)
- With a live record, it re-acquires a slot in the record's worker pool (see Slot hold),
  then attaches `--from 0` and rebuilds its `GateResult`s from every frame; file writes for
  frames with `n <= cursor` are skipped, and the events it returns are deduplicated by
  `event_uid` when `_apply` writes them.

**Runner exits.** On every SPEC-0229 runner exit (`signal`/`ctx.stop`, `code_changed`,
`once`/`max_ticks`, `crash`), an async remote gate step is **detached**: the step stops
attaching, leaves its record non-final (`state: detached`, or whatever non-final state a
crash left) and is left to adoption. It is not cancelled and not discarded, which amends
SPEC-0229's "a gate killed by a stop is discarded" rule for async remote gates only. Local
gates and `gate-job` (old-worker) gates keep SPEC-0229's behaviour.

**Cancel.** `nw queue abandon <campaign>` and a park that ends the gate step send
`nw worker cancel` for every non-final record of the campaign, attach until `done` (10 s
grace), then set the record's `state` to `cancelled`.

**Old workers.** The coordinator reads `features` from the worker's `info`, cached in the
registry (SPEC-0337) by `nw workers check` and refreshed at most once per tick; a worker with
no cached `info` is treated as lacking `async`. `nw workers check` warns (does not fail) when
`info` lacks the `async` feature, and the coordinator then uses `gate-job` for that worker,
without re-attach or adoption.

## Alternatives considered

- **Coordinator-side dedupe only, without `n`.** Matching on payloads is fragile. A
  monotonically increasing frame number per job is simple and total.
- **Resume at `cursor + 1` without rebuilding earlier results.** That loses the
  `GateResult`s of frames applied before a restart, so the adopted `GateRun` would be
  incomplete. The step re-reads every frame and skips only the side effects.
- **Cancel remote gates on a runner stop (SPEC-0229's rule as is).** That throws away the
  run on every code-change restart, which is the main case this spec exists for. Detaching
  and adopting costs nothing extra, because the worker owns the job.
- **Write ledger events from the step thread.** Every other step returns its events to
  `_apply`; breaking that would race the main thread on the ledger. The step keeps to files
  and the record.

## Acceptance criteria

- [ ] Killing the coordinator's SSH process mid-job does not stop the job. The coordinator
      re-attaches and the final `gates/` files and events equal those of an uninterrupted
      run, with no duplicate events.
- [ ] A coordinator restart (a new process) during a remote gate job adopts the job from its
      record and completes the gate step without re-running it; the adopted `gate` event's
      `gate_seq` equals the record's seq and no `gates/<seq+1>-*` file exists.
- [ ] A crash after `done` is read but before `_apply` writes the `gate` event leaves the
      record non-final; the next start replays it and writes exactly one `gate` event.
- [ ] An adopted gate step's final `GateRun` includes the results of frames applied before
      the restart.
- [ ] `store.append_event` skips an append whose `event_uid` is already in the last 200
      ledger entries, and appends payloads without `event_uid` unchanged.
- [ ] Killing the worker's job process yields `lost`, then a `worker_job_lost` ledger record
      and a `worker_job_lost` stream event, then a local run of the gate step.
- [ ] With the transport failing every call, the step retries with 2, 4, 8 … 60 s backoff,
      sends `poll --renew` before each re-attach, and gives up with
      `worker_job_lost {reason: "unreachable"}` once `worker_ttl_s` has passed.
- [ ] A job past its record's `deadline_at` is given up with `reason: "deadline"`.
- [ ] While adoption waits for a slot, it sends `poll --renew` every `job_lease_s / 3`, and
      stops waiting on `done` (replay only) or `lost` (give up).
- [ ] An abandon cancels the campaign's remote jobs and their processes end; a runner stop
      leaves them running and the next start adopts them.
- [ ] A `code_changed` runner exit during a remote gate leaves the record non-final and the
      job running; the restarted runner adopts it.
- [ ] A worker whose cached `info` lacks `async` (or has no cached `info`) gets a `check`
      warning and its gates run through `gate-job`.
- [ ] With no workers registered, behaviour is unchanged (SPEC-0338's golden test still
      passes).

## Test plan

- **Automated tests** (`tests/test_remote_job_records.py`, offline). They use SPEC-0338's
  loopback transport:
  - **drop:** the transport script is killed after k frames, then re-attach; assert no
    duplicate `event_uid` and the files are byte-equal to a clean run;
  - **restart:** the gate step is run in a subprocess and killed after `accepted`. A fresh
    tick adopts the job from `remote-jobs/` and finishes it; assert the adopted `gate`
    event's `gate_seq` equals the record's seq and no `gates/<seq+1>-*` file exists;
  - **restart before apply:** the subprocess is killed after the step has read `done` but
    before `_apply` runs (a test hook between the step's return and `_apply`); assert the
    record is still non-final, and the next tick writes exactly one `gate` event with the
    record's `event_uid` and sets `state: done`;
  - **adoption replay:** kill after two of three gate frames; the adopted `GateRun` has three
    results and no duplicate events;
  - **lost:** the job's pid is killed; assert the `worker_job_lost` ledger record, the
    `worker_job_lost` stream event after `events.sync`, and a local run;
  - **unreachable:** `worker_ttl_s: 3` and a fake clock or tiny backoff, with a transport
    that always fails; assert the backoff sequence (2, 4, 8 … capped at 60), the
    `poll --renew` calls before each re-attach, and `worker_job_lost {reason:
    "unreachable"}` followed by a local run;
  - **deadline:** a record whose `deadline_at` is in the past gives up with
    `reason: "deadline"`;
  - **slot wait:** the worker pool is full during adoption; with a small `job_lease_s`,
    assert `poll --renew` every `job_lease_s / 3`, and that a `done` poll ends the wait with
    a replay and a `lost` poll ends it with a give-up;
  - **runner exits:** `ctx.stop` and `code_changed` during a remote gate leave the record
    non-final and the worker job running; the next runner adopts it;
  - **abandon / park:** `nw queue abandon` and a park cancel the job, its process ends and
    the record's `state` is `cancelled`;
  - `store.append_event` skips a duplicate `event_uid` and appends payloads without one;
  - an old worker (cached `info` without `async`, and a worker with no cached `info`) gates
    through `gate-job`, with a `check` warning.
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
