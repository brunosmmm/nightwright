---
id: SPEC-0334
title: "Pull workers against a coordinator job API"
status: in-progress
owner: user
created: 2026-10-31
updated: 2026-11-02
source_idea: IDEA-561
parent: SPEC-0310
tags: [agentic, remote, workers, security, api]
depends_on: [SPEC-0342, SPEC-0344]
---

## Context

[SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md) and
[SPEC-0332](./0332-worker-owned-async-jobs.md) **push** jobs: the coordinator SSHes into the
worker. That requires the coordinator to reach the worker. A laptop behind NAT, a cloud VM
without inbound SSH, or a machine on another network cannot be a worker.

**Pull** turns the connection around. The worker dials out to the coordinator, asks for a
job, and reports frames back. Once the coordinator serves jobs over a network API, two new
needs appear:
- jobs must be authenticated end to end (signed), not merely trusted because they arrived
  over SSH;
- each worker needs its own revocable credential.

## Goals / Non-goals

**Goals**
- `nw serve --jobs` exposes the SPEC-0332 job store over HTTP(S): lease a job, fetch its
  bundle, post frames, post the result bundle, renew the lease, receive a cancel.
- `nw worker serve` dials out, leases jobs of the kinds it supports, and runs them with the
  same job process as `nw worker submit`.
- Every job header is signed by the coordinator and checked by the worker. Replays and
  expired jobs are refused.
- Each worker has its own revocable credential. `nw workers rm` or `nw workers token
  --rotate` cuts it off at once.
- The listener is safe by default. It binds to loopback or a Tailscale address, or requires
  TLS, with optional mTLS.

**Non-goals**
- A public multi-tenant service, user accounts, or a web UI.
- Replacing SSH push. Both modes coexist, and each worker entry chooses one.
- New dependencies. Everything here uses the stdlib (`http.server`, `ssl`, `hmac`,
  `secrets`).

## Decision

**Note 2026-11-01:** this spec must NOT edit `tests/test_remote_workers_integration.py`. SPEC-0346
owns that file (SPEC-0334 runs in parallel with SPEC-0345/0346). Pull-worker tests go in
this spec's own test files.

Add a small stdlib HTTP API over the existing file-backed job store.

The dispatcher queues a job for a pull worker by writing `<agentic_dir>/job-queue/<job_id>.json`
instead of SSHing. A pull worker long-polls `POST /v1/lease` with its bearer token and gets
two things:
- the job header, signed with **HMAC-SHA256** under a per-worker secret;
- the sha256 of the bundle.

Frames flow back through `POST /v1/jobs/<id>/frames` and go through the same idempotent
`n`/`event_uid` apply path as SSH frames (SPEC-0332).

## Design

### Registry

`workers.json` entries gain:
- `mode: "ssh" | "pull"` (default `ssh`);
- for pull workers, `token_sha256` and `secret_ref`. The secret itself lives in
  `<config_dir>/secrets/workers/<name>.key` with mode `0600`, never in the registry.

The commands are:
- **`nw workers add NAME --pull [--kinds ...]`** creates the token and the secret and prints
  a one-time **enrolment blob** (`{coordinator_url, name, token, secret}`, base64) for the
  worker.
- **`nw workers token NAME --rotate`** replaces the token and the secret and prints a new
  blob. The old ones stop working immediately.
- **`nw workers rm NAME`** revokes the worker and cancels its jobs.

### API (`nw serve --jobs [--bind ADDR:PORT] [--tls-cert F --tls-key F [--client-ca F]]`)

`nw serve` is a **new** top-level command; `--jobs` is currently its only (required) mode.
The default bind is `127.0.0.1:8790`. The server is a `ThreadingHTTPServer` with JSON bodies. Every request carries
`Authorization: Bearer <token>`. The token is compared in constant time against
`token_sha256`, and an unknown or revoked token gets 401.

