---
id: SPEC-0564
title: "SPEC-0476 follow-up: close the Runs pane test-strength gaps"
status: accepted
owner: user
created: 2026-11-12
updated: 2026-11-12
kind: feature
source_idea: IDEA-1186
tags: [tui, agentic, tests]
depends_on: [SPEC-0476]
---

## Context

[SPEC-0476](./0476-campaign-detail-runs-pane.md) is `done`: the Campaign detail Runs tab
(`#campaign-runs` table plus `#runs-detail-text` detail) ships in
`src/nightwright/tui/campaign_detail.py` and `src/nightwright/tui/agentic_model.py`, with
its tests in `tests/test_tui_campaign_runs.py` (one module-level `pytestmark = pytest.mark.tui`).
Its final review (IDEA-887, campaign `SPEC-0476-20261109-ca46`) named seven places where the
shipped tests pass but would also pass if the behavior regressed. Each one is a
test-strength gap, not a known product bug:

1. **`must_fix` severity display.** `test_findings_must_fix_severity_and_excluded_keys`
   checks one `findings` item and `test_gate_and_reviewer_blockers_in_pane` checks one
   `verdict.must_fix` object. No test puts all three blocker sources (`verdict.findings`,
   `verdict.must_fix` objects, `verdict.blockers` strings) in one verdict and pins the
   row labels and order, or pins that a bare-string `must_fix` item, an unknown severity,
   and a differently cased severity stay out (`_blocking_rows`, `agentic_model.py`).
2. **Verdict lead fallback.** `test_verdict_line_lead_fallbacks` asserts only the first
   line of `load_run_pane_detail`. It does not cover a `null` lead key falling through to
   the next key, a verdict with no lead key at all, or that the table's `verdict_status`
   cell agrees with the pane's lead.
3. **Late load vs a changed run.** `test_stale_run_detail_dropped_on_switch` asserts only
   the rendered text after a slow load for run A lands while run B is selected. It does
   not assert that the widget's follow state (`_run_renderer`, `_run_offset`,
   `_run_loaded_id`, `_run_head`, `_run_body`, `_run_follow_done`) still belongs to B, and
   it does not show that each of the two guards in `_load_run_detail_async` (the token
   check and the `_selected_run_id` check) is needed.
4. **`j` / `k` move the detail with the cursor.** `test_runs_cursor_jk_and_arrows` checks
   the cursor row and then a conditional detail assertion (`if next_id == "020-test_reviewer"
   else GATE_TITLE not in …`) that is vacuous for most rows. Nothing asserts that after
   every `j`, `k`, `up`, `down` the detail is the run under the cursor.
5. **Verdict line composition.** The tests use `startswith` and a few exact strings. They
   do not pin the exact line `lead score summary` for every combination of present and
   absent parts, a score of `0`, an empty summary, or that Rich markup and control
   characters in the summary stay literal.
6. **`files.raw` outside the run directory.** `test_string_result_and_files_raw` covers
   one `../` escape. An absolute path, a symlink inside the run directory that points
   outside it, a sibling directory that shares the run directory's name prefix, a `..`
   that stays inside, and non-string or blank values are untested.
7. **Stream initial paint bound to 200 lines.** `test_stream_initial_paint_caps_rendered_lines`
   checks two marker lines around the cut. It does not check the count, the exact
   boundary (200 versus 201 rendered lines), that the cap counts rendered lines (one
   stream event can render several), or that the cap applies to the popup paint and not
   only to `load_run_pane_detail`.

## Goals / Non-goals

**Goals**

- Add or tighten tests so that each of the seven gaps fails when the behavior regresses.
- Show each strengthened test is not vacuous by a recorded one-time mutation check.

**Non-goals**

- Any change to product behavior, data shapes, key bindings, or SPEC-0476's Design. If a new
  test exposes a real bug, the bug is fixed in the smallest way that makes SPEC-0476's
  Design true and the fix is recorded in this spec's *As shipped*; it is not a redesign.
- New test modules. Every test lands in `tests/test_tui_campaign_runs.py` (it already has
  its single speed-tier marker), so no new `pytestmark` is added.
