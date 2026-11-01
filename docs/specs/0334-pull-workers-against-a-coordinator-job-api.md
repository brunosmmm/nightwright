---
id: SPEC-0334
title: "Pull workers against a coordinator job API"
status: accepted
owner: user
created: 2026-10-31
updated: 2026-10-31
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

- [ ] A pull worker leases, runs and completes a gate job and a persona job. The results are
      identical to the SSH-push path, with the same files, events and write-back.
- [ ] A missing, wrong or revoked token gets 401. Touching another worker's job gets 403.
- [ ] A tampered header, an expired job, a replayed nonce, and a bundle hash mismatch are
      each refused by the worker with `stage: "verify"`, and nothing runs.
- [ ] A non-loopback, non-Tailscale bind without TLS is refused at startup. With
      `--client-ca`, a wrong client CN is refused.
- [ ] Killing `nw worker serve` mid-job and restarting it re-posts frames from the server
      cursor with no duplicates. A lease that expires returns the job with `attempt + 1`,
      and after `max_remote_attempts` the job runs locally; the old worker's next frames
      post gets 410 and it cancels the job.
- [ ] A pull job running longer than `job_lease_s` completes as long as heartbeats succeed.
- [ ] A `done` frame before its result bundle is stored gets 409; after the PUT it applies.
- [ ] A SPEC-0229 stop of a campaign whose gate runs on a pull worker returns `cancel: true`
      on the next frames or heartbeat response; the worker runs `nw worker cancel` and the
      job ends with `interrupted: true` within 5 s.
- [ ] After `nw workers rm`, the worker's next request gets 401; `nw worker serve` then
      cancels every local job and exits non-zero, and the coordinator requeues or locally
      runs those jobs.
- [ ] The secret file and `coordinator.json` are created with mode 0600, and the registry
      contains only `token_sha256`.
- [ ] Oversized requests get 413.
- [ ] `nw workers token --rotate` invalidates the old credential immediately.

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
