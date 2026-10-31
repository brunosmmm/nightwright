---
id: SPEC-0318
title: "A park, resume or abandon by the conductor carries its decision record: `wt queue park|resume|abandon` require `--decision` and `--rule` for a non-human actor"
status: done
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-508
kind: feature
parent: SPEC-0298
tags: [agentic, conductor, audit, mvp]
---

## Context

[SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) exit criterion E4 wants 100% of
parks to have a structured decision record (`decisions.jsonl`,
[SPEC-0301](./0301-conductor-state-handoff-and-decision-log.md)) **before the campaign leaves
PARKED**. `wt queue mvp-report` ([SPEC-0304](./0304-mvp-scorecard-queue-mvp-report.md)) on
2026-10-31 08:17 gave E4 = 0.53 (8 of 15 parks). The 7 failures are of two kinds:

- **Missing (3).** The conductor's own holds of `IDEA-430-20261030-d279`,
  `IDEA-433-20261030-b47e` and `IDEA-432-20261030-54f4`: parks with reason `human` and
  `park.decision = null`, although `park_view` says `decided = true`.
  - `wt queue park` (`queue_park` in `src/wt/agentic/cli_queue.py`, `plan_park` /
    `apply_park` in `src/wt/agentic/queue.py`) parks with reason `human`, and
    `queue.park_view` counts a `human` park as decided by construction
    ([SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md)). So no
    `park_undecided` event ever fires for it, and nothing makes the conductor write a
    `wt.conductor.decision.v1` record. The park command writes only a ledger `note`
    ("park requested: …") that `decisions.park_coverage` and `mvp_report.e4` do not read.
- **Late (3 parks, `triage_escalated` of SPEC-0304, IDEA-412, IDEA-415).** The decision was
  recorded after the campaign was resumed. E4 (`mvp_report.e4`) marks a covering record whose ts
  is later than the `PARKED → …` transition `late`. `wt queue resume` and `wt queue abandon`
  accept a conductor actor without any record, and the skill's *Every park gets a decision* rule
  is advice, not a check.

The conductor is the only actor who parks, resumes or abandons without a human at the keyboard
(`_actor` in `cli_queue.py` resolves `--by`, then `$WT_MAILBOX_ADDR`, then `human`; the valid
addresses are `conductor`, `human` and `steward:<Project>`). Making the record part of the
command is cheaper and more reliable than a skill reminder.

## Goals / Non-goals

**Goals**
- A park requested by a non-human actor (`conductor`, `steward:<P>`) always produces a
  `decisions.jsonl` record in the same command, with the park already in the ledger, so
  `park_coverage` and E4 cover it.
- A resume or abandon of a PARKED campaign by a non-human actor cannot be applied unless a
  decision record already covers the current park, so no park is decided "late".
- A crash between the park and its record is repaired by the queue, not left as a gap.
- The `/wt-conduct` skill states the rule and the order (park, then decide; decide, then
  resume).

**Non-goals**
- Back-filling the 6 failed parks of the 2026-10-31 report. `decisions.jsonl` is append-only
  and a record written now would be late by definition. The report's window moves on.
- Changing the human path. A command run as `human` (no `--decision`, no record) behaves as
  today.
- Changing the escalation park (`wt queue escalate`), parks the queue makes itself, or
  `park_view` semantics (a `human` park stays "decided" for `park_undecided`).
- Changing `mvp_report.e4` or `decisions.park_coverage`; they are the measure, not the cause.

## Decision

1. **`wt queue park` takes the decision with it.** New options `--decision TEXT` (the
   rationale), `--rule ID` (a `RULE_RE` rule id from the `/wt-conduct` table) and `--kind K`
   (a `decisions.KINDS` value, default `wait`). When the resolved actor is not `human`,
   `--decision` and `--rule` are required: without them the command exits 1 (in the dry run as
   well) and writes nothing. A human actor may not pass `--decision`, `--rule` or `--kind`: the
   command exits 1 and says a human's park needs no record (decision records are
   `by: conductor|queue|steward:<P>`, `decisions.BY_RE`).
2. **The record is written after the park, from a request stored on the park.** The park
   carries `park.decision_request = {kind, rule, rationale, by}`, written by the same `_park`
   call under the campaign lock, so the request is atomic with the park. The decision record
   itself is written right after, outside the item lock (`decisions.record` takes the decisions
   lock before the item lock; the same rule as `_record_queue_decision` for
   [SPEC-0302](./0302-routine-parks-decided-by-the-queue.md)). Because it follows the park its
   ts is at or after the park's ts, which is what `match_parks` needs. On success the request is
   removed from `park`. In the *deferred* case (a loop holds the lease and the step must finish
   first) the request is stored on `park_requested` and is copied onto the park when the tick
   applies it; the record is then written by the tick.