| Method and path | Purpose |
|---|---|
| `POST /v1/lease` `{kinds, slots_free}` | Long-poll up to 30 s. Returns `{job: signed header, bundle_sha256}` or 204. |
| `GET /v1/jobs/<id>/bundle` | Streams the input bundle. |
| `POST /v1/jobs/<id>/frames` `{frames: [...]}` | Applies frames in `n` order. Returns `{cursor, cancel: bool}`. |
| `PUT /v1/jobs/<id>/result-bundle` | The SPEC-0333 write-back bundle. |
| `POST /v1/heartbeat` `{jobs: [{id, n, state}]}` | Renews the leases and returns cancels. |

Rules:
- A worker can touch only jobs leased to it (403 otherwise).
- **Lease expiry.** The server checks leases every 5 s. A lease not renewed within
  `job_lease_s` goes through the SPEC-0332 give-up path: it appends `worker_job_lost`, adds
  the worker to `excluded_workers`, and either requeues the job as `<...>-a<attempt+1>` or
  runs it locally once `max_remote_attempts` is reached. Later requests from the old worker
  for the old job id get **410 Gone**, and the worker cancels that job locally on 410.
- **Result bundle ordering.** In pull mode the `done` frame carries `result_bundle_sha256`
  instead of the bundle bytes. The worker PUTs the bundle first. The server rejects a `done`
  frame with 409 (`{cursor}` unchanged) until a bundle with that sha256 has been stored,
  then runs the SPEC-0333 write-back exactly as for SSH.
- **Size limits.** A request with `Content-Length` over 64 MiB (result bundle) or 4 MiB
  (frames batch) gets 413. Bodies are streamed to a temp file under
  `<agentic_dir>/job-uploads/` and hashed before use.
- **Bind safety.**
  - Without TLS, `--bind` must be loopback, inside `100.64.0.0/10` or inside
    `fd7a:115c:a1e0::/48` (Tailscale); wildcard binds (`0.0.0.0`, `::`) and anything else
    are refused at startup.
  - With `--client-ca`, the server requires a client certificate whose CN equals the name of
    the worker the bearer token resolves to.

### Coordinator integration (`src/nightwright/agentic/pull_jobs.py`)

1. The API server is the only writer of frames and of `remote-jobs/<job_id>.json` for pull
   jobs. It uses the SPEC-0332 apply path under a per-job file lock.
2. For a pull worker, the gate or persona step writes `job-queue/<job_id>.json` plus the job
   record with `state: queued`, then polls the record every 1 s until `done`, `error` or
   `lost`.
3. A SPEC-0229 stop or abandon sets `cancel_requested: true` on the record, and the server
   returns it as `cancel: true` on the next frames or heartbeat response.
4. Adoption after a coordinator restart re-enters the polling wait for a live record
   (SPEC-0332 adoption).

### Signed jobs

The server signs the header at lease time (`issued_at` = the lease time). The signature is
`sig = HMAC-SHA256(secret, canonical_json(header_without_sig))`. The
canonical form is sorted keys with no whitespace. The header gains:
- `issued_at` and `expires_at` (`issued_at` + 15 min);
- `nonce`, which is `job_id`;
- `bundle_sha256`.

The worker refuses the job, posts an `error` frame with `stage: "verify"`, and runs nothing
when any of these holds:
- the signature is bad;
- `expires_at` has passed (allowing 120 s of clock skew);
- the nonce has already been seen;
- the bundle hash does not match.

`expires_at` is checked once, when the worker verifies the job before starting it; a running
job is not affected by it. Seen nonces are kept in `<worker data>/agentic/worker/nonces.jsonl` until they expire. Job
ids already embed the attempt, so a legitimate retry has a new nonce.

### Worker (`nw worker serve --enrol BLOB | --config F`)

- It stores the enrolment blob in `<worker config>/coordinator.json` (0600).
- Its loop: lease, fetch the bundle, verify the signature and the hash, start the job process
  (the same code as `submit`), and tail its `frames.jsonl` to `POST frames` in batches every
  1 s. It also heartbeats every `job_lease_s / 3`.
