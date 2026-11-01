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
depends_on: []
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

**Amendment 2026-11-01:** `depends_on` changed from `[SPEC-0338]` to `[]`. Why: this spec is worker-side only (`nw worker
...` subcommands) and ships dark; it never calls `remote_gates.run` or the dispatch code, so
it can be built alongside SPEC-0337/0338. The only real coupling was the Test plan's reuse
of SPEC-0338's loopback fixture in `tests/test_remote_gates.py`. Resolution: this spec
carries its own minimal loopback helper, `tests/remote_loopback.py` (a transport script that
runs `nw worker <subcommand>` locally in place of `ssh`, killable after k frames). It does
not import from `tests/test_remote_gates.py` and does not edit it, so the two specs do not
conflict. SPEC-0338 may adopt the helper later; that is not required. The "SPEC-0338's tests
keep working" statements below apply once SPEC-0338 has landed; until then they apply to
SPEC-0336's tests only.

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

### Job dir

`jobs/<job_id>/` is the store from SPEC-0336. It keeps `job.lock`, `job.json` and
`frames.jsonl`, and adds new files:
- `job.lock` (existing): the detached job process takes this flock for its whole life.
  gc's liveness check (`_live_jobs`) keeps using it for async jobs.
- `job.json` (existing): keeps its SPEC-0336 fields, plus `state` and `header_digest`.
- `frames.jsonl` (already written today) gains a sequence number `n` on every frame. `n`
  starts at 1, and only the job process assigns it.
- `header.json` and `bundle` (new): the spooled input.
- `proc.json` (new): the job process claims it with `O_EXCL` at start, as `runproc.claim`
  does. It records the process's pid and start ticks, and the process group id and start
  ticks of every gate it starts. It is rewritten atomically each time a gate starts.
- `heartbeat.json` (new): `{ts, pid}`, rewritten by the job process every
  `agentic.workers.job_heartbeat_s` (default 10 s).
- `lease.json` (new): `{lease_until}`, renewed by the client.
- `result.json` (new): written last, atomically. It records the final `done` or `error`
  frame, or `state: "lost"` after a kill (see Lost jobs).

`submit` creates the dir under `jobs_lock`, exactly as `gate-job` does today (mkdir,
`job.json`). Before it spawns the job process, it writes `lease.json` with
`lease_until = now + job_lease_s`. A job that is submitted and never attached therefore
still has a lease that can expire.

### Frames

The job process appends each frame to `frames.jsonl` as one line ending in a newline.
`attach` and `poll` emit only newline-terminated lines. A trailing partial line (a frame of
up to about 16 MiB still being written) is held back until its newline appears, so a reader
never prints a truncated frame and never repeats one when it reads again from its cursor.
Error frames written before the job dir is claimed (for example a bad header) go to stdout
only and have no `n`.

### Subcommands

All of these are reachable through `nw worker ssh-command`:
- **`nw worker submit`**
  - It reads the header and the bundle from stdin, as `gate-job` does, and validates them
    the same way.
  - It claims the job dir and writes the initial `lease.json`. It then spawns
    `python -m nightwright.agentic.worker_job <job_dir>` with `start_new_session=True`.
  - It prints `{job_id, state: "accepted", n: 0}` and exits 0.
  - The header digest is the sha256 of
    `json.dumps(header, sort_keys=True, separators=(',', ':'), ensure_ascii=False)`, taken
    over the whole header as received (including `env` and `attempt`). It is stored in
    `job.json` as `header_digest`.
  - A resubmit of an existing `job_id` with an equal digest is **idempotent**: it prints the
    current state, starts no second process, and exits 0. A digest mismatch is an `error`
    with `stage: "claim"` and exit code 3. A client that bumps `attempt` therefore needs a
    new `job_id`.
