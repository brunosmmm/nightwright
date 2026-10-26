---
id: SPEC-0241
title: "TUI queue tab: live view of the agentic queue with park, resume, prioritize and abandon"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, tui, queue, textual]
source_idea: IDEA-037
depends_on: [SPEC-0185, SPEC-0222]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), Breakdown
line "TUI watch tab over `wt.queue.v1`" (promoted from `IDEA-037`).

`wt tui` (`src/wt/tui/app.py`, class `IdeaDesk`) has two tabs today: `ideas` and the read-only
`hub` ([SPEC-0104](./0104-optional-read-only-hub-triage-tab.md)). The agentic queue can only
be watched with `wt queue watch`, a Rich live render
([SPEC-0185](./0185-rich-human-output-everywhere-and-a-queue-watch.md), `cli_queue._dashboard`
over `queue.dashboard_payload`). To act on a campaign, the human leaves the watch and types
`wt queue park|resume|prioritize|abandon <long campaign id> --yes`.
[SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md) splits parks into
`needs_you` and `conductor_handling` in `dashboard_payload`. This tab shows that split.

This is the first of the agentic TUI specs. The campaign detail view
(`IDEA-084`, specced separately), mailbox, escalations and events tabs build
on the tab plumbing defined here.

## Goals / Non-goals

**Goals**
- G1. A `queue` tab in `wt tui` with the sections of `wt queue watch`: Needs you, Conductor
  handling, Active, Waiting, Backlog, Recently delivered. A header line shows runner and lease
  health, slots, the live conductor and stewards, and today's spend.
- G2. Live: the tab refreshes when the queue changes, without the human pressing a key, and
  without becoming an event-stream reader. It must never steal the conductor's cursor or lock.
- G3. Per-row actions (park, resume with an optional note, prioritize, abandon), each confirmed.
  They go through the same `queue.plan_*` / `apply_*` functions as the CLI.
- G4. Tab plumbing the later agentic tabs reuse: number keys to switch tabs, `wt tui --tab`, and
  a row-selection message that the campaign detail view consumes.

**Non-goals**
- The campaign detail view (`IDEA-084`). Until it lands, `enter` shows a CLI hint.
- `wt queue add` or `merge-request` from the TUI. Admission needs a spec review and a dry run;
  it stays on the CLI.
- Running or stopping the runner from the TUI.
- Mailbox, escalations, events: separate specs.

## Decision

A pure view model `src/wt/tui/agentic_model.py` (no Textual import) turns
`queue.dashboard_payload` and `events.followers` into rows and a header line. It computes a
cheap change signature and wraps the four actions. A widget module `src/wt/tui/queue_tab.py`
holds `QueueView` (a container with a `QueueTable(DataTable)` and a status line) and its
bindings. `IdeaDesk.compose` adds `TabPane("queue", id="tab-queue")` holding a `QueueView`,
and the existing `TabActivated` handler loads and focuses it.

## Design

### Tab plumbing (`app.py`)

- New app bindings: `1` → `tab-ideas`, `2` → `tab-hub`, `3` → `tab-queue`, all through one
  `action_tab(name)`. `h` keeps toggling ideas and hub (SPEC-0108). The digits are unbound today.
- `_tab_activated` gains a `tab-queue` branch that calls `QueueView.activate()` (load + focus).
  Leaving the tab calls `QueueView.deactivate()`, which pauses its timers.
- `wt tui --tab [ideas|hub|queue]` (default `ideas`) passes `tab=` through `T.run` to
  `IdeaDesk.__init__`, and `on_mount` activates that tab. Later specs extend the choice list.
- Bindings specific to a tab live on its focused table subclass. Textual resolves the focused
  widget's bindings before the app's, so `p` and `r` mean park and refresh on the queue table
  while the ideas list keeps its own meaning.
