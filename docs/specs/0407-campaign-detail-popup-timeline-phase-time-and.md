---
id: SPEC-0407
title: "Campaign detail popup shell: opener, Timeline, queue actions, polling, retire SPEC-0246"
status: proposed
owner: user
created: 2026-11-03
updated: 2026-11-07
kind: feature
parent: SPEC-0405
source_idea: IDEA-887
tags: [tui, agentic, observability]
supersedes: [SPEC-0246]
---

## Context

This is the shell slice of child 5 of epic
[SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md), promoted from
`IDEA-887`. The human asked to double-click a campaign and get a popup that explains what
happened. The full popup (phase maths, run stream, findings, mail, escalation answers) is
several independent surfaces. This spec ships the one that replaces
[SPEC-0246](./0246-tui-campaign-detail-screen.md): the modal, the opener, the Timeline, the
existing queue actions, and polling. Later specs under the same epic fill the other panes.
Only this spec supersedes SPEC-0246.

**What SPEC-0246 ships today** (`done`):

- `CampaignScreen(QueueActions, Screen)` in `src/nightwright/tui/campaign_screen.py`.
- `agentic_model.load_campaign`, `entry_detail`, `review_text`, and the tuple
  `agentic_model.campaign_signature`.
- `IdeaDesk` pushes that screen from `QueueView.CampaignSelected` and from
  `nw tui --campaign ID`.
- A five-line header, a ledger timeline with groups `transitions` / `runs` / `gates` /
  `findings` / `notes`, a detail pane, a review panel (`v`), `g`/`G`, a 2 s signature poll,
  and `p`/`u`/`P`/`A` through `QueueActions.run_queue_action`.
- `IdeaDesk.check_action` disables idea-scoped actions while a `CampaignScreen` is on the
  stack.

It has no double-click, no loop grouping, and no `decisions` filter group. It is a pushed
screen, so Esc does not read as closing a popup. It opens only from the queue tab.

**What this spec reads.** The history layer (epic child 2, promoted as SPEC-0406) serves
`nw.campaign.history.v1`. This shell uses the document's `header`, `timeline`, `runs` (ids
only), `records` (ids only), and `signature`. The function behind that `signature` field is
`history.campaign_signature(cfg, cid)`: the sha1 hex SPEC-0406 defines over the campaign's
ledger size, ledger `mtime_ns`, `state.json` `mtime_ns`, `runs/` `mtime_ns`, and `gates/`
`mtime_ns`. This spec calls that function. It does not recompute the hash.

The Pilot tests use the harness and fixture those children provide:
`tests/tui_harness.py` (`fixture_cfg`, `prepare_queue_window`, `visible_text`,
`pilot_until`, `Pilot.double_click`) and `tests/fixtures/agentic_history/`.

## Goals / Non-goals

**Goals**

- **G1.** A `CampaignDetail` modal, about 96% × 94%, for any campaign the queue can show.
  Enter and double-click open it. Esc returns the queue cursor to the same campaign id.
- **G2.** One opener API, `open_campaign_detail(cid, focus, origin)`, that this queue path
  and `nw tui --campaign` use, and that the History and Findings tabs can call later.
- **G3.** Overview shows the five SPEC-0246 header lines. Timeline shows every
  `history.timeline` row, grouped by loop, with the SPEC-0246 filter groups plus
  `decisions`, the detail pane, and the review panel.
- **G4.** `p`/`u`/`P`/`A` stay on `QueueActions` / `queue_action`. A live campaign polls
  `history.campaign_signature` every 2 s. A terminal campaign creates no poll timer.
- **G5.** `campaign_screen.py` is deleted and its tests are ported. SPEC-0246 can be marked
  superseded by this spec alone.

**Non-goals**

- **Overview maths (follow-on spec, parent SPEC-0405, depends on this shell).** Phase table,
  wall/cost/share, dominance tags, the `dominant:` line, `bar()`, `sparkline()`, the `gaps`
  list, the 160×48 Overview pyte golden, and the 500-campaign popup-open budget. This
  shell's Overview is the SPEC-0246 header only.
