---
id: SPEC-0533
title: "Remote park and resume"
status: done
owner: user
created: 2026-11-10
kind: feature
parent: SPEC-0310
depends_on: ["SPEC-0532"]
supersedes: ["SPEC-0527"]
source_idea: IDEA-562
---

## Context

[SPEC-0527](./0527-federation-bounded-relay-mail-and-two-remote-verbs.md) replaced
[SPEC-0335](./0335-federation-over-a-networked-mailbox.md) after two campaigns diverged
(`SPEC-0335-20261101-02a3` at about 7,300 lines, `SPEC-0335-20261108-5cbb` at about 2,400
lines). The verb gap those campaigns kept reopening belongs here: running park inside
`POST /v1/mail` tied the HTTP timeout, the dedupe record, and the verb's failure modes
together.

This spec is part 4 of the split of SPEC-0527. It depends on
[SPEC-0532](./0532-exactly-once-mail-between-hub-and-host.md). Mail already stores an
`action` object on the inbox record and does not run it. This spec commits that action,
runs `park` and `resume` in the host runner tick, and tells the conductor how to ask for
them. Enrolment, the event relay, health, read-only views, and exactly-once mail are the
other parts, listed under Non-goals.

What exists today and this spec reuses:

- `queue.plan_park` / `queue.apply_park` and `queue.plan_resume` / `queue.apply_resume`
  (`src/nightwright/agentic/queue.py`). `by="human"` is the actor
  `decisions.needs_decision` exempts, so neither call needs a decision record.
  `plan_park` returns `mode` `deferred` when the campaign state is in `ACTIVE_STATES`
  and `lease.is_held` is true; `apply_park` then sets `park_requested` and leaves the
  state unchanged. Any other mode parks immediately, with `park.reason` `human` and
  `park.detail` equal to the reason argument. `apply_park` passes that same `by` into
  `park_requested` and into the park ledger event.
- `queue.tick`, which already calls `_tick`, then `federation.relay_tick`, then
  `federation.relay_mail`.
- SPEC-0532's `post_mail`: inbox append, inbox fsync, `received.jsonl` append,
  `received.jsonl` fsync. A `dup` whose inbox line is already durable appends only a
  missing received line. `enqueue_remote` writes one outbox line under `events._locked`.
- `mailbox.send` and `mailbox.read_inbox`. A send to `conductor` while
  `federation.json` exists stores its own `msg_uid` (SPEC-0532). The `re` argument is
  a separate field.
- `events.ping_digest` and `quiet.ping_fingerprint`. A `conductor_ping` is delivered
  when the fingerprint is non-empty and differs from the digest's last delivered
  fingerprint. `quiet.RULES` already has `host_unreachable` and `host_mail_rejected`
  as `wake` (SPEC-0530). This spec does not edit `RULES` or `EVENT_TYPES`.

## Goals / Non-goals

**Goals**

- A posted `action` object becomes one pending line in `federation/actions.jsonl`, and
  that line is fsynced before the `received.jsonl` dedupe line. A later `dup` for the
  same `msg_uid` writes the action line when it is missing, and does not append the
  inbox again.
- `drain_actions` runs once in the host `queue.tick`, after the campaign steps. It
  calls `plan_park`/`apply_park` or `plan_resume`/`apply_resume` with `by="human"`.
- A deferred park stays `started` with `wait` `PARKED` until the campaign state is
  `PARKED`, then one answer `ok` is sent. A raised verb is `failed` and is not called
  again. A `started` line with no `wait` and no `done` is `unknown` and is not called
  again.
- Answers are sent again until the host conductor inbox contains a record whose `re`
  is the action `msg_uid`. A refused action is also copied to the host `human` inbox.
- The conductor treats an event with no `host` field as local. A remote event is
  mailbox `--action park|resume` or plain mail. A host that has been unreachable for
  at least 30 minutes is a delivered `conductor_ping` `data.federation` row.

**Non-goals**

- Part 1, federation enrolment and the signed host channel: `hosts.json` and the 0600
  secrets, `nw hosts add|list|rm|on|off|check|enrol|unenrol`, HMAC on requests and on
  enrolled responses, the unsigned 401 after unenrol, SPEC-0334 bind rules, and
  `GET /v1/status` mapped from `queue.queue_payload` items. Inert without `hosts.json`
  and `federation.json`. No event relay, mail, or verbs.
- Part 2, hub event relay, health, and read-only host views: byte-offset
  `GET /v1/events`, write-then-checkpoint, crash rescan, 409 reset, host and origin
  tags, campaign rewritten to `<campaign>@<host>`, unknown types kept as `host_event`,
  and `quiet.RULES` for the five new types. Network calls run in parallel, hub event
  appends and cursor updates are serialized, and a result that arrives after the 2.5 s
  budget is dropped. `host_unreachable` once at 10 min, persisted across restart.
  `nw queue status --all-hosts`, the conductor-brief line, and `hub_payload`
  `federation_hosts`, including stale, `--state`, and `--json`. Depends on the channel
  spec.
