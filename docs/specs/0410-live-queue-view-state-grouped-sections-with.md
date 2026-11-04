---
id: SPEC-0410
title: "Live queue view: state-grouped sections with priority, admission order and why-waiting"
status: proposed
owner: user
created: 2026-11-03
kind: feature
parent: SPEC-0405
source_idea: IDEA-886
tags: [tui, agentic, queue, observability]
supersedes: [SPEC-0241]
---

## Context

Child 4 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md),
goal G1 ("live queue ordering"). It needs the epic's child 3 (the TUI test harness:
`tests/tui_harness.py`, the real-data fixture helpers and the removal of the Textual skip
guards) to land first. That child has no spec id yet, so `depends_on` is added when it does
(see *Rollout*).

**What exists today** ([SPEC-0241](./0241-tui-queue-tab-over-the-queue-dashboard.md), done):
- The queue tab is one `QueueTable(DataTable)` in `src/nightwright/tui/queue_tab.py`, fed by
  `agentic_model.load_queue` over `queue.dashboard_payload`.
- Columns are `section / campaign / spec / what / meta`. The section column is printed on the
  first row of a section only. The sections are needs you, conductor, active, waiting, backlog
  and delivered (`agentic_model.rows_from_payload`).
- `meta` is one free-text cell. For active rows it mixes the state, the running persona and its
  elapsed time, the SPEC-0277 reviewer chips, the loop count and the cost
  (`_active_meta`). For parked rows it mixes park reason, age and detail.
- Active rows come in `queue_payload` order, with no stated priority order, and all of
  CLOSING, REVIEWING, GATING and IMPLEMENTING share one `active` section.
- There is no priority column, no time in state, no cost-vs-cap bar, and no "why is it
  waiting" (although `dashboard_payload` already computes `why` and `waiting_for` per row via
  `journey.why` / `journey.waiting_for`, SPEC-0326).
- The whole table repaints from a `stat()` signature poll every 2 s and a reload every 15 s;
  elapsed times only move on those reloads.
- Enter posts `QueueView.CampaignSelected`, and the desk pushes `CampaignScreen`
  ([SPEC-0246](./0246-tui-campaign-detail-screen.md)). There is no double-click.

`nw queue backlog` (`queue.backlog_view`, ordered by `queue.admission_key`) is the order in
which the runner admits queued work. The TUI does not show that order today.

This spec replaces SPEC-0241's table layout, grouping, row model and sort, and carries its
actions, plumbing and read-only guarantees forward unchanged (restated below). The detail
popup is child 5 of the epic. Until it lands, Enter and double-click open the SPEC-0246 screen.

## Goals / Non-goals

**Goals**
- G1. The queue tab groups rows into the epic's fixed state order, each group under a header
  row with a count that the cursor skips.
- G2. Every non-delivered row shows priority, campaign and spec, title, state plus activity,
  loop `n/max`, cost against its cap as a bar, time in state, and a one-line why.
- G3. Order is stated and stable: sort keys are fixed timestamps, priority and ids only. A row
  moves only when its group changes, and the cursor follows the campaign id.
- G4. Queued rows appear in exactly the runner's admission order (the `nw queue backlog`
  `position`).
- G5. Elapsed and time-in-state cells tick every second from cached timestamps, with no reload
  and no file read.
- G6. Enter and double-click open the campaign detail.
- G7. Park, resume, prioritize and abandon, the SPEC-0277 per-reviewer chips and the
  read-only guarantees of SPEC-0241 keep working as they do.

**Non-goals**
- The detail popup (child 5), the History tab (child 6) and the Findings tab (child 7).
- Any new write path or new action.
- Changing `nw queue watch`, the CLI renderers or the runner's admission logic.
- Filtering or searching the queue (a later spec if needed).
- Persisting the collapsed state of a group across `nw tui` runs.

## Decision

Replace the single `section` column and the free-text `meta` column with a **grouped table**
over a new row model, in the same `QueueView` / `QueueTable`.

