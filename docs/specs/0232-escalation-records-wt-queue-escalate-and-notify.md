---
id: SPEC-0232
title: "Escalation records: wt queue escalate parks human_decision, mails and notifies the human"
status: done
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
    the current `park.resume_to`, and the mode is `immediate`. When the current reason is
    already `human_decision`, the re-park is allowed: no escalation is open (`open_for`
    already refused that case), so the previous one was answered or withdrawn. It keeps
    the park's existing `escalated_from` and `resume_to`. This is how the conductor asks a
    follow-up after an answer (SPEC-0235).
  - `QUEUED`, `WAITING`, or active with no lease held: `immediate`, with `resume_to` taken
    as `plan_park` computes it.
  - Active with the lease held (a loop is running): `deferred`, as `plan_park` does. If
    the campaign already has a `park_requested` from `wt queue park` (reason `human`), the
    escalation **replaces** it with the `human_decision` request. The earlier detail is
    added to the record's `facts` as `park requested earlier: <detail>`. Choice: the
    escalation is the stronger hold, because it also waits for the human, and it keeps the
    earlier intent.
  - No campaign: `park: null`. Runner or global escalations have no campaign.
- `apply_escalate(cfg, plan)` runs, in this order:
  1. With a campaign, take `store.item_lock(cfg, cid)` and keep it through step 3. Re-read
     the state and re-validate the plan: the same `state`, the same `park.reason` when
     PARKED, not terminal, and `open_for` still None. A mismatch raises `ValueError("<cid>
     changed since the plan; re-run")` before any id is allocated. This is the same re-check
     that `apply_park` does. Lock order is always `item_lock`, then the escalations lock, and
     SPEC-0235 follows the same order.
  2. Under the escalations lock (nested): allocate the id and write the record (status
     `open`).
  3. Park (still under `item_lock`):
     - `immediate`: `queue._park(cfg, st, "human_decision", f"{id}: {question}",
       resume_to, by=<from>, extra={"escalated_from": …}, count=not re_park)`. `_park`
       gains `extra=None`, merged into `state["park"]`, and `count=True`. A PARKED→PARKED
       re-park does not increment `counters.parks`: the campaign parked once, and the
       escalation only changes who owns it.
     - `deferred`: set `st["park_requested"] = {"reason": "human_decision", "detail":
       f"{id}: {question}", "at": now, "by": <from>}`.
     - If the park raises anyway (an I/O error), the record file is deleted before the
       error propagates, so no orphan `open` record blocks `open_for`.
  4. `events.append(cfg, "escalation", campaign=…, spec_id=…, project=…, by=<from>,
     data={"escalation_id", "question", "options", "recommendation"})`.
     `EVENT_TYPES["escalation"] = ("escalation_id", "question", "options")`.
  5. `mailbox.send(cfg, to="human", frm=<from>, kind="question", campaign=…,
     needs_reply=False, text=…)`. The text holds the question, the numbered options with
     `(recommended)` on one, and the line `answer: wt escalations show <id>`. The record's
     `mail_id` is set. `needs_reply=False`: the record, not the mailbox thread, tracks
     whether the human answered, so `wt mailbox list --unanswered` does not show stale
     questions.
  6. `notify(cfg, rec)`. The outcome is stored in `notified`. Steps 4 to 6 run after the
     locks are released.
### Deferred parks (`queue.py`)

- `queue.plan_step` (L129) returns `Step("park", reason=req["reason"] if req.get("reason")
  == "human_decision" else "human", by=req.get("by"), …)`. `Step` gains an optional `by`
  field. `queue._park_row` (L1663) uses `step.by` when set, else today's rule (`"human"`
  for reason `human`, `"tick"` otherwise), so the park is attributed to the escalating
  actor.
- `queue._apply` (L945, the hard-coded `"human"`) parks with the request's reason and `by`
  in the same way. Two more cases are handled there:
  - The running step produced **its own park** (`park` is set, e.g. `loop_exhausted`) while
    a `human_decision` request is pending: the campaign parks as `human_decision`, with
    `escalated_from` = the step's reason, `resume_to` = the step's `resume_to`, and the
    detail `"<ESC id>: <question> (step parked: <step detail>)"`. The step's own park
    reason is kept in `escalated_from`, so nothing is lost, and the open escalation matches
    the park. This replaces today's behaviour, where the step park wins and pops the
    request. A pending `human` request (from `wt queue park`) keeps today's behaviour.
  - The step ends the campaign (MERGED, ABANDONED, HANDED_OFF): the transition is
    unchanged, and the open escalation is withdrawn automatically (below).

### Escalations that end without an answer

