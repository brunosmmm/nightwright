---
id: SPEC-0232
title: "Escalation records: wt queue escalate parks human_decision, mails and notifies the human"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, conductor, escalation, notify, queue]
source_idea: IDEA-087
depends_on: [SPEC-0222, SPEC-0183, SPEC-0212]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from `IDEA-087` (part 1 of 2; answering is
[SPEC-0235](./0235-escalation-answers-decide-waive-attest-conductor-applies.md)).

When the conductor ([SPEC-0182](./0182-event-driven-wt-conduct-monitor-armed-conductor.md))
reaches an "escalate" row of its decision table, the escalation today is only text in its
chat session:
- The mailbox ([SPEC-0183](./0183-file-based-mailbox-between-conductor-and-stewards.md)) has a
  `human` address, but nothing reads it and nothing notifies anyone.
- There is no record of which escalations are open or answered.
- No alert reaches a human who is not watching that session.
- The campaign stays parked under whatever reason it had (`loop_exhausted`, `cost_cap`…), so
  `wt queue watch` cannot tell "waiting for the human" from "the conductor is on it".

[SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md) reserved the park reason
`human_decision` as **human-owned** (`queue.PARK_OWNERS`) and left this spec to add the reason
and the command that creates it.

## Goals / Non-goals

**Goals**
- G1. A durable escalation record (`wt.escalation.v1`, `ESC-NNNN`) holding the question, the
  numbered options, the recommendation and the facts, with status `open | answered |
  withdrawn`.
- G2. `wt queue escalate [CAMPAIGN]` creates it. With a campaign, it also parks the campaign
  with reason `human_decision` and keeps the original `resume_to`. The record goes onto the
  event stream and into the `human` inbox.
- G3. A desktop notification when one is available (`notify-send`), configurable, and a
  reminder while the escalation stays open.
- G4. `wt escalations list | show | withdraw`.
- G5. Every "escalate" row of `wt-conduct` creates an escalation instead of chat text. The
  chat keeps one line.

**Non-goals**
- Answering (`wt escalations answer`, decide/waive/attest) and the conductor applying an
  answer: [SPEC-0235](./0235-escalation-answers-decide-waive-attest-conductor-applies.md).
- A TUI inbox tab. IDEA-085 covers the TUI.
- Claude Code push notifications or email. Only a local command is run; `notify` takes any
  argv, so other channels can be plugged in through config.
- Changing which situations escalate. The decision table keeps its rows. Only the way it
  escalates changes.

## Decision

A new module, `src/wt/agentic/escalations.py`, stores one JSON file per escalation under
`<agentic_dir>/escalations/` and allocates ids under a lock. `plan_escalate` /
`apply_escalate` follow the dry-run / `--yes` convention of `plan_park` / `apply_park`. Apply
does four things:
- writes the record;
- parks the campaign as `human_decision` (`"human_decision"` joins `queue.PARK_REASONS`);
- appends an `escalation` event and mails `human`;
- runs the notifier.

`events.sync(ping=True)` (followers only) calls `escalations.remind_due` and adds open
escalations to `ping_digest`. A new `wt escalations` group (`cli_escalations.py`) lists,
shows and withdraws records.

## Design

### Record (`wt.escalation.v1`)

`<agentic_dir>/escalations/ESC-0001.json`, written with `export._atomic_write`:

```json
{"schema": "wt.escalation.v1", "id": "ESC-0001", "created": "<iso>", "by": "conductor",
 "campaign": "c-…" | null, "spec_id": "SPEC-…" | null, "project": "…" | null,
 "question": "…", "options": [{"n": 1, "label": "…"}], "recommendation": 1 | null,
 "facts": ["…"], "status": "open",
 "park": {"escalated_from": "loop_exhausted" | null, "resume_to": "IMPLEMENTING",
          "mode": "immediate" | "deferred"} | null,
 "mail_id": "human:7", "notified": {"via": "notify-send" | "none" | "command",
 "ok": true, "error": null, "at": "<iso>"}, "reminded_at": null,
 "answer": null, "answered_by": null, "answered_at": null, "withdrawn": null}
```