- **`nw worker attach JOB --from N`**
  - It prints every frame with `n > N`, then follows the file, polling every 0.5 s.
  - It exits 0 once `done` has been printed and 3 once `error` has been printed.
  - While connected it renews the job lease on every follow tick, whether or not a new frame
    was read. `lease.json` is rewritten when more than `min(30, job_lease_s/3)` s have
    passed since the last rewrite. At least two thirds of `job_lease_s` is then always left
    while attached, so the lease never lapses for any `job_lease_s` ≥ 1, and a long silent
    gate is never cancelled while attached.
  - On every tick it also derives the job state (see Job states). When the state is `lost`,
    it prints a trailing `{"type": "status", "state": "lost", heartbeat_age_s}` line, stops
    renewing the lease and exits 4.
  - It never reads stdin. EOF on stdin, or a signal (SIGTERM/SIGINT/SIGHUP) to `attach`,
    never cancels the job: `attach` just exits.
- **`nw worker poll JOB --from N [--renew]`**
  - It prints the complete frames after `N` once, plus a trailing
    `{"type": "status", state, heartbeat_age_s}` line, and exits 0.
  - `--renew` sets `lease_until = now + job_lease_s`.
- **`nw worker cancel JOB`** acts according to the job's state:
  - `accepted` or `running`: it writes `cancel` into the job dir. The job process sees it
    within 1 s and calls `gates.terminate_all()`, then emits `done` with `interrupted: true`.
  - `done` or `error`: a no-op that prints the state and exits 0.
  - `lost`: it follows the hung-job kill path (see Lost jobs).
- **`nw worker jobs [--json]`** lists the jobs with their state.
- **`nw worker gate-job`** stays: `submit` plus `attach --from 0` in one process, with these
  differences from the two commands:
  - It still refuses an existing `job_id` with `error`/`stage: "claim"` and exit 3, as
    SPEC-0336 does (`WorkerError("claim", "job … already exists on this worker")`). It never
    attaches idempotently.
  - It exits 3 after `error` and 0 after `done`.
  - It keeps SPEC-0336's stdin contract: EOF on stdin before `done` writes `cancel` into the
    job dir. SIGTERM, SIGINT and SIGHUP are treated like stdin EOF.
  - It keeps SPEC-0336's tests (and SPEC-0338's once landed) and `nw queue gates --worker`
    working.
- The `ssh-command` allowlist becomes `info|have|gate-job|gc|submit|attach|poll|cancel|jobs`.
  `JOB` must match `[A-Za-z0-9._-]+`, and `--from` must be a non-negative integer.
- `info` reports `protocol: "nw.job.v1"` and `features: ["async"]`.

### Job states

`poll`, `attach`, `cancel`, `jobs` and `gc` derive the state the same way, and `job.json`
mirrors it in `state`:
- `accepted`: the dir is claimed and the job process has not yet claimed `proc.json`.
- `running`: `proc.json` exists, the pid is alive (checked with its start ticks), the
  heartbeat is fresh, and there is no `result.json`.
- `done`: `result.json` records a `done` frame.
- `error`: `result.json` records an `error` frame. For example, the job process's checkout
  or validation failed.
- `lost`: any one of these holds:
  - there is no `result.json`, `proc.json` exists, and its pid (with start ticks) is dead;
  - there is no `result.json` and the heartbeat is older than `agentic.workers.job_lost_after_s`
    (default 60 s), even if the pid is alive (a hung process);
  - the job is still `accepted` with no `proc.json` after `job_lost_after_s`;
  - `result.json` records `state: "lost"` (written by the kill path).

### Lease

The lease length is `agentic.workers.job_lease_s`, default 900 s. The job process checks
`lease.json` every 1 s, together with the `cancel` file. If `lease_until` has passed with no
renewal, nobody is listening any more, so the job process cancels the job as `cancel` does
and emits `done` with `interrupted: true`.

### Lost jobs (hung or dead job process)

A hung job process cannot heartbeat, check its lease or see a `cancel` file, so the worker
must reap it from outside. `cancel`, and `gc`, act on a job whose state is `lost`:
1. If the job process's pid is alive, they send SIGTERM to its process group, and then
   SIGKILL after 5 s if it is still alive.