- Part 3, exactly-once mail between hub and host: uuid `msg_uid`, outbox acks for
  `delivered`, `dup`, and `rejected`, relayed conductor delivery that stores `msg_uid`
  and one host-tagged message event with campaign `<campaign>@<host>`, and
  `validate_address` unchanged for bare addresses. Mail reads seek to a byte offset. A
  lost response or a 5xx retries into `dup`; a 4xx is terminal. `POST /v1/mail` only
  writes mail. Depends on the channel spec.
- Verbs other than `park` and `resume`. `stop`, `gates`, `rebase`, and the rest are
  refused. Their `GitError` paths do not exist here.
- Running `plan_park`, `apply_park`, `plan_resume`, or `apply_resume` inside
  `POST /v1/mail`. The handler writes the inbox line, the action line, and the dedupe
  line.
- Copying a `failed` or `unknown` outcome to the host human. The human copy exists
  only for a refused action.
- A decision record on the remote park or resume. `by` is `human`.
- Editing `relay_tick`, `relay_mail`'s ack rules, `EVENT_TYPES`, or `quiet.RULES`.
- Scanning old inboxes for actions at startup. Only a `POST /v1/mail` of that
  `msg_uid` repairs a missing action line.

## Decision

- The action line is fsynced before the dedupe record. `post_mail` appends and fsyncs
  the inbox, then appends and fsyncs `federation/actions.jsonl` when the posted
  `action` is a JSON object, then appends and fsyncs `received.jsonl`. A `dup` that
  finds the inbox line and no action line writes that action line, fsyncs it, and only
  then writes a missing received line.
- `queue.tick` calls `federation_actions.drain_actions` once after `_tick` returns and
  before `relay_tick`. Park calls `plan_park` then `apply_park`. Resume calls
  `plan_resume` then `apply_resume`. Both pass `by="human"` and no `decision`.
- A deferred park (`plan["mode"] == "deferred"`) records `started` with `wait`
  `PARKED` and does not answer. A later drain answers `ok` once when the campaign
  state is `PARKED`. An `Exception` from plan or apply records `failed` and is not
  retried. A `started` line without `wait` records `unknown` and is not retried.
- The conductor answer is `mailbox.send` to `conductor` with `re` set to the action
  `msg_uid`. Drain sends it again while `read_inbox` of `conductor` has no record with
  that `re`. A `refused:` outcome is also sent to the host `human` inbox, with the
  same retry rule on that inbox.
- An event with no `host` field stays on the local decision table. An event with a
  `host` field is remote: park and resume go out as
  `nw mailbox send --action park|resume`, and every other act is plain mail.
  `events.ping_digest` adds `data.federation`. A row is present when the latest
  `host_unreachable` or `host_reachable` for that host is `host_unreachable` and
  `data.since` is at least 1800 seconds before `now`. `quiet.ping_fingerprint`
  includes that row, so the ping is delivered.

## Design

### Modules

| Module | Add |
|---|---|
| `src/nightwright/agentic/federation_host.py` | The action-line append inside `post_mail`. Still calls no queue attribute except `queue_payload`. |
| `src/nightwright/agentic/federation_actions.py` | `drain_actions`. This module calls `plan_park`, `apply_park`, `plan_resume`, and `apply_resume`. |
| `src/nightwright/agentic/federation.py` | `enqueue_remote(..., action=None)`. A dict `action` is stored on the outbox line. No `queue` import. |
| `src/nightwright/agentic/cli_mailbox.py` | `--action` and `--reason` on `nw mailbox send`. |
| `src/nightwright/agentic/queue.py` | `tick` calls `drain_actions` once, imported inside the function. |
| `src/nightwright/agentic/events.py` | `ping_digest` adds `federation`. |
| `src/nightwright/agentic/quiet.py` | `ping_fingerprint` adds one pair per federation row. |
| `skills/nw-conduct/SKILL.md` | The remote-event paragraph below. The existing `conductor_ping` cell stays, plus one sentence for `data.federation`. |
| `skills/nw-steward/SKILL.md` | The outcome sentence below, added under Scope. |

`federation_actions.py` is a separate module so SPEC-0529's AST guard stays true:
`federation_host.py` still calls only `queue_payload` and still does not reference
`subprocess`. `hosts.py`, `federation.py`, and `cli_hosts.py` still do not import
`queue`.

Journal path: `<agentic_dir>/federation/actions.jsonl`. Lock path:
`<agentic_dir>/federation/actions.lock`, taken with `events._locked`. `post_mail`
already holds `federation/mail.lock` for the batch; it takes `actions.lock` only
around the action append, after the inbox fsync and before the received append.
`drain_actions` takes `actions.lock` and never takes `mail.lock`.

### Action line before the dedupe record

`enqueue_remote` gains keyword-only `action=None`. When `action` is a dict, the outbox
line includes that object under `action`. When `action` is `None`, the line has no
`action` key. The rest of the line is unchanged from SPEC-0532. `relay_mail` already
POSTs the outbox line, so the object arrives on the host. This spec does not edit
`relay_mail`.

`nw mailbox send` gains `--action` with choices `park` and `resume`, and `--reason`
(a string, default empty). The flag builds
`{"verb": "<park or resume>", "campaign": "<--campaign>", "reason": "<--reason>"}`.
`reason` is always the third key. An omitted `--reason` stores `""`.

The command exits non-zero and writes no inbox line and no outbox line when:

- `--reason` is set and `--action` is omitted;
- `--action` is set and `--campaign` is missing, empty, or combined with
  `--no-campaign`;
- `--action` is set and `hosts.load` is `[]`, or the address contains no `@`.

A bare address never gains an `action` key. A remote send without `--action` stays
the SPEC-0532 line, with no `action` key. The text argument is still required.

Inside `post_mail`, after the inbox fsync and before the `received.jsonl` append, for
both the fresh path and either `dup` path:

1. When the posted element's `action` is not a dict, skip the action file. Do not
   create `actions.jsonl`.
2. When it is a dict and some line of `actions.jsonl` already has this `msg_uid`,
   append nothing to the action file.
3. Otherwise append one line and `os.fsync` the action file:
   `{"msg_uid", "state": "pending", "to", "ts", "action"}`. `to` is the canonical
   inbox address SPEC-0532 stored. `ts` is `store.now_iso()`. `action` is the posted
   object, including any extra keys. The line is one `json.dumps` plus `\n`.
4. If that fsync raises, the response is 500 `{"error": "commit_failed"}` and the
   handler does not append this message's received line.
5. If the received line for this `msg_uid` is missing, append it and fsync
   `received.jsonl` only after step 3's fsync has returned. If the received line is
   already present, leave `received.jsonl` unchanged.

A `dup` still does not append the inbox. The result is still `dup`. `post_mail` does
not call `drain_actions`, `plan_park`, `apply_park`, `plan_resume`, `apply_resume`, or
any other queue writer, and it does not open a campaign state file.

Later journal lines, also one JSON object per line, are:

- `{"msg_uid", "state": "started", "ts"}`
- `{"msg_uid", "state": "started", "wait": "PARKED", "ts"}`
- `{"msg_uid", "state": "done", "outcome": "<text>", "ts"}`

The last line for a `msg_uid` wins. `outcome` is the conductor answer text.

### `drain_actions`

`queue.tick` calls `federation_actions.drain_actions(cfg)` once, after `_tick` returns
and before `federation.relay_tick`, still inside the tick's `try`. The import is
inside `tick`. An exception is logged on the `federation_actions` logger and does not
fail the tick. `relay_tick` and `relay_mail` still run.

When `<config_dir>/secrets/federation.json` is not a regular file, or when
`actions.jsonl` is absent, `drain_actions` returns without creating a file and without
calling `plan_park`, `apply_park`, `plan_resume`, or `apply_resume`.

Otherwise it groups lines by `msg_uid` and handles each id once, in order of first
appearance. Plan, apply, and `mailbox.send` run outside `actions.lock`. Every journal
append is fsynced before the lock is released. A raised `Exception` from plan or apply
is caught. `KeyboardInterrupt` and other `BaseException`s propagate, and they leave
whatever journal line was already fsynced.

**Pending checks.** The first failing check wins. Drain appends `done` with that
outcome, fsyncs, sends the answer, sends the human copy, and does not call plan or
apply.

1. `action` is not a dict, or it has a key other than `verb`, `campaign`, and
   `reason`, or `reason` is present and is not a string: `refused: fields`.
2. `verb` is not `park` and not `resume`: `refused: verb`.
3. `to` is not `steward:` plus a non-empty project name: `refused: address`.
4. `campaign` is not a non-empty string, or the string contains `@`:
   `refused: campaign`.
5. `store.read_state` raises `ValueError`: `refused: campaign`.
6. `str(state.get("project") or "").casefold()` differs from the project name's
   casefold: `refused: project`.

The reason passed to park is the action's `reason` string, or `""` when the key is
absent. Resume passes `note=reason` when that string is non-empty, and `note=None`
when it is empty.

**Park.** `plan = queue.plan_park(cfg, campaign, reason, by="human")` with no
`decision` argument.

- Plan raises `Exception`: append `started` with no `wait`, fsync, append `done` with
  `failed: <type>: <message>`, fsync, answer. `<type>` is `type(exc).__name__` and
  `<message>` is `str(exc)`. Do not call `apply_park`.
- `plan["mode"] == "deferred"`: append `started` with `wait` `PARKED` and fsync that
  line before `apply_park`. Then `queue.apply_park(cfg, plan)`. On `Exception`, append
  `done` `failed: ...` and answer. On return, append nothing else and send nothing.
  The campaign stays in its current state with `park_requested`.
- Any other mode: append `started` with no `wait` and fsync it before `apply_park`.
  On `Exception`, append `done` `failed: ...` and answer. On return, append `done`
  `ok` and answer.

**Resume.** Append `started` with no `wait` and fsync it before
`queue.plan_resume(cfg, campaign, note=..., by="human")`. Do not pass `decision`,
`to`, or `reset_loops`. Then `queue.apply_resume(cfg, plan)`. On `Exception`, append
`done` `failed: ...` and answer. On return, append `done` `ok` and answer.

**Latest line is `started` with `wait` `PARKED`.** Read the campaign state. Do not
append another `started` line.

- State is `PARKED`: append `done` `ok`, fsync, answer once. Do not call plan or
  apply.
- `park_requested` is a dict: do nothing. Send nothing.
- Otherwise the deferred apply did not stick. Call `plan_park` and `apply_park` once,
  with `by="human"`. On `Exception`, append `done` `failed: ...` and answer. On
  return, re-read state. State `PARKED` appends `done` `ok` and answers. Any other
  state appends nothing and sends nothing.

