---
id: SPEC-0343
title: "Worker drain"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, ops]
depends_on: [SPEC-0341]
---

## Context

> **Amendment 2026-11-01 (scope narrowed to drain only).** Bootstrap moved to
> [SPEC-0354](./0354-nw-workers-bootstrap.md) because it needs only SPEC-0337, not the job
> records of SPEC-0341, and the human wants it ahead of the job chain. We edit this spec in
> place rather than supersede it: it is `accepted` with no code or tests shipped, its id,
> parent, dependency and drain design are unchanged, and the epic already uses dated
> amendments for the same kind of re-split. Only the bootstrap content was removed (it lives
> on, expanded, in SPEC-0354); a supersede would orphan the drain design under a new id for
> no gain.

Taking a worker out of service means disabling it
and hoping no job is running. With jobs that outlive the connection
([SPEC-0340](./0340-worker-async-job-protocol.md),
[SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md)), the second needs a
graceful path.

This is the fourth part of the split of
[SPEC-0332](./0332-worker-owned-async-jobs.md). It is independent of the retry work
([SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md)) and can land in
parallel with it.

## Goals / Non-goals

**Goals**
- `nw workers drain` takes a worker out of service without losing running jobs.

**Non-goals**
- Persona prerequisites (`claude` login): SPEC-0344's `check --auth`.
- Automatic worker upgrades.

## Decision

Drain marks the worker `draining`; the queue disables it once its last remote job
record is final.

## Design

- **`nw workers drain NAME`** stops new jobs from being placed on the worker
  (`draining: true`, added to the registry schema). Its running jobs finish. `nw workers
  list` shows `draining (k jobs)`. On each tick the queue sets `enabled: false` and clears
  `draining` for a draining worker with no non-final `remote-jobs` record (SPEC-0341).

## Alternatives considered

- **Disable immediately on drain.** That would orphan running jobs.

## Acceptance criteria

- [ ] `nw workers drain` stops new placements on the worker; running jobs finish; the queue
      then sets `enabled: false` and clears `draining`.
- [ ] `nw workers list` shows `draining (k jobs)`.

## Test plan

- **Automated tests** (`tests/test_workers_drain.py`, offline):
  - **drain:** a draining worker gets no new job, its running job finishes, and the worker
    then becomes disabled; `list` output shows the draining count.
- **Manual verification:**
  1. Start a gate on `box`, run `nw workers drain box`, and confirm the gate finishes and
     the worker ends up disabled.
- **Regression guard:** affected tests while iterating; SPEC-0337's registry tests run
  unchanged.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. New command only. The registry gains an optional `draining` field; absent means false.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0332:
- The queue (not the human) disables a drained worker, on a tick.