- **Runs pane (follow-on spec, parent SPEC-0405, depends on this shell).** The run table,
  the verdict pane, stderr, `stream_tail`, and visible-only live follow. That spec's
  live-follow test makes a fixture run running by removing its `exit.json` and `run.json`
  in the copied data dir and monkeypatching `runproc.run_status` (or `tail._ended`) to
  report `running` until the test writes `exit.json`. Follow stops when
  `tail._ended(run_dir)` becomes true (for example when `exit.json` or `run.json`
  appears), after draining the remaining lines once. Deleting `exit.json` alone leaves
  `_ended` true, because a closed run still has `run.json` and `run_status` is not
  `running`. This shell has no live-follow test.
- **Findings & decisions, Mail, and the shared escalation answer flow (follow-on spec,
  parent SPEC-0405, depends on this shell and the Runs spec).** The records table, the
  `not re-raised` label, `/` filters, `r`/`d`/`i`, `mail_threads`, and moving
  `MailboxView._escalation_flow` into `tui/escalation_flow.py::answer_escalation`. This
  shell does not bind `e`/`E` and does not move that coroutine. The Mailbox and escalation
  tab tests stay on `MailboxView._escalation_flow`.
- Those three follow-ons add panes to the `CampaignDetail` this spec creates. None of them
  sets `supersedes: [SPEC-0246]`.
- The History and Findings tabs (epic children 6 and 7). They call the opener this spec
  defines.
- Live-queue column layout (epic child 4). This spec only changes what Enter and
  double-click open.
- New history fields. Numbers and rows come from `nw.campaign.history.v1` as the history
  layer returns them.
- Any write path other than the existing park, resume, prioritize, and abandon flow.

## Decision

- **The screen** is `CampaignDetail(QueueActions, ModalScreen)` in
  `src/nightwright/tui/campaign_detail.py`. It renders a `DetailView`.
- **The view** is `agentic_model.load_detail(cfg, cid) -> DetailView`, built from
  `history.campaign_history(cfg, cid)` plus the SPEC-0246 header lines, `entry_detail`, and
  `review_text`, which stay as they are.
- **The frame** is 96% × 94% with five tabs. Overview and Timeline are filled. Runs,
  Findings & decisions, and Mail are placeholders until their follow-on specs.
- **Opening** is a `DetailOpener` mixin. `IdeaDesk.open_campaign_detail` pushes the modal
  and, on dismiss, restores the opener's cursor by campaign id.
- **Polling** a live campaign stats `history.campaign_signature` plus the global logs named
  below. A terminal campaign never starts that timer.
- **Removal.** `CampaignScreen` and `campaign_screen.py` are deleted. Their behavior is
  restated here and their tests move to the popup.

## Design

### Opening and closing

`DetailOpener` (in `campaign_detail.py`) is mixed into a `DataTable` subclass. The subclass
implements `detail_target(row_key) -> (cid, focus) | None`.

- On the table's select binding (`enter`) and on a `Click` with `event.chain == 2` over a
  data row, the mixin moves the cursor to that row, then posts `OpenCampaign(cid, focus)`.
- A click on a header or section row, and a row whose `detail_target` returns `None`, posts
  nothing.
- One keypress posts one message. The queue table's `enter` path posts `OpenCampaign` and
  does not also post `QueueView.CampaignSelected`. `CampaignSelected` stays defined, and
  `IdeaDesk._campaign_selected` still calls `open_campaign_detail`, so a later queue table
  that posts `CampaignSelected` opens the same modal.

The queue table adopts `DetailOpener` with `focus=None`.

`IdeaDesk.open_campaign_detail(cid, focus=None, origin=None)`:

1. Pushes `CampaignDetail(cfg, cid, focus=focus)`.
2. On dismiss, calls `origin.select_campaign(cid)` when `origin` is given. That moves the
   cursor to the row whose campaign id is `cid` when that row is still present, and leaves
   the cursor where it is when the row is gone.

The queue reloads under the modal, so restore is by campaign id.

`focus` is `None`, `{"tab": "runs", "run": run_id}`, or
`{"tab": "records", "record": record_id}`. `resolve_focus(history, focus)` is pure:

- `None` selects Overview.
- A known run id selects the Runs tab. A known record id selects the Findings & decisions
  tab. The placeholder shows which id was requested. Row widgets inside those panes are the
  follow-on specs' work.
- An unknown id, or a `tab` other than `overview`, `timeline`, `runs`, `records`, or
  `mail`, selects Overview and notifies `not found: <id>`.

`nw tui --campaign ID` keeps SPEC-0246's startup checks:

- The id is validated before the app starts.
- A unique prefix is accepted, resolved with `store.list_campaigns(cfg, include_terminal=True)`.
- An unknown or ambiguous id raises `click.ClickException` before the TUI starts. The
  ambiguous case lists the matches.

On a valid id it pushes `CampaignDetail` on mount, over the queue tab, including when the
campaign is terminal.

`escape` and `q` dismiss the popup from any pane. A text prompt or choice screen pushed on
top consumes Esc first, as Textual already does.

While the popup is on the screen stack:

- The popup binds `1`–`5` to its own panes, so those keys do not switch desk tabs.
- `IdeaDesk.check_action` treats `CampaignDetail` the way it treats `CampaignScreen` today:
  idea-scoped actions stay disabled. The `isinstance(..., CampaignScreen)` check becomes
  `CampaignDetail` (the screen class is gone).

### Loading and polling

`CampaignDetail.on_mount` loads off the event-loop thread
([SPEC-0129](./0129-tui-poll-reload-off-the-event-loop-thread.md)), using the same
`run_worker` / thread pattern as `CampaignScreen`:

- Until `load_detail` returns, the frame shows `loading <cid>…`.
- When `load_detail` raises, the frame shows `history unavailable: <error>` as a `Text`
  object, and Esc still dismisses the popup.

`DetailView` holds `campaign`, `history` (the `nw.campaign.history.v1` dict), `terminal`,
`header_lines`, `timeline` (the grouped rows below), `actions` (from `allowed_actions`),
and `signature` (the `detail_signature` value that was current at load).

`terminal` is true when `history.header.outcome` is `merged`, `handed_off`, or `abandoned`,
or when the campaign state is in `store.TERMINAL_STATES`.

**Live poll.** When `terminal` is false, `set_interval(2.0)` runs
`detail_signature(cfg, cid)` in a worker. That value is:

- the string `history.campaign_signature(cfg, cid)` returns;
- `(size, mtime_ns)` of `conductor/decisions.jsonl`;
- `(size, mtime_ns)` of each file in `mailbox/`.

It uses `stat` only. It does not open those files. A signature change reloads `load_detail`
in a worker.

**After a reload.** The active pane and the Timeline filter stay. The Timeline cursor stays
on the same `seq` (or the same non-ledger `ref`). When the cursor was on the last event, it
follows the new last event. When a live campaign becomes terminal on a reload, the interval
is stopped and not restarted.

**Terminal.** When `terminal` is true at the end of the initial load, no interval is
created. After that load, the popup does no file I/O until a key or a Timeline highlight
needs one file the view does not hold (a run detail or a gate log tail). That read is one
file, in a worker. The queue tab's own poll keeps running under the modal; the idle test
below pauses that poll so the popup's own calls can be counted.

`load_detail`, `detail_signature`, and the Timeline helpers never call `events.sync`,
`cursor_set`, or `reader_lock`.

### Overview and the three placeholders

Overview shows `header_lines` from `load_campaign`: the five SPEC-0246 lines (identity,
loops/runs/cost, branch/worktree, park, follow-ups), unchanged. It does not draw a phase
table, bars, a sparkline, or `gaps`.