- Ids: `ESC-%04d`, one more than the highest existing file. They are allocated under
  `events._locked(<escalations>/.lock)`, and the file is written inside that lock.
- `escalations.read(cfg, id)`, `list_all(cfg, status=None)` (newest first),
  `open_for(cfg, campaign)` (the open record for a campaign, or None) and `_write(cfg,
  rec)` are the only accessors.
- `answer` and `answered_*` stay null in this spec. SPEC-0235 fills them.

### `wt queue escalate`

```
wt queue escalate [CAMPAIGN] --question TEXT [--option TEXT]... [--recommend N]
                  [--fact TEXT]... [--from ADDR] [--yes] [--json]
```

- `--from` defaults to `$WT_MAILBOX_ADDR`, then `conductor`. It must be a valid mailbox
  address (`mailbox.validate_address`).
- `--recommend N` must name one of the options, 1-based. It is an error when no options are
  given.
- The command validates, then prints the plan (`wt.queue.escalate.v1`, `written: false`)
  unless `--yes` is given.
- `plan_escalate(cfg, campaign, ...)` refuses when:
  - the campaign is terminal (`TERMINAL_STATES`);
  - `open_for(cfg, campaign)` already returns an escalation (the error names it). One open
    escalation per campaign.
- The plan's `park` follows the campaign's state:
  - `PARKED`: a **re-park**. `escalated_from` is the current `park.reason`, `resume_to` is
    the current `park.resume_to`, and the mode is `immediate`. If the current reason is
    already `human_decision`, the plan is refused (an escalation is already pending or
    stale).
  - `QUEUED`, `WAITING`, or active with no lease held: `immediate`, with `resume_to` taken
    as `plan_park` computes it.
  - Active with the lease held (a loop is running): `deferred`, as `plan_park` does.
  - No campaign: `park: null`. Runner or global escalations have no campaign.
- `apply_escalate(cfg, plan)` runs, in this order:
  1. Write the record (status `open`).
  2. Park, under `store.item_lock`:
     - `immediate`: `queue._park(cfg, st, "human_decision", f"{id}: {question}",
       resume_to, by=<from>, extra={"escalated_from": …})`. `_park` gains `extra=None`,
       merged into `state["park"]`.
     - `deferred`: set `st["park_requested"] = {"reason": "human_decision", "detail":
       f"{id}: {question}", "at": now}`. `queue.plan_step` (L129) and `queue._apply` (L935)
       now use `req["reason"]` when it is `human_decision` instead of the hard-coded
       `"human"`.
  3. `events.append(cfg, "escalation", campaign=…, spec_id=…, project=…, by=<from>,
     data={"escalation_id", "question", "options", "recommendation"})`.
     `EVENT_TYPES["escalation"] = ("escalation_id", "question", "options")`.
  4. `mailbox.send(cfg, to="human", frm=<from>, kind="question", campaign=…,
     needs_reply=False, text=…)`. The text holds the question, the numbered options with
     `(recommended)` on one, and the line `answer: wt escalations show <id>`. The record's
     `mail_id` is set. `needs_reply=False`: the record, not the mailbox thread, tracks
     whether the human answered, so `wt mailbox list --unanswered` does not show stale
     questions.
  5. `notify(cfg, rec)`. The outcome is stored in `notified`.
- The detail starts with the escalation id, so `wt queue watch` shows the id and the question
  in SPEC-0222's **Needs you** section without any change to that code.
- A campaign park through `_park` projects to the existing `parked` event (reason
  `human_decision`) as usual.

### Notification

- Setting: `agentic.escalations.notify`, default `"auto"`:
  - `"auto"`: `shutil.which("notify-send")`. If found, run `["notify-send", "--app-name=wt",
    "--urgency=critical", "wt: {id} needs you", "{question}"]`; if not found, do nothing
    (`via: "none"`).
  - `"none"`: never notify.
  - A list of strings: a custom argv, with `{id}`, `{campaign}`, `{question}` and
    `{recommendation}` substituted (`via: "command"`).