- Findings & decisions, Mail, or Overview coverage (SPEC-0477, SPEC-0407).

## Decision

Extend `tests/test_tui_campaign_runs.py` with one strengthened test per gap, reusing its
existing helpers (`_copy_campaign`, `_edit_run_json`, `_live_copy`, `runs_detail`,
`runs_table`, `_spy_polls`, `pilot_until`). Where an existing test already covers part
of a gap, tighten it in place instead of adding a near-duplicate. Waits use
`tests/waits.py` (`pilot_until`), never a fixed sleep before a positive assertion.

## Design

All fixtures are copies of the SPEC-0409 / SPEC-0415 fixture campaign made with
`_copy_campaign`; no test writes the live data directory.

### 1. `must_fix` severity display

One copied run's `run.json` gets this verdict (titles contain a unique marker each):

- `findings`: `{severity: "must_fix", title, files: [a, b]}`, `{severity: "major", title}`,
  `{severity: "minor"}`, `{severity: "nit"}`, `{severity: "MUST_FIX"}`,
  `{severity: "blocker"}`, and one item that is a bare string.
- `must_fix`: one object with a title and files, one object without `files`, and one
  bare-string item.
- `blockers`: one string and one non-string item (an integer).

Assert on `load_run_pane_detail(...).head.plain` that the blocking rows are exactly, in
this order, `must_fix: <t>` plus `  files: a, b`, `major: <t>`, `must_fix: <t>` plus its
files line, `must_fix: <t>` with no files line, then `major: <blocker string>` with no files
line, and that no other `<severity>:` row exists. Assert none of the minor, nit,
`MUST_FIX`, `blocker`, bare-string, and integer markers appear anywhere in the detail.
Repeat the first row through the open popup (`runs_detail(app)`) so the widget text, not
only the loader, shows the `must_fix` label.

### 2. Verdict lead fallback

Table of verdicts through `only(verdict)` (the existing helper in
`test_verdict_line_lead_fallbacks`), each asserting the exact first line and the history row:

- `{status: None, disposition: "d", verdict: "v"}` leads with `d`.
- `{status: None, disposition: None, verdict: "v"}` leads with `v`.
- `{status: "s", disposition: "d"}` leads with `s`.
- `{score: 4}` has no lead: the first line is `4`.
- `{}` and a non-dict `verdict` (`"text"`, `[1]`) produce no verdict line: the first line of the
  detail is the first blocking row, else the first stderr line, else the first body line.

For each case with a lead, assert that `Hist.campaign_history(...)["runs"]` row's
`verdict_status` equals the lead the pane shows, so the table and the pane cannot disagree.

### 3. Late load vs a changed run

In `_load_run_detail_async` (`campaign_detail.py`), selecting a run via `_select_run_row`
resets the follow state and either calls `_load_run_detail` (Runs tab active: bumps
`_run_detail_token`) or only sets `_run_detail_stale` (any other tab: token unchanged); in
both cases `_selected_run_id` is set. A late A-then-B switch therefore trips both guards at
once, so the A-to-B case alone cannot show that each guard is needed. Because the worker is
`exclusive`, a superseded worker is normally cancelled before it reaches the guards; the
guard cases below therefore land the late result by awaiting
`screen._load_run_detail_async(run_id, token)` directly inside the pilot, with
`AM.load_run_pane_detail` patched to return a distinguishable snapshot (the run's marker
plus `LATE-0564`) after a `threading.Event` gate, and `screen._paint_runs_detail` wrapped
by a counting spy.

- **3a. A-to-B (existing test, tightened).** After the slow load for A lands while B is
  selected, assert `_run_loaded_id == B`, `_run_offset` equals B's initial offset,
  `_run_renderer` is the object painted for B, `_run_head` and `_run_body` contain B's
  marker and not A's, `_run_follow_done` matches B's snapshot, and the paint spy count did
  not rise after B's paint. Both guards trip here; this case does not distinguish them.
