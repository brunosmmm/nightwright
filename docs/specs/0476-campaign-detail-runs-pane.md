---
id: SPEC-0476
title: "Campaign detail Runs pane: run table, verdict, and stream tail"
status: done
owner: user
created: 2026-11-08
updated: 2026-11-10
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
- Live follow runs only while the Runs tab is visible and the campaign is not
  terminal.

**Non-goals**

- Overview phase maths, bars, and sparklines. Overview stays the header lines
  SPEC-0407 ships.
- The Findings & decisions table, Mail threads, and the escalation-answer move
  (SPEC-0477).
- The desk History and Findings tabs.
- Any new write path. The pane only reads.
- Implementer `changed_files` and `tests_run`. The epic named them on this
  pane; this spec does not show them.

## Decision

- Replace the Runs placeholder with a run table and a detail pane inside
  `CampaignDetail`.
- The table is `history.runs` in the order the history layer returns them.
- The detail pane reads that run's directory. It shows the verdict status, the
  blocking rows from `verdict.findings`, `verdict.must_fix`, and
  `verdict.blockers`, the last 200 lines of `stderr.log`, and either the
  rendered stream tail or the pre-stream result text. `changed_files` and
  `tests_run` stay out.
- `agentic_model.stream_tail(cfg, campaign_id, run_id, offset, renderer)` feeds
  that Renderer and returns the new rendered lines and the next offset. The
  widget keeps the Renderer. It is no longer a no-op.
- Live follow uses `tail.read_lines`, drains once when `tail._ended(run_dir)`
  becomes true, then stops. Follow runs only while `tab-runs` is active and
  the campaign is not terminal.

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

`select_run(run_id)` is the same path as the first-paint selection. It
activates `tab-runs`, moves `#campaign-runs` to that `run_id`, and paints that
run's detail. SPEC-0477's `r` key calls it. An unknown id notifies
`not found: <id>` and does not change the tab.

### Detail

The selected row's run directory is `runs/<run_id>` under the campaign
directory.

Shown, as Rich `Text`, never as markup from file contents:

- The verdict line is the first of `verdict.status`, `verdict.disposition`,
  and `verdict.verdict` that is present, then `verdict.score` and
  `verdict.summary` when those keys exist. That line is the verdict status.
  Blocking rows, each as Rich `Text` with severity, title, and files: each
  `verdict.findings` item whose severity is `must_fix` or `major`; each
  `verdict.must_fix` object, shown as `must_fix`, with its title and files;
  each `verdict.blockers` string, shown as `major`, with the string as the
  title and no files. Other severities stay out. `changed_files` and
  `tests_run` stay out. SPEC-0477 lists every finding.
- The last 200 lines of `stderr.log`, when the file exists.
- When `stream.jsonl` is present, Live follow's initial paint shows the last
  200 rendered lines. When `stream.jsonl` is absent, show the pre-SPEC-0362
  result. Read the file named by `run.json` `files.raw` when that value is a
  relative path inside the run directory; if `run.json` is missing or has no
  `files.raw`, read `result.json`. If the JSON value is an object with a
  string `result` and `structured_output` is not an object, show that string's
  last 200 lines. Otherwise show the file's last 200 lines. A missing file is
  the line `no result.json`. Render it as `Text`.

A missing run directory, a malformed `run.json`, or a missing `stderr.log` is
a line in the detail (`no run directory`, `run.json malformed`, `no stderr`),
not an exception. The history layer's `gaps` stay as they are.

### Live follow

The widget keeps one `tail.Renderer` and a byte offset for the selected run,
and replaces both when the selection changes. `stream_tail` reads new
`stream.jsonl` lines from `offset` with `tail.read_lines`, feeds them with
`renderer.feed_line`, calls `renderer.flush_prose`, and returns
`(lines, next_offset)`. The initial paint feeds the whole file and shows the
last 200 rendered lines, with the offset at the end of the file. That read,
and the reads of `run.json`, `stderr.log`, and `result.json`, run in the same
worker as `load_detail`, off the event-loop thread.