- After each successful `POST /v1/heartbeat` or `POST frames`, it extends each listed job's
  `lease.json` to `now + job_lease_s` (the value comes in the lease response). During a
  network outage the local lease is not renewed, so a job outlives the outage only while its
  lease lasts. This matches the coordinator, which gives up after the same `job_lease_s`.
- It honours `cancel` by calling `nw worker cancel`. On 410 it cancels that job. On 401 it
  cancels every local job and exits non-zero.
- Running it as a service is left to the human (`docs/WORKFLOW.md` shows an example systemd
  user unit); there is no `--print-unit`.
- On a network failure it backs off (2 s doubling to 60 s) and keeps running its jobs. The
  frames stay spooled locally and are re-posted from the server's `cursor`.

## Alternatives considered

- **Ed25519 signatures.** These need the `cryptography` package, and nightwright keeps its
  base dependencies minimal. A per-worker HMAC key gives the same guarantee at this trust
  scale: the coordinator is the only signer and each worker holds only its own key.
- **WebSockets or gRPC.** These are new dependencies. Long-polling plus batched POSTs is
  enough for minute-scale jobs.
- **Running jobs over the mailbox.** The mailbox is for human-readable coordination
  (SPEC-0335), not bulk bundles.
- **Exposing the API publicly with TLS only.** It stays allowed, but the default is the
  narrowest safe bind.

## Acceptance criteria

- [x] A pull worker leases, runs and completes a gate job and a persona job. The results are
      identical to the SSH-push path, with the same files, events and write-back.
- [x] A missing, wrong or revoked token gets 401. Touching another worker's job gets 403.
- [x] A tampered header, an expired job, a replayed nonce, and a bundle hash mismatch are
      each refused by the worker with `stage: "verify"`, and nothing runs.
- [x] A non-loopback, non-Tailscale bind without TLS is refused at startup. With
      `--client-ca`, a wrong client CN is refused.
- [x] Killing `nw worker serve` mid-job and restarting it re-posts frames from the server
      cursor with no duplicates. A lease that expires returns the job with `attempt + 1`,
      and after `max_remote_attempts` the job runs locally; the old worker's next frames
      post gets 410 and it cancels the job.
- [x] A pull job running longer than `job_lease_s` completes as long as heartbeats succeed.
- [x] A `done` frame before its result bundle is stored gets 409; after the PUT it applies.
- [x] A SPEC-0229 stop of a campaign whose gate runs on a pull worker returns `cancel: true`
      on the next frames or heartbeat response; the worker runs `nw worker cancel` and the
      job ends with `interrupted: true` within 5 s.
- [x] After `nw workers rm`, the worker's next request gets 401; `nw worker serve` then
      cancels every local job and exits non-zero, and the coordinator requeues or locally
      runs those jobs.
- [x] The secret file and `coordinator.json` are created with mode 0600, and the registry
      contains only `token_sha256`.
- [x] Oversized requests get 413.
- [x] `nw workers token --rotate` invalidates the old credential immediately.

## Test plan

- **Automated tests** (`tests/test_pull_workers.py`, offline):
  - The setup: the API server starts on `127.0.0.1:0` in a thread, and the worker loop runs
    in a subprocess against a temp worker data dir.
  - Gate and persona jobs, compared with the SSH loopback results from SPEC-0338, SPEC-0332
    and SPEC-0333.
  - **Security** (`tests/test_pull_workers_security.py`):
    - token cases;
    - cross-worker 403;
    - each verify failure (tamper one header field, an expired `expires_at`, a reused nonce,
      a bundle byte flipped);
    - bind refusal (a public address, `0.0.0.0` and `::`), and the Tailscale IPv4 and IPv6
      ranges accepted;
    - 413 for an oversized frames batch and bundle;
    - 0600 modes and no secret in `workers.json`;
    - 401 after `rm` (the worker exits non-zero, its job is requeued);
    - mTLS with a test CA generated by `openssl` in a fixture, skipped when `openssl` is
      missing;
    - rotation.
  - Restart: kill the worker loop and restart it, then check the cursor resume. Lease expiry
    requeues the job with `job_lease_s=2`, and runs it locally after `max_remote_attempts`;
    the old worker gets 410. A job outliving `job_lease_s` with working heartbeats
    completes. A `done` before the result-bundle PUT gets 409. A stop delivers `cancel` and
    ends the job within 5 s. All use `tests/waits.py`.