2. They do the same to every gate process group recorded in `proc.json`.
3. Every signal is sent only after a start-ticks check, so a recycled pid is never hit.
4. They then write `result.json` with `state: "lost"`.

Once the job process is gone, its `job.lock` is free.

### gc

`nw worker gc` holds `jobs_lock` while it works:
1. It first runs the kill path on every `lost` job that has no `result.json`.
2. It then deletes a job dir only when its `job.lock` is free (the job is done, error or
   lost and killed) and the dir was last touched more than `--keep-days` ago.

A `running` job holds `job.lock`, so `gc --keep-days 0` never removes it.

## Alternatives considered

- **Keep synchronous SSH and just retry.** This wastes whole gate runs and makes persona
  runs on workers unaffordable.
- **A daemon on the worker (`nw worker serve` listening).** That is a second long-lived
  process and a port to secure. Detached job processes plus the forced command need neither.
  Pull workers come later (SPEC-0334).
- **No worker-side lease.** A coordinator that dies for good would leave remote jobs
  running until their timeout. With the lease, a responsive job process stops after at most
  15 min. A hung one is reaped by the next `cancel` or `gc`, which run the lost-job kill
  path.
- **Renew the lease per frame.** A single gate can run longer than `job_lease_s`, so a
  frame-driven renewal would cancel a long silent gate. `attach` renews on every tick.
- **A fixed 30 s throttle on lease rewrites.** This lets any `job_lease_s` under about 30 s
  lapse while attached. The throttle is `min(30, job_lease_s/3)`.
- **Have `gate-job` attach idempotently like `submit`.** A repeated `gate-job` would
  silently replay an old job's frames. `gate-job` keeps SPEC-0336's refusal.

## Acceptance criteria

- [ ] `nw worker submit` returns before the gates run and writes `lease.json` with
      `lease_until = now + job_lease_s` before it spawns the job process.
- [ ] A resubmit with the same header digest (sha256 of the sorted, compact `json.dumps` of
      the whole header, stored as `job.json` `header_digest`) is idempotent. One with a
      different header is refused (`stage: "claim"`, exit 3).
- [ ] `nw worker attach JOB --from N` prints the frames after `N`, follows them, and exits
      0 after `done` and 3 after `error`.
- [ ] Re-attaching after a dropped connection returns the same frames from the cursor, in
      order, with no frame lost or repeated.
- [ ] `attach` and `poll` never emit a trailing partial line, and `n` starts at 1.
- [ ] EOF or SIGTERM/SIGHUP on `attach` does not cancel the job.
- [ ] `attach` to a job whose pid is killed prints the `lost` status line and exits 4
      within 2 s, and it stops renewing the lease.
- [ ] A job whose lease is not renewed for `job_lease_s` cancels itself on the worker.
- [ ] `attach` rewrites `lease.json` at most every `min(30, job_lease_s/3)` s. With
      `job_lease_s=2`, a single 5 s gate completes while attached and is not cancelled.
- [ ] `nw worker poll` reports `lost` for a job whose pid is dead, and for one whose
      heartbeat is older than `job_lost_after_s`. `job_heartbeat_s` and `job_lost_after_s`
      are config keys with defaults 10 and 60.
- [ ] `nw worker cancel` on a running job ends the job's processes, and the job emits `done`
      with `interrupted: true`. On a `done`/`error` job it is a no-op that prints the state
      and exits 0.
- [ ] `cancel` and `gc` on a hung (`lost`, live pid) job kill its process group and every
      gate group in `proc.json`, and write `result.json` with `state: "lost"`.
- [ ] `nw worker jobs --json` lists accepted, running, done, error and lost states.
- [ ] `nw worker gc --keep-days 0` removes done, error and lost job dirs and never removes
      a running job.