- **3b. Token guard alone.** Select A and wait until A is painted (`_selected_run_id == A`,
  `_run_loaded_id == A`). Remember `old = screen._run_detail_token`, call
  `screen._load_run_detail(A)` again (token bumps, selection stays A) and wait for its
  paint. Record the spy count, `_run_renderer` and `_run_head`. Then await
  `screen._load_run_detail_async(A, old)` returning the `LATE-0564` snapshot. Selection
  matches, so only the token check can reject it: assert the spy count, `_run_renderer`
  identity, `_run_offset`, `_run_head`, `_run_body`, and `runs_detail(app)` are unchanged
  and `LATE-0564` is absent. **Kills the mutation that removes only the token check.**
- **3c. Selection guard alone (Timeline tab, select B).** Select A and wait for its paint,
  then press `2` (Timeline tab active) and call `screen._select_run_row(B)`. This only
  sets `_selected_run_id = B`, resets the follow state (`_run_loaded_id is None`,
  `_run_renderer is None`, `_run_offset == 0`) and sets `_run_detail_stale = True`; the
  token is unchanged. Await `screen._load_run_detail_async(A, screen._run_detail_token)`
  returning the `LATE-0564` snapshot. The token matches, so only the `_selected_run_id`
  check can reject it: assert `_selected_run_id == B`, `_run_loaded_id is None`,
  `_run_renderer is None`, `_run_offset == 0`, `_run_detail_stale is True`, `_run_head`
  and `_run_body` equal their pre-load values, and the paint spy count is unchanged.
  **Kills the mutation that removes only the `_selected_run_id` check.**
- **3d. Timeline tab, same run (accepted, scroll skipped).** Select A, wait for its paint,
  press `2`, spy on `screen._scroll_runs_detail`, then await
  `screen._load_run_detail_async(A, screen._run_detail_token)` returning the `LATE-0564`
  snapshot. Nothing checks the active tab, and token and selection match, so the load is
  accepted: assert `_run_loaded_id == A`, `_run_detail_stale is False`, `_run_head` contains
  `LATE-0564`, the paint spy count rose by exactly one, and `_scroll_runs_detail` was not
  called (the scroll is skipped off the Runs tab).

### 4. `j` / `k` and arrows move the detail

On a copy, give every run in `history.runs` a distinct verdict `summary` marker
(`row-marker-<run_id>`) by editing each `run.json`. Walk the whole table with `down`, `j`,
`up`, `k`. The detail loads in a worker after `_RUN_DETAIL_DEBOUNCE_S` and a thread hop, so
the cursor moves before the detail does and the cursor alone is not a wait condition. After
every key:

1. `await pilot_until(pilot, lambda: tbl.cursor_row == expected_row and
   f"row-marker-{run_id_at(expected_row)}" in runs_detail(app))`, where `expected_row` is
   the row the key must reach and `run_id_at` reads `history.runs`.
2. Only then assert that no other run's marker is in `runs_detail(app)` (unconditionally,
   no `if`) and that `screen._selected_run_id` is the cursor's run.

The boundary presses (`k` at the first row, `j` at the last row) have no positive event to
wait on: press, run `settle` (or `_after_ticks`), then assert the cursor row, the selected
run, and the detail (the same single marker) are unchanged and nothing raised. No fixed
sleeps.

### 5. Verdict line composition

Through `load_run_pane_detail(...).head.plain.splitlines()[0]`, assert exact equality:

- `{status: "s", score: 7, summary: "m"}` is `s 7 m`.
- `{status: "s", score: 0, summary: "m"}` is `s 0 m` (a zero score is shown).
- `{status: "s", score: 7, summary: ""}` is `s 7`.
- `{status: "s", summary: "m"}` is `s m`.
- `{score: 7, summary: "m"}` is `7 m` and `{summary: "m"}` is `m`.
- A summary of `[b]x[/b]` is shown literally, and one with `\x1b[31m` and `\x85` has those
  control characters stripped, in both the loader head and the popup (`runs_detail`).
- The verdict line is line one, ahead of the blocking rows and the stderr lines.

### 6. `files.raw` outside the run directory

For a copied run without `stream.jsonl`, with `result.json` holding a known sentinel
`RESULT-JSON-0564` and a sentinel file `OUTSIDE-0564` placed outside the run directory,
set `run.json` `files.raw` to each value and assert the pane text:

- Hostile values show `RESULT-JSON-0564` and never `OUTSIDE-0564`: `../../../outside.txt`; an
  absolute path to the sentinel; **an absolute path to a file inside the run directory**
  (`str(run_dir / "alt.txt")`, holding `ALT-0564`), which must also show `RESULT-JSON-0564`
  and not `ALT-0564` because absolute paths are rejected even when they stay inside
  (`_result_file_path` checks `Path(raw).is_absolute()` first). Without the `is_absolute()`
  check the outside-sentinel absolute value is still rejected by `is_relative_to`
  (`run_dir / "/abs"` resolves to `/abs`), so the inside-the-run-dir absolute case is the one
  that kills that mutation; keep both. More hostile values: a symlink `link.txt` inside the run directory that points
  to the sentinel; `../<run_id>-evil/x.txt`, where that sibling directory shares the run
  directory's name as a prefix and holds the sentinel; a whitespace-only string; a number;
  a list; and `files` itself being a list.
- Allowed values: `alt.txt` and `sub/../alt.txt` (both stay inside) show the content of
  `alt.txt`.
- An allowed relative path to a file that does not exist shows the line `no result.json`.
- A missing `run.json` reads `result.json`.

Symlink creation is guarded with `pytest.skip` only when `os.symlink` raises
`OSError`/`NotImplementedError`.

### 7. Stream initial paint bound

On a copy whose `stream.jsonl` holds one rendered line per event (the existing
`tool_result` events), build files of 199, 200, 201, and 250 events:

- Assert `len(snap.body)` is `min(n, 200)`, the first shown line is
  `tool-out-{n-200:04d}` once `n > 200` and `tool-out-0000` otherwise, the last shown line
  is `tool-out-{n-1:04d}`, and `snap.offset` equals the file size.
- Build a file where one event renders three lines (a multi-line tool result) and the file
  has 100 such events: assert the body is 200 lines (a bound on rendered lines, not events)
  and ends with the last event's last line.
- Open the popup on a 250-event run and assert `runs_detail(app)` contains line
  `tool-out-0050` and not `tool-out-0049`, that the stderr tail (200 lines) and verdict line
  still appear in full (the cap covers the stream body only), and that a follow append after
  the paint adds its line without dropping `tool-out-0050`.

## Alternatives considered

- **Reopen SPEC-0476.** It is `done`; the repo contract forbids rewriting a past-draft
  spec. A follow-up spec keeps the ledger faithful.
- **One generic property or snapshot test of the whole detail.** A snapshot passes when the
  text drifts consistently and hides which of the seven behaviors broke. Exact-line
  assertions per behavior fail with a reason.
- **Ignore the gaps because behavior shipped correctly.** The point is the next refactor of
  `_blocking_rows`, the verdict line, `_result_file_path`, or the load guards: today those
  would pass silently.

## Acceptance criteria

- [ ] **AC1. `must_fix` display.** A test with the mixed verdict of Design 1 asserts the
  exact ordered blocking rows and labels (`must_fix`, `major`), that unknown, differently
  cased, bare-string, and non-string items are absent, and asserts the `must_fix` row again
  through the popup widget text.
- [ ] **AC2. Lead fallback.** The Design 2 table asserts the exact first line for each
  verdict, the no-lead and no-verdict cases, and that the history `verdict_status` equals the
  pane's lead.
- [ ] **AC3. Late load.** Case 3a asserts the widget's stored run id, offset, renderer,
  head, and body belong to the selected run after a stale load lands and that no repaint
  follows. Case 3b (stale token, same selection) and case 3c (changed selection, current
  token, Timeline tab) each assert the unchanged state and paint count; case 3d asserts the
  accepted load with the scroll skipped. The mutation check shows 3b fails when only the
  token check is removed and 3c fails when only the `_selected_run_id` check is removed.
- [ ] **AC4. Cursor follows detail.** The `j` / `k` test asserts, after every key over the whole
  table, once `pilot_until` sees the cursor at the expected row and that run's marker in the
  detail, that no other run's marker is present, and that `k` at the first row and `j` at the
  last row change nothing after `settle`. It has no conditional assertion and no fixed sleep.
