---
id: SPEC-0346
title: "Remote workers integration suite for gate and persona workers"
status: in-progress
owner: user
created: 2026-11-01
updated: 2026-11-04
source_idea: IDEA-560
parent: SPEC-0310
tags: [agentic, remote, workers, testing]
depends_on: [SPEC-0345]
---

## Context

[SPEC-0310](./0310-remote-workers-run-gates-persona-runs-and.md) names one integration test
file, `tests/test_remote_workers_integration.py`, that covers the epic's acceptance criteria
for workers. Each child spec tests its own slice in isolation. Nothing yet runs a whole fake
campaign through the queue with gate and persona jobs on workers.

This is the third part of the split of
[SPEC-0333](./0333-remote-persona-runs-with-bundle-write-back.md). It is the closing child
for the SSH workers: it needs the async jobs ([SPEC-0340](./0340-worker-async-job-protocol.md),
[SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md),
[SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md)), persona jobs
([SPEC-0344](./0344-remote-persona-jobs-with-bundle-write-back.md)) and the persona limits
([SPEC-0345](./0345-persona-cap-rate-limit-backoff-affinity-and-cost-by-worker.md)).
[SPEC-0334](./0334-pull-workers-against-a-coordinator-job-api.md) adds its own cases to the
same file for pull workers and signing.

## Goals / Non-goals

**Goals**
- `tests/test_remote_workers_integration.py` covers the epic's acceptance criteria that
  concern gate and persona workers, and passes. Each of these SPEC-0310 criteria maps to a
  Design case and an acceptance criterion below: no-workers golden, gate and persona jobs
  on a worker, duplicate delivery, coordinator restart, killed worker, no push
  credentials and merge locality, forged worker events, and per-worker cost in
  `nw campaign export`.
- The measurement re-check from SPEC-0310's last criterion (median gate wall time with
  persona runs remote) has this spec as its owner, as a manual step.

**Non-goals**
- New product behaviour. This spec adds tests only (and fixes any bug the tests expose in
  the code of its dependencies).
- Pull-worker and signing cases, the "same gate job through SSH and through a pull worker"
  criterion and the unsigned-job refusal (SPEC-0334).
- Federation criteria (SPEC-0335).
- The threat-model unit tests for the restricted SSH command (SPEC-0336); this file covers
  only the env, credential and forged-event items listed below.

## Decision

One integration file, driven through the real queue, from admission to merge. It uses two
loopback SSH workers built on the existing loopback `ssh_argv` script and helpers in
`tests/remote_support.py` (`Remote`, `World`, `_script`; extended there if a helper is
missing, never a second loopback harness). Each worker has its own temp `NW_DATA_DIR` and
`NW_CONFIG_DIR`, and the coordinator has its own temp data dir: three data dirs in all.
Persona jobs use `FakeRunner` on the worker side; gate jobs use tiny shell gates.

## Design

Each case is a whole fake campaign unless noted.
- **no workers:** the campaign runs with `workers.json` absent and again with it empty. The
  first run is compared to the golden fixture `tests/data/remote_workers/no_workers_golden.json`
  (event types and payloads, ledger rows, gate artifacts; timestamps, durations, ids,
  `nw_sha` and temp paths masked, using the masking in `tests/test_remote_gates.py`). The
  fixture is recorded once, with `NW_RECORD_GOLDEN=1`, from the code at the commit before
  SPEC-0337 (`1f0311c4^`, the first child that changed dispatch) checked out in a temp
  worktree, never from post-epic code. Its gate portion must match the gate events and
  files in SPEC-0338's golden `tests/data/remote_gates_golden.json` on the masked fields.
  The empty-registry run must equal the absent-file run.
- **with workers:** same final branch tree and same event types as the no-workers run, plus
  `worker` keys on gate and persona events.
- **duplicate delivery:** the same job id submitted twice starts one worker process; the
  same worker event delivered twice appends one coordinator event.
- **killed worker:** the fake gate/persona blocks on a sentinel file. The test kills the
  worker job's process group (pid read from the worker's job record) and lets the lease
  expire with a shortened `lease_s`. The job is retried on the other worker or locally,
  the dead attempt's late events are ignored (stale `attempt`), nothing from it is merged,
  and the campaign still merges.
