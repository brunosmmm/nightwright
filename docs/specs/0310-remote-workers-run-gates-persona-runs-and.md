---
id: SPEC-0310
title: "Remote workers: run gates, persona runs and campaigns on other machines"
status: accepted
owner: user
created: 2026-10-30
updated: 2026-10-31
source_idea: IDEA-431
kind: epic
parent: SPEC-0169
tags: [agentic, remote, workers, concurrency, security]
---

## Context

Measured on 2026-10-30 over the [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
queue:

- **Gates are about half of active campaign time**: 490 of roughly 1000 campaign-minutes.
  A full serial `pytest` took 13-19 min and a campaign ran about 4 of them.
  [SPEC-0309](./0309-faster-gates-parallel-pytest-affected-tests-only.md) (IDEA-430, done)
  made gates faster locally (parallel pytest, a host-wide gate-slot pool, affected tests in
  fix loops); the slot pool is now the local ceiling.
- **One 8-core laptop caps slots at 4-6.** Under 5-6 concurrent gates the suite exceeded the
  1200 s gate timeout; those timeouts were triaged as code fixes (IDEA-432), not capacity.
- **Everything stops when the coordinator laptop sleeps**, because every run is a child of
  the local runner.

Today the unit of remote-able work already exists in a narrow form:

- A **gate run** is `gates.run_all` (`src/nightwright/agentic/gates.py:386`): it takes one
  host-wide slot (`gate_slots.acquire`, `gate_slots.py:162`), runs the project's gate list
  in the campaign worktree with `run_gate_with_retries`, writes `gates/NNN-name.{log,json}`
  under the campaign dir and returns a `GateRun`. It is called from three places only: the
  loop's GATING step (`queue._do_gate`, defined at `queue.py:1946`), the close re-gate
  (`close.prepare_close`, defined at `close.py:410`, its `G.run_all` call at line 551) and
  `nw queue gates` (`cli_queue.py:1142`, via `run_and_log`). Its inputs are a gate list, a tree and an environment; its outputs are
  files and a dataclass. Nothing in it needs the coordinator's filesystem except the tree.
- A **persona run** is a `RunRequest` (`runner.py:33`: persona, prompt, cwd, model, budget,
  timeout, permission mode, env, run dir) turned into a `claude -p` command line by
  `build_argv` (`runner.py:153`) with the prompt on stdin. A run is a detached local process
  claimed by an exclusive `proc.json` (`runproc.claim`, `runproc.py:72`).
- Everything that happens is an event appended under flock with a monotonic id
  (`events._append_locked`, `events.py:249`).

This epic multiplies concurrency by running that work on other machines, and lets work
continue while the coordinator sleeps. Its prerequisites have landed: the MVP epic
[SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) and SPEC-0309 are `done`.
It is one epic because the protocol, two transports, scheduling, code flow, security and
cost accounting cannot be one spec, and the children must share one contract.

Related ideas: IDEA-427 (stream-json capture, viewer, herdr), IDEA-424 (project budgets),
IDEA-423 (ML workloads as a future job kind), IDEA-432 (gate timeout triage).

## Goals / Non-goals

**Goals**
1. **More concurrency.** Gates first, then persona runs, run on other machines, so slots are
   no longer capped by one laptop.
2. **Work survives coordinator sleep.** A submitted job is owned by its worker and finishes
   (or fails) without the coordinator connected.
3. **One protocol, several transports.** A transport-agnostic job protocol `nw.job.v1`
   behind which SSH and pull workers are interchangeable.
4. **Zero change when unused.** With no workers registered, behaviour is exactly today's.
5. **Safe failure.** A worker dying or vanishing can never produce a half-merge.
6. **Honest accounting.** Claude cost per job is reported as today, attributed to the
   worker that ran it.

**Non-goals**
- Replacing the coordinator with a distributed consensus system. There is one coordinator.
- Multi-user or multi-tenant operation. One human, their machines.
- Workers merging or pushing to `main`. Ever.
- Remote foreman agents (a Claude per machine making judgment calls) in this epic.
- Changing what a gate or a persona run does. Only where it runs.
- **Cloud or hosted compute** (human 2026-10-30): no cloud VMs, no ephemeral or autoscaled
  workers, no Anthropic-hosted managed sandboxes. Workers are the human's own machines.
- **Several Claude accounts** (human 2026-10-30): one shared account, capped.
- Whole campaigns as one remote job. Persona runs and gates are remote; the campaign state
  machine stays on the coordinator (decided 2026-10-31, see Decision).
- Project budget enforcement (IDEA-424). This epic records per-worker spend so a future
  project-budget spec can count it; enforcing project budgets is out of scope.

## Decision

The **coordinator** (the human's box) keeps the queue, the ledger, the lease, the conductor,
the stewards, and **all rebases and merges**. **Workers** execute self-contained jobs and
report results. Job kinds grow in order: **gate run**, then **persona run**.

Delivery order (revised 2026-10-31, before acceptance; the human asked for the fastest
route to remote gates):

1. **Thin slice first: remote gates over SSH on one host** (child 1). A synchronous SSH
   call per gate run: the coordinator sends the job header and a git bundle on stdin, the
   worker runs the exact gate list in a throwaway worktree and streams the gate logs and
   results back, the coordinator writes them where local gates write them. Any transport
   failure falls back to a local run of the same gates, so the slice is safe to turn on.
   It ships the biggest measured win (gates are half of campaign time) with no daemon and
   no new state machine. It defines the `nw.job.v1` job header and the `nw worker`
   entrypoint, which the later children extend rather than replace.
2. **Worker-owned async jobs** (child 2): `nw worker submit|attach|poll`, a worker-owned
   lease with heartbeats, idempotent job and event ids, several workers, retry on another
   worker. This is what makes jobs survive coordinator sleep (goal 2) and is the protocol
   the persona and pull children build on.
3. **Remote persona runs with branch write-back** (child 3), with campaign affinity,
   account-aware concurrency and per-worker cost.
4. **Pull workers** (child 4): `nw worker serve` dials out to a coordinator API, signed
   jobs, per-worker credentials.
5. **Federation, not a foreman, for remote judgment** (child 5). See Architecture.

Why not "protocol first, no behaviour change" (the order in the 2026-10-30 draft): it
delays any measurable gain by a whole spec, and the protocol's shape is better decided
against one working transport. The slice is designed so child 2 generalizes it: the job
header, the worker store layout, the forced-command dispatcher and the frame format are
kept; only the lifetime (connection-bound vs worker-owned) changes.

Alternatives considered:
- **SSH stream as the lease for everything** (job lives only while the connection does).
  Accepted for gates in child 1 only, because a gate is stateless and a dropped gate is
  re-run locally at no correctness cost. Rejected as the end state: a laptop sleep kills
  every job, which defeats goal 2.
- **Per-machine "foreman" Claude agent.** Rejected for now: idle per-turn cost, and
  conductor cost is an MVP metric (SPEC-0298 E1/E2).
- **A daemon-only (pull) design.** Rejected as the only transport: SSH needs no new
  service and gets gates remote fastest.
- **Whole-campaign jobs.** Dropped: with remote gates and remote persona runs, the only
  thing left local is the state machine and the merge, which must stay local anyway. Moving
  the state machine would duplicate the queue on every worker.
- **Shared filesystem (NFS/sshfs) for worktrees.** Rejected: pytest over a network
  filesystem is slower than local disk, and file locks (`flock`) are unreliable on it.

## Architecture / cross-cutting design

### Job protocol `nw.job.v1`

- **Job header** (submitted): `{schema: "nw.job.v1", job_id, kind (gate|persona), campaign,
  spec_id, repo_key, commit (sha to check out), have (base shas the bundle excludes),
  bundle_bytes, env, timeout_s, attempt, payload}`. `payload` is kind-specific: for a gate
  job the resolved gate list (after `campaign_gate_config` and tier filtering, so the worker
  never reads `.nw/project.yaml`) and the gate budget; for a persona job the serialized
  `RunRequest` fields (`runner.py:33`), which the worker turns into a command with
  `build_argv` (`runner.py:153`), so the `claude -p` contract is unchanged. `job_id` is
  deterministic: `<campaign>-<kind>-<seq or run_id>-a<attempt>`, so a resubmission is
  idempotent.
- **Frames** (worker to coordinator): newline-delimited JSON, `nw.job.frame.v1`:
  `accepted`, `slot_wait`, `gate` (one finished gate: its `GateResult`, sidecar and logs,
  gzip+base64, each log capped at 16 MiB keeping the tail), `event` (child 2+), `done`,
  `error`. Child 1 streams them over the SSH connection; child 2 also spools them in the
  worker store so `attach`/`poll` replay them.
- **Result**: the `done` frame, written once: `{job_id, state (succeeded|failed|transient|
  cancelled), exit_code, duration_s, ...kind fields}`; for gates the `GateRun` fields, for
  persona runs `cost_usd, usage, branch, head_sha`.
- **Events** (child 2+): the existing `nw.event` records, appended on the worker to the
  job's spool with a per-job sequence and an `event_uid` (job_id plus sequence). The
  coordinator merges them with `events.append`, skipping any `event_uid` already merged.
  Before merging, the coordinator **overwrites** `campaign`, `spec_id` and `project` from
  the job header and sets `by: worker:<name>`, and it refuses (and logs as a
  `worker_event_refused` finding) any event whose type is not in the **worker-event
  allowlist**: `gate`, `run_started`, `run_finished`, `run_failed`, `progress`, `finding`.
  Merge, close, park, resume, lease and escalation types are never accepted from a worker
  (child 2 implements and tests this).
- **Lease** (child 2+): owned by the worker; heartbeat every `ttl/3`; lost after `ttl`
  without one. Lease loss is a **transient failure** through the existing transient path
  (`events._is_transient`, `events.py:304`; `run_failed` with `transient`): the job is
  retried with a new `attempt`, so late events of the dead attempt are recorded but never
  acted on.
- **Worker-side store**: `<worker data_dir>/agentic/worker/repos/<repo_key>.git` (a bare
  object cache per coordinator repo) and `.../worker/jobs/<job_id>/{job.json, tree/,
  out/, frames.jsonl, result.json, proc.json}`. A job dir is claimed `O_EXCL` like
  `runproc.claim`, so a second submit of the same id never starts a second process.

### Workers, registry, scheduling

- Registry: `<coordinator agentic dir>/workers.json` (`nw.workers.v1`), written only by
  `nw workers add|rm|on|off` (never hand-edited config.yaml, which nightwright does not
  rewrite). `nw workers list|check` read it. A missing or empty registry means today's code
  path, unchanged.
- The local machine is the implicit fallback, not a registry entry.
- **Slots** are per worker: a coordinator-side flock pool per worker
  (`gate-slots/w-<name>/slot-<n>.lock`, the SPEC-0309 mechanism). Gates prefer a remote
  slot, then the local pool. The worker additionally takes its own host's gate slot, so a
  worker that is also someone's laptop is not oversubscribed.
- **Campaign affinity** (child 3): a campaign's persona jobs go to one worker while it is
  healthy (warm object cache, `uv` cache).
- **Account-aware scheduling** (child 3): the shared Claude account's concurrency is a
  second limit on persona jobs across all workers, with backoff on transient 429 failures.

### Code flow

The coordinator sends the code as a **git bundle** on the job's stdin: the commit to test
(for a gate, the worktree's HEAD, or a `git stash create` commit when tracked files are
modified) minus the bases the worker already has (asked with `nw worker have`). No shared
git remote is required. Implementer commits come back (child 3) as a bundle the coordinator
fetches into `nw/<campaign>`. **Rebase, re-gate and merge stay on the coordinator**; workers
hold no credentials for any remote. A dead worker leaves at worst an unreferenced object,
never a half-merge.

### Observability and cost

- Remote gate logs and sidecars land in the campaign's `gates/` dir exactly as local ones
  (except that a log over 16 MiB is tail-truncated and its sidecar records
  `truncated_bytes`), so `nw campaign show`, triage prompts and the export work unchanged. The `gate` event
  gains a `worker` key only when remote.
- IDEA-427's stream-json is just more frames on the job's stream (child 3).
- Claude cost is reported per job with the worker id (child 3), so per-worker spend shows
  in `nw campaign export`; existing per-run and per-campaign caps count remote cost like
  local cost. Project budgets (IDEA-424) are a Non-goal; the per-worker data is there for
  them.

### "Claude on the other side"

Workers are deterministic code by default; Claude runs only inside persona jobs, so Claude
cost is unchanged. Delegated remote judgment is deferred. The preferred form is
**federation** (child 5): a project's runner and steward run on a remote machine, the
mailbox (`src/nightwright/agentic/mailbox.py`, per-address inboxes with sequence numbers,
`mailbox.send` line 79) is carried over the network, and one conductor coordinates N
project machines asynchronously.

### Threat model (required)

Dispatching a gate (arbitrary repo code) or a `bypassPermissions` agent job is **remote
code execution by design** on the worker, and a persona worker holds a Claude login.
Required in every child:
- Workers are dedicated users, containers or VMs. No personal credentials, no git push
  credentials (code goes back as bundles).
- SSH: a dedicated key whose `authorized_keys` line forces `nw worker ssh-command`, which
  only runs the `nw worker` subcommands with validated arguments (child 1).
- Pull workers: Tailscale or mutual TLS, per-worker revocable credentials, jobs signed by
  the coordinator and verified by the worker (unsigned or replayed jobs refused) (child 4).
- A compromised worker must not be able to alter the ledger except through results and
  events of its own jobs, or to land code except via a branch the coordinator re-gates
  itself.
- Job `env` is an allowlist (a job carrying any other key is refused, not trimmed); secrets
  are never placed in jobs, frames, events or results.

### Known limits

- Merges into `main` stay serial, so more parallel campaigns mean more rebase conflicts;
  [SPEC-0303](./0303-fewer-rebase-conflict-parks.md) matters more.
- The Claude account rate limit is a hard external ceiling on persona concurrency.
- Gates that depend on untracked, non-ignored files in the worktree cannot run remotely
  (the bundle carries commits only); such a gate run stays local.

## Breakdown / sub-specs

- [ ] [SPEC-0336](./0336-gate-worker-job-runner-and-forced-command.md) Gate worker job runner and forced-command dispatcher (project: Meta-Tools) — `nw worker info|have|gate-job|gc|ssh-command`, `nw.job.v1` header and frames, per-repo object cache, base refs, env allowlist, threat model docs
- [ ] [SPEC-0337](./0337-worker-registry-and-multi-pool-gate-slots.md) Worker registry and multi-pool gate slots (project: Meta-Tools) — `workers.json`, `nw workers add|list|check|on|off|rm`, `gate_slots.acquire(pools=...)`, remote holds in `occupancy()`/`holding()` (depends_on SPEC-0336)
- [ ] [SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md) Remote gate dispatch with local fallback (project: Meta-Tools) — thin slice, dispatch part: seq claim, bundle code flow with base ref, remote eligibility and opt-out, local fallback, reporting (depends_on SPEC-0336, SPEC-0337)
- [x] [SPEC-0331](./0331-remote-gates-over-ssh-on-one-worker-host.md) Remote gates over SSH on one worker host (project: Meta-Tools) — superseded by the three-way split SPEC-0336/0337/0338
- [x] [SPEC-0332](./0332-worker-owned-async-jobs.md) Worker-owned async jobs (project: Meta-Tools) — superseded by the four-way split SPEC-0340/0341/0342/0343
- [ ] [SPEC-0340](./0340-worker-async-job-protocol.md) Worker-side async job protocol (project: Meta-Tools) — `nw worker submit|attach|poll|cancel|jobs`, `frames.jsonl` with `n`, worker lease and heartbeat, `lost`, `gate-job` kept (depends_on none; runs alongside SPEC-0337/0338)
- [ ] [SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md) Coordinator re-attach, job records and adoption (project: Meta-Tools) — `remote-jobs/` records, idempotent apply by `n`/`event_uid`, re-attach with backoff, adoption after a restart, stop/abandon cancel, old-worker fallback (depends_on SPEC-0338, SPEC-0340)
- [ ] [SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md) Lost-job retry across workers and escalation (project: Meta-Tools) — retry with `attempt + 1`, `excluded_workers`, several workers, escalation after three losses (depends_on SPEC-0341)
- [ ] [SPEC-0343](./0343-worker-bootstrap-and-drain.md) Worker drain (project: Meta-Tools) — `nw workers drain` only; bootstrap moved to SPEC-0354 (depends_on SPEC-0341)
- [ ] [SPEC-0354](./0354-nw-workers-bootstrap.md) nw workers bootstrap (project: Meta-Tools) — idempotent install, forced-command key install, `--admin` user creation, `--dry-run`; ships ahead of the job chain (depends_on SPEC-0337)
- [x] [SPEC-0333](./0333-remote-persona-runs-with-bundle-write-back.md) Remote persona runs with bundle write-back (project: Meta-Tools) — superseded by the three-way split SPEC-0344/0345/0346
- [ ] [SPEC-0344](./0344-remote-persona-jobs-with-bundle-write-back.md) Remote persona jobs with bundle write-back (project: Meta-Tools) — persona jobs, `request_to_job`, env allowlist, eligibility, ff-only plus stash write-back, progress frames, `nw workers set`, `check --auth` (depends_on SPEC-0342)
- [ ] [SPEC-0345](./0345-persona-cap-rate-limit-backoff-affinity-and-cost-by-worker.md) Persona cap, rate-limit backoff, campaign affinity and cost by worker (project: Meta-Tools) — `max_personas`, `rate-limit.json`, `remote_affinity`, `cost_by_worker` (depends_on SPEC-0344)
- [ ] [SPEC-0346](./0346-remote-workers-integration-suite.md) Remote workers integration suite (project: Meta-Tools) — `tests/test_remote_workers_integration.py` for gate and persona workers (depends_on SPEC-0345)
- [ ] [SPEC-0334](./0334-pull-workers-against-a-coordinator-job-api.md) Pull workers against a coordinator job API (project: Meta-Tools) — `nw worker serve` dials out, signed jobs, per-worker credentials, Tailscale/mTLS (depends_on SPEC-0342, SPEC-0344)
- [x] [SPEC-0335](./0335-federation-over-a-networked-mailbox.md) Federation over a networked mailbox (project: Meta-Tools) — remote project runner and steward, one conductor, N project machines (depends_on SPEC-0334) — superseded by SPEC-0527 after two diverging campaigns
- [ ] [SPEC-0527](./0527-federation-bounded-relay-mail-and-two-remote-verbs.md) Federation, bounded: relay, mail and two remote verbs (project: Meta-Tools) — replaces SPEC-0335 (depends_on SPEC-0334)

Sequencing: the thin slice is SPEC-0336, then SPEC-0337, then SPEC-0338 (the spec review of
SPEC-0331 asked for this three-way split: worker, registry and pools, dispatch).
Together they are useful on their own (remote gates, local fallback).
SPEC-0340 to SPEC-0343 (the split of SPEC-0332) generalize the job lifetime: the worker
protocol first, then the coordinator re-attach, then retry and bootstrap/drain in parallel.
SPEC-0344 to SPEC-0346 (the split of SPEC-0333) add persona runs. SPEC-0334 needs the retry
path (SPEC-0342) and the persona write-back (SPEC-0344), so it runs in parallel with
SPEC-0345 and SPEC-0346. SPEC-0335 needs only pull workers (SPEC-0334) and runs alongside
SPEC-0346.

Amendment 2026-11-01 (dependency graph relaxed; no spend had started): SPEC-0340 is worker-side
only and ships dark, so it has no dependencies and runs alongside SPEC-0337/0338 (it carries
its own loopback test helper). SPEC-0341 depends on SPEC-0338 and SPEC-0340. SPEC-0342 and
SPEC-0343 run in parallel after SPEC-0341. SPEC-0344 stays after SPEC-0342 (shared
remote-step/give-up module, a merge hotspot). SPEC-0335 depends on SPEC-0334 only.

Amendment 2026-11-01 (bootstrap split out): SPEC-0343 is now drain only. Bootstrap is
SPEC-0354, which depends only on SPEC-0337 and ships ahead of the job chain (SPEC-0340 to
SPEC-0342); SPEC-0343 (drain) stays after SPEC-0341.

Own machines come first: an SSH worker on a
second computer or home server (human 2026-10-30).

## Acceptance criteria

- [ ] All breakdown children are `done` or `superseded`.
- [ ] With **no workers registered**, a recorded queue run produces the same events, ledger
      and run artifacts as before the epic (golden comparison, timestamps and ids masked).
- [ ] A gate job and a persona job each complete on a remote worker over SSH and their
      results appear in the coordinator's ledger with the usual event types.
- [ ] Submitting the same job id twice starts one process; delivering the same event twice
      appends one coordinator event.
- [ ] Killing the coordinator mid-job and restarting it resumes the job (attach or poll) and
      merges its result without re-running it.
- [ ] Killing a worker mid-job yields a transient failure, a retry on another worker (or
      locally), and no merge from the dead attempt.
- [ ] Workers hold no push credentials; merge happens only on the coordinator after a
      coordinator re-gate.
- [ ] The threat-model section is implemented as tests (restricted SSH command, env
      allowlist, unsigned job refused on a pull worker, and a worker-supplied event for
      another campaign or with a non-allowlisted type refused).
- [ ] The same gate job completes through the SSH transport and through a pull worker
      (`nw worker serve`) with identical coordinator events (goal 3).
- [ ] A federated project host's campaign appears on the hub, and hub-to-steward mail is
      delivered exactly once across a dropped connection (SPEC-0335).
- [ ] Per-worker Claude spend is visible in `nw campaign export` (`cost_usd` per job with
      its `worker` id).
- [ ] Measured over at least 5 campaigns with one remote worker: median gate wall time per
      campaign is at least 30% lower than the SPEC-0338 baseline, recorded in the epic's
      as-shipped notes from `nw campaign export`.

## Test plan

- **Integration/e2e (offline, `tests/test_remote_workers_integration.py`, added by
  SPEC-0333):** a loopback SSH transport (the "remote" command runs locally against a
  second data dir, no network) and a fake persona runner. Assertions:
  - a queue of campaigns with no workers registered: output equals the golden local run.
    **SPEC-0338 records that golden** (events, ledger, gate artifacts; timestamps and ids
    masked) as a fixture *before* it modifies `gates.run_all`, with a test asserting the
    no-workers run matches it; every later child keeps that test green;
  - gate and persona jobs complete remotely, results merged in order;
  - duplicate `submit` and duplicate event delivery are idempotent;
  - coordinator restart mid-job resumes from `poll`, no re-run;
  - heartbeat stops, lease expires, `run_failed` transient, job retried on a second
    loopback worker, stale-attempt events ignored;
  - branch write-back through a bundle; a worker that returns no bundle leaves the branch
    and `main` untouched;
  - the same gate job through a loopback pull worker (`nw worker serve` against
    `nw serve --jobs` on `127.0.0.1:0`) yields the same coordinator events as through the
    loopback SSH transport (case owned by SPEC-0334);
  - forged worker events (wrong campaign, non-allowlisted type) are refused (SPEC-0332).
- **Federation:** SPEC-0335's relay tests (`tests/test_federation.py`: hub sees host
  campaigns, exactly-once mail across a drop) are the epic's federation coverage.
- **Security tests:** forced-command dispatcher (SPEC-0338); env allowlist (SPEC-0338);
  unsigned, replayed and revoked-credential jobs refused (SPEC-0334).
- **Measurement:** SPEC-0338 records the local-only baseline (median gate wall time per
  campaign over the last 5 campaigns, from `nw campaign export`) before turning a worker
  on, then the same median over the first 5 campaigns with the worker; SPEC-0333 re-checks
  it with persona runs remote. Both go in this epic's as-shipped notes.
- **Manual verification:** one real remote gate over SSH on a second machine (SPEC-0338);
  laptop put to sleep mid-job, result collected on wake (SPEC-0332); one 429 backoff
  observed on the real account (SPEC-0333).
- **Regression guard:** full `uv run pytest` on each child.

## Rollout / sequencing

1. Record the local gate-time baseline (gate minutes per campaign, campaigns per hour)
   from `nw campaign export` when SPEC-0338 starts.
2. SPEC-0338: remote gates on one machine, off until `nw workers add` is run.
3. SPEC-0340 (alongside SPEC-0337/0338), then SPEC-0341 to SPEC-0343: async jobs; remote gates move onto it.
4. SPEC-0344 to SPEC-0346 remote persona runs, SPEC-0334 pull workers (parallel with SPEC-0345 and SPEC-0346), then SPEC-0335 (parallel with SPEC-0346).
5. Partial delivery is safe at every step: an unregistered feature is today's behaviour,
   and `nw workers off NAME` returns a worker's work to the local machine.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; `uv run pytest` green.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

Resolved by the human on 2026-10-30:
- **Machines first:** the human's own machines (a second computer or home server) over SSH.
- **Claude accounts:** one shared account; the scheduler caps total concurrent persona runs.
- **Cloud compute:** not intended; cloud and hosted workers are Non-goals.

Decided by the author on 2026-10-31 (recorded in Decision): thin remote-gate slice first;
whole-campaign jobs dropped; bundles over SSH as the only code flow (no shared remote).

No open questions remain.
