---
id: SPEC-0477
title: "Campaign detail Findings & decisions pane and Mail pane"
status: done
owner: user
created: 2026-11-08
kind: feature
parent: SPEC-0405
source_idea: IDEA-1036
tags: [tui, agentic, observability]
depends_on: [SPEC-0407, SPEC-0476]
updated: 2026-11-10
---

## Context

Child of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md).
The popup shell [SPEC-0407](./0407-campaign-detail-popup-timeline-phase-time-and.md)
left two tabs as one line each: `placeholder: records` on Findings & decisions,
and `placeholder: mail` on Mail. The history layer already builds
`nw.history.record.v1` rows, including mail grouped by `links.thread`, finding
status, and links to a run, a resolving decision, and a follow-up idea.

The human opened a queue row and still saw those two lines. This spec fills
both tabs. The Runs pane they jump into is
[SPEC-0476](./0476-campaign-detail-runs-pane.md).

## Goals / Non-goals

**Goals**

- Findings & decisions lists this campaign's history records and can filter
  them and jump to the run, the resolving decision, and the follow-up idea.
- Mail lists this campaign's mail threads.
- Answering or withdrawing an open escalation uses the same flow the mailbox
  tab already uses.

**Non-goals**

- The desk-wide Findings tab (epic child 7). This pane is one campaign.
- Overview phase maths. Overview stays the SPEC-0407 header.
- New mail composition. The mailbox tab already sends mail. This pane reads
  threads and answers or withdraws an escalation that is already open.
- Any new writer. This spec adds no writer besides the SPEC-0250 answer and
  withdraw flow (`escalations.plan_answer` / `apply_answer` and
  `plan_withdraw` / `apply_withdraw`). `p`, `u`, `P`, and `A` stay the
  SPEC-0407 queue actions on every tab.
- New fields on `nw.history.record.v1`, and mailbox messages that history did
  not attach to this campaign.

## Decision

- Replace both placeholders inside the existing `CampaignDetail` tabs.
- Findings & decisions is a table of `history.records` for this campaign,
  newest first, plus a detail pane and a filter line.
- A record whose `status` is `fixed` is labeled `not re-raised`.
- `r` selects that record's run on the Runs tab. `d` selects the resolving
  record. `i` appends the follow-up idea id and title to the detail.
- Mail groups records with `kind: mail` by `links.thread`. Sender, recipient,
  and message kind come from the mailbox line the history record points at,
  not from the history record.
- `MailboxView._escalation_flow` and `_parked_for_decision` move to
  `tui/escalation_flow.py::answer_escalation(owner, cfg, row, action)`. `row`
  is an `EscRow` whose `id` is the `ESC-…` id. The mailbox tab passes its
  current `EscRow`; this popup loads an `EscRow` from `links.escalation`. On
  an open escalation record the popup binds `c`, `t`, and `w`, the same keys
  the escalation inbox uses.

## Design

### Findings & decisions

`tab-records` keeps its id. The placeholder `Static` goes away.

A filter `Input` (`#records-filter`) sits above a `DataTable`
(`#campaign-records`). `/` focuses the filter. A line `#records-filter-status`
sits under the input.

**Filter.** The filter applies on every change of `#records-filter`.
Whitespace-separated tokens AND together. A token is split on the first colon
only, so `agent:steward:Meta-Tools` matches agent `steward:Meta-Tools`.
`agent:`, `kind:`, `severity:`, and `status:` match those record fields
exactly, on the raw value: `status:fixed` matches status `fixed`, including a
row whose cell reads `not re-raised`. A bare word is a case-insensitive
substring of `title` or `text`. Any other key is not applied.
`#records-filter-status` lists `unmatched: <token>` for each ignored token and
is empty when every token applied. An empty filter shows every record and
clears that line.

**Keyboard.** While `#campaign-records` or `#campaign-mail` has focus, `j`,
`k`, `up`, and `down` move that table's cursor. Those keys move the timeline
cursor only on `tab-timeline`; the timeline bindings do not consume them on
Findings or Mail (so the existing priority `up`/`down` bindings must not
swallow the key when another tab is active). `g` and `G` stay timeline-only.
Printable keys, including digits, type into `#records-filter`; the `1`–`5`
tab bindings do not switch tabs while the input is focused. `escape` in
`#records-filter` returns focus to `#campaign-records` and does not dismiss
the popup. `enter` there also returns focus to the table. Digit keys `1`–`5`
still switch tabs when a table has focus.