- [ ] **AC5. Composition.** The Design 5 cases assert exact first-line equality, a shown zero
  score, an omitted empty summary, literal markup, stripped control characters, and the line
  order (verdict line, blocking rows, stderr).
- [ ] **AC6. `files.raw`.** Every hostile value in Design 6 (including an absolute path to a
  file inside the run directory) shows `result.json` content and never the outside or
  `ALT-0564` sentinel, every allowed value shows its file, a missing allowed file shows
  `no result.json`, and a missing `run.json` reads `result.json`.
- [ ] **AC7. 200-line bound.** The Design 7 tests assert `len(body)` for 199, 200, 201, and 250
  events, the first and last shown lines, offset at end of file, the rendered-line bound for a
  multi-line event, and the popup text including the uncapped stderr tail and verdict line.
- [ ] **AC8. No product change by default.** The diff outside `tests/` and this spec is empty,
  or each production change is a bug fix listed in *As shipped* with the test that exposed it.
- [ ] **AC9. Tier and waits.** `tests/test_tui_campaign_runs.py` still has exactly one
  module-level `pytestmark = pytest.mark.tui`, adds no fixed sleep before a positive
  assertion, and `python3 tools/test_tiers.py --check` exits 0.

## Test plan

- **Automated tests:** all in the existing `tests/test_tui_campaign_runs.py` (tier `tui`,
  already marked `pytestmark = pytest.mark.tui`; no second marker and no new module).
  Tightened in place: `test_findings_must_fix_severity_and_excluded_keys` (gap 1),
  `test_verdict_line_lead_fallbacks` (gap 2), `test_stale_run_detail_dropped_on_switch`
  (gap 3), `test_runs_cursor_jk_and_arrows` (gap 4), `test_string_result_and_files_raw`
  (gap 6), and `test_stream_initial_paint_caps_rendered_lines` (gap 7). New:
  `test_verdict_line_composition` (gap 5), `test_late_load_token_guard` (3b),
  `test_late_load_selection_guard` (3c), and `test_late_load_accepted_on_timeline_tab` (3d). Run them with `uv run pytest -q tests/test_tui_campaign_runs.py` while
  iterating, and `python3 tools/affected_tests.py --run uv run pytest -q` before close.
- **Mutation check (once, recorded in *As shipped*):** for each of these edits made
  temporarily in a scratch copy and reverted, the named test must fail: drop the `must_fix`
  label for `verdict.must_fix` objects (AC1), change the lead key order to
  `disposition, status, verdict` (AC2), remove the `token != self._run_detail_token` check (only
  `test_late_load_token_guard` must fail; 3a and 3c stay green) and, separately, the
  `self._selected_run_id != run_id` check (only `test_late_load_selection_guard` must fail;
  3a and 3b stay green) (AC3), make `_select_run_row`
  skip the detail load (AC4), join the verdict parts with `", "` or drop a zero score (AC5),
  replace `is_relative_to` with a string-prefix test and, separately, drop the
  `is_absolute()` check (the absolute-path-inside-the-run-dir case must fail) (AC6), and change `RUNS_STREAM_LINES` to 199, 201, and an
  event-count bound (AC7).
- **Manual verification:** none beyond the mutation check; the behavior under test is not
  changed.
- **Regression guard:** the rest of `tests/test_tui_campaign_runs.py`, plus
  `tests/test_tui_campaign_detail_ac.py`, `tests/test_tui_campaign_detail.py`, and
  `tests/test_tui_campaign_detail_model.py` stay green;
  `python3 tools/test_tiers.py --check` exits 0; `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

Tests only; no migration. Lands on main through the normal queue. Nothing depends on it.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed (affected tests pass, mutation check recorded); manual steps: none.
- [ ] No regressions: affected tests green at the close gate; `python3 tools/test_tiers.py
  --check` and `python3 tools/spec_lint.py` exit 0.
- [ ] Spec body updated to match what shipped (*As shipped* lists any bug fix).
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

None.
