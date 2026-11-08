---
id: SPEC-0476
title: "Campaign detail Runs pane: run table, verdict, and stream tail"
status: accepted
owner: user
created: 2026-11-08
kind: feature
parent: SPEC-0405
source_idea: IDEA-1035
tags: [tui, agentic, observability]
depends_on: [SPEC-0407]
---

## Context

Child of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md).
The popup shell [SPEC-0407](./0407-campaign-detail-popup-timeline-phase-time-and.md)
shipped Overview and Timeline and left the Runs tab as one line,
`placeholder: runs`. The history layer already returns `history.runs`, and each
run directory already has `run.json`, `stderr.log`, and, for runs after
[SPEC-0362](./0362-stream-json-capture-of-persona-runs-and-nw.md),
`stream.jsonl`. `agentic_model.stream_tail` is a no-op waiting for this spec.

The human opened a queue row and still saw placeholder panes. This spec is the
Runs pane named in SPEC-0407's non-goals. Findings & decisions and Mail are
[SPEC-0477](./0477-campaign-detail-findings-and-mail.md).

## Goals / Non-goals

**Goals**

- The Runs tab lists every `history.runs` row and shows the selected run's
  verdict, blocking findings, stderr tail, and stream tail.
- A popup opened on a known run id selects that run.
- Live follow runs only while the Runs tab is visible and the run has not ended.

**Non-goals**

- Overview phase maths, bars, and sparklines. Overview stays the header lines
  SPEC-0407 ships.
- The Findings & decisions table, Mail threads, and the escalation-answer move
  (SPEC-0477).
- The desk History and Findings tabs.
- Any new write path. The pane only reads.

## Decision

- Replace the Runs placeholder with a run table and a detail pane inside
  `CampaignDetail`.
- The table is `history.runs` in the order the history layer returns them.
- The detail pane reads that run's directory. It shows the verdict, findings
  whose severity is `must_fix` or `major`, the last 200 lines of `stderr.log`,
  and either the rendered stream tail or the pre-stream result text.
- `agentic_model.stream_tail` returns those rendered lines. It is no longer a
  no-op.
- Live follow uses `tail.read_lines` and stops when `tail._ended(run_dir)` is
  true. Follow is armed only while `tab-runs` is the active tab.

## Design

### Table

`tab-runs` keeps its id. The placeholder `Static` goes away. A `DataTable`
(`#campaign-runs`) lists `run_id`, `persona`, `phase`, `status`,
`verdict_status`, `duration_s`, and `cost_usd`. Cursor type is row. Digit keys
`1`–`5` still switch popup tabs, forwarded the same way the timeline table
forwards them. `j` / `k` and the arrow keys move the run cursor.

`resolve_focus` already returns `("runs", run_id)` for a known run. The pane
selects that row on the first paint. An unknown run id still selects Overview
and notifies `not found: <id>`, which SPEC-0407 already specifies. The text
`placeholder: runs` is not rendered.

### Detail

The selected row's run directory is `runs/<run_id>` under the campaign
directory. The pane reads `run.json` off the event-loop thread.

Shown, as Rich `Text`, never as markup from file contents:

- `verdict.verdict`, `verdict.score`, and `verdict.summary` when those keys
  exist.
- Each `verdict.findings` item whose `severity` is `must_fix` or `major`:
  severity, title, and that item's `files` list. Those two severities are the
  blockers. Other severities stay out of this pane. SPEC-0477 lists every
  finding.
- The last 200 lines of `stderr.log`, when the file exists.
- When `has_stream` is true, the last 200 lines produced by feeding
  `stream.jsonl` through `tail.Renderer`. When it is false, the result text
  from `result.json` (`files.raw` on `run.json`, otherwise `result.json`).
  That is the pre-SPEC-0362 record.

A missing run directory, a malformed `run.json`, or a missing tail file is a
line in the detail (`no run directory`, `run.json malformed`, `no stderr`),
not an exception. The history layer's `gaps` stay as they are.

### Live follow

`stream_tail(cfg, campaign_id, run_id, offset) -> (lines, next_offset)` reads
new `stream.jsonl` lines from `offset` with `tail.read_lines`, feeds them to a
`tail.Renderer`, and returns the rendered lines. The widget keeps the offset.