**Latest line is `started` without `wait`.** Append `done` with outcome
`unknown: host restarted during the verb`, fsync, and answer. Do not call plan or
apply, and do not read the campaign for this branch.

**Latest line is `done`.** Do not call plan or apply. Run the answer step. When
`outcome` starts with `refused:`, also run the human-copy step.

### Answers and the human copy

The answer calls `mailbox.send(cfg, to="conductor", frm=frm, kind="answer", re=msg_uid,
campaign=campaign, text=outcome, needs_reply=False)`. `frm` is the journal line's `to`
when `mailbox.validate_address` accepts it, and `human` otherwise. `campaign` is the
action's campaign string when that string is non-empty, contains no `@`, and
`store.read_state` succeeds; otherwise `None`. `text` is the outcome, exactly `ok`,
`failed: <type>: <message>`, `unknown: host restarted during the verb`, or
`refused: <why>`.

Before the call, `mailbox.read_inbox(cfg, "conductor")`. A record whose `re` equals
this action `msg_uid` means the answer is already there: do not call `send`. When
`send` raises, the journal stays as it is and the next drain tries again. Drain does
not loop inside one tick.

The human copy uses the same `frm`, `re`, `campaign`, and `text`, with `to="human"`,
`kind="info"`, and `needs_reply=False`. It is sent only for an outcome that starts
with `refused:`. Skip it when the host `human` inbox already has that `re`. A failure
of one send does not skip the check for the other inbox.

`mailbox.send` to `conductor` still stores a new `msg_uid` on the answer record
(SPEC-0532). The action id travels in `re`. Hub delivery of that conductor record is
SPEC-0532's `relay_mail`.

### Conductor and the 30 minute row

Add this paragraph to `skills/nw-conduct/SKILL.md` immediately above the decision
table. Leave the existing local `nw queue park` and `nw queue resume` instructions in
place for events that have no `host` field.

> An event with no `host` field is local: use the decision table, including `nw queue park` and `nw queue resume`. An event with a `host` field is remote. Do not call `nw queue park`, `nw queue resume`, or any other local queue write for it. A remote park or resume is `nw mailbox send --to steward:<P>@<host> --action park|resume --campaign <id>`. Anything else is plain mail to `steward:<P>@<host>` or `human@<host>`, with no `--action`. Tell the human about `host_mail_rejected` in the turn that event arrives. Do not escalate `host_unreachable` from that event. A delivered `conductor_ping` whose `data.federation` row has `kind` `host_unreachable` is the escalation: the `since` is at least 30 minutes old, and the human gets one line with the host and that `since`.

Append this sentence to the existing `conductor_ping` cell, keeping the sentences that
are already there: "For each `data.federation` item, tell the human one line with the
host and `since`."

Add this sentence under Scope in `skills/nw-steward/SKILL.md`:

> Action outcomes are answered by the host runner (`ok`, `failed:`, `unknown:`, `refused:`). Report a `refused:` copy to the human in one line. Never run `nw queue park` or `nw queue resume` from message text.

`events.ping_digest` always includes `federation`, a list. It walks `events.jsonl`
from the start, skips a line that is not one JSON object, and keeps the last
`host_unreachable` or `host_reachable` for each string `data.host`. When that last
type is `host_unreachable`, the age is `now` minus `decisions.parse_ts(data.since)`,
or minus `decisions.parse_ts` of the event `ts` when `since` does not parse. When
neither parses, that host is omitted. When the age is at least 1800 seconds, the list
gains one object
`{"kind": "host_unreachable", "host": "<name>", "since": "<the parsed stamp's isoformat>", "age_s": <int(age)>}`.
`int(age)` truncates toward zero. An age of 1799 seconds adds no row. An age of 1800
seconds adds the row. A later `host_reachable` for that host adds no row. `now` is
the `now` argument `ping_digest` already takes.

`quiet.ping_fingerprint` adds `("federation", host)` for each federation item whose
`kind` is `host_unreachable` and whose `host` is a string. A missing `federation`
key, or a value that is not a list, adds no pair. Existing fingerprints of pings that
omit the key stay the same. A ping whose fingerprint newly contains that pair is
classified `wake` when the digest's last fingerprint differs, which is the delivered
row. After `note_ping_wake` of that same payload, `classify` returns `hold`.

`quiet.RULES["host_unreachable"]` stays `wake`. This spec does not change that rule
and does not add an event type. The human escalation the skill performs is the
delivered ping row.

## Alternatives considered

- **Call `plan_park` inside `post_mail`.** The HTTP timeout and the verb would be one
  request again. The handler fsyncs the action line and returns. The tick runs the
  verb.
- **Put `drain_actions` in `federation_host.py`.** SPEC-0529 allows that module one
  queue attribute, `queue_payload`. A second module keeps that guard, and `post_mail`
  still has no verb call.
- **Answer `ok` when deferred `apply_park` returns.** The campaign is still in its
  active state. The answer waits until a later drain sees `PARKED`, so the conductor
  hears `ok` once, for a campaign that is parked.