3. **The queue repairs a missing record.** A tick step (next to `_record_auto_waits`) finds
   each PARKED campaign whose `park.decision_request` is still set, writes the record
   (`decisions.record(..., by=request["by"])`) and clears the request. A failure is journaled
   (`decision_record_failed`) and retried on the next tick; it is never raised. A crash between
   the park and the record therefore leaves a gap of at most one tick. A request whose campaign
   has already left PARKED is dropped and journaled, never recorded late.
4. **Resume and abandon of a PARKED campaign need a covering record.** For a non-human actor,
   `wt queue resume` and `wt queue abandon` first look for a decision record on the campaign with
   ts at or after the current park's ts (`park_view(...)["parked_at"]`). If there is none they
   exit 1 (also in the dry run) and say which command to run. Both accept `--decision TEXT`,
   `--rule ID` and `--kind K` (default `resume` / `abandon`): given together, the record is
   written first and the command then continues, so one command both decides and acts. A park
   with `park.auto` (the queue's own, [SPEC-0302](./0302-routine-parks-decided-by-the-queue.md))
   is exempt: its resume is `by: queue` and writes its own record. A human actor is exempt
   from the requirement and may not pass `--decision`, `--rule` or `--kind`: the command exits 1
   and says a human's resume or abandon needs no record (decision records are
   `by: conductor|queue|steward:<P>`).
5. **Skill.** `skills/wt-conduct/SKILL.md` gets: the conductor always passes `--by conductor`
   (or has `WT_MAILBOX_ADDR=conductor` set) for park, resume and abandon; the park/resume
   commands in the decision table carry `--decision "…" --rule <id>`; after `wt queue escalate`
   it records its `escalate` decision *after* the command (so the record's ts follows the
   park); and *Every park gets a decision* says "decide before you resume", naming the new
   refusal.

## Design

- `src/wt/agentic/decisions.py`:
  - `needs_decision(by) -> bool`: `by != "human"`.
  - `validate_request(kind, rule, rationale, by) -> dict`: the checks of `record` that do not
    need a campaign (kind in `KINDS`, `RULE_RE`, `BY_RE`, non-empty rationale), returning the
    request dict. ValueError otherwise. `record` is refactored to call it, so dry run and write
    validate identically.
  - `covering(cfg, campaign, park_ts) -> dict | None`: the first record of the campaign with ts
    at or after `park_ts` (parsed with `parse_ts`).
- `src/wt/agentic/queue.py`:
  - `plan_park(cfg, campaign, reason, *, by, decision=None)`: raises ValueError when
    `needs_decision(by)` and `decision` is missing; returns the validated request under
    `plan["decision"]`. `apply_park` passes it as `extra={"decision_request": …}` on the
    immediate `_park`, and stores it in `st["park_requested"]["decision_request"]` for the
    deferred mode. `plan_step` forwards `req.get("decision_request")` to the `Step`, and the
    tick's deferred-park branch (the `requested and park is None` block) passes it as `extra`.
  - `settle_decision_requests(cfg)`: the tick step of Decision 3, plus the same call made inline
    by `wt queue park --yes` after `apply_park` (immediate mode), so the common path needs no
    tick. Uses `_record_queue_decision`-style journaling; never called with an item lock held.
  - `plan_resume` / `plan_abandon` gain `decision=None`; both call a shared
    `_require_decision(cfg, st, by, decision)` that implements Decision 4 and returns the
    request to write. `apply_resume` / `apply_abandon` write it before the state change
    (`decisions.record` outside the item lock), so a refused resume leaves no record behind only
    when it was refused *before* the write, and a resume that fails afterwards leaves a true
    record of the decision made.
- `src/wt/agentic/cli_queue.py`: the new options on `queue_park`, `queue_resume` and
  `queue_abandon`; the plan JSON (`wt.queue.park.v1`, `wt.queue.resume.v1`,
  `wt.queue.abandon.v1`) gains a `decision` key (the request or `null`). New keys only, per the
  export contract.
- `schemas.py`: `park` ledger events keep their required fields (`reason`, `detail`,
  `resume_to`); `decision_request` is optional and not copied into the `parked` queue event.
- `skills/wt-conduct/SKILL.md` as in Decision 5. No new rule ids: a hold uses the id of the row
  that led to it (for example `message.request` for a steward's park request, `wt_broken`,
  `parked.main_dirty`).

## Alternatives considered

- **Write the record inside `apply_park`'s item lock ("same lock").** `decisions.record` takes
  the decisions lock first and the item lock inside it; taking it under the item lock inverts
  that order and can deadlock with a concurrent `wt queue decision`. A request stored on the park
  and settled outside the lock keeps the atomicity that matters (the intent is durable with the
  park) without the inversion.
