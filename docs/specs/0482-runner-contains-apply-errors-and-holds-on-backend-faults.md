---
id: SPEC-0482
title: The runner contains a campaign's apply error and holds steps on a backend fault
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, runner, resilience, cursor]
---

## Context

On 2026-11-08 at 21:19:42 the runner (`nw queue run`) died with `KeyError: 'sha'` raised from
`_apply_spec_review`. `_harvest` calls `_apply` through `_guarded(guard, ...)`, and only the
crash sweep passes `guard=True`. In a normal tick one campaign's apply bug therefore killed the
whole runner. The result's `run.start` had no `run.end`, so the next runner re-adopted the
same run and crashed again: a crash loop. (That KeyError is fixed on its own branch; this spec
is the general containment.)

Separately, a runner relaunched at 21:11 had no `CURSOR_API_KEY` while `agentic.runner: cursor`
was configured. Every `gate_triager` run failed at once with "CURSOR_API_KEY is not set"
(`cursor_exec.py`, `error_code: startup`). The queue treated those failures as verdicts: the
triage retry was spent and the campaign parked `triage_escalated`, which looks exactly like a
triager that judged the campaign. Red-main fix-forward campaigns failed the same way.

The human's requirement: "the runner cannot crash and must self heal". Restarting a dead runner
is [SPEC-0483](./0483-runner-under-a-systemd-user-unit.md); this spec keeps a live runner from
dying of one campaign and keeps an environment fault from being read as a verdict.

## Goals / Non-goals

**Goals**
- An exception while applying one campaign's result parks only that campaign and the runner
  keeps ticking, in every tick and in the drain and crash sweeps.
- The park is idempotent across restarts: a re-adopted result cannot crash-loop.
- A persona run that cannot start for a missing credential or SDK spends no retry, does not
  park, holds the step, and raises one escalation naming what is missing.
- The runner warns loudly at start when the configured backend fails its preflight.

**Non-goals**
- Fixing `_apply_spec_review`'s KeyError (another branch).
- A preflight for `pi` (its probe is a 30 s node subprocess; too heavy per tick) or `claude`
  (its failures already surface as `launch failed`). Only `cursor` has a cheap in-process probe.
- The spec reviewer (`_do_spec_review`): its runs are ungated and its apply path is being edited
  elsewhere; a cursor key fault there still fails the review as today.

## Decision

1. **Containment.** `_apply` becomes a wrapper: it calls the old body (`_apply_step`) and on any
   `Exception` calls `_contain_apply_error`. `_guarded` (the harvest) does the same for
   `_finish_reviews`, in a normal tick as in the crash sweep. Containment:
   - journals `apply_error` (campaign, action, from_state, run ids, error, traceback tail);
   - under the campaign lock, re-reads the state from disk and appends a `run.end` (via
     `_record_run`, falling back to a synthetic failure when the result itself cannot be
     recorded) for every run of the result still open on the ledger. Adoption reads only open
     runs, so a restart has nothing to re-apply: this is the idempotency;
   - parks the campaign `apply_error` with `detail` = "`<action>` apply failed: `<Type>: <msg>`"
     and `park.apply_error = {action, error, run_ids, from_state}`. The park is the conductor's
     (`park_owner` default) and projects to a `parked` event like any park;
   - `resume_to` is `_resume_target(<state on disk>)`: the step whose result failed is re-run
     (OPENING -> QUEUED, TRIAGING -> GATING, a WAITING campaign keeps its `waiting.resume_to`).
     The state on disk is the safe one to park from: `_apply_step` mutates only the in-memory
     snapshot until `finish` writes it, so either nothing was written (park from the step's
     state) or the transition was written (park from the new state, re-run from there);
   - a campaign already PARKED or terminal is left as it is (a note records the failure);
   - if the park itself fails, `apply_error_uncontained` is journaled and the loop goes on.
     Containment never raises.
2. **Backend preflight.** `backend_preflight.check("cursor")` (cursor-sdk imports,
   `CURSOR_API_KEY` set; no subprocess, no billed call) is applied at three points:
   - the dispatch gate (`persona_gate.Gate.admit`, once per tick per backend): a campaign whose
     steps resolve to cursor, with no persona worker that could take them, is held
     `backend_unavailable` (like `rate_limit`; `held_by` reports it so the stuck detectors stay
     quiet while the escalation covers it);
   - the spawn (`_preflight_hold` in `_do_persona` for fresh runs and in `_gated_runner` for
     `write_spec` / `resolve`): a local `CursorRunner` is replaced by a `PreflightHeldRunner`
     that never starts, claims the run dir `held`, and returns an interrupted, uncharged result.
     A run dir that already has `proc.json` or `launch.json` (adoption) is left alone;
   - the result (`_do_persona`): a failed run whose text is the cursor child's startup fault
     ("CURSOR_API_KEY is not set", "cursor-sdk is not installed") becomes interrupted and
     uncharged (covers a run started by a keyless runner and adopted by one with the key).
   Interrupted results leave the state unchanged and spend no triage or review retry (the
   existing path for rate-limit holds). `note_fault` journals `backend_preflight_failed` once
   per process and fault and files one queue-wide escalation (`kind: backend_unavailable`,
   `topic: human_settings`) while none is open for that backend. When the var is mapped in
   `agentic.credentials` (SPEC-0483) the credential's own escalation names the fix and this
   one is not filed.
