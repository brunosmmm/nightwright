---
id: SPEC-0407
title: "Campaign detail popup: timeline, phase time and cost with dominant phase, runs with stream tail, findings and decisions"
status: proposed
owner: user
created: 2026-11-03
kind: feature
parent: SPEC-0405
source_idea: IDEA-887
tags: [tui, agentic, observability]
supersedes: [SPEC-0246]
---

## Context

This is child 5 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md),
promoted from `IDEA-887`. The human asked to "double click an item and get a popup that
thoroughly explains the timeline of the developments, cost, time, what phase is dominating
the time", and to see the findings and decisions behind it, for live and closed campaigns.

**What exists today.** [SPEC-0246](./0246-tui-campaign-detail-screen.md) (`done`) has these
parts:
- `CampaignScreen(QueueActions, Screen)` in `src/nightwright/tui/campaign_screen.py`.
- The loader `agentic_model.load_campaign` / `entry_detail` / `review_text` /
  `campaign_signature` in `src/nightwright/tui/agentic_model.py`.
- `IdeaDesk` pushes the screen on `QueueView.CampaignSelected` (`app.py`, the
  `_campaign_selected` handler) and for `nw tui --campaign ID`.

What the screen offers:
- a five-line header;
- a ledger timeline with a group filter (`f`) and a per-entry detail pane;
- a review panel (`v`);
- `g`/`G` jumps;
- live refresh from a 2 s signature poll;
- the SPEC-0241 actions (`p`/`u`/`P`/`A`) through `QueueActions.run_queue_action`.

What it does not have:
- per-phase time or cost, and no dominant phase;
- the run stream;
- conductor decisions, mail and escalations;
- links between findings, the runs that produced them and their resolutions;
- double-click.

It opens only from the queue tab, so campaigns that closed more than 24 h ago cannot be
reached from the TUI.

**What this spec builds on:**
- **The history layer, child 2 of SPEC-0405.** `agentic/history.py` serves
  `nw.campaign.history.v1`:
  - header and phases;
  - `dominant`;
  - the merged timeline;
  - runs and gates;
  - `nw.history.record.v1` records with `status`, `links` and `linked_by`;
  - `gaps`.

  It does this from a disposable cache, together with a per-campaign signature.
- **The TUI test harness, child 3.** `tests/tui_harness.py` gives:
  - a cfg over a copied `tests/fixtures/agentic_history/` data dir;
  - the real `IdeaDesk` under `run_test`;
  - `pilot_until` waits;
  - `Pilot.double_click`;
  - visible-text extraction;
  - pyte text snapshots at 160×48 (`NW_UPDATE_SNAPSHOTS=1`);
  - a scale-clone helper.
- **Run tail rendering.** `agentic/tail.py` already has:
  - `read_lines(path, offset)` for complete lines by byte offset;
  - the stateful `Renderer` (`feed_line` → `list[Text]`, `result_line`);
  - `_ended(run_dir)`;
  - `_summary_without_stream` for runs that predate
    [SPEC-0362](./0362-stream-json-capture-of-persona-runs-and-nw.md).
- **The escalation answer flow.** The SPEC-0250 flow is
  `MailboxView._escalation_flow(row, action)` in `src/nightwright/tui/mailbox_tab.py`
  (choose an option, or a free-text answer, through `escalations.plan_answer` /
  `apply_answer`).

This spec supersedes SPEC-0246. It replaces `CampaignScreen` with a `CampaignDetail` modal and
carries forward everything SPEC-0246 promised, restated below so that SPEC-0246 can be retired.

## Goals / Non-goals

**Goals**
- **G1.** One `CampaignDetail` popup for any campaign, live or terminal. It opens by Enter or
  double-click from the queue tab, by `nw tui --campaign ID`, and through one opener API that
  the History and Findings tabs (children 6 and 7) reuse. Esc returns to the same row.
- **G2. Overview.** It shows:
  - the header;
  - a per-phase table of wall time, cost and share, with the dominant phase highlighted;
  - a unicode bar breakdown of each phase's time;
  - a cost-per-run sparkline;
  - the history's `gaps`.
- **G3. Timeline.** Every event is grouped by loop. The SPEC-0246 group filter, detail pane and
  review panel are kept.
- **G4. Runs.** A run table plus a run pane, which holds:
  - the verdict, `must_fix`, blockers, changed files and tests;
  - the stderr tail;
  - the last 200 rendered lines of the stream, followed live only while the pane is visible;
  - the `result` text for runs that have no stream.
- **G5. Findings & decisions.** Every record on the campaign, with filters and with jumps to
  the producing run (`r`), the resolving decision (`d`) and the follow-up idea (`i`).
- **G6. Mail.** The campaign's mailbox threads, read-only.
- **G7. Actions.** Only the existing actions: park, resume, prioritize and abandon through
  `queue_action`, and the SPEC-0250 escalation answer flow.
- **G8. Polling.** A live campaign polls a cheap signature every 2 s. A terminal campaign never
  polls.

**Non-goals**
- The History and Findings tabs themselves (children 6 and 7). This spec gives them only the
  opener API and the `focus` argument.
- Live-queue ordering and columns (child 4). This spec only rewires what Enter and
  double-click on a queue row open.
