---
id: SPEC-0304
title: "MVP scorecard: `wt queue mvp-report` computes exit criteria E1–E6 over the last N merged campaigns"
status: accepted
owner: user
created: 2026-10-30
updated: 2026-10-30
milestone: "M10: agentic MVP"
kind: feature
parent: SPEC-0298
depends_on: [SPEC-0299, SPEC-0300, SPEC-0301]
tags: [agentic, conductor, metrics, mvp]
---

## Context

[SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) is done when its exit
criteria E1–E6 hold over a window of at least 20 consecutive merged campaigns that start
after SPEC-0299, SPEC-0300 and SPEC-0301 are merged. The data for each criterion exists, but
it is spread over four sources, and none of them takes a campaign window:

- `wt queue conductor-cost --json` (`conductor_cost.cost_report`,
  `src/wt/agentic/conductor_cost.py:287-361`) reports conductor USD, `merges`,
  `usd_per_merge` and `context.median`. Its window is whole local days (`since`/`until`
  are `datetime.date`), and `merges` counts terminal campaigns that ended in those days.
- `wt queue wakeups --json` (`quiet.wakeups_report`, `quiet.py:348-380`) counts delivered
  lines, also by whole days. Its `merges` comes from `events.jsonl` `merged` / `handed_off`
  events. On the live store it reports 115 merges and 2 deliveries, because deliveries were
  only recorded from SPEC-0300's merge on, so an all-time ratio (0.02) is meaningless.
- `wt queue decisions --parks` (`decisions.park_coverage`, `decisions.py:137`) matches each
  park to a covering decision. It does not check E4's "before the campaign leaves PARKED".
- `wt campaign export` has per-campaign `conductor_usd` and outcomes.
- E5 (a comfort-assistant PR merged) and E6 (restarts and compactions re-oriented from the
  brief) have no report at all. The conductor transcript does record compactions, as
  `{"type": "system", "subtype": "compact_boundary"}` lines (3 in the live conductor's
  transcript). `events.jsonl` records `park_undecided`.

Computing the exit table by hand from these sources is what the epic's baseline did, and it
is error-prone (day vs campaign windows, all-time vs windowed merges).

## Goals / Non-goals

**Goals**
- One read-only command, `wt queue mvp-report`, that picks the window (the last N merged
  campaigns, default 20) and computes E1–E6 over exactly that window, with a pass/fail
  status per criterion and the numbers behind it.
- It says whether the window *qualifies*: N campaigns, all opened after the three
  measurement specs merged.
- It lists every park in the window with no decision record, or with one written only after
  the campaign left PARKED.
- The SPEC-0298 integration test (`tests/test_agentic_mvp_integration.py`) is added with this
  spec, as the epic's Test plan says.

**Non-goals**
- New data collection. Everything is read from the files SPEC-0299/0300/0301 and the
  campaign store already write.
- Changing `conductor-cost`, `wakeups` or `decisions` output. They keep their schemas. Only
  internal helpers with datetime bounds are added beside them.
- Judging E5's "human pulled in only for scope decisions" beyond what the records show. The
  report lists the human touchpoints for the person reading it.
- Rule conformance of decisions (the epic's "each decision carries a rule id" is enforced by
  `RULE_RE` at write time and only counted here).

## Decision

1. **Window.** Merged campaigns are those in `store.list_campaigns(include_terminal=True)`
   whose terminal transition is `MERGED` or `HANDED_OFF`, ordered by that transition's ts.
   The window is the last `--window N` (default 20) of them, optionally only those opened at
   or after `--since <ISO datetime | YYYY-MM-DD>`, and only those of `--project P` when
   given. The interval is `[start, end]`, where `start` is the earliest `open` ts among the
   window's campaigns (`conductor_cost.live_windows` already computes it) and `end` is the
   latest terminal ts. `merges` = the number of merged campaigns that ended inside
   `[start, end]` (≥ N, since campaigns of other projects or before `--since` may end
   inside it too).
   **Qualifying** = the window holds N campaigns and every one opened after the latest
   terminal ts of the merged campaigns for SPEC-0299, SPEC-0300 and SPEC-0301 (read from the
   store by `spec_id`). When those are not all merged, `qualifying` is false and
   `qualifying_reason` says which one is missing.