- **Manual verification:**
  1. On the coordinator, start `nw serve --jobs --bind <tailscale-ip>:8790`.
  2. On a laptop that has no inbound SSH, run `nw worker serve --enrol <blob>`.
  3. Run one campaign's gates through it.
  4. Revoke the worker and confirm the laptop's next lease gets 401.
- **Regression guard:** the full `uv run pytest`. The SSH-push tests run unchanged.

### As shipped (2026-11-02)

Code: `src/nightwright/agentic/pull_jobs.py` (credentials, signing, the coordinator's queue,
wait and cancel, the HTTP server), `src/nightwright/agentic/pull_worker.py` (`nw worker serve`),
`nw serve --jobs` and `nw worker serve` in `cli_worker.py`, `nw workers add --pull [--kinds]
[--url]` / `token --rotate` / `rm` in `cli_workers.py`, `mode: pull` records in `workers.py`,
and pull branches in `remote_gates.py` (`cut`, `_call`, `_submit`, `_follow`, `_cancel`),
`remote_personas.py` (`_follow`) and `remote_jobs.py` (`has_async`, `prune`). Docs:
`docs/WORKFLOW.md` "Pull workers", with the example systemd user unit. Where it differs from
or sharpens the Design:

- **Record fields.** The server writes the record's pull fields (`pull_state` queued / leased /
  done / error / lost, `pull_cursor`, `lease_until`, `pull_error`, `progress`) and, for a gate
  job, the gate files and `cursor` through SPEC-0341's own `_Job.feed`, under the per-job lock
  `job-frames/<job_id>.lock`; applied frames are spooled to `job-frames/<job_id>.jsonl`. The
  record's `state` keeps its SPEC-0341 meaning, so `live_gate`, adoption and `mark_done` are
  unchanged. The waiting step polls `pull_state`, not `state`.
- **Gate `done`.** The step rebuilds the `GateRun` by replaying the spool through `_Job.feed`
  (no file is rewritten: every `n` is at or below the cursor), so the `gate` event and its
  `event_uid` are the SSH path's.
- **Persona write-back.** The server refuses a `done` with 409 until the bundle with its
  `result_bundle_sha256` is stored, then marks the job done; the step then runs SPEC-0344's
  `_finish` (the SSH write-back code) on the spooled `done` frame with the uploaded bundle read
  back inline. The server never touches the campaign worktree.
- **Lease expiry.** The server's 5 s check only marks the job `pull_state: lost`
  (`pull_lost_reason: lease`) and unqueues it; the waiting step then runs the SPEC-0342 give-up
  (`worker_job_lost`, `excluded_workers`, `-a<attempt+1>` or local past
  `max_remote_attempts`) with the same code as an SSH loss. With no step waiting, the next
  step's adoption does it.
