---
id: SPEC-0433
title: "Human-readable campaign story: one shared timeline formatter for the TUI, the dashboard drilldown and nw campaign show"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
source_idea: IDEA-975
kind: feature
parent: SPEC-0405
tags: [agentic, tui, dashboard, observability, history]
---

## Context

Promoted from idea `IDEA-975`. Child of the TUI epic
[SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md). The dashboard part
belongs to [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md).

On 2026-11-04 the human pasted the timeline of `SPEC-0345-20261101-e596` and said it "looks like
some key=value json thing that you would send to an observability platform". Each ledger event
is one row: `seq ts type actor key=value …`. For example, `721 … run.end tick persona=gate_triager
run_id=092-gate_triager exit_code=0 is_error=False … usage={'input_tokens': 2, …}`. A loop
produces dozens of rows. Nothing is grouped, and nothing says what mattered.

That rendering (`render.campaign_event`) is shown in three places:
- **TUI campaign screen** ([SPEC-0246](./0246-tui-campaign-detail-screen.md), `done`).
  `tui/campaign_screen.py` shows a `DataTable` with one row per event, built by
  `agentic_model._entry` / `TimelineEntry`.
- **Dashboard drilldown** ([SPEC-0430](./0430-dashboard-campaign-drilldown-ledger-timeline.md),
  `done`). The payload `dashboard/campaign.py::_timeline` holds the last 40 events as
  `campaign_event` text, and `app.js` puts one `div.tl` per event in `#drill-timeline`.
- **`nw campaign show`** (`cli_campaign.py`). It prints the state header, then the last 5
  events as `campaign_event` lines.

This spec changes how those two `done` specs present the timeline. They are not rewritten:
their raw views remain, one key away (see Decision).