2. **Criteria** (targets from SPEC-0298; config overrides under
   `agentic.conductor.mvp.targets`, bad values fall back to the defaults):
   - **E1** conductor USD per merge = the USD of registered conductor sessions' turns with
     ts in `[start, end]` ÷ `merges`. Target ≤ 1.00. Status `incomplete` if any turn in the
     interval is unpriced (its USD is unknown), `fail` if over the target, else `pass`.
   - **E2** median `context` of those same turns. Target ≤ 150,000.
   - **E3** delivered lines (`deliveries.jsonl` records of reader `conductor` with
     `action` ≠ `hold`, ts in `[start, end]`) ÷ `merges`. Target ≤ 5. Status `no_data` when
     no delivery record exists before `start` and none in the interval (quiet mode was not
     yet recording).
   - **E4** parks in the window's campaigns with a covering decision written before the
     campaign left PARKED ÷ all parks in those campaigns with ts in `[start, end]`. A park
     is covered by the first decision on its campaign with `park.ts ≤ decision.ts <
     next park ts` (the `park_coverage` rule) **and** `decision.ts ≤` the ts of the
     transition out of PARKED that follows the park, or any decision if it is still
     PARKED. Target 100%. Uncovered parks are listed with `campaign`, `reason`, `park ts`,
     and `missing` or `late` (decided only after it left PARKED).
   - **E5** count of campaigns, any time at or after the qualifying point, of a project
     whose `merge.policy` is `pr` or `draft-pr`, with outcome `HANDED_OFF` and a PR that
     is merged (`close.pr_state == "MERGED"`, written by `sweep_handed_off`) or outcome
     `MERGED` via the sweep. Target ≥ 1. For each such campaign the report lists its
     human touchpoints: parks owned by the human (`queue.park_owner` == `human`) and
     escalations (`ESC` records for the campaign) with their question. Status `pass` when
     ≥ 1 such campaign has no human-owned park other than `human_decision`. Otherwise
     `fail`, with the touchpoints shown.
   - **E6** re-orientations in `[start, end]`. An event is a `compact_boundary` line in a
     registered conductor transcript, or the first turn of a registered conductor session
     other than the earliest one (a restart). An event is **oriented** when a Bash
     `tool_use` whose command contains `wt queue conductor-brief` occurs in that transcript
     within the next 10 assistant turns. It is **clean** when no `park_undecided` event in
     `events.jsonl` has ts in `(event ts, event ts + 15 min]`. Target: ≥ 1 event, and all
     oriented and clean. Status `no_data` with zero events.
   - Every criterion's status is one of `pass`, `fail`, `incomplete`, `no_data`. `ok` is
     true only when the window qualifies and all six are `pass`.
3. **Output.** `wt queue mvp-report [--window N] [--since D] [--project P] [--json]
   [--check]`. JSON schema `wt.conductor.mvp.v1`: `{window: {n, requested, start, end,
   merges, qualifying, qualifying_reason, campaigns: [ids]}, criteria: [{id, name, value,
   target, op, status, detail}], parks_undecided: [...], e5_campaigns: [...],
   e6_events: [...], ok}`. The text form is a six-row table (id, criterion, value, target,
   status), then the window line and the undecided parks. `--check` exits 1 unless `ok`;
   without it the command exits 0 whenever it ran.

## Design

