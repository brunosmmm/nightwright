---
id: SPEC-0345
title: "Persona cap, rate-limit backoff, campaign affinity and cost by worker"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-560
parent: SPEC-0310
tags: [agentic, remote, workers, personas, cost]
depends_on: [SPEC-0344]
---

## Context

With persona jobs on workers ([SPEC-0344](./0344-remote-persona-jobs-with-bundle-write-back.md)),
adding workers adds concurrent persona runs. The queue's slot count only caps campaigns on
one host, and the `claude` rate limit belongs to the account, not to the host. Cost is
already in each run's result, but nothing attributes it to a worker.

This is the second part of the split of
[SPEC-0333](./0333-remote-persona-runs-with-bundle-write-back.md).

## Goals / Non-goals

**Goals**
- One campaign's runs stick to one worker while it is healthy (**affinity**), so the
  worker's object cache and tree stay warm.
- A host-independent cap on concurrent persona runs (`agentic.workers.max_personas`), so
  adding workers does not overrun the account's rate limit. On a rate-limit failure,
  dispatch backs off globally, local dispatch included.
- Cost is attributed per worker in `nw campaign show`, the campaign export and budgets.

**Non-goals**
- Load-aware scheduling.
- Per-worker rate limits: the limit is the account's.
- The `worker` key in run records and events (SPEC-0344).

## Decision

Campaign state gains `remote_affinity`; a global cap and a global rate-limit backoff gate
every persona dispatch; cost attribution reads the `worker` key SPEC-0344 puts in the run
record.

## Design

- **Affinity.** Campaign state gains `remote_affinity: <worker>|null`, set by the first
  remote persona run. Later persona runs and gate runs for the campaign prefer that worker's
  pools and fall through to others only when it is full or failing. Affinity is cleared on
  `worker_job_lost` (SPEC-0341).
- **Global cap.** `agentic.workers.max_personas` (default: the local queue slots) bounds
  live persona runs across local and all workers. Live runs are counted from local live run
  dirs plus `remote-jobs/` records with `kind: persona` that are not final. This joins the
  eligibility list of SPEC-0344.
- **Rate limit.** A new `events.RATE_LIMIT_MARKERS = ("rate limit", "429", "overloaded",
  "529")`, a subset of `events._TRANSIENT_MARKERS`. A run failure whose error matches one
  sets `<agentic_dir>/rate-limit.json {until, backoff_s}`, written atomically. `usage_limit`
  and `session limit` keep their existing handling. The backoff starts at 60 s and doubles
  up to 15 min; a successful run resets it. No new persona job is dispatched, local or
  remote, before `until`. A `rate_limited {until, backoff_s}` queue event is appended once
  per backoff (added to `EVENT_TYPES`).
- **Cost per worker.**
  - `RunResult` is unchanged.
  - `campaign_export` and `nw campaign show` gain `cost_by_worker` (the `worker` key from the
    run record; local runs count under `local`).
  - Budgets (`max_budget_usd` per run and the campaign caps) count remote costs exactly like
    local ones, because the cost comes from the same `claude` result.

## Alternatives considered

- **Per-worker rate limits.** The limit belongs to the account, not to the host, so the cap
  and the backoff are global.
- **Affinity as a hard pin.** A failing worker would then stall the campaign; affinity is a
  preference and falls through.

## Acceptance criteria

- [ ] Affinity: three consecutive runs of one campaign land on the same worker while it has
      slots; affinity is cleared on loss.
- [ ] The global persona cap holds across local and remote runs.
- [ ] A rate-limit failure stops all persona dispatch (local and remote) until the backoff
      ends; a success resets the backoff; one `rate_limited` event is appended per backoff;
      a `usage_limit` error does not set it.
- [ ] Run records, `nw campaign show` and the export attribute cost per worker
      (`cost_by_worker`); budgets count remote costs.
- [ ] With no workers registered, behaviour is unchanged except that the rate-limit backoff
      also applies to local dispatch.

## Test plan

- **Automated tests** (`tests/test_remote_persona_limits.py`, offline), with the loopback
  workers and `FakeRunner`:
  - **affinity:** placement across three runs, and affinity cleared on loss;
  - **global cap:** with `max_personas: 2` and two workers plus local, only 2 run at once;
  - **rate limit:** a fake 429 error sets the backoff, nothing dispatches before `until`, a
    success resets it, and one `rate_limited` event is appended; a `usage_limit` error does
    not set it;
  - **cost:** `cost_by_worker` in `campaign show` and the export, budget caps counted.
- **Manual verification:**
  1. Run one real campaign with persona jobs on `box` and check `nw campaign show` for
     `cost_by_worker`.
  2. Observe one 429 backoff on the real account if it occurs (see SPEC-0346).
- **Regression guard:** affected tests while iterating; the persona-run and queue tests run
  unchanged.

## Clock Log

Use `nw idea clock-in IDEA-560` / `nw idea clock-out IDEA-560`.

## Rollout / migration

1. `max_personas` defaults to the local queue slots, so nothing changes until persona jobs
   are enabled per worker (SPEC-0344).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0333:
- The persona cap and the rate-limit backoff are global, with a 60 s to 15 min backoff.
- Affinity is a preference, cleared on `worker_job_lost`.
