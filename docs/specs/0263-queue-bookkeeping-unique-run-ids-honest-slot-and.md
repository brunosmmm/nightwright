---
id: SPEC-0263
title: "Queue bookkeeping: unique run ids, honest slot and profile parks, gated declared drops"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-324
parent: SPEC-0169
tags: [agentic, queue, review-follow-up]
---

## Context

This spec comes from a triage of the accumulated `wtfug:` review follow-ups, the minor
findings that reviewers raised on merged specs SPEC-0202..0259. Each finding kept here was
re-checked against `main` on 2026-10-27 and is still true. Each one is a piece of queue
bookkeeping in `src/wt/agentic/queue.py` that either corrupts campaign state or misleads the
conductor.

Kept findings:

- **IDEA-354** `wtfu:579916faeb5e` (SPEC-0253): *A reused run_id lets the tick sweep delete a
  live run's TMPDIR.* The code check found more damage than the finding names. If
  `store.write_state` fails after `run.start` (`_prepare_run`), `counters.runs` is never
  saved, so the next run mints the same `run_id`. It then shares the run directory
  (`prompt.md`, `exit.json` and the rest) with the earlier run.
- **IDEA-320** `wtfu:e126f813d352` (SPEC-0228): *Frequent occupancy drops keep re-stamping
  slot_freed_at, which can hide a WAITING item.* `_SlotWatch.check` stamps the time on every
  drop, even when a slot was already free. Under churn, `waiting_not_admitted` can then
  never reach its grace period.
- **IDEA-324** `wtfu:be17e9590168` (SPEC-0237): *A half-written user profile file parks every
  active campaign of that profile `open_failed`, with no auto-recovery.* The code check
  confirmed this. A profile `ValueError` in the `_tick` planning loop becomes a
  `StepResult(Step("open"), error=...)` without `config_error`. It therefore parks
  `open_failed` with `resume_to: QUEUED`, so an IMPLEMENTING campaign gets resumed into a
  re-open.
- **IDEA-355** `wtfu:60de6b7331a9` (SPEC-0259): *The re-gate safety net does nothing when a
  project has no required gates.* `_close_regates` honors a resolver's
  `dropped_commits` declaration whenever the merge is not docs-only. But
  `G.run_all(...).ok` is `all(...)` over an empty set when no gate is required, so a
  declared drop is never checked.

The other findings in these source ideas were dropped. Most were already fixed:
`busy.since` parsing, the drain budget for close flights, the `finish` race under
`item_lock`, the `prioritized` transition that marked adopted runs superseded, and the
profile `.yml`/directory errors. The rest were test-only.

## Goals / Non-goals

**Goals**
- A campaign never reuses a `run_id`.
- `slot_freed_at` marks when the runner went from full to having a free slot. It does not
  mark every drop in occupancy.
- A profile that fails to resolve parks the campaign as `config_invalid`, with `resume_to`
  set to the campaign's current state. The campaign recovers on its own once the profile
  loads again, the same way a broken project config recovers.
- A resolver's dropped-commit declaration is honored only when the close re-gates with at
  least one required gate.

**Non-goals**
- Debouncing profile edits, for example ignoring a file modified less than N seconds ago.
  Auto-recovery makes a brief park harmless.
- Capping the number of declared drops (IDEA-355 `be3de626c3ce`). With the re-gate
  required, the gates are the control.
- Reworking `counters` persistence in general.

## Decision

Make four local fixes in `queue.py`, each covered by a regression test:

- Mint the run number from the higher of the stored counter and the run numbers already on
  the ledger.
- Stamp `slot_freed_at` only on a full-to-free transition.
- Route profile resolution errors through the existing `config_invalid` park and its
  step-0 recovery.
- Make `_close_regates` also require a required gate in the project config.

## Design

1. **Unique run ids** (`_prepare_run`).
   - `run_no = max(counters["runs"], _ledger_run_max(cfg, cid)) + 1`.
   - `_ledger_run_max` reads `store.read_ledger(cfg, cid, types=["run.start"])` and returns
     the largest integer prefix of `run_id` (`"012-implementer"` gives 12), or 0.
   - This costs one ledger read per run start, which is negligible next to the run itself.

2. **Full-to-free stamp** (`_SlotWatch.check`).
   - Stamp `info["slot_freed_at"]` only when `self.last >= cap > now`, where
     `cap = slots_for(self.cfg)`. The watchdog (`events.waiting_not_admitted_items`) uses
     the same `slots_for(cfg)`.
   - `run()` still stamps `slot_freed_at` at start.
   - If `test_run_stamps_slot_freed_at_on_drop` relies on a drop that never started from
     full, change its fixture so the runner starts full, and note the change under
     As shipped.