**Relation to SPEC-0406 (the shared history layer).** SPEC-0406 (`history.py`,
`nw.campaign.history.v1`, campaign IDEA-884) is `accepted` on its campaign branch. Its
`history.py` is not on `main`. Nothing on `main` groups ledger events today. This spec therefore
adds the lower layer, `agentic/timeline.py`, with no dependencies beyond the standard library.
It holds the phase table (the epic's `history.PHASES` names plus a terminal `done`), the
fix-loop predicate and the per-event collapsing. SPEC-0406's `history.py` and the epic's TUI
children (IDEA-884 history layer, IDEA-887 detail popup and its Timeline tab "grouped by loop")
are to import `timeline.PHASES`, `timeline.PHASE_OF`, `timeline.is_fix_loop` and
`timeline.story()` rather than re-derive them. `timeline` never imports `history` or
`campaign_export`, so there is no cycle when `history` moves `FIX_REASONS`.

## Goals / Non-goals

**Goals**
- G1. One pure, UI-independent formatter, `src/nightwright/agentic/timeline.py`. It turns ledger
  events into a *story*, `nw.campaign.story.v1`: events grouped by loop, then by phase visit.
  Each group carries its state path, wall time and cost delta. Lines are collapsed, and every
  line keeps the `seqs` of its raw events.
- G2. The TUI campaign screen shows the story as a tree. Enter or space expands and collapses
  a node, and `r` toggles the raw SPEC-0246 table.
- G3. The dashboard drilldown shows the story as collapsible, phone-friendly sections. A tap
  expands a section, and a "raw" toggle shows the raw events.
- G4. `nw campaign show` prints the story by default. `--raw` prints the old view, `--expand`
  also prints every line's children and quiet lines, and `--json` emits both.
- G5. The existing keys and filters keep working in every surface.

**Non-goals**
- Changing what the queue writes (no new ledger fields).
- `nw campaign log` stays raw. It is the raw ledger by definition.
- The SPEC-0406 history document, cache, records or CLIs.
- The epic's detail popup (IDEA-887). It will reuse this formatter.
- Server routing, listeners and auth for the dashboard. A parallel branch owns `server.py`.

## Decision

1. **A pure `timeline.story(events, *, campaign=None, state=None, now=None) -> dict` builds the
   story.** It imports only the standard library, writes nothing and never raises on odd
   input:
   - An event of unknown type becomes a short line, `type (by): k=v …`, and is counted in
     `unknown_types`.
   - A handler that throws on a malformed event yields an "(unreadable …)" line for that event,
     and the rest of the story is still built.
   - Every event's `seq` lands in exactly one group's `seqs`. Every event other than a
     `transition` lands in at least one line's `seqs`, so no data is silently hidden.
2. **Grouping.**
   - Loops are counted by `is_fix_loop`: a transition to IMPLEMENTING with reason `triage_fix`
     or `review_fix`, the same predicate as `campaign_export` and SPEC-0406.
   - Within a loop, a new group starts whenever a transition enters a different phase. The
     phases are queue, spec, implement, gate (GATING and TRIAGING), review, close, parked,
     waiting and done.
   - Transitions are not lines. They become the group's `states` path
     (`GATING → TRIAGING → GATING → REVIEWING`) and its `transitions` list.
3. **Collapsing.** One line each for:
   - **A persona run:** its `run.start`, `run.end` and `cost`, the `finding`s it produced (by
     `run_id`, else the last ended run of that persona), its `triage` event, its `commit`, and
     `spec_written` / `spec_review` for spec runs.
   - **A review fan-out:** the reviewer runs started together in REVIEWING, plus
     `review_outcome` (or, on older campaigns, the "review score N/M verdict" note).
   - **A gate:** the gate itself, with the `gate.reuse_declined`, `gate.reused`,
     `gate.skipped`, `gate_waiting` and `worker_*` events before it, and any `flaky_recorded`
     after it, as children.
   - **A rebase:** the `finding gate=rebase`, the `conflict_resolver` run and `rebase_resolved`.

   Parks, resumes, escalations (`park` with an `*_escalated` reason, `escalated_from`, or
   `human_decision`), human answers, cost-cap raises, close, notes and conductor decisions
   each get one line. A conductor decision folds into the next park, resume or cap line in its
   group. Findings are counted by severity in the line text and listed as expandable children.
   A waived finding says "waived by triager" and gives the reason.
4. **Surfaces.**
   - **TUI.** The story `Tree` is the default view. `r` toggles the raw SPEC-0246 `DataTable`,
     unchanged. `f` cycles the same SPEC-0246 filter groups in both views; in the story, each
     line has a `category` in those groups. `g`, `G`, `j`, `k`, `v` and the action keys are
     unchanged. The detail pane shows the selected line, its children and its raw events, and
     the run or gate detail for its main event.
   - **Dashboard.** The payload adds `story` (the whole ledger) and `raw`
     (`{seq: campaign_event text}`). The old `timeline` (last 40 events) stays for
     compatibility.
   - **CLI.** `nw campaign show` prints the story by default. `--raw` prints the old last-5
     view, and `--json` adds `story` to the unchanged `nw.campaign.v1` document.

## Design

### `nw.campaign.story.v1`

```
{schema, campaign, events: int, lines: int, unknown_types: {type: count},
 header: {state, loops, cost_usd, cap_usd, first_ts, last_ts},
 loops: [{id: "loop<N>", loop: N, title: "Loop N+1", header, start, end, wall_s,
          cost_from, cost_to, cap_usd, phases: [phase…],
          groups: [{id: "g<first seq>", phase, loop, title, header, states: [STATE…],
                    path: "A → B → C", transitions: [{seq, from, to, reason, by, ts}],
                    start, end, wall_s, cost_from, cost_to, cap_usd, seqs: [int…],
                    lines: [{id: "l<first seq>", kind, category, text, tone, quiet,
                             seqs: [int…], ref_seq, children: [{text, tone, seqs,
                             severity?, waived?, persona?, key?, run_id?}], …}]}]}]}
```

The fields:
- **`kind`** is one of `run`, `review_round`, `gate`, `rebase`, `park`, `escalation`,
  `resume`, `answer`, `cost_raise`, `decision`, `close`, `open`, `admit`, `spec_accepted`,
  `note`, `finding`, `followups`, `backlog`, `bookkeeping`, `priority`, `divergence`, `commit`,
  `cost`, `gate_note`, `flaky` or `event` (the unknown-type fallback).
- **`category`** is one of SPEC-0246's filter groups (`transitions`, `runs`, `gates`,
  `findings`, `notes`).
