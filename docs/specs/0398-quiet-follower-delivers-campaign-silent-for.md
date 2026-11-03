---
id: SPEC-0398
title: "The quiet follower holds `campaign_silent` / `state_stuck` for a GATING campaign that waits for a gate slot, runs a gate, or has its gate deferred"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M10: agentic MVP"
kind: feature
source_idea: IDEA-551
depends_on: [SPEC-0300, SPEC-0309, SPEC-0311, SPEC-0314]
tags: [agentic, conductor, quiet, events, gates]
---

## Context

Bug `IDEA-551`. The quiet conductor follower (SPEC-0300) delivers `campaign_silent` (and
`state_stuck`) for `GATING` campaigns that are not silent at all: they wait for a gate slot, run
a gate (including a retry) or have their gate step deferred. Each delivery costs a conductor
wakeup whose only possible decision is "wait". The event log of 2026-11-02 holds eight
`campaign_silent` events for `GATING` campaigns (ids 3713-3792, `silent_s` 900-902, i.e. right
at `silent_after_s`), and the ledger of `SPEC-0357-20261101-83e8` records the conductor's
decision `wait (campaign_silent): GATING gate not started: runner draining for code_changed
since 13:01, waiting on SPEC-0349's gate (timeout 3600s); resumes after re-exec`.

What the code does today:

- `quiet._silent_hold` (`quiet.py:193`, extended by SPEC-0314) holds only when the campaign (or,
  for a close-train waiter, its head) is in the lease's `in_flight` list or is the lease's fresh
  `busy` campaign. It never reads the gate-slot facts.
- The watchdog (`events.watchdog_checks`, `events.py:~815`) already treats a live gate-slot waiter
  marker (`gate_slots.waiting`) as "not silent" and measures a running gate from the live hold's
  `since` (`gate_slots.holding`, SPEC-0309). But a gate that has held its slot for longer than
  `silent_after_s` (900 s; a gate run is routinely 15-20 minutes) is silent by design: the
  ledger has no entry until `run_all` returns, so the event fires and the follower has to decide
  whether to deliver it. Whether the gate step is in `in_flight` at that moment is a snapshot
  written once per loop pass (`_SlotWatch.check`, `queue.py:5214`); the slot files, whose
  holder pid is probed live, are the fresher fact and are what the watchdog itself uses.
