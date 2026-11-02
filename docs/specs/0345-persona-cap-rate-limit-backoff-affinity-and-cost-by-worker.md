---
id: SPEC-0345
title: "Persona cap, rate-limit backoff, campaign affinity and cost by worker"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-02
source_idea: IDEA-560
parent: SPEC-0310
tags: [agentic, remote, workers, personas, cost]
depends_on: [SPEC-0344]
---

## Context

With persona jobs on workers ([SPEC-0344](./0344-remote-persona-jobs-with-bundle-write-back.md)),
adding workers adds concurrent persona runs. The queue's slot count only caps campaigns on
one host, and the `claude` rate limit belongs to the account, not to the host. This spec
assumes every host uses the same account (see Non-goals). Cost is already in each run's
result, but nothing attributes it to a worker.

This is the second part of the split of
[SPEC-0333](./0333-remote-persona-runs-with-bundle-write-back.md).

## Goals / Non-goals

**Goals**
- One campaign's runs stick to one worker while it is healthy (**affinity**), so the
  worker's object cache and tree stay warm.
- An optional host-independent cap on concurrent persona runs
  (`agentic.workers.max_personas`), so adding workers does not overrun the account's rate
  limit. On a rate-limit failure, persona dispatch backs off globally, local dispatch
  included.
- Cost is attributed per worker in `nw campaign show` and the campaign export, and remote
  costs count toward the campaign budget.

**Non-goals**
- Load-aware scheduling.
- Per-worker rate limits: the limit is the account's.
- Workers logged into a different claude account than the coordinator: the cap and
  backoff assume one account across all hosts; per-account limits are out of scope.
- The `worker` key in run records and events (SPEC-0344).
- Gating spec-review, conductor or steward runs.

## Decision

Campaign state gains `remote_affinity`. A global persona cap (unset by default) and a global
rate-limit backoff form a **dispatch gate** that every campaign persona dispatch passes
before SPEC-0344's eligibility check; a closed gate leaves the step pending, it never falls
back to local. Stuck detectors skip campaigns held by the gate. Cost attribution reads the
`worker` key SPEC-0344 puts in the run record.

## Design