- The command runs through `escalations._run_notify(argv)` (`subprocess.run`, timeout 5 s,
  output captured). Any `OSError`, timeout or non-zero exit is recorded as `ok: false` with
  the error text. Escalating never fails because of a notification.
- Reminders: `escalations.remind_due(cfg, now)` re-notifies (with `wt: {id} still needs you`)
  each open record whose `reminded_at or created` is older than
  `agentic.escalations.remind_s` (default 3600; 0 disables), then sets `reminded_at`.
  `events.sync` calls it when `ping=True`, after the events lock is released. Only live
  followers ping, so reminders run while a conductor is armed, which is when escalations
  happen.
- `ping_digest` gains `escalations_open: [{id, campaign, age_s}]` and
  `counts.escalations_open`. The change is additive; the `conductor_ping` required keys are
  unchanged.

### `wt escalations` (`cli_escalations.py`, registered in `agentic/cli.py` like `mailbox`)

- `list [--all] [--json]`: open records by default, newest first. With `--all`, every status.
  Rich table: id, age, campaign, status, question (folded to 2 lines), options count. JSON:
  `{"schema": "wt.escalation.list.v1", "escalations": [...]}`.
- `show ID [--json]`: the full record. Options are numbered, the recommended one is marked,
  facts are a bullet list, and the park shows `escalated_from → resume_to`. JSON is the record.
- `withdraw ID --reason TEXT [--from ADDR] [--yes]`: only for `open` records. Sets `status:
  withdrawn` and `withdrawn: {reason, by, at}`. On the campaign it appends a ledger `note`
  `escalation <id> withdrawn: <reason>`. It appends the event `escalation_withdrawn`
  (`EVENT_TYPES["escalation_withdrawn"] = ("escalation_id", "reason")`) and mails `human`
  (`kind info`).
- Withdrawing does **not** resume the campaign. The conductor resumes it with `wt queue resume`
  as for any park. Choice: one way to leave a park, already audited.

### Conductor skill (`skills/wt-conduct/SKILL.md`)

- *Escalate crisply* now says: escalate with `wt queue escalate <campaign> --question … --option
  … --recommend N --fact … --yes` (no campaign for runner or global matters), then give the
  human one chat line: `escalated ESC-NNNN: <question>`.
- Every table cell that says "escalate" refers to that convention. The cells themselves are
  unchanged.
- New row `parked` `human_decision`: "waiting for the human's answer (SPEC-0235). Do nothing.
  If the situation resolves without the human, `wt escalations withdraw <id> --reason … --yes`
  and resume."
- New row `escalation` / `escalation_withdrawn`: informational.

## Alternatives considered

- **One JSONL log of escalation events instead of a file per record.** Rejected. Answers
  and withdrawals change status, so a log needs a fold to read current state. A file per
  record is read directly and atomically replaced, the same way `state.json` is.
- **Escalate without parking (record only).** Rejected. The campaign would stay under its old
  reason and show as conductor-owned, which is exactly the confusion SPEC-0222 fixes.
- **Keep the mail `needs_reply=True`.** Rejected. Two sources of truth for "answered".
- **Notify through the Claude Code session.** Rejected. That is the very channel the human is
  not watching, and the configurable argv covers other channels.
- **Remind from the runner loop.** Rejected. The runner has no reason to know about the human,
  while the follower's `sync(ping=True)` is already the periodic watchdog.

## Acceptance criteria

- [ ] `wt queue escalate … --yes` writes `<agentic_dir>/escalations/ESC-NNNN.json`
      (`wt.escalation.v1`, every field above), with sequential ids under the lock. Without
      `--yes` it writes nothing and prints the plan. A campaign-less escalation works.
