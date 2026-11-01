---
id: SPEC-0340
title: "Worker-side async job protocol: submit, attach, poll, cancel"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, lease, reliability]
depends_on: [SPEC-0338]
supersedes: [SPEC-0332]
---

## Context

In [SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md), the SSH connection is the
job's lifetime. When the connection drops, the remote gates die. Local persona runs already
survive a runner restart: they are detached wrapper processes with `proc.json` (SPEC-0225,
`runproc.py`). A remote job needs the same property.

This is the first part of the four-way split of
[SPEC-0332](./0332-worker-owned-async-jobs.md) (its spec review recommended a split). It is
the **worker side only**: the commands that run on the worker, with no change to the
coordinator. The parts are:
- SPEC-0340 (this spec): the worker-side protocol;
- [SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md): the coordinator
  re-attaches, keeps job records and adopts jobs after a restart;
- [SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md): retry on another
  worker and escalation;
- [SPEC-0343](./0343-worker-bootstrap-and-drain.md): `nw workers bootstrap` and `drain`.

## Goals / Non-goals

**Goals**
- A remote job is owned by the worker. It keeps running when the SSH connection goes away,
  and a client can re-attach from a cursor.
- The worker detects an abandoned job (no lease renewal) and cancels it.
- A client can tell a hung or dead job process (`lost`) from a running one.
- `nw worker gate-job` keeps working unchanged.

**Non-goals**
- Anything on the coordinator: job records, re-attach, adoption, retry
  (SPEC-0341, SPEC-0342).
- Persona jobs ([SPEC-0344](./0344-remote-persona-jobs-with-bundle-write-back.md)).
- Pull workers and any HTTP API ([SPEC-0334](./0334-pull-workers-against-a-coordinator-job-api.md)).
  The transport stays SSH with the forced command.

## Decision

Split the synchronous `gate-job` into four worker subcommands:
- **`submit`** claims the job, spools the bundle, starts a detached job process and returns
  at once;
- **`attach`** streams the job's spooled frames from a cursor and follows them until `done`;
- **`poll`** returns the frames after a cursor once, without following;
- **`cancel`** stops the job.

The job process writes every frame to `frames.jsonl` with a sequence number `n`. Liveness
uses a lease in the worker: the client renews a **job lease**, and the worker cancels a job
whose lease expires.

## Design

`jobs/<job_id>/` (the store from SPEC-0336) gains:
- `header.json` and `bundle` (spooled);
- `proc.json` (the detached job process, claimed with `O_EXCL` as in `runproc.claim`);
- `frames.jsonl`;
- `heartbeat.json` (`{ts, pid}`, rewritten every 10 s by the job process);
- `lease.json` (`{lease_until}`, renewed by the client);
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
- **`nw worker gate-job`** stays: `submit` plus `attach --from 0` in one process. It keeps
  SPEC-0336's stdin contract: EOF on stdin before `done` writes `cancel` into the job dir.
  It keeps SPEC-0338's tests and `nw queue gates --worker` working.
- The `ssh-command` allowlist becomes `info|have|gate-job|gc|submit|attach|poll|cancel|jobs`;
  `JOB` must match `[A-Za-z0-9._-]+` and `--from` a non-negative integer.
- `info` reports `protocol: "nw.job.v1"` and `features: ["async"]`.

Job states, as derived by `poll` and `jobs`:
- `accepted`, `running` and `done` follow from the files;
- `lost` means `proc.json` exists, there is no `result.json`, and the pid (with its start
  ticks) is dead, or the heartbeat is older than 60 s.

The lease is `agentic.workers.job_lease_s`, default 900 s. A job whose `lease_until` passes
without a renewal is cancelled by its own job process: nobody is listening to it any more.
`nw worker gc` deletes finished and lost jobs after `--keep-days`.

## Alternatives considered

- **Keep synchronous SSH and just retry.** This wastes whole gate runs and makes persona
  runs on workers unaffordable.
- **A daemon on the worker (`nw worker serve` listening).** That is a second long-lived
  process and a port to secure. Detached job processes plus the forced command need neither.
  Pull workers come later (SPEC-0334).
- **No worker-side lease.** A coordinator that dies for good would leave remote jobs
  running until their timeout. The lease bounds the waste at 15 min.
- **Renew the lease per frame.** A single gate can run longer than `job_lease_s`, so a
  frame-driven renewal would cancel a long silent gate. `attach` renews on every tick.

## Acceptance criteria

- [ ] `nw worker submit` returns before the gates run. A resubmit with the same header is
      idempotent, and one with a different header is refused (`stage: "claim"`).
- [ ] `nw worker attach JOB --from N` prints the frames after `N` and follows to `done`;
      re-attaching after a dropped connection returns the same frames from the cursor, in
      order, with no frame lost or repeated.
- [ ] A job whose lease is not renewed for `job_lease_s` cancels itself on the worker.
- [ ] A single gate that runs longer than `job_lease_s` while attached completes and is not
      cancelled.
- [ ] `nw worker poll` reports `lost` for a job whose pid is dead and for one whose
      heartbeat is older than 60 s.
- [ ] `nw worker cancel` ends the job's processes and the job emits `done` with
      `interrupted: true`.
- [ ] `nw worker jobs --json` lists accepted, running, done and lost states; `nw worker gc
      --keep-days 0` removes done and lost job dirs.
- [ ] `nw worker gate-job` behaves as before, including the cancel on stdin EOF.
- [ ] `ssh-command` admits the new subcommands and refuses bad `JOB` or `--from` values;
      `info` reports `features: ["async"]`.
- [ ] With no workers registered, behaviour is unchanged (SPEC-0338's golden test still
      passes).

## Test plan

- **Automated tests** (`tests/test_worker_jobs.py`, offline). They use SPEC-0338's loopback
  transport:
  - submit, attach to `done`, and the resubmit cases (same header, different header);
  - **drop:** the transport script is killed after k frames, then re-attach from the cursor;
    the frames are in order, with none lost or repeated;
  - **lease expiry:** `job_lease_s=2`, no renewals, and the job process cancels itself. The
    test waits with `tests/waits.py`;
  - **long attached gate:** `job_lease_s=2` and a gate sleeping 5 s while attached completes;
  - **hung job:** a job process that stops heartbeating reports `lost` via `poll`; a killed
    pid reports `lost` too;
  - `cancel` ends the processes and the job emits `done` with `interrupted: true`;
  - `gate-job`: stdin EOF before `done` still cancels;
  - `jobs --json`, `gc --keep-days 0`, and the extended `ssh-command` allowlist (including
    refused `JOB` and `--from` values), and `info` features.
- **Manual verification:**
  1. On the second machine, `nw worker submit` a job with a long gate, kill the SSH
     session, and `nw worker attach JOB --from 0` again. The frames resume and the job
     reaches `done`.
- **Regression guard:** run the affected tests while iterating
  (`python3 tools/affected_tests.py --run uv run pytest -q`); SPEC-0336 and SPEC-0338's
  tests run unchanged.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. Upgrade workers when convenient. Nothing on the coordinator calls the new subcommands
   until SPEC-0341, so this ships dark.
2. No data migration. The new files appear in new job dirs only.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0332:
- The lease default is `job_lease_s` 900 s.
- `gate-job` is kept as a compatibility path.