- New history fields or derivations. Every number comes from `nw.campaign.history.v1`
  unchanged.
- Replying to or sending mail from the popup. The Mailbox tab keeps that.
- Withdrawing escalations, adding notes, re-running gates or personas, and editing anything.
- Plots beyond terminal bars and sparklines.

## Decision

- **The screen.** A new `CampaignDetail(QueueActions, ModalScreen)` in
  `src/nightwright/tui/campaign_detail.py` renders a view model.
- **The view model.** `agentic_model.load_detail(cfg, cid) -> DetailView` builds it from
  `history.campaign_history(cfg, cid)`, plus the SPEC-0246 header lines, `entry_detail` and
  `review_text`, which are kept as they are.
- **Layout.** The popup is a frame of 96 % × 94 % with a `TabbedContent` of five panes:
  Overview, Timeline, Runs, Findings & decisions, Mail.
- **Pure helpers.** Everything that turns history into text (phase rows, bars, sparkline, loop
  grouping, record filters, link resolution, mail threading) is a pure function in
  `agentic_model.py`, with no Textual import, so it is unit-tested without an app.
- **Opening.** A `DetailOpener` mixin for any `DataTable` turns Enter and `Click.chain == 2`
  into an `OpenCampaign(cid, focus)` message. `IdeaDesk.open_campaign_detail(cid, focus=None)`
  pushes the popup and, on dismiss, puts the opener's cursor back on that campaign's row.
- **Escalations.** The SPEC-0250 flow moves unchanged out of `MailboxView` into a shared
  coroutine, `tui/escalation_flow.py::answer_escalation`, so the popup and the Mailbox tab run
  the same code.
- **Removals.** `CampaignScreen` and `campaign_screen.py` are deleted. Their tests are ported to
  the popup.

## Design

### Opening and closing

- **The mixin.** `DetailOpener` (in `campaign_detail.py`) is mixed into a `DataTable` subclass.
  The subclass implements `detail_target(row_key) -> (cid, focus) | None`. The mixin does two
  things:
  - On `enter` (the table's existing select binding) and on a `Click` event with
    `event.chain == 2` over a data row, it posts `OpenCampaign(cid, focus)` for that row.
    The double-click also moves the cursor to the clicked row first.
  - Clicks on header or section rows, and rows whose `detail_target` returns `None`, do
    nothing.
- **Queue tab.** The queue table adopts `DetailOpener`, with `focus=None`. The existing
  `QueueView.CampaignSelected` message stays, for compatibility with child 4. Its handler
  calls `open_campaign_detail`.
- **The opener.** `IdeaDesk.open_campaign_detail(cid, focus=None, origin=None)`:
  1. pushes `CampaignDetail(cfg, cid, focus=focus)`;
  2. on dismiss, calls `origin.select_campaign(cid)` when `origin` (the opener table) is
     given. That method moves the cursor to the row whose campaign id is `cid` if it is still
     present, and otherwise leaves the cursor where it is.

  Because the queue keeps reloading underneath the modal, this restores the row by campaign
  id, not by index.
- **`focus`** is `None`, `{"tab": "runs", "run": run_id}` or
  `{"tab": "records", "record": record_id}`. The popup opens on that tab with that row
  selected. If the id is unknown, the popup opens on Overview and notifies
  `not found: <id>`. Child 7's `c` key uses the record form.
- **`nw tui --campaign ID`** keeps SPEC-0246's behaviour:
  - the id is validated before the app starts;
  - a unique prefix is accepted, resolved over `store.list_campaigns(cfg,
    include_terminal=True)`;
  - an unknown or ambiguous id raises a `ClickException`, and the ambiguous case lists the
    matches.

  It now pushes `CampaignDetail` on mount over the queue tab, for terminal campaigns too.
- **Closing.** `escape` and `q` dismiss the popup from any pane. If a text prompt or choice
  screen is open on top, it consumes Esc first, as Textual already does.
- **Desk key gating.** While the popup is shown, the desk's own tab keys (`1`–`5`, and `6`/`7`
  once children 6 and 7 add them) do not switch desk tabs. Inside the popup, `1`–`5` select
  its own panes. SPEC-0241's `IdeaDesk.check_action` rule ("the default screen is on top and
  `tab-ideas` is active") keeps the idea-scoped app actions disabled.

### Loading and polling

`CampaignDetail.on_mount` loads the view off the event loop
([SPEC-0129](./0129-tui-poll-reload-off-the-event-loop-thread.md)):
- It starts a thread worker that runs `load_detail`.
- Until the worker returns, it shows `loading <cid>…`.
- If `load_detail` raises, the frame shows `history unavailable: <error>` as plain text, and
  Esc still closes the popup.

`DetailView` is a dataclass with these fields:
- `campaign`;
- `history` (the raw `nw.campaign.history.v1` dict);
- `terminal: bool`, true when `header.outcome` is not null or the state is in
  `queue.TERMINAL`;
- `header_lines: list[Text]`;
- `phase_rows`;
- `bars`;
- `sparkline: Text`;
- `timeline: list[TimelineRow]`;
- `runs: list[RunRow]`;
- `records: list[RecordRow]`;
- `threads: list[MailThread]`;
- `actions`, as `allowed_actions`;
- `open_escalations: list[str]`;
- `signature`.