Columns: time, agent, kind, severity, status, title. Sort is `ts` descending,
then `id` ascending, so a reload does not reorder equal timestamps. The
status cell for `fixed` reads `not re-raised`. Other statuses show the raw
status.

The detail pane (`#record-detail`) shows title, text, status, `linked_by`,
and the link ids (`run_id`, `resolved_by`, `followup_idea`, `escalation`,
`thread`), each labeled by its key. Text from the record is Rich `Text`,
never markup.

Keys, when the table has focus:

- `r` switches to `tab-runs` and selects that `run_id` on `#campaign-runs`,
  the same selection SPEC-0476 uses for `focus={"tab": "runs", "run":
  run_id}`. If `links.run_id` is absent or is not a `history.runs` row,
  notify `no run` and stay on the Findings row.
- `d` selects the row whose id is `links.resolved_by`. If that id is missing
  or not in the table, notify `no decision` and stay put.
- `i` appends `follow-up: <id>` to `#record-detail`, and when the idea file
  can be read, the idea title on the same line, as Rich `Text`. That line is
  absent before the key. The links list keeps the label `followup_idea`. A
  missing `links.followup_idea` notifies `no follow-up` and does not append.
  The popup does not switch to the ideas desk.

**Focus and reload.** `resolve_focus` already returns `("records",
record_id)` for a known record; it moves the cursor only on the first paint.
The text `placeholder: records` is not rendered. An unknown record id still
opens Overview and notifies `not found: <id>`. A campaign that is not
terminal polls and `reload()` repaints, and `reload()` also runs after a
successful answer or withdraw. On reload the active tab stays,
`#records-filter` keeps its text, `#campaign-records` stays on the same record
id, and `#campaign-mail` stays on the same `links.thread`. If that id is gone,
select the nearest remaining row under the same sort.

### Mail

`tab-mail` keeps its id. Group this campaign's `kind: mail` history records
by `links.thread`. A null `links.thread` is its own row, keyed by the record
id. Threads sort by the latest record `ts` descending, then thread id
ascending. One table row per thread (`#campaign-mail`) shows that `ts`, the
sender, the history `status` of the latest record (blank when null), and the
first line of its `text`.

Sender, `to`, and message kind come from the mailbox line, not from the
history record (whose `kind` is `mail` for every mailbox row, whose `agent`
folds `from`, and which has no `to` or message kind). The record id is
`mailbox:<stem>:<byte offset>`; the stem has no colons (`events._safe` turns
colons into underscores, so `steward:Meta-Tools` is `steward_Meta-Tools`).
Split the id on the first and last colon, seek to that byte offset in
`mailbox/<stem>.jsonl`, and read one JSON object. The row's sender and the
detail's `from`, `to`, and `kind` are that object's `from`, `to`, and `kind`
(message kind is `question`, `answer`, `request`, or `info`). A missing or
unparsable line shows the history `text`, uses `agent` as `from`, and leaves
`to` and message kind blank; it does not raise. `links.thread` is a root
message id that may itself contain colons; it is never parsed as a path.

The detail lists the campaign's records that share the thread, `ts`
ascending, as Rich `Text`: time, `from`, `to`, message `kind`, and `text`.
Mailbox messages history did not attach to this campaign are not pulled in.
Question status stays the history record's `open` or `answered`. No field is
added to `nw.history.record.v1`.

`placeholder: mail` is not rendered. `focus` of `{tab: mail}` still opens
this tab, with the newest thread selected when there is one.

### Escalation answer

Move `MailboxView._escalation_flow` and `_parked_for_decision` to
`answer_escalation(owner, cfg, row, action)`. `row` is an `EscRow` whose `id`
is the `ESC-…` id (not the findings row id `escalation:ESC-0001`).
`action` is `choose`, `text`, or `withdraw`.