- **Never leased.** A job still `queued` after `worker_ttl_s` is given up as `unreachable`
  (an offline pull worker would otherwise hold the step until the job's deadline).
- **Stop.** A SPEC-0229 signal or `ctx.stop` stop cancels a pull gate job (`state: cancelled`,
  `cancel_requested`), as the criterion asks; a SPEC-0231 code-change detach leaves the record
  live and the next runner re-enters the wait (adoption). A park or abandon cancels it through
  `cancel_campaign`. Persona steps detach as over SSH.
- **Lease long-poll.** `POST /v1/lease` takes an optional `wait_s` (capped at 30 s). The worker
  sends 1 s while it runs jobs, so the lease poll doubles as its 1 s frames tick.
- **Assignment.** A job is queued for the worker whose gate slot the dispatcher took; a pull
  worker leases only its own jobs. A pull worker gets the whole bundle (no `have` round trip).
- **Disabled is not revoked.** `nw workers off` keeps the credential working, but the worker
  gets no new lease. Only `rm` and `token --rotate` answer 401.
- **Smaller details.** `issued_at`/`expires_at` are epoch seconds. A body without
  `Content-Length` gets 411. Lease and heartbeat bodies share the 4 MiB cap. The blob's
  `coordinator_url` comes from `--url`, then `agentic.workers.coordinator_url`, then
  `http://127.0.0.1:8790`. `coordinator.json` may also carry `ca`, `cert` and `key` for TLS
  and mTLS. `nw worker serve` exits 3 when revoked; the systemd example sets
  `RestartPreventExitStatus=3`. There is no `--print-unit`.

### Test record (2026-11-02)

- `uv run pytest -q -n 0 tests/test_pull_workers.py`: 6 passed. Covers a gate job against the
  SSH loopback (the same gate files byte for byte, the same payload, ledger event types and
  `event_uid`); a persona job against the SSH loopback (the same write-back tree, status, diff
  and new file, the same run-dir files and `result.json`); a worker killed mid-job and
  restarted (the spool holds each `n` once, one `gate` event); lease expiry (`job_lease_s=2`,
  worker SIGSTOPped: `worker_job_lost`, retried as `-a2` on a second pull worker, local after
  `max_remote_attempts=2`, and after SIGCONT the old worker logs 410 and its job ends); a 6 s
  gate under a 2 s lease completing with heartbeats; a stop delivering `cancel` with the job's
  `done` (`interrupted: true`) within 5 s.
- `uv run pytest -q -n 0 tests/test_pull_workers_security.py`: 32 passed. Covers missing,
  wrong and revoked tokens (401); cross-worker frames and bundle (403); tampered header,
  expired header, replayed nonce and flipped bundle byte, each refused with `stage: "verify"`
  and no job dir; bind refusal (public address, `0.0.0.0`, `::`) and the Tailscale IPv4/IPv6
  ranges accepted; 413 for frames, heartbeat and result-bundle bodies; `done` before the
  bundle PUT gets 409 and applies after it; 0600 secret and `coordinator.json`, no token or
  secret in `workers.json`; `rm` of a running worker (it exits 3, its job runs locally); mTLS
  with an `openssl` test CA (right CN 204, wrong CN 403, no certificate refused); rotation; a
  disabled worker leasing nothing; `prune` removing a pull job's spool and upload.
- `python3 tools/affected_tests.py --run uv run pytest -q -n 4`: 883 passed, 2 skipped,
  3 failed. Three existing tests stub `remote_gates._cancel` with two arguments, so the pull
  branch moved to a new `_cancel_job(cfg, worker, job_id)` and `_cancel` kept its signature.
  The rerun of `tests/test_remote_job_records.py`, `tests/test_remote_personas.py` and both
  pull test files then gave 148 passed.
- Manual, on loopback with the real binaries (no second machine was available to this run):
  `nw workers add lap --pull`, `nw serve --jobs --bind 127.0.0.1:18793` and
  `nw worker serve --enrol <blob>` against a separate data dir. The worker long-polled (204),
  `coordinator.json` and the secret were mode 600, and after `nw workers rm lap` the worker's
  next lease got 401; it logged "revoked ... cancelling every local job" and exited 3. Manual
  steps 1 and 3 across machines (a Tailscale bind, a laptop without inbound SSH, a real
  campaign's gates) were not run. The automated tests run the same gate and persona paths
  with real `nw worker serve` processes.
- The full `uv run pytest` is left to the queue's close gate.

### Test record (2026-11-02, loop 2)

- The gate ran pytest inside a worker job process, which ignores SIGHUP, and the loopback
  `ssh-command` inherited that through exec, so two SPEC-0332 attach tests hung. Applied
  main's fix (a257d16f: `tests/remote_loopback.py` resets SIGHUP to its default in the child,
  plus a regression test in `tests/test_worker_jobs.py`); no product code changed.
- With SIGHUP ignored in the parent (`signal.signal(SIGHUP, SIG_IGN)` then exec
  `uv run pytest`): `-n 0 tests/test_worker_jobs.py -k 'attach or reattach or loopback'`
  28 passed; `-n 2 tests/test_worker_jobs.py tests/test_pull_workers.py
  tests/test_pull_workers_security.py` 129 passed.
- The cross-machine manual steps (Tailscale bind, a laptop without inbound SSH) are still not
  run, so the spec stays `in-progress`. The full suite is left to gating.

### As shipped (2026-11-02, loop 3)

Fixes from the adversarial review, no Design change:

- **Queue order.** `submit` marks the record `pull_state: queued` first, then writes the queue
  entry under the queue lock, so a lease never sees an entry whose record is not yet `queued`
  and its `leased` is never reset. A renewal (frames or heartbeat) that finds a record still
  `queued` sets it `leased` again.
- **Accept loop.** In TLS mode the listening socket no longer handshakes in `accept()`: each
  request's thread sets a 60 s socket timeout (`HANDLER_TIMEOUT_S`) and then runs the
  handshake. Request handlers have the same timeout, so a silent or half-sent request no longer
  holds a thread forever.
- **Locking.** The lease check and `give_up_lease` re-read the record under the job's lock and
  skip a lease renewed meanwhile (or a job already `done` / `error`); heartbeat renewals take
  the same lock.
- **Mid-batch refusal.** A 400 or 409 part way through a frames batch keeps the frames before
  it applied: the record's `pull_cursor` matches the spool, so a re-post appends nothing twice.
- **Heartbeat 403.** A heartbeat for a job that is not this worker's says `gone` (the worker
  cancels it), as 410 does.
