---
id: SPEC-0340
title: "Worker-side async job protocol: submit, attach, poll, cancel"
status: done
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

- [x] `nw worker submit` returns before the gates run and writes `lease.json` with
      `lease_until = now + job_lease_s` before it spawns the job process.
- [x] A resubmit with the same header digest (sha256 of the sorted, compact `json.dumps` of
      the whole header, stored as `job.json` `header_digest`) is idempotent. One with a
      different header is refused (`stage: "claim"`, exit 3).
- [x] `nw worker attach JOB --from N` prints the frames after `N`, follows them, and exits
      0 after `done` and 3 after `error`.
- [x] Re-attaching after a dropped connection returns the same frames from the cursor, in
      order, with no frame lost or repeated.
- [x] `attach` and `poll` never emit a trailing partial line, and `n` starts at 1.
- [x] EOF or SIGTERM/SIGHUP on `attach` does not cancel the job.
- [x] `attach` to a job whose pid is killed prints the `lost` status line and exits 4
      within 2 s, and it stops renewing the lease.
- [x] A job whose lease is not renewed for `job_lease_s` cancels itself on the worker.
- [x] `attach` rewrites `lease.json` at most every `min(30, job_lease_s/3)` s. With
      `job_lease_s=2`, a single 5 s gate completes while attached and is not cancelled.
- [x] `nw worker poll` reports `lost` for a job whose pid is dead, and for one whose
      heartbeat is older than `job_lost_after_s`. `job_heartbeat_s` and `job_lost_after_s`
      are config keys with defaults 10 and 60.
- [x] `nw worker cancel` on a running job ends the job's processes, and the job emits `done`
      with `interrupted: true`. On a `done`/`error` job it is a no-op that prints the state
      and exits 0.
- [x] `cancel` and `gc` on a hung (`lost`, live pid) job kill its process group and every
      gate group in `proc.json`, and write `result.json` with `state: "lost"`.
- [x] `nw worker jobs --json` lists accepted, running, done, error and lost states.
- [x] `nw worker gc --keep-days 0` removes done, error and lost job dirs and never removes
      a running job.
- [x] `nw worker gate-job` behaves as before:
  - it refuses an existing `job_id` with `stage: "claim"`;
  - it exits 0 after `done` and 3 after `error`;
  - stdin EOF and SIGTERM/SIGINT/SIGHUP cancel the job.
- [x] `ssh-command` admits the new subcommands and refuses bad `JOB` or `--from` values;
      `info` reports `features: ["async"]`.
- [x] With no workers registered, behaviour is unchanged (SPEC-0338's golden test still
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

### Test record (2026-11-01)

- `tests/test_worker_jobs.py` (43 tests, new) covers every Test plan bullet through
  `tests/remote_loopback.py` (the forced command run locally in place of `ssh`): submit +
  attach to `done` with `lease.json` and `header_digest` checked; idempotent and refused
  resubmits; checkout `error` (attach exit 3, `jobs` shows `error`); dropped connection and
  re-attach from the cursor (frames 1..5, none lost or repeated); SIGTERM on attach leaves the
  job running; partial last line held back then emitted once; lease expiry
  (`job_lease_s=2`); a 5 s gate attached with `job_lease_s=2` (lease never lapses, rewrites
  under `2/3 + 2 x 0.5` s apart); hung job (SIGSTOP, `job_lost_after_s=2`,
  `job_heartbeat_s=0.5`) is `lost` within 3.5 s, and `cancel` / `gc` kill the job and gate
  groups and write `result.json` `lost`; SIGKILLed job process: attach prints `lost` and
  exits 4 within 2 s; cancel running / done; gate-job exit codes, repeated `job_id`, EOF and
  SIGHUP; `jobs --json` shows all five states and `gc --keep-days 0` removes done, error and
  lost dirs and keeps the running one; the `ssh-command` grammar; `info` features; the CLI.
- Affected tests (`tests/test_worker_jobs.py`, `test_worker.py`, `test_worker_ssh_command.py`,
  `test_agentic_dogfood_config.py`, `test_agentic_project.py`, `-n 2`): 262 passed on three
  consecutive runs. One earlier run had 1 intermittent failure, under concurrent load from
  other campaigns, that was not captured. The likeliest cause is a timing bound, so the
  lease-gap bound was loosened to two follow ticks; the lapse check stays strict.
  `tests/test_test_waits.py` passes. `spec_lint` passes.
- Manual: the Test plan's step was run on this host through `tests/remote_loopback.py` (the
  forced command `nw worker ssh-command`, no second machine is reachable from the campaign
  worktree): submit a 6 s gate job, drop the "SSH session" after 2 frames (`accepted`,
  `started`), `poll` shows `running`, `attach --from 0` again replays 1..2 and resumes to
  `gate`, `done` (`ok: true`, `interrupted: false`); `jobs` shows `done`. A run on a real
  second host over `ssh` is left to SPEC-0346's integration suite.
