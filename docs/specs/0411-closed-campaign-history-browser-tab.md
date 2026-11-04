---
id: SPEC-0411
title: Closed-campaign history browser tab
status: accepted
owner: user
created: 2026-11-03
updated: 2026-11-03
kind: feature
parent: SPEC-0405
source_idea: IDEA-888
tags: [tui, agentic, observability]
---

## Context

Child 6 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md). The
human wants "a better interface that shows the same for PAST, already closed items, not just
what is live in the queue".

**What exists today.**
- `nw tui` (`IdeaDesk`, `src/nightwright/tui/app.py`) has five tabs: ideas, hub, queue,
  mailbox, events. The keys are `1`-`5` (`app.py:425-430`). `_tab_activated`
  (`app.py:1055`) activates and deactivates the per-tab views.
- The queue tab shows live campaigns and the ones delivered in the last 24 h. A campaign
  closed earlier cannot be reached from any TUI tab. The CLI is the only way to find it.
- The events tab (`src/nightwright/tui/events_tab.py`, SPEC-0251) is the pattern to follow
  for a filterable table. Its parser is `agentic_model.parse_event_filter`
  (`agentic_model.py:568`), which raises `ValueError` naming the bad token. The tab is
  read-only, has a status line and a filter bar, polls only while active, and loads in a
  worker.

**What the epic provides.** The epic's shared layer, `agentic/history.py` (child 2), serves
`nw.history.index.v1`: one summary row per campaign, with these fields:
- the header fields (`closed_at`, `outcome`, `project`, `spec_id`, `title`, `profile`,
  `size`, `loops`, `runs`, `cost_usd`, `wall_s`, `active_s`, `followup_idea`);
- `dominant`;
- the finding counts by severity;
- the record count;
- the `signature`.

It also provides a disposable cache under `cache_dir/history/v1`, with a cold build and a warm
load, and `nw campaign history --list`. Child 5 provides the campaign detail popup, a
`ModalScreen` over `nw.campaign.history.v1`. Child 3 provides the real-app test harness and
the sanitized fixture.

This spec is the History tab: a browser over the index. It computes nothing new, writes
nothing and shows one row per campaign. Everything deeper is one Enter away, in the popup.

## Goals / Non-goals

**Goals**
- A new desk tab, `6 History`, listing closed campaigns (merged, handed off, abandoned)
  newest-closed first, with the fourteen columns listed in the Design section.
- `a` toggles live campaigns into the list.
- A filter bar in the events-tab style: `project: spec: state: since: until: cost>N dominant:`
  plus free text. A bad token is named and the previous filter stays in effect.
- Sort cycling over the useful columns.
- A cold index build runs in a worker with a visible progress line. A warm load comes from the
  cache and shows rows quickly. The event-loop thread does no file I/O (SPEC-0129).
- Enter, and double-click (`Click.chain == 2`), open the child-5 popup for the row. Esc
  returns to the same row.
- Strictly read-only, and fully keyboard-drivable.

**Non-goals**
- Any new mutation path, including a "rebuild cache" key. `nw campaign history --rebuild-cache`
  stays a CLI command.
- Per-campaign detail: timeline, phases, runs and findings live in the popup (child 5).
- Cross-campaign findings and decisions (the Findings tab, child 7).
- Computing history in the tab. The tab calls `history.py` only, and never reads ledgers,
  `state.json` or runs itself.
- Changing the index schema. If a column needs a field the index lacks, the column shows `-`,
  and the missing field is a follow-up for `history.py`. It is not computed here.

## Decision

1. **A new `HistoryView` widget in `src/nightwright/tui/history_tab.py`**, mounted as
   `TabPane("history", id="tab-history")` with key `6`. It is modelled on `EventsView`: a
   `DataTable` subclass that owns its bindings, a one-line filter `Input`, a one-line status
   line, and `activate()` / `deactivate()` hooks that `_tab_activated` calls. The other tabs'
   hooks deactivate it, the same way the events and mailbox views are handled.
2. **Row shaping, filtering and sorting are pure functions in `agentic_model.py`**
   (`HistoryRow`, `HistoryFilter`, `parse_history_filter`, `format_history_filter`,
   `filter_history_rows`, `sort_history_rows`, `history_columns`). They are unit-tested
   without Textual. The widget only paints.
3. **The tab reads only `nw.history.index.v1` rows, through `history.py`.** It never opens a
   campaign directory.
