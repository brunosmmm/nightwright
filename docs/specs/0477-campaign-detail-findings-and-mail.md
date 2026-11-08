---
id: SPEC-0477
title: "Campaign detail Findings & decisions pane and Mail pane"
status: accepted
owner: user
created: 2026-11-08
kind: feature
parent: SPEC-0405
source_idea: IDEA-1036
tags: [tui, agentic, observability]
depends_on: [SPEC-0407, SPEC-0476]
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
- Answering an open escalation uses the same flow the mailbox tab already uses.

**Non-goals**

- The desk-wide Findings tab (epic child 7). This pane is one campaign.
- Overview phase maths. Overview stays the SPEC-0407 header.
- New mail composition. The mailbox tab already sends mail. This pane reads
  threads and answers an escalation that is already open.
- Any write path other than `escalations.plan_answer` / `apply_answer`.

## Decision

- Replace both placeholders inside the existing `CampaignDetail` tabs.
- Findings & decisions is a table of `history.records` for this campaign,
  newest first, plus a detail pane and a filter line.
- A record whose `status` is `fixed` is labeled `not re-raised`.
- `r` selects that record's run on the Runs tab. `d` selects the resolving
  record. `i` shows the follow-up idea id in the detail.
- Mail groups records with `kind: mail` by `links.thread`.
- `MailboxView._escalation_flow` moves to
  `tui/escalation_flow.py::answer_escalation`. The mailbox tab and this popup
  both call it. On an open escalation record the popup binds `c`, `t`, and
  `w`, the same keys the escalation inbox uses.

## Design

### Findings & decisions

`tab-records` keeps its id. The placeholder `Static` goes away.

A filter `Input` (`#records-filter`) sits above a `DataTable`
(`#campaign-records`). `/` focuses the filter. The filter tokens are
`agent:`, `kind:`, `severity:`, and `status:`, plus a bare word that matches
`title` and `text` case-insensitively. An empty filter shows every record.
Tokens the parser does not know are ignored, and the input shows them as
unmatched rather than applying them.

Columns: time, agent, kind, severity, status, title. Sort is `ts` descending,
then `id` ascending, so a reload does not reorder equal timestamps. The
status cell for `fixed` reads `not re-raised`. Other statuses show the raw
status.

The detail pane (`#record-detail`) shows title, text, status, `linked_by`,
and the link ids (`run_id`, `resolved_by`, `followup_idea`, `escalation`,
`thread`). Text from the record is Rich `Text`, never markup.

Keys, when the table has focus:

- `r` switches to `tab-runs` and selects `links.run_id`, using the SPEC-0476
  selection. If `run_id` is missing, notify `no run` and stay put.
- `d` selects the row whose id is `links.resolved_by`. If that id is missing
  or not in the table, notify `no decision` and stay put.
- `i` writes `links.followup_idea` into the detail. If it is missing, notify
  `no follow-up`. The popup does not switch to the ideas desk.

`resolve_focus` already returns `("records", record_id)` for a known record.
The first paint selects that row. The text `placeholder: records` is not
rendered. An unknown record id still opens Overview and notifies
`not found: <id>`.

Digit keys `1`–`5` still switch tabs.

### Mail

`tab-mail` keeps its id. Records with `kind` `mail` and this campaign are
grouped by `links.thread`. One table row per thread (`#campaign-mail`):
latest message time, who spoke (`from` of the latest message), status of the
latest message, and the first line of the latest text. Newest thread first.

The detail lists the thread's messages in time order: time, `from`, `to`,
`kind`, and text, as Rich `Text`. A question record keeps the status the
history layer set (`open` or `answered`).

`placeholder: mail` is not rendered. `focus` of `{tab: mail}` still opens
this tab, with the newest thread selected when there is one.

### Escalation answer

Move the body of `MailboxView._escalation_flow` to
`answer_escalation(screen, cfg, row, action)`. `action` stays `choose`,
`text`, or `withdraw`. The mailbox method becomes a call to that function,
and the mailbox tests keep calling it through `MailboxView`.

When the selected Findings row is `kind: escalation` and `status: open`, the
popup binds `c`, `t`, and `w` to that function with the same prompts the
escalation inbox uses. A record that is not an open escalation notifies
`not an open escalation` and does not plan an answer. The answer still goes
through `plan_answer` then confirm then `apply_answer`. No other key writes.

