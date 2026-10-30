---
id: SPEC-0305
title: "Quiet follower holds the spec-phase events spec_written, spec_reviewed and spec_accepted"
status: proposed
owner: user
created: 2026-10-30
source_idea: IDEA-411
kind: feature
tags: [agentic, conductor, events, cost]
---

## Context

[SPEC-0300](./0300-quiet-conductor-follower-digest.md) made the conductor's event follower
hold events that never need a decision and fold them into one `conductor_digest` line. What
it holds is the table `RULES` in `src/wt/agentic/quiet.py`. `classify` looks a type up in
`RULES` and, by design, any type that is missing wakes the conductor ("Unknown event types
wake").

The idea-campaign spec phase ([SPEC-0217](./0217-standalone-spec-reviews-in-watch-spec-phase-events.md)) emits three
events that `RULES` does not list: `spec_written`, `spec_reviewed` (carries `verdict` and
`round`) and `spec_accepted` (`src/wt/agentic/events.py:72-74`, projected from the ledger in
`_project_ledger_event`). The `/wt-conduct` decision table already calls them informational
(`skills/wt-conduct/SKILL.md`, the row `spec_written`, `spec_reviewed`, `spec_accepted`:
"none: informational events of the idea-campaign spec phase"). So today the quiet follower
delivers each of them as its own line, and each line is one wakeup: one API turn that re-reads
the whole conductor context, which does nothing. An idea campaign emits at least three of
them (one `spec_written`, one `spec_reviewed` per review round, one `spec_accepted`), so every
idea campaign costs the conductor several empty turns before any code is written. This is the
cost that SPEC-0300 and [SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) exist
to remove (exit criterion E3, wakeups per merge).

The bug is a gap between two lists that were meant to agree: the skill says "informational,
no action", and the code that decides what wakes has no entry for the types.

A `spec_reviewed` with a non-accepting verdict is not lost when it is held. The campaign then
either re-enters `SPEC_WRITING` for another round, which emits a further `spec_written` that
is held the same way, or it parks (for example `spec_review_exhausted`). The `parked` event
wakes the conductor and has its own table row. The held events still reach `events.jsonl`,
`wt queue events` history, the TUI and the steward followers, and they appear in the next
`conductor_digest`.

## Goals / Non-goals

**Goals**
- The conductor's quiet follower holds `spec_written`, `spec_reviewed` and `spec_accepted`
  (rule `hold`), so they are counted in the digest's `held` map and in `wt queue wakeups`
  as held, not delivered as wakeups.
- The `/wt-conduct` skill's *Quiet stream* paragraph lists them under **Always held**, and the
  existing mirror test stays green.
- A guard so the gap cannot reopen unnoticed: every event type the decision table calls
  informational is either in `quiet.RULES` or on a named, justified exception list.

**Non-goals**
- Changing what `events.sync` appends or what any other reader (`--all`, TUI, steward) sees.
- Making a `spec_reviewed` verdict decide wake or hold. The table row says no action is ever
  needed for it; a campaign that fails its spec review surfaces through `parked`.
- Changing the spec-phase state machine, the event payloads or the `conductor_digest` shape.
- Adding a startup self-check beyond the test guard (see Alternatives considered).

## Decision

Add `spec_written`, `spec_reviewed` and `spec_accepted` to `quiet.RULES` with the value
`"hold"`, update the *Quiet stream* **Always held** list in `skills/wt-conduct/SKILL.md` to
match, and add a test that cross-checks the decision table's informational rows against
`RULES` so that a new informational event type cannot be left out again.

## Design

1. `src/wt/agentic/quiet.py`: extend `RULES` with a third group on the "hold" line, with a
   comment tying it to SPEC-0217, for example
   `"spec_written": "hold", "spec_reviewed": "hold", "spec_accepted": "hold"`. No change to
   `classify`, `Digest.add` or the digest renderer: `Digest.add` already folds any held type
   into `held[type]` and `campaigns[type]` generically.
2. `skills/wt-conduct/SKILL.md`, *Quiet stream* paragraph: append the three types to the
   **Always held** bullet, inside backticks and comma-separated, in the same format the
   existing test parses. Update the decision-table row for the three types so its second
   column says they "arrive only in `conductor_digest`", the wording the other held
   informational rows use, and keep the row's "none: informational" action text.
3. Guard test (`tests/test_agentic_quiet.py`): parse the decision table in
   `skills/wt-conduct/SKILL.md`; for every row whose action cell starts with `none` or
   `informational`, take the event types named in the first column, and assert each is in
   `quiet.RULES` (any rule) or in `INFORMATIONAL_EXEMPT`, a small constant in the test with a
   one-line reason for each member. Starting members are only what the audit finds is
   informational by the table but must wake or is synthetic (for example `conductor_digest`,
   which the follower itself produces); the implementer fills the list from the actual run and
   justifies each entry in a comment.
4. No migration: `RULES` is read on every poll. A digest file written before the change has
   no entries for the new types and is valid as is. A running conductor follower picks the
   change up when `wt queue events --follow` restarts (the code-change drain of
   [SPEC-0229](./0229-runner-exit-paths-and-draining-ticks-with-detached-runs.md) already re-execs the runner; the
   Monitor is re-armed by the conductor on `code_changed`).

## Alternatives considered

- **Hold `spec_reviewed` only when its verdict is accepting, wake otherwise.** Rejected: a
  failed review always leads to either another `spec_written` (informational) or a `parked`
  event (which wakes). Waking on the verdict would spend a turn that the `parked` line then
  repeats.
- **Invert the default: hold unknown types.** Rejected: SPEC-0300 deliberately wakes on unknown
  types so a new, unclassified failure signal is never silenced.
- **A runtime warning when the follower meets an unlisted type.** Rejected: it adds a
  warning on the hot path and only fires after the cost was paid. The unit test catches the
  gap before merge.
- **Move the three types to the `cond` group.** Rejected: nothing about them depends on facts
  at delivery time.

## Acceptance criteria

- [ ] `quiet.RULES["spec_written"]`, `RULES["spec_reviewed"]` and `RULES["spec_accepted"]`
      are each `"hold"`.
- [ ] `quiet.classify` returns `"hold"` for an event of each of the three types, whatever its
      `data` (including a `spec_reviewed` whose `verdict` is not accepting).
- [ ] With quiet mode on, `events.follow(cfg, "conductor", quiet=True)` over a log holding the
      three events and one `parked` event prints no line for the three events and a line for
      `parked`; the next `conductor_digest` has `held` counts of 1 for each of the three types.
- [ ] With `quiet=False` (`wt queue events --follow --all`) and for a `steward` reader, all
      three events are still printed, and `events.jsonl` holds them unchanged.
- [ ] `wt queue wakeups` counts the three events under `held`, not `delivered`, in `by_type`.
- [ ] `skills/wt-conduct/SKILL.md`'s **Always held** line names exactly the `hold` types of
      `quiet.RULES`, including the three new ones
      (`test_conduct_skill_quiet_rules_match_code` passes), and the decision-table row for
      the three types says they arrive only in `conductor_digest`.
- [ ] A test fails when a decision-table row marked informational (`none` / `informational`
      action) names an event type that is in neither `quiet.RULES` nor the documented exempt
      list, and passes on the repository as it stands after this change.
- [ ] The whole existing suite passes, with no change to the events other than the three
      types in `tests/test_agentic_quiet.py`'s existing expectations.

## Test plan

- **Automated tests** (all `uv run pytest`, in `tests/test_agentic_quiet.py` unless noted):
  - `test_spec_phase_events_are_held`: asserts the `RULES` entries and `classify` for
    all three types, with a `spec_reviewed` of verdict `accept` and one of verdict `revise`.
  - `test_follow_holds_spec_phase_events`: appends `spec_written`, `spec_reviewed`,
    `spec_accepted` and `parked` with `events.append` (payloads from `events.DATA`/schemas),
    runs `E.follow(..., quiet=True)` as the existing digest tests do (`_lines`, a clock
    advanced past `digest_interval_s`), and asserts only `parked` plus the digest are
    printed and `held` counts are 1 each. A second run with `quiet=False` and a
    `steward` reader prints all of them.
  - `test_wakeups_report_counts_spec_phase_as_held`: after the follow above,
    `QT.wakeups_report(cfg)["by_type"]["spec_written"] == {"wake": 0, "hold": 1}` and the
    same for the other two.
  - `test_decision_table_informational_rows_covered_by_rules`: the guard from Design item 3.
    It also gets a negative case: a made-up table row `| \`foo_event\` | \`foo_event\` | none: informational |`
    makes the helper report `foo_event` as uncovered.
  - The existing `test_conduct_skill_quiet_rules_match_code` in `tests/test_skills.py` is
    the check for the skill/`RULES` mirror; it must pass unchanged.
- **Manual verification:** in a scratch data dir, `wt queue events --follow --for conductor`
  against a log containing the three events and a `parked` event prints only the `parked`
  line (and a `conductor_digest` after the interval); `--all` prints all four. `wt queue
  wakeups` shows the three types under held.
- **Regression guard:** run the full `uv run pytest`; the existing digest, wakeup-report and
  skill-row tests (`tests/test_agentic_quiet.py`, `tests/test_skills.py`,
  `tests/test_agentic_dashboard_spec_reviews.py`) pass without edits to their expectations.
  `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Land the `RULES` entries, the skill edit and the tests in one change.
2. The runner re-execs on the code change; the conductor re-arms its Monitor on
   `code_changed`, which restarts the follower with the new table.
3. Verify with `wt queue wakeups` over the next idea campaign: the three types appear under
   held and wakeups per merge does not rise.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped (notably the exempt list in the guard test).
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