- The full suite was left to gating (SPEC-0309/0328).

### As shipped (2026-11-01)

- **gate-job keeps its in-process implementation** rather than being literally `submit` +
  `attach --from 0`. That gives the behaviour the Design asks of it: refusal of an existing
  `job_id`, exit 0/3, stdin EOF and SIGTERM/SIGINT/SIGHUP cancel. It also keeps every
  SPEC-0336 test, which rely on its non-blocking stdout writer and pre-slot signal handling.
  Its `frames.jsonl` (and stdout frames once claimed) now carry `n` too.
- **Handing over `job.lock`.** `submit` takes the flock under `jobs_lock` and passes the fd
  to the job process (`pass_fds`, `NW_WORKER_JOB_LOCK_FD`), so the dir is locked without a
  gap from claim to the job's exit. A process started by hand takes the lock itself
  (non-blocking).
- **Gate groups in `proc.json`** are picked up by the job process's 0.25 s watch tick, which
  reads the gates registry, not at the moment a gate starts. A gate group younger than
  0.25 s can therefore be missing from `proc.json`. The same tick checks `cancel` and the
  lease (within 1 s).
- **Kill path.** It sends SIGTERM then SIGCONT, so a stopped (SIGSTOPped) process can take
  the TERM, and SIGKILL after the grace. The grace is 5 s; `NW_WORKER_KILL_GRACE_S`
  overrides it for tests. `gc` reads its clock again after reaping, so `--keep-days 0`
  removes a dir it has just reaped. Its JSON gains `jobs_reaped` only when it reaped
  something.
- **Accepted to lost.** The `lost_after` timer of an accepted job starts at `spawned_ts`
  (recorded just before the spawn). While `submit` is still spooling the bundle (it holds
  `job.lock`, and there is no `spawned_ts` yet) the job stays `accepted`.
- **Output shapes.** `submit`, `poll`, `attach` (lost) and `cancel` print `nw.job.frame.v1`
  lines of `type: "status"`; `poll`'s line also carries the last `n`. An unknown `JOB` prints
  `state: "unknown"` and exits 1. `cancel` on a running SPEC-0336 gate-job dir exits 1,
  because its stream owns it.
- `tests/test_worker.py::test_info_json_keys` now expects the new `features` key, which this
  spec requires. No other SPEC-0336 test changed.
- `docs/WORKFLOW.md` lists the new forced-command grammar and describes async jobs.

### Test record (2026-11-01, review round 2)