3. **Profile errors park `config_invalid`** (the `_tick` planning loop, about line 2569, and
   step 0 recovery, about line 2503).
   - In the planning loop, catch the `ctx.profile(st)` failure on its own and build the
     `StepResult` with `config_error=True` and `error=f"profile: {e}"`. `_apply`'s existing
     `config_invalid` branch (about line 1561) then parks with
     `resume_to = res.from_state`.
   - Change that branch's detail text from the fixed `"project config: …"` to
     `res.error`, already prefixed `"project config: "` or `"profile: "` by the producer.
     The other `config_error` producers (`_do_open`, `_do_open_merge`, `_do_persona`,
     review fan-out) prefix `"project config: "` themselves.
   - In step 0, a campaign parked `config_invalid` recovers only when both `ctx.project(st)`
     and `ctx.profile(st)` load. A `ValueError` from either keeps it parked.

4. **Declared drops need a real re-gate** (`_close_regates`, `_declared_drops`, `_do_resolve`).
   - Change the signature to `_close_regates(state, pcfg)`. It returns False for a
     docs-only merge, as it does today, and also returns False when
     `not any(g.get("required", True) for g in pcfg.get("gates") or [])`.
   - `_declared_drops(state, verdict, pcfg)` passes `pcfg` through.
   - `_do_resolve` obtains `pcfg = ctx.project(state)` before verification. Its existing
     outer `except (GitError, ValueError, OSError)` already handles a failure there.
   - Without a required gate, an undeclared missing commit fails verification, as it did
     before SPEC-0259. This fails closed.

## Alternatives considered

- **Persist `counters.runs` before writing `run.start`.** This reorders a carefully locked
  sequence and still fails if that write fails. Reading the ledger is simpler and cannot
  drift.
- **Stamp only when the free-slot count goes from 0 to 1, per profile.** The watchdog reads
  one global `slots_for(cfg)`. Matching it exactly matters more than per-profile precision.

## Acceptance criteria

- [ ] When `write_state` fails once after `run.start`, the next run of the campaign gets a
      new `run_id`, one above the highest on the ledger, and its own run directory.
- [ ] With `slots_for(cfg) == 3`, occupancy moving 2 → 1 → 2 → 1 leaves `slot_freed_at`
      unchanged. Moving 3 → 2 stamps it.
- [ ] A tick that cannot resolve an active campaign's profile parks it with reason
      `config_invalid` and detail starting `profile:`, and sets `resume_to` to the
      campaign's state (IMPLEMENTING stays IMPLEMENTING).
- [ ] After the profile file is fixed, the next tick recovers that campaign (reason
      `config_recovered`) without a human resume.
- [ ] For a project with no required gate, `_declared_drops` returns `[]`, and a resolver
      that drops a campaign commit fails verification even if it declares the drop.
- [ ] `uv run pytest` is green and each sabotage check below fails its test.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_queue.py::test_run_id_not_reused_after_state_write_failure`
    - Monkeypatch `store.write_state` to raise `OSError` once, inside `_prepare_run`.
    - Tick twice with FakeRunner.
    - Assert that the two `run.start` events have different `run_id`s, the second numbered
      one higher, and that the two run directories are distinct.
  - `tests/test_agentic_loop_state.py::test_slot_freed_at_only_on_full_to_free`
    - Drive `_SlotWatch.check` with a stub `count()` sequence 3, 2, 1, 2, 1 and slots set
      to 3.
    - Assert that `update_info` gets `slot_freed_at` exactly once, on 3 → 2.
  - `tests/test_agentic_queue.py::test_broken_profile_parks_config_invalid_and_recovers`
    - Write a user profile `p.yaml` and put a campaign on profile `p` into IMPLEMENTING.
    - Overwrite `p.yaml` with invalid YAML and tick.
    - Assert `park.reason == "config_invalid"`, `park.resume_to == "IMPLEMENTING"` and
      that the detail starts with `profile:`.
    - Restore the file and tick.
    - Assert the transition reason is `config_recovered` and the state is IMPLEMENTING or
      WAITING.
  - `tests/test_agentic_resolver.py::test_declared_drop_ignored_without_required_gates`
    - With `pcfg["gates"]` set to `[]`, and again with every gate `required: false`, assert
      that `_declared_drops(state, verdict, pcfg) == []`.
    - Through `_do_resolve` with a resolver that drops a commit and declares it, assert
      `resolution.verified is False`.
- **Sabotage checks.** Each change must make its test fail. Record the result in the spec.
  - Revert `run_no` to `counters["runs"] + 1`.
  - Stamp on every drop again.
  - Drop `config_error=True` from the profile failure.
  - Remove the required-gate condition from `_close_regates`.
- **Regression guard:** run the full `uv run pytest`. These suites must stay green:
  - `test_agentic_resolver.py` (the SPEC-0259 declared-drop tests use a project with a
    required gate; add one to the fixture if they do not)
  - `test_agentic_watchdog_gaps.py`
  - `test_agentic_config_reload.py`
  - `test_agentic_profiles.py`
- **Manual verification:** none. All paths are driven through `tick` in tests.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log