1. **Groups, in this fixed order**, each rendered as a non-selectable header row
   `<name> (<count>)`. Empty groups emit no header.

   | # | Group | Rows from `dashboard_payload` |
   |---|---|---|
   | 1 | needs you | `needs_you` |
   | 2 | conductor | `conductor_handling` |
   | 3 | closing | `active` with state CLOSING |
   | 4 | reviewing | `active` with state REVIEWING |
   | 5 | gating | `active` with state GATING or TRIAGING |
   | 6 | building | `active` with state OPENING, SPEC_WRITING, SPEC_REVIEW or IMPLEMENTING |
   | 7 | waiting | `waiting` (state WAITING) |
   | 8 | queued | `backlog` (state QUEUED) |
   | 9 | delivered (24 h) | `delivered`, collapsed by default |

   Furthest-along work comes first, because what is closest to landing is what the human
   acts on. An `active` state not in the table lands in `building`, so no row is dropped.

2. **Sort keys, stable only.**
   - closing / reviewing / gating / building: `(priority, created_at, campaign id)`, which is
     `queue.admission_key`.
   - queued: the backlog `position` (the runner's admission order).
   - needs you / conductor: `(parked_at, campaign id)`, oldest park first. `parked_at` is the
     park's own timestamp (for a deferred park, the request's `at`).
   - waiting: `(priority, waiting.since, campaign id)`.
   - delivered: finished time, newest first, ties by campaign id.

   No key is an age, an elapsed time, a cost or any other value that changes while the row is
   visible. In particular the overdue flag no longer reorders the conductor group (today it
   sorts overdue first); it is shown in the why column instead.

3. **Row identity and the cursor.** A row is keyed `(group, campaign)`, as today: a campaign
   with a deferred park is listed in its park group and in its active group. After every
   repaint the cursor goes to the same `(group, campaign)`, else to the campaign's first row,
   else to the nearest data row at the old index. A reload where no campaign changed group
   leaves every row and the cursor where they were.

4. **Header rows are not selectable.** Cursor movement (`j`/`k`, arrows, page keys, a mouse
   click, a repaint) never rests on a header row: it moves to the next data row in the
   direction of travel, and stays put at the ends. Header rows carry no actions. `z` toggles
   the delivered group between collapsed (header only) and expanded.

5. **Columns** (one `DataTable`, left to right):

   | Column | Content |
   |---|---|
   | `pri` | priority `1`-`5`; blank on delivered rows |
   | `campaign / spec` | spec id (or idea id) over/beside the campaign id without its spec prefix |
   | `title` | spec title, clipped to a measured budget |
   | `state` | state, then the activity (below) |
   | `loop` | `n/max` from the payload's `loops` string; blank when not applicable |
   | `cost` | `$x.xx` plus an 8-cell bar of cost against `cap_usd`; the bar is omitted (money only) when there is no cap |
   | `time` | time in state |
   | `why` | the payload's `why` sentence; for waiting and queued rows, the `waiting_for` text |

   **Activity** (appended to the state):
   - an open run: `<persona> <elapsed>`;
   - gates: the gate name when `gate_hold.progress` or `last_gate` names one, else `gates`;
     `gate slot` waits show `waiting for a gate slot`;
   - waiting: what it waits on (`waiting_for`: campaign slot, gate slot, close train,
     dependency ids);
   - REVIEWING: the SPEC-0277 chips, `glyph + 4-char tag` per reviewer, exactly as
     `agentic_model._active_meta` builds them today.

   **Time in state** is: for active rows the age of the last transition into the current
   state (`state_age_s`); for parked rows the age of the park; for waiting rows the age of
   `waiting.since`; for queued rows the age since creation; blank for delivered rows, which
   show `finished <ago> ago` in the `time` column instead.

6. **Ticking from cached timestamps.** On load, each row stores absolute epoch timestamps
   (`state_since`, `activity_since`, `finished_at`), computed once as `loaded_wall - age_s`
   from the payload's ages. A 1 s timer, active only while the tab is visible, recomputes the
   time-bearing cells from `time.time()` and updates those cells in place. It reads no file,
   calls no payload function and re-sorts nothing.

7. **Open.** Enter and a double-click (`events.Click` with `chain == 2`) on a data row both
   post `QueueView.CampaignSelected(campaign_id, title)`. A click on a header row does
   nothing. The desk keeps its handler, which pushes the SPEC-0246 `CampaignScreen`; child 5
   replaces that handler body and nothing in this spec changes then.

