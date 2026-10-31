---
id: SPEC-0315
title: "Quiet follower holds a parked event the conductor itself caused (by=conductor)"
status: proposed
owner: user
created: 2026-10-31
source_idea: IDEA-436
kind: feature
tags: [agentic, conductor, events, cost]
---

## Context

[SPEC-0300](./0300-quiet-conductor-follower-digest.md) made the conductor's event follower
hold events that never need a decision. [SPEC-0302](./0302-routine-parks-decided-by-the-queue.md)
and the later quiet specs refined the `parked` rule: in `src/wt/agentic/quiet.py` `parked` is a
`cond` type and `classify` holds it only when its data carries `auto` (a park the queue decided
itself, with an automatic resume scheduled). Every other `parked` event wakes the conductor.

A `parked` event is projected from the campaign ledger in `_project_ledger_event`
(`src/wt/agentic/events.py`, the `t == "park"` branch) and appended with
`by=ev.get("by") or "tick"`, so the event's `by` field is the actor that parked the campaign.
The conductor parks campaigns itself: `wt queue park <id> --by conductor` (`_actor` in
`src/wt/agentic/cli_queue.py` resolves `--by`, else `$WT_MAILBOX_ADDR`, else `human`, and
`conductor` is a valid address) and `wt queue escalate`, which parks the campaign
`human_decision` with the requesting actor as `by`
(`src/wt/agentic/queue.py`, the escalation park). In both cases the event carries
`by: "conductor"`.