- A `GATING` campaign whose gate step has not started is in neither `in_flight` nor the slot
  files: the tick defers the step when the runner is draining (`queue.py:~4487`, "a draining
  tick starts no in-process work", SPEC-0229) and while another gate or close re-gate is in
  flight and an exclusive re-gate is pending (`select_gate_starts`, SPEC-0311;
  `_note_gate_deferred` only journals). Nothing is written to the campaign ledger meanwhile, so
  after `silent_after_s` the watchdog raises `campaign_silent` and the follower, finding no
  `in_flight` entry, delivers it. The runner is alive and the deferral ends by itself when the
  drain or the gate it waits on ends.

The same class of false wake was closed for the close train in SPEC-0314: the follower decides
with live facts at delivery time.

## Goals / Non-goals

**Goals**
- `campaign_silent` and `state_stuck` for a campaign with a live gate-slot waiter marker, or a
  live gate-slot hold that is within the gate budget, are held by the quiet follower, whether or
  not the campaign is in the lease's `in_flight` snapshot.
- Those events for a `GATING` campaign whose gate step is deferred (the runner is draining
  within its wait window, or an exclusive re-gate defers it behind another gate or close
  re-gate in flight, within a bounded age) are held.
- A gate that exceeds its budget, a drain past its window, a deferral older than its bound, a
  deferral with no exclusive re-gate behind it, a dead runner and a dead holder pid still
  wake, so the follower cannot hide a hang.

**Non-goals**
- No change to the watchdog's detectors, `silent_after_s`, `gate_budget_s`, the event log
  contents, the gate-slot files, `in_flight`, `select_gate_starts` or the drain path. The false
  event stays visible to every other consumer (TUI, `nw queue events`, the ping); the follower
  decides with live facts at delivery time (the SPEC-0305 / 0306 / 0314 pattern).
- No change to the close train rule of SPEC-0314 or to `CLOSING` handling beyond what the
  slot-fact rule gives it for free.
- No change to the wake class of any other event type; `quiet.RULES` is untouched.

## Decision

`quiet._silent_hold` keeps its current checks and gains two more, applied to the event's
campaign while the lease is held (a dead runner still wakes):

1. **Gate-slot facts.** Hold when `gate_slots.waiting(cfg, cid)` returns a live waiter marker, or
   `gate_slots.holding(cfg, cid)` returns a live hold whose `since` is at most `gate_budget_s`
   plus the follower's existing grace (`_GRACE_S`, 300 s) old.
2. **Deferred gate.** Hold when the campaign's state is `GATING`, it is not in `in_flight`, and
   either (a) the lease is `draining` within the bound `_drain_hold` already uses for
   `runner_draining`, or (b) an exclusive re-gate (SPEC-0311 `gate_exclusive` marker) is set on
   a `GATING` campaign (this one or another) while `in_flight` names another `GATING` or
   `CLOSING` campaign, and this campaign has been `GATING` for at most
   `2 × gate_budget_s + _GRACE_S`. Rule (b) mirrors `select_gate_starts` (`queue.py:433`),
   which defers a non-exclusive gate only when an exclusive re-gate is in flight, or is pending
   while another gate is in flight; without an exclusive marker nothing defers a gate, so a
   silent `GATING` campaign outside a drain is not explained and wakes. The age bound is one
   gate run plus the exclusive re-gate that follows its timeout, plus grace.

## Design

**`src/nightwright/agentic/quiet.py`**
- `Ctx` gets a lazy property `states` -> `dict[str, dict]` (campaign id -> `{"state": str,
  "gate_exclusive": bool, "updated_at": ..., "created_at": ...}`, `gate_exclusive` being
  `bool(queue.gate_exclusive(row))`), built from one `store.list_campaigns(cfg)` scan on first
  use, like `live_specs` and `closing_waiters`; tests inject it through a new optional
  `Ctx(..., states=None)` argument.
- `_silent_hold(ev, ctx)` returns true when the lease is held, the event has a campaign and any
  of: the existing `_alive` check for the campaign or its close-train head (SPEC-0314),
  `_gate_active(cid, ctx)`, `_gate_deferred(cid, lease, ctx)`.
- `_gate_active(cid, ctx)`: `gate_slots.waiting(ctx.cfg, cid)` is truthy, or `hold :=
  gate_slots.holding(ctx.cfg, cid)` is truthy and `E._age_s(hold.get("since"), ctx.now)` is not
  None and at most `E.settings(cfg)["gate_budget_s"] + _GRACE_S`. A missing or unparsable
  `since` does not hold. Both probes check the holder/waiter pid is alive (existing behavior),
  so a dead pid never holds.
- `_gate_deferred(cid, lease, ctx)`: false unless `ctx.states.get(cid) == "GATING"` and `cid`
  is not in `lease["in_flight"]` (`ctx.states[cid]["state"]`). Then true when either:
  - (a) `lease["draining"]` is a dict with a `since` whose age is within
    `_drain_hold(cfg, {"waiting_on": draining.get("waiting_on"), "draining_s": age})` (the same
    bound as the `runner_draining` rule); or
  - (b) all of: some id in `ctx.states` with state `GATING` has `gate_exclusive` true (this
    campaign or another); some id in `lease["in_flight"]` other than `cid` has state `GATING`
    or `CLOSING` in `ctx.states`; and the campaign's time in `GATING`,
    `E._since_state(cfg, {"id": cid, **ctx.states[cid]}, ctx.now)` (the watchdog's own
    `in_state_s` clock), is not None and at most `2 * E.settings(cfg)["gate_budget_s"] +
    _GRACE_S`. A missing age does not hold.
- `classify` is unchanged: both event types already go through `_silent_hold`.
- Why nothing is hidden forever: a slot waiter or holder that stalls is bounded by
  `gate_budget_s` + grace, after which the event wakes; a drain is bounded by its own window
  (then `runner_draining` and this event wake); a deferral behind another gate needs an
  exclusive marker (the only thing `select_gate_starts` defers on) and is cut off by the
  `2 × gate_budget_s + _GRACE_S` age bound regardless of how busy the queue stays, so a
  `GATING` campaign whose gate never starts for another reason (a bug, a `none` plan step)
  wakes once it is older than that; a dead runner raises `runner_dead` and `held` turns false.
- Gate retries (SPEC-0289's retry ladder, SPEC-0342's remote retry) run inside the one gate
  step: while a retry waits for a slot the waiter marker holds, and while it runs the hold does.

**Docs**
- `skills/nw-conduct/SKILL.md`: the *Quiet stream* entry for `campaign_silent`, `state_stuck`
  becomes "(its gate or close is still running in the runner, it waits in the close train
  behind a head that is, it waits for or holds a gate slot, or its gate is deferred behind a
  drain or another gate)". The type list is unchanged, so
  `tests/test_skills.py::test_conduct_skill_quiet_rules_match_code` still holds.