- **`tone`** is `ok`, `bad`, `warn`, `info` or empty.
- **`quiet`** lines are routine bookkeeping. Examples: backlog, idea links, priority, a cost
  event with no run, and gate noise with no gate after it. They are hidden in a collapsed
  view and shown when expanded.
- **`ref_seq`** is the line's main event (the `run.end` of a run, the `gate` of a gate), so a
  UI can show the existing run or gate detail.

Group header: `<title> · <path> · <wall> · $a → $b of $cap`. Loop header:
`Loop N · <wall> · $a → $b of $cap`. Line formats:
- `Gate triager (sonnet, 9s, $0.13): retry — suspected flaky (medium)`
- `Review round — 3 reviewers (3m 10s, $3.18): ✓ conformance 90 ✓ tests 90 ✓ adversarial 84 →
  pass 92/90 · 6 findings (6 minor; 4 waived by triager)`
- `Re-gate #29 on box — FAILED after 40m 31s (pytest)` (the label is `Re-gate` in CLOSING and
  `Gate` otherwise), and `Gate #30 on box — passed (13m 30s)`
- `Rebase onto main (318652ba) conflicted (tests/test_remote_gates.py) → resolved by
  conflict_resolver (opus, 2m 5s, $1.02)`
- `Implementer (opus, 13m 37s, $1.00): done — <summary> · 1 finding (1 major)`
- `Cost cap raised $75.00 → $90.00 (score rose 72→88) by cost_cap_policy`

`timeline.text_lines(doc, *, expand=False)` returns `(indent, text, tone)` rows for terminals.
Collapsed, it shows loop and group headers and the non-quiet lines; a `[+N]` suffix marks
hidden children, and `(N quiet details)` counts the hidden quiet lines.

### TUI (`tui/campaign_screen.py`, `tui/agentic_model.py`)

- **Model.** `CampaignDetail` gains `story` (the doc, built with `now`) and `events_by_seq`.
  `agentic_model.story_detail(cfg, cid, detail, node)` renders the detail pane for a story
  node. It shows the text, the children, the run or gate detail of `ref_seq` (through the
  existing `entry_detail`), and then `raw events` as `campaign_event` lines.
- **Screen.**
  - The default view is a `Tree` (`#campaign-story`). Its nodes are loop → group → line →
    child, and each node's data is the story dict.
  - The newest loop and its newest group start expanded.
  - Enter or space toggles the node under the cursor.
  - `r` swaps the tree and the raw `DataTable` (`display`), and the status line says which view
    is shown.
  - `f` filters both views. In the story, a group with no matching line is hidden.
  - `g`, `G`, `j` and `k` move the cursor of the visible view.
  - On a reload, the expanded node ids and the cursor id are kept.

### Dashboard (`dashboard/campaign.py`, `static/app.js`, `static/app.css`)

- **Payload.** `build()` adds two sections, each failing alone like the others:
  - `story`: `timeline.story` over the full ledger, with `now`;
  - `raw`: a `{seq: clean(campaign_event text)}` map.
- **Rendering.** `#drill-timeline` renders the story:
  - Loops are listed newest first, and each group is a full-width toggle button showing its
    header.
  - The newest group starts open, and open state is kept across refreshes in `dv.open`.
  - Tapping a line opens its children and its raw rows (monospace, dim).
  - A `raw` / `story` toggle button above the list switches `#drill-timeline` to every event's
    raw text, newest first.
  - When `story` is missing or an error, the page falls back to the raw list.
  - All text goes through `textContent`, as SPEC-0413 requires.

### CLI (`cli_campaign.py`)

`nw campaign show CID [--raw] [--expand/-x] [--json]`:
- The header lines are unchanged.
- The story is printed through `text_lines`, coloured by tone.
- `--raw` prints the previous last-5 `campaign_event` lines instead of the story.
- `--json` emits the unchanged `nw.campaign.v1` (`last_events` included) plus `story`.

## Alternatives considered