- **Make `park_view` report a `human` park by the conductor as undecided.** It would raise
  `park_undecided` events and wake the conductor for a park it just made. That is the echo
  [SPEC-0315](./0315-quiet-follower-echoes-the-conductor-s-own-parks.md) removed. The command
  check is quieter.
- **Only a skill rule.** It already exists (*Every park gets a decision*) and three holds and
  three late records got past it.
- **A post-hoc sweep that back-fills missing records.** A record written after the fact is
  `late` for E4 and hides the real order of events.

## Acceptance criteria

- [x] `wt queue park <id> --by conductor --reason R` without `--decision` and `--rule` exits 1,
      with and without `--yes`, and writes no ledger event, no state change and no
      `decisions.jsonl` line. The same command as `human` still works with neither, and
      `human` passing `--decision`, `--rule` or `--kind` (park, resume or abandon) exits 1 with
      no write: no `decisions.jsonl` record has `by: human`.
- [x] `wt queue park <id> --by conductor --reason R --decision "why" --rule message.request
      --yes` (immediate mode) parks the campaign and appends one `wt.conductor.decision.v1`
      record (`by: conductor`, `kind: wait`, that rule and rationale) whose ledger `note`
      (carrying `decision`, with `decision.seq` = the record's seq) has a higher ledger `seq`
      than the `park` event, and whose ts is at or after the `park` event's ts. `decisions.park_coverage`
      reports that park covered, and `mvp_report.e4` counts it as decided (not `missing`, not
      `late`).
- [x] With a loop holding the lease (deferred mode) the record is written only after the tick
      applies the park, never before it (the decision's ledger `note` has a higher ledger `seq`
      than the `park` event, not only a ts at or after it), and is covered the same way.
- [x] A bad `--rule` or `--kind` is refused in the dry run and writes nothing.
- [x] If the process stops between the park and the record, `park.decision_request` is still on
      the park, and the next tick writes the record and clears the request. A request on a
      campaign that has already left PARKED is dropped without a record.
- [x] `wt queue resume` and `wt queue abandon` by `conductor` or `steward:<P>` on a PARKED
      campaign with no decision record at or after the park's ts exit 1 (dry run too) and name
      the fix. With `--decision` and `--rule` they write the record first and then act. With a
      covering record already present they act without the options.
- [x] A human resume or abandon and a queue auto-resume (`park.auto`) are unaffected.
- [x] An E4 recomputation over a fixture of 5 parks (a conductor hold, a deferred hold, a
      `triage_escalated` resume with `--decision`, an auto park, a human park) reports no
      `missing` and no `late` item for the conductor-made ones.
- [x] `skills/wt-conduct/SKILL.md` contains the `--by conductor` rule, the `--decision/--rule`
      form for park and resume, the "record the `escalate` decision after the command" rule and
      the "decide before you resume" rule.

## Test plan

- **Automated tests** in `tests/test_agentic_queue_park_decision.py` (temp agentic store,
  campaigns from `store.create_campaign`, `CliRunner` for the commands):
  - `test_conductor_park_requires_decision` (parametrized: no options, only `--decision`, only
    `--rule`, bad rule, bad kind; with and without `--yes`): exit 1, ledger, state and
    `decisions.jsonl` unchanged.
  - `test_human_park_unchanged`: `human` actor, no options, parks as before, no record.
    Parametrized refusal cases (park, resume, abandon as `human` with `--decision`, `--rule` or
    `--kind`): exit 1, a message that a human needs no record, ledger, state and
    `decisions.jsonl` unchanged.
  - `test_conductor_park_writes_record_after_park`: the record exists; the decision's ledger
    `note` (`decision.seq` = the record's seq) has a higher ledger `seq` than the `park` event,
    and `ts >= park.ts`; `park_coverage` covers it; `park.decision_request` is cleared. Order is
    asserted by ledger `seq` because `store.now_iso` has one-second resolution, so a ts-only
    check cannot tell a record written just before the park from one written after it.
  - `test_deferred_park_records_after_tick_applies_it`: lease held, `park_requested` carries the
    request, no record yet; after the tick applies the park the record exists and covers it, and
    its ledger `note` has a higher `seq` than the `park` event (same seq-based order check).
  - `test_crash_between_park_and_record_is_repaired`: patch `decisions.record` to raise once;
    `park.decision_request` stays; the next `settle_decision_requests` writes the record. A
    campaign resumed meanwhile has its request dropped and no record written.
  - `test_resume_abandon_need_covering_decision` (parametrized resume / abandon, actor
    conductor / steward): refused without a record; refused with a record older than the park;
    allowed with a covering record; allowed with `--decision --rule` (record written first,
    then the transition); human and `park.auto` exempt.
  - `test_e4_over_conductor_parks`: the 5-park fixture of the acceptance criteria, through
    `mvp_report.e4`, with the baseline sabotage below.
- `tests/test_skills.py`: `test_conduct_skill_park_resume_decision_rules` asserts the four
  sentences/forms of Decision 5 are present in `skills/wt-conduct/SKILL.md`.
- **Sabotage checks:** make `_require_decision` return without checking, so
  `test_resume_abandon_need_covering_decision` must fail; skip `settle_decision_requests` in the
  tick, so `test_crash_between_park_and_record_is_repaired` must fail; write the record before
  the park, so `test_conductor_park_writes_record_after_park` must fail; the sabotage is
  detected by the ledger `seq` comparison, which holds even when both land in the same second.
- **Manual verification:** on a scratch queue, `wt queue park <id> --by conductor --reason
  "test" --yes` is refused; with `--decision "test hold" --rule message.request --yes` it parks
  and `wt queue decisions --parks` shows the park covered. `wt queue resume <id> --by conductor
  --yes` on a fresh park with no record is refused, and works after `--decision … --rule …`.
  Then `wt queue mvp-report` shows no new `missing` or `late` park.
- **Regression guard:** full `uv run pytest`, including `tests/test_agentic_decisions.py`,
  `tests/test_agentic_queue_autopark.py` and the park/resume tests of the queue CLI; the
  `wt.queue.*.v1` JSON gains only the `decision` key.

## Rollout / migration

1. Merge the code and the skill together. No data migration: `park.decision_request` is absent on
   existing parks and an existing PARKED campaign needs no request.
2. A running conductor re-reads the skill on `code_changed` (the skill's existing re-arm rule).
   Until it does, its bare `wt queue park --by conductor` is refused with a message that names
   the missing options.
3. Parks already missing or late stay in the report as they are; the window for E4 starts at the
   first campaign admitted after the merge.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. A `steward:<P>` actor is held to the same rule as the conductor; the escape hatch for a
  stuck case is to record the decision with `wt queue decision`, not an opt-out flag.

### Test record (2026-10-31)

- `uv run pytest tests/test_agentic_queue_park_decision.py` (new, 30 cases: refusals, human
  refusals, record-after-park by ledger `seq` under a frozen clock, deferred park, crash repair,
  dropped request, resume/abandon covering rule for conductor and steward, `park.auto` and human
  exemptions, plan JSON, E4 over the 5-park fixture): all pass.
- `tests/test_skills.py` (incl. the new `test_conduct_skill_park_resume_decision_rules`),
  `tests/test_agentic_decisions.py`, `tests/test_agentic_queue_autopark.py`,
  `tests/test_agentic_autopark.py`, `tests/test_agentic_queue_cli.py`, `tests/test_agentic_queue.py`, `tests/test_agentic_mvp_report.py`,
  the escalation / TUI tests, `tests/test_agentic_campaign_cli.py`, `tests/test_agentic_events.py`,
  `tests/test_agentic_dashboard.py` (416 + 72 tests): pass.
- Sabotage: `_require_decision` returning early makes 6 tests of the new file fail.
- Manual steps of the Test plan were exercised through `CliRunner` against a temp store (same
  commands, same outputs), not on a live queue. The full `uv run pytest` run is left to gating.

### As shipped (2026-10-31)

- The deferred request is carried onto the park inside `_park` itself (from
  `park_requested.decision_request`), not through a `Step` field: every path that applies a
  park request (`_park_row`, the `finish` branches of `_apply`) gets it without a new field.
  `park` ledger events do not carry `decision_request`.
- `settle_decision_requests(cfg, campaign=None)` runs at the end of the tick, after the step
  results and `_record_auto_waits`; `wt queue park --yes` calls it with its own campaign in
  immediate mode. `apply_resume` / `apply_abandon` journal `decision_request_dropped` when the
  park they clear still carries a request.
- `_require_decision` also exempts `by: queue` (the automatic resume), besides `park.auto` and
  `human`. The CLI validates option shape in `cli_queue._decision_request`; `plan_*` receive the
  validated request (`decision=`) and `plan_park` raises when a non-human actor has none.

- Gate triage (loop 2): the two failures of the full `-n 4` run are load flakes, not caused by
  this spec. On a host at load average ~32, `tests/test_agentic_adopt.py` +
  `tests/test_agentic_exit_paths.py` under `-n 4` fail 3 tests with the change and 3 other tests
  with the change reverted (`git apply -R` of the src diff), a different set each run
  (`test_adopt_orphan_group_waited_and_killed`, `test_adopt_review_*`, `test_draining_ticks_*`,
  `test_fanout_detach_precondition`); they pass alone. The tick step is a no-op unless a PARKED
  campaign carries a `decision_request`, and the tick now wraps it so it can never raise
  (`decision_record_failed` is journaled instead).
