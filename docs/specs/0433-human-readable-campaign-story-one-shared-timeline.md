---
id: SPEC-0433
title: "Human-readable campaign story: one shared timeline formatter for the TUI, the dashboard drilldown and nw campaign show"
status: accepted
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

- [ ] `timeline.py` imports only the standard library. `story()` returns a JSON-serialisable
      `nw.campaign.story.v1`. Over every real campaign in the data dir, it raises nothing, puts
      every event seq in exactly one group, and covers every non-transition seq in some line.
- [ ] Synthetic ledgers produce exactly these lines:
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
- [ ] Loops split on `triage_fix` and `review_fix`, and `timeline.FIX_REASONS ==
      campaign_export.FIX_REASONS`.
- [ ] TUI: the screen opens on the story tree, and Enter or space expands a node. `r` shows the
      raw table and `r` again the story. `f`, `g`, `G`, `j`, `k`, `v`, `p`, `u`, `P`, `A`,
      Escape and `q` keep working. The detail pane shows a line's raw events.
- [ ] Dashboard: `GET /api/campaigns/<cid>` carries `story` and `raw`, and `timeline` is
      unchanged. The page renders collapsible groups, tapping expands, and the raw toggle shows
      every raw event. It renders at 390×844 in headless Chromium against a real
      `nw serve --dashboard`.
- [ ] CLI: `nw campaign show` prints the story, `--raw` prints the previous output, and
      `--json` carries both `last_events` and `story`.
- [ ] Real-data verification of `SPEC-0345-20261101-e596` plus at least three other shapes
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

## Rollout / migration

There is no data migration: the story is derived on read. The dashboard service picks up the
new `app.js` on restart.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