**Polling a live campaign:**
- When `terminal` is false, `set_interval(2.0)` computes `detail_signature(cfg, cid)` in a
  worker. That signature is the tuple of child 2's per-campaign signature (the function behind
  the history's `signature` field), plus `(size, mtime_ns)` of `conductor/decisions.jsonl`,
  plus the mailbox inbox files. It does only `stat` calls and never opens a file.
- When the signature changes, the view reloads in a worker.

**After a reload:**
- The active pane and every filter are kept.
- Each table keeps its cursor on the same key: `seq` for the timeline, `run_id` for runs,
  record `id` for records, thread root id for mail. The timeline follows the end when its
  cursor was on the last row.

**Terminal campaigns:**
- When `terminal` is true, no interval is ever created.
- If a live campaign becomes terminal on a reload, the interval is stopped.
- After the initial load, a terminal popup does no file I/O except on a key press or a row
  highlight that needs a file the view does not hold: a run pane, a gate log tail, or an idea.
  Each of these reads that one file, once, in a worker.

### Overview pane

1. **Header.** It shows the SPEC-0246 header lines 1–5, unchanged, from `load_campaign`, then
   one history line:

   ```
   outcome <merged|handed_off|abandoned|live>  wall <h:mm>  active <h:mm>  conductor $<x>
   [merged <sha7>] [pr <url>]
   ```
2. **Phase table.** It is a `DataTable` with one row per phase that has `visits > 0`, in
   `history.PHASES` order. The columns are:

   | Column | Content |
   |---|---|
   | `phase` | the phase name |
   | `visits` | the number of visits |
   | `wall` | `h:mm:ss` |
   | `wall%` | share of `header.wall_s` |
   | `run` | run time |
   | `gate` | gate time |
   | `wait` | `gate_wait_s` |
   | `down` | `runner_down_s` |
   | `other` | the remainder |
   | `cost` | `$x.xx` |
   | `cost%` | share of the sum of phase costs |
   | `runs` | the number of runs |
   | `dom` | the dominance tags |

   **Dominance.** `dom` lists the tags that apply to the row: `wall` (`dominant.wall`),
   `cost` (`dominant.cost`) and `working` (`dominant.working`). Every tagged row is drawn
   bold-reverse. The tags carry the meaning in text, so it does not depend on colour.

   **Dominant line.** Above the table, one line reads:

   ```
   dominant: <phase> by wall (<p>%) · <phase> by cost (<p>%)[ · working <phase> (<p>%)]
   ```

   A share is `—` when its denominator is 0.
3. **Bar breakdown.** For each phase row there is one line: `<phase:9> <bar> <wall>`.
   - `bar(phase, max_wall, width)` is pure. The bar's length is proportional to
     `wall_s / max_wall` over `width` cells. It uses eighth blocks (`▏▎▍▌▋▊▉█`) for the
     fractional end cell and never draws fewer than 1 cell for a non-zero phase.
   - Inside the bar, the segments are stacked in this fixed order and character set:

     | Segment | Character |
     |---|---|
     | run | `█` |
     | gate | `▓` |
     | gate wait | `▒` |
     | runner down | `░` |
     | other | `·` |

     Segment widths are rounded so that they sum to the bar's length.
   - A legend line follows.
   - `width` is the pane width minus 30, with a minimum of 10.
4. **Cost per run.** `sparkline(costs, width)` is pure and maps each run's `cost_usd`, in
   `started_at` order, to `▁▂▃▄▅▆▇█`, scaled to the maximum.
   - A `None` cost is shown as a space.
   - When there are more runs than `width`, consecutive runs are bucketed evenly and each
     bucket shows its maximum.
   - The line is `cost/run <spark>  n=<runs> max $<x> total $<y>`.
5. **Gaps.** The pane ends with `gaps:` and one line per `history.gaps` entry, or `no gaps`.

### Timeline pane

**Rows.** There is one row per `history.timeline` entry. The columns are `seq` (blank for
non-ledger rows), `time` (`MM-DD HH:MM:SS`), `phase`, `by`, `kind` and `summary`.

**Grouping by loop.**
- A separator row `── loop N ──` is inserted wherever `loop` changes, with `loop 0` covering
  everything before the first IMPLEMENTING.
- The cursor skips separator rows: `up`/`down`, `g`/`G` and the highlight all land on events.
- `[` / `]` jump to the previous or next loop separator's first event.

**Filter.** `f` cycles `all → transitions → runs → gates → findings → notes → decisions → all`.
- The first five groups keep SPEC-0246's membership exactly:

  | Group | Event types |
  |---|---|
  | `transitions` | `open`, `transition`, `park`, `resume`, `priority`, `close`, `admitted_after_revision` |
  | `runs` | `run.start` / `run.end` (merged into `run` by the history), `cost` |
  | `gates` | `gate` |
  | `findings` | `finding`, `followup` |
  | `notes` | `note`, `commit`, `backlog`, and any ledger type not listed |

- `decisions` is new. It holds the non-ledger rows the history interleaves (conductor
  decisions, escalations, answers and mail), plus the ledger types `human_decision` and
  `triage`.
- The status line reads `filter <group> · <shown>/<total> events · loop <n>/<loops>`.