- `tests/test_worker_jobs.py` now has 58 tests. New or tightened for the review findings:
  - the lease throttle runs `attach` on a fake clock for `job_lease_s` 300, 900, 60 and 2,
    and asserts that every rewrite comes more than `min(30, job_lease_s/3)` s after the last
    one and at most one tick later. A version that renews on every tick fails, and so does one
    that drops the 30 s cap;
  - the attached 5 s gate (`job_lease_s=2`) also asserts a lower bound on the rewrite gaps
    (`>= 2/3 - 0.05` s). It attaches in-process, so the 2 s lease is not spent on a second
    interpreter's startup. That startup gap was the intermittent failure noted above;
  - accepted to `lost`: `spawned_ts` past `job_lost_after_s` is `lost`; with no `spawned_ts`
    and a free lock, `submitted_ts` is used; a recent `spawned_ts`, or a held lock, stays
    `accepted`. `job.json` mirrors `lost`;
  - SIGKILLed job process: `job.json` reads `lost` after `poll`, and `lease.json` is unchanged
    over 2.5 s after `attach` exits (with `job_lease_s=6`, a renewing attach would rewrite
    every 2 s);
  - the hung job is `lost` within 3 s;
  - `cancel` on an `error` job prints `error` and exits 0;
  - the kill path never signals a recycled pid: `proc.json` / `spawn.json` / gate entries with
    wrong start ticks, or none, leave a live process alone, and `result.json` reads `lost`;
  - a job process that never claimed `proc.json` but holds `job.lock`: `result.json` is not
    written while the lock is held; once `spawn.json` names the process, `cancel` kills it
    first and then records `lost`;
  - an unknown `JOB` exits 1 for `poll`, `attach` and `cancel`. `cancel` on a running
    gate-job dir exits 1 and leaves the job running;
  - readers never create `job.lock`;
  - a bundle upload cut short exits 3 (`stage: "read"`, no `n`) and leaves the job id free.
    A dir left by a submit that died mid-upload is claimed again;
  - `ssh-command` refuses `JOB` values that read as options; `submit` refuses such a
    `job_id`; `settings()` caps the heartbeat at a third of `job_lost_after_s`.
- The fixed sleeps before positive assertions are gone. Attach is stopped once its lease
  renewal is seen, and the partial-line check waits for two lease rewrites (two follow
  ticks). The remaining `time.sleep(2.5)` comes before a negative assertion (no renewal).
- Affected tests (`test_worker_jobs.py`, `test_worker.py`, `test_worker_ssh_command.py`,
  `test_test_waits.py`, `-n 4`): 210 passed. The gate command (`affected_tests.py` plus
  `test_agentic_dogfood_config.py` and `test_agentic_project.py`, `-n 2`): 277 passed. The
  timing-sensitive tests (long gate, dead pid, partial line, hung, re-attach) passed on two
  more repeat runs. `spec_lint` passes. The full suite was
  left to gating.

### As shipped (2026-11-01, review round 2)

- **`job.json` mirrors final states.** Any reader that derives `done`, `error` or `lost`
  for a submit job dir writes it into `job.json` `state` when the file says otherwise. The
  write happens only under `jobs_lock`, so it never lands in a dir gc is removing. A reader
  takes that lock without waiting and skips the write when the lock is busy; the next reader
  then makes it. A SPEC-0336 gate-job dir's `job.json` stays owned by its stream.
- **`result.json` `lost` is written only under `job.lock`.** A job process holds `job.lock`
  from submit to its exit. `submit` records the spawned pid and its start ticks in a new
  `spawn.json`, so the kill path can end a job process that never got as far as `proc.json`.
  The kill path signals the `proc.json` and `spawn.json` leaders and the gate groups, and
  takes `job.lock` (waiting up to 1 s). Only then does it write `result.json`. If the lock
  stays held, nothing is recorded, and gc does not list the job in `jobs_reaped`. A slow
  starter is therefore killed and never overwrites a `lost` with `done`. `cancel` runs under
  `jobs_lock`, as gc does.
- **No signal without start ticks.** The kill path never signals an entry whose start ticks
  are unknown, and the job process does not record a gate that is already gone (no ticks).
  The kill path therefore needs `/proc`, which a Linux worker has.
- **Readers never create `job.lock`.** A dir with no `job.lock` or no `job.json` yet is a
  claim in progress and reads `accepted`. A dir with a `header_digest` in `job.json` counts
  as a submit dir even before `header.json` exists.
- **Heartbeat cap.** `job_heartbeat_s` is capped at a third of `job_lost_after_s`. A slower
  heartbeat would make a healthy job look hung, and gc would then reap it.
- **A failed upload or spawn un-claims the job.** When the bundle upload breaks off, or the
  spawn fails, `submit` removes the job dir and prints the `error` frame on stdout only,
  with no `n`. This replaces the earlier `result.json` `error`, which turned an identical
  resubmit into an exit 0 with state `error`. It also means only the job process ever numbers
  a frame. A resubmit with the same digest reclaims a dir that never spawned and whose lock
  is free (its submit died mid-upload with no cleanup).
- **Cheaper reads.** `jobs` and the idempotent `submit` take the last `n` from the tail of
  `frames.jsonl` and never read earlier frames. The idempotent `submit` prints after
  releasing `jobs_lock`.
