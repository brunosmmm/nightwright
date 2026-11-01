---
id: SPEC-0346
title: "Remote workers integration suite for gate and persona workers"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
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
  concern gate and persona workers, and passes.

**Non-goals**
- New product behaviour. This spec adds tests only (and fixes any bug the tests expose in
  the code of its dependencies).
- Pull-worker and signing cases (SPEC-0334).

## Decision

One integration file, driven through the real queue with `FakeRunner` and two loopback
workers (two temp data dirs), from admission to merge.

## Design

The cases, each a whole fake campaign:
- **no workers:** a golden ledger comparison against the pre-epic behaviour;
- **with workers:** same final branch and the same event types, plus `worker` keys;
- **a worker killed mid-run:** the job is retried on the other worker or locally and the
  campaign still merges;
- **a coordinator restart mid-job:** the job is adopted and the campaign still merges;
- **no git credentials on the worker side:** the job env is checked.

## Alternatives considered

- **Leave coverage to the per-spec tests.** They pass in isolation but never prove the
  pieces compose through the queue.

## Acceptance criteria

- [ ] `tests/test_remote_workers_integration.py` exists and its cases above pass offline.
- [ ] The no-workers golden ledger is byte-equal to the pre-epic ledger for the same fake
      campaign.
- [ ] With workers, the final branch and the event types equal the no-workers run, plus
      `worker` keys.
- [ ] A killed worker and a restarted coordinator each still end in a merged campaign.
- [ ] The worker-side env contains no git credentials.
- [ ] One 429 backoff is observed on the real account (manual), or recorded as not yet seen.

## Test plan

- **Automated tests:** the file itself (`tests/test_remote_workers_integration.py`), run with
  `uv run pytest tests/test_remote_workers_integration.py`.
- **Manual verification:** run a real campaign with gate and persona jobs on the second
  machine; note any 429 backoff in the epic's rollout notes.
- **Regression guard:** affected tests while iterating.

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
- The file is shared: SPEC-0334 appends its cases rather than creating a second file.