4. **Closed means `outcome` is `merged`, `handed_off` or `abandoned`.** Live campaigns have
   `outcome: null` and are hidden unless `a` is on or the filter says `state:live` or
   `state:all`.
5. **Row identity is the campaign id.** Reloads, re-sorts and re-filters keep the cursor on
   the same campaign when it is still visible.
6. **Opening a campaign goes through the one opener the Queue tab uses for the child-5
   popup.** History adds no popup of its own. Opening is by Enter and by `chain == 2` click
   only. `DataTable.RowSelected` is consumed and ignored, so one gesture pushes exactly one
   popup.
7. **Read-only.** The tab binds no key that mutates anything. The only disk writes are the
   history layer's own cache writes under `cache_dir`.

## Design

### Columns

In this order, with the index field each one reads:

| Column | Source | Rendering |
|---|---|---|
| closed | `closed_at` | `YYYY-MM-DD HH:MM` in local time. `live` for a live row. |
| outcome | `outcome` | `merged`, `handed_off` or `abandoned`. A live row shows its `state`. |
| project | `project` | |
| spec and title | `spec_id` + `title` | `SPEC-0123 title…`. The flexible column. For an id-less campaign, the campaign id + title. |
| profile | `profile` | |
| size | `size` | |
| loops | `loops` | `3` or `3/5` when `loop_max` is known. |
| runs | `runs` | |
| cost | `cost_usd` | `$12.34`. |
| wall | `wall_s` | Compact duration: `3d 4h`, `5h 12m`, `42s`. |
| active | `active_s` | Same formatter. |
| dominant | `dominant.wall` | The wall-time winner. When the winner is `queue`, `parked` or `waiting` and `dominant.working` exists, it reads `parked (gate)`. |
| must_fix / major | severity counts | `m/M`, e.g. `2/5`. The counts for severities named `must_fix` and `major`. A missing count is 0. |
| follow-ups | `followup_idea` | The follow-up idea id, or `-`. |

Rules for rendering:
- A missing or malformed field renders `-`. One bad row never aborts the paint (invariant 4
  of the epic).
- All cell text is built as `rich.text.Text`. It is never parsed as Rich markup, because
  titles and project names are untrusted data (SPEC-0265, epic invariant 3).
- `history_columns(width)` returns the columns to show for the table width. When the width is
  too small, it drops columns in this order: follow-ups, active, size, profile, runs. The
  closed, outcome, spec and title, cost and dominant columns are never dropped. The title
  column absorbs the rest.

### Default view, `a`, and sort

- **Default row set:** terminal campaigns only.
- **`a`** toggles live campaigns into the list. The status line shows `live shown` while on.
- **Default sort:** `closed_at` descending, i.e. newest-closed first. Live rows have no
  `closed_at` and sort as the newest, so they come first when shown.
- **`o`** cycles the sort key: closed -> cost -> wall -> active -> loops -> runs -> spec ->
  closed. **`O`** reverses the direction. The status line shows the key and an arrow.
- **Ties** break on campaign id, so the order is total and deterministic. Rows are never
  re-sorted for a reason other than the sort key, the filter or a changed row set.

### Filter bar

`f` or `/` opens the one-line filter input. Enter applies it, Esc closes it, and `x` clears
the filter. The grammar is `parse_history_filter(text) -> HistoryFilter`, in the style of
`parse_event_filter` but with `key:value` tokens. Tokens are split with `shlex`, so
`"two words"` works. An unbalanced quote is a `ValueError`.

| Token | Meaning |
|---|---|
| `project:P` | Exact project, case-insensitive. |
| `spec:S` | Spec id prefix, case-insensitive: `spec:SPEC-04` matches `SPEC-0411`. It also matches the campaign id prefix. |
| `state:S` | One of `merged`, `handed_off`, `abandoned`, `live`, `all`: the same words as `nw campaign history --list --state`. |
| `since:D` | `closed_at` >= D. The campaign's `created_at` is used for a live row. |
| `until:D` | `closed_at` <= D, end of day inclusive. |
| `cost>N` | `cost_usd` > N. |
| `dominant:X` | The wall-time winner, or the working phase, equals phase X (a name in `history.PHASES`). |
| *anything else* | Free text: a case-insensitive substring over campaign id, spec id, title and project. All free-text tokens must match (AND). |