**Detail pane.**
- For ledger rows, the detail pane shows `entry_detail(cfg, cid, entry)` as SPEC-0246 defines
  it. That covers:
  - a run's `campaign_run` fields, verdict lists, stderr tail and paths;
  - a gate's per-gate results and the last 40 lines of each failed gate's log;
  - the unclipped payload for every other type;
  - `(missing: …)` and `(run unavailable: …)` inline, without ever raising.
- For non-ledger rows, it shows the matching record: every field, its `links` and its
  `linked_by`.

**Review panel and cursor.**
- `v` toggles the detail pane to `review_text(cfg, state)`, with SPEC-0246's content and
  placeholders unchanged.
- The cursor opens on the last event.

### Runs pane

**The table.** A `DataTable` over `history.runs` in `started_at` order. The columns are:

| Column | Content |
|---|---|
| `run` | the run id |
| `persona` | the persona |
| `phase` | the phase |
| `loop` | the loop |
| `model` | the model |
| `started` | the start time |
| `dur` | the duration |
| `cost` | the cost |
| `budget` | `budget_frac` as % |
| `status` | `status` / `verdict_status` |
| `stream` | `●` if `has_stream`, else `-` |

The cursor opens on the last run.

**The run pane** shows, for the highlighted run, loaded in a worker on highlight:
1. **Verdict.** It shows `verdict_status`, the `summary`, then the `must_fix` and `blockers`
   lists, `changed_files` and `tests_run`, all from `runs.campaign_run(cfg, cid, run_id)`. A
   `ValueError` is shown inline as `(run unavailable: <error>)`.
2. **stderr.** The stderr tail (`runs.STDERR_TAIL_LINES`, as `campaign_run` returns it).
3. **Stream.**
   - **With a stream** (`has_stream`): `stream_tail(run_dir, n=200)` reads `stream.jsonl` with
     `tail.read_lines(path, 0)`. It feeds every line through one `tail.Renderer(cwd=…,
     budget_usd=…)`, the same price lookup `tail.tail` uses, and keeps the last 200 rendered
     `Text` lines in a `deque(maxlen=200)`. It returns those lines, the byte offset reached and
     the renderer.
     - The renderer is fed from the start because it is stateful: it maps tool-use ids to
       names. Streams average about 140 KiB, so this is cheap.
     - A stream over 16 MiB is read from its last 16 MiB, starting at the first newline. The
       pane then notes `(stream truncated: showing the end of <size>)`, and tool names that
       were opened before the cut show as their ids.
     - The heading is `stream · last 200 of <n> lines`, and the renderer's `cost_line` is
       appended.
   - **Without a stream** (runs before SPEC-0362): the pane shows
     `(no stream.jsonl: run predates SPEC-0362) — result text:` followed by the `result` text
     from `result.json`, the same source `tail._summary_without_stream` uses. A missing
     `result.json` shows `(missing: <path>)`.

**Live follow:**
- Follow is active only while all of these are true:
  - the popup is the top screen;
  - the Runs pane is the active pane;
  - the highlighted run has a stream;
  - `tail._ended(run_dir)` is false.
- While active, a 1 s interval calls `tail.read_lines(path, offset)` in a worker. It feeds the
  new lines into the same renderer, appends to the deque and scrolls to the end only if the
  pane was already scrolled to the end.
- Follow stops, and its timer is stopped (not merely skipped), when any of these happens:
  - the pane is switched away;
  - the run highlight changes;
  - another screen is pushed on top;
  - the run ends (the remaining lines are drained once).
- When the pane becomes active again, follow resumes from the stored offset, so the lines it
  missed appear.
- The status line shows `following` while active.

### Findings & decisions pane

**The table.** A `DataTable` over `history.records`, newest first. The columns are `time`,
`agent`, `kind`, `severity`, `status`, `title` and `link`. `link` shows `linked_by` as `=`
(field), `~` (inferred) or `?` (text).

**Labels.** `status` `fixed` is labelled `not re-raised`, as the epic decided.

**Filtering:**
- `/` opens a `TextPrompt` for a filter string. It is parsed by the pure
  `parse_record_filter(s)` in the events-tab parser style: the tokens `agent:` `kind:`
  `severity:` `status:`, each comma-separated and repeatable, ANDed across keys and ORed within
  a key; any remaining words are a case-insensitive substring match over title and text.