The popup's `POLL_S` timer calls `stream_tail` only while `tab-runs` is
active, the selected run directory contains `stream.jsonl` (re-statted on that
tick), and the campaign is not terminal. On the tick that first sees
`tail._ended(run_dir)`, it drains once, including a trailing fragment with no
newline when that fragment is one JSON object, then stops. Later appends are
not read. A hidden tab does not call `stream_tail` and does not move the
offset. A run that already has `run.json` is ended, so deleting only
`exit.json` does not make it look live.

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
  file the user cannot see. Follow is armed only on the visible Runs tab, and
  only while the campaign is not terminal.
- **Treat every verdict finding as a blocker.** The pane would duplicate
  SPEC-0477. Blockers here are reviewer `must_fix` and `major` findings,
  `verdict.must_fix` objects, and `verdict.blockers` strings.

## Acceptance criteria

- [x] **AC1. The table.** On a fixture campaign that has runs, the Runs tab
  shows one row per `history.runs` entry, with that entry's `run_id`,
  `persona`, and `status`. Columns are `run_id`, `persona`, `phase`,
  `status`, `verdict_status`, `duration_s`, and `cost_usd`, in the order
  `history.runs` returns them. The strings `placeholder: runs` and
  `placeholder: runs (<id>)` are absent.
- [x] **AC2. Focus.** `open_campaign_detail(cid, focus={"tab": "runs", "run": id})`
  for an id in `history.runs` opens the Runs tab with that row selected and
  the detail showing that run's verdict status. An unknown run id opens
  Overview and notifies `not found: <id>`. `select_run(run_id)` activates
  `tab-runs`, moves `#campaign-runs` to that id, and paints that run's detail.
  An unknown id notifies `not found: <id>` and does not change the tab.
- [x] **AC3. Detail.** The selected run's detail shows each reviewer
  `must_fix` and `major` finding title, each `verdict.must_fix` title, and
  each `verdict.blockers` string, plus the last 200 lines of `stderr.log`
  when that file exists. A nit is not listed. A title or stderr line
  containing Rich markup characters is shown literally. A missing run
  directory, a malformed `run.json`, and a missing `stderr.log` render
  `no run directory`, `run.json malformed`, and `no stderr`, and nothing
  raises.
- [x] **AC4. Stream and result.** A run with `stream.jsonl` (`has_stream`)
  shows the `tail.Renderer` tail, the last 200 rendered lines from the
  initial paint. A run without `stream.jsonl` shows the pre-SPEC-0362 result
  instead. The fallback is those last 200 lines, not the raw object: the
  `result` string's last 200 lines when the object has a string `result` and
  `structured_output` is not an object, otherwise the file's last 200 lines.
  A missing result file shows `no result.json`. `stream_tail(cfg,
  campaign_id, run_id, offset, renderer)` feeds that Renderer, returns the
  new rendered lines and the next offset, and is not a no-op. The reads of
  `run.json`, `stderr.log`, `result.json`, and `stream.jsonl` run in the same
  worker as `load_detail`, off the event-loop thread.
- [x] **AC5. Live follow.** With the Runs tab visible, the campaign not
  terminal, and `_ended` false, a line appended to `stream.jsonl` appears in
  the detail without a manual reload. One `tail.Renderer` survives across
  those polls. Changing the selected run replaces that Renderer and the byte
  offset. On the tick that first sees `tail._ended(run_dir)`, one drain runs,
  including a trailing fragment with no newline when that fragment is one
  JSON object, and further appends do not appear. While Timeline is the
  active tab, an append does not call `stream_tail` and does not move the
  stream offset. A run that already has `run.json` is ended, so deleting only
  `exit.json` does not start follow.
- [x] **AC6. No writes.** Opening the pane and moving the cursor leaves the
  campaign directory byte-identical. This pane does not write `cache_dir`.

## Test plan

Automated, in `tests/test_tui_campaign_detail_ac.py` and a new
`tests/test_tui_campaign_runs.py`, on the SPEC-0409 / SPEC-0415 fixture copy:

1. Open the popup from the queue, press `3`, and assert the run table rows
   match `history.runs` for that campaign. Assert the columns are `run_id`,
   `persona`, `phase`, `status`, `verdict_status`, `duration_s`, and
   `cost_usd`, in the order `history.runs` returns them. Assert
   `placeholder: runs` is absent. Update the existing placeholder assertions
   in the same file.
2. Open with `focus={"tab": "runs", "run": id}` for a fixture run id and assert the
   selected row and the verdict status. Open with an unknown id and assert the
   Overview notify. With the popup already open on Overview, call
   `select_run` with a fixture run id and assert `tab-runs`, the row, and the
   detail. Call `select_run` with an unknown id and assert `not found: <id>`
   and that the current tab is unchanged.
3. On the fixture gate_triager run whose `verdict.must_fix` has a title,
   assert that title and its file. On a fixture reviewer run with a `major`
   finding, assert that title. On the conformance run, assert the nit title
   is absent. Add one `blockers` string on a copied implementer run and
   assert it is shown as `major`. Put a Rich tag in a title and in an stderr
   line and assert both are literal. Assert the stderr tail is the last 200
   lines.
4. One fixture run with `stream.jsonl` asserts a rendered stream line and
   that the initial paint is the last 200 rendered lines, with the offset at
   the end of the file. One without `stream.jsonl` asserts the fallback is
   the `result` string's last 200 lines when that string is used, otherwise
   the file's last 200 lines, and not the raw object. Delete that result file
   and assert the line `no result.json`. A unit test calls
   `stream_tail(cfg, campaign_id, run_id, offset, renderer)` with one
   Renderer across two offsets and asserts the new lines and the next offset.
   Assert the `run.json`, `stderr.log`, `result.json`, and `stream.jsonl`
   reads run through the same `asyncio.to_thread` worker as `load_detail`,
   not on the event-loop thread.
5. Copy a fixture campaign that has `stream.jsonl`, set its `state.json`
   state to `IMPLEMENTING` so `view.terminal` is false and the `POLL_S` timer
   exists, remove that run's `exit.json` and `run.json`, and monkeypatch
   `runproc.run_status` (or `tail._ended`) to report running until the test
   writes `exit.json`. Append a line, wait with `pilot_until`
   (`tests/waits.py`), then assert the line is visible and that the same
   Renderer was fed on the initial paint and on the poll. End the run with a
   trailing fragment that has no newline and is one JSON object, assert that
   fragment is drained once, append again, and assert the new line stays out.
   Move the cursor to another run and assert the Renderer and the offset were
   replaced. Switch to Timeline, append, and assert `stream_tail` is not
   called and the stored offset is unchanged. On a second copy, delete only
   `exit.json` while `run.json` remains and assert follow does not start.
6. Hash the campaign directory before and after opening the pane and moving
   the cursor. Assert the directory is byte-identical. Assert this pane does
   not write `cache_dir`.
7. Delete a copied run directory, replace one `run.json` with `{`, and delete
   one `stderr.log`. Assert those three lines (`no run directory`,
   `run.json malformed`, and `no stderr`) and that the popup stays open.

Manual: `nw tui`, open a live campaign, press `3`, confirm the table and that
a finished run does not keep growing.

## Rollout

Lands after SPEC-0407, which is on main. SPEC-0477 depends on this spec for
the `r` jump into a selected run. No data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed and the results recorded in *As shipped*.
- [x] Affected tests green at the close gate. `python3 tools/spec_lint.py` exits 0.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

### Test record (2026-11-09)

Ran `uv run pytest tests/test_tui_campaign_runs.py`, `tests/test_tui_campaign_detail_ac.py`, and `python3 tools/spec_lint.py`. Full suite left to gating. AC5 timeline hidden-tab and exit-only follow rely on `_runs_follow_poll` gating plus live-follow test; timeline spy test skips when no `has_stream` fixture row.

