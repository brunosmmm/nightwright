---
id: SPEC-0250
title: "TUI escalation inbox: open escalations first, answer by choice or text, withdraw"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, tui, escalation, mailbox, textual]
source_idea: IDEA-085
depends_on: [SPEC-0249, SPEC-0232, SPEC-0235]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-085` (part 2 of 2; part 1 is the mailbox tab,
[SPEC-0249](./0249-tui-mailbox-tab.md)).

[SPEC-0232](./0232-escalation-records-wt-queue-escalate-and-notify.md) adds escalation records:
- each is `wt.escalation.v1`, `ESC-NNNN`, with status `open | answered | withdrawn`, the
  question, numbered options, a recommendation, facts and the park;
- they are stored under `<agentic_dir>/escalations/`;
- the accessors are `escalations.read`, `list_all(cfg, status=None)` and `open_for`;
- `wt escalations list | show | withdraw` is the CLI.

[SPEC-0235](./0235-escalation-answers-decide-waive-attest-conductor-applies.md) adds `wt
escalations answer` (`plan_answer` / `apply_answer`) with three forms: `choose`, `text` and
`override` (`--waive` / `--attest` with a reason). Both specs name the TUI answer surface as
out of scope and point here.

The escalation is also mailed to `human` as a `question` with `needs_reply=False` (the record,
not the mail thread, tracks the answer). In the mailbox tab it shows as an ordinary message,
and answering from there would not set the record's answer.

## Goals / Non-goals

**Goals**
- G1. An `escalations` entry at the top of the mailbox tab's inbox list. It shows the open
  count, and selecting it lists the escalation records: open first, then newest.
- G2. The selected record is shown in full: question, numbered options with the recommended
  one marked, facts, campaign and park, notification and reminder, and the answer or withdrawal.
- G3. Answer an open escalation by choosing an option or by free text, and withdraw one, each
  through SPEC-0235 / SPEC-0232's plan/apply functions with confirmation, as `human`.
- G4. For the `override` form, show the exact CLI command template with the campaign's finding
  ids, instead of a TUI form.

**Non-goals**
- A TUI form for `override` (`--waive` / `--attest`). Choice: an override is a deliberate
  judgment over specific finding ids and AC texts, and SPEC-0235 refuses ambiguous attestations
  with a list of candidates. A form would have to reproduce that loop of refusal and refinement.
  The detail pane lists the ids, and the command template is one copy away. This can be revisited
  from evidence.
- Creating escalations (`wt queue escalate` is the conductor's tool).
- Changing notifications or reminders.

## Decision

Extend SPEC-0249's `MailboxView`. The inbox table gets a first row `escalations` whose content
pane switches the threads table to an escalations table, backed by new view-model functions
`load_escalations` / `escalation_text` in `agentic_model.py`. The answer and withdraw actions
call `escalations.plan_answer` / `apply_answer` and the plan/apply behind `wt escalations
withdraw`, always with `by="human"`.

## Design

### View model (`agentic_model.py`)

- `load_escalations(cfg) -> list[EscRow]`. `EscRow` has `id`, `status`, `campaign`, `spec_id`,
  `question` (first line, 80 characters), `recommendation`, `age_s` (since `created`) and
  `options_n`. The rows come from `escalations.list_all(cfg)` (all statuses), with `open` first
  and then `created` descending.
- `escalation_text(cfg, rec) -> Text` renders, in order:
  1. `id  status  created  by`;
  2. the campaign and spec, with the campaign's current state from `store.read_state` (or
     `(campaign gone)`);
  3. the question, unclipped;
  4. the options as `N. label`, with ` (recommended)` on the recommended one;
  5. the facts as bullets;
  6. the park: `escalated_from → resume_to (mode)`;
  7. `notified via <via> ok|failed <error>` and the reminder time.

  Then the outcome:
  - for an answered record, `answered by <by> at <at>`, followed by the stored answer in the
    words of the SPEC-0235 summary format;
  - for a withdrawn record, `withdrawn by <by>: <reason>`;
  - for an open record with a campaign, a block `override (CLI):` with the template `wt
    escalations answer <id> --waive <finding-id> --attest <ac> --reason "<why>" --yes`, followed
    by the campaign's finding ids (`<id> <severity> <title>`). The ids come from
    `queue.triage_reviews` over `state.last_review.verdicts` and the resolved profile, the same
    input `plan_answer` validates against (SPEC-0235). The list shows at most 15 ids, then
    `+N more`.
- `escalations_signature(cfg)`: the sorted `(name, mtime_ns)` of
  `<agentic_dir>/escalations/ESC-*.json`, merged into the tab's signature next to
  `mailbox_signature`.

### Widget (extends `mailbox_tab.py`)

- The inbox table's first row is `escalations` with `open/total`, bold when `open > 0`. When an
  escalation is open, the tab opens on this row and not on SPEC-0249's first unanswered inbox.
  Choice: an open escalation parks a campaign until a human acts, so it outranks mail.
- With `escalations` selected, the right column shows the escalations table (`status`, `id`,
  `campaign`, `question`, `age`) instead of the threads table. The pane shows `escalation_text`
  for the highlighted record.
- **Bindings** on the escalations table:
  - `c` choose: only for an open record with options. `ChoiceScreen("answer <id>",
    ["1. label", …])` starts on the recommended option.
  - `t` text answer: only for an open record. Free text in a `BodyPrompt`.
  - `w` withdraw: only for an open record. `TextPrompt("withdraw reason")`, non-empty.
  - `enter` on a record with a campaign posts `QueueView.CampaignSelected`.
  - On a record that is not open, `c`, `t` and `w` notify `<id> is <status>` and open nothing.
- **Flow** for each action, following the SPEC-0241 pattern:
  1. Collect the input.
  2. Build the plan in a thread worker: `plan_answer(cfg, id, choose=N, by="human")` or
     `plan_answer(cfg, id, text=…, by="human")`, using SPEC-0235's keyword names for
     `--choose` / `--text` / `--by`, or the withdraw plan with `reason` and `by="human"`.
  3. Confirm with `ChoiceScreen` titled by the plan's resolved answer (`answer <id>: 2.
     <label>`, `answer <id>: "<text first line>"`, `withdraw <id>: <reason>`), with the choices
     `yes` and `cancel`.
  4. On yes, apply, notify `<id> answered` or `<id> withdrawn`, and reload.
  A `ValueError` from a plan or an apply (for example, the record was answered meanwhile) is an
  error notification with nothing written. Esc or cancel writes nothing.
- **Live refresh:** the SPEC-0249 poll now also covers `escalations_signature`, so a new
  escalation or an answer given from the CLI shows within one interval. The selected record is
  kept.
- **Read-only** for followers, as in SPEC-0249: no `events.sync`, `reader_lock`, `cursor_set`
  or `mailbox.follow`. The SPEC-0232 reminders keep running in the followers, not in the TUI.

## Alternatives considered

- **A separate `escalations` tab.** Rejected: escalations reach the human through the `human`
  inbox, so they belong next to it. A pseudo-inbox keeps the tab count and key map small.
- **Answer by replying to the escalation's mail.** Rejected: SPEC-0232 makes the record, not
  the mail thread, the source of truth, and SPEC-0235's conductor applies record answers.
- **An override form.** Rejected for now (see Non-goals).

## Acceptance criteria

- [ ] AC1. The mailbox tab's inbox list starts with `escalations` showing `open/total`. The tab
      opens on it when any escalation is open. Selecting it shows the escalations table, with
      open records first and then newest.
- [ ] AC2. The pane shows every documented field of the highlighted record, including the
      recommended marker, facts, park, notification, and the answer or withdrawal. For an open
      record with a campaign it shows the override CLI template and up to 15 finding ids taken
      from `triage_reviews` over the campaign's last review.
- [ ] AC3. `c` on an open record answers it through `plan_answer` / `apply_answer` with the chosen
      option and `by="human"`. Afterwards the record is `answered` with that choice, which
      `wt escalations show` confirms.
- [ ] AC4. `t` answers with free text, and `w` withdraws with a reason, through the SPEC-0235 /
      SPEC-0232 functions. The record's status and fields afterwards equal what the CLI produces.
- [ ] AC5. Cancel or Esc at any step writes nothing. A `ValueError` (the record was answered
      meanwhile by the CLI) is an error notification and nothing is written. `c`, `t` and `w`
      on a record that is not open notify and open no prompt. `c` on a record without options
      notifies.
- [ ] AC6. An escalation created or answered by another process shows within one poll interval
      with no key press. The tab never calls `events.sync`, `events.reader_lock`,
      `events.cursor_set` or `mailbox.follow` (monkeypatched to raise).
- [ ] AC7. Full `uv run pytest` is green, the SPEC-0249 mailbox tests are unchanged and green,
      and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_tui_escalations_model.py` (new, no Textual). Create records through
    `escalations.plan_escalate` / `apply_escalate` (SPEC-0232) in a temp data dir, with the
    notifier set to `"none"`, then test:
    - `load_escalations` ordering;
    - `escalation_text` for open, answered, withdrawn, campaign-less and gone-campaign records;
    - the override block's finding ids from a seeded `last_review`, and its 15-id cap;
    - the signature changing on a write.
  - `tests/test_tui_escalations_tab.py` (new, `needs_textual`):
    - the opening choice with and without open escalations;
    - choose, text and withdraw end to end, read back with `escalations.read`;
    - cancel and Esc;
    - the answered-meanwhile `ValueError`;
    - a record that is not open;
    - a record without options;
    - live creation from outside;
    - the forbidden-call monkeypatches.
  - `tests/test_tui_mailbox_tab.py` unchanged and green.
- **Manual verification:** create a test escalation (`uv run wt queue escalate --question … --option
  a --option b --recommend 2 --yes`, no campaign), answer it from `uv run wt tui --tab mailbox`
  with `c`, and check `uv run wt escalations show ESC-…`. Record the result here.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Nothing to migrate. The escalations entry appears on the next `wt tui` launch once
SPEC-0232/0235 have shipped.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