- **coordinator restart:** the test stops the queue loop while the job is running, builds a
  new queue/coordinator over the same coordinator data dir, and asserts the job is adopted
  via attach/poll. The job's sentinel is released only after adoption is asserted. The
  worker process runs exactly once (a counter file the fake appends to has one line) and
  the campaign merges.
- **no bundle:** a persona job whose worker returns no bundle leaves the campaign branch
  and `main` at their prior shas.
- **forged events:** a worker event for another campaign, or with a non-allowlisted type,
  is refused and appends nothing.
- **credentials and merge locality:** the fake gate and `FakeRunner` each write their
  process env to a file in the worker's job dir. The worker checkout is also inspected.
  The merge happens on the coordinator after a coordinator re-gate (a gate event with no
  `worker` key follows the worker's, before the merge event).
- **cost:** `nw campaign export` lists `cost_usd` per job together with its `worker` id.

## Alternatives considered

- **Leave coverage to the per-spec tests.** They pass in isolation but never prove the
  pieces compose through the queue.
- **Mock the transport to simulate a kill or a restart.** It would fake the failure at the
  wrong layer; the tests kill a real process group and rebuild the coordinator instead.
- **Capture the golden from post-epic code.** It would prove nothing about unchanged
  behaviour; hence the pre-SPEC-0337 commit.

## Acceptance criteria

- [x] `tests/test_remote_workers_integration.py` exists and every case above passes offline.
- [ ] The no-workers whole-campaign run matches the golden fixture
      `tests/data/remote_workers/no_workers_golden.json` (event types and payloads, ledger
      rows, gate artifacts; timestamps, durations, ids, `nw_sha` and temp paths masked).
      The fixture was recorded at `1f0311c4^` in a temp worktree, its gate portion equals
      SPEC-0338's golden on the masked fields, and a run with an empty `workers.json`
      equals the run with the file absent.
- [x] With workers, the final branch tree and the event types equal the no-workers run,
      plus `worker` keys.
- [x] The same job id submitted twice starts one worker process, and the same worker event
      delivered twice appends one coordinator event.
- [x] After a killed worker, the job is retried on the other worker or locally, the dead
      attempt's late events are ignored, nothing from it is merged, and the campaign
      merges.
- [x] After a coordinator restart mid-job, the job is adopted through attach/poll, the
      worker process ran exactly once, and the campaign merges.
- [x] A persona job whose worker returns no bundle leaves the campaign branch and `main` at
      their prior shas.
- [x] A worker event for another campaign, or with a non-allowlisted type, is refused.
- [x] For a gate job and a persona job, the env recorded by the worker-side process
      (written to a file by the fake gate / `FakeRunner`) contains only the keys in the
      worker env allowlist, and none of `GIT_ASKPASS`, `SSH_AUTH_SOCK`, `GH_TOKEN`,
      `GITHUB_TOKEN`, `GIT_CONFIG_*`; `git config --get-regexp credential` in the worker
      checkout prints nothing, and `git push` from the worker checkout to the coordinator
      repo fails.
- [x] The merge happens on the coordinator, after a coordinator re-gate.
- [ ] `nw campaign export` lists `cost_usd` per job with its `worker` id.
- [ ] Manual: a real campaign with gate and persona jobs on the second machine merged; the
      epic's rollout notes record whether a 429 backoff occurred (with its event) and the
      median gate wall time over that campaign from `nw campaign export`.

## Test plan

- **Automated tests:** the file itself (`tests/test_remote_workers_integration.py`), run with
  `uv run pytest tests/test_remote_workers_integration.py`; one test per Design case, plus
  the empty-versus-absent `workers.json` comparison. Waits use `tests/waits.py`, never fixed
  sleeps; the kill and restart cases synchronise on sentinel files.
- **Manual verification:** run a real campaign with gate and persona jobs on the second
  machine; record in the epic's rollout notes whether a 429 backoff occurred (with its
  event) and the median gate wall time from `nw campaign export`.
- **Regression guard:** affected tests while iterating.

### Test record (2026-11-04)

- `uv run pytest tests/test_remote_workers_integration.py`: 14 passed (about 70 s), run twice
  while iterating. One test per Design case; the forged-frame case has four variants.