- `record_filter(rows, flt)` applies the parsed filter.
- `\` clears the filter.
- The status line reads `<shown>/<total> records · filter <s>`.

**The record pane** shows the full title and text, every `links` entry and `linked_by`.

**Jumps:**
- **`r` (producing run).** It needs `links.run_id`. It switches to Runs and selects that run.
  When the run id is absent or not in `history.runs`, it notifies `no run linked`.
- **`d` (resolving decision)** follows `resolve_decision(view, record)`:
  1. If `links.resolved_by` names a record id in `history.records`, it selects that record in
     this pane and clears a filter that would hide it, notifying `filter cleared`.
  2. Otherwise, if the id has the form `ledger:<cid>:<seq>` for this campaign, it switches to
     Timeline and selects that `seq`, with the filter reset to `all`.
  3. Otherwise it notifies `no resolving decision` (when `resolved_by` is null) or
     `resolving decision <id> is outside this campaign`.
- **`i` (follow-up idea)** needs `links.followup_idea`. It opens a read-only text modal with:
  - the idea id and heading;
  - the checklist line carrying this finding's `nwfu:<key12>` marker, when it is present;
  - the idea's summary.

  The idea is read in a worker through the same org-idea reader the ideas tab uses
  (`tui/model.py`). An unknown id shows `idea <id> not found`, and no link shows
  `no follow-up idea`.

### Mail pane

`mail_threads(history)` is pure. It groups the records whose `kind` is `mail` into threads by
following `links.thread`, or `re`, back to the root message.

- **Threads table.** Root time, from → to, kind, subject (the first line of the root text),
  message count and `linked_by`. It is newest-activity first.
- **Thread pane.** Every message in order: `time from → to kind`, then the text as plain text.
- **Read-only.** There is no reply or send. A hint line reads
  `reply from the Mailbox tab (4)`.
- An empty campaign shows `no mail for this campaign`.

### Actions

**Queue actions.**
- `p`, `u`, `P`, `A` call `QueueActions.run_queue_action(action, cid, state, view.actions)`:
  the SPEC-0241 prompt → plan → confirm → apply flow, through `agentic_model.queue_action`.
- After an apply, the view reloads. After `abandon`, the popup stays open on the now-terminal
  campaign, and its poll stops.
- An action missing from `view.actions` notifies `<action> not available in <state>`.

**Escalations.**
- `e` answers an open escalation of this campaign by choosing an option; `E` answers it with
  free text. Both go through `answer_escalation(app, cfg, eid, action)` (`action` is
  `"choose"` or `"text"`), which is `MailboxView._escalation_flow`'s body moved verbatim.
  `MailboxView` now calls it too.
- `view.open_escalations` lists the open escalations whose `campaign` is this cid. When there
  are none, the keys notify `no open escalation`. When there are several, a `ChoiceScreen`
  picks one first.
- `e` on an escalation with no options falls back to `"text"`.
- Withdraw is not offered here.

**No other write path.** The popup calls nothing that writes, other than these two flows.

### Safety and keyboard

- **Untrusted text.** Titles, finding text, summaries, mail, stderr and stream lines are never
  parsed as Rich markup
  ([SPEC-0265](./0265-operator-surfaces-survive-bad-data-and-never.md)). Every cell and pane
  is built with `Text(...)` / `Text.assemble`, and stream lines are the `Text` objects the
  `Renderer` returns.
- **Missing or partial data** renders a placeholder or a `gaps` line, and never an exception.
- **Keys.** Everything is reachable from the keyboard:

  | Key | Does |
  |---|---|
  | `1`–`5` or `tab` / `shift+tab` | switch panes |
  | `f`, `v`, `[`, `]`, `g`, `G` | Timeline |
  | `/`, `\`, `r`, `d`, `i` | Findings & decisions |
  | `p`, `u`, `P`, `A`, `e`, `E` | actions |
  | `escape`, `q` | close |

  A footer lists the active pane's keys. Mouse clicks on tabs and scrolling are shortcuts
  only.
- **Size.** The CSS is `CampaignDetail > #detail-frame { width: 96%; height: 94%; }`, centred
  on a dimmed backdrop.

### Retiring SPEC-0246

`campaign_screen.py` is deleted. The non-screen helpers (`load_campaign`, `entry_detail`,
`review_text`, `campaign_signature`) stay in `agentic_model.py`, and
`tests/test_tui_campaign_model.py` stays unchanged.

`tests/test_tui_campaign_screen.py` is replaced by `tests/test_tui_campaign_detail.py`. Each
of its cases is ported:
- open, then Esc with the cursor kept;
- `--campaign` with a full id, a prefix, an unknown id and an ambiguous id;
- `f` cycling and the status counts;
- `v`;
- a live append;
- park end to end;
- monkeypatches that make `events.sync`, `cursor_set` and `reader_lock` raise.

When this spec is closed, SPEC-0246 is set `status: superseded` with
`superseded_by: SPEC-0407`.

## Alternatives considered

- **Keep `CampaignScreen` as a pushed `Screen` and add tabs to it.** This lost because the
  human asked for a popup, and the epic decided on a modal (Decision 4). At 96 % × 94 %, the
  modal still leaves the timeline nearly full height. SPEC-0246 rejected the modal for height;
  the near-full size answers that.
- **Render the stream tail from a fresh `Renderer` over only the last 200 raw lines.** This
  lost because the renderer maps tool-use ids to names across the whole stream, so a cold tail
  would label tools by id. Feeding the whole file is cheap at the observed sizes, and the
  16 MiB cut bounds the worst case.
- **Colour-only dominant highlight.** This lost because the dominance must survive monochrome
  terminals and text snapshots. The `dom` tags and the `dominant:` line carry it as text.
- **Separate filter keys per field (`a`/`k`/`s`/`t` cycling).** This lost to one `/` filter
  string. The events tab already uses that parser style, children 6 and 7 use the same syntax,
  and it allows several values per key.
- **Copy `_escalation_flow` into the popup.** This lost because two copies of a write path
  drift. Moving it into a shared coroutine keeps one SPEC-0235 path.

## Acceptance criteria