- **Wait for SPEC-0406's `history.py`.** It is not on `main`, and its schema is about phase
  time and records, not readable lines. The story is the presentation layer that history's
  Timeline tab needs anyway, so building it first and having history import it avoids two
  groupers.
- **Render HTML or Rich in the formatter.** That ties it to one UI. Plain dicts and plain text
  serve all three surfaces and JSON.
- **Separate rows for transitions.** That is what made the old view noisy. The state path in
  the group header carries the same information.
- **A lazy raw endpoint for the dashboard.** It would need a `server.py` route, and a parallel
  branch is changing `server.py`. The raw map costs about 200 KB for a 750-event campaign,
  which is acceptable for an on-demand drilldown.

## Acceptance criteria

- [x] `timeline.py` imports only the standard library. `story()` returns a JSON-serialisable
      `nw.campaign.story.v1`. Over every real campaign in the data dir, it raises nothing, puts
      every event seq in exactly one group, and covers every non-transition seq in some line.
- [x] Synthetic ledgers produce exactly these lines:
      - `Gate triager (sonnet, 9s, $0.13): retry — suspected flaky (medium)`;
      - a 3-reviewer review round with `✓ conformance 90 ✓ tests 90 ✓ adversarial 84 → pass
        92/90`;
      - `Re-gate #N on box — FAILED after 40m 31s (pytest)`;
      - `Rebase onto main (…) conflicted (tests/x.py) → resolved by conflict_resolver (opus,
        2m 5s, $1.02)`;
      - findings counted by severity, with waived ones marked "waived by triager";
      - transitions only in group paths;
      - `gate.reuse_declined` as a gate child;
      - an unknown event type as a short line.
- [x] Loops split on `triage_fix` and `review_fix`, and `timeline.FIX_REASONS ==
      campaign_export.FIX_REASONS`.
- [x] TUI: the screen opens on the story tree, and Enter or space expands a node. `r` shows the
      raw table and `r` again the story. `f`, `g`, `G`, `j`, `k`, `v`, `p`, `u`, `P`, `A`,
      Escape and `q` keep working. The detail pane shows a line's raw events.
- [x] Dashboard: `GET /api/campaigns/<cid>` carries `story` and `raw`, and `timeline` is
      unchanged. The page renders collapsible groups, tapping expands, and the raw toggle shows
      every raw event. It renders at 390×844 in headless Chromium against a real
      `nw serve --dashboard`.
- [x] CLI: `nw campaign show` prints the story, `--raw` prints the previous output, and
      `--json` carries both `last_events` and `story`.
- [x] Real-data verification of `SPEC-0345-20261101-e596` plus at least three other shapes
      (merged, cost-raised, idea campaign with spec writing) is recorded below with
      before/after excerpts.

## Test plan

- **Automated tests:**
  - `tests/test_campaign_timeline.py`. Synthetic ledgers for each line shape in the acceptance
    criteria, plus grouping, loops, cost deltas, waivers, unknown types, malformed events,
    seq coverage, the stdlib-only import check and the `FIX_REASONS` parity. It also runs a
    sweep over a copied real campaign ledger when the live data dir exists; otherwise that
    test is skipped.
  - `tests/test_tui_campaign_screen.py` is updated: the existing table tests press `r` first.
    New Pilot tests cover the default story tree, expand and collapse, the `r` toggle, `f` in
    story mode, the detail pane's raw events, and a text snapshot.
  - `tests/test_dashboard_campaign.py` covers `story` and `raw` in the payload.
  - `tests/js/answer.test.js` covers story rendering, tap to expand, the raw toggle and the
    fallback when `story` is an error.
  - `tests/test_agentic_campaign_cli.py` covers story by default, `--raw` and `--json`.
- **Manual verification (real processes, read-only data):**
  1. Render `SPEC-0345-20261101-e596` and three other shapes from the live data dir with
     `nw campaign show`, and record before and after excerpts.
  2. Drive the real TUI with a Textual Pilot over a copied fixture of those campaigns, and
     record text snapshots.
  3. Run a real `nw serve --dashboard` on a free loopback port over a scratch copy of the data
     dir. Fetch `/api/campaigns/<cid>`, then screenshot and inspect the phone view at 390×844
     with headless Chromium (`tests/js/browser_drive.js`).
