---
id: SPEC-0310
title: "Remote workers: run gates, persona runs and campaigns on other machines"
status: draft
owner: user
created: 2026-10-30
updated: 2026-10-30
source_idea: IDEA-431
kind: epic
parent: SPEC-0169
tags: [agentic, remote, workers, concurrency, security]
---

## Context

Measured on 2026-10-30 over the [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
queue:

- **Gates are about half of active campaign time**: 490 of roughly 1000 campaign-minutes.
  A full serial `pytest` takes 13-19 min and a campaign runs about 4 of them.
- **One 8-core laptop caps slots at 4-6.** Under 5-6 concurrent gates the suite exceeded the
  1200 s gate timeout; those timeouts were triaged as code fixes (IDEA-432), not capacity.
- **Everything stops when the coordinator laptop sleeps**, because every run is a child of
  the local runner.

Today the unit of remote-able work already exists in a narrow form. A persona run is a
`RunRequest` (`src/wt/agentic/runner.py:33`: persona, prompt, cwd, model, budget, timeout,
permission mode, env, run dir) turned into a `claude -p` command line by `build_argv`
(`runner.py:153`) with the prompt on stdin. A run is a detached local process claimed by an
exclusive `proc.json` (`src/wt/agentic/runproc.py`, `claim`). Everything that happens is an
event appended under flock with a monotonic id (`events._append_locked`,
`src/wt/agentic/events.py:245`). Nothing in that path assumes the process is local except
`cwd`, the pid in `proc.json`, and the filesystem the run writes to.

This epic multiplies concurrency by running that work on other machines, and lets work
continue while the coordinator sleeps. It is **not MVP work**: it starts only after the MVP
epic [SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) closes and IDEA-430
(faster gates, locally) lands. Fix gate time locally first, then scale out. It is one epic
because the protocol, two transports, scheduling, code flow, security and cost accounting
cannot be one spec, and the children must share one contract.

Related ideas: IDEA-427 (stream-json capture, viewer, herdr), IDEA-424 (project budgets),
IDEA-430 (faster gates), IDEA-423 (ML workloads as a future job kind), IDEA-432 (gate
timeout triage).

## Goals / Non-goals

**Goals**
1. **More concurrency.** Gates first, then persona runs, then whole campaigns, run on other
   machines, so slots are no longer capped by one laptop.
2. **Work survives coordinator sleep.** A submitted job is owned by its worker and finishes
   (or fails) without the coordinator connected.
3. **One protocol, several transports.** A transport-agnostic job protocol `wt.job.v1`
   behind which SSH and pull workers are interchangeable.
4. **Zero change when unused.** The local machine is just another worker. With no workers
   configured, behaviour is exactly today's.
5. **Safe failure.** A worker dying or vanishing can never produce a half-merge.
6. **Honest accounting.** Compute cost of cloud workers feeds project budgets (IDEA-424);
   Claude cost is reported as today.

**Non-goals**
- Replacing the coordinator with a distributed consensus system. There is one coordinator.
- Multi-user or multi-tenant operation. One human, their machines.
- Workers merging or pushing to `main`. Ever.
- Remote foreman agents (a Claude per machine making judgment calls) in this epic.
- Changing what a gate or a persona run does. Only where it runs.

## Decision

The **coordinator** (the human's box) keeps the queue, the ledger, the lease, the conductor,
the stewards, and **all rebases and merges**. **Workers** execute self-contained jobs and
report events and a result. Both paradigms (push over SSH, pull by workers) are supported
behind one protocol, `wt.job.v1`. Job kinds grow in order: **gate run**, then **persona
run**, then **whole campaign**.

The conductor-recommended design, recorded here as decided:

1. **A job protocol first** (child 1), with no behaviour change: `submit`, `events`,
   `result`; idempotent job and event ids; a worker-owned lease with TTL heartbeats.
2. **Transport 1: SSH, async form** (children 2, 3). `wt worker submit` reads the job on
   stdin and returns a job id; the worker runs it detached and owns it; `wt worker attach
   <id>` streams and `wt worker poll <id>` catches up. No daemon; auth is SSH keys. Jobs
   survive coordinator sleep and connection loss.
3. **Transport 2: pull workers** (child 4). `wt worker serve` dials out (NAT-friendly,
   elastic) against a job store. Options, in the order we try them: (a) a `wt serve` API
   over the coordinator's file store; (b) a shared store (git, object store, NATS or Redis)
   so the coordinator can be offline; (c) a control plane on an always-on box. Secured by
   Tailscale or mutual TLS plus per-worker credentials.
4. **Federation, not a foreman, for remote judgment** (child 6). See Architecture.

Alternatives considered:
- **SSH stream as the lease** (job lives only while the connection does). Simplest, but a
  laptop sleep kills every job, which defeats goal 2. Rejected in favour of a worker-owned
  lease.
- **Per-machine "foreman" Claude agent.** Rejected for now: idle per-turn cost, and
  conductor cost is an MVP metric (SPEC-0298 E1/E2).
- **A daemon-only (pull) design.** Rejected as the only transport: SSH needs no new
  service and gets gates remote fastest.

## Architecture / cross-cutting design

### Job protocol `wt.job.v1`

- **Job** (submitted): `{schema, job_id, kind (gate|persona|campaign), campaign, repo,
  commit (sha), base_ref, argv, env, budget_usd, timeout_s, permission_mode, cwd_policy,
  attempt}`. A persona job carries the serialized `RunRequest` fields
  (`runner.py:33`) and the worker rebuilds the command with `build_argv` (`runner.py:153`),
  so the `claude -p` contract is unchanged. `job_id` is deterministic from
  (campaign, kind, attempt, request hash), so resubmission is idempotent.
- **Events**: the existing `wt.event` records (`events.py:245`), appended on the worker to a
  per-job spool with a per-job sequence and an `event_uid` (job_id plus sequence). The
  coordinator merges them into its `events.jsonl` with `events.append`, skipping any
  `event_uid` already merged. Late or duplicate delivery is therefore harmless, and the
  ledger stays append-only.
- **Result**: `{job_id, state (succeeded|failed|transient|cancelled), exit_code,
  duration_s, cost_usd, usage, verdict, branch, head_sha, artifacts}`. Written once.
- **Lease**: owned by the worker. The worker heartbeats every `ttl/3`; the coordinator
  treats a job as lost when `ttl` passes with no heartbeat. Lease loss is a **transient
  failure**: the existing transient path (`events._is_transient`, `events.py:300`, and the
  `run_failed` event with `transient`, `events.py:39`) retries the job elsewhere. The retry
  has a new `attempt`, so the dead attempt's late events carry a stale attempt and are
  recorded but never acted on.
- **Worker-side store**: `<worker_data>/jobs/<job_id>/{job.json,events.jsonl,result.json,
  lease.json}`. The detached-process claim reuses the `runproc.claim` pattern
  (`runproc.py`), so a second submit of the same id does not start a second process.

### Workers, registry, scheduling

- `wt workers add NAME --ssh HOST --slots N --kinds gate,persona`, `wt workers
  list|check|drain|off`. `check` verifies the identical wt git sha, Claude auth, `uv`, and
  repo access. `wt worker bootstrap` installs the exact wt version.
- The local machine is registered implicitly as worker `local`. Zero configured remote
  workers means the scheduler takes today's code path unchanged.
- **Slots** are per worker. Gates prefer remote workers. **Campaign affinity**: a campaign's
  jobs go to one worker while it is healthy (warm worktree, `uv` and caches).
- **Account-aware scheduling**: the shared Claude account's concurrency is a second limit on
  persona jobs, separate from machine slots. A 429 parked a campaign before (the SPEC-0179
  canary), so the scheduler caps concurrent persona runs per account and backs off on
  transient 429 failures; several accounts are supported by tagging a worker with an
  account.

### Code flow

Workers get the code by fetching the campaign branch from a shared git remote, or by
receiving a **git bundle** over SSH when no shared remote exists. Implementer commits come
back as a pushed `wt/<campaign>` branch (or a bundle fetched by the coordinator). **Rebase,
re-gate and merge stay on the coordinator**; workers hold no `main` credentials. Combined
with the lease rules, a dead worker leaves at worst an unreferenced branch, never a
half-merge.

### Observability and cost

- IDEA-427's stream-json is just more events on the job's stream, so the tail viewer and
  herdr panes work wherever a job runs.
- Compute cost of cloud workers (instance-seconds times rate) is recorded as a job-level
  field, summed per campaign, and feeds IDEA-424 project budgets. Claude cost is reported
  as today.

### "Claude on the other side"

Workers are deterministic code by default; Claude runs only inside persona jobs, so Claude
cost is unchanged. Delegated remote judgment is deferred. The preferred form is
**federation**: a project's runner and steward run on a remote machine, the mailbox
(`src/wt/agentic/mailbox.py`, per-address inboxes with sequence numbers, `mailbox.send`
line 79) is carried over the network, and one conductor coordinates N project machines
asynchronously. We also spike **Anthropic-hosted managed-sandbox agents** as a possible
third backend for persona jobs; fit with the `claude -p` contract, budgets and git
write-back is **unverified**.

### Threat model (required)

Dispatching a `bypassPermissions` agent job is **remote code execution by design** on the
worker, and the worker holds a Claude login. Required in this epic and in children 2-6:
- Workers are dedicated users, containers or VMs, with only a repo deploy key (no `main`
  push rights) and the Claude login. No personal credentials.
- SSH: dedicated keys, `command=` restriction to `wt worker` subcommands where possible.
- Pull workers: Tailscale or mutual TLS, per-worker revocable credentials, jobs signed by
  the coordinator and verified by the worker (a worker refuses unsigned or replayed jobs).
- A compromised worker must not be able to alter the ledger except by appending events for
  its own jobs, or to land code except via a branch the coordinator re-gates itself.
- Job `env` is an allowlist; secrets are never placed in events or results.

### Known limits

- Merges into `main` stay serial, so more parallel campaigns mean more rebase conflicts;
  [SPEC-0303](./0303-fewer-rebase-conflict-parks.md) matters more.
- The Claude account rate limit is a hard external ceiling on persona concurrency.

## Breakdown / sub-specs

Multi-project fan-out (SPEC-0095/0097): the `(project: Name)` annotation tells
`wt spec seed-children` which project a child belongs to. Spec numbers are assigned when the
children are created.

- [ ] Job protocol `wt.job.v1`, worker registry and local worker adapter, with no behaviour change (project: Meta-Tools) — schemas, idempotent job/event ids, lease/heartbeat, event merge, `wt workers add|list|check|drain|off`, local machine as worker `local`.
- [ ] Remote gates over SSH, async submit/attach (project: Meta-Tools) — `wt worker submit|attach|poll`, gate jobs on a remote worker with bundle or remote fetch, per-worker slots and gate preference, `wt worker bootstrap` (depends_on job protocol)
- [ ] Remote persona runs with branch write-back (project: Meta-Tools) — persona jobs rebuilt via `build_argv` on the worker, campaign affinity, account-aware concurrency, implementer commits returned as `wt/<campaign>` (depends_on job protocol)
- [ ] Pull workers: `wt worker serve` and a job store (project: Meta-Tools) — dial-out workers, option (a) `wt serve` API over the file store first, signed jobs, Tailscale/mTLS and per-worker credentials, threat-model tests (depends_on job protocol)
- [ ] Ephemeral cloud workers scaled by queue depth, with compute cost accounting (project: Meta-Tools) — provision/teardown, scale on queue depth, compute cost into IDEA-424 budgets (depends_on pull workers)
- [ ] Federation: remote project runner and steward with a networked mailbox (project: Meta-Tools) — one conductor, N project machines, asynchronous (depends_on pull workers)
- [ ] Spike: managed-sandbox backend for persona runs (project: Meta-Tools) — verify fit with the `claude -p` contract, budgets and git write-back; write up go/no-go

Sequencing: the protocol child goes first and changes nothing observable. Remote gates and
remote persona runs can then proceed in parallel; remote gates ship first (stateless,
biggest win). Pull workers depend only on the protocol and may run in parallel with the SSH
children. Cloud workers and federation both need pull workers. The spike is independent
and can run any time after acceptance.

## Acceptance criteria

- [ ] All breakdown children are `done` or `superseded`.
- [ ] With **no workers configured**, a recorded queue run produces byte-for-byte the same
      events, ledger and run artifacts as before the epic (golden comparison).
- [ ] A gate job and a persona job each complete on a remote worker over SSH and their
      events appear in the coordinator's `events.jsonl` with the usual types.
- [ ] Submitting the same job id twice starts one process; delivering the same event twice
      appends one coordinator event.
- [ ] Killing the coordinator mid-job and restarting it resumes the job (attach or poll) and
      merges its result without re-running it.
- [ ] Killing a worker mid-job yields a transient failure, a retry on another worker, and
      no merge from the dead attempt.
- [ ] Workers cannot push `main`; merge happens only on the coordinator after a coordinator
      re-gate.
- [ ] The stream-json viewer and herdr pane work against a remote job.
- [ ] The threat-model section is implemented as tests (unsigned job refused on a pull
      worker, restricted SSH command, env allowlist).
- [ ] Cloud worker compute cost is visible in project budget reports.
- [ ] Measured: with at least 2 remote workers, gate wall time per campaign and campaigns
      per hour improve over the local-only baseline recorded when IDEA-430 lands.

## Test plan

- **Integration/e2e (offline, `tests/test_remote_workers_integration.py`, added with the
  last SSH child):** a fake SSH transport (in-process, no network) and a fake worker that
  scripts job output. Assertions:
  - queue of campaigns, no workers configured: output equals the golden local run;
  - gate and persona jobs complete remotely, events merged in order;
  - duplicate `submit` and duplicate event delivery are idempotent;
  - coordinator restart mid-job resumes from `poll`, no re-run;
  - heartbeat stops, lease expires, `run_failed` transient, job retried on a second fake
    worker, stale-attempt events ignored;
  - branch write-back through a temp bare git repo; a worker without a returned branch
    leaves `main` untouched.
- **Security tests (pull-worker child):** unsigned or replayed job refused; revoked
  credential refused; env allowlist enforced.
- **Manual verification:** one real remote gate over SSH on a second machine, laptop put to
  sleep mid-job, job result collected on wake; one 429 backoff observed on a real account.
- **Regression guard:** full `uv run pytest` on each child.

## Rollout / sequencing

1. Not before SPEC-0298 closes and IDEA-430 lands. Record the local gate-time baseline.
2. Protocol and local adapter (no behaviour change), then remote gates over SSH on one
   machine, off by default (`wt workers` empty).
3. Remote persona runs and account-aware scheduling.
4. Pull workers, then cloud workers and federation as separate, individually switchable
   steps. The managed-sandbox spike can inform step 3 or 4.
5. Partial delivery is safe at every step: an unconfigured feature is today's behaviour.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; `uv run pytest` green.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

Scope questions for the human; technical details are decided above.
- Which machines first: an existing second computer, a home server, or cloud VMs? (Sets the
  order of children 2 and 5.)
- One Claude account or several for worker-side persona runs? (Decides whether
  account-aware scheduling is a cap or a pool.)
- Is the compute-cost budget for cloud workers a hard cap or a report-only number at first?
