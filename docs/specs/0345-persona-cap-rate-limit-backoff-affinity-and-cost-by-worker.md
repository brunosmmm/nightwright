---
id: SPEC-0345
title: "Persona cap, rate-limit backoff, campaign affinity and cost by worker"
status: in-progress
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

- [x] Affinity: three consecutive persona runs and one gate run of one campaign land on the
      affine worker while it has slots; with the worker full, the run falls through to
      another worker without clearing affinity; a failing worker (disabled, failed check, or
      `worker_job_lost` for the campaign) is skipped; affinity is cleared on loss; a gate
      run never sets affinity.
- [x] With `max_personas` set, live campaign persona runs across local and remote never
      exceed it; a fan-out dispatches only as many members as fit and the rest start on
      later ticks; a capped step stays pending and is never run locally as a fallback.
- [x] Spec-review, conductor and steward runs are neither counted toward the cap nor held by
      the backoff.
- [x] A rate-limit API error stops all campaign persona dispatch (local and remote) until
      `until`; several rate-limit failures within one episode produce one `rate_limited`
      event and no extra doubling; the next episode doubles `backoff_s` up to 900 s; a
      success from a run started after `until` resets it to 60; a `usage_limit` error, and a
      failed run whose tail merely contains `429` (e.g. `x.py:429: AssertionError`), do not
      set it.
- [x] A campaign held by the backoff or the cap raises no `campaign_silent` and is not
      counted as stuck; `nw queue status` shows `held_by: rate_limit|persona_cap`.
- [x] `nw campaign show` and the export show `cost_by_worker` (local runs under `local`), and
      a remote run's cost is added to `state.cost.total_usd` so the campaign cap check
      (`_cap_blocks`) parks the next run exactly as for a local run of the same cost.