- **Regression guard:** affected tests (`tools/affected_tests.py`). `nw campaign log` is
  untouched, and the queue's close gate runs the full suite.

## As shipped

**Shipped as designed.** The only additions and decisions taken while building:
- **Rebase findings by title.** A `finding gate=rebase` "generated files regenerated during
  rebase" (131 of them in the live data) is a clean line, `Rebase onto main: regenerated
  generated files (docs/LEDGER.md)`. "conflict resolver did not resolve" and "resolver dropped
  campaign commits" fold into the current rebase line as its failure, for example `… →
  conflict_resolver (sonnet, 37s, $0.41): conflict resolver did not resolve`.
- **The first cost total backfills the starting cost.** A ledger's first `cost` event names
  what was spent before it (`total_usd - delta_usd`), so the groups before it do not claim $0.
- **Loop numbers are cumulative.** `Loop N` counts every fix loop since the campaign opened,
  including those after a conductor's `loops_reset`. `SPEC-0345-20261101-e596` therefore shows
  Loop 15 while `state.counters.loops` says 2. The story is a timeline, and resetting would
  give two loops the same number.
- **The TUI cursor follows the end**, as the SPEC-0246 table does. On first paint it sits on the
  newest line. When it is on the last line, a ledger append moves it to the new last line;
  anywhere else it stays put. Open nodes survive reloads.
- **The open defaults.** The TUI and the dashboard open the newest loop, its newest group, and
  the newest group that has lines; a group just entered is often still empty. On the
  dashboard, loops and groups are listed newest first and lines inside a group oldest first.
  Quiet lines sit under one `details (N)` node per group.
- **Payload size.** The drilldown of the 753-event, 15-loop `SPEC-0345-20261101-e596` is
  413 KB (story 185 KB, raw 208 KB). It is fetched only while a drilldown is open, and that is
  accepted for now. A lazy raw endpoint needs a `server.py` route (see Alternatives).
- **Unchanged.** `nw campaign log` is untouched, and so is the drilldown's `timeline` field. The
  SPEC-0246 tests run the raw table through a `raw=True` helper that presses `r`.

**Files:**
- `src/nightwright/agentic/timeline.py` (new);
- `src/nightwright/agentic/cli_campaign.py` (`show --raw/--expand/--json`);
- `src/nightwright/tui/agentic_model.py` (`CampaignDetail.story` / `events_by_seq`,
  `story_detail`);
- `src/nightwright/tui/campaign_screen.py` (the `Tree`, `r`);
- `src/nightwright/agentic/dashboard/campaign.py` (`story` and `raw`);
- `static/app.js` and `static/app.css` (`renderTimeline`).

**Tests:**
- `tests/test_campaign_timeline.py` (new, 11 tests, including the sweep over a copied real
  ledger);
- `tests/test_tui_campaign_screen.py` (+3 Pilot tests, the raw helper);
- `tests/test_dashboard_campaign.py` (+1, story and raw assertions);
- `tests/js/answer.test.js` (+4; the pin in `test_dashboard_answer_page.py` goes 7 → 11);
- `tests/test_agentic_campaign_cli.py` (+1).

**Affected tests.** `tools/affected_tests.py --run uv run pytest -q -n 2` ran 148 of 302 files:
3722 passed, 8 skipped and 33 failed. All 33 failures are in `tests/test_dashboard_tailnet_cli.py`,
which this change does not touch. Each one asserts `not listening(8791)`, and port 8791 is held by
the human's live dashboard service (`nw`, pid 2608335, on 127.0.0.1 and the tailnet address). The
cause is the environment, not this change; on a gate host the port is free. Every test this
change touched passed: timeline 11, TUI campaign screen and model 31, dashboard campaign and
answer page 10, campaign CLI 12, and JS `answer.test.js` 11.

### Real-data verification (read-only; copies under a scratch `NW_DATA_DIR`)

**Sweep.** `timeline.story` ran over all 233 campaigns in the live data dir (27 821 events),
which became 5 736 lines. Nothing raised, there were no unknown types, every seq fell in
exactly one group, and every non-transition seq was in some line.

**Before** (`SPEC-0345-20261101-e596`, the pasted campaign, seqs 709–722 as the timeline
showed them):

```
 709  …15:12:58  finding      tick  persona=close severity=info gate=rebase title=rebase conflict; resolver dispatched detail=rebase onto main (318652ba43) conflicts: tests/test_remot...
 710  …15:13:00  run.start    tick  persona=conflict_resolver run_id=091-conflict_resolver model=opus effort=high max_budget_usd=4.0 timeout_s=2700 permission_mode=bypassPermissions …
 711  …15:15:06  run.end      tick  persona=conflict_resolver run_id=091-conflict_resolver exit_code=0 is_error=False timed_out=False interrupted=False duration_s=124.94 cost_usd=1.0228…
 712  …15:15:07  cost         tick  persona=conflict_resolver delta_usd=1.0228340000000002 total_usd=77.5211 cap_usd=90.0 estimated=False
 713  …15:15:07  rebase_resolved tick  rebase resolved: 1 paths (both×1)
 714  …15:26:12  gate.reuse_declined tick  gate=pytest rule=R2 detail=the full 'pytest' close run was red at 48913835c2; reuse ...
 715  …15:26:12  gate         tick  gate_seq=28 ok=True started_at=… duration_s=658.113 slot_wait_s=0.0 worker=box head=f8af7e5a… tree=a4dd7c2c…
 716  …15:26:12  finding      tick  persona=close severity=info gate=base title=base moved during re-gate; redoing close detail= files=[] run_id=None key=9c4dbbab…
 717  …16:06:51  gate.reuse_declined tick  gate=pytest rule=R3 detail=branch files differ from the gated tree: docs/LEDGER.md, ...
 718  …16:06:51  gate         tick  gate_seq=29 ok=False started_at=… duration_s=2430.727 slot_wait_s=0.0 worker=box head=3d3f7198… tree=72a30d44…
 719  …16:06:51  transition   tick  from=CLOSING to=TRIAGING reason=regate_failed
 720  …16:06:53  run.start    tick  persona=gate_triager run_id=092-gate_triager model=sonnet effort=high max_budget_usd=1.0 …
 721  …16:07:02  run.end      tick  persona=gate_triager run_id=092-gate_triager exit_code=0 is_error=False … duration_s=8.662 cost_usd=0.1298214 … usage={'input_tokens': 2, …
 722  …16:07:02  cost         tick  persona=gate_triager delta_usd=0.1298214 total_usd=77.6509 cap_usd=90.0 estimated=False
```

**After** (`nw campaign show SPEC-0345-20261101-e596`, the same span plus its neighbours;
753 events → 144 lines):

```
Loop 15 · 2h 4m · $70.97 → $80.61 of $90.00
  Reviewing · REVIEWING → CLOSING · 3m 27s · $73.19 → $76.50 of $90.00
    Cost cap raised $75.00 → $90.00 (score rose 72→88) by cost_cap_policy
    Review round — 3 reviewers (3m 10s, $3.18): ✓ conformance 90 ✓ tests 90 ✓ adversarial 84 → pass 92/90 · 6 findings (6 minor; 4 waived by triager)  [+9]
    Review triager (sonnet, 11s, $0.12): waived 4 findings
  Closing (rebase, re-gate, merge) · CLOSING → TRIAGING · 53m 56s · $76.50 → $77.52 of $90.00
    Rebase onto main (318652ba) conflicted (tests/test_remote_gates.py) → resolved by conflict_resolver (opus, 2m 5s, $1.02)  [+2]
    Re-gate #28 on box — passed (10m 58s)  [+4]
    Base: base moved during re-gate; redoing close
    Re-gate #29 on box — FAILED after 40m 31s (pytest)  [+4]
  Gating & triage · TRIAGING → GATING → REVIEWING · 13m 45s · $77.52 → $77.65 of $90.00
    Gate triager (sonnet, 9s, $0.13): retry — suspected flaky (medium)
    Gate #30 on box — passed (13m 30s)  [+2]
  Reviewing · REVIEWING → CLOSING · 1m 59s · $77.65 → $80.61 of $90.00
    Review round — 3 reviewers (1m 47s, $2.85): ✓ conformance 92 ✓ tests 88 ✓ adversarial 82 → pass 96/90 · 6 findings (4 minor, 2 nit; 5 waived by triager)  [+9]
    Review triager (sonnet, 6s, $0.11): waived 5 findings
```

`-x` expands each line. For example, `Re-gate #29` expands to `✓ spec-lint (8s)`,
`✓ docs-tests (22s)`, `✗ pytest (40m, exit 143)` and `pytest: reuse declined (R3: branch files
differ from the gated tree: …)`. Each waived finding reads `minor · tests: Wall-clock windows
in detector and hold tests (tests/test_remote_persona_limits.py) — waived by triager: The
tests rely on a 600 s window …`.

**Other shapes** (after; the before was the same one-row-per-event form):
- **An idea campaign with spec writing, merged** (`IDEA-038-20261030-e2ce`, 79 events → 18
  lines). Before, seqs 8–22 were 15 rows (commit, spec_written, transition, run.start, run.end,
  cost, five findings, spec_review, transition, run.start, run.end). After, they are three
  lines:
  ```
  Spec writing & review · SPEC_WRITING → SPEC_REVIEW → SPEC_WRITING → SPEC_REVIEW → IMPLEMENTING · 6m 45s · $0.00 → $1.26 of $15.00
    Spec writer (sonnet, 1m 30s, $0.43): written — SPEC-0297 Outbound projects end to end: a merged campaign pulls the portable spec's status back  [+1]
    Spec reviewer (opus, 1m 23s, $0.62): revise — round 1 (1 major, 3 minor, 1 nit) · 5 findings (1 major, 3 minor, 1 nit)  [+5]
    Spec writer (sonnet, 19s, $0.22): written — SPEC-0297 …  [+2]
    Spec SPEC-0297 accepted (round 1)
  Closing (rebase, re-gate, merge) · CLOSING → MERGED · 15m 34s · $3.84 of $15.00
    Follow-ups: 5 findings filed as IDEA-407  [+5]
    Rebase onto main: regenerated generated files (docs/LEDGER.md)
    Re-gate #2 — passed (14m 37s)  [+2]
    Merged a0abb8ef onto 2442aafc (gate #2)
  ```
- **A cost-raised campaign with an escalation answer** (`SPEC-0335-20261101-02a3`, 675
  events). Before: `605 … cap_raised queue from=75.0 to=90.0 ceiling=90.0 base_cap=30.0
  spent=73.4126 why=score rose 52→80; raise $75.00 → $90.00 (cei…`. After:
  ```
  Loop 17 · 1h 39m · $73.41 → $79.03 of $90.00
    Implementing · IMPLEMENTING → GATING · 6m 38s · $73.41 → $74.61 of $90.00
      Cost cap raised $75.00 → $90.00 (score rose 52→80) by cost_cap_policy
      Implementer (opus, 6m 35s, $1.20): partial — SPEC-0335 gate triage: test-only load hardening. … · 1 finding (1 major)  [+2]
      Queue decision resume (cost_cap_policy): score rose 52→80; raise $75.00 → $90.00 (ceiling $90.00, spent $73.41)
  …
    Parked · PARKED → IMPLEMENTING · 1h 9m · $78.43 of $90.00
      Parked by conductor: human_decision (from triage_escalated) — ESC-0029: SPEC-0335 passed review (91) … (resume to GATING)  [+2]
      Human answered ESC-0029 (choose): Defer to IDEA-972, mark done with honest deferral note (doc-only change)
      Resumed by conductor → IMPLEMENTING, loops reset: ESC-0029 answered: defer manual two-machine verification to IDEA-972. …  [+2]
  ```
- **A merge-request campaign** (`MERGE-20261026-4266`, 12 events → 7 lines):
  `Queued · QUEUED → PARKED`, then `Parked by human: human — conductor: spec review round before
  merge`, `Resumed by human → QUEUED: …`, `Rebase onto main: regenerated generated files
  (docs/LEDGER.md)`, `Re-gate #1 — passed (10m 7s)` and `Merged 2eb7c052 onto 8ba8ed3b (gate
  #1)`.
- **A failed resolver** (`IDEA-714-20261102-cc4e`): `Rebase onto main conflicted
  (docs/LEDGER.md, docs/specs/0369-…) → conflict_resolver (sonnet, 37s, $0.41): conflict resolver
  did not resolve`, then `Parked by tick: rebase_conflict — …`.

**TUI, a real Pilot.** The real `IdeaDesk(cfg, tab="queue", campaign=…)` ran at 170×50 with
`NW_DATA_DIR` set to a scratch copy of the five campaigns above. Text snapshot for
`SPEC-0345-20261101-e596` on open:

```
 ▶ Loop 14 · 36m 9s · $66.78 → $70.97 of $75.00
 ▼ Loop 15 · 2h 17m · $70.97 → $80.61 of $90.00
   ▶ Implementing · IMPLEMENTING → GATING · 7m 44s · $70.97 → $73.19 of $75.00
   ▶ Gating · GATING → REVIEWING · 44m 5s · $73.19 of $75.00
   ▶ Reviewing · REVIEWING → CLOSING · 3m 27s · $73.19 → $76.50 of $90.00
   ▶ Closing (rebase, re-gate, merge) · CLOSING → TRIAGING · 53m 56s · $76.50 → $77.52 of $90.00
   ▶ Gating & triage · TRIAGING → GATING → REVIEWING · 13m 45s · $77.52 → $77.65 of $90.00
   ▼ Reviewing · REVIEWING → CLOSING · 1m 59s · $77.65 → $80.61 of $90.00
     ▶ Review round — 3 reviewers (1m 47s, $2.85): ✓ conformance 92 ✓ tests 88 ✓ adversarial 82 → pass 96/90 · 6 findings (4 minor, 2 nit; 5 waived by triager)  [+9]
>      Review triager (sonnet, 6s, $0.11): waived 5 findings
   ▼ Closing · CLOSING · 12m 43s · $80.61 of $90.00
```

The status line reads `story · filter all · 144 lines from 753 events · enter/space expands ·
r raw`. The detail pane shows the run detail (persona, model, cost, paths), then
`raw events (9)`: the run's start, end, cost, triage and five waiver notes as
`campaign_event` lines. `r` showed the raw table, `filter all · 753/753 events`, and `r`
switched back. `f f f` (gates) left only the gate groups. The snapshots for `IDEA-038` and
`MERGE-20261026-4266` match the "other shapes" lines above.

**Dashboard, a real server and a real browser.**
- **Server.** `NW_DATA_DIR=<scratch> nw serve --dashboard --bind 127.0.0.1:56691`. The
  scratch dir held the same five copied campaigns.
- **Payload.** `GET /api/campaigns/SPEC-0345-20261101-e596` returned 200 with 413 KB and the
  keys `campaign, current_run, generated_at, raw, running, runs, schema, state, story,
  timeline`. The story had 753 events and 144 lines, `raw` had 753 entries, and `timeline` had
  40.
- **Browser.** Headless Chromium (Playwright `chrome-headless-shell` 1208) at 390×844 (mobile,
  DPR 2) opened `#/c/SPEC-0345-20261101-e596`.
  - **On open.** Loops were listed newest first (`Loop 15 · 2h 25m · $70.97 → $80.61 of
    $90.00`). `▾ Closing · CLOSING …` (an empty live group, showing "nothing yet") and
    `▾ Reviewing · REVIEWING → CLOSING …` were open, with the review round and triager lines;
    older groups were collapsed. There was no horizontal overflow, and every button was at
    least 44 px tall.
  - **Tapping the review round** listed its findings, the waived ones muted with "waived by
    triager: …", and then its raw rows.
  - **The `raw` button** showed all 753 raw rows, newest first (`753 … transition … from=REVIEWING
    to=CLOSING reason=review_pass`), and relabelled itself `story`. Tapping it again restored
    the story with the same nodes open.
  - Screenshots were inspected by eye: the headers wrap cleanly, and the tone bars are green
    for a passing round and red for a failed gate.

## Rollout / migration

There is no data migration: the story is derived on read. The dashboard service picks up the
new `app.js` on restart.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