Semantics:
- Different keys combine with AND. A repeated key replaces the earlier one (last wins).
- An explicit `state:` overrides the `a` toggle. Without `state:`, the toggle decides.
- `since:` and `until:` use the same date parser as `nw campaign history --list --since/--until`
  (one function, imported from `history.py`), so the tab and the CLI agree on every date.
- An unknown key (`foo:bar`), a bad value (`cost>abc`, `since:xyz`, `state:nope`,
  `dominant:nope`) or an empty value raises `ValueError` naming the token. The filter input
  shows the message in the status line and **keeps the previously applied filter**.
- The active filter is shown in the status line, rendered by `format_history_filter`. It
  round-trips: `parse_history_filter(format_history_filter(f)) == f`.

### Status line and empty states

The status line reads, e.g.:
`42 of 191 campaigns · $318.40 · sorted closed ↓ · filter: project:Meta-Tools state:merged`

- It reports the shown count, the total count and the summed cost of the shown rows.
- During a cold build, it reads `building history index 37/217…`. It is replaced by the
  counts when the build ends.
- If the index cannot be built (history layer error), it shows the error text as plain text
  and keeps the last good rows.
- With no terminal campaigns: `no closed campaigns yet (a: include live)`.
- With a filter that matches nothing: `no campaign matches the filter (x: clear)`.

### Loading

- **One load path, in a thread worker.** The worker calls the history layer's index reader.
  On a cold cache, the reader builds the index and calls a progress callback `(done, total)`.
  The worker forwards it to the event loop with `call_from_thread`, and the status line
  updates. On a warm cache, the reader returns the cached rows after stat-ing the live
  campaigns and the global logs.
- **No file I/O on the event-loop thread.** Filtering and sorting of an already-loaded list
  are pure and in-memory, so they run on the event loop. They must be fast enough for the
  budget below.
- **Stale results are dropped.** Each load carries a generation number. A result for an
  older generation never repaints.
- **Rows paint as soon as they exist.** The first load shows an empty table plus the
  progress line. It never blocks the keyboard: the other tabs stay usable during a cold
  build.
- **Refresh.** `activate()` starts a load (a warm load is cheap). While the tab is active, a
  15 s timer refreshes through the same path. The timer is paused in `deactivate()`.
  Closed campaigns are immutable, so a refresh only re-reads live and newly closed ones; that
  incremental work is the history layer's, not the tab's. `r` forces a refresh.
- **Cursor stability.** The painter remembers the cursor's campaign id and scroll offset.
  After a repaint it restores both. If the campaign has left the view, the cursor goes to the
  nearest remaining row index. A refresh that changes no row leaves the table byte-for-byte
  unchanged (no repaint at all).

### Opening a campaign

- **Enter** (an explicit binding on the table subclass) and a **double-click** (an `on_click`
  with `event.chain == 2` on a data row) call `app.open_campaign_detail(campaign_id)`. This
  is the opener child 5 exposes for the Queue tab, and History reuses it unchanged. Clicking
  the header row, or an empty area, does nothing.
- A single click only moves the cursor. `RowSelected` is `stop()`ped without action.
- Esc or q in the popup closes it and focus returns to the History table, on the same row
  (child 5 guarantees the return; this spec tests it for History).
- For a live row, the popup behaves as it does from the Queue tab (it polls the live
  campaign). For a terminal one, it never polls.

### Keys

On the table: `j`/`k`/arrows move, `Enter` opens, `f` or `/` filter, `x` clear filter, `o`
sort, `O` reverse, `a` live on/off, `r` refresh. The table subclass owns these, so they
shadow the desk's same-letter bindings, the way `EventsTable` does. `6` selects the tab
from anywhere. Every function has a key, so the mouse is never required.

### Files

- `src/nightwright/tui/history_tab.py` (new): `HistoryTable`, `HistoryFilterInput`,
  `HistoryView`.
- `src/nightwright/tui/agentic_model.py`: the pure helpers named in Decision 2.
- `src/nightwright/tui/app.py`: the `TabPane`, the `6` binding, and a history branch in
  `_tab_activated`, so entering any other tab deactivates History and entering History
  deactivates the others.
- `tests/test_tui_history_model.py`, `tests/test_tui_history_tab.py` (new).

### Dependencies