- **New module `src/wt/agentic/mvp_report.py`**:
  - `DEFAULT_TARGETS`, `settings(cfg)`.
  - `merged_campaigns(cfg, *, project=None) -> list[dict]` (id, spec_id, project, start,
    end, outcome), built on `conductor_cost.live_windows` plus `store.read_state`.
  - `pick_window(merged, *, n, since) -> Window` and `qualifying(cfg, window, merged)`.
  - `e1_e2(cfg, start, end)`: uses a new `conductor_cost.turns_between(cfg, start, end)`,
    which returns the priced turns of registered sessions in the interval (reusing
    `sessions`, `find_transcript` and `_turns_of`).
  - `e3(cfg, start, end, merges)`: uses a new public `quiet.read_deliveries(cfg, reader)`
    (today's `_read_deliveries` under a public name, with the old name kept as an alias).
  - `e4(cfg, window)`: reads `decisions.read(cfg)` and each campaign's
    `store.read_ledger(cfg, cid, types=["park", "transition"])`, reusing the matching
    helper factored out of `decisions.park_coverage` (`decisions.match_parks(parks,
    records)`), and adds the exit check.
  - `e5(cfg, since)`: `store.list_campaigns(..., include_terminal=True)`, the project
    config's `merge.policy` via the existing project loader, `queue.park_owner`, and
    `escalations` records for the campaign.
  - `e6(cfg, start, end)`: a new `conductor_cost.transcript_marks(path)` yields
    `(ts, kind)` for `compact_boundary` lines and `(ts, "brief", turn_index)` for Bash
    tool_use inputs containing `conductor-brief`. `park_undecided` events come from
    `events.read_since(cfg, 0, types=["park_undecided"])`.
  - `report(cfg, *, n=20, since=None, project=None, now=None) -> dict` and
    `render_text(rep) -> str`.
- **CLI**: `wt queue mvp-report` is registered in `src/wt/agentic/cli_conductor.py`'s
  `attach`, next to `conductor-cost`. `--since` accepts an ISO date or datetime (the
  parser `wt queue decisions --since` uses).
- **Docs**: SPEC-0298's Acceptance criteria already name `wt queue mvp-report`. No other
  doc changes.

## Alternatives considered

- **Day-granular windows (reuse `conductor-cost --since/--until` as is).** The exit criteria
  are defined over campaigns, and a day boundary cuts a window's conductor turns in half
  or includes another window's. Datetime bounds from the window's own campaigns match the
  definition.
- **Sum the export's per-campaign `conductor_usd` for E1.** Attribution splits each turn
  over every live campaign, including abandoned and other-project ones, so the sum over
  merged campaigns under-counts the conductor's real cost per merge. The interval total
  divided by merges is the epic's definition.
- **Detect E6 re-orientation from `decisions.jsonl`.** A decision after a restart does not
  show the brief was read. The transcript's own tool call does.
- **Fail the command (exit 1) when criteria fail.** The report is mostly read by people and
  the conductor. Only `--check`, for scripts, turns `ok` into an exit code.

## Acceptance criteria

- [ ] `wt queue mvp-report --json` emits `wt.conductor.mvp.v1` with the window (N
      campaigns, `start`, `end`, `merges`, `qualifying`), six criteria each with `value`,
      `target` and a status in `pass|fail|incomplete|no_data`, `parks_undecided`, `ok`.
- [ ] The window is the last N merged campaigns by terminal ts, filtered by `--since` and
      `--project`. With fewer than N available, `qualifying` is false. A window containing a
      campaign opened before the latest of SPEC-0299/0300/0301 merged is not qualifying.
- [ ] E1 and E2 use only conductor turns inside `[start, end]`, and E1 is `incomplete` when
      any of them is unpriced. E3 counts only non-`hold` deliveries inside the interval.
      All three divide by merges that ended inside the interval.
- [ ] E4 reports a park decided only after the campaign left PARKED as `late` and one with no
      record as `missing`, and neither counts as covered. A park covered by a `by: queue`
      record counts as covered.
- [ ] E5 counts a PR-policy project's campaign whose PR is merged, and lists its human
      touchpoints. A campaign with a human-owned `main_dirty` park does not pass E5.
- [ ] E6 finds `compact_boundary` lines and session restarts in the interval, marks each
      oriented or not (a `conductor-brief` Bash call within 10 assistant turns) and clean or
      not (no `park_undecided` within 15 min), and passes only with ≥ 1 event, all of them
      oriented and clean.
- [ ] `--check` exits 1 unless `ok`. Without it the exit code is 0. The text table has one
      row per criterion.
- [ ] `tests/test_agentic_mvp_integration.py` exists and passes as the SPEC-0298 Test plan
      describes.

## Test plan

- **Automated, `tests/test_agentic_mvp_report.py`** (temp agentic store, campaigns from
  `store.create_campaign` driven to MERGED with `store.append_event` transitions at fixed
  timestamps, a registered conductor session pointing at a fixture transcript, prices
  configured):
  - `test_window_selection`: 25 merged campaigns → the last 20 by end ts; `--since` and
    `--project` filter; 15 available → `qualifying: false`.
  - `test_qualifying_after_measurement_specs`: a window campaign opened before the
    SPEC-0301 campaign merged → not qualifying, with the reason.
  - `test_e1_e2_interval`: fixture turns before, inside and after `[start, end]`; only the
    inside ones count; one unpriced inside turn → E1 `incomplete`; E2 equals the
    hand-computed median.
  - `test_e3_deliveries`: holds are excluded, a digest and a `code_changed` count, and no
    deliveries at all → `no_data`.
  - `test_e4_missing_late_covered`: four parks: one covered by a conductor decision, one by a
    `by: queue` decision, one decided after its resume (`late`), one never (`missing`) →
    `value == 0.5`, `fail`, both listed.
  - `test_e5_pr_campaign`: a `merge: pr` project campaign HANDED_OFF with
    `pr_state: MERGED` → pass. Add a `main_dirty` park → fail with the touchpoint listed.
  - `test_e6_compaction_and_restart`: a transcript with a `compact_boundary` followed by a
    `wt queue conductor-brief` Bash call within 10 turns, and a second session registered
    later whose first turns never call it → 2 events, one not oriented → `fail`. A
    `park_undecided` 10 min after an oriented event → not clean.
  - `test_cli_text_and_check`: CliRunner, text has six criterion rows; `--check` exits 1 on
    a failing fixture and 0 on an all-pass fixture.
- **Automated, `tests/test_agentic_mvp_integration.py`** (the SPEC-0298 integration test):
  fake runner and temp git repos, a scripted conductor transcript fixture and scripted
  follower deliveries. A queue of 3 campaigns runs to merge with one park and one decision.
  Assert that `conductor-cost` attributes the fixture's tokens to the 3 merges, that
  `wakeups` counts only decision-needing deliveries plus digests, that the brief lists the
  park with its decision, and that `mvp-report --window 3` gives E1–E4 equal to
  hand-computed values.
- **Sabotage checks:** drop the exit-time check in E4 → `test_e4_missing_late_covered`
  fails; use day bounds instead of the interval in E1 → `test_e1_e2_interval` fails; count
  `hold` deliveries in E3 → `test_e3_deliveries` fails.
- **Manual verification:** on the live store, run `wt queue mvp-report` and
  `wt queue mvp-report --json`. Check that E1's `value` matches `wt queue conductor-cost`
  for the same days when the window aligns with whole days, and paste the table into this
  spec's *As shipped*. The qualifying run belongs to SPEC-0298's *As shipped*.
- **Regression guard:** full `uv run pytest`; `tests/test_agentic_conductor_cost.py`,
  `tests/test_agentic_quiet.py` and `tests/test_agentic_decisions.py` unchanged (the
  `_read_deliveries` alias and the factored `match_parks` keep their callers working).

## Rollout / migration

1. Merge after SPEC-0299, SPEC-0300 and SPEC-0301 (all merged). Read-only, so nothing to
   migrate.
2. The skill is not changed by this spec. Having the conductor run `wt queue mvp-report`
   daily, or at the end of a window, is a conductor tuning call.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