- [x] With no workers registered and the default config, behaviour is unchanged (fan-outs run
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

### Test record (2026-11-02)

- `uv run pytest -q -n 4 tests/test_remote_persona_limits.py`: 41 passed. This covers the
  affinity, global cap, scope, rate-limit, detector, cost and no-workers cases of the Test
  plan.
- `uv run pytest -q -n 4 tests/test_remote_personas.py tests/test_remote_gates.py
  tests/test_agentic_quiet*.py tests/test_agentic_watchdog*.py`: 281 passed.
- Related files, 30 in all (queue, reviewers, review triage, e2e, events, quiet, adopt,
  autopark, campaign export and CLI, dashboard, TUI models, workers, remote job records,
  skills, split queue): 1226 passed, 2 skipped, 2 failed. Both failures were the export
  column pins, which did not yet list the new `cost_by_worker` column. After updating them,
  `tests/test_agentic_campaign_export.py` gives 66 passed, 2 skipped.
- Full suite left to gating (`tools/affected_tests.py` selects the full suite).
- Gate fix (helper move): `tests/test_remote_persona_limits.py` imported helpers from test
  modules that reference `.nw/project.yaml`, so the replay case
  `test_recorded_fallback[SPEC-0338 .nw/project.yaml]` reached 91/227 files and fell back to
  the full suite. The loopback `World`/`Remote` helpers moved from `test_remote_gates.py`, and
  the persona-job helpers from `test_remote_personas.py`, into the non-test module
  `tests/remote_support.py`; both test files import them back. The e2e and reviewer fan-out
  helpers are copied into `tests/fanout_support.py`, which writes the temp repo's config via
  `P.CONFIG_RELPATH`. The selection is back to 90/227: the same set as before minus the new
  file. `uv run pytest -q -n 4 tests/test_affected_tests_replay.py
  tests/test_remote_persona_limits.py`: 46 passed. The affected-tests, remote gate, remote
  persona, worker drain/retry, remote job record, e2e and reviewer files: 482 passed.
- Full suite `uv run pytest -q -n 4` after the helper move: 5782 passed, 5 skipped, 1 failed.
  The failure was `test_test_waits.py::test_no_bare_join_before_result_read`, caused by a
  numeric `th.join(10)` in the new file. It now uses `waits.join`. After that,
  `tests/test_test_waits.py`, `tests/test_remote_persona_limits.py` and
  `tests/test_affected_tests_replay.py` give 50 passed.
- Manual verification was **not** performed. This host's only worker, `box`, takes gates
  only and has no cached `async`/`persona` feature, and a real 429 cannot be produced on
  demand. Both steps are left to the human and to SPEC-0346.

### As shipped (2026-11-02)

- **Module.** `agentic/persona_gate.py` holds the gate (`Gate`), the backoff
  (`is_rate_limit`, `note_run`, `rate-limit.json` under `rate-limit.lock`), the holds
  (`persona-holds.json`, `held_by`), affinity (`affinity`, `failing`, `order`,
  `set_affinity`, `clear_affinity`) and `cost_by_worker`.
- **Gated steps.** The gate runs in the tick after `plan_step` (so after the campaign cost-cap
  park) and before `_prepare_run`, which is before SPEC-0344's eligibility check in
  `_do_persona`. Gated step kinds: `implement`, `triage`, `review` (the review triager
  included), `write_spec` and `resolve`. The campaign's `spec_review` step is not gated, and
  `spec_reviewer` runs are not counted. Gate, close, open and finalize steps are not persona
  runs and are never held.
- **Counting live runs.** The live count is taken once per tick, and only when `max_personas`
  is set. It is the union, keyed by `(campaign, run_id)`, of the open runs of every active
  campaign (a `run.start` with no `run.end`, which covers local runs, detached runs and the
  local side of remote runs) and the live `kind: persona` remote-job records. Runs the tick
  admits are added to it as they are admitted.
- **A partly capped fan-out.** The trimmed step carries the held members in `Step.held`.
  When the flight is applied, `_apply_reviews` stores the valid verdicts and stays in
  `REVIEWING` with reason `persona_cap` and a `persona cap: … wait …` note, without routing.
  Members that were held use no retry. A member that ran without a valid verdict uses its one
  retry, as in a full pass. The next ticks plan the pending reviewers again.
- **Rate-limit detection.** `api_error_status` decides when the result JSON has it: 429 or
  529 counts. Without it, the result message must start with `API Error` and its first line
  must match `\b429\b`, `\b529\b`, `rate[ _]limit` or `\boverloaded\b`.
  `rate_limit` also matches because it is the API's error type name. A message naming
  `usage_limit`, `usage limit` or `session limit` never counts.
  `events.RATE_LIMIT_MARKERS` is the documented subset of `_TRANSIENT_MARKERS`.
- **Backoff file.** `{until (ISO), backoff_s, reset?}`. A reset writes `backoff_s: 60` plus
  `reset: true`, so the next episode uses 60 s instead of doubling the stored value. A run's
  start time is taken just before `runner.run`. An adopted run's start time is unknown, so it
  never resets the backoff. Every campaign persona run's result is folded in:
  `_do_persona` (after any write-back re-run), `_do_write_spec` and `_do_resolve`.
- **`rate_limited` event.** It is a queue event (`EVENT_TYPES`) carrying the failing run's
  campaign. The quiet conductor holds it (`quiet.RULES` and the `nw-conduct` skill's
  always-held list), because dispatch resumes on its own.
- **`nw queue status`.** No such command existed, so `status` is registered as an alias of
  `nw queue list`. `nw.queue.v1` items gain `held_by` (`rate_limit`, `persona_cap` or null;
  null while a run is in flight), and the payload gains `personas`:
  `{max_personas, rate_limit: {until, backoff_s, active}}`. The table shows
  `STATE [held: …]`.
- **Holds and detectors.** The tick rewrites `persona-holds.json` with the campaigns it held
  (each with `held_by` and `since`). `campaign_silent` (`watchdog_checks`) and
  `stuck_items` (`state_stuck`, and the conductor ping's stuck list and count) skip a
  campaign with no run in flight in either of two cases. Case 1: the backoff is running and
  the campaign is in a persona state (`IMPLEMENTING`, `TRIAGING`, `REVIEWING`,
  `SPEC_WRITING`) or has a `rate_limit` hold. Case 2: the campaign has a `persona_cap` hold.
- **Affinity.** State gains `remote_affinity` and `remote_affinity_at`. `dispatch` sets them,
  under the item lock, when it places the first persona run while the affinity is null.
  `failing` means one of: the worker is disabled; the worker's `check_ok` is false (`nw
  workers check` now records `check_ok` and `checked_at` on the registry record); or the
  campaign's ledger has a `worker_job_lost` on that worker at or after
  `remote_affinity_at`. Such a loss also voids the affinity when it is read, which covers gate
  losses without writing campaign state from the gate thread. A persona job's loss now also
  appends `worker_job_lost` to the campaign ledger (gate losses already did) and clears the
  stored affinity. Gate runs reorder `pools_for`'s pools so that the affine worker comes
  first, unless it is failing; a full pool falls through in `gate_slots.acquire`. Gate runs
  never set the affinity.
- **Cost.** `campaign_export` gains the `cost_by_worker` campaign column. `nw campaign show`
  gains `cost_by_worker` in its JSON output and a `cost by worker` line in the text output.
  Both sum `charged_usd` of `run.end` by `worker`, with no worker counted as `local`. Remote
  costs already went through `_record_run`, so `_cap_blocks` treats them like local costs.
- **Test support.** A `FakeRunner` script entry may carry
  `api_error: {status, message}`, which yields an API-error result.

## Clock Log

Use `nw idea clock-in IDEA-560` / `nw idea clock-out IDEA-560`.

## Rollout / migration

1. `max_personas` is unset by default, so local concurrency is unchanged; set it when
   persona workers are enabled (SPEC-0344).
2. The rate-limit backoff is active from the start; it only triggers on API-error rate
   limits, so ordinary failures behave as before.

## Definition of done

- [x] Acceptance criteria all met.
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
