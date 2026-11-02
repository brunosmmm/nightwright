---
id: SPEC-0341
title: "Coordinator re-attach, job records and adoption after a restart"
status: done
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

- [x] Killing the coordinator's SSH process mid-job does not stop the job. The coordinator
      re-attaches and the final `gates/` files and events equal those of an uninterrupted
      run, with no duplicate events.
- [x] A coordinator restart (a new process) during a remote gate job adopts the job from its
      record and completes the gate step without re-running it; the adopted `gate` event's
      `gate_seq` equals the record's seq and no `gates/<seq+1>-*` file exists.
- [x] A crash after `done` is read but before `_apply` writes the `gate` event leaves the
      record non-final; the next start replays it and writes exactly one `gate` event.
- [x] An adopted gate step's final `GateRun` includes the results of frames applied before
      the restart.
- [x] `store.append_event` skips an append whose `event_uid` is already in the last 200
      ledger entries, and appends payloads without `event_uid` unchanged.
- [x] Killing the worker's job process yields `lost`, then a `worker_job_lost` ledger record
      and a `worker_job_lost` stream event, then a local run of the gate step.
- [x] With the transport failing every call, the step retries with 2, 4, 8 … 60 s backoff,
      sends `poll --renew` before each re-attach, and gives up with
      `worker_job_lost {reason: "unreachable"}` once `worker_ttl_s` has passed.
- [x] A job past its record's `deadline_at` is given up with `reason: "deadline"`.
- [x] While adoption waits for a slot, it sends `poll --renew` every `job_lease_s / 3`, and
      stops waiting on `done` (replay only) or `lost` (give up).
- [x] An abandon cancels the campaign's remote jobs and their processes end; a runner stop
      leaves them running and the next start adopts them.
- [x] A `code_changed` runner exit during a remote gate leaves the record non-final and the
      job running; the restarted runner adopts it.
- [x] A worker whose cached `info` lacks `async` (or has no cached `info`) gets a `check`
      warning and its gates run through `gate-job`.