- **Rotation.** `nw workers token --rotate` writes the registry's new `token_sha256` before the
  new secret, then gives up the worker's live jobs as `rm` does (the old worker gets 401 on its
  next request, cancels them and exits 3).

### Test record (2026-11-02, loop 3)

- New in `tests/test_pull_workers_security.py`: a lease racing `submit` (the lease runs inside
  `submit`'s record update) ends `leased` and is handed out once; a renewal repairs a record
  left `queued`; a lease renewed under the job lock during the expiry check is not given up; a
  409 mid-batch keeps frame 1 (cursor 1, then 2 after the PUT, spool `[1, 2]`); a heartbeat for
  another worker's job says `gone`; rotation gives up the live job (`revoked`); a silent TCP peer
  on the TLS listener does not block a real lease (openssl test CA); a half-sent request is
  closed after the handler timeout. With `submit`'s old order and the old
  `do_handshake_on_connect=True` restored, the race test and the TLS test both fail.
- `uv run pytest -v -n 0 tests/test_pull_workers_security.py`: 40 passed.
- `test_lease_expiry_retries_then_runs_locally_and_the_old_worker_gets_410` failed once under
  `-n 4`. The test released the blocked gate as soon as the coordinator marked the job lost,
  which raced the job process seeing its own expired lease. It now waits for the old job's
  `result.json` before the release. 3 + 2 runs alone passed.
- `uv run pytest -q -n 4 tests/test_pull_workers.py tests/test_pull_workers_security.py
  tests/test_workers_registry.py tests/test_remote_job_records.py tests/test_remote_personas.py
  tests/test_worker_job_retry.py`: 252 passed. `python3 tools/spec_lint.py`: ok.
- The cross-machine manual steps are still not run, so the spec stays `in-progress`. The full
  suite is left to gating.

### As shipped (2026-11-02, loop 4)

Fixes from the second adversarial review. The 4 MiB frames-batch cap stands; one endpoint is
added to the API table:

- **Frames too big for a batch.** A frame whose JSON is still over the worker's 3 MiB batch size
  once its result bundle is uploaded goes up whole by `PUT /v1/jobs/<id>/frame-upload` (the
  64 MiB cap, hashed like the result bundle) and is posted as a stub `{n, frame_sha256}`. The
  server applies the stub as the uploaded frame: 409 until the upload is stored, 400 when the
  upload is not frame `n`. The spool holds the whole frame, and the upload is deleted once it is
  applied. Such frames are gate frames carrying big logs and persona `done` frames carrying
  run files.
- **Over the upload cap.** A frame or result bundle over 64 MiB is never sent. The worker posts
  an `error` frame (`stage: "frames"`) in its place, so the job ends as an error instead of
  being retried forever. A 413 on the upload or on a frames post is handled the same way.
- **Final jobs.** The frames response also carries `final` (the job is `done` or `error` on the
  server). Once a job is final, the worker drops it when the job ends locally. If the job is
  still running there with frames the server will never take, the worker cancels it first.
- **Per-job isolation.** `tick` catches a `Transport` failure per job. A failed sync forces the
  heartbeat, so every job's lease is renewed, including the failing job's, and the lease poll
  still runs. A coordinator that is down fails the heartbeat or the lease, and `run` backs off as
  before.
- **Smaller fixes.** A body of the wrong shape (TypeError, KeyError) gets 400, not a dropped
  connection. `apply_frames` keeps the cursor of the frames already applied when any exception
  interrupts a batch, not only a refusal. Re-enrolling (`--enrol`) keeps `ca`, `cert` and
  `key` from the existing `coordinator.json`. `prune` and the step's cleanup also remove
  leftover frame uploads.

### Test record (2026-11-02, loop 4)

- New in `tests/test_pull_workers_security.py`:
  - A stub before its upload gets 409. After the PUT it applies, and the whole frame is
    spooled with the upload removed. Another worker's PUT gets 403. An upload that is not
    frame `n` gets 400 with the cursor kept.
  - Two persona jobs in one `Loop.tick`. One has a 5 MiB `done` frame with a result bundle:
    it ends `done`, its whole frame is spooled, the bundle can be read back, and the job is
    dropped. The other job's lease is renewed.
  - A frame over the upload cap (the cap lowered to 1 MiB) ends the job with
    `stage: "frames"`, cursor 2. The running local job is cancelled, then dropped once it
    ends.
  - One job's frames post raises `Transport`. The tick completes, runs the heartbeat and the
    lease poll, and both jobs' leases are renewed.
  - Re-enrolling keeps the TLS keys.
  - A body of the wrong shape gets 400.
  - `prune` removes frame uploads.
- With `pull_worker.py` and `pull_jobs.py` reverted to the previous commit, the five new
  out-of-band, cap, isolation, stub and enrol tests fail.
- `uv run pytest -q -n 0 tests/test_pull_workers_security.py`: 45 passed (before the 400 and
  prune additions). Then `uv run pytest -q -n 4 tests/test_pull_workers.py
  tests/test_pull_workers_security.py tests/test_remote_job_records.py
  tests/test_remote_personas.py tests/test_workers_registry.py tests/test_worker_job_retry.py`:
  258 passed. `python3 tools/spec_lint.py`: ok.
- The cross-machine manual steps are still not run, so the spec stays `in-progress`. The full
  suite is left to gating.

## Clock Log

Use `nw idea clock-in IDEA-561` / `nw idea clock-out IDEA-561`.

## Rollout / migration

1. Inert until `nw serve --jobs` runs and a `--pull` worker is added.
2. Existing SSH workers are unaffected (`mode` defaults to `ssh`).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:
- Signatures use HMAC-SHA256 with a per-worker secret (stdlib only).
- Jobs expire after 15 min.
- Without TLS, only loopback and Tailscale binds are allowed.
- The client uses long-polling.
- Frames are spooled on the worker so they survive network loss.