- **Prerequisites.** Child 2 (`history.py`, the index reader with a progress hook, the
  fixture), child 3 (`tests/tui_harness.py`, the snapshot helper, the scale-clone helper, the
  no-skip guard test) and child 5 (the popup and its opener) land first. The implementing
  campaign starts only after all three are `done`.
- **Front matter.** At the time of writing none of the three has a spec id (the epic's
  breakdown lists them, but only this child is promoted), so `depends_on` cannot name them
  yet and the linter would reject invented ids. **Gate:** this spec is not moved to
  `accepted` until the child-2, child-3 and child-5 specs exist and
  `depends_on: [<child 2 id>, <child 3 id>, <child 5 id>]` is added to the front matter in
  the same edit. The source idea IDEA-888 already records "depends_on child 2 and child 5"
  for the queue, and child 3 is added to it at that point.
- **Symbols this spec takes from them.** The names below are the contract this spec codes
  against. All reach the tab through one adapter, the module-level `_history_api` in
  `history_tab.py`, so a rename in the owning spec is a one-line change here. When the child
  specs exist, the gate above includes reconciling this table with their text.

| Symbol | Owner | Used for |
|---|---|---|
| `history.load_index(cfg, *, progress=None) -> list[dict]` | child 2 | the `nw.history.index.v1` rows. On a cold cache it builds and calls `progress(done, total)`. On a warm one it returns the cached rows after stat-ing live campaigns and the global logs. |
| `history.parse_date(text, *, end=False) -> datetime` | child 2 | the one `since:`/`until:` parser, shared with `nw campaign history --list`. Raises `ValueError`. |
| `history.PHASES` | child 2 | the valid `dominant:` values. |
| `history.CLOSED_OUTCOMES = ("merged", "handed_off", "abandoned")` | child 2 | the closed set of Decision 4. |
| `app.open_campaign_detail(campaign_id: str) -> None` | child 5 | pushes the `CampaignDetail` popup. |
| `tests/tui_harness.py` (`start_desk`, `pilot_until`, visible-text extraction, `scale_clone`, `snapshot`) | child 3 | the Pilot, perf and snapshot tests. |
| `tests/fixtures/agentic_history/` | child 2, with the capture tool | real closed, abandoned, handed-off and live campaigns. |

- If child 2's reader lacks a `progress` argument, child 2's spec must add it; this spec
  does not patch `history.py`.
- No `importorskip` and no `needs_textual` guard is added by this spec (epic child 3 forbids
  them).

### Performance

With the 500-campaign clone from the epic's perf helper:
- Filtering and sorting 500 index rows take ≤ 20 ms (asserted with ×3 slack).
- A repaint of 500 rows takes ≤ 100 ms.
- The tab's own warm-load overhead on top of the history layer's index load stays within the
  epic's 300 ms warm-load budget.

## Alternatives considered

- **Reuse the queue tab with a "closed" section.** The queue is a live, state-grouped
  view with stable ordering rules (child 4). History needs sort cycling, free-text filters
  and up to hundreds of rows. Mixing them would break both.
- **Compute rows in the tab from `state.json` and ledgers.** This duplicates the history
  layer, and a cold scan of 217 campaigns on the TUI side is exactly what the shared cache
  prevents.
- **`key=value` filters like the events tab.** The idea fixes `project: spec: …` and `cost>N`;
  the colon form also matches what `nw campaign records` uses for its options. The
  parse/format/round-trip structure is kept.
- **A rebuild-cache key.** It is a write, and the epic bans new write paths from the UI. The
  CLI already has `--rebuild-cache`.
- **Open on `RowSelected`.** A double-click then fires two selections (the first click moves
  the cursor, the second selects), which makes one popup depend on timing. Explicit Enter
  plus `chain == 2` gives one gesture, one popup.

## Acceptance criteria

- [ ] **AC1.** Key `6` selects a `History` tab. `_tab_activated` activates it and
  deactivates the queue, events and mailbox views. Leaving it pauses its timer.
- [ ] **AC2.** By default the tab lists exactly the campaigns of the index whose outcome is
  `merged`, `handed_off` or `abandoned`, newest `closed_at` first, ties broken by campaign
  id. Every fixture campaign of those outcomes appears.
- [ ] **AC3.** The table shows the fourteen columns of the Design table, with the stated
  rendering. A missing or malformed field renders `-` and does not raise. Untrusted text is
  not interpreted as Rich markup.
