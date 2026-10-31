---
id: SPEC-0332
title: "Worker-owned async jobs"
status: accepted
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, lease, reliability]
depends_on: [SPEC-0338]
---

## Context

In [SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md), the SSH connection is the
job's lifetime. When the connection drops, or the coordinator restarts (a code-change
restart, SPEC-0231, or a crash), the remote gates die and are re-run locally. That is
correct, but it wastes the run. It also blocks two things:
- persona runs ([SPEC-0333](./0333-remote-persona-runs-with-bundle-write-back.md)), which
  last tens of minutes and cost money;
- retrying on a second worker.

Local persona runs already survive a runner restart. They are detached wrapper processes
with `proc.json` (SPEC-0225, `runproc.py`), and the queue adopts them on the next tick.
Remote jobs need the same property.

## Goals / Non-goals

**Goals**
- A remote job is owned by the worker. It keeps running when the SSH connection or the
  coordinator goes away, and the coordinator re-attaches from where it left off.
- Applying frames is idempotent. Re-reading a frame never appends a duplicate event or log.
- Each side detects that the other is gone:
  - the coordinator detects a lost worker or job, and retries the job with a new attempt on
    another worker (or locally, for gates);
  - the worker detects an abandoned job and cancels it.
- Several workers are used at once.
- `nw workers bootstrap` installs nightwright on a new worker, and `nw workers drain` takes
  one out of service gracefully.

**Non-goals**
- Persona jobs (SPEC-0333; this spec is the substrate they ride on).
- Pull workers and any HTTP API ([SPEC-0334](./0334-pull-workers-against-a-coordinator-job-api.md)).
  The transport stays SSH with the forced command.
- Load-aware scheduling. "First free slot in registry order" plus affinity (SPEC-0333) is
  enough at this scale.

## Decision

Split the synchronous `gate-job` into four worker subcommands:
- **`submit`** claims the job, spools the bundle, starts a detached job process and returns
  at once;
- **`attach`** streams the job's spooled frames from a cursor and follows them until `done`;
- **`poll`** returns the frames after a cursor once, without following;
- **`cancel`** stops the job.

The job process writes every frame to `frames.jsonl` with a sequence number `n`.

The coordinator records each remote job in a durable **job record**: the worker, the attempt
and the last applied `n`. It applies frames strictly in `n` order, so a re-attach after a
drop or a restart resumes at `n + 1`.

Liveness uses leases in both directions:
- the coordinator renews a **job lease** on the worker;
- the worker heartbeats the job.

Each side gives up on the other after its TTL.

## Design

### Worker side

`jobs/<job_id>/` (the store from SPEC-0336) gains:
- `header.json` and `bundle` (spooled);
- `proc.json` (the detached job process, claimed with `O_EXCL` as in `runproc.claim`);
- `frames.jsonl`;
- `heartbeat.json` (`{ts, pid}`, rewritten every 10 s by the job process);
- `lease.json` (`{lease_until}`, renewed by the coordinator);
- `result.json` (written last, atomically).

Subcommands, all reachable through `nw worker ssh-command`:
- **`nw worker submit`**
  - It reads the header and the bundle from stdin, as `gate-job` does, and validates them
    the same way.
  - It claims the job dir and spawns `python -m nightwright.agentic.worker_job <job_dir>`
    with `start_new_session=True`.
  - It prints `{job_id, state: "accepted", n: 0}` and exits.
  - A resubmit of the same `job_id` whose header digest (sha256 of the canonical header) is
    equal is **idempotent**: it prints the current state and does not start a second
    process. A digest mismatch is an `error` with `stage: "claim"`.
- **`nw worker attach JOB --from N`** prints every frame with `n > N`, then follows the file
  (polling every 0.5 s) until `done` or `error` has been printed, and exits 0. While it is
  connected it renews the job lease on every follow tick (every 0.5 s poll, `lease.json`
  rewritten at most every 30 s), whether or not a new frame was read, so a long silent gate
  is never cancelled while attached. `attach` never reads stdin and never cancels on EOF.
- **`nw worker poll JOB --from N [--renew]`** prints the frames after `N` once, plus a
  trailing `{"type": "status", state, heartbeat_age_s}` line. `--renew` extends the lease.
  `poll` also reports `lost` when `heartbeat_age_s > 60` even if the pid is alive (a hung job
  process).
- **`nw worker cancel JOB`** writes `cancel` into the job dir. The job process sees it
  within 1 s and calls `gates.terminate_all()`. The process then emits `done` with
  `interrupted: true`.
- **`nw worker jobs [--json]`** lists the jobs with their state.
- **`nw worker gate-job`** stays: `submit` plus `attach --from 0` in one process. It costs
  nothing and keeps SPEC-0338's tests and `nw queue gates --worker` working. It keeps
  SPEC-0336's stdin contract: EOF on stdin before `done` writes `cancel` into the job dir.
