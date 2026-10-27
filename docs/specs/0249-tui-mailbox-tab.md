---
id: SPEC-0249
title: "TUI mailbox tab: inboxes, threads unanswered-first, reply and send as human"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-26
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, tui, mailbox, textual]
source_idea: IDEA-085
depends_on: [SPEC-0241, SPEC-0183]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-085` (part 1 of 2; the escalation inbox is a sibling spec that depends on this one).

The mailbox ([SPEC-0183](./0183-file-based-mailbox-between-conductor-and-stewards.md),
`src/wt/agentic/mailbox.py`) has inboxes for `conductor`, `human` and each `steward:<Project>`.
Each inbox is an append-only `<agentic_dir>/mailbox/<addr>.jsonl` of `wt.message.v1` records
(`id`, `seq`, `ts`, `from`, `to`, `kind`, `campaign`, `re`, `text`, `needs_reply`). A reply
lands in the **sender's** inbox, so one conversation is spread over several files.
`mailbox.thread` re-joins it. The human can only read and write mail with `wt mailbox list /
thread / send`, and nothing shows which questions are still unanswered except `wt mailbox list
--unanswered` for one address at a time.

The queue tab ([SPEC-0241](./0241-tui-queue-tab-over-the-queue-dashboard.md)) established the
tab plumbing: number keys, `--tab`, a pure view model in `src/wt/tui/agentic_model.py`, `stat()`
polling and no follower cursors.

## Goals / Non-goals

**Goals**
- G1. A `mailbox` tab with an inbox list (each address with unanswered/total counts) and, for
  the selected inbox, its conversations: one row per thread, unanswered first, then newest
  activity.
- G2. The selected thread is shown in full (every message across inboxes, in reply order).
- G3. The human replies to a thread and sends a new message, through `mailbox.send` with
  `frm="human"`, with confirmation.
- G4. Live refresh without a mailbox follower: the tab never holds a reader lock or cursor, so a
  running steward's or conductor's follower is never displaced.

**Non-goals**
- The escalation inbox (open escalations, answer and withdraw): the sibling spec.
- Sending as any address other than `human`. The conductor and stewards are agents with their
  own sessions, and impersonating them from the TUI would make the mailbox's `from`
  meaningless.
- Marking messages read. The mailbox has no read state, only answered/unanswered (a reply
  whose `re` is the message id), and this spec keeps that model.
- Deleting or editing messages (the inboxes are append-only).

## Decision

The view-model functions `load_inboxes`, `load_threads` and `thread_text` in
`agentic_model.py` are built on `mailbox.known_addresses`, `read_inbox`, `_all_messages`,
`unanswered` and `thread`. A widget module `src/wt/tui/mailbox_tab.py` holds `MailboxView` with
two tables (inboxes, threads) and a thread pane. `IdeaDesk` adds `TabPane("mailbox",
id="tab-mailbox")` on key `4` and `--tab mailbox`. Replies and new messages go through
`mailbox.send`.

Replying from another agent's inbox is intended: it is the human stepping in. A reply whose
`re` is a steward's question in the conductor inbox answers that question, so it leaves
`unanswered("conductor")` and the conductor is no longer expected to answer it. The
confirmation says so explicitly (see Reply), so the human makes that choice knowingly.

## Design

### View model (`agentic_model.py`)

- **One snapshot per reload.** Every function below takes `msgs = mailbox._all_messages(cfg)`,
  read once per reload, and computes everything from it. That covers the per-inbox lists
  (filter `to == addr`), the replied-id set (`{m["re"] for m in msgs if m.get("re")}`, the
  rule `mailbox.unanswered` uses) and the thread closure (the transitive `re` closure, the rule
  `mailbox.thread` uses). Rows within one reload therefore cannot disagree, and the cost is one
  read of the inbox files per reload, not one per thread. Tests assert equality with
  `mailbox.unanswered` / `mailbox.thread` on the same data.
- `load_inboxes(cfg) -> list[InboxRow]`. `InboxRow` has `address`, `unanswered`, `total` and
  `last_ts`. There is one row per `mailbox.known_addresses(cfg)` entry, in that order
  (conductor, human, stewards). `unanswered` counts the inbox's `needs_reply` messages not in
  the replied-id set, `total` counts the inbox's messages, and `last_ts` is the newest message
  `ts` or None.
- `thread_root(msgs_by_id, msg) -> str`: follow `re` up to a message without `re`, or whose `re`
  is unknown, and return its id. When the walk revisits an id (a cycle), the root is the
  lexicographically smallest id in the cycle, so every member of the cycle maps to the same
  root.
- `load_threads(cfg, addr) -> list[ThreadRow]`. `ThreadRow` has `root_id`, `subject`, `peer`,
  `campaign`, `count`, `last_ts`, `unanswered` and `reply_to`. It groups the inbox's messages
  by `thread_root` over the snapshot and fills the fields as follows:
  - `subject` is the root's text, first line, clipped to 80 characters;
  - `peer`: take the latest message of the thread that has `addr` as `from` or `to`; `peer`
    is its other party. If no such message exists, `peer` is the `from` of the thread's latest
    message;
  - `count` is the size of the whole thread (the closure from the root);
  - `unanswered` is true when any message of the thread in this inbox needs a reply and is not
    in the replied-id set;
  - `reply_to` is the newest message of the thread in this inbox (the message a reply answers).

  Order: `unanswered` first, then `last_ts` descending.
- `thread_text(cfg, root_id) -> Text` renders each message of `mailbox.thread` as a header line
  `<id>  <ts>  <from> → <to>  <kind>  [<campaign>]`. Unanswered `needs_reply` messages get a
  yellow `needs reply` marker. The full text follows, indented. `re` is implied by the order.
- `mailbox_signature(cfg)` is the sorted `(name, size, mtime_ns)` of
  `mailbox.mailbox_dir(cfg)/*.jsonl`.
- `send_plan(cfg, *, to, kind, text, campaign=None, re=None) -> dict` validates with
  `mailbox.validate_address`, `mailbox.KINDS` and a non-empty text, without writing. It returns
  `{to, frm: "human", kind, campaign, re, text, needs_reply}`, where `needs_reply` is the
  `send` default for the kind. `send_apply(cfg, plan)` calls `mailbox.send(cfg, to=…,
  frm="human", text=…, kind=…, campaign=…, re=…)`.

### Widget (`mailbox_tab.py`)

- **Layout:** the left column (width 28) is the inboxes table (`address`, `open/total`, with a
  bold address when `unanswered > 0`; rows are keyed by address, and code and tests select
  inbox rows by key, never by index). The right column holds the threads table (`!` marker for
  unanswered, `peer`, `subject`, `campaign`, `n`, `last` age) above a thread pane
  (`VerticalScroll`, about 50% of the height) showing `thread_text` for the highlighted thread.
- **Opening:** the tab selects the first inbox with `unanswered > 0`, else `human`.
- **Focus:** `tab` / `shift+tab` move between the two tables. Highlighting an inbox reloads the
  threads, and highlighting a thread renders the pane.
- **Bindings** on the threads table:
  - `r` replies to the highlighted thread;
  - `n` writes a new message, and is also available on the inboxes table;
  - `enter` on a thread with a campaign posts `QueueView.CampaignSelected(campaign)`, which
    opens the campaign detail screen when it exists (SPEC-0246) or the SPEC-0241 hint. On a
    thread without a campaign, `enter` moves focus to the thread pane (so it can be scrolled)
    and posts nothing;
  - `j` / `k` move.
- **Reply:**
  1. The target is `reply_to.from`, with `re = reply_to.id`. When `reply_to` was sent by
     `human` itself (a follow-up to the human's own message), the target is `reply_to.to` and
     `re = reply_to.re` (None when it has no `re`). The human's follow-up therefore never
     marks the human's own message as answered: a `request` the human sent stays unanswered
     until its addressee replies.
  2. Pick the kind with `ChoiceScreen("kind", ["answer", "info", "request", "question"])`,
     defaulting to `answer` when `reply_to.kind` is `question` or `request`, else `info`.
  3. Enter the text with `BodyPrompt` (multi-line).
  4. Confirm with `ChoiceScreen("send <kind> to <addr> re <re or '-'>?", ["send", "cancel"])`.
     When the selected inbox is not `human` and `re` names a message that needs a reply, the
     title adds `; this answers it for <inbox>`.
  5. Call `send_apply` with that `re` and `campaign=reply_to.campaign`.
- **New message:**
  1. Pick the address with `ChoiceScreen` (every known address except `human`).
  2. Pick the kind, defaulting to `request`.
  3. Pick the campaign with `ChoiceScreen` over `(none)` plus the non-terminal campaigns from
     `store.list_campaigns`, shown as `<id>  <spec_id>`. The chosen label is mapped back to
     its campaign id through a label→id dict built with the choices, and `(none)` maps to
     `campaign=None`.
  4. Enter the text with `BodyPrompt`, confirm, and send.
- After a reply the tab reloads, keeps the selected inbox (the thread touches it through
  `reply_to`) and keeps the cursor on that thread. After a new message the tab selects the
  **target** inbox and puts the cursor on the new thread, since the selected inbox usually
  holds no thread with it.
- A `ValueError` from the plan or the send is an error notification with nothing written. Esc
  or cancel at any step writes nothing.
- **Live refresh:** while the tab is active, `set_interval(2.0)` compares `mailbox_signature`
  and reloads the inbox counts, the threads and the pane in a thread worker. The selected inbox
  and thread are kept.
- **No followers:** the tab never calls `mailbox.follow`, `events.reader_lock`,
  `events.cursor_set` or `events.sync`. Choice: `mailbox.follow` takes the `mailbox:<addr>`
  reader lock, which a steward session holds for its whole life (SPEC-0184). A TUI follower
  would either fail or, worse, consume the cursor the steward depends on.

### Tab plumbing

- `4` → `tab-mailbox`, through SPEC-0241's `action_tab`.
- `wt tui --tab` accepts `mailbox`.
- `_tab_activated` activates and deactivates `MailboxView` like `QueueView`.

## Alternatives considered

- **One flat list of all messages.** Rejected: a conversation spans inboxes, and the useful
  question is "which threads wait for someone", which needs threads.
- **Thread by `campaign`.** Rejected: one campaign can carry several unrelated questions, and
  `re` is the protocol's real threading key.
- **Send as any address.** Rejected (see Non-goals). The human speaks as `human`, and the
  conductor's skill already treats `human` mail as the human's words.

## Acceptance criteria

- [x] AC1. `4`, a click, and `wt tui --tab mailbox` open the `mailbox` tab. The inbox table lists
      every known address with `unanswered/total`, and the tab opens on the first inbox with
      unanswered mail, else `human`.
- [x] AC2. `load_threads` groups an inbox's messages into threads by their `re` chain across
      inboxes (a reply stored in another inbox joins its root). It sets `subject`, `peer`,
      `count`, `unanswered` and `reply_to` as documented and orders unanswered first, then
      newest. A cyclic or dangling `re` does not hang or raise.
- [x] AC3. The thread pane shows every message of the highlighted thread in reply order with
      the documented header line and full text, and marks unanswered `needs_reply` messages.
- [x] AC4. Reply sends one `mailbox.send` with `frm="human"`, the documented target and default
      kind, `re` = `reply_to.id` and the thread's campaign. Afterwards that message is no longer
      in `mailbox.unanswered` for its inbox, the thread loses its `!` marker after the reload,
      and the cursor stays on the thread. A follow-up to the human's own `request` goes to its
      addressee with `re = reply_to.re`, and the request stays unanswered. A reply from the
      conductor inbox to a steward's question shows the "answers it for conductor" notice in
      the confirmation and removes the question from `unanswered("conductor")`.
- [x] AC5. New message picks the address (never `human`), the kind, an optional campaign and the
      text, then sends. Cancel or Esc at any step writes nothing. An empty text or an invalid
      address is refused with an error notification and nothing written. After a send the tab
      shows the target inbox with the cursor on the new thread, and the chosen campaign label
      maps to its id (`(none)` → no campaign).
- [x] AC6. A message appended to any inbox by another process shows within one poll interval
      with no key press, and the selected inbox and thread are kept. The tab never calls
      `mailbox.follow`, `events.reader_lock`, `events.cursor_set` or `events.sync`
      (monkeypatched to raise in the test).
- [x] AC7. `enter` on a thread with a campaign posts `CampaignSelected`. On a thread without one
      it focuses the thread pane and posts nothing.
- [x] AC8. Full `uv run pytest` is green, the existing assertions in `tests/test_tui_*` are
      unchanged and green (new tests may be added), and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_tui_mailbox_model.py` (new, no Textual). Seed messages with `mailbox.send` in a
    temp data dir with two registered projects, then test:
    - `load_inboxes` counts;
    - `thread_root` with a dangling `re` and a hand-written cycle (every member maps to the
      smallest id);
    - snapshot results equal to `mailbox.unanswered` / `mailbox.thread` on the same data;
    - `load_threads` grouping across inboxes, ordering, `reply_to` and `peer` (including a
      thread whose last message is from `human`);
    - `thread_text` content;
    - `send_plan` refusals;
    - `mailbox_signature` changes on a send.
  - `tests/test_tui_mailbox_tab.py` (new, `needs_textual`):
    - tab opening via `4` and `--tab mailbox`;
    - the initial inbox choice;
    - reply end to end (the message read back from the target inbox, unanswered cleared);
    - a follow-up to the human's own request (`re = reply_to.re`, request still unanswered);
    - a reply from the conductor inbox to a steward question (notice shown);
    - new message end to end;
    - cancel and Esc at each step;
    - an error notification;
    - a live append from outside;
    - `enter` posting `CampaignSelected`;
    - the forbidden-call monkeypatches.
- **Manual verification:** with a steward session following `steward:<P>`, open `uv run wt tui
  --tab mailbox`, reply to a steward question, and check that the steward's follower prints the
  reply and is not displaced (`wt queue followers` still shows it held). Record the result here.
- **Regression guard:** full `uv run pytest`, in particular `tests/test_agentic_mailbox*.py`,
  unchanged.

### Test record (2026-10-26)

- `uv run pytest tests/test_tui_mailbox_model.py tests/test_tui_mailbox_tab.py
  tests/test_tui_entry.py`: 15 model tests, 18 pilot tests (tab keys/click/`--tab`, initial
  inbox, pane, reply, follow-up to own request, conductor-inbox notice, new message with campaign
  mapping, cancel/Esc at every step, error notifications, live append, forbidden follower calls,
  `enter`, `tab` focus) and the `--tab` option tests: all pass.
- Gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py` exit 0; full
  `uv run pytest -q -x`: 2941 passed, 3 skipped.
- Manual verification (headless, scripted): a `mailbox.follow` reader held `steward:Meta-Tools`
  while the tab replied to that steward's question in the conductor inbox. The follower printed
  `steward:Meta-Tools:1 answer from=human re=conductor:1: main` and `events.followers` still
  showed `mailbox:steward:Meta-Tools` held, cursor advanced to 1 (not displaced).

### As shipped (2026-10-26)

- `tests/test_tui_entry.py::test_tui_tab_option_rejects_unknown_tab` used `mailbox` as its
  example of an unknown tab, which AC1 makes valid; only that example became `bogus` (and
  `mailbox` was added to the passed-through cases). No other assertion in `tests/test_tui_*`
  changed.
- The view-model functions take an optional `msgs` snapshot (`load_inboxes`, `load_threads`,
  `thread_text`); `load_mailbox(cfg, inbox)` reads one snapshot per reload for the whole tab.
  `thread_of` re-implements `mailbox.thread` over a snapshot (tests assert the same order).
- The `; this answers it for <inbox>` notice names the `to` of the message `re` points at, shown
  when that message is unanswered, needs a reply and is not in the `human` inbox. This equals the
  selected inbox in every case of the Design and stays truthful for a follow-up whose `re` is the
  parent of the human's own message.
- Highlight events read the row from the table cursor, not the event, so the transient row-0
  highlight posted while repainting cannot change the selection.
- The thread-pane focus returns to the threads table on `esc` / `tab`.
- A reload that finishes after the user changed the selected inbox (highlight, or the target inbox of a
  new message) discards its snapshot and lets the queued `_again` reload load the inbox now selected;
  `snap.inbox` is only taken when no inbox was asked for. `_reload_async` also reports a
  `TypeError` / `KeyError` from a malformed record as an error notification. Pilot test:
  `test_a_selection_made_during_a_running_load_wins_over_the_stale_snapshot`.
- Thread-table `peer` and `subject` cells are `rich.text.Text` (coerced with `str()` first), so
  agent-written text such as `[x] done` or `[/tmp]` renders literally instead of being parsed as
  markup. `_reload_async` also discards a snapshot when the selected thread changed during the
  load and a reload is queued, so a new message to the already-selected inbox keeps the cursor on
  the new thread; the `_guarded` error notification escapes its text. Pilot tests:
  `test_agent_written_markup_in_a_subject_or_peer_renders_literally`,
  `test_a_new_thread_in_the_selected_inbox_made_during_a_load_keeps_the_cursor`.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Nothing to migrate. The tab appears on the next `wt tui` launch with the `tui` extra.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