- [ ] `nw worker gate-job` behaves as before:
  - it refuses an existing `job_id` with `stage: "claim"`;
  - it exits 0 after `done` and 3 after `error`;
  - stdin EOF and SIGTERM/SIGINT/SIGHUP cancel the job.
- [ ] `ssh-command` admits the new subcommands and refuses bad `JOB` or `--from` values;
      `info` reports `features: ["async"]`.
- [ ] With no workers registered, behaviour is unchanged (SPEC-0338's golden test still
      passes once SPEC-0338 has landed).

## Test plan

- **Automated tests** (`tests/test_worker_jobs.py`, offline). They use their own loopback
  helper (`tests/remote_loopback.py`, see the amendment in Decision), not SPEC-0338's. Every
  wait uses `tests/waits.py`.
  - **submit:** submit, then attach to `done` (exit 0). Check that `lease.json` exists with
    `lease_until ≈ now + job_lease_s`. Then the resubmit cases: the same header is
    idempotent (one process), and a different header gives `stage: "claim"` with exit 3.
    Check that `header_digest` in `job.json` equals the specified sha256.
  - **attach after error:** a job whose checkout fails ends in `error`; attach exits 3, and
    `jobs --json` shows `error`.
  - **drop:** the transport script is killed after k frames, then re-attach from the cursor.
    The frames are in order, with none lost or repeated. `attach` killed by SIGTERM leaves
    the job running.
  - **partial line:** a fixture appends half a frame to `frames.jsonl`. `poll` and `attach`
    do not emit it until the newline is written, and then emit it once. The first `n` is 1.
  - **lease expiry:** `job_lease_s=2`, no renewals, and the job process cancels itself
    (`done`, `interrupted: true`).
  - **long attached gate:** `job_lease_s=2` and a gate sleeping 5 s while attached
    completes. The `lease.json` rewrites are no more than `min(30, 2/3)` s apart, give or
    take one 0.5 s tick.
  - **hung job:** `job_lost_after_s=2`, `job_heartbeat_s=0.5`, and the job process is
    stopped with SIGSTOP, so its heartbeat thread stops. `poll` reports `lost` within 3 s.
    `cancel` on the hung job then kills its process group and its gate groups (the test
    asserts their pids are gone) and writes `result.json` with `state: "lost"`. A variant
    shows `gc` doing the same.
  - **dead pid:** the job process is killed with SIGKILL. `poll` reports `lost`, and an
    `attach` that is running prints the `lost` status line and exits 4 within 2 s.
  - **cancel:** on a running job, `cancel` ends the processes and the job emits `done` with
    `interrupted: true`. On a `done` job, `cancel` prints `done` and exits 0.
  - **gate-job:** stdin EOF before `done` still cancels, and SIGHUP does too. A repeated
    `job_id` is refused with `stage: "claim"`. The exit code is 0 after `done` and 3 after
    `error`.
  - **jobs and gc:** `jobs --json` shows each of accepted, running, done, error and lost.
    `gc --keep-days 0` removes the done, error and lost dirs and leaves a running job's dir
    in place.
  - **ssh-command and info:** the extended `ssh-command` allowlist admits the new
    subcommands and refuses bad `JOB` and `--from` values. `info` reports its features.
- **Manual verification:**
  1. On the second machine, `nw worker submit` a job with a long gate, kill the SSH
     session, and `nw worker attach JOB --from 0` again. The frames resume and the job
     reaches `done`.
- **Regression guard:** run the affected tests while iterating
  (`python3 tools/affected_tests.py --run uv run pytest -q`). SPEC-0336's tests (and
  SPEC-0338's once landed) run unchanged.

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
- The lease default is `job_lease_s` 900 s. The heartbeat defaults are `job_heartbeat_s`
  10 s and `job_lost_after_s` 60 s.
- `gate-job` is kept as a compatibility path, with SPEC-0336's refusal of an existing
  `job_id`.
- `attach` exits 4 on `lost`, and the kill path waits a 5 s grace between SIGTERM and SIGKILL.