- `choose` and `text` go through `plan_answer`, confirm, then `apply_answer`.
- `withdraw` goes through `plan_withdraw`, confirm, then `apply_withdraw`.
- On success, `owner.reload()` runs, and the withdraw notify keeps the
  parked-campaign suffix ("; <campaign> is still PARKED (human_decision);
  resume it from the queue tab (u)") produced by `_parked_for_decision`.
- Cancel, empty text, and an empty withdraw reason write nothing.

The mailbox method becomes a call to that function passing its current
`EscRow`, and the mailbox tests keep driving it through `MailboxView`.

When the selected Findings row is `kind: escalation` and `status: open`, the
popup binds `c`, `t`, and `w`. It reads `links.escalation`, loads that
`EscRow` (via `ESC.read`), and calls `answer_escalation` with it; it never
passes the history record or the findings row. The popup uses the same busy
flag and the same `_guarded` notifies as `MailboxView.start_escalation`:
`ValueError` / `OSError` become an error notify, any other exception becomes
`mailbox action failed: …`. `c` on an open escalation with no options
notifies `<id> has no options` and does not plan. Any other findings row
notifies `not an open escalation` and does not plan.

## Alternatives considered

- **One spec per pane.** SPEC-0407 already grouped Findings, Mail, and the
  escalation move because Mail and the answer flow share the mailbox code,
  and both panes are the same record list. Splitting them would land two
  campaigns on one widget.
- **Bind `e` / `E`.** SPEC-0407 named those keys only to say the shell does
  not bind them. The shipped escalation inbox uses `c`, `t`, and `w`. The
  popup uses those keys so the two surfaces match.
- **Open the ideas tab on `i`.** The epic says the key shows the follow-up
  idea. Showing the id and title in the detail keeps the popup in place and
  adds no navigation path.
- **Add `from`/`to`/message kind to the history record.** It would change
  `nw.history.record.v1` for every consumer; reading the one mailbox line at
  the record's offset needs no schema change.

## Acceptance criteria

- [x] **AC1. Records table.** On a fixture campaign that has records, Findings
  & decisions shows one row per `history.records` entry, newest `ts` first.
  Two records with the same `ts` appear in `id` ascending order. A row with
  `status: fixed` shows `not re-raised`. The strings `placeholder: records`
  and `placeholder: records (<id>)` are absent.
- [x] **AC2. Filter, keys, and focus.** `agent:conductor` hides rows whose
  agent is not `conductor`; `agent:steward:Meta-Tools` keeps rows whose agent
  is `steward:Meta-Tools`. One `kind:` case and one `severity:` case each keep
  only matching rows. `status:fixed` keeps a row whose cell reads
  `not re-raised`. `bogus:x` leaves the rows unchanged and
  `#records-filter-status` shows `unmatched: bogus:x`. A bare word matches
  title and text case-insensitively. Clearing the input shows every row and
  clears that line. With `#campaign-records` focused, `j` or `down` moves to
  another findings row. Pressing `escape` in `#records-filter` returns focus
  to the table with the popup still open. Opening with
  `focus={"tab": "records", "record": id}` selects that record. An unknown id
  opens Overview and notifies `not found: <id>`. A reload keeps the active
  tab, the filter text, and the selected record id (and the selected thread on
  Mail); if the id is gone the nearest remaining row is selected.
- [x] **AC3. Jumps.** On a record with `links.run_id` that is a
  `history.runs` row, `r` opens the Runs tab with that run selected
  (SPEC-0476). On a record with `links.resolved_by`, `d` selects that record.
  On a record with `links.followup_idea`, the detail has no `follow-up:` line
  before `i` and has `follow-up: <id>` after it, with the idea title on that
  line when the idea file exists. Each missing link notifies `no run`,
  `no decision`, or `no follow-up` and does not change the selection or append
  a line; `r` with a `run_id` absent from `history.runs` notifies `no run` and
  stays on the Findings row.
- [x] **AC4. Mail.** The Mail tab shows one row per `links.thread` among this
  campaign's mail records (a null thread is its own row), sorted by latest
  `ts` descending then thread id ascending, and the detail shows every one of
  the campaign's records in that thread in `ts` ascending order.
  For one fixture message the detail's `from`, `to`, and message `kind` equal
  those of the `mailbox/<stem>.jsonl` line at the record id's byte offset. A
  missing or unparsable line falls back to the history `text`, `agent` as
  `from`, and blank `to` and kind without raising. `placeholder: mail` is
  absent. Another campaign's mail is not listed.
- [x] **AC5. Escalation.** On an open escalation record, `c` and `t` call
  `answer_escalation` with the `EscRow` loaded from `links.escalation` and,
  after confirm, `apply_answer`; `w` calls it and, after confirm,
  `apply_withdraw`, then `owner.reload()` runs and the withdraw notify keeps
  the parked-campaign suffix. `c` on an open escalation with no options
  notifies `<id> has no options` and plans nothing. The same keys on any
  other finding notify `not an open escalation` and plan nothing. Cancel,
  empty text, and an empty withdraw reason write nothing. Errors use the same
  `_guarded` notifies as the mailbox tab. The mailbox tab's existing choose,
  text, and withdraw tests still pass against `answer_escalation`.
- [x] **AC6. Literal text.** A record title or mail body that contains Rich
  markup characters is shown literally.
- [x] **AC7. Writers.** This spec adds no writer besides the SPEC-0250 answer
  and withdraw flow. `p`, `u`, `P`, and `A` still run the SPEC-0407 queue
  actions on Findings and Mail. Filtering, jumping, opening Mail, and
  dismissing a prompt leave the campaign directory byte-identical. A
  confirmed `c` or `t` may change whatever `apply_answer` already changes
  (escalation file, campaign state, ledger, mail). A confirmed `w` may change
  whatever `apply_withdraw` already changes. The history cache under
  `cache_dir` may change on a read.

## Test plan

Automated, in `tests/test_tui_campaign_detail_ac.py` and a new
`tests/test_tui_campaign_records.py`, on the fixture copy. The new module
carries exactly one module-level marker, `pytestmark = pytest.mark.tui`
(SPEC-0490: `test_tui_*`); extending the existing module adds no second tier.
`python3 tools/test_tiers.py --check` exits 0 when `tools/test_tiers.py`
exists.

1. Open Findings and assert row count and order against `history.records`,
   including two same-`ts` records in `id` ascending order. Assert a `fixed`
   row reads `not re-raised`. Replace the `placeholder: records` and
   `placeholder: mail` assertions in the shell tests.
2. Filter: type `agent:` plus an agent in the fixture and assert the others
   disappear; `agent:steward:Meta-Tools`; one `kind:` and one `severity:`
   token; `status:fixed` keeps a `not re-raised` row; `bogus:x` leaves the
   rows unchanged and `#records-filter-status` reads `unmatched: bogus:x`; a
   bare word. Clear it and assert rows return and the status line is empty.
   Focus `#campaign-records`, press `j` (or `down`) and assert the cursor
   moves to another row; press `escape` in `#records-filter` and assert focus
   is on the table and the popup is still open. Open with a known record id
   and an unknown one.
3. Pick a fixture record that has `run_id`, press `r`, and assert the Runs
   table selection. Pick one whose `run_id` is not in `history.runs` and
   assert `no run` and an unchanged Findings selection. Pick one with
   `resolved_by` and press `d`. Pick one with `followup_idea`, assert no
   `follow-up:` line in `#record-detail`, press `i`, assert `follow-up: <id>`
   plus the idea title. Repeat each on a record that lacks that link and
   assert the notify and an unchanged cursor.
4. Open Mail and assert one row per thread (null thread its own row), the
   sort, and the `ts` ascending message order in the detail. Assert `from`,
   `to`, and message `kind` for one fixture message equal the mailbox line at
   the record id's byte offset. Corrupt or remove that line and assert the
   fallback without an exception. Assert a mail record whose campaign is
   different is absent.
5. Drive `c` on an open escalation through confirm and assert `apply_answer`
   ran with the `EscRow` from `links.escalation`. Drive `w` through confirm
   and assert `apply_withdraw` ran, `reload()` ran, and the notify has the
   parked suffix. Press `t` as far as the text prompt. Press `c` on an
   escalation with no options and assert `<id> has no options` and no plan.
   Press `c` on a finding and assert `not an open escalation` and no answer
   file written. Cancel, empty text, and an empty withdraw reason write
   nothing. The existing mailbox escalation tests (choose, text, withdraw)
   still pass, now calling `answer_escalation`.
6. Put `[bold]` in a title and a mail body and assert the widget text
   contains those characters and no markup styling from them.
7. Hash the campaign directory across a filter, a jump, and `c` then escape,
   and assert it is unchanged. Press `p` (or `u`/`P`/`A`) on Findings and
   assert the SPEC-0407 queue action still runs.
8. Reload: set a filter and select a non-newest record and a non-newest
   thread, trigger one `reload()`, and assert the active tab, the filter
   text, the selected record id, and the selected thread survive. Remove the
   selected record and assert the nearest row is selected.

Manual: `nw tui`, open a campaign, press `4` and `5`, confirm the tables are
not the placeholder lines, move with `j`/`k`, and press `r` on a finding that
names a run.

### Test record (2026-11-10)

- `uv run pytest -q tests/test_tui_campaign_records.py` — 13 passed: the pure model (sort,
  filter grammar, thread grouping, mailbox-line fallback) and Pilot drives of test steps 1–8
  (table order with same-`ts` ties and `not re-raised`; all four keyed tokens, `bogus:x`
  unmatched line, `j`/`down`/`k`/`up`, digits typing into the filter, `escape` leaving the popup
  up; known and unknown focus; `r` / `d` / `i` with their `no run` / `no decision` /
  `no follow-up` misses; mail threads, `from`/`to`/`kind` against the mailbox line, the
  corrupt-offset fallback, another campaign's mail absent; `c` / `t` / `w` through confirm,
  cancel, empty text and reason, no options, a non-escalation row, the guarded error notify;
  literal markup; campaign-directory hash across filter, jump and `c` then escape, and
  `p`/`u`/`P`/`A` still reaching the queue action; reload keeping tab, filter and both selections).
- Review loop 1 added five tests to `tests/test_tui_campaign_records.py` (18 passed): a second
  mailbox message at a non-zero byte offset (and a `steward_Meta-Tools` stem) checked against the
  raw jsonl line; the popup's `guarded()` RuntimeError and OSError notifies with `_busy` reset,
  plus `not an open escalation` for an answered or null-link escalation record and `t` through
  confirm to `apply_answer`; `i` on an unknown or unreadable idea; `j`/`down`/`k`/`up` on the Mail
  table, a vanished thread on reload, `enter` in the filter, and an empty filter with `r`/`d`/`i`;
  and `p`/`u`/`P`/`A` and the directory hash across a Mail visit.
- Existing suites touched by the move and the placeholder removal, all green:
  `tests/test_tui_mailbox_tab.py`, `tests/test_tui_escalations_tab.py` (choose, text and withdraw
  now run through `answer_escalation`), `tests/test_tui_campaign_detail.py`,
  `tests/test_tui_campaign_detail_ac.py`, `tests/test_tui_campaign_runs.py`,
  `tests/test_tui_queue_tab.py`, `tests/test_tui_events_tab.py`,
  `tests/test_tui_fixture_harness.py`.
- `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py` exit 0.
- The manual `nw tui` pass was not done in a real terminal; the Pilot tests drive the same keys
  against the real widgets. The full suite was left to gating.

### As shipped

- New `tui/campaign_records.py` holds the pure model (sort, filter grammar, status label, mail
  threads, the mailbox-line read, idea title). `tui/escalation_flow.py` holds
  `answer_escalation(owner, cfg, row, action)` (a coroutine), `parked_for_decision(cfg, cid)` and
  `guarded(owner, flow)`; `MailboxView` runs `answer_escalation(self, self.cfg, row, action)` under
  its own `_guarded`, and `AM.esc_row` builds an `EscRow` from a record for both callers.
- On reload the selected row is kept by id; if that id is gone the row at the same position
  (clamped to the last row) is selected.
- `#record-detail` is the `Static` inside the scrolling `#records-detail`. `i` reads the idea title
  in a worker thread and appends `follow-up: <id>  <title>` once it is read; the line is dropped
  when the selection moves to another record.
- Mail rows are keyed by thread id (or the record id for a null thread); duplicate history record
  ids are collapsed before the Findings table is built.

## Rollout

Lands after SPEC-0476. No data migration. The mailbox tab changes only by
calling the moved function.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed and the results recorded in *As shipped*.
- [x] Affected tests green at the close gate. `python3 tools/spec_lint.py` exits 0.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

None.