- **Idea-scoped app actions are gated.** Keys the queue table does not shadow would otherwise
  resolve through `IdeaDesk.BINDINGS` and act on the selected idea of the hidden ideas list.
  `IdeaDesk.check_action(action, parameters)` returns `False` for the idea-scoped actions
  (`search`, `toggle_all`, `filter`, `cycle_sort`, `toggle_desc`, `toggle_wide`,
  `toggle_tree`, `refresh_desk`, `log`, `summary`, `questions`, `capture`, `retitle`,
  `explored`, `metadata`, `clock_toggle`, `close_idea`) unless `tab-ideas` is active. Textual
  then neither runs them nor shows them in the footer. The keys that stay global are `q`
  (quit), `1`–`3` (and the later tabs' digits), `h` (ideas/hub toggle) and `escape`.

### View model (`agentic_model.py`)

`QueueRow` is a frozen dataclass: `section`, `campaign`, `spec` (spec id or idea id), `title`,
`project`, `state`, `meta`, and `actions` (a tuple of the allowed action names).

`load_queue(cfg, *, project=None) -> tuple[list[QueueRow], str]` calls
`queue.dashboard_payload(cfg, project=project)` once and emits sections in this order (an empty
section emits no rows):

| section | source list | `meta` text |
|---|---|---|
| `needs you` | `needs_you` | `<park.reason> · <age> · <park.detail first line>` |
| `conductor` | `conductor_handling` | same, plus ` · overdue` when SPEC-0222 marks it |
| `active` | `active` | `<state> · <persona> <elapsed>` when `activity.kind == "run"`, else `<state> · gates` or `<state>`; then ` · loops <loops> · $<cost>` |
| `waiting` | `waiting` | `waiting <age> → <resume_to>` |
| `backlog` | `backlog` | `#<position> p<priority> · ready` or `· blocked by <ids>`, plus ` · <size>/<tone>` when sized |
| `delivered` | `delivered` | `<state> <merged_sha[:7] or pr_url> · $<cost> · <age> ago` |

- Ages are rendered with `cli_queue._dur` / `_ago`, the formatters `wt queue watch` uses, so both
  surfaces read the same.
- A payload with no rows yields one placeholder row (`section ""`, meta `queue is empty`) with no
  actions.

`actions` is computed by `allowed_actions(state, park_requested)`. It mirrors the state guards
of the plan functions, so the footer never offers an action that the plan would refuse:
- `park`: not terminal, not PARKED, and `park_requested` not set (a second `p` would only
  overwrite the pending deferred park; the row's `meta` already shows it);
- `resume`: PARKED;
- `prioritize`: QUEUED, WAITING, PARKED;
- `abandon`: PARKED, QUEUED, WAITING.

`park_requested` changes nothing else: `resume` stays unavailable because the state is not
PARKED. The plan functions still re-check every guard.

`header_line(payload, followers) -> str` returns one line:
`runner <pid|down|stale> · hb <heartbeat_age_s>s · slots <busy>/<total> · conductor <pid|none>
· stewards <names|none> · today $<spend_today_usd>`.
- `followers` is `events.followers(cfg)`.
- The conductor is the `events` reader named `conductor` with `held` true.
- Stewards are the held `mailbox:steward:<P>` readers.
- `draining` (SPEC-0224) appends ` · draining`.
- Payload paths: `payload["lease"]` gives `held`, `pid`, `stale`, `heartbeat_age_s` and
  `draining`; `payload["slots"]` gives `busy` / `total`; `payload["totals"]["spend_today_usd"]`
  gives the spend. The runner cell is `down` when `lease.held` is false, `stale` (with the pid,
  `stale <pid>`) when `lease.stale` is true, else the pid. `hb` shows `-` when
  `heartbeat_age_s` is None.

`queue_signature(cfg) -> tuple` is built from `stat()` calls only:
- `(size, mtime_ns)` of `events.events_path(cfg)`;
- `(size, mtime_ns)` of `lease.lease_path(cfg)` (the file `lease.read_lease` reads);
- the sorted `(name, mtime_ns)` of every `campaigns/*/state.json`.

A missing path contributes `None`. A follower taking or dropping its reader lock usually
changes no mtime, so conductor and steward changes in the header show up on the 15 s reload
(below), not on the 2 s poll. That is accepted.

`queue_action(cfg, action, campaign, **kw) -> dict` dispatches the four actions:
- `park` → `plan_park(cfg, c, reason)` then `apply_park`;
- `resume` → `plan_resume(cfg, c, note=note)` then `apply_resume`;
- `prioritize` → `plan_prioritize(cfg, c, priority)` then `apply_prioritize`;
- `abandon` → `plan_abandon(cfg, c, keep_worktree=keep)` then `apply_abandon`.

`plan_queue_action(...)` returns the plan alone, for the confirmation text. Both raise the plan
functions' `ValueError` unchanged.

### Widget (`queue_tab.py`)

- Columns: `section` (dim, printed only on a section's first row, as the hub does), `campaign`
  (the id without its `SPEC-NNNN-` prefix when the spec column already shows it), `spec`, `what`
  (the title, clipped to a measured budget like `load_hub`), and `meta` (clipped, dim). The
  status line under the table shows `header_line`.
- **Refresh:**
  - while the tab is active, `set_interval(2.0)` compares `queue_signature`, and on a change
    reloads in a thread worker (`run_worker(..., thread=True)`), as SPEC-0134 does for the desk;
  - a second interval of 15 s reloads anyway, because ages and the runner heartbeat change
    without any file write;
  - rows are keyed by `(section, campaign)`, because a campaign with a deferred park
    (`park_requested`) is listed both under its park section and under `active`
    (`dashboard_payload` adds it to the attention list and then falls through). A reload
    restores the cursor to the same `(section, campaign)`, else to the first row of that
    campaign, else to the same row index clamped to the table;
  - a reload that raises `OSError` or `ValueError` shows `queue failed: <error>` in the status
    line and keeps the previous rows.
- **Read-only by construction:** the tab never calls `events.sync`, `events.cursor_set`,
  `events.reader_lock`, `events.follow` or `mailbox.follow`. A follower syncs the event stream.
  The tab only reads state and the files those followers write. Choice: a TUI that syncs or holds
  a cursor would compete with the conductor's `conductor` reader. Polling `stat()` is cheap and
  matches the desk's SPEC-0128 pattern.
- **Bindings** on `QueueTable`:
  - `p` park, `u` resume ("unpark"), `P` prioritize, `A` abandon (capital, so a slip of the
    finger does not abandon);
  - `r` refresh now, `enter` open, `j`/`k` move.
- **Actions:** each action runs these steps:
  1. Check the row's `actions`. If the action is not allowed, notify
     `<action> not available for <state>`.
  2. Collect the input:
     - park: `TextPrompt("park reason")`, non-empty;
     - resume: `TextPrompt("resume note (optional)")`, where empty means no note;
     - prioritize: `ChoiceScreen("priority", ["1".."5"], current)`;
     - abandon: no input.
  3. Build the plan with `plan_queue_action` in a thread worker.
  4. Confirm with `ChoiceScreen`, whose title is a one-line plan summary:
     - park: `park <id> (<mode>): <note>`;
     - resume: `resume <id> → <to>`, plus ` (waits for a slot)` when the plan says so;
     - prioritize: `priority <from> → <to>`;
     - abandon: `abandon <id>`, plus `, removes worktree <path>` when the plan removes it.
     The choices are `yes` and `cancel`. For abandon they are `abandon`, `abandon, keep
     worktree` and `cancel` when the plan's `worktree_present` is true, else only `abandon`
     and `cancel`. Choosing keep re-plans with `keep_worktree=True`.
  5. On yes, apply in a thread worker, notify `<action> <id> done`, and reload. A park whose
     plan `mode` is `deferred` notifies `park requested for <id> (applies after the running
     step)` instead, since `apply_park` only sets `park_requested`.
  A `ValueError` at any step notifies with severity `error` and writes nothing. Esc or cancel at
  any step writes nothing.
- **Enter:** posts `QueueView.CampaignSelected(campaign_id)`. `IdeaDesk` handles it. Until
  the detail view (`IDEA-084`) lands, the handler notifies `wt campaign show <id>` with the
  title `run this in a shell`, as the hub does for spec rows.

## Alternatives considered

- **Render `cli_queue._dashboard` (Rich) into a `Static`.** Rejected: it gives no row cursor, so
  no per-row actions or drill-down. The view model reuses the same payload, so the two views
  cannot disagree on data.
- **Follow the event stream with a TUI reader cursor.** Rejected: it adds another long-lived
  reader that must hold a lock and shows up in `wt queue followers`, and the stream only moves
  when some follower syncs. A `stat()` signature catches every state write directly.
- **One DataTable per section.** Rejected: one table with a section column (the hub pattern)
  keeps one cursor and one set of bindings, and it fits short terminals.
- **Lower-case `a` for abandon.** Rejected: `a` is the desk's non-active toggle, and abandon is
  destructive, so it gets the capital letter and a confirmation.

## Acceptance criteria

- [x] AC1. `wt tui` has a `queue` tab. `3`, a mouse click, and `wt tui --tab queue` open it, and
      each loads and focuses the table. `1` and `2` select ideas and hub. `h` still toggles
      ideas and hub. On the queue tab the idea-scoped app actions are disabled: a pilot test
      presses `x`, `i`, `X`, `l` and `c` there and the ideas file is byte-identical afterwards.
- [x] AC2. `load_queue` emits the sections in the documented order with the documented `meta`
      text, from a seeded data dir with at least one campaign per section. Empty sections emit
      nothing, and an empty queue yields the single `queue is empty` row. A campaign with a
      deferred park appears in both its park section and `active` without a duplicate-key
      error, and `allowed_actions` does not offer `park` (nor `resume`) for it.
- [x] AC3. `header_line` shows the runner pid or `down`/`stale`, the heartbeat age, the slots,
      the held conductor pid or `none`, the held steward projects, and the spend today. The tab
      renders it under the table.
- [x] AC4. While the tab is active, a change of `queue_signature` (a campaign state write) reloads
      the rows within one poll interval, with no key press, and the cursor stays on the same
      campaign. No reload happens while the signature is unchanged and the 15 s interval has
      not elapsed. A load error shows `queue failed: …` and keeps the rows.
- [x] AC5. The tab never calls `events.sync`, `events.cursor_set`, `events.reader_lock`,
      `events.follow` or `mailbox.follow`. A pilot test monkeypatches them to raise and drives
      load, refresh and every action.
- [x] AC6. Park, resume (with and without a note), prioritize and abandon (with and without
      keeping the worktree) each prompt, confirm with the plan summary, and then call the
      matching `plan_*`/`apply_*`. The campaign state afterwards equals what the CLI produces,
      compared on `state`, `priority`, `park`, `park_requested` (without its `at`),
      `pending_brief` and whether the worktree exists, ignoring timestamps. A deferred park
      shows the `park requested` notification. The keep-worktree choice appears only when a
      worktree exists.
      Cancel or Esc at any step writes nothing. A `ValueError` from a plan or an apply is shown
      as an error notification and writes nothing. An action that is not allowed for the row's
      state notifies and opens no prompt.
- [x] AC7. `enter` posts `CampaignSelected`, and without the detail view the desk shows the
      `wt campaign show <id>` hint.
- [x] AC8. Full `uv run pytest` is green, the existing assertions in `tests/test_tui_*` are
      unchanged and green (new tests may be added to those files), and
      `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_tui_queue_model.py` (new, no Textual needed): `load_queue` sections and `meta`
    for campaigns seeded with `store.create_campaign` plus state edits (active run, waiting,
    backlog ready and blocked, needs-you and conductor parks, delivered); `allowed_actions`
    table; `header_line` with and without lease, conductor and stewards (fake `followers`
    rows); `queue_signature` changes on a state write and not otherwise; `queue_action` for each
    action against the CLI's resulting state.
  - `tests/test_tui_queue_tab.py` (new, `needs_textual`, `app.run_test`):
    - tab switching via `3`, `1`, `2`, `h` and `--tab queue` through `T.run` kwargs;
    - live reload after a state write (advance with `pilot.pause`), with the cursor kept;
    - each action through its prompts with yes, cancel and Esc;
    - a plan `ValueError` notification;
    - an action that is not allowed;
    - the `enter` hint;
    - the AC5 forbidden-call monkeypatches;
    - a load error kept in the status line.
  - `tests/test_tui_entry.py` (added tests only): `wt tui --tab` option parsing and
    pass-through.
  - `tests/test_tui_queue_tab.py` also covers: a deferred-park campaign listed twice with the
    cursor restored, the `park requested` notification, abandon of a QUEUED campaign without a
    worktree (no keep choice), and the idea-key gating (AC1).
- **Manual verification:** `uv run wt tui --tab queue` against the live data dir while a
  campaign runs: watch a phase change appear without a key press, park and resume a queued test
  campaign, and record the result here.
- **Regression guard:** full `uv run pytest`, in particular `tests/test_tui_hub.py` and
  `tests/test_tui_desk.py`, unchanged.

### Test record (2026-10-25)

- `uv run pytest tests/test_tui_queue_model.py tests/test_tui_queue_tab.py tests/test_tui_entry.py`:
  new model tests (25), pilot tests (27) and the `--tab` entry tests all pass.
- Full `uv run pytest -q -x`: green except `test_internal_spec_lint_unaffected_by_outbox` on the first
  run, which only flagged a stale `docs/LEDGER.md` (the spec status had just changed). After
  `python3 tools/spec_lint.py --write-ledger` the full suite was rerun and is green (see below).
- `python3 tools/spec_lint.py` exits 0.
- Manual verification, read-only (no tty, so a headless `run_test` against the live data dir with
  `POLL_S = 1.0`): `wt tui --tab queue` opened on the queue tab with 27 rows, and over 60 s the
  tab reloaded 15 more times on its own as the live campaigns wrote state, a run's elapsed time
  advancing with no key press. Park/resume of a queued test campaign was **not** done on the live
  queue (it would mutate real work); those flows are covered by the pilot tests on a seeded
  data dir, compared against the CLI's resulting state.

### As shipped

- `QueueRow` has one extra field, `priority` (default `None`), so the prioritize picker can show
  the current value. `backlog` payload entries carry no `state`, so the model sets `QUEUED` for them.
- `QueueView.CampaignSelected` also carries the row title (unused for now).
- `wt tui --tab` accepts `ideas|hub|queue` case-insensitively. The initial tab is passed to
  `TabbedContent(initial=...)`, and `on_mount` skips focusing the ideas list for other tabs
  (focusing a hidden pane's widget switches back to that tab).
- `IdeaDesk.check_action` tracks the active tab in `_active_tab` (set by the `TabActivated`
  handler) rather than querying the DOM, so it also works while a modal is on top.
- Refresh and action flows use async workers with `asyncio.to_thread` (the desk's SPEC-0134
  pattern) instead of `run_worker(..., thread=True)`; the action flow awaits modals with
  `push_screen_wait`. A deferred park is detected from the applied state (not `PARKED`).
- The pilot tests press keys one at a time with a short pause so each modal can mount.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Nothing to migrate. The tab appears on the next `wt tui` launch for users with the `tui` extra.
Base installs are unaffected: `wt.tui` still imports Textual only inside `run()`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