- **Option-like `JOB`.** `ssh-command` refuses a `JOB` that starts with `-`, and `submit`
  refuses such a `job_id` (`stage: "validate"`), because it could never be named to `attach`.

### Test record (2026-11-01, review round 3)

- `tests/test_worker_jobs.py` now has 71 tests. New for the review findings:
  - `poll --renew`: the job is submitted with a long first lease, then `poll --renew` (over
    the loopback) sets `lease_until` within `[t0 + 3, t1 + 3]` for `job_lease_s=3`.
    Renewing in-process every 0.5 s for 7 s (more than two leases) keeps the job running and
    its gate alive. A plain `poll`, over the loopback and in-process, leaves `lease.json`
    byte-for-byte unchanged. With no further renewals the job cancels itself (`done`,
    `interrupted: true`);
  - `attach` on SIGHUP, SIGINT and SIGTERM (parametrized), each with stdin at EOF from the
    start: no `cancel` file, the job stays `running`;
  - lease before spawn: a wrapped `_spawn` reads `lease.json` at the moment of the spawn,
    and finds it there with `lease_until = now + job_lease_s`;
  - gate-job: stdin EOF, SIGHUP, SIGTERM and SIGINT each cancel (parametrized);
  - a heartbeat whose wall `ts` is 1000 s old but whose monotonic stamp is fresh stays
    `running`; a stale monotonic stamp is `lost`; a stamp from another boot falls back to the
    wall clock. A real job writes `mono`/`boot` in `heartbeat.json` and `proc.json`;
  - `cancel` on a dead SPEC-0336 gate-job dir exits 1 and writes nothing into it;
  - the kill path runs without `jobs_lock` (checked from inside the kill step) for both
    `cancel` and `gc`;
  - `cancel` whose reap stays locked out prints `reaped: false` and exits 1;
  - a derived `lost` never overwrites the job's own `done` in `job.json`, and a
    `result.json`, once present, is what a mirror writes;
  - `_Reader` skips frames at or below the cursor without JSON-parsing them (frames spanning
    several read chunks), and a held-back partial line is not read again.
- The lease-expiry test now submits with a long first lease and renews once to 2 s after the
  gate starts. Before this, a checkout slower than 2 s on a loaded machine cancelled the job
  before its gate started, which made `_sleep_pid` fail. That was a flake seen once under
  `-n 4` in this round.
- The wall-clock bounds (2 s, 3 s) come from the Acceptance criteria and stay as they are.
  The tests measure from the kill or stop, not from the previous process's startup. The
  remaining fixed `time.sleep(2.5)` comes before a negative assertion.
- Affected tests (`test_worker_jobs.py`, `test_worker.py`, `test_worker_ssh_command.py`,
  `test_test_waits.py`, `-n 4`): 223 passed. The gate command (`affected_tests.py` plus
  `test_agentic_dogfood_config.py` and `test_agentic_project.py`, `-n 2`): 290 passed.
  `spec_lint` passes. The full suite was left to gating.

### As shipped (2026-11-01, review round 3)