The quiet follower then delivers that event back to the conductor as a wakeup: one API turn that
re-reads the whole conductor context, to tell it about a park it made seconds ago and already
knows the reason for. This is an echo. It costs a turn per conductor-made park and does not feed
any decision (the conductor's act of parking was the decision), which is exactly the cost
[SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) exists to remove (wakeups per
merge, conductor cost as an MVP metric).

The echo is also safe to remove. Two backstops already cover a conductor-made park that is
left without a follow-up: `park_undecided` (SPEC-0222, "you left a park without a decision note
for 5 min") wakes the conductor when a conductor-owned park has no decision note after
`park_decide_s`, and the periodic `conductor_ping` fingerprint lists conductor-owned parks with
`decided: false`. Neither depends on the `parked` event being delivered.

## Goals / Non-goals

**Goals**
- A `parked` event whose `by` is `conductor` is held by the conductor's quiet follower, so it is
  counted in the digest's `held` map and in `wt queue wakeups` as held, not delivered.
- A `parked` event from any other actor (`tick`, `queue`, `human`, `steward:<P>`, `watchdog`,
  absent) still wakes the conductor exactly as before, and the SPEC-0302 `auto` hold is
  unchanged.
- The `/wt-conduct` skill's *Quiet stream* paragraph and `parked` guidance say so, and the
  existing skill/`RULES` mirror test stays green.

**Non-goals**
- Changing what `events.sync` appends or what any other reader (`--all`, TUI, steward
  followers) sees: the held event stays in `events.jsonl` and in `wt queue events` history.
- Changing `park_undecided`, the `conductor_ping` parked fingerprint, `park_owner` or
  `park_view`: those are the backstop that makes the hold safe.
- Holding parks made by other actors (a human parking from the CLI, a steward) or holding
  `resumed`/`escalation` variants beyond what `RULES` already holds.
- Distinguishing a conductor park from a conductor sub-agent's park: both act as
  `conductor` through the same `--by`/`$WT_MAILBOX_ADDR` path and are equally known to it.

## Decision

In `quiet.classify`, the `parked` branch returns `"hold"` when `ev["by"] == "conductor"`, in
addition to the existing `data.auto` hold; every other `parked` event wakes. The type stays
`cond` in `RULES`. The skill documents the second hold condition.

## Design

1. `src/wt/agentic/quiet.py`, `classify`, `parked` branch: replace
   `return "hold" if data.get("auto") else "wake"` with a hold when `data.get("auto")` or
   `ev.get("by") == "conductor"`, and extend the comment to cite SPEC-0315 ("a park the
   conductor itself made: it already knows; `park_undecided` and the ping fingerprint catch a
   park it leaves undecided"). Compare against the literal `"conductor"`, the canonical address
   `mailbox.validate_address` returns, with no case folding or prefix matching, so
   `steward:conductor`-like or `Conductor` values do not match. A missing `by` (older events,
   synthetic test events) does not match and wakes.
2. No change to `RULES`, `Digest.add`, the digest renderer or the digest schema: a held
   `parked` folds into `held["parked"]` and `campaigns["parked"]` through the generic path.
   `wt queue wakeups` already counts any held type.
3. `skills/wt-conduct/SKILL.md`, *Quiet stream* paragraph, the *Held while a fact says so*
   bullet: extend the `parked` item to read "a park the queue decided itself, with auto set in
   its data: SPEC-0302, or a park you made yourself, with `by` set to `conductor`: SPEC-0315;
   every other parked wakes you". The type names in the bullet do not change, so
   `test_conduct_skill_quiet_rules_match_code` is unaffected. Add one sentence to the
   `park_undecided` row: a park you made yourself arrives only through this event if you leave
   it without a decision note.
4. No migration. `classify` reads `by` on every poll. A running conductor follower picks up the
   change when `wt queue events --follow` restarts (the `code_changed` re-arm of
   [SPEC-0229](./0229-runner-exit-paths-and-draining-ticks-with-detached-runs.md) /
   [SPEC-0231](./0231-followers-exit-on-code-change-sessions-re-arm.md)). A digest file written before the
   change is valid as is.

## Alternatives considered

- **Hold every `parked` whose `by` is not an automatic actor, i.e. compare against a list of
  "known" actors.** Rejected: only the conductor's own actions are an echo; a human or steward
  park is news to the conductor and must wake it.
- **Hold only `parked.human_decision` made by the conductor (the escalation park).**
  Rejected: the echo is the same for `wt queue park --by conductor`, and the escalation
  already has its own held `escalation` event.
- **Suppress the event at projection time (do not append a `parked` event when `by` is
  `conductor`).** Rejected: the event is history for the TUI, `--all`, steward followers and
  `wt queue wakeups`; only the conductor's delivery should change, as in SPEC-0300.
- **Drop the echo by making the conductor ignore it in the skill ("do nothing on a parked you
  caused").** Rejected: the wakeup, the turn that re-reads the context, is the cost; the skill
  cannot refuse to be woken.

## Acceptance criteria

- [ ] `quiet.classify` returns `"hold"` for a `parked` event with `by: "conductor"`, for any
      `reason` (including `human`, `human_decision` and a reason such as `loop_exhausted`).
- [ ] `quiet.classify` still returns `"hold"` for a `parked` event with `data.auto` set
      (SPEC-0302), whatever its `by`.
- [ ] `quiet.classify` returns `"wake"` for a `parked` event with `by` equal to `tick`, `queue`,
      `human`, `watchdog`, `steward:Meta-Tools`, `Conductor`, an empty string, or with no `by`
      key, when `data.auto` is unset.
- [ ] With quiet mode on, `events.follow(cfg, "conductor", quiet=True)` over a log holding a
      `parked` event with `by=conductor` and another `parked` event with `by=tick` prints no
      line for the first and a line for the second; the next `conductor_digest` has
      `held["parked"] == 1` and lists the first campaign under `campaigns["parked"]`.
- [ ] With `quiet=False` (`--all`) and for a `steward` reader, both `parked` events are still
      printed, and `events.jsonl` holds both unchanged.
- [ ] `wt queue wakeups` counts the conductor-made `parked` under `held` and the other under
      `delivered` in `by_type["parked"]`.
- [ ] A campaign parked by the conductor with `wt queue park --by conductor` and left without a
      decision note still produces a `park_undecided` event after `park_decide_s` (the backstop
      is unchanged), and that event wakes the conductor.
- [ ] `skills/wt-conduct/SKILL.md`'s *Quiet stream* paragraph names the `by` condition for
      `parked`, and `test_conduct_skill_quiet_rules_match_code` passes.
- [ ] The existing suite passes, with the `parked` expectations in `tests/test_agentic_quiet.py`
      (`test_parked_auto_is_held` and the `WAKE` list use) unchanged for events without
      `by=conductor`.

## Test plan

- **Automated tests** (all `uv run pytest`, in `tests/test_agentic_quiet.py` unless noted):
  - `test_parked_by_conductor_is_held`: with the module's `_cfg`/`_ctx`/`_ev` helpers (extend
    `_ev` with an optional `by` keyword, defaulting to absent so existing callers are
    unchanged), assert `classify` is `"hold"` for `by="conductor"` across several reasons, with
    and without `auto`; `"wake"` for `by` in `tick`, `queue`, `human`, `watchdog`,
    `steward:Meta-Tools`, `Conductor`, `""` and absent; and that `RULES["parked"]` is still
    `"cond"`.
  - `test_follow_holds_conductor_parks_only`: appends two `parked` events with
    `events.append(..., by="conductor")` and `by="tick"`, runs `E.follow(..., quiet=True)` as
    the existing digest tests do (`_lines`, a clock advanced past `digest_interval_s`), and
    asserts only the `tick` park plus the digest are printed, with `held["parked"] == 1`. A
    second run with `quiet=False` and a `steward` reader prints both.
  - `test_wakeups_report_counts_conductor_park_as_held`: after the follow above,
    `QT.wakeups_report(cfg)["by_type"]["parked"] == {"wake": 1, "hold": 1}`.
  - `test_conductor_park_left_undecided_still_wakes` (in the file that tests
    `park_undecided`, `tests/test_agentic_events.py` or its equivalent): a campaign parked by
    the conductor with no note, past `park_decide_s`, yields `park_undecided` from the
    watchdog sweep, and `classify` returns `"wake"` for it.
  - The existing `test_conduct_skill_quiet_rules_match_code` in `tests/test_skills.py` must
    pass unchanged, and `test_decision_table_informational_rows_covered_by_rules` stays green.
- **Manual verification:** in a scratch data dir with a queued campaign, run
  `wt queue events --follow --for conductor` in one shell and `wt queue park <id> --by conductor
  --reason human --yes` in another. Expected: no `parked` line is printed to the follower; the
  event appears in `wt queue events --campaign <id>` and in the next `conductor_digest` under
  `held`. Park a second campaign without `--by conductor` (default `human`): its line is
  printed.
- **Regression guard:** the full `uv run pytest` run, plus the unchanged SPEC-0302
  `test_parked_auto_is_held` and the other `WAKE`-list tests in `tests/test_agentic_quiet.py`.

## Rollout / migration

1. Land the `classify` change, the skill edit and the tests together.
2. No data migration. The running conductor follower picks the change up when its Monitor is
   re-armed after `code_changed`.
3. Verify with `wt queue wakeups --since <day>` after a day of conducting: `by_type["parked"]`
   shows `hold` counts for conductor-made parks and `wake` counts only for parks the conductor
   did not make.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. A conductor sub-agent that parks a campaign acts as `conductor` and is held the same
  way (see Non-goals); `park_undecided` covers the case where nothing follows.