- [ ] `queue.PARK_REASONS` contains `human_decision`. Escalating a PARKED campaign re-parks it
      as `human_decision` with `park.escalated_from` set and the original `resume_to` kept.
      A QUEUED, WAITING or idle active campaign parks immediately.
- [ ] With the lease held, an active campaign gets `park_requested.reason ==
      "human_decision"`, and the tick parks it with that reason (`plan_step` / `_apply`). A
      terminal campaign, a second open escalation for one campaign, and an already
      `human_decision` park are refused.
- [ ] Apply appends one `escalation` event (registered in `EVENT_TYPES`) and one `human` inbox
      message (`kind question`, `needs_reply: false`, the text naming the id and the options).
      The record carries `mail_id`.
- [ ] `notify` runs `notify-send` with `auto` when it is on PATH and nothing when it is not.
      `none` never runs. A list setting runs the substituted argv. A failing or missing
      command is recorded in `notified` and the escalation still succeeds.
- [ ] `remind_due` re-notifies an open record once per `remind_s` (not a withdrawn one), and is
      off at 0. `ping_digest` carries `escalations_open` and `counts.escalations_open`.
- [ ] `wt escalations list` (open by default, `--all`, `--json` as
      `wt.escalation.list.v1`) and `show ID` (`--json` is the record) work. An unknown id is a
      clean error.
- [ ] `wt escalations withdraw ID --reason … --yes` sets `withdrawn`, notes the campaign, emits
      `escalation_withdrawn` and mails `human`. It leaves the campaign PARKED and refuses a
      non-open record. The `wt-conduct` skill carries the escalate convention and the
      `human_decision` row.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_escalations.py` (new), with `_run_notify` and `shutil.which`
    monkeypatched; no real notification is sent:
    - plan/apply for a campaign-less escalation: the record fields, sequential ids for two
      creates, no file without apply;
    - a PARKED (`loop_exhausted`, `resume_to: IMPLEMENTING`) campaign: re-park reason,
      `escalated_from`, `resume_to`, and the ledger `park` event;
    - QUEUED and WAITING campaigns: immediate park;
    - an active campaign with a held lease (the lease fixture from `test_agentic_queue.py`):
      `park_requested.reason == "human_decision"`, and `plan_step` returns a park Step with
      that reason;
    - refusals: terminal, a second open escalation, an existing `human_decision` park,
      `--recommend` out of range;
    - the event in `events.read_since` and the message in `mailbox.read_inbox(cfg, "human")`
      with `needs_reply` false;
    - notify `auto` with and without `notify-send`, `none`, a custom argv (substitution
      asserted), and a raising `_run_notify` (recorded, no exception);
    - `remind_due` with a frozen `now` before and after `remind_s`, a withdrawn record
      skipped, and `remind_s: 0`;
    - `ping_digest` `escalations_open`.
  - `tests/test_agentic_escalations_cli.py` (new, `CliRunner`): `wt queue escalate` dry-run
    vs `--yes`; `wt escalations list` / `--all` / `--json`; `show` (text contains
    `(recommended)`); `withdraw` (event, note, state still PARKED); unknown id.
  - `tests/test_agentic_queue.py`: `PARK_REASONS` contains `human_decision`, and `_park`
    with `extra` merges into `state["park"]`.
  - `tests/test_skills.py`: `wt-conduct` mentions `wt queue escalate` and `human_decision`.
- **Manual verification:** in a throwaway data dir (`WT_DATA_DIR`), run
  `uv run wt queue escalate --question "test?" --option yes --option no --recommend 1 --yes`.
  A desktop notification appears, and `uv run wt escalations list` shows the record. Then
  withdraw it. Record the output in the spec.
- **Regression guard:** full `uv run pytest`. The park-request tests that assert reason
  `human` for `wt queue park` still pass, because only `human_decision` requests change.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None. The escalations directory is created on the first escalate. Existing parks are
untouched. After merge, the conductor re-reads `/wt-conduct` (SPEC-0231 automates this once it
lands) and uses `wt queue escalate` from then on.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