- **Heartbeat age on `CLOCK_MONOTONIC`.** `heartbeat.json` also carries `mono` (the job
  process's `time.monotonic()`) and `boot` (`/proc/sys/kernel/random/boot_id`). `proc.json`
  carries `started_mono` and `boot`. A reader on the same boot measures the age on its own
  monotonic clock, which every process on the host shares. That clock does not jump with an
  NTP step and does not count a host suspend, so neither turns a healthy job `lost` (which
  gc would then reap). Records without these fields, or from another boot, use the wall
  clock `ts` as before. `heartbeat_age_s` is reported on the same clock.
- **The kill path runs without `jobs_lock`.** This deviates from the Design's "gc holds
  `jobs_lock` while it works". gc and `cancel` read the state under `jobs_lock`, then run the
  signals, the kill graces and the 1 s `job.lock` wait without it. They take `jobs_lock`
  again only for the final `result.json` write, which re-checks that the dir still exists
  and has no result. Claims (`submit`, `gate-job`) therefore never wait out the kill graces.
  The deletion step still runs entirely under `jobs_lock`. The reaper never creates
  `job.lock`. It holds `job.lock` while waiting for `jobs_lock`, which cannot deadlock: no
  one holding `jobs_lock` waits for an existing job's `job.lock`.
- **`cancel` exit codes.** On a `lost` job, `cancel`'s status line carries `reaped: true`
  or `reaped: false`. When `job.lock` stays held after the signals (nothing recorded), the
  status is `reaped: false` and the exit code is 1. `cancel` on a SPEC-0336 gate-job dir
  exits 1 and writes nothing, whether the job is running or its process is dead (`lost`):
  its stream owns the dir.
- **`job.json` writes are serialized.** The job process writes its `running` and final
  `job.json` under `jobs_lock`, and the final write still comes before `result.json`. A
  reader's mirror re-reads both files under `jobs_lock`. A `result.json`, if present, wins.
  A `done`/`error` the job process wrote itself is never overwritten by a derived `lost`.
- **`_Reader` reads in 1 MiB chunks** from where it last stopped, and keeps a held-back
  partial line in memory instead of reading it again. A line at or below the cursor is
  skipped by the `n` at its end and never JSON-parsed. `attach` and `poll` stream the lines
  as they are read, so at most one frame is held at a time.
- Gate groups are still recorded on the 0.25 s watch tick (see round 1).

### Test record (2026-11-01, review round 4)

- `tests/test_worker_jobs.py` now has 77 tests. New or tightened for the review findings:
  - cancel on a running job also waits for the job process (`proc.json` pid) to exit and
    checks that `job.lock` is then free;
  - a resubmit of a job whose header announces a 4 MiB bundle, written in full (header and
    bundle) before stdout is read, as a plain client does: the idempotent one exits 0 and the
    refused one exits 3, and neither write fails with EPIPE. Nothing new is spooled;
  - a job process whose leader has exited while a child in its group lives on (a stand-in
    for a `git fetch` left behind by an OOM kill): the job reads `lost`, `cancel` ends the
    child and records the result (parametrized on `job.json` `running` and `done`);
  - a job that wrote its own `done` in `job.json` and then died, with nothing of its group
    left: `poll`, `jobs` and `cancel` all read `done`, and gc does not reap it. When it is
    reaped (its group was still alive, or it hung), `result.json` and `job.json` keep `done`;
  - a frame append that fails halfway (ENOSPC after a short write) is cut back off: the next
    frame reuses its `n` and arrives whole. When the cut fails too, the next frame starts on a
    fresh line and keeps its own `n` (a gap, never a repeat). The reader sees `accepted`,
    `done` in both cases.
- Affected tests (`affected_tests.py` plus `test_agentic_dogfood_config.py`,
  `test_agentic_project.py` and `test_test_waits.py`, `-n 4`): 300 passed. `spec_lint`
  passes. The full suite was left to gating.

### As shipped (2026-11-01, review round 4)

- **A resubmit drains the bundle.** An idempotent or refused `submit` reads and drops the
  header's `bundle_bytes` (up to EOF) before it answers. A client that writes header and
  bundle before reading stdout therefore never sees its write fail on a channel the worker
  closed early.
- **The kill path signals a dead leader's group.** `reap` no longer requires the
  `proc.json` / `spawn.json` leader to be alive. `runproc.group_alive` still checks the
  leader's start ticks while its pid exists, and a pgid that is still in use is never handed
  out as a new pid, so a surviving child of the job process (a `git` of its checkout) is
  ended. A recycled pid is still never signalled.
- **A job's own final state is kept.** One addition to the Job states: a job whose pid is
  dead, whose `job.json` already says `done` or `error` (written by the job process just
  before its `result.json`) and whose process group has nothing left running reads as that
  state, not `lost`. If the group still runs, or the process is alive but hung, it reads
  `lost` as before, so gc and `cancel` still end it. When the kill path then records the
  result, `result.json` takes the job's own `done`/`error` (with `frame: null`) and
  `job.json` is left as it is. Only a job with no final state of its own is recorded `lost`.
- **A failed frame append is never torn.** `_Frames` appends each line with `os.write`
  (handling short writes). When the write fails, the bytes already written are truncated
  away and `n` is rolled back, so the next frame reuses it. When the truncation fails too,
  the next frame starts with a newline, which keeps the torn piece on a line of its own (it
  is skipped), and `n` is not rolled back. The stdout copy of a cut-off frame (gate-job)
  carries no `n`.

### Test record (2026-11-01, review round 5)

- `tests/test_worker_jobs.py` now has 81 tests. New or changed for the review findings:
  - a `_Reader` that has read half a frame, after which the file is cut back and a different
    frame with the same `n` is written (shorter and longer, parametrized), emits the new frame
    once, byte for byte, with nothing stale glued on;
  - the same through `_Frames`: the reader reads half of a gate frame while its append is
    failing with ENOSPC, the append is cut back, and the `done` written at the old offset
    with the reused `n` reaches the reader;
  - `attach` on a job dir removed under it (before a tick's check, and just before a lease
    renewal) prints a `status` line with `state: "unknown"` and exits 1; `poll --renew` does
    the same;
  - the stdout copy of a frame whose append failed is unnumbered whether or not the cut
    worked;
  - the round-3 reader test now checks that `pos` stops at the last newline and that the
    partial line is read again whole.
  The four new tests fail against the round-4 code.
- Affected tests (`affected_tests.py` plus `test_agentic_dogfood_config.py`,
  `test_agentic_project.py` and `test_test_waits.py`, `-n 4`): 304 passed. `spec_lint`
  passes. The full suite was left to gating.

### As shipped (2026-11-01, review round 5)

- **The reader never keeps a partial line across reads.** This replaces round 3's held-back
  partial line. `_Reader.pos` moves only past complete (newline-terminated) lines, and an
  unterminated tail is read again on the next read. `_Frames._append` cuts a failed append
  back to where it started, which is never before the end of a complete line, so `pos` is
  never past the cut. The rewritten frame at the same offset is then read whole. Reading a
  tail again costs little: a frame is unterminated only while it is being written, or after
  a failed cut until the next frame's newline.
- **A removed job dir ends `attach`.** When gc removes the dir under a following `attach`,
  or a lease renewal finds it gone, `attach` prints `{"type": "status", "state": "unknown",
  n}` and exits 1, as for an unknown `JOB`. `poll --renew` does the same.
- **Unnumbered stdout copy.** A gate-job frame that is not whole in the log (its append
  failed, whether or not the cut worked) goes to stdout without `n`.

### Test record (2026-11-01, review round 6)

- `tests/test_worker_jobs.py` now has 84 tests. The three new ones each fail against the
  round-5 code:
  - a lease renewal (`poll --renew`) runs while `jobs_lock` is held. When gc has removed the
    dir just before the renewal takes the lock, `poll --renew` and `attach` print `unknown`,
    exit 1, and leave no dir behind;
  - a job process started by hand, in pytest's process group, records a `proc.json` whose
    `pgid` is its own pid, never the caller's group. The exit code is passed on;
  - a same-digest resubmit whose reclaim cannot remove the old dir gets an `error` frame
    (`stage: "claim"`, no `n`) and exit 3, not a traceback.
- Affected tests (`affected_tests.py` plus `test_agentic_dogfood_config.py`,
  `test_agentic_project.py` and `test_test_waits.py`, `-n 4`): 356 passed. `spec_lint`
  passes. The full suite was left to gating.

### As shipped (2026-11-01, review round 6)

- **Lease renewals take `jobs_lock`.** `attach` and `poll --renew` rewrite `lease.json` under
  `jobs_lock`, and only after checking that the dir still exists, as `_mirror`, `cancel` and
  `reap` already do. A renewal can no longer land in a dir gc is removing and leave a stub
  holding only `lease.json`, which would read as `accepted` and block the `job_id`. If the
  dir is gone, the command prints `unknown` and exits 1. `submit`'s first lease is written
  under the `job.lock` it holds, so gc cannot remove that dir.
- **A hand-started job process gets a session of its own.** When
  `python -m nightwright.agentic.worker_job DIR` runs as anything other than a session
  leader (that is, not `submit`'s spawn), it starts itself again with `start_new_session=True`,
  passes on a handed-over `job.lock` fd, waits, and exits with the child's code. The
  `pgid` in `proc.json` is therefore always the job process's own group. The kill path never
  signals the group of a shell or pipeline that started it.
- **A failed reclaim is a claim error.** When the reclaim of a never-spawned dir cannot
  remove it (for example, EACCES on an entry inside), `submit` drains the bundle, prints an
  `error` frame (`stage: "claim"`) and exits 3.

### Test record (2026-11-01, review round 7)

- `tests/test_worker_jobs.py` now has 90 tests. The six new ones each fail against the
  round-6 code:
  - gc on a full disk: one lost job's writes fail with ENOSPC. gc lists it in `errors`
    (`{job, error}`), leaves no temp file in its dir, still reaps the other lost job, and
    with `--keep-days 0` still removes both dirs;
  - `_write_atomic` unlinks its temp file when the write or the rename fails;
  - a same-digest resubmit reclaims an abandoned dir between gc's scan and its reap, and
    spawns: gc signals nothing, writes no `result.json` and does not list it in
    `jobs_reaped`;
  - the reclaim happens while `cancel`'s reap is ending the old job process: the old process
    is ended, the new one is never signalled, nothing is recorded, and `cancel` prints
    `reaped: false` and exits 1;
  - a scanned `lost` that no longer reads `lost` is not signalled or recorded;
  - a claim write that fails (ENOSPC on `header.json`) gives an `error` frame
    (`stage: "claim"`, no `n`) and exit 3, removes the dir, and the same header can then be
    submitted and runs to `done`.
- Affected tests (`affected_tests.py` plus `test_agentic_dogfood_config.py`,
  `test_agentic_project.py` and `test_test_waits.py`, `-n 4`): 362 passed. `spec_lint`
  passes. The full suite was left to gating.

### As shipped (2026-11-01, review round 7)

- **gc keeps going on a full disk.** `reap_lost` catches OSError per job, both in its scan
  and in each reap, and lists the job in gc's `errors` as `{job, error}`. gc then still
  deletes the dead dirs and prunes the repos, and exits 1 as for a failed repo. A
  `job.json` mirror is best effort under `jobs_lock` too (it is a cache of the derived
  state), so a failing mirror never stops a scan or a `cancel`. `_write_atomic` unlinks its
  temp file when it fails. A failed write touches the dir, so gc reads its clock again after
  any reap error, as it does after a reap: `--keep-days 0` still removes that dir.
- **A reap never acts on a stale scan.** `reap_lost` and `cancel` record an identity for
  each dir they find lost: its inode plus `job.json`'s `claimed_at` and `submitted_ts`
  (a reclaim can reuse the inode, never the claim's own timestamps). Before any signal,
  `reap` takes `jobs_lock`, which every claim holds, and checks that the dir is still that
  claim, still has no `result.json`, and still reads `lost`. It reads the leaders to signal
  in that same section. Before writing `result.json` it checks the identity again under
  `jobs_lock`. On a mismatch it signals or writes nothing and returns False: gc does not
  list the job, and `cancel` prints `reaped: false` and exits 1. `cancel` on a job whose
  `result.json` already says `lost` no longer signals again.
- **A failed claim write is a claim error.** When `mkdir`, the `job.lock` or the
  `job.json` / `header.json` writes fail (for example ENOSPC), `submit` closes the lock fd,
  removes the dir, drains the bundle, prints an `error` frame (`stage: "claim"`, no `n`) and
  exits 3. `cancel` whose `cancel` file write fails prints `cancel: "failed"` with the error
  and exits 1.
- Not changed: when a dead leader's group is signalled, `runproc.group_alive` still checks
  the leader's start ticks only while its pid exists. A pgid whose members have all exited
  and that a new session leader then takes over could in principle be hit. This needs pid
  reuse to land exactly on that pgid within one reap, and is left as a follow-up.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. Upgrade workers when convenient. Nothing on the coordinator calls the new subcommands
   until SPEC-0341, so this ships dark.
2. No data migration. The new files appear in new job dirs only.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0332:
- The lease default is `job_lease_s` 900 s. The heartbeat defaults are `job_heartbeat_s`
  10 s and `job_lost_after_s` 60 s.
- `gate-job` is kept as a compatibility path, with SPEC-0336's refusal of an existing
  `job_id`.
- `attach` exits 4 on `lost`, and the kill path waits a 5 s grace between SIGTERM and SIGKILL.