8. **Carried forward verbatim from SPEC-0241:**
   - the actions `p` park, `u` resume, `P` prioritize, `A` abandon, with the same prompts,
     confirmations, cancel behaviour and `ValueError` handling, through `plan_queue_action` /
     `queue_action` and `allowed_actions`, unchanged;
   - tab plumbing: digits `1`-`5` to switch tabs, `h`, `nw tui --tab`, and
     `IdeaDesk.check_action` gating idea-scoped actions off the queue tab;
   - the header status line (`header_line`), the `queue failed: <error>` line that keeps the
     previous rows, the 2 s `stat()` poll plus 15 s reload in a worker, and the read-only
     guarantee: the tab never calls `events.sync`, `events.cursor_set`, `events.reader_lock`,
     `events.follow` or `mailbox.follow`.

## Design

### Payload additions (appended fields only)

`queue.queue_payload` items gain `created_at` (the campaign's `created_at`, already read for
`age_s`). `dashboard_payload` rows gain `state_since` (the ISO `ts` of the last transition into
the current state, the same event that feeds `state_age_s`; for needs-you, conductor and
waiting rows it is the park's `parked_at` / `waiting.since`). `nw.queue.dashboard.v1` consumers
ignore unknown keys, and no existing field changes. `dashboard_payload` is called by the view
with `recent=20` so the delivered group is useful; the CLI keeps its default of 5.

### View model (`src/nightwright/tui/agentic_model.py`, no Textual import)

- `GROUPS`: the ordered tuple of the nine group names above, and `group_of(state, source)`,
  the one place that maps payload list plus state to a group.
- `QueueRow` keeps its fields and gains `group`, `pri`, `loops`, `cost_usd`, `cap_usd`,
  `activity`, `why`, `state_since`, `activity_since`, `finished_at` and `sort_key`. `meta` is
  removed, and `section` is renamed `group`. `kind` (`"header"` or `"campaign"`) distinguishes
  header rows, which have an empty `campaign`, no actions and `count`.
- `rows_from_payload(payload, *, collapsed=frozenset({"delivered"}), now=None)` returns header
  and data rows in final order. It sorts with the keys of Decision 2 and does not depend on
  the payload's list order. `load_queue(cfg)` keeps its `(rows, header_line)` shape.
- `cost_bar(cost, cap, width=8) -> str` renders `███░░░░░`-style cells, clamped to
  `0..width`; an over-cap campaign fills the bar and is flagged in the cell style. No cap
  returns `""`.
- `format_elapsed(since_epoch, now)` is the single formatter for the ticking cells; it uses
  `cli_queue._dur` / `_ago`, as `nw queue watch` does.
- `activity_text(item)` builds the activity string, including the SPEC-0277 chips
  (the glyph map and `Q.review_short_name(...)[:4]` code move here from `_active_meta`).
- `queue_signature` and the four action wrappers are unchanged.

### Widget (`src/nightwright/tui/queue_tab.py`)

- `on_mount` adds the eight columns of Decision 5.
- `paint(previous)` draws header rows (bold, group colour, `▾`/`▸` fold marker on delivered)
  and data rows, keyed `"<group>|<campaign>"`, with header keys `"#<group>"`. The `title`
  and `why` columns share the width left after the fixed columns (measured, as today); `why`
  is clipped first, then `title`.
- `QueueTable` overrides `action_cursor_down`, `action_cursor_up`, page movement and `Home`/`End`
  to skip header rows, and handles `DataTable.RowHighlighted` so a mouse click or a repaint that
  lands on a header row snaps to the nearest data row. `on_click` with `event.chain == 2`
  calls `view.open_selected()`; Enter keeps its binding. `z` is bound to `toggle_delivered`.
- `_tick` (1 s interval, paused with the other timers in `deactivate`) calls
  `table.update_cell` for the elapsed part of the `state` cell and for the `time` cell of
  data rows, from `time.time()`. It never calls `reload` and never reorders.
- `_restore_index` is extended to skip header rows. Collapsing or expanding delivered
  repaints from the cached rows with no reload.

### Parity with the CLI and the dashboard

`rows_from_payload` must agree with the sources the human also reads:
- the queued rows' campaign order equals `[i["campaign"] for i in queue.backlog_view(cfg)]`,
  which is what `nw queue backlog --json` prints under `items`;
- each group's campaign set equals the corresponding `dashboard_payload` list.

## Alternatives considered

- **One `DataTable` per group.** Rejected for the same reasons as in SPEC-0241: one cursor,
  one set of bindings and short-terminal behaviour are simpler with one table and header rows.
- **Keep sorting by the payload's order and time-varying keys (age, overdue first).**
  Rejected: rows would jump under the cursor as ages cross each other, which is the problem
  this spec fixes.
- **Reload every second to tick elapsed times.** Rejected: it reads every campaign's ledger
  and hammers the data dir (epic G6). Cached timestamps cost nothing.
- **A separate `active` group sorted by state.** Rejected: the human asked for state
  groups with the furthest-along work on top, and a flat active list hides what is closing.
- **Textual `DataTable` row labels as headers.** Rejected: they do not span columns and
  cannot be made unselectable; header rows plus cursor skipping do.

## Acceptance criteria

- [ ] AC1. `rows_from_payload` emits groups in the fixed order needs you, conductor, closing,
      reviewing, gating, building, waiting, queued, delivered; an empty group emits no header;
      each header's count equals its data rows; a seeded campaign in each of CLOSING,
      REVIEWING, GATING, TRIAGING, OPENING, SPEC_WRITING, SPEC_REVIEW and IMPLEMENTING lands in
      the group the table in Decision 1 names; an unlisted active state lands in building.
- [ ] AC2. Inside closing, reviewing, gating and building, rows are sorted by
      `(priority, created_at, campaign id)`; queued rows by backlog `position`; needs-you and
      conductor rows by `(parked_at, campaign id)`; waiting rows by
      `(priority, waiting.since, campaign id)`; delivered newest first. Shuffling the payload
      lists before building the rows does not change the result.
- [ ] AC3. On a seeded data dir, the queued rows' campaign ids equal `nw queue backlog --json`
      `items[].campaign` in order, and for every group the campaign set equals the matching
      `dashboard_payload` list.
- [ ] AC4. Every non-delivered data row shows `pri`, campaign and spec, title, state plus
      activity, `loop`, `cost`, `time` and `why`. Cost renders a bar scaled to `cap_usd`
      (full and flagged when over the cap) and money only when there is no cap. A running
      campaign shows `<persona> <elapsed>`, a gating one its gate name or `gates`, a waiting
      one what it waits on, and a REVIEWING one the SPEC-0277 per-reviewer chips unchanged.
- [ ] AC5. Header rows are never the cursor row: `j`/`k`, arrows, page keys, `Home`/`End`, a
      mouse click on a header and a repaint all end on a data row; at either end the cursor
      stays put. Header rows have no actions and Enter on one does nothing.
- [ ] AC6. A reload (including a forced one) where no campaign changed group leaves the row
      order, the row keys and the cursor campaign unchanged. When a campaign changes group,
      the cursor follows its campaign id to the new row. A deferred-park campaign appears in
      its park group and its active group without a duplicate-key error, and the cursor is
      restored to the right one of the two.
- [ ] AC7. Time-bearing cells advance without a reload: after 3 s on the tab with an
      unchanged signature, the elapsed and time-in-state cells have advanced by about 3 s,
      `load_queue` was called no more than once, and no file under the data dir was opened by
      the tick. The tick is paused when another tab is active.
- [ ] AC8. Enter and a double-click on a data row each post `CampaignSelected` for that
      campaign and open the SPEC-0246 screen (until child 5 replaces it); a single click only
      moves the cursor; a double-click on a header row opens nothing.
- [ ] AC9. `z` toggles the delivered group between header-only and expanded without a reload;
      delivered is collapsed on first load and its header shows the count.
- [ ] AC10. Park, resume (with and without a note), prioritize and abandon (with and without
      keeping the worktree) behave as in SPEC-0241 AC6: same prompts, plan summary
      confirmation, resulting campaign state equal to the CLI's, and cancel or Esc writes
      nothing; an action not allowed for the row's state notifies and opens no prompt; the
      same holds on rows in the new groups.
- [ ] AC11. The tab never calls `events.sync`, `events.cursor_set`, `events.reader_lock`,
      `events.follow` or `mailbox.follow` (monkeypatched to raise) across load, refresh, tick,
      fold and every action; the 2 s signature poll, 15 s reload, `queue failed: <error>`
      status line and idea-action gating on the queue tab are unchanged.
- [ ] AC12. The payload additions are append-only: `dashboard_payload` and `queue_payload`
      keep every existing key and value, and `nw queue watch` and `nw queue backlog --json`
      output are unchanged on a seeded data dir.
- [ ] AC13. SPEC-0241 is set `superseded` with `superseded_by: SPEC-0410` when this spec is
      done, and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_tui_queue_groups.py` (new, no Textual): `rows_from_payload` group order and
    counts (AC1); sort keys and independence from payload list order, with a hand-built
    payload that shuffles every list (AC2); `cost_bar` (empty, partial, full, over cap, no
    cap); `activity_text` including the chips; `allowed_actions` for rows of each new group;
    delivered collapse; `format_elapsed`.
  - `tests/test_agentic_dashboard.py` (added tests only): `created_at` on queue items and
    `state_since` on dashboard rows, and that every pre-existing key is unchanged (AC12).
  - `tests/test_tui_queue_view.py` (new, Textual `run_test` via the child 3 harness,
    `tests/tui_harness.py`, with `pilot_until` waits, never a fixed sleep):
    - cursor skipping across headers with `j`/`k`/page/`Home`/`End`, a click on a header, and
      a repaint (AC5);
    - a state write that moves a campaign between groups, with the cursor following it, and a
      forced reload with no change leaving rows and cursor untouched (AC6), including the
      deferred-park campaign listed twice;
    - the 1 s tick with `time.time` monkeypatched forward and `load_queue` and `open` counted
      (AC7);
    - Enter, single click and `Pilot.double_click` on a data row, double-click on a header
      (AC8);
    - `z` fold (AC9);
    - each action through its prompts with yes, cancel and Esc, moved from
      `tests/test_tui_queue_tab.py` and run on rows of the new groups (AC10);
    - the forbidden-call monkeypatches (AC11) and a load error kept in the status line.
  - `tests/test_tui_queue_parity.py` (new, harness): on the child 3 fixture data dir copy,
    seeded with live campaigns in each state through `store.create_campaign` plus state edits
    where the fixture lacks them, assert the queued order equals
    `nw queue backlog --json` `items[].campaign` (run through `run_cli` on the same data dir)
    and each group equals the `dashboard_payload` grouping (AC3); and that the order read back
    from the mounted `DataTable` rows equals `rows_from_payload`.
  - A text snapshot of the queue tab at 160x48 on that data dir, through the SPEC-0115 pyte
    path, regenerated only with `NW_UPDATE_SNAPSHOTS=1`, showing headers, columns and the cost
    bar.
  - `tests/test_tui_queue_model.py` and `tests/test_tui_queue_tab.py`: assertions about the
    SPEC-0241 `section`/`meta` rows are rewritten to the new model (this spec supersedes
    them on purpose); the action, plumbing, forbidden-call and `check_action` assertions
    stay as they are.
- **Manual verification:** with the queue running, `uv run nw tui --tab queue` on the real
  data dir. Check that the group order and counts match `nw queue watch`, that queued rows
  match `nw queue backlog`, that elapsed times move every second without the rows
  repainting, that no row jumps for two minutes, and that double-click opens the campaign.
  Record the outcome under *As shipped*.
- **Regression guard:** affected tests (`python3 tools/affected_tests.py --run uv run pytest -q`)
  including `tests/test_agentic_dashboard*.py`, `tests/test_tui_*`, `tests/test_cli_queue*` (if
  present) and the epic's guard that no TUI test skips for a missing Textual;
  `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

Nothing to migrate: the view model is rebuilt from live state on each load.

1. Land after the epic's child 3 (test harness). When its spec id exists, add it to
   `depends_on` in this spec's front matter.
2. Land the payload additions, the view model and the widget in one change, with the rewritten
   SPEC-0241 assertions.
3. On `done`, set SPEC-0241 `superseded` with `superseded_by: SPEC-0410` and regenerate
   `docs/LEDGER.md`.
4. Child 5 later replaces the desk's `CampaignSelected` handler body; this spec needs no
   change then.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing:

| Question | Decision | Why |
|---|---|---|
| Does overdue still sort the conductor group first? | No; parked-at order, overdue shown in `why` | overdue flips with time, so sorting on it would move rows |
| How many delivered rows? | `recent=20` for the view | the group is collapsed by default, so a longer list costs nothing |
| Key to fold delivered? | `z` | free on the queue table and not an idea-scoped action |
| Are the SPEC-0241 `meta` tests kept? | rewritten | the `meta` column no longer exists |