- The `ssh-command` allowlist becomes `info|have|gate-job|gc|submit|attach|poll|cancel|jobs`;
  `JOB` must match `[A-Za-z0-9._-]+` and `--from` a non-negative integer.
- `info` reports `protocol: "nw.job.v1"` and `features: ["async"]`.

Job states, as derived by `poll` and `jobs`:
- `accepted`, `running` and `done` follow from the files;
- `lost` means `proc.json` exists, there is no `result.json`, and the pid (with its start
  ticks) is dead.

The lease is `agentic.workers.job_lease_s`, default 900 s. A job whose `lease_until` passes
without a renewal is cancelled by its own job process: nobody is listening to it any more.
`nw worker gc` deletes finished and lost jobs after `--keep-days`.

### Coordinator side

**Job records.** Each remote job has a record at
`<agentic_dir>/remote-jobs/<job_id>.json`:

```
{schema: "nw.remote_job.v1", job_id, kind, campaign, worker, attempt, seq_or_run,
 cursor, state, submitted_at, last_seen_at, excluded_workers}
```

- `job_id` is `<campaign>-gate-<seq:03d>-a<attempt>` (one record per attempt). Giving up
  sets that record's `state` to `lost` atomically **before** the next attempt's record is
  written, so the `(campaign, seq)` lookup returns the single non-final record with the
  highest attempt.

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
4. Giving up appends `worker_job_lost {worker, job_id, attempt, reason}`, sends a best-effort
   `cancel`, and retries with `attempt + 1` on the next eligible worker that is not in
   `excluded_workers`.
5. After `agentic.workers.max_remote_attempts` (default 2), the job runs locally. Retries
   append `worker_job_retried {job_id, from_worker, to_worker, attempt}`.
6. The `gates/` files of an abandoned attempt are deleted before the next attempt writes, as
   in SPEC-0338's fallback.

**Adoption after a restart.** At startup, and on each tick, the queue reads
`remote-jobs/*.json` with `state` not final. A record whose campaign is in a gate step is
re-entered through the same gate step:
- `_do_gate` first looks up a live record for `(campaign, seq)`;
- if there is one, it re-acquires a slot in the record's worker pool (waiting if needed),
  then attaches `--from 0` and rebuilds its `GateResult`s from every frame; side effects
  (file writes, events) for frames with `n <= cursor` are skipped.
- A retry releases the current worker's slot and acquires one in the next eligible pool,
  excluding `excluded_workers`.

This mirrors how detached persona runs are adopted (SPEC-0225).

**Several workers.** The slot pools of SPEC-0337 already cover N workers. The only change
is that `excluded_workers` removes a worker from one job's pool list.

**Cancel.** A SPEC-0229 stop sends `nw worker cancel` and then attaches until `done`
(10 s grace). An abandon (SPEC-0229) cancels all of the campaign's records.

**Events.** `worker_job_lost` and `worker_job_retried` join `EVENT_TYPES`. Both are
informational for the conductor, except that three `worker_job_lost` for one worker within
3600 s raise one `escalation` (raised by the queue, as other escalations are) with options
`off` (sets the worker `enabled: false`) and `keep` (no change).

**Old workers.** `nw workers check` warns (does not fail) when `info` lacks the `async`
feature, and the coordinator then uses `gate-job` for that worker, without re-attach or
adoption.

### Bootstrap and drain

- **`nw workers bootstrap NAME --ssh DEST [--slots N]`**
  1. It builds a wheel of the coordinator's checkout (`uv build --wheel`) and copies it
     with `scp`.
  2. It runs `uv tool install --force <wheel>` over a normal (not forced-command) SSH
     session. This needs the human's own key to the worker account.
  3. It runs `nw worker info` to check the install.
  4. It prints the forced-command `authorized_keys` line, built from the key the coordinator
     uses for workers (`agentic.workers.key`, default `~/.ssh/id_ed25519.pub`).
  5. With `--install-key` it calls `nw workers add` (with check); without it, it calls
     `nw workers add --no-check` and prints `run nw workers check NAME after installing the
     key line`.

  `--install-key` appends the key line to `~/.ssh/authorized_keys` on the worker, unless the
  line is already present.
- **`nw workers drain NAME`** stops new jobs from being placed on the worker
  (`draining: true`, added to the registry schema). Its running jobs finish. `nw workers
  list` shows `draining (k jobs)`. On each tick the queue sets `enabled: false` and clears
  `draining` for a draining worker with no non-final `remote-jobs` record.

## Alternatives considered

- **Keep synchronous SSH and just retry.** This wastes whole gate runs and makes persona
  runs on workers unaffordable, because a 30-minute run would restart on every drop.