- [ ] **AC1. Opening and closing from the queue.**
  - On the queue tab, Enter on a campaign row and `Pilot.double_click` on another row each open
    `CampaignDetail` for that row's campaign.
  - A double-click on a section or header row opens nothing.
  - Esc and `q` close the popup and leave the queue cursor on the same campaign id, even when
    the queue reloaded while the popup was open.
- [ ] **AC2. The shared opener.**
  - A minimal app with a `DetailOpener` table opens the popup on Enter and on double-click.
  - `open_campaign_detail(cid, focus={"tab": "records", "record": id})` opens on Findings &
    decisions with that record selected, and the run form opens on Runs with that run selected.
  - An unknown focus id opens Overview and notifies `not found: <id>`.
- [ ] **AC3. `nw tui --campaign`.**
  - It opens the popup for a full id or a unique prefix, including a merged campaign.
  - An unknown or ambiguous id exits with a `ClickException` before the TUI starts, and the
    ambiguous case lists the matches.
- [ ] **AC4. The frame.** At 160×48, the popup frame measures 96 % ± 1 cell × 94 % ± 1 cell of
  the screen, and it has the five panes Overview, Timeline, Runs, Findings & decisions and
  Mail, which `1`–`5` select.
- [ ] **AC5. Overview numbers.**
  - For every fixture campaign, the Overview's phase rows show `visits`, `wall_s`, `run_s`,
    `gate_s`, `gate_wait_s`, `runner_down_s`, `other_s`, `cost_usd` and `runs` equal to
    `nw campaign history <cid> --json` `phases` (after formatting).
  - The shares are computed against `header.wall_s` and the sum of phase costs.
  - The header line shows `outcome`, `wall`, `active` and `conductor` from the history header.
- [ ] **AC6. Dominance.**
  - The rows named by `dominant.wall`, `dominant.cost` and `dominant.working` carry the `wall`,
    `cost` and `working` tags and are drawn bold-reverse.
  - The `dominant:` line names the same phases, including `working` only when the history sets
    it.
  - A zero denominator renders `—`.
- [ ] **AC7. Bars and sparkline.**
  - `bar()` returns a bar whose length is proportional to `wall_s` (±1 cell), is at least
    1 cell for a non-zero phase, and has segment widths summing to its length, in the
    documented character order.
  - `sparkline()` maps the maximum cost to `█`, `None` to a space, and buckets runs beyond
    `width` by maximum.
  - The `gaps` lines equal `history.gaps`, or read `no gaps`.
- [ ] **AC8. Timeline.**
  - It lists every `history.timeline` entry in order, with `── loop N ──` separators wherever
    the loop changes. The cursor never rests on a separator, and `[` / `]` jump between loops.
  - `f` cycles the seven groups. The five SPEC-0246 groups keep their exact membership for
    every `schemas.EVENT_KEYS` type plus an unknown type, and `decisions` holds the non-ledger
    rows plus `human_decision` and `triage`.
  - The status line shows the filter, `<shown>/<total>` and the loop.
  - The cursor opens on the last event.
- [ ] **AC9. Timeline detail and review.**
  - The detail pane shows `entry_detail` for ledger rows, with the SPEC-0246 content, including
    `(missing: …)` and `(run unavailable: …)` without an exception.
  - Non-ledger rows show the record's fields, links and `linked_by`.
  - `v` shows `review_text` with SPEC-0246's content and placeholders.
- [ ] **AC10. Run pane.** For a run with `stream.jsonl`, it shows:
  - the verdict status and summary, `must_fix`, blockers, changed files and tests run;
  - the stderr tail;
  - exactly the last 200 lines that a `tail.Renderer` fed the whole stream produces, which
    equal those lines computed directly in the test.

  For a run without a stream, it shows the `predates SPEC-0362` note and the `result.json`
  `result` text. A missing `result.json` shows `(missing: …)`.
- [ ] **AC11. Live follow.**
  - With a running run selected on the visible Runs pane, a line appended to its
    `stream.jsonl` appears within 2 s, with no key press.
  - After switching to another pane, the follow timer is stopped, and over the next 2.5 s
    `stream.jsonl` is not opened.
  - On switching back, the lines appended in the meantime appear.
  - Follow stops when the run's `exit.json` appears.
- [ ] **AC12. Records and filtering.**
  - Findings & decisions lists every `history.records` entry newest first, with `fixed`
    labelled `not re-raised`.
  - `parse_record_filter` and `record_filter` give the documented AND/OR and full-text
    semantics on table-driven cases.
  - The status line shows `<shown>/<total>`.
- [ ] **AC13. Record jumps.**
  - On a finding with a `run_id`, `r` switches to Runs with that run selected.
  - On a waived finding, `d` selects its resolving `triage` or `human_decision` record, or the
    Timeline `seq` of that ledger event.
  - On a filed finding, `i` shows the follow-up idea's id, heading and its `nwfu:` line.
  - Each missing link produces its documented notice, and none of them raises.
- [ ] **AC14. Mail.** The Mail pane lists the campaign's mail grouped into threads, matching
  `mail_threads(history)`. The thread pane shows every message in order, and the pane offers
  no send or reply binding.