Runs, Findings & decisions, and Mail each show one plain `Text` line,
`placeholder: runs`, `placeholder: records`, or `placeholder: mail`. `1`–`5` and
`tab` / `shift+tab` select Overview, Timeline, Runs, Findings & decisions, Mail, in that
order. A footer lists the active pane's keys. Mouse clicks on tabs and scrolling are
shortcuts.

The CSS is `CampaignDetail > #detail-frame { width: 96%; height: 94%; }`, centred on a
dimmed backdrop.

### Timeline pane

Rows come from `history.timeline` in the order `history.campaign_history` returns them.
Each row has `seq`, `ts`, `kind`, `phase`, `loop`, `by`, `summary`, `ref`. Columns are
`seq` (blank when `seq` is null), `time` (`MM-DD HH:MM:SS`), `phase`, `by`, `kind`, and
`summary`.

**Loop grouping.** `group_timeline(rows)` is pure. It inserts a separator `── loop N ──`
wherever `loop` changes. `loop` 0 is the history layer's value for rows before the first
fix-loop visit. The cursor never rests on a separator: `up`/`down`, `g`/`G`, and highlight
land on events. `[` / `]` jump to the first event of the previous or next loop. The cursor
opens on the last event.

**Filter.** `f` cycles `all → transitions → runs → gates → findings → notes → decisions → all`.
`timeline_group(row)` is pure:

| Group | Rows |
|---|---|
| `transitions` | kind `open`, `transition`, `park`, `resume`, `priority`, `close`, `admitted_after_revision`, `admitted_revise_pending` |
| `runs` | kind `run` (the history layer's merge of `run.start` / `run.end`) and `cost` |
| `gates` | kind `gate` |
| `findings` | kind `finding`, `followup` |
| `decisions` | kind `human_decision`, `triage`, and every row with `seq` null (the non-ledger rows the history layer interleaves) |
| `notes` | every other kind, including `note`, `commit`, `backlog`, and an unknown kind |

The five SPEC-0246 groups keep that membership for every `schemas.EVENT_KEYS` type other
than `human_decision` and `triage`, and for an unknown ledger type (`notes`). The status
line reads `filter <group> · <shown>/<total> events · loop <n>/<loops>`.

**Detail pane.**

- A row with an integer `seq` shows `entry_detail(cfg, cid, entry)` for that ledger event,
  with SPEC-0246's content: a run's `campaign_run` fields, verdict lists, stderr tail, and
  paths; a gate's results and the last 40 lines of each failed gate's log; the unclipped
  payload for every other type; `(missing: …)` and `(run unavailable: …)` inline.
  For kind `run`, the event is the ledger event the history row kept (the `run.start` when
  both ends exist).
- A row with `seq` null shows the `history.records` object whose `id` is `ref`: every
  field, `links`, and `linked_by`. A missing record shows `record <id> not in history`.

**Review.** `v` toggles the detail pane to `review_text(cfg, state)`, with SPEC-0246's
content and placeholders unchanged. Another `v` returns to the row detail.

### Actions

`p`, `u`, `P`, and `A` call `QueueActions.run_queue_action(action, cid, state, view.actions)`:
the SPEC-0241 prompt, then `plan_*`, then confirm, then `apply_*`, through
`agentic_model.queue_action`. After an apply, the view reloads. After `abandon`, the popup
stays open on the now-terminal campaign and its poll timer is stopped. An action absent
from `view.actions` notifies `<action> not available in <state>`.

The popup writes only through that flow. It does not answer escalations, send mail, or call
any other writer.

### Safety

Titles, summaries, review text, and detail-pane text are built with `Text` /
`Text.assemble`. They are never parsed as Rich markup
([SPEC-0265](./0265-operator-surfaces-survive-bad-data-and-never.md)). A missing file or a
`ValueError` from `entry_detail` / `campaign_run` becomes the SPEC-0246 placeholder line.
`load_detail` failures stay on the `history unavailable:` line. None of these raise out of
the screen.

### Retiring SPEC-0246

`campaign_screen.py` is deleted. `load_campaign`, `entry_detail`, `review_text`, and the
tuple `agentic_model.campaign_signature` stay in `agentic_model.py`.
`tests/test_tui_campaign_model.py` stays unchanged. The popup's poll uses
`detail_signature` / `history.campaign_signature`, not the tuple helper.

`tests/test_tui_campaign_screen.py` is replaced by `tests/test_tui_campaign_detail.py`.
Every case in the old file is ported (see the Test plan).

When this spec is closed, SPEC-0246 is set `status: superseded` with
`superseded_by: SPEC-0407`.

## Alternatives considered

- **Keep `CampaignScreen` as a pushed screen and add the tabs there.** The human asked for
  a popup, and SPEC-0405 Decision 4 chose a modal. At 96% × 94% the Timeline keeps nearly
  a full screen of height. SPEC-0246 had rejected a short modal; the near-full size is the
  answer.
- **Ship Overview maths, the Runs tail, findings, mail, and the escalation-flow move in
  this spec.** The review counted 21 acceptance criteria across those surfaces. Each
  follow-on listed under Non-goals can ship on its own against the shell this spec leaves.
- **Stop the queue tab's poll while the modal is open.** The queue stays live under the
  popup, which is why cursor restore is by campaign id. The idle-I/O check pauses that poll
  in the test, and the manual check compares against the queue's own baseline.
- **Guess a running run by deleting only `exit.json`.** `tail._ended` stays true while
  `run.json` exists. The Runs follow-on owns the corrected setup, under Non-goals.

## Acceptance criteria

- [ ] **AC1. Opening and closing from the queue.**
  - On the queue tab, Enter on a campaign row and `Pilot.double_click` on a different
    campaign row each open `CampaignDetail` for that row's campaign.
  - A double-click on a section or header row opens nothing.
  - Esc and `q` each dismiss the popup and leave the queue cursor on the same campaign id
    after the queue has reloaded while the popup was open.
  - When that campaign row is no longer in the table, the cursor stays on the row it
    already occupied.
- [ ] **AC2. The shared opener.**
  - A minimal app whose only table is a `DetailOpener` `DataTable` opens the popup on Enter
    and on double-click.
  - `open_campaign_detail(cid, focus={"tab": "records", "record": id})` with an id in
    `history.records` shows the Findings & decisions placeholder for that id.
  - The run form with an id in `history.runs` shows the Runs placeholder for that id.
  - An unknown focus id shows Overview and notifies `not found: <id>`.
- [ ] **AC3. `nw tui --campaign`.**
  - A full id and a unique prefix each open the popup, including for a merged campaign.
  - An unknown id and an ambiguous id each raise `click.ClickException` before the TUI
    starts. The ambiguous exception lists the matches.
- [ ] **AC4. The frame.**
  - At 160×48 the popup frame measures 96% ± 1 cell by 94% ± 1 cell of the screen.
  - The five panes are Overview, Timeline, Runs, Findings & decisions, and Mail. `1`–`5`
    select them in that order.
  - Overview shows the five SPEC-0246 header lines and no phase table.
  - The other three panes show `placeholder: runs`, `placeholder: records`, and
    `placeholder: mail`.
  - While the popup is shown, desk tab keys do not change the desk tab, and idea-scoped
    actions stay disabled.
- [ ] **AC5. Timeline.**
  - The table lists every `history.timeline` row in order, with a `── loop N ──` separator
    wherever `loop` changes. The cursor never rests on a separator. `[` and `]` move to the
    first event of the previous and next loop. The cursor opens on the last event.
  - `f` cycles the seven groups. For every `schemas.EVENT_KEYS` type, for an unknown type,
    and for a `seq`-null row, `timeline_group` matches the Design table.
  - The status line shows the filter name, `<shown>/<total>`, and the loop.
- [ ] **AC6. Timeline detail and review.**
  - The detail pane shows `entry_detail` for a ledger row, including a run, a gate, and
    `(missing: …)` / `(run unavailable: …)`, and it does not raise.
  - A `seq`-null row shows that record's fields, `links`, and `linked_by`.
  - `v` shows `review_text` with SPEC-0246's content, and a second `v` restores the row
    detail.
- [ ] **AC7. Queue actions.**
  - `p`, `u`, `P`, and `A` in the popup run the SPEC-0241 flow through `QueueActions` /
    `queue_action`. After a confirmed apply, the header shows the new state.
  - After `abandon`, the popup stays open and its poll timer is stopped.
  - An action absent from `allowed_actions` notifies `<action> not available in <state>`
    and writes nothing.
- [ ] **AC8. Polling.**
  - On a live campaign, a ledger append is reflected within one 2 s poll with no key press.
    The active pane, the Timeline filter, and the Timeline cursor (by `seq` or `ref`) stay.
    The Timeline follows the end when the cursor was on the last row.
  - On a terminal campaign, no interval timer exists.
  - After the load completes, a terminal popup left idle on Overview for 2.5 s creates no
    interval timer, and `detail_signature`, `load_detail`, and `stream_tail` are not
    called; with the desk's queue poll paused in the test, no `stat` or `open` happens
    under its campaign dir.
  - The popup never calls `events.sync`, `cursor_set`, or `reader_lock`. A monkeypatch that
    raises from each of them leaves the popup open and responsive.
- [ ] **AC9. Bad data on these panes.**
  - A header field and a timeline summary containing `[bold red]x[/]` and
    `[link=file:///etc]y[/link]` render those characters literally.
  - A history document with a non-empty `gaps` list, and a gate row whose log file is
    missing, still open. The detail pane shows `(missing: …)`. Nothing raises.
  - When `load_detail` raises, the frame shows `history unavailable: …` and Esc dismisses
    the popup.
- [ ] **AC10. Write paths and the old screen.**
  - Over a Pilot session that visits all five panes and every Timeline key, with no queue
    action confirmed, every file under the data dir is byte-identical before and after.
  - `campaign_screen.py` is gone, and no module imports `CampaignScreen`.

## Test plan

**Automated tests.** They run under plain `uv run pytest` with no Textual skip guard. UI
tests boot the real `IdeaDesk` through `tests/tui_harness.py` on a copy of
`tests/fixtures/agentic_history/`. Queue tests call `prepare_queue_window` first so the
delivered rows are on screen, and they read campaign ids from the mounted queue rows.

- **`tests/test_tui_campaign_detail_model.py`** (pure, no app):
  - `timeline_group` for every `schemas.EVENT_KEYS` type, one unknown type, kind `run`,
    kind `triage`, kind `human_decision`, and one `seq`-null row (AC5).
  - `group_timeline` separator placement when `loop` changes, including a leading loop 0
    (AC5).
  - `resolve_focus` for `None`, a known run id, a known record id, and an unknown id (AC2).
  - `load_detail` header lines for one fixture campaign equal `load_campaign`'s five lines
    (AC4).
  - A summary and a header string containing the AC9 markup are `Text` whose plain value
    is the source characters (AC9).
- **`tests/test_tui_campaign_detail.py`** (Pilot on the real `IdeaDesk`). It replaces
  `tests/test_tui_campaign_screen.py` and ports each of its cases:
  - open, then Esc, cursor kept (`test_enter_opens_the_screen_and_escape_returns_with_the_cursor`);
  - `--campaign` full id, prefix, unknown, ambiguous (`test_campaign_option_opens_the_screen_directly`,
    `test_cli_campaign_resolves_ids_and_rejects_bad_ones`);
  - `f` cycling and the status counts (`test_cursor_starts_on_the_last_event_and_filters_cycle`);
  - `v` and the detail pane (`test_detail_pane_follows_the_cursor_and_v_toggles_the_review`);
  - a live append (`test_a_ledger_append_shows_within_a_poll_and_never_syncs`);
  - park end to end (`test_park_runs_end_to_end_through_the_screen`);
  - idea keys inert (`test_idea_keys_are_inert_on_the_screen`);
  - reload error, failed paint, signature error, verbatim timeline text, detail error,
    filter/resize errors (`test_reload_error_keeps_the_app_alive`,
    `test_a_failed_paint_is_retried_by_the_next_poll`,
    `test_a_signature_error_in_poll_is_contained`, `test_timeline_text_renders_verbatim`,
    `test_show_detail_error_is_shown_in_the_pane`,
    `test_filter_key_and_resize_paint_errors_are_contained`).

  It adds:
  - **Queue opening (AC1).** Enter on one delivered row, `Pilot.double_click` on another,
    a double-click on the delivered section row, Esc and `q` after a forced queue reload,
    and a dismiss after the row has been removed.
  - **Opener (AC2).** A minimal `App` with a `DetailOpener` table; the run and record
    `focus` forms; an unknown focus id.
  - **`--campaign` (AC3).** Full id, prefix, merged campaign, unknown, and ambiguous, with
    `T.run` patched for the valid cases.
  - **Frame (AC4).** Widget-region size at 160×48, `1`–`5`, the five header lines against
    `load_campaign`, the three placeholder strings, and idea-key gating.
  - **Timeline (AC5, AC6).** `f`, `[`, `]`, `v`, a run row, a gate whose log is missing,
    and a `seq`-null row. Visible text for one merged fixture campaign includes every
    `history.timeline` summary from `nw campaign history <cid> --json` on the same data dir.
  - **Actions (AC7).** `p` end to end on a copy whose state was set to a live non-terminal
    state, then `A`, with the poll timer stopped after abandon, and one refused action.
  - **Polling (AC8).**
    - One copied fixture campaign with `state.json` `state` set to `IMPLEMENTING` (so
      `header.outcome` is null and `terminal` is false). Append a ledger line and wait with
      `pilot_until`.
    - A merged fixture campaign: the popup's interval list is empty.
    - After that terminal load, pause the queue timers (`QueueView.deactivate`), spy
      `agentic_model.detail_signature`, `agentic_model.load_detail`, and
      `agentic_model.stream_tail` (install a zero-arg spy on the name when the Runs spec
      has not defined it yet), wait 2.5 s, and assert the spies stay at the post-load
      count. With those timers paused, count `os.stat` and `open` and assert no path under
      that campaign dir.
    - `events.sync`, `cursor_set`, and `reader_lock` monkeypatched to raise.
  - **Bad data (AC9).** Markup fixtures, a missing gate log, `load_detail` patched to raise.
  - **No writes (AC10).** sha256 of every file under the harness data dir before and after
    a session that visits all five panes and the Timeline keys with no confirm, plus an
    import check that `nightwright.tui.campaign_screen` and `CampaignScreen` are gone.

**Manual verification.** On the real data dir, record the results in *As shipped*:

1. `uv run nw tui`, press `3`, double-click a campaign. Overview shows the SPEC-0246 header.
   Timeline loops match `nw campaign log <cid>` for the same id.
2. Press Esc. The queue cursor is on that campaign.
3. Repeat step 1 with Enter, then `1`–`5`, `f`, `v`, `[`, `]`, and `q`.
4. Leave a terminal campaign's popup open for 1 minute. Pause the queue tab's poll, or take
   a baseline `strace -f -e trace=openat,statx -p <pid>` of the queue tab alone and subtract
   it. The popup adds no `stat` or `open` under that campaign dir.

**Regression guard.**

- At the queue close gate (SPEC-0378): the affected tests
  (`python3 tools/affected_tests.py --run uv run pytest -q`), plus
  `tests/test_tui_queue_tab.py`, `tests/test_tui_campaign_model.py`,
  `tests/test_tui_mailbox_tab.py`, and `tests/test_tui_escalations_tab.py`.
- `tests/test_tui_fixture_harness.py`, once that file exists: its double-click case currently
  expects an unchanged screen stack, because nothing opened a campaign yet. If its offset
  lands on a campaign row, this change updates that one assertion to expect `CampaignDetail`.
  A header or section offset still leaves the stack unchanged. The rest of that file stays
  as its spec wrote it.
- `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. **Hard ordering.** Implement this spec only after all of these are `done` and present
   in the tree this branch builds on:
   - **SPEC-0406** — epic child 2, the history layer. It defines
     `history.campaign_signature` and `history.campaign_history`.
   - **SPEC-0415** — the sanitized fixture `tests/fixtures/agentic_history/` (split out of
     child 2).
   - **SPEC-0409** — epic child 3 as promoted (Textual skip removal).
   - **SPEC-0459** — the Pilot harness `tests/tui_harness.py` (split out of child 3).

   Those four files are not in this worktree. `tools/spec_lint.py` rule 6 rejects a
   `depends_on` id whose spec file is absent, so the front matter does not list them yet.
   As soon as this branch contains the four files, set

   `depends_on: [SPEC-0406, SPEC-0409, SPEC-0415, SPEC-0459]`

   and do not admit the spec for implementation while any of them is missing. SPEC-0406 and
   SPEC-0409 are the promoted child-2 and child-3 ids. SPEC-0415 and SPEC-0459 are the
   halves those children dropped that this shell's loader and Pilot tests call.
2. If the live-queue spec has not landed, wire `DetailOpener` into the current queue table.
   When that spec replaces the table, it keeps the mixin.
3. In one change: add `CampaignDetail`, `load_detail`, `detail_signature`, and the Timeline
   helpers; point `CampaignSelected` and `--campaign` at the popup; delete
   `campaign_screen.py`; port its tests.
4. No data migration. The popup reads the history layer. The cache, when that layer has
   one, builds lazily.
5. At close: set SPEC-0246 to `status: superseded` with `superseded_by: SPEC-0407`,
   regenerate the ledger, and record `DetailOpener`, `open_campaign_detail`, and `focus`
   in *As shipped* for the History and Findings specs.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the manual steps, and the results recorded in *As shipped*.
- [ ] No regressions: the affected tests and the listed TUI suites are green at the close gate.
- [ ] Spec body updated to match what shipped. If `history.campaign_signature`'s name differs
      from SPEC-0406, the Design uses the name that shipped.
- [ ] Front matter `depends_on` lists SPEC-0406, SPEC-0409, SPEC-0415, and SPEC-0459 once
      those files are on the branch, and `python3 tools/spec_lint.py` exits 0.
- [ ] SPEC-0246 set `superseded` with `superseded_by: SPEC-0407`.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

None. Decided while revising after the split review:

| Question | Decision | Why |
|---|---|---|
| Which slice does SPEC-0407 keep? | the shell: opener, Timeline, queue actions, polling, retire SPEC-0246 | the other three surfaces are follow-on specs under Non-goals |
| Who supersedes SPEC-0246? | only this spec | it is the slice that deletes `campaign_screen.py` |
| What fills the history `signature`? | `history.campaign_signature` | that is the function SPEC-0406 puts in the `signature` field |
| How is a terminal popup's idle I/O counted? | spy `detail_signature`, `load_detail`, and `stream_tail`, and pause the queue poll before counting `stat`/`open` | the queue poll also stats campaign dirs |
| How does the Runs follow-on start live follow? | remove `exit.json` and `run.json`, and patch `run_status` or `tail._ended` until the test writes `exit.json` | `tail._ended` is true whenever `run.json` exists |
| Why is `depends_on` absent in this revision? | the four spec files are not in this worktree, and lint rule 6 rejects unknown ids | Rollout step 1 adds them once the files are on the branch |