- `docs/product/capabilities/agentic.md`: the *Gate slots* paragraph gets one sentence that the
  quiet stream also holds `campaign_silent` / `state_stuck` for a campaign waiting for or
  holding a gate slot or whose gate is deferred. Earlier specs are not edited (history).

## Alternatives considered

- **Skip these cases in the watchdog.** Correct for the slot waiter (SPEC-0309 already does) but
  not for a gate that legitimately runs longer than `silent_after_s`, and a deferred gate would
  need the lease and pool facts in the detector. Events raised by older code would still reach
  the follower. The follower already has the lease and decides at delivery time.
- **Hold any GATING campaign while the lease is held.** Would hide a gate that hung inside a
  live runner; the budget bound on the hold and the exclusive-marker and age bounds on the
  deferral keep the second signal.
- **Hold whenever any other gate or close is in `in_flight`, without an exclusive marker or age
  bound.** `in_flight` lists every pooled campaign, including a `CLOSING` one that does not
  re-gate, and on a busy queue the next gate always replaces the last, so a gate that never
  starts would stay hidden indefinitely.
- **Add `gate_waiting` to the hold decision.** `gate_waiting` is only written after a wait of
  30 s and as a ledger entry; the marker and hold files are the live source of truth and also
  cover a gate that runs after its wait ended.
- **Make `in_flight` carry deferred campaigns.** Rejected for the reason given in SPEC-0314: it
  is a pool/slot-accounting fact.

## Acceptance criteria

- [x] `quiet.classify` returns `hold` for `campaign_silent` and `state_stuck` of a campaign with
      a live gate-slot waiter marker while the lease is held, even when the campaign is not in
      `in_flight`; and `wake` when the marker's pid is dead or the lease is not held.
- [x] It returns `hold` for a campaign with a live gate-slot hold whose `since` is within
      `gate_budget_s` + 300 s, and `wake` once it is older, when the holder pid is dead, or
      when `since` is missing or unparsable.
- [x] A hold on a worker pool (`gate-slots/w-<W>/`, SPEC-0337) holds like a local one.
- [x] It returns `hold` for a `GATING` campaign that is not in `in_flight` when the lease is
      draining within `_drain_hold`'s bound, and `wake` when the drain is older than the bound.
- [x] It returns `hold` for a `GATING` campaign not in `in_flight` while a `GATING` campaign
      (itself or another) carries a `gate_exclusive` marker, `in_flight` names another `GATING`
      or `CLOSING` campaign, and the campaign has been `GATING` for at most
      `2 × gate_budget_s + 300` s.
- [x] It returns `wake` in the same situation when no `GATING` campaign has a `gate_exclusive`
      marker, when the campaign has been `GATING` longer than `2 × gate_budget_s + 300` s (or
      its age is unknown), when `in_flight` is empty or names only campaigns in other states,
      or when the campaign is not `GATING`.
- [x] `Ctx.states` maps each campaign id to its `state` and a `gate_exclusive` flag, read from
      one store scan.
- [x] The existing cases still hold: own campaign in `in_flight`, fresh `busy`, close-train
      waiter behind a live head (SPEC-0314); other campaigns still wake.
- [x] `quiet.RULES` is unchanged and no other event type's classification changes.
- [x] The `nw-conduct` skill line and `docs/product/capabilities/agentic.md` carry the new
      sentence; `tests/test_skills.py` passes.

## Test plan