- [ ] **AC15. Queue actions.**
  - `p`/`u`/`P`/`A` in the popup run the SPEC-0241 flow through `QueueActions` /
    `queue_action`, and the header reflects the new state after an apply.
  - After `abandon`, the popup stays open, and its poll timer is stopped.
  - An action not in `allowed_actions` is refused with a notice.
- [ ] **AC16. Escalation answers.**
  - `e` / `E` on a campaign with an open escalation run `answer_escalation`, and they reach
    `escalations.plan_answer` / `apply_answer` with the same arguments the Mailbox tab passes.
  - With no open escalation, they notify `no open escalation`.
  - The Mailbox tab's escalation tests pass unchanged against the moved coroutine.
- [ ] **AC17. Polling.**
  - On a live campaign, a ledger append is reflected within one poll interval (2 s) with no key
    press. The active pane, the filters and every table cursor (by key) are kept, and the
    timeline follows the end when it was on the last row.
  - On a terminal campaign, no interval timer exists.
  - After the load completes, a terminal popup left idle on Overview for 2.5 s does no `stat`
    and no `open` under its campaign dir.
  - The popup never calls `events.sync`, `cursor_set` or `reader_lock`.
- [ ] **AC18. Bad data.**
  - A finding title, mail text and stream line containing `[bold red]x[/]` and
    `[link=file:///etc]y[/link]` render literally.
  - A campaign with a corrupt `run.json` and a missing gate log opens, shows the history's
    `gaps`, and raises nothing.
  - A `load_detail` failure shows `history unavailable: …`, and Esc still closes the popup.
- [ ] **AC19. Popup open budget.** On child 3's 500-campaign clone, the time from
  `open_campaign_detail` to the Overview being painted is ≤ 1.5 s (the 500 ms budget × 3) with
  a warm history cache.
- [ ] **AC20. Write paths.**
  - Over a full Pilot session through all five panes and every jump key, with no action
    confirmed, every file in the data dir is byte-identical before and after.
  - `campaign_screen.py` no longer exists, and no module imports `CampaignScreen`.
- [ ] **AC21. Snapshot.** A pyte text snapshot of the popup's Overview at 160×48 on a merged
  multi-loop fixture campaign matches the committed golden file. The golden file is regenerated
  only with `NW_UPDATE_SNAPSHOTS=1`.

## Test plan

**Automated tests.** All of them run under plain `uv run pytest`, with no Textual skip guards
(child 3), on `tests/fixtures/agentic_history/` copied by `tests/tui_harness.py`.

- **`tests/test_tui_campaign_detail_model.py`** (new, pure, no app):
  - **Overview numbers:** `load_detail` over every fixture campaign. The phase rows equal
    `history.campaign_history` `phases`, and the shares are correct (AC5).
  - **Dominance:** the tags and the `dominant:` line for these cases (AC6):
    - wall and cost dominant in the same phase;
    - wall and cost dominant in different phases;
    - a parked-dominant campaign with `working`;
    - a zero-cost campaign.
  - **`bar()`:** table cases covering a zero phase, a tiny phase (1 cell), the full width,
    segment sums and the character order (AC7).
  - **`sparkline()`:** `None` costs, a single run, runs exceeding the width, and the maximum
    mapping to `█` (AC7).
  - **Timeline:** loop grouping and separator placement; group membership for every
    `schemas.EVENT_KEYS` type, an unknown type and each non-ledger kind (AC8).
  - **Record filter:** `parse_record_filter` / `record_filter` table cases: one key, several
    values, several keys, free text, an unknown key treated as text, the empty string (AC12).
  - **`resolve_decision`:** a record target, a ledger-seq target, `resolved_by` null, and an
    id outside the campaign (AC13).
  - **`mail_threads`:** `re` chains, an orphan reply, stamped and text-linked messages (AC14).
  - **`stream_tail`:**
    - a generated 500-event stream gives the last 200 lines equal to a direct `Renderer` run;
    - a stream over 16 MiB gives the truncation note;
    - no stream gives the `result.json` text;
    - a missing `result.json` gives `(missing: …)` (AC10).
  - **Markup:** the payloads from AC18 render literally in every builder (AC18).