- `escalations.withdraw_for_campaign(cfg, cid, reason, by)` withdraws the campaign's
  `open` record, if any. It sets `withdrawn`, appends the `escalation_withdrawn` event and
  mails `human` (`kind info`), exactly like `wt escalations withdraw`. It never touches
  `answered` records. It is called:
  - by `queue.apply_resume` and `queue.apply_abandon` (reason `campaign resumed by <by>` /
    `campaign abandoned by <by>`);
  - by `queue._transition` when the target is terminal (reason `campaign <STATE>`).

  Choice: rule (a), closing the record at the moment the campaign leaves the park. A lazy
  scan alone would leave the record open until the next reader comes along.
- As a backstop for edits made outside these paths (a hand-edited `state.json`),
  `remind_due`, `ping_digest` and `open_for` skip any open record whose campaign is neither
  PARKED with reason `human_decision` nor holding a `human_decision` `park_requested`.
  `remind_due` withdraws such records (reason `campaign left the park`), so the skip is not
  permanent.

### Watch

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
  `agentic.escalations.remind_s` (default 3600; 0 disables). It **claims** each due record
  first: under the escalations lock it re-reads the record, re-checks `open` and due, writes
  `reminded_at`, and releases the lock. The notifier then runs outside the lock. Two
  followers syncing at once therefore send one reminder, not two. The cost is that a failed
  notification is not retried before the next interval.
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
  as for any park. Choice: one way to leave a park, already audited. Until that resume, the
  campaign stays under SPEC-0222's **Needs you** with the withdrawn id in its detail. So
  `withdraw` prints `<cid> is still PARKED (human_decision); resume it: wt queue resume <cid>
  --yes`, and the `--json` output carries `campaign_state`. A new escalation for that
  campaign is allowed (re-park rule above).

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

- [x] `wt queue escalate … --yes` writes `<agentic_dir>/escalations/ESC-NNNN.json`
      (`wt.escalation.v1`, every field above), with sequential ids under the lock. Without
      `--yes` it writes nothing and prints the plan. A campaign-less escalation works.
- [x] `queue.PARK_REASONS` contains `human_decision`. Escalating a PARKED campaign re-parks it
      as `human_decision` with `park.escalated_from` set, the original `resume_to` kept and
      `counters.parks` unchanged. A `human_decision` park with no open escalation can be
      re-parked. A QUEUED, WAITING or idle active campaign parks immediately. Apply
      re-validates under `item_lock` and leaves no record when the campaign changed after
      the plan.
- [x] With the lease held, an active campaign gets `park_requested = {reason:
      "human_decision", by}`, replacing a `human` request (whose detail goes to `facts`).
      The tick parks it with that reason, `by` the escalating actor (`plan_step`,
      `_park_row`, `_apply`). A step that parks by itself parks as `human_decision` with
      `escalated_from` = the step's reason. A terminal campaign and a second open
      escalation for one campaign are refused.
- [x] Apply appends one `escalation` event (registered in `EVENT_TYPES`) and one `human` inbox
      message (`kind question`, `needs_reply: false`, the text naming the id and the options).
      The record carries `mail_id`.
- [x] `notify` runs `notify-send` with `auto` when it is on PATH and nothing when it is not.
      `none` never runs. A list setting runs the substituted argv. A failing or missing
      command is recorded in `notified` and the escalation still succeeds.
- [x] `remind_due` claims due records under the escalations lock and re-notifies an open record
      once per `remind_s` (not a withdrawn one, and once even with two concurrent callers).
      It is off at 0. `ping_digest` carries `escalations_open` and `counts.escalations_open`.
- [x] `wt escalations list` (open by default, `--all`, `--json` as
      `wt.escalation.list.v1`) and `show ID` (`--json` is the record) work. An unknown id is a
      clean error.
- [x] `wt escalations withdraw ID --reason … --yes` sets `withdrawn`, notes the campaign, emits
      `escalation_withdrawn` and mails `human`. It leaves the campaign PARKED and refuses a
      non-open record, and prints the resume hint. The `wt-conduct` skill carries the escalate convention and the
      `human_decision` row.
- [x] An open escalation is withdrawn automatically when its campaign is resumed, abandoned or
      reaches a terminal state, and an answered record is never touched.
      `remind_due` / `ping_digest` / `open_for` skip, and `remind_due` withdraws, an open
      record whose campaign has left the `human_decision` park by another route.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

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
    - refusals: terminal, a second open escalation, `--recommend` out of range;
    - a re-park of a `human_decision` park whose escalation was withdrawn (allowed, keeps
      `escalated_from` / `resume_to`), with `counters.parks` unchanged by a re-park;
    - the race: plan on a PARKED campaign, then resume it, then apply ⇒ `ValueError` and no
      record file; a park that raises (monkeypatched `_park`) ⇒ no record file;
    - a pending `human` `park_requested` replaced by the escalation, with the earlier detail
      in `facts`;
    - `_apply` with a pending `human_decision` request and a step result that parks
      `loop_exhausted` ⇒ PARKED `human_decision`, `escalated_from: loop_exhausted`; a step
      result that merges ⇒ the record is `withdrawn`; `_park_row` records `by` = the
      escalating actor;
    - `apply_resume` and `apply_abandon` withdraw the open record; an `answered` record (set
      by hand in the fixture) is left alone; a hand-edited state (PARKED `cost_cap`) makes
      `remind_due` withdraw the record and `ping_digest` skip it;
    - the event in `events.read_since` and the message in `mailbox.read_inbox(cfg, "human")`
      with `needs_reply` false;
    - notify `auto` with and without `notify-send`, `none`, a custom argv (substitution
      asserted), and a raising `_run_notify` (recorded, no exception);
    - `remind_due` with a frozen `now` before and after `remind_s`, a withdrawn record
      skipped, and `remind_s: 0`; two back-to-back calls at the same due `now` notify once;
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