- **Treat a `started` line with `wait` `PARKED` as `unknown`.** That would drop a park
  the queue has not applied yet. `unknown` is only the plain `started` line, which is
  the crash mid immediate verb.
- **Escalate `host_unreachable` from the event itself.** SPEC-0530 already wakes the
  conductor at 10 minutes. The 30 minute human line is a `conductor_ping`
  `data.federation` row so the fingerprint, and not a second timer in the skill, is
  what gets delivered.

## Acceptance criteria

- [x] For a posted `action` object, `os.fsync` runs on the inbox file, then on
      `actions.jsonl`, then on `received.jsonl`. The pending line's `action` equals
      the posted object, and the campaign state file is unchanged. An action-file
      fsync that raises returns 500 `{"error": "commit_failed"}`, leaves
      `received.jsonl` without that `msg_uid`, and leaves one inbox line. The retry
      result is `dup`, the inbox length stays 1, and exactly one pending action line
      exists before the received line is appended.
- [x] A `dup` whose action line is missing appends that one pending line, fsyncs it,
      and appends a received line only when the received line was missing. A `dup`
      whose action line is already present appends no second action line. A posted
      message whose `action` is not a dict creates no `actions.jsonl`. An AST walk of
      the function `post_mail` finds none of `drain_actions`, `plan_park`,
      `apply_park`, `plan_resume`, and `apply_resume`.
- [x] `queue.tick` calls `drain_actions` once, after `_tick` and before `relay_tick`,
      and then calls `relay_mail` once. A `drain_actions` that raises `RuntimeError`
      still lets `tick` return and still calls `relay_tick`. With no `federation.json`,
      `drain_actions` creates no `actions.jsonl` and calls none of `plan_park`,
      `apply_park`, `plan_resume`, and `apply_resume`.
- [x] An immediate `park` of a local campaign in `QUEUED`, addressed to
      `steward:<project>`, calls `plan_park` and `apply_park` with `by="human"` and
      no `decision`. The campaign state becomes `PARKED`, `park.reason` is `human`,
      `park.detail` is the action reason, and the park ledger event's `by` is
      `human`. The host conductor inbox gains one record with `re` equal to the
      action `msg_uid`, `kind` `answer`, and `text` `ok`. A second drain adds no
      second record and does not call `apply_park` again.
- [x] With the lease held and the campaign in `IMPLEMENTING`, drain fsyncs `started`
      with `wait` `PARKED` before `apply_park` returns. The state stays
      `IMPLEMENTING`, `park_requested.by` is `human`, `park_requested.reason` is
      `human`, and `park_requested.detail` is the action reason. The conductor inbox
      has no record with that `re`. A second drain, while the state is not `PARKED`
      and `park_requested` is still set, does not call `apply_park`. After the state
      is `PARKED`, one drain appends `done` `ok` and exactly one conductor record with
      that `re` and `text` `ok`. A further drain adds no record and does not call
      `apply_park`.
- [x] A `resume` of that parked campaign calls `plan_resume` and `apply_resume` with
      `by="human"` and no `decision`. The state leaves `PARKED`. The resume ledger
      event's `by` is `human`. No decision record is written. The conductor inbox
      gains one `ok` answer for the resume `msg_uid`, and a second drain does not add
      another.
- [x] `apply_park` patched to raise `RuntimeError("boom")` records
      `failed: RuntimeError: boom`, sends that text once, and a second drain does not
      call `apply_park` again and does not append a second conductor record.
- [x] A journal whose latest line is `started` without `wait` and without `done`
      records `unknown: host restarted during the verb`, sends that text once, and
      calls neither `plan_park` nor `apply_park` even when the campaign state is
      `PARKED`. A second drain does not send again.
- [x] `rebase`, a campaign string that contains `@`, a campaign whose project does not
      casefold-match the `steward:` address, an action with an extra key, and an
      action posted to `human` are each `done` with `refused: verb`,
      `refused: campaign`, `refused: project`, `refused: fields`, and
      `refused: address` in that order of examples. Each sends one conductor answer
      and one host `human` record with the same `re` and the same text. None of them
      calls `plan_park` or `plan_resume`, and the campaign state is unchanged.
- [x] When `mailbox.send` to `conductor` raises and the conductor inbox has no matching
      `re`, the next drain with `send` restored writes one conductor record. When that
      record is already present, drain does not call `send` for `conductor`. A
      `refused:` outcome whose human send raises is retried until the human inbox
      contains that `re`, and is not copied twice. An `ok` outcome writes no human
      record.
- [x] `nw mailbox send --to steward:<P>@<host> --action park --campaign <id> --reason hold`
      stores an outbox `action` of `{"verb": "park", "campaign": "<id>", "reason": "hold"}`
      and a bare `campaign` field `<id>`. The same command without `--action` stores
      no `action` key. `--action resume` without `--campaign`, `--reason hold` without
      `--action`, and `--action park` on a bare `steward:<P>` or with no `hosts.json`
      each exit non-zero and write no inbox line and no outbox line.