### Test record (2026-11-09, loop 1)

Ran `uv run pytest tests/test_tui_campaign_runs.py` (14 passed) and `tests/test_tui_campaign_detail_ac.py -k runs` (3 passed). Full suite left to gating.

### Test record (2026-11-10, loop 1)

Ran `uv run pytest -q tests/test_tui_campaign_runs.py`, `tests/test_tui_campaign_detail_ac.py -k runs`, `python3 tools/test_tiers.py --check`, and `python3 tools/spec_lint.py`. Full suite left to gating.

### Test record (2026-11-10, loop 0 gate)

Ran `python3 tools/test_tiers.py --check` (exit 0) and `uv run pytest -q tests/test_test_tiers.py::test_every_real_test_module_has_exactly_one_tier` after adding `pytestmark = pytest.mark.tui` to `tests/test_tui_campaign_runs.py`. Full suite left to gating.

### As shipped

Runs tab is a `#campaign-runs` table plus `#runs-detail-text` detail. Verdict blockers come from `findings`, `must_fix`, and `blockers`. Live follow keeps one `tail.Renderer` on the popup poll when the campaign is not terminal and the Runs tab is active. Review fixes: ignore stale `RowHighlighted` on refill; drain a no-newline JSON fragment on initial load when the run already ended; stop trailing `rstrip` on detail text; scroll `#runs-detail` on paint/append with page keys on Runs; resolve `files.raw` inside the run directory; do not latch follow done when stream is absent until ended or terminal; guard follow polls against overlap and stale selection. Loop 1: ended-stream initial catch-up stops when `read_lines` makes no progress, sets offset to EOF after one fragment drain, and latches `_run_follow_done` after the ended poll drain even when trailing bytes remain; tests cover 200 rendered stream lines, verdict lead priority, cursor/detail sync, stale async paints, and escaping `files.raw`.

Loop 2 (review fixes): the detail is a head (verdict line, blocking rows, stderr tail) plus a bounded stream/result body (`RunPaneDetail.head` / `.body`; follow keeps at most 2000 lines). `ended` is read in the worker before the stream (`AM.run_follow_state`), so the follow latch always matches what was read; `TL._ended` and the stream stat no longer run on the event loop. Stale tail/drain results are dropped unless the same Renderer is still current (A→B→A safe). When a followed run ends, or the campaign turns terminal on a reload, one drain runs and the head (verdict, blockers, stderr) is re-read. Control characters are stripped from stderr, stream lines, results, and verdict text. `findings`, `must_fix`, `blockers`, and `files` shapes are type-checked; an absolute `files.raw` is ignored; a renderer error becomes a visible `(stream line skipped: …)` line instead of a silent retry. A finished run opens scrolled to its verdict; a live run follows its tail and appends scroll only when the pane was already at the bottom. The detail loads only while the Runs tab is active (hidden tabs mark it stale and reload on activation), and held cursor keys are debounced. `_RunsTable` is gone: `#campaign-runs` reuses `_TimelineTable`. Deviation from Design: a bare-string `verdict.must_fix` item is no longer shown (the Design lists objects only). The unrelated `tests/test_remote_workers_integration.py` reorder was reverted.

### Test record (2026-11-10, loop 2)

Ran `uv run pytest -q tests/test_tui_campaign_runs.py tests/test_tui_campaign_detail_ac.py tests/test_tui_campaign_detail.py tests/test_tui_campaign_detail_model.py`, `python3 tools/test_tiers.py --check`, and `python3 tools/spec_lint.py`. New tests cover verdict lead fallbacks, `must_fix` findings, excluded `changed_files`/`tests_run`, malformed shapes and control characters, error lines in the open popup, verdict-first scroll, A→B→A stale tails, off-main-thread `_ended`, and the terminal-reload final drain; live follow now seeds `stream.jsonl` and asserts one Renderer is fed on the initial paint and the poll; no-append checks wait on counted follow ticks instead of fixed pauses. Full suite left to gating.

## Open questions

None.