### Test record (2026-10-25)

- `uv run pytest -q`: whole suite green (1135+ tests), including the new
  `tests/test_agentic_escalations.py` (26 tests: records, parks, the race, a raising park,
  deferred requests, resume/abandon/terminal withdrawal, the backstop, notifier modes,
  reminders incl. a second caller at the same `now`, `ping_digest`),
  `tests/test_agentic_escalations_cli.py` (dry-run vs `--yes`, `list`/`--all`/`--json`, `show`,
  `withdraw`, unknown id), the `_apply` cases in `tests/test_agentic_queue.py` (pending
  escalation + gate, + a `loop_exhausted` triage, + a merging close), `_park` `extra`/`count`,
  and the `wt-conduct` row checks in `tests/test_skills.py`.
- `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py`: exit 0.
- Manual (throwaway `WT_DATA_DIR`): `uv run wt queue escalate --question "test?" --option yes
  --option no --recommend 1 --yes` wrote `ESC-0001.json` with `notified: {via: notify-send,
  ok: true}` (`notify-send` is on this machine's PATH), mailed `human:1`; `wt escalations
  list` showed it; `wt escalations withdraw ESC-0001 --reason "manual check" --yes` set
  `withdrawn` and mailed `human:2` (`info`). The desktop popup itself was not observed (headless
  session); the notifier ran and exited 0.

### As shipped

Matches the Design, with these additions and clarifications:

- `escalations.note_park` keeps the open record's `park.escalated_from` / `resume_to` in step
  when the running step parked by itself (`_apply`), so the record agrees with the campaign.
- Withdrawing a record whose `human_decision` park request is still pending (deferred, nothing
  parked yet) also drops that `park_requested`; otherwise the tick would park the campaign for
  an escalation that no longer exists. A campaign already PARKED stays parked.
- `notify` in `auto` mode with no `notify-send` on PATH records `via: none, ok: false, error:
  "notify-send not found"` (and runs nothing); `none` records `ok: true`. A malformed
  `notify` setting is recorded the same way, as an error.
- `remind_due` also writes the reminder's outcome into `notified`.
- Mail from a withdraw uses `conductor` as sender when the actor is not a mailbox address
  (`tick`, `wt`).
- `wt escalations withdraw --json` is `wt.escalation.withdraw.v1` and carries
  `campaign_state` and `hint`; without `--yes` it prints the plan.
- The existing `test_park_owner_table` asserted `human_decision not in PARK_REASONS`; it now
  asserts the opposite. `docs/product/loop/operate.md` (park table) and
  `docs/product/reference/index.md` (top-level commands) gained the `human_decision` row and
  the `escalations` command, which existing docs tests require.
- Review round 1 hardening: `wt queue park` (plan and apply, both modes) now refuses, naming
  the ESC id, while a `human_decision` park request is pending, instead of overwriting or
  popping it; `notify-send` in `auto` mode gets `--` before the title and question;
  `apply_resume` withdraws after the transition succeeds, and `apply_abandon` passes the reason
  `campaign abandoned by human` into `_transition`'s terminal withdrawal (`withdraw_reason`);
  `wt escalations withdraw` reads the campaign state defensively and `wt queue escalate`
  reports `OSError` as a ClickException. New tests cover the `open_for` double-plan race, a
  changed park reason, the `human` info mail on manual and automatic withdrawals, and the park
  guard.
- Review round 2: a deferred escalation that replaces a pending `human` park request keeps it
  on the new request as `park_requested.replaces`; `withdraw` restores it instead of dropping
  the request, so the human's `wt queue park` still parks the campaign. `apply_escalate`
  refuses (`changed since the plan`) when the pending request changed after the plan.
  `_announce` records an event or mail failure in `notified.error` instead of failing the
  apply, `remind_due` skips a record that raises, and `_park_row` gives `human_decision` parks
  the same `escalated_from: null` shape on both deferred paths.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None. The escalations directory is created on the first escalate. Existing parks are
untouched. After merge, the conductor re-reads `/wt-conduct` (SPEC-0231 automates this once it
lands) and uses `wt queue escalate` from then on.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