- **`tests/test_tui_campaign_detail.py`** (new, Pilot on the real `IdeaDesk`). It ports every
  case of the retired `tests/test_tui_campaign_screen.py` and adds:
  - **Queue opening:** Enter and `Pilot.double_click` from the queue, a double-click on a
    section row, Esc and `q` returning to the same campaign id after a forced queue reload
    (AC1).
  - **Opener:** a minimal `App` with a `DetailOpener` `DataTable`, the `focus` forms and an
    unknown focus (AC2).
  - **`--campaign`:** a full id, a prefix, a merged campaign, unknown and ambiguous ids, using
    a CLI runner with `T.run` patched for the valid cases (AC3).
  - **Frame:** the size measured from the widget region at 160×48, and the `1`–`5` panes (AC4).
  - **Overview:** visible text compared against `nw campaign history <cid> --json` run on the
    same data dir, for one campaign of each outcome (AC5, AC6).
  - **Timeline:** `f`, `[`, `]`, `v`, the detail pane for a run, a gate with a missing log,
    and a non-ledger row (AC8, AC9).
  - **Runs:** a SPEC-0362 run (stream tail) and a pre-SPEC-0362 run (result text) (AC10).
  - **Live follow (AC11):**
    - a fixture run made "running" by deleting its `exit.json` in the copied data dir;
    - lines appended and awaited with `pilot_until`;
    - `open` patched to count opens of the stream path during the hidden-pane 2.5 s window;
    - then `exit.json` restored.
  - **Jumps:** `r`, `d`, `i` on fixture findings that are linked by field, inferred and filed
    (the triage-waive campaign), plus each "no link" notice (AC13).
  - **Mail:** the Mail pane on the campaign with an answered escalation (AC14).
  - **Actions:** `p` end to end on a live copy, then `A` with the poll timer asserted stopped,
    and a refused action (AC15).
  - **Escalations:** `e` with `escalations.plan_answer` / `apply_answer` spied, on a seeded
    open escalation; no escalation; several escalations, through the choice screen (AC16).
  - **Polling (AC17):**
    - a ledger append on a live campaign, with the cursors and filters kept;
    - a terminal campaign with no timer;
    - `os.stat` / `open` counting under the campaign dir over 2.5 s idle;
    - `events.sync`, `cursor_set` and `reader_lock` monkeypatched to raise.
  - **Bad data:** a corrupt `run.json`, a missing gate log, `load_detail` patched to raise
    (AC18).
  - **No writes:** a hash of every data-dir file before and after a full no-confirm session,
    and an import check that `nightwright.tui.campaign_screen` is gone (AC20).
- **`tests/test_tui_campaign_detail_perf.py`** (new). It uses child 3's scale-clone helper to
  build 500 campaigns, warms the history cache, and asserts that the popup opens to a painted
  Overview in ≤ 1.5 s, measured with `time.perf_counter` around `open_campaign_detail` plus a
  `pilot_until` on the Overview's phase table (AC19).
- **Snapshot.** A pyte snapshot through child 3's helper:
  `detail_overview_160x48.txt` for the merged multi-loop fixture campaign (AC21).
- **Mailbox regression.** `tests/test_tui_mailbox_tab.py` and `tests/test_tui_escalations_tab.py` run
  unchanged after `_escalation_flow` moves (AC16).

**Manual verification.** Run on the real data dir and record the results in this spec's
*As shipped*:
1. Run `uv run nw tui`, press `3`, and double-click a running campaign.
   - Check that the Overview's dominant phase is believable against `nw campaign history <cid>`.
   - Check that the Timeline's loops match `nw campaign log <cid>`.
   - On Runs, select the running run and check that the stream tail moves. Switch to Overview
     and back, and check that it caught up.
2. Press Esc and check that the cursor is on the same campaign.
3. Run `uv run nw tui --campaign <a merged campaign with waived findings>`. On Findings &
   decisions, filter `status:waived`, press `d` and `r`, then on a filed finding press `i`.
4. Repeat steps 1–3 with the keyboard only (Enter instead of double-click).
5. Leave a terminal campaign's popup open for 1 minute. Check with
   `strace -f -e trace=openat,statx -p <pid>` that nothing under its campaign dir is touched.

**Regression guard.**
- These pass at the queue's close gate (SPEC-0378):
  - the affected tests (`python3 tools/affected_tests.py --run uv run pytest -q`);
  - `tests/test_tui_queue_tab.py`, `tests/test_tui_campaign_model.py` and
    `tests/test_tui_mailbox_tab.py`;
  - `tests/test_tui_escalations_tab.py`.
- `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. This spec lands after child 2 (the history layer and fixture) and child 3 (the harness) of
   SPEC-0405. If child 4 has not landed yet, the popup is wired to the current queue table
   through `DetailOpener`, and child 4 keeps the mixin when it replaces the layout.
2. In one change:
   - add `CampaignDetail`, the pure helpers and `answer_escalation`;
   - switch `IdeaDesk`'s `CampaignSelected` handler and `--campaign` to the popup;
   - delete `campaign_screen.py`, and port its tests.
3. There is no data migration. The popup reads the history cache, which builds lazily, and old
   campaigns render with `~`/`?` link markers.
4. At close:
   - set SPEC-0246 to `status: superseded` with `superseded_by: SPEC-0407`;
   - regenerate the ledger;
   - note the opener API (`DetailOpener`, `open_campaign_detail`, `focus`) in *As shipped* so
     that children 6 and 7 use it.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed: the new tests pass, the manual steps are performed and recorded.
- [ ] No regressions: the affected tests and the listed TUI suites are green at the close gate.
- [ ] Spec body updated to match what shipped, including the final name of child 2's signature
      function if it differs.
- [ ] SPEC-0246 set `superseded` with `superseded_by: SPEC-0407`.
- [ ] `docs/LEDGER.md` regenerated, and `python3 tools/spec_lint.py` exits 0.

## Open questions

None. These were decided while writing the spec:

| Question | Decision | Why |
|---|---|---|
| How are records filtered? | one `/` filter string | it matches children 6 and 7 |
| How is the stream tail fed? | the whole stream, cut at 16 MiB | the renderer needs the tool-id map |
| Does `fixed` show as `fixed`? | no, it is labelled `not re-raised` | |
| Is mail read-only here? | yes | |
| Can escalations be withdrawn from the popup? | no | |
| How is the cursor restored after Esc? | by campaign id | the queue reloads under the modal |