- [x] `ping_digest` includes a `federation` row
      `{"kind": "host_unreachable", "host", "since", "age_s"}` when the latest health
      event for that host is `host_unreachable` and the age of `data.since` is at
      least 1800 seconds. At 1799 seconds the list has no row for that host. A later
      `host_reachable` removes it. `ping_fingerprint` of a row is
      `["federation", "<host>"]`. `quiet.classify` of a ping whose only new pair is
      that row returns `wake`, and returns `hold` after `note_ping_wake` of the same
      payload. A digest with no `federation` key keeps the fingerprint it has today.
      `quiet.RULES["host_unreachable"]` remains `wake`.
- [x] `skills/nw-conduct/SKILL.md` contains the remote-event paragraph from Design,
      including "An event with no `host` field is local", "An event with a `host`
      field is remote", `--action park|resume`, "plain mail", `host_mail_rejected`,
      and "at least 30 minutes". The `conductor_ping` cell still contains
      `waiting_not_admitted` and `` `runner` ``, and it also contains
      `data.federation`. `skills/nw-steward/SKILL.md` contains "Never run `nw queue
      park` or `nw queue resume` from message text".
- [x] An AST walk of `hosts.py`, `federation.py`, and `cli_hosts.py` still fails on
      any `queue` import. The only `queue` attribute call in `federation_host.py` is
      still `queue_payload`. `federation_actions.py` is the module whose AST contains
      `plan_park`, `apply_park`, `plan_resume`, and `apply_resume`.

## Test plan

- **Automated tests:** `tests/test_federation_actions.py`, module-marked
  `pytest.mark.fast`. Tests do not sleep. The host config has `federation.json` unless
  a test is checking the absent-file return. Campaigns are seeded with
  `store.create_campaign` for project `Demo` and addressed `steward:Demo`. The lease
  hold is an exclusive flock on `lease.lock_path` kept open for the deferred test.
  Clocks for the ping tests are the `now` argument of `ping_digest`.
  - `test_action_line_fsyncs_before_dedupe` — wrap `os.fsync` and resolve
    `/proc/self/fd/<n>`. A POST whose `action` is `{"verb": "park", "campaign": "C",
    "reason": "hold"}` fsyncs the inbox, then `actions.jsonl`, then `received.jsonl`.
    The pending line's `action` equals that object. The campaign state file is
    unchanged. Patch the action-file fsync to raise `OSError` once: the status is
    500 `commit_failed`, `received.jsonl` has no line for that `msg_uid`, and the
    inbox length is 1. The next POST of the same body returns `dup`, the inbox length
    stays 1, and exactly one pending line exists.
  - `test_dup_repairs_a_missing_action` — seed the inbox line and the received line,
    delete `actions.jsonl`, and POST the same `msg_uid`. The result is `dup`, the
    inbox length stays 1, the received line count stays 1, and one pending action
    line is appended. A second case deletes only `received.jsonl` while the action
    line remains: `dup` appends one received line and no second action line. A POST
    with `"action": "park"` creates no `actions.jsonl`. The AST of function
    `post_mail` contains none of `drain_actions`, `plan_park`, `apply_park`,
    `plan_resume`, and `apply_resume`.
  - `test_drain_runs_in_the_tick` — monkeypatch `drain_actions`, `relay_tick`, and
    `relay_mail`. `queue.tick` calls them once, in that order. A `drain_actions` that
    raises `RuntimeError` still returns a tick report and still calls `relay_tick`.
    With `federation.json` absent, the real `drain_actions` creates no
    `actions.jsonl`. A spy on `plan_park` records no call.
  - `test_immediate_park_and_resume_by_human` — a `QUEUED` campaign and one pending
    `park` with reason `hold`. The wrappers call through to the real functions and
    record `by` and whether `decision` was passed. State is `PARKED`, `park.reason`
    is `human`, `park.detail` is `hold`, and the park ledger `by` is `human`. One
    conductor record has `re` equal to that `msg_uid`, `kind` `answer`, and `text`
    `ok`. A second drain leaves the call count and the inbox length unchanged. A
    second pending action `resume` moves the state off `PARKED`, writes a resume
    ledger event with `by` `human`, writes no decision record, and adds one `ok`
    answer for the resume id. `decisions.read` is empty.
  - `test_deferred_park_answers_ok_once` — hold the lease flock and seed
    `IMPLEMENTING`. After one drain the latest line is `started` with `wait`
    `PARKED`, `apply_park` has been called once with `by="human"`, the state is
    `IMPLEMENTING`, and `park_requested` has `by` `human`, `reason` `human`, and
    `detail` equal to the action reason. The conductor inbox has no matching `re`.
    A second drain does not call `apply_park`. Write the state as `PARKED` and drain
    again: one `done` `ok` and one conductor `ok` record. Another drain adds no
    record and does not call `apply_park`. A separate case removes `park_requested`
    while the latest line is still `started`/`wait` `PARKED` and the state is
    `IMPLEMENTING`: the next drain calls `apply_park` once more and still sends no
    answer while the state is not `PARKED`.
  - `test_failed_apply_is_not_rerun` — patch `apply_park` to raise
    `RuntimeError("boom")`. The outcome and the conductor `text` are
    `failed: RuntimeError: boom`. A second drain leaves the `apply_park` call count
    at 1 and the conductor inbox length at 1. The human inbox has no record.
  - `test_started_without_wait_is_unknown` — write a single `started` line with no
    `wait` for a campaign whose state is `PARKED`. Drain calls neither `plan_park`
    nor `apply_park`, appends `unknown: host restarted during the verb`, and sends
    that text once. A second drain adds no conductor record.
  - `test_refused_actions_copy_the_human` — five pending lines: verb `rebase`;
    campaign `SPEC-1@box`; project `Other` against `steward:Demo`; an extra key
    `note`; `to` `human`. Outcomes are `refused: verb`, `refused: campaign`,
    `refused: project`, `refused: fields`, and `refused: address`. Each id has one
    conductor answer and one `human` record with that `re` and that text. `plan_park`
    and `plan_resume` are not called. The campaign state file is unchanged.
  - `test_answers_retry_until_re_is_present` — a `done` `ok` line and a `send` that
    raises `OSError` leave the conductor inbox empty. The next drain with `send`
    restored writes one record whose `re` is the action `msg_uid`. A pre-seeded
    conductor record with that `re` makes drain skip `send`. A `done` `refused: verb`
    line whose human `send` raises is retried once the patch is removed, and a third
    drain leaves one human record. The `ok` case writes no human record.
  - `test_mailbox_send_action_flag` — with `hosts.json` and an enabled host, the CLI
    `--action park --campaign SPEC-1 --reason hold` to `steward:Demo@box` writes one
    outbox line whose `action` is `{"verb": "park", "campaign": "SPEC-1", "reason":
    "hold"}` and whose `campaign` is `SPEC-1`. The same send without `--action` has
    no `action` key. `--action resume` without `--campaign`, `--reason hold` without
    `--action`, `--action park` to bare `steward:Demo`, and `--action park` to
    `steward:Demo@box` with no `hosts.json` each exit non-zero and create no inbox
    file and no outbox file.
  - `test_federation_ping_delivers_at_30_min` — append `host_unreachable` with
    `data.since` 1800 seconds before `now`, and a second host at 1799 seconds.
    `ping_digest` lists only the first host, with `kind` `host_unreachable` and
    `age_s` 1800. Append `host_reachable` for the first host and the list no longer
    contains it. `ping_fingerprint` of the 1800-second digest contains
    `["federation", "<host>"]`. `quiet.classify` on a `Ctx` whose last ping
    fingerprint is empty returns `wake` for a payload whose only actionable pair is
    that row, and returns `hold` after `note_ping_wake`. `ping_fingerprint` of a
    digest dict that has no `federation` key equals the fingerprint of that dict
    today. `quiet.RULES["host_unreachable"]` is `wake`.
  - `test_skill_docs` — `skills/nw-conduct/SKILL.md` contains the Design paragraph's
    sentences "An event with no `host` field is local", "An event with a `host` field
    is remote", `--action park|resume`, "plain mail", `host_mail_rejected`, and "at
    least 30 minutes". `_conduct_row("conductor_ping")` contains
    `waiting_not_admitted`, `` `runner` ``, and `data.federation`.
    `skills/nw-steward/SKILL.md` contains "Never run `nw queue park` or `nw queue
    resume` from message text".
  - `test_queue_import_guards` — the SPEC-0529 AST walk still passes for `hosts.py`,
    `federation.py`, `cli_hosts.py`, and `federation_host.py`. The AST of
    `federation_actions.py` contains `plan_park`, `apply_park`, `plan_resume`, and
    `apply_resume`.
- **Manual verification:**
  1. Create two empty directories. Point `NW_DATA_DIR` and `NW_CONFIG_DIR` at the hub,
     then at the host.
  2. On the hub, run `nw hosts add box --url http://127.0.0.1:8792` and pipe stdout
     into the host's `nw hosts enrol`.
  3. On the host, start `nw serve --federate`. Seed one `QUEUED` campaign in project
     `Demo`. On the hub, run
     `nw mailbox send --to steward:Demo@box --from conductor --action park --campaign <id> --reason hold "park"`,
     then `nw queue tick` once so the mail is delivered.
  4. On the host, run `nw queue tick` once. The campaign state is `PARKED` and the
     host conductor inbox has one record with `text` `ok` and `re` equal to the
     outbox `msg_uid`. A second host tick does not add a second `ok`.
  5. On the hub, run `nw queue tick` once more. The hub conductor inbox has that `ok`
     answer, with `re` equal to the action `msg_uid`.
  6. Record the commands and the output under an `As shipped` heading in this spec.
  Enrolling a second physical machine is rollout, not part of this spec's definition
  of done.