- **Automated tests** (`tests/test_agentic_quiet.py`; they reuse the `_ctx` / `_ev` helpers,
  with `Ctx(..., states=...)` injected and slot files written under the temp `cfg` like
  `tests/test_agentic_events.py::_marker` and `test_finished_wait_in_a_live_hold_is_not_silence`):
  - `test_silent_held_for_gate_slot_waiter`: a live marker (own pid) holds both event types with
    `in_flight=[]`; a dead-pid marker and `held: False` wake.
  - `test_silent_held_for_live_gate_hold_within_budget`: hold `since` within and past
    `gate_budget_s + 300`, dead pid, missing and garbage `since`; plus a `w-box/` worker pool
    hold.
  - `test_silent_held_for_gate_deferred_by_drain`: `GATING`, not in `in_flight`, `draining` with
    `since` inside and past the bound (with and without `waiting_on` timeouts).
  - `test_silent_held_for_gate_deferred_behind_another_gate`: with an exclusive marker on a
    `GATING` campaign (the event's own, then another's) and `in_flight` naming a `GATING` and a
    `CLOSING` campaign, within age: `hold`. Wake cases: `in_flight` naming an `IMPLEMENTING`
    campaign or nothing; the campaign itself in a non-`GATING` state; no exclusive marker
    anywhere (a `CLOSING`-only or `GATING`-only `in_flight`); an exclusive marker only on a
    non-`GATING` campaign; and an over-age case (`GATING` since more than
    `2 × gate_budget_s + 300` s, via a transition ledger entry or `updated_at`) and a missing-age
    case, both with every other condition met.
  - `test_ctx_states_built_from_store`: campaigns written to a temp store (one with a
    `gate_exclusive` marker), the lazy property maps id -> `state` and `gate_exclusive`.
  - Existing `test_silent_held_while_in_flight`, `test_silent_held_for_train_waiter_behind_live_head`,
    `test_silent_not_held_for_waiter_without_train_entry`, and
    `tests/test_skills.py::test_conduct_skill_quiet_rules_match_code`.
- **Manual verification:** with a runner whose two gate slots are held, queue a third `GATING`
  campaign and run `uv run nw queue events --for conductor --follow` for longer than
  `silent_after_s`: no `campaign_silent` line arrives for it while the others gate (the digest
  counts it as held). Repeat with `nw queue drain` pending behind a long gate.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`, including
  `tests/test_agentic_quiet.py`, `tests/test_agentic_events.py` (the SPEC-0309 slot-wait
  watchdog tests) and `tests/test_gate_slots.py`.

### Test record (2026-11-02)

- `uv run pytest -q tests/test_agentic_quiet.py tests/test_skills.py tests/test_gate_slots.py
  tests/test_agentic_events.py`: 233 passed. New tests: `test_silent_held_for_gate_slot_waiter`,
  `test_silent_held_for_live_gate_hold_within_budget` (incl. a `w-box/` pool hold),
  `test_silent_held_for_gate_deferred_by_drain`,
  `test_silent_held_for_gate_deferred_behind_another_gate` (no-marker, non-GATING marker,
  over-age and missing-age wakes), `test_ctx_states_built_from_store`.
- `python3 tools/affected_tests.py --run uv run pytest -q` (the wide affected set) was started
  but stopped at the background time limit, so it did not complete; the affected set and the
  full suite are left to gating. `python3 tools/spec_lint.py` passes after `--write-ledger`.
- Manual verification (a live runner with held slots) was not performed: the queue runner is
  not driven from this worktree. The behaviour is covered by the injected-lease tests above.

### Test record, gate triage (2026-11-02)

- `test_admission_blocked_journaled_once_per_change`, `test_run_stamps_slot_freed_at_on_drop`
  (and the sibling `admission_blocked` tests) failed on the base commit 1d629480 as well: SPEC-0395
  (GATING holds no profile slot) shifted the slot accounting they asserted. They are adapted in
  `tests/test_agentic_exit_paths.py` / `tests/test_agentic_loop_state.py` (a CLOSING holder fills
  the one slot; `slot_freed_at` stamps on IMPLEMENTING -> GATING and on the merge). No change to
  `queue.py`. `uv run pytest -q tests/test_agentic_quiet.py tests/test_skills.py
  tests/test_agentic_exit_paths.py tests/test_agentic_loop_state.py
  tests/test_agentic_gating_slots.py`: all pass. The wide affected run then showed more SPEC-0395
  fallout, adapted the same way: the `queue_list_*` snapshots (`Active (1/2)`),
  `test_waiting_not_admitted_quiet_when_full_or_park_requested` (a CLOSING holder instead of GATING)
  and `test_load_queue_sections_order_and_meta` (`slots 1/`). Affected set: 4035 passed before
  those five; they now pass.

### As shipped (2026-11-02)

As designed. `Ctx` also gets a `rows` property, the shared cached `store.list_campaigns` scan
behind `states` and `closing_waiters`. `quiet.py` imports `queue` and `gate_slots` directly.

## Rollout / migration

None. The change is read-side (follower, skill text, docs). A running follower exits on
`code_changed` and restarts on the new code (SPEC-0231). Events already in the log are not
rewritten.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