A follow timer (the popup's existing poll interval, `POLL_S`) calls it only
when all of these hold:

- `tab-runs` is active,
- the selected run has `has_stream`,
- `tail._ended(run_dir)` is false.

When the tab is hidden, the timer does not read the stream. When `_ended`
becomes true, the pane drains the remaining lines once and then stops. A run
that already has `run.json` is ended, so deleting only `exit.json` does not
make it look live.

The live-follow test builds a running run by removing both `exit.json` and
`run.json` in the copied data dir and monkeypatching `runproc.run_status` (or
`tail._ended`) to report running until the test writes `exit.json`.

### Tests that change

`tests/test_tui_campaign_detail_ac.py` currently asserts `placeholder: runs`.
Those assertions become assertions that the run id is a row in
`#campaign-runs` and that the detail shows that run's verdict status. The
`placeholder: records` and `placeholder: mail` assertions stay until
SPEC-0477.

## Alternatives considered

- **Render the stream on the Timeline row.** The epic keeps the stream tail on
  the Runs pane, and the Timeline already has a one-event detail.
- **Follow a run while the user is on Timeline.** Follow would keep reading a
  file the user cannot see. Follow is armed only on the visible Runs tab.
- **Treat every verdict finding as a blocker.** The pane would duplicate
  SPEC-0477. Blockers here are `must_fix` and `major` only.

## Acceptance criteria

- [ ] **AC1. The table.** On a fixture campaign that has runs, the Runs tab
  shows one row per `history.runs` entry, with that entry's `run_id`,
  `persona`, and `status`. The strings `placeholder: runs` and
  `placeholder: runs (<id>)` are absent.
- [ ] **AC2. Focus.** `open_campaign_detail(cid, focus={"tab": "runs", "run": id})`
  for an id in `history.runs` opens the Runs tab with that row selected and
  the detail showing that run's verdict status. An unknown run id opens
  Overview and notifies `not found: <id>`.
- [ ] **AC3. Detail.** The selected run's detail shows each `must_fix` and
  `major` finding title from `run.json`, and the last 200 lines of
  `stderr.log` when that file exists. A finding of any other severity is not
  listed there. File text is rendered as `Text`, so a title containing Rich
  markup characters is shown literally.
- [ ] **AC4. Stream and result.** A run with `has_stream` shows the
  `tail.Renderer` tail. A run without `stream.jsonl` shows the `result.json`
  text instead. `stream_tail` returns rendered lines and is not a no-op.
- [ ] **AC5. Live follow.** With the Runs tab visible and `_ended` false, a
  line appended to `stream.jsonl` appears in the detail without a manual
  reload. After `_ended` becomes true, one more drain runs and further
  appends do not appear. While Timeline is the active tab, an append does
  not move the stream offset.
- [ ] **AC6. No writes.** Opening the pane and moving the cursor leaves the
  campaign data dir byte-identical apart from the history cache under
  `cache_dir`.

## Test plan

Automated, in `tests/test_tui_campaign_detail_ac.py` and a new
`tests/test_tui_campaign_runs.py`, on the SPEC-0409 / SPEC-0415 fixture copy:

1. Open the popup from the queue, press `3`, and assert the run table rows
   match `history.runs` for that campaign. Assert `placeholder: runs` is
   absent. Update the existing placeholder assertions in the same file.
2. Open with `focus={"tab": "runs", "run": <a fixture run id>}` and assert the
   selected row and the verdict status. Open with an unknown id and assert the
   Overview notify.
3. Point the detail at the fixture conformance run whose verdict has a `nit`
   finding and assert that title is absent. Add a copied run whose verdict
   has one `must_fix` finding and assert that title and its file are present.
   Put a Rich tag in the title and assert it is not interpreted.
4. One fixture run with `stream.jsonl` asserts a rendered stream line. One
   without asserts result text. A unit test calls `stream_tail` and asserts
   it returns lines.
5. The live-follow setup from the Decision: remove `exit.json` and `run.json`,
   patch `_ended` until the test writes `exit.json`, append a line, wait with
   `pilot_until` (`tests/waits.py`), then assert the line is visible, then
   end the run, append again, and assert the new line stays out. Switch to
   Timeline, append, and assert the stored offset is unchanged.
6. Hash the campaign directory before and after opening the pane.

Manual: `nw tui`, open a live campaign, press `3`, confirm the table and that
a finished run does not keep growing.

## Rollout

Lands after SPEC-0407, which is on main. SPEC-0477 depends on this spec for
the `r` jump into a selected run. No data migration.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed and the results recorded in *As shipped*.
- [ ] Affected tests green at the close gate. `python3 tools/spec_lint.py` exits 0.
- [ ] Spec body matches what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

None. The live-follow fixture setup is the one SPEC-0407 already recorded.