- **Regression guard:** at the close gate, `python3 tools/affected_tests.py` plus
  `python3 tools/spec_lint.py`. `tests/test_federation_mail.py` covers mail.
  `test_post_mail_writes_mail_only` still passes: a second POST after deleting only
  `received.jsonl` appends one received line and no second action line.
  `tests/test_agentic_quiet.py` fingerprints that omit `federation` stay the same.
  `tests/test_skills.py` still finds `waiting_not_admitted` and `` `runner` `` on the
  `conductor_ping` row.

### Test record (2026-11-12)

- `uv run pytest -q tests/test_federation_actions.py` — 21 passed (every test named in the plan,
  plus the review-round tests listed under As shipped; module-marked `fast`;
  `python3 tools/test_tiers.py --check` exits 0). With `test_federation_mail.py` and
  `test_federation_channel.py`: 107 passed.
- `python3 tools/affected_tests.py --guards --exclude-marks integration,tui,remote,perf --run uv run pytest -q -n 3`
  — 5401 passed, 6 skipped; the one failure, `test_conduct_skill_never_asks_the_human`, is fixed
  (see As shipped) and passes. The full suite was left to gating.
- `python3 tools/spec_lint.py` exits 0 and the ledger was regenerated.

### As shipped (2026-11-12)

Built to Design. Notes:

- `post_mail` calls `_post_one`, which calls the new `_ensure_action` (in `federation_host.py`,
  under `actions.lock`) after the inbox fsync and before the received append on the fresh path and
  both `dup` paths. `actions_path` lives in `federation_host.py` and `federation_actions.py`
  imports it with `_append_durable`; the AST of `post_mail` itself has none of the verb names.
- `drain_actions` also catches an `Exception` per `msg_uid` (logged), so one broken line does not
  stop the others; `queue.tick` imports `federation_actions` inside the function and logs an
  exception from `drain_actions` on the `federation_actions` logger.
- Two existing guard tests had to follow the Design text: `test_event_types_and_quiet_rules`
  (SPEC-0530) asserted no `host_*` type appeared in `nw-conduct/SKILL.md`; it now exempts
  `host_unreachable` and `host_mail_rejected`. `test_conduct_skill_never_asks_the_human` forbids
  "tell the human" outside its rule; it now removes the two one-line federation reports this spec
  adds, and asserts each appears exactly once.
- Review round 2 (code): a remote `campaign` must be one path component (not `.`/`..`, no `/`, `\\`
  or NUL), else `refused: campaign` (a tightening of check 4 so a value like `../x` never reaches a
  store path); `_read_state` enforces it, so `_answer` and `_await_parked` use the same rule. A
  `started`/`wait: PARKED` line whose campaign is no longer readable finishes `failed: ValueError:
  no readable campaign ...` (an addition; tested). Before re-requesting a park that "did not
  stick", `_await_parked` looks for a `park` ledger event at or after the `started` line's `ts`;
  when there is one the park did happen (someone resumed it since), so it appends `done` `ok`
  instead of parking again (an addition to the recovery flow; tested). `_append_durable` writes a
  leading newline when the file does not end in one, so a torn trailing line cannot swallow the
  next record (also applies to the inbox and `received.jsonl` appends).
- Review round 2 (tests): journal-before-verb ordering, plan/apply failure paths, deferred recovery
  paths, refusal order and edge inputs, reason pass-through, drain robustness, torn-line repair,
  ping-row edge cases, and the exact fsync sequence (inbox, `actions.jsonl`, `received.jsonl`).
- Manual verification, two data/config dirs, the host served on `127.0.0.1:18793` (8792 was
  already bound on this machine): `nw hosts add box --url http://127.0.0.1:18793` on the hub piped
  into `nw hosts enrol` on the host; `nw serve --federate --bind 127.0.0.1:18793`; one `QUEUED`
  campaign `C-1` in project `Demo` seeded on the host. Hub:
  `nw mailbox send --to steward:Demo@box --from conductor --action park --campaign C-1 --reason hold "park"`
  printed `queued 09442be4… → steward:Demo@box`; `nw queue tick` delivered it and the host's
  `actions.jsonl` held one `pending` line with that `msg_uid`. Host `nw queue tick`: `C-1` state
  `PARKED`, host conductor inbox one record `re` = that `msg_uid`, `answer`, `ok`; a second host
  tick left one record. Hub `nw queue tick`: the hub conductor inbox gained
  `re` = that `msg_uid`, from `steward:Demo@box`, text `ok`.

## Clock Log

Use `nw idea clock-in IDEA-562` at the start of an implementation session and
`nw idea clock-out IDEA-562` when pausing or finishing.

## Rollout / migration

1. With no `federation.json`, `drain_actions` returns without creating
   `actions.jsonl`. With no `hosts.json`, `--action` exits non-zero and writes nothing.
2. `--action` ships in this spec, so no earlier inbox line needs a backfill. A message
   committed by SPEC-0532 with an `action` object and a received line gains its
   pending action line only when that `msg_uid` is POSTed again.
3. After SPEC-0532 enrolment, the hub sends `--action park` or `--action resume`. The
   host's next `nw queue tick` drains the pending line. The hub's following tick
   pulls the conductor answer.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed and recorded.
- [x] No regressions (affected tests at the close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:

- The action line is fsynced before `received.jsonl`. `dup` writes a missing action
  line and does not append the inbox again.
- `drain_actions` runs in `queue.tick` after `_tick` and before `relay_tick`. Park
  uses `plan_park`/`apply_park` and resume uses `plan_resume`/`apply_resume`, both
  with `by="human"` and no decision record.
- A deferred park stays `started` with `wait` `PARKED` until the state is `PARKED`,
  then answers `ok` once. A plain `started` line is `unknown` and is not re-run. A
  raised verb is `failed` and is not re-run.
- Answers retry until the conductor inbox contains `re`. Only a refused action is
  copied to the host human.
- The conductor treats only an event with no `host` field as local. A remote event is
  mailbox park/resume or plain mail. The 30 minute escalation is a delivered
  `conductor_ping` `data.federation` row, at an age of at least 1800 seconds.