- [ ] **AC4.** `a` adds live campaigns (shown as `live`, with their state) and removes them
  again. A repeated `a` restores the previous view and cursor campaign.
- [ ] **AC5.** `parse_history_filter` accepts `project: spec: state: since: until: cost>N
  dominant:` and free text. It raises `ValueError` naming the offending token for an unknown
  key, a bad value or an unbalanced quote. `parse_history_filter(format_history_filter(f))
  == f`.
- [ ] **AC6.** Applying a filter shows only matching rows. Applying a bad filter leaves the
  rows and the previous filter unchanged and shows the error in the status line. `x`
  restores the unfiltered list. `since:`/`until:` select the same campaigns as
  `nw campaign history --list --since/--until` with the same dates, on the fixture. A
  filter that matches nothing shows `no campaign matches the filter (x: clear)`.
- [ ] **AC7.** `o` cycles the sort keys in the documented order, `O` reverses, and the status
  line states both. The order is total and deterministic.
- [ ] **AC8.** Loading and performance. On a cold cache, the index build runs off the
  event-loop thread and the status line shows `building history index N/M…` progress; the
  app still responds to keys while it builds. On a warm cache, no build runs and rows appear.
  With no terminal campaigns the status line reads `no closed campaigns yet (a: include
  live)`. When the history layer raises, the error text shows as plain text and the last
  good rows stay. On the 500-campaign clone, filtering and sorting the loaded rows take
  ≤ 20 ms and a repaint ≤ 100 ms (×3 slack in the assertions).
- [ ] **AC9.** Enter, and a double-click on a data row, each open the child-5 popup for that
  row's campaign, exactly once. Esc returns to History with the cursor on the same row. A
  single click only moves the cursor. Every function in the Keys list, including open,
  works without the mouse.
- [ ] **AC10.** A refresh that changes no row neither repaints nor moves the cursor or
  scroll offset. A refresh that changes the row set keeps the cursor on the same campaign
  when it is still visible.
- [ ] **AC11.** The tab is read-only. After a session (open the tab, sort, filter, toggle
  `a`, open and close the popup), a hash of every file in the data dir is identical. The
  tab binds no key that calls `queue_action` or any `apply_*`.

## Test plan

**Automated: pure model.** `tests/test_tui_history_model.py`, no Textual:
- `parse_history_filter` over every token kind, last-wins on repeated keys, quoted
  free text, and each error case (unknown key, `cost>abc`, `since:xyz`, `state:nope`,
  `dominant:nope`, empty value, unbalanced quote), asserting the message names the token.
  Also a format/parse round-trip (AC5).
- `filter_history_rows` on rows built from the fixture index: each token alone and combined,
  the `state:` override over the `a` toggle, a live row's `since:` using `created_at` (AC2,
  AC4, AC6).
- `sort_history_rows`: the default order, every key in the cycle, `O` reversal, and the
  ties-by-id total order (AC2, AC7).
- Row shaping: all fourteen columns for a full row, a row with every optional field missing,
  a row with `[red]markup[/]` in the title and project (the output is a `Text` with the
  literal characters), and the `parked (gate)` dominant rendering (AC3).
- `history_columns(width)`: the drop order at 160, 120 and 80 columns, and that the five
  never-dropped columns survive (AC3).
- Dates: `since`/`until` parsing is the history layer's function (identity check), plus a
  CLI-parity test that runs `nw campaign history --list --since/--until --json` against the
  fixture data dir and compares the campaign ids with `filter_history_rows` (AC6).

**Automated: Pilot, the real app, real data.** `tests/test_tui_history_tab.py`, using
child 3's `tests/tui_harness.py`, with cfg over a copy of `tests/fixtures/agentic_history/`:
1. Press `6`. Assert the tab is active, the rows equal the closed fixture campaigns in
   `closed_at` order (cross-checked against `nw campaign history --list --json`), and the
   column texts for one merged and one abandoned campaign (AC1, AC2, AC3).
2. Press `a`. Assert live rows appear as `live`, then press `a` again and assert the
   original rows and cursor campaign are restored (AC4).
3. Open the filter with `f`, type `state:abandoned project:Meta-Tools`, press Enter. Assert
   only the abandoned campaigns remain and the status line shows the filter. Then type
   `cost>abc` and assert the error text, the unchanged rows and the unchanged filter. Press
   `x` and assert the full list returns (AC6).