3. **Start warning.** `run()` calls `backend_preflight.startup_check(cfg)` after the `start`
   journal record: when `cursor_needed(cfg)` and the probe fails it prints a `WARNING:` line to
   stderr (the runner log / journal) and notes the fault. It never stops the runner.

## Design

- `src/nightwright/agentic/queue.py`: `PARK_REASONS` gains `apply_error`; `_apply` /
  `_apply_step` / `_contain_apply_error` / `_result_runs` / `_close_result_runs` /
  `_group_result` / `_guarded(guard, ctx, st, fn, report, res)`; `_preflight_hold`;
  `_do_persona` env-fault conversion; `run()` calls `CR.ensure` and `BP.startup_check`.
- `src/nightwright/agentic/backend_preflight.py` (new): `check`, `env_fault`, `note_fault`,
  `startup_check`.
- `src/nightwright/agentic/runner.py`: `PreflightHeldRunner`; `mark_held(req, by=...)`.
- `src/nightwright/agentic/persona_gate.py`: `Gate.backend_fault`, the `backend_unavailable`
  hold, `held_by` reports it.
- Docs: `docs/product/loop/operate.md` park table, `skills/nw-conduct/SKILL.md` row
  `parked.apply_error`.

## Alternatives considered

- **Catch in `run()` and keep looping.** Loses which campaign failed and still leaves the run
  open, so every tick (and every restart) re-applies it.
- **Skip the result without parking** (the crash sweep's old journal-only behavior). The
  campaign's step is then planned again with the bad state; a deterministic bug repeats every
  tick, spending a run each time.
- **Park `triage_escalated` / `open_failed` with a better detail for env faults.** Still a park
  for something the queue can wait out; the human fixes the environment, not the campaign.

## Acceptance criteria

- [x] An exception raised while applying a campaign's result (normal tick, drain, crash sweep)
      parks that campaign `apply_error` with the exception in `park.detail`, journals
      `apply_error`, projects a `parked` event, and the runner keeps ticking (no `crash`).
- [x] The failed result's runs are closed (`run.end`), so `_open_runs` is empty and a restart
      re-adopts nothing; a later tick does not touch the parked campaign.
- [x] A campaign already PARKED or terminal is not re-parked; a failing park is journaled
      `apply_error_uncontained` and does not raise.
- [x] With the cursor backend failing its preflight, the gate holds the step
      `backend_unavailable` and `held_by` reports it; a fresh cursor spawn returns an
      interrupted, uncharged result; a finished run whose text is the cursor startup fault is
      turned into an interrupted one; no triage retry is spent and nothing parks.
- [x] One `backend_unavailable` escalation is filed while none is open; `runner start` prints a
      `WARNING:` and journals `backend_preflight_failed` when the probe fails.
- [x] `park_owner("apply_error") == "conductor"`; the operate page and the conductor skill name
      `apply_error`.

## Test plan

- **Automated tests** (`tests/test_runner_selfheal.py`):
  - `test_apply_error_parks_only_that_campaign_and_closes_its_run`: an e2e FakeRunner
    campaign whose implement apply raises `KeyError('sha')` under `nw queue run --until-idle`:
    exit 0, PARKED `apply_error` with `KeyError` in the detail, `resume_to == "IMPLEMENTING"`,
    journal `apply_error`, no `crash`, `_open_runs` empty, `events.sync` emits `parked`; a
    second `Q.run(max_ticks=2)` leaves it parked.
  - `test_contain_leaves_a_parked_campaign_and_never_raises`: containment on a PARKED
    campaign adds a note only; on an unknown campaign journals `apply_error_uncontained`.
  - `test_harvest_contains_finish_reviews_error`: `_finish_reviews` raising in a normal
    harvest parks `apply_error` and the other flight still applies.
  - `test_gate_holds_backend_unavailable_and_escalates_once`,
    `test_preflight_held_runner_is_interrupted_and_uncharged`,
    `test_do_persona_turns_cursor_startup_fault_into_interrupted`,
    `test_startup_check_warns_and_journals`.
  - Existing `test_crash_sweep_journals_harvest_error_and_applies_the_rest` still passes.
- **Manual verification:** none beyond the tests (the live runner is not touched).
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus the files
  touched; `test_docs_two_pillars` (park table), `test_park_owner_table`.

## As shipped

- Shipped as designed. The crash sweep (`guard=True`) also parks `apply_error` now (it still
  journals `harvest_error`, and its drain report keeps its old shape); a campaign the sweep
  cannot find is journaled `apply_error_uncontained`.
- `tests/test_remote_workers_integration.py::test_a_duplicate_submit_…` simulated a runner that
  dies mid-apply through an apply exception; with containment that no longer kills the runner,
  so the test restores the pre-0482 harvest for its crash (its subject, adoption dedupe, is
  unchanged).
- Test record (2026-11-09): `tests/test_runner_selfheal.py` 7 passed locally; the affected
  set (minus the conftest trigger) on the box, GATEJOB-0994: 6119 passed, 3 failed; the three
  (output-guard allowlist, `systemd-analyze` on a box without a user session, the remote
  crash test above) were fixed and pass locally. Re-gate GATEJOB-0995: 6121 passed, 1
  failed: `test_forged_worker_frames_are_refused_and_append_nothing[type-park]`, whose
  frame forger left no marker (worker transport timing; the campaign still MERGED). It
  passed in GATEJOB-0994 and passes locally (all 5 params, and `[type-park]` 3 of 3); it
  touches no code this spec changed, so it is recorded as a box flake.

## Rollout / migration

Lands with the code; the next runner start picks it up (re-exec on merge).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