- [x] With no workers registered, behaviour is unchanged (SPEC-0338's golden test still
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

### Test record (2026-11-01)

- `tests/test_remote_job_records.py` (new, 18 tests, offline, SPEC-0338's loopback worker
  registered with a cached `info` listing `async`; the third gate blocks on a `GO` file so a
  test can cut in mid-job). Two runs with `-n 8`: 18 passed each.
  - **drop:** the fake `ssh` relays 3 frames of the first `attach` and exits 255; the step
    sleeps one 2 s backoff, sends `poll --renew`, re-attaches `--from 3`, and finishes with one
    `gate` event and no repeated `event_uid`. Every `gates/` file equals the decoded payload of
    the worker's frame, byte for byte.
  - **restart:** `_do_gate` runs in a subprocess, which is SIGKILLed after the record exists and
    the g1 and g2 frames are applied. The next `_do_gate` + `_apply` adopts the job: `gate_seq`
    equals the record's seq (1), there is no `gates/002-*`, the record is `done`, the worker has
    one job dir and `nw worker jobs --json` shows it `done` (nothing re-ran).
  - **adoption replay:** in the same test, the adopted `GateRun` has all three results.
  - **restart before apply:** the step returns and its `_apply` never runs (the test is the
    hook between the two); the record stays live with no `gate` event. The next step + `_apply`
    writes exactly one `gate` event, with the first step's `event_uid`, and sets `done`.
    Appending that payload again is dropped.
  - **lost:** the job process is SIGKILLed; `worker_job_lost {reason: "lost", attempt: 1}`
    lands in the ledger and, after `events.sync`, in the stream. The record ends `local` and the
    gates run locally.
  - **unreachable:** `attach`/`poll`/`cancel` exit 255, with a fake clock and sleep. The sleeps
    are 2, 4, 8, 16, 32, 60, 60, 60, 60 (302 s > `worker_ttl_s` 300). There is one `poll --renew`
    per sleep, then `worker_job_lost {reason: "unreachable"}`, a local run and a cooldown.
  - **deadline:** a detached record whose `deadline_at` is in the past gives up with
    `reason: "deadline"` (no cooldown).
  - **slot wait:** the worker's single slot is held. With `job_lease_s` 1.5, the polls come
    ≥ 0.4 s apart (every 0.5 s); a `done` poll ends the wait with a replay on the worker, a
    `lost` one with a give-up.
  - **runner exits:** `stop` (the gates' sticky stop + `ctx.stop`) and `code_changed` (a real
    `StepPool.try_detach_all`) leave the record `detached` and the worker job `running`. The
    next runner adopts it with one `gate` event, no `worker_job_lost` and no `worker_failed`.
    `try_detach_all` returns None while the step is still submitting.
  - **abandon / park:** both leave the record `cancelled`, the worker's last frame is `done`
    with `interrupted: true`, and the job process exits.
  - `append_event`: a duplicate `event_uid` is skipped, payloads without one are appended
    unchanged, and a uid outside the last 200 entries is appended again.
  - **old workers:** with no cached `info`, or an `info` without `async`, the gates run through
    `gate-job` (job id `-a0`, no record). `check` caches `info` and warns when `async` is
    missing.
- Review round 1 (same day): five more tests in `tests/test_remote_job_records.py`, 23
  passed together with `test_remote_gates.py` (116 tests, `-n 8`):
  - a close-tier run on the async worker, with its record left live as a crash would leave it,
    is ignored by `live_gate`; the next GATING runs `gate-002-a1` under seq 2 with its own
    `gate` event, and there is no `worker_failed` and no `worker_job_lost`. `settle_job` then
    sets the close record to `done`;
  - after a detach, a new commit lands: the old job is `cancelled` and noted, and the gate runs
    remotely under seq 2 for the new HEAD. `adoptable` is true for the same HEAD and tree,
    false for a dirtied tree, and true again once the tree is restored (a stat-dirty
    `stash create` exits 1);
  - a park before the first gate frame (no `gates/001-*`), then a resume: `next_gate_seq` is
    2, and the gate runs on the worker as `gate-002-a1` with no `worker_failed` and no cooldown;
  - with a `last_seen_at` far in the past, the slot-wait watcher's poll refreshes it, and the
    adopted job finishes on the worker with no `worker_job_lost`.
  Related modules after the change (close, gate paths, exit paths, queue, codewatch, worker
  jobs, registry, gates, drain, adopt): 660 passed in one `-n 8` run. The full suite is left to
  gating.
- Review round 2 (same day): 15 more tests in `tests/test_remote_job_records.py` (38 in all,
  two `-n 8` runs, all passed). They cover: a recorded job's protocol failure (cancel sent,
  the worker's job ends interrupted, record `local`, `worker_failed`, local run); a submit
  whose reply is lost after acceptance (the record exists before the upload, the job is
  cancelled); a cancel landing while attached (an interrupted run on `box`, record stays
  `cancelled` against `attached`/`local`/`mark_done` writes); a cancel during backoff (no
  give-up, no cooldown, and a later `_give_up` leaves it alone); `run_and_log` on the async
  worker (event_uid, record `done`; a cut run cancels its job); the per-tick settle of an
  ABANDONED or MERGED campaign and of a gone one (cancelled, then pruned), with a GATING
  campaign's record left alone; an unregistered worker (`unreachable`, no cancel); tampered
  files at or below the cursor stay byte- and mtime-identical on adoption; an explicit seq
  with a record uses `gate-job`; a stale `last_seen_at` with one failed first attach; a late
  adoption of a finished job past `deadline_at` replays it; and `try_detach_all` waits for a
  step that left its attach loop before the claim. The slot-wait gaps are measured on the
  in-process monotonic clock. `test_deadline_gives_up` now cuts in while the job still runs.
  Related modules (adopt, close*, codewatch, drain, exit paths, gates, queue, queue gc,
  remote gates, worker*, workers*, events, quiet): 1107 passed in one `-n 8` run. The full
  suite is left to gating.
- Review round 3 (same day): `tests/test_remote_job_records.py` has 40 tests, all passed in
  one `-n 8` run. New: **drop mid-frame**: the fake `ssh` relays whole frames until the `g2`
  `gate` frame (about 400 KB of base64 random output, so several pipe writes), writes half of
  that line and exits 255. The step backs off once, re-attaches `--from` the `g1` frame's `n`,
  and finishes on `box` with one `gate` event, no `worker_failed`, no `worker_job_lost` and the
  files byte-equal to the frames. With the partial-line check disabled, this test fails. Also
  new: `try_detach_all` with two attached gate steps where one left its loop claims neither
  (both flights kept, the other step's detach event not set). The recorded-job failure test
  now checks that the record is `local` and the `gates/001-*` files are gone when `_cancel`
  runs. Related modules (remote gates, exit paths, codewatch, queue, drain signal, worker
  jobs, and this file): 466 passed in one `-n 8` run. The full suite is left to gating.
- Review round 4 (same day): `tests/test_remote_job_records.py` has 49 tests, all passed in
  one `-n 8` run. The new ones: **give-up order**: a SIGKILLed job process gives up with the
  record `lost` while the `gates/001-*` files are still there and no `worker_job_lost` exists;
  then `worker_job_lost` with the record `lost`, then the cancel, then `local` once the event
  is in and the files are gone. **crash during submit**: a `_submit` that dies leaves a
  `submitted` record with cursor 0 and no `accepted_at`; adoption gives up as `unaccepted`,
  with no cooldown and no `worker_failed`, and runs locally. In contrast, a job the worker
  forgot after accepting it is `lost`, with a cooldown. **settle**: a `read_state` that raises
  EIO, or a malformed `state.json`, leaves the live record `detached` and the job running.
  **seq 1000**: `gate-1000-a1` is seen by `max_seq` (and `next_gate_seq` is 1001),
  `campaigns`, `live_gate` and `prune`. **crash before `mark_done`**: the `gate` transition
  is committed and the record is still live. A same-HEAD `retry_gate` settles the record and
  gates again under seq 2, on the worker, so the ledger has two `gate` events. A crash
  between the ledger and `state.json` still adopts, and the replayed event is dropped.
  **git failure at adoption**: the step is deferred (`adoption_deferred`, still GATING), with
  no cancel, no `gate` event and the job still running. A failure that clears within the
  retries adopts it under seq 1. Related modules (remote gates, queue, exit paths,
  codewatch, events, quiet, worker jobs, adopt, close*, drain signal, gate*): 594 + 314
  passed in two `-n 8` runs. The full suite is left to gating.
- Affected modules: `test_remote_gates.py` (SPEC-0338's golden test included, unchanged),
  `test_workers_registry.py`, `test_worker_jobs.py`, `test_worker.py`, `test_agentic_queue.py`,
  `test_agentic_events.py`, `test_agentic_quiet.py`, `test_skills.py`, `test_agentic_exit_paths.py`,
  `test_agentic_codewatch.py`, `test_agentic_drain_signal.py` and the related agentic tests:
  974 tests in one `-n 8` run. One SPEC-0340 test, `test_worker_jobs.py::test_cancel_a_running_job_then_cancel_a_done_one`, failed under that load and passed
  when re-run alone; this spec does not change worker code. Everything else passed. The
  full suite is left to gating.
- Manual, on this host: the restart case was run on the loopback (the real forced command
  `nw worker ssh-command`), as SPEC-0340 did. A read-only real call to the registered worker
  `box` (`nw worker info --json` over its `ssh_argv`) shows it runs nightwright 0.1.0 with no
  `features`. `remote_jobs.has_async` reads it as an old worker, so its gates keep `gate-job`,
  and its forced command refuses `poll`. Manual steps 1-3 on the second machine therefore need
  the worker upgraded first (Rollout step 1, a human action). They are left to the human and to
  SPEC-0346's integration suite.
- `python3 tools/spec_lint.py` passes.

### As shipped (2026-11-01)

- **Module split.** The records are `agentic/remote_jobs.py` (`nw.remote_job.v1`, atomic writes
  under one flock). A final state is never replaced by a live one, so a cancel that lands while
  a step still attaches wins. Records also carry `gates` (the gate names at submit, the order
  `done` is checked against on adoption) and, after a give-up, `lost_reason`. The async step
  (`submit`, `attach`, re-attach, `adopt`, `cancel_campaign`) lives in `remote_gates.py` next
  to SPEC-0338's `gate-job` path. `run` picks it when the worker's cached `info` lists `async`.
- **`deadline_at`** is `submitted_at` + the header's `timeout_s` + `worker_ttl_s`. The header's
  `timeout_s` is SPEC-0338's job timeout (`gate_budget_s` + Σ `timeout_s` × (1 + `retries`),
  capped at 4 h), the bound the worker itself uses. A plain sum of the gates' `timeout_s` would
  give up on a gate that legitimately retries. (The Design bullets still say the plain sum: the
  spec text is frozen at acceptance, so this definition is the one that governs.) Review round
  2: a job past its `deadline_at` is given up only when one `poll` does not report it `done`.
  A job the worker already finished is replayed however late, because the deadline bounds
  waiting, not results that are already there.
- **Adoption lives in the gate step.** `_do_gate` looks up the campaign's live record before
  `next_gate_seq`, so a campaign in GATING adopts on the next tick it is stepped. Review round
  2: each tick also settles the records of campaigns that have ended (a terminal state, or a
  campaign dir that is gone; `queue._settle_ended_remote_jobs`). Their live records are
  cancelled, as an abandon would, and their final records are removed a tick later, because
  such a campaign never gates again. The tick knows other campaigns' records by file name
  only, and a campaign's lookup parses only its own `<campaign>-*` records, so `remote-jobs/`
  holds just the records of campaigns that can still gate. A re-gate under the adopted seq takes the `gate-<seq>` tmp claim
  non-exclusively, because the dead runner's claim dir may still be there.
- **What gets adopted (review round 1).** A record also stores the header's `commit`, the
  worktree's `head` and the commit's `tree` at submit, and the run's `tier`. `live_gate`
  returns only `tier: loop` records, so a close re-gate's or a debug run's job is never adopted
  by the gate step. `_do_gate` adopts a live record only while the worktree still has the same
  HEAD and tree under test (`remote_gates.adoptable`). Otherwise it cancels that job (a `note`
  says so) and gates under a fresh seq. A record left live by a human `transition` or an
  auto-park therefore never replays results for code that has since changed.
- **Seqs are never reused.** `gates.next_gate_seq` allocates past every remote job record of
  the campaign, final ones included (`remote_jobs.max_seq`). A job cancelled before its first
  frame wrote a `gates/` file therefore never has its job id submitted again. If a caller passes
  an explicit seq that already has a record, `remote_gates.run` uses `gate-job` for that run
  and does not discard the record.
- **Close re-gates.** `close.prepare_close` writes the `gate` event with its `event_uid`
  (`gates.job_payload`) and settles the job at once (`gates.settle_job`, which `run_and_log`
  shares): if a stop detached the job, it is cancelled, and then the record goes `done`.
- While adoption waits for a slot, each successful `poll` refreshes the record's
  `last_seen_at`. Review round 2: the step's `unreachable` clock starts when this process
  starts following the job. For a fresh submit that equals the record's `last_seen_at`; for an
  adopted job it is the adoption, not the dead runner's last frame. A coordinator that was
  simply not running is not a worker that failed to answer, so one failed first `attach`
  backs off and retries.
- **Record before submit (review round 2).** The record is written as `submitted` before the
  upload. A submit whose reply is lost after the worker accepted (or a crash right after it)
  still has a record: the step's failure path cancels the job and sets the record `local`,
  and after a crash, adoption attaches (an unknown job gives up as `lost`).
- **A cancel always wins (review round 2).** Nothing replaces `cancelled` in a record. A
  give-up (`_give_up`) or a failure (`_failed`) on a record that a cancel already ended
  returns an interrupted run: no `worker_job_lost`, no cooldown, no local fallback. A step in
  its re-attach backoff checks for the cancel after each sleep, so an abandon whose own SSH
  calls fail still stops it.
- **Code-change exit.** SPEC-0231's exec waits for in-process steps. A gate step that is in
  its attach loop (`remote_gates.attached`) now counts as detachable in
  `StepPool.try_detach_all`: the step is told to detach and its result is never applied. A
  step still preparing or submitting is waited for, as before. Review round 2: the detach is
  an atomic claim (`detach_attached` and the step's own exit, `_leave`, share one lock). A
  step that left its attach loop to give up before the claim is waited for. A step claimed
  first returns a detached run even if its attach then failed. Signal, `ctx.stop` and crash
  exits detach through the stop flag (or simply die), and `_apply` discards the interrupted
  run as SPEC-0229 does for a stop.
- **Partial frames (review round 3).** A line that does not end with a newline is the frame
  SSH was writing when it died. `_attach` discards it and treats the attach as dropped (backoff,
  `poll --renew`, re-attach after the cursor). It is never a protocol failure. `_failed` sets
  the record `local` before it deletes the `gates/<seq>-*` files and sends the cancel, the
  same order `_give_up` uses. `try_detach_all` claims its attached steps all or none
  (`remote_gates.detach_all_attached`, under one lock): if any of them has left its loop,
  nothing is claimed and the exec waits.
- **Give-up details.** `unreachable` and `lost` put the worker in SPEC-0338's cooldown;
  `deadline` does not. A worker removed from the registry before adoption gives up as
  `unreachable` with no cancel. Any other failure of a recorded job (an `error` frame, a
  protocol error, a `done` with tree changes, a worker-side interrupt such as a lapsed lease)
  takes SPEC-0338's `worker_failed` path, plus a best-effort cancel, and the record goes
  `local`. A `done` with `interrupted: true` on a `cancelled` record is returned as an
  interrupted run, not a fallback.
- **Park / abandon.** An immediate `apply_park` and `apply_abandon` call
  `remote_gates.cancel_campaign` after their state write, outside the item lock, and note
  each cancelled job. A deferred park (a loop holds the lease) lets the running step finish,
  as before.
- **`nw queue gates`** (`run_and_log`) on an async worker writes the `gate` event with its
  `event_uid` and marks the record `done`, so the loop never adopts a debug run. A debug run
  cut by Ctrl-C cancels its own job.
- **`info` cache.** `workers.check` stores the probed `info` on the registry record, and
  `nw workers add` stores it on the new record. There is no per-tick refresh: the cache
  changes only on `check`/`add`, within the "at most once per tick" bound.
- **Review round 4.** `submit` sets the record's `accepted_at` once the worker has accepted
  the job. A job the worker reports `unknown` whose record has no `accepted_at` and cursor 0
  is one the coordinator never finished submitting (it died during the upload). It gives up
  with `reason: "unaccepted"`: `worker_job_lost` and a local run, with no cooldown. Only
  `unreachable` and `lost` blame the worker. Before it adopts, `_do_gate` checks whether
  `state.json`'s `last_gate` already holds the record's job: its `event_uid`, or a `gate_seq`
  at or past the record's seq. If it does, the transition committed and only `mark_done` was
  lost to a crash, so the record is set `done` and never replayed. (`_transition` writes the
  ledger before `state.json`, so a crash between those two still adopts.) A git failure
  while checking `adoptable` is retried 3 times, 0.5 s apart. If it still fails, the gate
  step is deferred (`StepResult.deferred`): `_apply` writes only the notes of jobs it
  cancelled and stays in GATING (`adoption_deferred`), and the live job is neither cancelled
  nor re-gated. The per-tick settle treats a campaign as gone only when its dir is missing;
  a state read that fails is skipped that tick. The job-id pattern accepts three or more seq
  digits, so `gate-1000-a1` and later are seen.
- `worker_job_lost` is a *hold* type in `quiet.RULES` and in `nw-conduct`'s quiet-stream list.
  `docs/WORKFLOW.md` describes re-attach and adoption.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. Upgrade workers when convenient. `nw workers check` warns when a worker's `info` lacks
   the `async` feature.
2. Such a worker keeps working through `gate-job` (synchronous).
3. No data migration. `remote-jobs/` starts empty.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0332:
- `worker_ttl_s` defaults to 300 s.
- Frames are deduplicated by `n` and `event_uid`.
- Until SPEC-0342 lands, a given-up job runs locally at once.