4. Press `o` repeatedly and assert the key and order each time, then `O` (AC7).
5. Cold build: start with an empty `cache_dir`, with the index builder wrapped to stall
   between campaigns. Assert (with `pilot_until`) that the status line shows
   `building history index N/M…`, and that pressing `1` moves to the ideas tab during the
   build, which proves the event loop is free. Assert the rows appear when it ends. Then
   restart the app with the warm cache and assert the builder is not called (AC8).
6. Move to a row, press Enter and assert the popup is `CampaignDetail` for that campaign
   id. Press Esc and assert the History table has focus with the same cursor campaign.
   Repeat with `Pilot.double_click` on another row and assert exactly one screen was pushed
   (count `app.screen_stack`). A single `click` only moves the cursor (AC9).
7. Refresh stability: patch the loader to return identical rows, call `r`, and assert the
   table's repaint counter did not change and the cursor and scroll are unchanged. Then add
   one closed campaign to the returned set and assert the cursor campaign is unchanged
   (AC10).
8. Failure states: loader returns no rows -> `no closed campaigns yet`; filter with no match
   -> `no campaign matches the filter`; loader raises -> the error text appears and the last
   good rows stay (AC6, AC8).
9. Keyboard-only run: steps 1, 3, 4, 6 repeated with key presses only (AC9).
10. Hash every file in the data dir before and after steps 1-9, and assert they are equal.
    Assert, by source inspection, that `history_tab.py` references neither `queue_action` nor
    `apply_` (AC11).
11. Pyte snapshot of the History tab on the fixture at 160x48, through the SPEC-0115 path
    and child 3's snapshot helper. It is regenerated only with `NW_UPDATE_SNAPSHOTS=1`.

**Performance.** In `tests/test_history_perf.py`, or a sibling `tests/test_tui_history_perf.py`
that reuses child 3's scale-clone helper: load the 500-campaign index, time
`filter_history_rows` + `sort_history_rows` and a full repaint, and assert the AC8 budgets
with ×3 slack.

**Manual verification** on the real data dir, with the TUI built from this branch:
1. `nw tui`, press `6`. On the first run, watch for the progress line. Note how long the cold
   build takes. Quit and rerun, and rows should appear at once.
2. Find last week's most expensive merged campaign with `cost>5 since:<date>` and `o`
   (sort by cost). Open it with Enter.
3. Double-click an abandoned campaign (`state:abandoned`). Esc, and confirm the cursor is on
   the same row.
4. Repeat 2 and 3 with the keyboard only.
5. Compare the first ten rows against `nw campaign history --list --json | head`.
6. Leave the tab open for 10 minutes while the queue runs, and check that it does not flicker
   and the cursor does not move.

**Regression guard:** `uv run pytest` on `tests/test_tui_*.py` and the dashboard suites
(`python3 tools/affected_tests.py --run uv run pytest -q`); the epic's guard test (child 3)
that no TUI test brings back a Textual skip, which also covers the two new test files of this
spec (no `importorskip`, no skip marker for a missing Textual); `python3 tools/spec_lint.py`.

## Rollout / migration

- No data migration. The tab reads the index cache, which the history layer builds lazily.
- Order: land after child 2 (history layer), child 3 (TUI harness) and child 5 (popup).
  Independent of child 7
  (Findings tab), so the two can land in either order. Whichever lands second reconciles the
  `_tab_activated` branches and the tab-key bindings (`6` here, `7` there).
- On the first use of the tab on a large data dir the cold build takes a few seconds, with a
  progress line. Later loads are warm.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest` on the affected tests);
  manual steps performed and noted in the spec.
- [ ] No regressions: the existing `test_tui_*` suites stay green and no new Textual skip
  guard was added by this spec.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None. Decided while writing:

| Question | Decision | Why |
|---|---|---|
| Where do live rows sort? | First under the default sort, since they have no `closed_at` | The newest activity is the most relevant when the human asked to see live rows |
| Should `state:` override `a`? | Yes | An explicit filter is more specific than a toggle |
| Which `dominant` does the column show? | The wall winner, with the working phase in parentheses when the winner is idle-like | It matches the epic's `dominant` semantics and keeps "it sat parked" and "gates were slow" both visible |
| A rebuild-cache key? | No | A UI write path is out of the epic's scope; the CLI has `--rebuild-cache` |