## Alternatives considered

- **One spec per pane.** SPEC-0407 already grouped Findings, Mail, and the
  escalation move because Mail and the answer flow share the mailbox code,
  and both panes are the same record list. Splitting them would land two
  campaigns on one widget.
- **Bind `e` / `E`.** SPEC-0407 named those keys only to say the shell does
  not bind them. The shipped escalation inbox uses `c`, `t`, and `w`. The
  popup uses those keys so the two surfaces match.
- **Open the ideas tab on `i`.** The epic says the key shows the follow-up
  idea. Showing the id in the detail keeps the popup in place and adds no
  navigation path.

## Acceptance criteria

- [ ] **AC1. Records table.** On a fixture campaign that has records, Findings
  & decisions shows one row per `history.records` entry, newest `ts` first.
  A row with `status: fixed` shows `not re-raised`. The strings
  `placeholder: records` and `placeholder: records (<id>)` are absent.
- [ ] **AC2. Filter and focus.** `agent:conductor` hides rows whose agent is
  not `conductor`. A bare word matches title and text. Clearing the filter
  shows every row again. Opening with
  `focus={"tab": "records", "record": id}` selects that record. An unknown
  id opens Overview and notifies `not found: <id>`.
- [ ] **AC3. Jumps.** On a record with `links.run_id`, `r` opens the Runs tab
  with that run selected (SPEC-0476). On a record with `links.resolved_by`,
  `d` selects that record. On a record with `links.followup_idea`, `i` shows
  that id in the detail. Each missing link notifies `no run`, `no decision`,
  or `no follow-up` and does not change the selection.
- [ ] **AC4. Mail.** The Mail tab shows one row per `links.thread` among this
  campaign's mail records, newest thread first, and the detail shows every
  message in that thread in time order. `placeholder: mail` is absent.
  Another campaign's mail is not listed.
- [ ] **AC5. Escalation.** `c`, `t`, and `w` on an open escalation record call
  `answer_escalation` and, after confirm, `apply_answer`. The same keys on a
  finding notify `not an open escalation` and do not write. The mailbox tab's
  existing choose, text, and withdraw tests still pass against
  `answer_escalation`.
- [ ] **AC6. Literal text.** A record title or mail body that contains Rich
  markup characters is shown literally.
- [ ] **AC7. No other writes.** Filtering, jumping, and opening Mail leave the
  data dir byte-identical apart from the history cache. The only data-dir
  write this pane can make is an escalation answer the user confirms.

## Test plan

Automated, in `tests/test_tui_campaign_detail_ac.py` and a new
`tests/test_tui_campaign_records.py`, on the fixture copy:

1. Open Findings and assert row count and order against `history.records`.
   Assert a `fixed` row reads `not re-raised`. Replace the `placeholder:
   records` and `placeholder: mail` assertions in the shell tests.
2. Type `agent:` plus an agent that exists in the fixture and assert the
   other agents disappear. Clear it and assert they return. Open with a
   known record id and an unknown one.
3. Pick a fixture record that has `run_id`, press `r`, and assert the Runs
   table selection. Pick one with `resolved_by` and press `d`. Pick one with
   `followup_idea` and press `i`. Repeat each on a record that lacks that
   link and assert the notify and an unchanged cursor.
4. Open Mail and assert one row per thread and the message order in the
   detail. Assert a mail record whose campaign is different is absent.
5. Drive `c` on an open escalation through confirm and assert the answer file
   changes. Press `c` on a finding and assert no answer file is written.
   The existing mailbox escalation tests still pass, now calling
   `answer_escalation`.
6. Put `[bold]` in a title and a mail body and assert the widget text
   contains those characters and no markup styling from them.
7. Hash the campaign directory across a filter and a jump.

Manual: `nw tui`, open a campaign, press `4` and `5`, confirm the tables are
not the placeholder lines, and press `r` on a finding that names a run.

## Rollout

Lands after SPEC-0476. No data migration. The mailbox tab changes only by
calling the moved coroutine.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed and the results recorded in *As shipped*.
- [ ] Affected tests green at the close gate. `python3 tools/spec_lint.py` exits 0.
- [ ] Spec body matches what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

None.