- **A daemon on the worker (`nw worker serve` listening).** That is a second long-lived
  process and a port to secure. Detached job processes plus the forced command need neither.
  Pull workers come later (SPEC-0334), for hosts that cannot be reached over SSH.
- **Coordinator-side dedupe only, without `n`.** Matching on payloads is fragile. A
  monotonically increasing frame number per job is simple and total.
- **No worker-side lease.** A coordinator that dies for good would leave remote jobs
  running until their timeout. The lease bounds the waste at 15 min.

## Acceptance criteria

- [ ] `nw worker submit` returns before the gates run. A resubmit with the same header is
      idempotent, and one with a different header is refused.
- [ ] Killing the coordinator's SSH process mid-job does not stop the job. The coordinator
      re-attaches and the final `gates/` files and events equal those of an uninterrupted
      run, with no duplicate events.
- [ ] A coordinator restart (a new process) during a remote gate job adopts the job from its
      record and completes the gate step without re-running it.
- [ ] Killing the worker's job process yields `lost`, then `worker_job_lost`, then a retry
      with `attempt + 1` on a second worker. After `max_remote_attempts`, the job runs
      locally.
- [ ] A job whose lease is not renewed for `job_lease_s` cancels itself on the worker.
- [ ] With two workers registered, concurrent campaigns use both.
- [ ] A stop or an abandon cancels the remote job, and its processes end.
- [ ] `nw workers bootstrap` installs and registers a worker. `nw workers drain` lets the
      running jobs finish, then disables the worker.
- [ ] With no workers registered, behaviour is unchanged (SPEC-0338's golden test still
      passes).
- [ ] A single gate that runs longer than `job_lease_s` while attached completes and is not
      cancelled.
- [ ] An adopted gate step's final `GateRun` includes the results of frames applied before
      the restart.
- [ ] Three `worker_job_lost` for one worker within 3600 s raise one `escalation` with
      options `off` and `keep`.
- [ ] A worker whose `info` lacks `async` gets a `check` warning and its gates run through
      `gate-job`.
- [ ] `nw worker jobs --json` lists accepted, running, done and lost states; `nw worker gc
      --keep-days 0` removes done and lost job dirs. `ssh-command` admits the new
      subcommands and refuses bad `JOB` or `--from` values.

## Test plan

- **Automated tests** (`tests/test_worker_jobs.py`, offline). They use SPEC-0338's loopback
  transport, with two loopback workers (two temp data dirs):
  - submit, attach to `done`, and the resubmit cases (same header, different header);
  - **drop:** the transport script is killed after k frames, then re-attach; assert no
    duplicate `event_uid` and the files are byte-equal to a clean run;
  - **restart:** the gate step is run in a subprocess and killed after `accepted`. A fresh
    tick adopts the job from `remote-jobs/` and finishes it;
  - **lost:** the job's pid is killed, which gives `worker_job_lost` and a retry on worker 2;
    with both excluded, the run is local;
  - **lease expiry:** `job_lease_s=2`, no renewals, and the job process cancels itself. The
    test waits with `tests/waits.py`;
  - stop and abandon cancel the job;
  - two campaigns against two workers place one job on each;
  - **bootstrap:** the ssh/scp/uv calls go through a recording fake `ssh_argv`; assert the
    sequence, plus that `--install-key` is idempotent;
  - **drain:** a draining worker gets no new job, its running job finishes, and the worker
    then becomes disabled;
  - `store.append_event` skips a duplicate `event_uid`;
  - **long attached gate:** `job_lease_s=2` and a gate sleeping 5 s while attached completes;
  - **adoption replay:** kill after two of three gate frames; the adopted `GateRun` has three
    results and no duplicate events;
  - **hung job:** a job process that stops heartbeating reports `lost` via `poll`;
  - three `worker_job_lost` in an hour give one `escalation`;
  - an old worker (fake `info` without `async`) gates through `gate-job`;
  - `jobs --json`, `gc --keep-days 0`, and the extended `ssh-command` allowlist.
- **Manual verification:**
  1. With the second machine registered, start a campaign gate on it.
  2. Restart the coordinator (`nw queue stop` / `run`) mid-gate. The gate completes without
     re-running; check the frames count in `nw worker jobs` on the worker.
  3. Reboot the worker mid-gate. Expect `worker_job_lost` and a local run.
- **Regression guard:** the full `uv run pytest`, including SPEC-0338's tests unchanged.

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

None. Decided while writing this spec:
- The lease defaults are `job_lease_s` 900 s and `worker_ttl_s` 300 s.
- A job gets 2 remote attempts before it runs locally.
- Frames are deduplicated by `n` and `event_uid`.
- Bootstrap installs a wheel of the coordinator's own checkout, so both run the same code.
- `gate-job` is kept as a compatibility path.