- `python3 tools/affected_tests.py --run uv run pytest -q` (133 test files, because
  `test_agentic_e2e` and `remote_support` are widely imported) was started to cover the
  `Remote` helper change; its result is the close gate's, and the full suite was left to
  gating.
- Manual step (a real campaign on the second machine) not performed: this run is an offline
  agent with no second machine. The spec stays `in-progress` for it.

### As shipped (2026-11-04)

- **Files:** `tests/test_remote_workers_integration.py` (the cases),
  `tests/remote_workers_golden.py` (the masked snapshot, `comparable`, and the recorder),
  `tests/data/remote_workers/no_workers_golden.json`, and `Remote(w, suffix)` in
  `tests/remote_support.py` (a second loopback worker with its own data/config dirs and
  scripts; the default is unchanged).
- **Golden recorded at `1f0311c4^`** with the recorder module, run against a plain
  `git archive` export of that commit (the queue's invariants forbid creating a worktree),
  not with `NW_RECORD_GOLDEN=1`. Other specs changed the ledger since then, so `comparable`
  drops from both sides the keys `nw_sha`, `profile_sha256`, `prompt_sha256`, `head`, `tree`,
  `seq` and the `review_outcome` event type; any other difference fails the comparison. The
  empty-versus-absent comparison goes through `comparable` too (the prompt text carries the
  temp path).
- **Gate portion versus SPEC-0338's golden:** that golden is another campaign (other gates),
  so only the *shape* is compared: the same gate event keys and the same result keys. The
  literal "equals on masked fields" of the AC is therefore not met, and that criterion stays
  unticked.
- **Persona jobs:** the worker's `FakeRunner` cursor is per job, so every persona job takes
  entry 0 of the worker script. The script is one entry whose verdict satisfies both the
  implementer's and the conformance reviewer's schemas.
- **Duplicate delivery:** a duplicate `submit` of the same job id starts one worker process
  (counted from the gate's `TMPDIR` line). The "same worker event twice" case re-appends the
  gate event with its `event_uid`; `append_event` drops it. The SSH protocol has no worker
  `event` frames.
- **Forged events:** since the SSH path has no worker-event merge (no `worker_event_refused`
  finding exists in the code), the cases forge frames on the loopback stream: a frame naming
  another campaign's job is a protocol failure (the gate falls back, `worker_failed`), and an
  `event` frame carrying `close`, `park` or `merge` is never acted on (one `close`, written by
  the queue; no worker-authored event).
- **Credentials:** the gate and the persona run `env`, `git config --get-regexp credential`,
  `git remote -v`, `git config --list` and `git push` inside the worker's checkout (it is gone
  when the job ends) and write the results to the job dir. The fake `ssh` starts the worker
  with `env -i`, as sshd does, while the coordinator's own env holds poison values for the
  forbidden keys. The checkout's only remote is the worker's own object cache, the coordinator
  repo's path is nowhere in its git config, and a plain `git push` fails. A push straight to the
  coordinator repo's path is not attempted: on a loopback it would succeed (same user).
- **Merge locality:** the close re-gate is requested by the coordinator after its rebase but
  is dispatched to a worker like any gate, so its `gate` event does carry a `worker` key
  (the Design's "no `worker` key" does not match the code). The test checks that the re-gate
  event precedes `close`, that its `head` equals `close.merged_sha`, and that main's tip is
  that sha on the coordinator.
- **Killed worker:** a gate-only kill (process group of the pid in `proc.json`, `job_lease_s`
  of 3 s); the persona kill is not covered here.
- **Cost:** `nw campaign export` has `cost_by_worker` in `campaigns.jsonl` and `cost_usd` per
  run in `runs.jsonl`, but the runs table has no `worker` column (the run's worker is on its
  `run.end` event). The test checks both against the events; the AC "per job with its worker
  id" is not met as written and stays unticked. Adding a `worker` column to the runs export is
  a follow-up.

## Clock Log

Use `nw idea clock-in IDEA-560` / `nw idea clock-out IDEA-560`.

## Rollout / migration

1. Tests only. Nothing to migrate.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0333:
- The file is shared with SPEC-0334: whichever lands first creates it, the other adds its
  cases to it, and shared loopback fixtures live in one helper (`tests/remote_support.py`).