- **Scope.** The cap and the backoff apply to campaign persona runs only (run dirs under
  each campaign's `runs/` plus `remote-jobs/` records with `kind: persona`); spec-review
  (`spec_review.py`), conductor and steward runs are neither counted nor gated.
- **Affinity.** Campaign state gains `remote_affinity: <worker>|null`. Only a remote persona
  run sets affinity (the first one placed on a worker while affinity is null); gate runs
  only read it. Later persona runs and gate runs of the campaign prefer the affine worker's
  pools and fall through to another eligible worker only when the affine worker is **full**
  (no free slot in its pools) or **failing**. Failing means: the worker is disabled, or
  `nw workers check` last failed for it, or it had a `worker_job_lost` for this campaign.
  Falling through because the worker is full does not clear affinity. Affinity is cleared on
  `worker_job_lost` (SPEC-0341) for this campaign.
- **Dispatch gate.** The cap and the backoff are a dispatch gate checked before eligibility,
  for local and remote runs alike. When the gate is closed, the step is not started and
  stays pending; the queue retries it on a later tick. Nothing falls back to local. A
  fan-out dispatches only as many members as the cap leaves room for, and the rest wait
  (they start on later ticks as room frees up). SPEC-0344's sentence "The global persona cap
  and the rate-limit backoff are added to this list by SPEC-0345" is superseded by this
  gate: they are not eligibility rules.
- **Global cap.** `agentic.workers.max_personas` defaults to null (no cap); when set, it
  must be an int ≥ 1 (config validation rejects other values). When set, it bounds live
  campaign persona runs across local and all workers. Live runs are counted from local live
  persona run dirs under each campaign's `runs/` plus `remote-jobs/` records with
  `kind: persona` that are not final.
- **Rate limit.** A new `events.RATE_LIMIT_MARKERS = ("rate limit", "429", "overloaded",
  "529")`, a subset of `events._TRANSIENT_MARKERS`. A run failure counts as a rate limit only
  when the claude result is an API error (`is_error` with an API-error subtype/status from
  the result JSON) and its error matches `RATE_LIMIT_MARKERS` as whole tokens (`\b429\b`,
  `\b529\b`, `rate limit`, `overloaded`) in the API error message, not anywhere in the
  transcript tail (`result_tail` contains model and test output, cf. `divergence.py:21-22`).
  `usage_limit` and `session limit` keep their existing handling and never set the backoff.
  - State lives in `<agentic_dir>/rate-limit.json {until, backoff_s}`, written atomically;
    every read-modify-write holds a lock on `<agentic_dir>/rate-limit.lock`.
  - A **backoff episode** starts with the first rate-limit failure while `now >= until`
    (or no file): it sets `until = now + backoff_s` and appends one
    `rate_limited {until, backoff_s}` queue event (added to `EVENT_TYPES`); the next
    episode's `backoff_s` is doubled, capped at 900 s. The first episode uses 60 s.
  - A rate-limit failure while `now < until` neither doubles nor extends the backoff and
    appends no event.
  - A successful persona run resets `backoff_s` to 60 only if the run started after the
    current `until`; a run that started before the episode ended does not reset it.
  - No new campaign persona run is dispatched, local or remote, while `now < until`.
- **Held campaigns and detectors.** While `rate-limit.json` `until` is in the future, or a
  step is held by the persona cap, `campaign_silent` and the conductor's stuck count
  (`state_stuck` included) skip the held campaign; the `rate_limited` event and a
  `held_by: rate_limit|persona_cap` field in `nw queue status` show why it waits.
- **Cost per worker.**
  - `RunResult` is unchanged.
  - `campaign_export` and `nw campaign show` gain `cost_by_worker` (the `worker` key from the
    run record; local runs count under `local`).
  - A remote run's cost is added to `state.cost.total_usd` exactly like a local run's, so
    the campaign cap check (`queue._cap_blocks`) parks the next run exactly as for a local
    run of the same cost; per-run `max_budget_usd` is passed to the remote `claude` the same
    way.

## Alternatives considered

- **Per-worker rate limits.** The limit belongs to the account, not to the host, so the cap
  and the backoff are global.
- **Affinity as a hard pin.** A failing worker would then stall the campaign; affinity is a
  preference and falls through.
- **Cap and backoff as eligibility rules (SPEC-0344's list).** A failed eligibility rule runs
  the persona locally, which would bypass both; a separate gate before eligibility holds the
  step instead.
- **Default cap = local queue slots.** Fan-outs already exceed the slot count today, and
  slots differ per profile; the default would throttle existing local runs. The cap is
  opt-in.

## Acceptance criteria

- [ ] Affinity: three consecutive persona runs and one gate run of one campaign land on the
      affine worker while it has slots; with the worker full, the run falls through to
      another worker without clearing affinity; a failing worker (disabled, failed check, or
      `worker_job_lost` for the campaign) is skipped; affinity is cleared on loss; a gate
      run never sets affinity.
- [ ] With `max_personas` set, live campaign persona runs across local and remote never
      exceed it; a fan-out dispatches only as many members as fit and the rest start on
      later ticks; a capped step stays pending and is never run locally as a fallback.
- [ ] Spec-review, conductor and steward runs are neither counted toward the cap nor held by
      the backoff.
- [ ] A rate-limit API error stops all campaign persona dispatch (local and remote) until
      `until`; several rate-limit failures within one episode produce one `rate_limited`
      event and no extra doubling; the next episode doubles `backoff_s` up to 900 s; a
      success from a run started after `until` resets it to 60; a `usage_limit` error, and a
      failed run whose tail merely contains `429` (e.g. `x.py:429: AssertionError`), do not
      set it.
- [ ] A campaign held by the backoff or the cap raises no `campaign_silent` and is not
      counted as stuck; `nw queue status` shows `held_by: rate_limit|persona_cap`.
- [ ] `nw campaign show` and the export show `cost_by_worker` (local runs under `local`), and
      a remote run's cost is added to `state.cost.total_usd` so the campaign cap check
      (`_cap_blocks`) parks the next run exactly as for a local run of the same cost.
- [ ] With no workers registered and the default config, behaviour is unchanged (fan-outs run
      all members concurrently) except that the rate-limit backoff also applies to local
      dispatch.

## Test plan

- **Automated tests** (`tests/test_remote_persona_limits.py`, offline), with the loopback
  workers and `FakeRunner`:
  - **affinity:** three persona runs and one gate run land on the affine worker; with it
    full, a run falls through and `remote_affinity` is unchanged; a disabled worker, one
    whose last check failed, and one with a `worker_job_lost` for the campaign are skipped;
    affinity cleared on loss; a gate run with null affinity leaves it null;
  - **global cap:** with `max_personas: 2` and two workers plus local, only 2 run at once; a
    3-member reviewer fan-out with one slot of room dispatches one member and the others
    start on later ticks; the held step never runs locally as a fallback; invalid values
    (`0`, `"2"`) are rejected by config validation;
  - **scope:** a live spec-review run is not counted toward the cap and starts during a
    backoff;
  - **rate limit:** a fake API-error 429 sets the backoff with `backoff_s` 60, nothing
    dispatches before `until`, a success from a run started after `until` resets it, a
    success from a run started before does not; three runs failing with 429 within one
    second produce `backoff_s` 60 and one `rate_limited` event; a second episode doubles to
    120, and repeated episodes stop at 900; a `usage_limit` error does not set it; a failed
    run whose tail contains `x.py:429: AssertionError` does not set the backoff;
  - **detectors:** with a frozen clock, a campaign held through a 900 s backoff raises no
    `campaign_silent` and is not in the stuck count, and `nw queue status` shows
    `held_by: rate_limit`; a cap-held step shows `held_by: persona_cap`;
  - **cost:** `cost_by_worker` in `campaign show` and the export; a remote run's cost raises
    `state.cost.total_usd` and `_cap_blocks` parks the next run as for an equal local cost;
  - **no workers:** with no workers and the default config, a reviewer fan-out runs all
    members concurrently as before.
- **Manual verification:**
  1. Run one real campaign with persona jobs on `box` and check `nw campaign show` for
     `cost_by_worker`.
  2. Observe one 429 backoff on the real account if it occurs (see SPEC-0346), and check
     `nw queue status` for `held_by: rate_limit`.
- **Regression guard:** affected tests while iterating; the persona-run, queue and
  `events` detector tests run unchanged.

## Clock Log

Use `nw idea clock-in IDEA-560` / `nw idea clock-out IDEA-560`.

## Rollout / migration

1. `max_personas` is unset by default, so local concurrency is unchanged; set it when
   persona workers are enabled (SPEC-0344).
2. The rate-limit backoff is active from the start; it only triggers on API-error rate
   limits, so ordinary failures behave as before.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0333 and in review d5154998fba3:
- The persona cap and the rate-limit backoff are global, with a 60 s to 15 min backoff, and
  form a dispatch gate rather than eligibility rules.
- The cap is opt-in (default null).
- Affinity is a preference, cleared on `worker_job_lost`.
