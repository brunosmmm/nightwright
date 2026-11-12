---
id: SPEC-0532
title: "Exactly-once mail between hub and host"
status: accepted
owner: user
created: 2026-11-10
kind: feature
parent: SPEC-0310
depends_on: ["SPEC-0530", "SPEC-0595"]
supersedes: ["SPEC-0527"]
source_idea: IDEA-562
---

## Context

Signing note (2026-11-12, ESC-0072): [SPEC-0595](./0595-federation-wire-mac-domain-separation.md) domain-separates the wire MAC before this spec opens. Every endpoint here signs and verifies with `direction=`.

[SPEC-0527](./0527-federation-bounded-relay-mail-and-two-remote-verbs.md) replaced
[SPEC-0335](./0335-federation-over-a-networked-mailbox.md) after two campaigns diverged
(`SPEC-0335-20261101-02a3` at about 7,300 lines, `SPEC-0335-20261108-5cbb` at about 2,400
lines). Three of the mail gaps those campaigns left open belong here: a `dup` reply was
never acked, two concurrent sends could share one `msg_uid`, and a permanent failure was
retried.

This spec is part 3 of the split of SPEC-0527. It depends on
[SPEC-0530](./0530-hub-event-relay-health-and-read-only-host-views.md). SPEC-0530 depends
on the signed channel in
[SPEC-0529](./0529-federation-enrolment-and-the-signed-host-channel.md), and mail uses
that channel: `HubClient`, `FederateServer`, and `hosts.json`. The event relay skips
host `message` events and reserves `host_mail_rejected`. This spec delivers conductor
mail itself, stores `msg_uid`, and emits that one event. Enrolment, the event relay,
health, read-only views, and remote park and resume are the other parts, listed under
Non-goals.

What exists today and this spec reuses:

- The file mailbox ([SPEC-0183](./0183-file-based-mailbox-between-conductor-and-stewards.md),
  `src/nightwright/agentic/mailbox.py`). `validate_address` accepts `conductor`, `human`,
  and `steward:<Project>`. `send` assigns `seq` under the inbox flock, does not fsync,
  and does not store `msg_uid`. A send to `conductor` mirrors one `message` event whose
  required data keys are `msg_id`, `from`, `kind`, and `text`.
- SPEC-0530's `events.append` keywords `host` and `ts`, and `host_mail_rejected` already
  listed in `EVENT_TYPES` and `quiet.RULES` as `wake` with data keys `host`, `msg_uid`,
  `to`, and `reason`. This spec does not add an event type.
- SPEC-0530's `relay_tick`, which creates `cursor` as
  `{events_offset, events_origin_id, hub_events_offset}` for an enabled host and does
  not write mail keys. `queue.tick` calls `relay_tick` once.
- `events._locked` and `events._safe`, the flock helper and the filename sanitizer.

With no `hosts.json`, `nw mailbox send` and `validate_address` stay as they are. With
no `federation.json`, a conductor send record has no `msg_uid`.

## Goals / Non-goals

**Goals**

- The sender chooses `msg_uid` as `uuid.uuid4().hex`. Concurrent sends to one host get
  distinct ids, and both messages are delivered.
- `delivered`, `dup`, and `rejected` each append an outbox ack. A lost response or a
  5xx leaves the line pending; the retry is `dup` when the host inbox already has that
  `msg_uid`. A signature-verified 4xx on `POST /v1/mail` is terminal.
- The hub pulls the host's `conductor` inbox with a byte offset. Delivery stores that
  `msg_uid` on the hub conductor record and appends exactly one `message` event with
  `host` set and, when the campaign is non-empty, `campaign` `<campaign>@<host>`.
- Bare addresses still go through `validate_address`. That function is unchanged. A
  bare address is local even when `hosts.json` exists.
- `POST /v1/mail` only writes mail: the destination inbox line and the dedupe line.

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
- Part 4, remote park and resume: the action line is fsynced before the dedupe record,
  `dup` repairs a missing action, and `drain_actions` runs in the host runner tick. Park
  uses `plan_park`/`apply_park` and resume uses `plan_resume`/`apply_resume`, with `by
  human`. A deferred park stays started until state is `PARKED`, then answers `ok` once.
  Answers retry until the conductor inbox contains `re`. A refused action is also copied
  to the host human. The conductor treats only events with no `host` field as local; a
  remote event is mailbox park/resume or plain mail, and the 30 minute escalation is a
  delivered `conductor_ping` `data.federation` row. Depends on the mail spec.
- Editing `relay_tick`, `EVENT_TYPES`, or `quiet.RULES`. `relay_tick` still does not
  write `mail_seq` or `hub_inbox_offset`.
- `nw mailbox send --action`, `drain_actions`, and any queue mutation. A posted
  `action` object is stored on the inbox record and is not run.
- Resolving a bare `steward:<P>` or `human` onto a host. Remote mail needs an explicit
  `@<host>`.

## Decision

- `msg_uid` is `uuid.uuid4().hex`, chosen by the sender and written once under
  `events._locked`. There is no shared sequence file.
- Hub-to-host mail is an outbox line. `delivered`, `dup`, and `rejected` each end the
  retry by appending an ack. A lost response or a 5xx does not ack; the next attempt is
  `dup` when that `msg_uid` is already in the destination inbox, and `delivered` when it
  is not. A signature-verified 4xx on `POST /v1/mail` acks every line in that POST as
  `rejected` and does not POST them again.
- Host-to-hub mail is `GET /v1/mail`, which seeks to a byte offset in the host's
  `conductor` inbox. The hub stores the original `msg_uid` on the conductor record and
  appends one `message` event tagged `host`. A non-empty campaign becomes
  `<campaign>@<host>`. Write, fsync, then checkpoint. A crash between the append and the
  checkpoint does not write a second inbox line or a second event.
- `mailbox.validate_address` is not modified. Bare addresses call it and stay local.
  `@<host>` is parsed only when `hosts.json` exists. With no `hosts.json`, an `@`
  address is rejected by `validate_address` as it is today.
- `POST /v1/mail` appends the inbox line, fsyncs, appends the dedupe line, fsyncs, and
  returns. It does not park, resume, or drain actions.
- `queue.tick` calls `relay_mail` once after `relay_tick`. `relay_mail` does its own
  fetches and waits at most `POOL_WAIT_S` (2.5 s). A result that is still running is
  not applied. Appends and cursor updates run on the calling thread.

## Design

### Modules

| Module | Add |
|---|---|
| `src/nightwright/agentic/federation_host.py` | `read_mail_page`, `post_mail`, `GET /v1/mail`, `POST /v1/mail` on the existing `FederateServer`. |
| `src/nightwright/agentic/federation.py` | `relay_mail`, `enqueue_remote`, `deliver_conductor`. Still no `queue` import. |
| `src/nightwright/agentic/mailbox.py` | When `federation.json` exists, a send to `conductor` stores `msg_uid`. `validate_address` is unchanged. |
| `src/nightwright/agentic/cli_mailbox.py` | `nw mailbox send` enqueues `@<host>` when `hosts.json` exists. |
| `src/nightwright/agentic/queue.py` | `tick` calls `relay_mail` once, after `relay_tick`, imported inside the function. |

`hosts.py`, `federation.py`, and `cli_hosts.py` still must not import `queue`. The host
project check uses `rules.projects_payload`. `federation_host.py` still calls no queue
attribute except `queue_payload`, and it still does not reference `subprocess`.

Constants already on `federation` from SPEC-0530: `POOL_WAIT_S = 2.5`. This spec adds
`MAIL_LIMIT = 200` and `MAIL_MAX_BYTES = 1048576`.

### `msg_uid` and the outbox

A remote send calls `federation.enqueue_remote(cfg, *, to, frm, text, kind, campaign,
re, needs_reply)`. `msg_uid` is `uuid.uuid4().hex` (32 lowercase hex characters). The
line is one UTF-8 `json.dumps` plus `\n`, written with one `os.write` while
`events._locked` holds `<agentic_dir>/mailbox/outbox-<host>.lock`. The file is
`<agentic_dir>/mailbox/outbox-<host>.jsonl`, with `<host>` passed through `events._safe`.
The write is fsynced before `enqueue_remote` returns. An fsync failure raises and the
CLI exits non-zero.

The outbox line's keys are `msg_uid`, `to`, `from`, `kind`, `text`, `campaign`, `re`,
and `needs_reply`. `to` is the local part, `human` or `steward:<Name>`, with no
`@<host>`. `campaign` and `re` are strings or null.

An ack line in the same file is `{"msg_uid", "result", ...}`. `result` is `delivered`,
`dup`, or `rejected`. `delivered` and `dup` include integer `seq`. `rejected` includes
string `reason` and no `seq`. A message line is pending when no ack line in the file
has its `msg_uid`. Pending lines are taken in file order, at most `MAIL_LIMIT` per
host per tick.

`nw mailbox send` parses `--to` as follows.

- `hosts.load` is `[]` (the file is missing, empty, or unreadable): call the existing
  local path. `validate_address` sees the raw string. No outbox file is created.
- `hosts.load` is non-empty and the address contains no `@`: the existing local path.
  A bare address is never routed to a host.
- `hosts.load` is non-empty and the address contains `@`: split on the last `@` into
  local part and host name, then `enqueue_remote`. Do not call `validate_address`.

The remote path refuses, writes no outbox line, and exits non-zero when the host name
is absent from the registry, when `enabled` is false, when the local part is not
`human` and not `steward:<name>` with a non-empty name, or when `status_cache` is an
object whose `projects` is a list that does not casefold-match the project. A null
`status_cache`, or a `projects` value that is not a list, skips the membership check.
A casefold hit stores `to` as `steward:` plus the cache's spelling. With a null cache,
`to` keeps the typed project text, stripped.

Success on the remote path prints `queued <msg_uid> → <local>@<host>` through the same
console the local path uses. `--json` prints the outbox record. `--from` still defaults
to `NW_MAILBOX_ADDR` and is checked with `validate_address` before enqueue.

On a host, `mailbox.send` to `conductor` adds `msg_uid` when
`<config_dir>/secrets/federation.json` is a regular file. The id is chosen in the
sender, before the inbox append. Sends to any other address do not add the key. When
the file is absent, the record has no `msg_uid` key. `send` still calls
`validate_address` for `to` and `from`. The function does not import `federation`.

### `GET /v1/mail`

The route is authenticated and signed like every other enrolled route in SPEC-0529.
`GET /v1/mail` and `POST /v1/mail` are success paths. Any other method on this path is
405 `{"error": "method_not_allowed"}`, signed while enrolled.

`federation_host.read_mail_page(path, offset, limit)` reads
`mailbox._inbox_path(cfg, "conductor")`. The boundary check, the one-byte lookbehind,
the partial-line rule, the non-JSON rule, and the byte cap match `read_events_page` in
SPEC-0530, with `MAIL_LIMIT` and `MAIL_MAX_BYTES` in place of the events caps. `offset`
omitted means `0`. `limit` omitted, not a base-10 integer, or outside `1..200` means
`200`. A non-integer offset, a negative offset, an offset past the end, or an offset
whose preceding byte is not `\n` is 409 `{"error": "bad_offset"}` with
`Content-Type: application/json`, signed while enrolled. The handler decodes no line
that starts before `offset`. It does not hold the inbox flock.

A 200 body is
`{"schema": "nw.federation.mail.page.v1", "messages": [...], "next_offset": <int>}`.
`messages` are the decoded objects, in file order, with no added fields. An empty or
missing file at offset `0` returns `messages: []` and `next_offset: 0`. The request
line the hub sends is `/v1/mail?offset=<n>&limit=200`, `offset` then `limit`.

### `POST /v1/mail`

The body is a JSON object `{"messages": [...]}`. A body that is not a JSON object, or
whose `messages` is not a list, is 400 `{"error": "bad_request"}`, signed while
enrolled. The handler writes nothing for that request. An empty list is 200
`{"schema": "nw.federation.mail.v1", "results": []}`.

The handler holds `events._locked` on `<agentic_dir>/federation/mail.lock` for the
whole batch. For each element, in order:

1. Reject with `bad_msg_uid` when `msg_uid` is not 32 lowercase hex characters, with
   `bad_message` when `kind` is not in `mailbox.KINDS`, when `text` is empty after
   strip, when `from` is not a non-empty string, or when the element is not an object,
   and with `bad_address` when `to` is not `human` and not `steward:<project>` for a
   project in `rules.projects_payload`. The project match is casefold, and the stored
   `to` uses the registry spelling. A rejection writes no inbox line. Its result is
   `{"msg_uid", "result": "rejected", "reason"}`. `msg_uid` in that result is the posted
   value when that value is a string, and `""` otherwise, so a bad id such as `abc` still
   matches the outbox line that sent it.
2. If `received.jsonl` already contains that `msg_uid`, the result is `dup` and the
   inbox is not appended.
3. Otherwise rescan the target inbox from the `inbox_end` of the last
   `received.jsonl` line for that inbox, or from byte `0` when there is none. The
   rescan decodes no byte before that offset. A line with that `msg_uid` is `dup`:
   append the missing received line, fsync `received.jsonl`, and do not append the
   inbox again.
4. Otherwise append one `nw.message.v1` record, fsync the inbox, append the received
   line, and fsync `received.jsonl`. The result is `delivered` with the assigned
   `seq`.

The inbox record's keys are `schema`, `id`, `seq`, `ts`, `from`, `to`, `kind`,
`campaign`, `re`, `text`, `needs_reply`, and `msg_uid`. `id` is `<to>:<seq>`. `seq` is
one past the last integer `seq` in that inbox. `ts` is `store.now_iso()`. `from` is the
sender string, not passed through `validate_address`. `campaign` and `re` are copied
when they are strings, otherwise null. `needs_reply` is the posted bool when it is a
bool, otherwise true for `question` and `request` and false for `answer` and `info`.
When `action` is a JSON object it is stored under `action`. The handler does not read
`action` and does not call `drain_actions`, `plan_park`, `apply_park`, `plan_resume`,
`apply_resume`, or any other queue writer.

`<agentic_dir>/federation/received.jsonl` gets one line
`{"msg_uid", "inbox", "inbox_end"}`. `inbox` is the canonical `to`. `inbox_end` is the
inbox file size after the inbox fsync.

If an inbox fsync or a received fsync raises, the response is 500
`{"error": "commit_failed"}` and the handler stops the batch. Lines already fsynced
stay on disk. The hub does not use a 500 body as results.

A finished batch returns 200
`{"schema": "nw.federation.mail.v1", "results": [...]}` with one result per element, in
order, including rejections. `Content-Type` is `application/json`.

### Hub relay (`relay_mail`)

`queue.tick` calls `federation.relay_mail(cfg)` once, after the `relay_tick` call
returns, including when `relay_tick` raised and was logged. The import stays inside
`tick`. An exception from `relay_mail` is logged and does not fail the tick.

`relay_mail(cfg, *, now=None)` uses `now` or `datetime.now().astimezone()`. When
`hosts.load` returns `[]` it returns without creating a file, opening a socket, or
starting a pool.

It does not create a `cursor` object. SPEC-0530's `relay_tick` does that, and `tick`
calls `relay_tick` first. An enabled host with no `cursor` is skipped. A disabled host
is skipped and gains no mail key. For each enabled host whose `cursor` lacks
`mail_offset`, before any call is submitted and in registry order, `hosts.update` sets
only these keys inside the existing `cursor`:

- `mail_offset`: `0`
- `hub_inbox_offset`: the current length of the hub conductor inbox, or `0` when the
  file is absent
- `hub_mail_events_offset`: the current length of the hub `events.jsonl`, or `0` when
  the file is absent

`events_offset`, `events_origin_id`, `hub_events_offset`, `health`, and `status_cache`
are left as they were. This update does not write `mail_seq`. `relay_tick` still writes
neither `mail_seq` nor `hub_inbox_offset`.

The pool then submits one task per remaining host. The task does HTTP only. It does not
append and it does not call `hosts.update`.

1. `GET /v1/mail?offset=<mail_offset>&limit=200` through `HubClient`.
2. When that call is a verified page or a verified 409, and the host has at least one
   pending outbox line, `POST /v1/mail` with
   `{"messages": [<up to MAIL_LIMIT pending lines>]}`. The POST body is
   `json.dumps(..., ensure_ascii=False, separators=(",", ":")).encode("utf-8")` and is
   the signed body. A GET transport failure, a bad signature, or a dropped GET does not
   POST.

`concurrent.futures.wait` is given `POOL_WAIT_S`. A future that is not done is dropped:
the tick never calls `result()` on it. `Executor.shutdown(wait=False, cancel_futures=True)`
follows the wait. Dropping a task does not ack and does not move `mail_offset`.

A verified response is one whose signature `HubClient` accepts. An unsigned, tampered,
or stale response is a transport failure on that call. `HubClient` returns the status
and body of a verified response of any status so this relay can tell 4xx from 5xx.
SPEC-0529's status path still treats a non-200 as a failed check.

Apply runs on the calling thread, in registry order, after `wait`. Hosts are not
applied concurrently.

**Conductor page.** A verified 200 with schema `nw.federation.mail.page.v1` is a page.
For each message whose `msg_uid` is 32 lowercase hex, `deliver_conductor` runs. A
message without that `msg_uid` is consumed and not delivered, so a conductor line
written before `federation.json` existed does not stall the cursor. Then checkpoint
`mail_offset` to `next_offset`, `hub_inbox_offset` to the hub conductor inbox size, and
`hub_mail_events_offset` to the hub events size. If a delivery fsync raises, skip the
checkpoint. The next tick reads the same offset and dedupe leaves one copy.

**Mail 409.** A verified 409 whose JSON `error` is `bad_offset` sets `mail_offset` to
`0` and leaves `hub_inbox_offset` and `hub_mail_events_offset` unchanged. It appends no
`host_cursor_reset` and no inbox line. The next tick reads from `0`. `msg_uid` dedupe
stops a second copy. Any other GET failure leaves `mail_offset` unchanged.

**Outbox results.** A verified 200 with schema `nw.federation.mail.v1` and a list
`results` acks each element whose `result` is `delivered`, `dup`, or `rejected` and
whose `msg_uid` equals a `msg_uid` in that POST. Any other element leaves its line
pending. Any other status-200 shape leaves every posted line pending. A verified status in
`400..499` acks every line that was in that POST as `rejected`. The reason is the
body's `error` string when the body is a JSON object with a string `error`, truncated
to 200 characters, otherwise `http_<status>`. A verified status in `500..599`, a
transport failure, or a response the client never reads leaves every posted line
pending.

Ack lines are appended under the outbox lock and the outbox is fsynced. If that fsync
raises, the lines stay pending when the ack bytes are not in the file. For each
`rejected` ack, before the ack write, append one `host_mail_rejected` unless the events
tail from `hub_mail_events_offset` already has one whose `data.host` is this host and
whose `data.msg_uid` is this id. The event uses `events.append` with `host` set to the
registry name, `by` `"federation"`, `campaign` null, no `origin`, and `data`
`{"host", "msg_uid", "to", "reason"}`. `to` is the outbox line's local `to`. Fsync the
events file before the ack. If that fsync raises, do not ack. A later tick that sees
`rejected` again finds the event in the tail and does not append a second one.

### Conductor delivery

`deliver_conductor(cfg, message, host)` writes the hub conductor inbox and one event.

1. Rescan the hub conductor inbox from `cursor.hub_inbox_offset`. Decode no earlier
   byte. If `msg_uid` is already there, keep that record's `id` and do not append
   another inbox line.
2. Otherwise append one `nw.message.v1` record and fsync the inbox, holding
   `mailbox._inbox_lock(cfg, "conductor")` for the `seq` read and the append. `id` is
   `conductor:<seq>`. `to` is `conductor`. `from` is `<sender>@<host>`, where `<sender>`
   is the host record's `from` string. `msg_uid` is the host record's `msg_uid`. A
   non-empty string `campaign` is stored as that string plus `@` plus the registry
   name. Any other campaign is null. `kind`, `text`, and `re` are copied the same way
   `post_mail` copies them. `ts` is the host `ts` when that value is a string,
   otherwise `store.now_iso(now)`. This path does not call `mailbox.send` and does not
   call `validate_address`.
3. Rescan the hub `events.jsonl` from `cursor.hub_mail_events_offset`. If that tail
   already has a `message` whose `data.msg_uid` equals this id and whose `host` equals
   this registry name, do not append another event. Otherwise `events.append` one
   `message` and fsync the events file. The event has `host` set, no `origin`, `by`
   equal to the rewritten `from`, `spec_id` null, and `project` null. `campaign` uses
   the same `@<host>` rule as the inbox record. `ts` is the host `ts` when that is a
   string. `data.msg_id` is the inbox `id` from step 1 or step 2, not a new `seq`.
   `data` keys are exactly `msg_id`, `from` (the rewritten from), `kind`, `text`, `re`,
   and `msg_uid`.

Both rescans parse only the tail. A crash after either append and before the page
checkpoint is covered by those tails, so the retry writes neither a second inbox line
nor a second `message` event.

### Bare addresses

`mailbox.validate_address` keeps its current body: `conductor` and `human` unchanged,
`steward:<Project>` canonicalized case-insensitively against `projects_payload`, and
any other string raising `ValueError`. The source does not reference `hosts`,
`federation`, `@`, or `msg_uid`.

## Alternatives considered

- **Page the conductor inbox by `seq` (`after=<seq>`, cursor `mail_seq`).** Each poll
  would read the whole file inside a 2 second timeout. A byte offset keeps the poll to
  one page, the same rule SPEC-0530 uses for events. `mail_seq` is not a cursor key.
- **Deliver host mail by calling `mailbox.send`.** `validate_address` rejects
  `sender@<host>`, and `send` neither fsyncs nor stores a caller-supplied `msg_uid`.
  `deliver_conductor` writes the record and the one event itself.
- **Retry a signature-verified 4xx.** A permanent rejection would be POSTed on every
  tick. The ack is what stops it. A 5xx still retries, because the host may have
  committed.
- **A shared counter file for `msg_uid`.** Two processes can read the same counter.
  `uuid4` needs no shared file. The flock is only there so the outbox lines do not
  interleave.
- **Run a verb inside `POST /v1/mail`.** The HTTP timeout, the dedupe, and the verb's
  failure modes would be one request again. This handler only writes mail. Part 4 runs
  park and resume in the host tick.
- **Put the mail fetches inside `relay_tick`'s pool.** SPEC-0530 fixes that pool's
  membership and its tests. A second wait, capped at the same `POOL_WAIT_S`, bounds
  mail without editing the event pool.

## Acceptance criteria

- [ ] `enqueue_remote` stores `msg_uid` as 32 lowercase hex from `uuid.uuid4().hex`
      and fsyncs the outbox before it returns. An fsync failure raises and the CLI
      exits non-zero. Two `multiprocessing` processes that each send 20 messages to
      one enabled host produce 40 distinct `msg_uid`s, and one drained tick delivers
      all 40, each once. The outbox append is one `os.write` under `events._locked`.
      `--json` prints that outbox record. A successful remote send prints
      `queued <msg_uid>`.
- [ ] A `delivered` result, a `dup` result, and a `rejected` result each append one ack
      with that `result`. A later `relay_mail` POST body does not contain those
      `msg_uid`s. `delivered` and `dup` acks include `seq`. A `rejected` ack includes
      `reason` and no `seq`.
- [ ] When the host commits a POST and the client then raises before the hub reads the
      response, the line stays pending, the retry result is `dup`, the destination
      inbox holds one copy, and the outbox is acked. The same holds when the inbox
      fsync raises after the append and the handler returns 500: the retry is `dup` and
      the inbox holds one copy. When the handler returns 500 before any append, the
      retry is `delivered` and the inbox holds one copy.
- [ ] A signature-verified status in `400..499` on `POST /v1/mail` acks every line in
      that POST as `rejected`, emits one `host_mail_rejected` per line, and a later
      tick does not POST those ids. A 400 `{"error": "bad_request"}` writes no inbox
      line and the ack reason is `bad_request`. An unsigned response, a timeout, or a
      connection error acks nothing.
- [ ] A message to a project the host does not have, a bad `kind`, an empty `text`, and
      a `msg_uid` that is not 32 lowercase hex are each a 200 `rejected` with reason
      `bad_address`, `bad_message`, or `bad_msg_uid`. Each is acked, is absent from the
      next POST, writes no inbox line, and emits one `host_mail_rejected`. A second
      tick emits no second event for that `msg_uid`.
- [ ] `GET /v1/mail` returns `Content-Type: application/json` and
      `nw.federation.mail.page.v1`. Offset `0` on a missing file returns
      `messages: []` and `next_offset: 0`. With 10,000 complete lines before `offset`,
      the page parser decodes only the lines from `offset` onward. The page stops at
      `limit` lines, or at 200 when `limit` is omitted or outside `1..200`, or at
      1,048,576 raw bytes, and a single first line longer than that byte limit is still
      returned. A trailing partial line is not consumed. A non-JSON line is consumed
      and omitted. A mid-line offset, an offset past the end, and a negative or
      non-integer offset each return signed 409 `{"error": "bad_offset"}` while
      enrolled. `PUT /v1/mail` is a signed 405 `{"error": "method_not_allowed"}`.
      An empty `messages` list is 200 `nw.federation.mail.v1` with `results: []` and
      writes nothing. The hub request line is `/v1/mail?offset=<n>&limit=200`.
- [ ] A verified mail 409 sets `mail_offset` to `0` and appends no `host_cursor_reset`.
      The following read from `0` delivers no second copy of a `msg_uid` already in the
      hub conductor inbox. An unsigned mail response leaves `mail_offset` unchanged and
      appends nothing.
- [ ] One host conductor message with a `msg_uid` and a non-empty campaign becomes one
      hub conductor record that stores that `msg_uid`, with `from` ending in `@<host>`
      and `campaign` `<campaign>@<host>`, and exactly one hub `message` event with
      `host` equal to the registry name, the same campaign, `data.msg_uid` equal to
      that id, `data.msg_id` equal to the hub inbox `id`, and no `origin`. A missing
      campaign stays null on the record and on the event. A conductor line with no
      `msg_uid` is not delivered, and `mail_offset` still advances past it. A second
      `relay_mail` adds neither a second record nor a second event.
      `deliver_conductor` does not call `mailbox.send` or `validate_address`. A crash
      that leaves the inbox line and loses the event reuses that line's `id` as
      `data.msg_id` and still appends only one event.
- [ ] If the hub inbox fsync or the message-event fsync raises, `mail_offset` is
      unchanged. The next successful tick leaves one inbox record and one `message`
      event. If the checkpoint raises after both fsyncs, the next tick's rescan from
      `hub_inbox_offset` and `hub_mail_events_offset` also leaves one of each, and
      neither rescan decodes bytes before its offset.
- [ ] `POST /v1/mail` with an `action` object stores that object on the inbox record,
      leaves campaign state unchanged, and calls none of `drain_actions`, `plan_park`,
      `apply_park`, `plan_resume`, or `apply_resume`. An AST walk of `post_mail` finds
      none of those names. A `dup` whose inbox line is already durable appends only the
      missing `received.jsonl` line and does not append the inbox again.
- [ ] `validate_address` still returns `conductor`, `human`, and the canonical
      `steward:<Project>`, and it still raises `ValueError` for `steward:P@host`,
      `human@host`, and `bob`. Its source contains none of `hosts`, `federation`, `@`,
      and `msg_uid`. `mailbox.py` does not import `federation`. With no `hosts.json`,
      `nw mailbox send --to steward:<P>@<host>` exits non-zero, creates no outbox
      file, and creates no `hosts.json`. With `hosts.json` present, a bare
      `steward:<P>` is delivered locally and writes no outbox line. With no
      `federation.json`, a conductor send record has no `msg_uid` key. With that file
      present, a conductor send stores a 32-hex `msg_uid` and a send to `human` does
      not. `steward:demo@<host>` with `status_cache.projects` of `["Demo"]` stores
      `to` as `steward:Demo`.
- [ ] `nw mailbox send --to human@<host>` to an unknown host, to a disabled host, or to
      a project absent from a list `status_cache.projects` exits non-zero and writes no
      outbox line. The same send to an enabled host with a null `status_cache` appends
      one pending line. `relay_mail` does not call a disabled host, and it does not call
      an enabled host that has no `cursor` object. After `on`, the next `relay_mail`
      can deliver.
- [ ] `POOL_WAIT_S` is `2.5`, and `relay_mail` passes that value to
      `concurrent.futures.wait`. A task still running when `wait` returns is not
      applied: no ack and no `mail_offset` change, including after the task finishes.
      When that task had already committed the POST, the next applied tick gets `dup`
      and the inbox holds one line. A GET transport failure does not POST. A verified
      200 whose body is not `nw.federation.mail.v1` with a `results` list leaves the
      posted lines pending. Apply runs on the calling thread, in registry order.
      `queue.tick` calls `relay_mail` once after
      `relay_tick`. A raising `relay_mail` does not fail the tick. `relay_tick` still
      does not add `mail_seq` or `hub_inbox_offset`. After `relay_mail`, an enabled
      host's `cursor` also has `mail_offset`, `hub_inbox_offset`, and
      `hub_mail_events_offset`, and its event cursor keys are unchanged.
- [ ] With no `hosts.json`, `relay_mail` and `queue.tick` create no `hosts.json` and no
      outbox file. An AST walk of `hosts.py`, `federation.py`, and `cli_hosts.py` still
      fails on any `queue` import, including `from . import queue as Q`. The only
      `queue` attribute call in `federation_host.py` is still `queue_payload`.

## Test plan

- **Automated tests:** `tests/test_federation_mail.py`, module-marked
  `pytest.mark.fast`. Two configs (hub data dir, host data dir). The host
  `FederateServer` listens on `127.0.0.1:0` in a thread and is enrolled with the
  SPEC-0529 files. The hub record's `cursor` is the SPEC-0530 object unless a test is
  checking that `relay_mail` skips a missing cursor. Tests do not sleep. Host conductor
  lines are written through `mailbox.send` while `federation.json` exists unless a test
  says otherwise.
  - `test_concurrent_sends_have_distinct_uids` — two `multiprocessing` processes each
    call `enqueue_remote` 20 times for one enabled host. The outbox has 40 lines, 40
    distinct 32-hex `msg_uid`s, and one `relay_mail` delivers 40 inbox records. The
    outbox fsync runs before `enqueue_remote` returns; a raising fsync makes the CLI
    exit non-zero. `--json` output is the outbox record, and the text line starts with
    `queued`.
  - `test_acks_stop_retry_for_each_result` — stub results `delivered`, `dup`, and
    `rejected`. Each uid gains one ack with the matching `result`. `delivered` and
    `dup` include `seq`. `rejected` has `reason` and no `seq`. The next `relay_mail`
    POST body contains none of the three ids.
  - `test_lost_response_acks_dup` — the real `POST /v1/mail` commits on the host, then
    the client raises before the hub reads the body. The line stays pending. The retry
    result is `dup`, the inbox length is 1, and the uid is acked.
  - `test_lost_response_without_dedupe_would_duplicate` — the same wrap, with both the
    `received.jsonl` lookup and the inbox rescan patched to miss. The inbox length is
    2. This is the control that `test_lost_response_acks_dup` fails closed if the
    `msg_uid` check is removed.
  - `test_fsync_500_retries_into_dup` — the inbox append happens and the inbox fsync
    raises, so the handler returns 500 `commit_failed`. The retry result is `dup` and
    the inbox length is 1. A second case returns 500 before the append: the retry
    result is `delivered` and the inbox length is 1.
  - `test_verified_4xx_is_terminal` — a signed 400 `{"error": "bad_request"}` writes
    no inbox line, acks `rejected` with reason `bad_request`, and appends one
    `host_mail_rejected`. The next tick does not POST that uid and does not append a
    second event. A stubbed signed 404 acks `http_404` the same way. An unsigned 400
    acks nothing and the next tick POSTs the uid again.
  - `test_rejected_address_is_acked_once` — a project the host lacks, kind `nope`,
    text `" "`, and msg_uid `abc` are each 200 `rejected` with `bad_address`,
    `bad_message`, and `bad_msg_uid`. None of them is in the inbox. Each emits one
    `host_mail_rejected`. The next tick POSTs none of them and emits no second event.
  - `test_mail_page_seeks_and_caps` — 10,000 lines before `offset`, then 3 short
    lines. The parser's decoded-line count is 3. `next_offset` sits after those 3. A
    first line larger than `MAIL_MAX_BYTES` is returned alone. A trailing partial line
    is absent and `next_offset` does not pass it. A non-JSON line is skipped and
    consumed. An omitted limit and a limit of `0` both cap at 200. `PUT /v1/mail` is
    a signed 405 `{"error": "method_not_allowed"}`. POST `{"messages": []}` is 200
    `nw.federation.mail.v1` with `results: []` and creates no inbox file. The hub
    request path is `/v1/mail?offset=<n>&limit=200`.
  - `test_mail_bad_offset_is_409` — a mid-line offset, an offset past the end, and
    `offset=-1` each get a signed 409 `{"error": "bad_offset"}`. Offset `0` on a
    missing file is an empty page.
  - `test_mail_409_resets_without_duplicates` — deliver one `msg_uid`, force the stored
    `mail_offset` to a mid-line value, and run `relay_mail`. `mail_offset` becomes `0`,
    the hub log gains no `host_cursor_reset`, and the next tick adds no second
    conductor record and no second `message` event. An unsigned GET leaves
    `mail_offset` unchanged and appends nothing.
  - `test_conductor_delivery_one_event` — one host conductor message with campaign
    `SPEC-1` and a `msg_uid` produces one hub record with that `msg_uid`, `from`
    ending `@<host>`, and campaign `SPEC-1@<host>`, plus one `message` event with
    `host`, that campaign, `data.msg_uid`, and no `origin` key. A message with
    `campaign` null keeps null on both. A raw conductor line with no `msg_uid` is
    absent from the hub inbox, and `mail_offset` moves past it. A second `relay_mail`
    adds no record and no event. A spy shows `deliver_conductor` calls neither
    `mailbox.send` nor `validate_address`. Deleting the `message` event after the
    inbox fsync and before the checkpoint leaves one inbox id; the retry's event uses
    that same `data.msg_id` and the log has one `message` for that `msg_uid`.
  - `test_delivery_fsync_and_checkpoint` — monkeypatch the hub inbox fsync to raise
    once. `mail_offset` is unchanged. The next tick stores one record and one event.
    Monkeypatch `hosts.update` to raise once after both fsyncs. Pad 1,000 bytes before
    each of `hub_inbox_offset` and `hub_mail_events_offset` and assert neither rescan
    decodes those bytes. The retry stores one record and one event.
  - `test_post_mail_writes_mail_only` — POST a message whose `action` is
    `{"verb": "park"}`. The inbox record's `action` equals that object, and the
    campaign state file is unchanged. The AST of `post_mail` contains none of
    `drain_actions`, `plan_park`, `apply_park`, `plan_resume`, and `apply_resume`.
    A second POST of the same `msg_uid` after deleting `received.jsonl` returns `dup`,
    appends one received line, and leaves the inbox length at 1.
  - `test_bare_addresses_stay_local` — `validate_address` accepts `conductor`,
    `human`, and a known `steward:` name, and raises `ValueError` on
    `steward:P@host`, `human@host`, and `bob`. `inspect.getsource(validate_address)`
    contains none of `hosts`, `federation`, `@`, and `msg_uid`. With no `hosts.json`,
    the CLI send to `steward:P@box` exits non-zero and creates neither an outbox nor
    `hosts.json`. With `hosts.json`, a bare `steward:` send appends the local inbox
    and no outbox line. With no `federation.json`, a conductor record has no
    `msg_uid`. With the file, the conductor record has a 32-hex `msg_uid` and a
    `human` record does not. The AST of `mailbox.py` has no `federation` import.
    `steward:demo@box` with `status_cache.projects` `["Demo"]` stores `to`
    `steward:Demo`.
  - `test_send_refuses_unknown_off_and_missing_project` — unknown host, `enabled`
    false, and `status_cache.projects` of `["Other"]` each make the CLI exit non-zero
    with no outbox line. A null `status_cache` on an enabled host appends one line.
    `relay_mail` does not open a connection to a disabled host, or to an enabled host
    whose record has no `cursor`. After `on`, a pending line is delivered.
  - `test_mail_pool_and_tick_order` — `relay_mail` calls `concurrent.futures.wait` with
    `timeout` equal to `POOL_WAIT_S`, and `POOL_WAIT_S` is `2.5`. A task that sleeps
    past a patched `0.05` wait does not ack and does not change `mail_offset` after it
    returns. A task that finishes the POST and is then dropped leaves the line pending;
    the next applied tick gets `dup` and the inbox length is 1. A GET that raises
    `OSError` is followed by no POST. A verified 200 body of `{}` leaves the outbox
    line pending. Apply callbacks run on the main thread, in registry order.
    `queue.tick` with `relay_tick` and `relay_mail` both monkeypatched calls
    `relay_mail` once, after `relay_tick`. A `relay_mail` that raises `RuntimeError`
    still lets `tick` return a report. After a real `relay_tick` and `relay_mail`, the
    cursor keeps its event keys and also has `mail_offset`, `hub_inbox_offset`, and
    `hub_mail_events_offset`. A `relay_tick` with no `relay_mail` leaves the record
    without `mail_seq` and without `hub_inbox_offset`.
  - `test_inert_and_no_queue_import` — no `hosts.json`: `relay_mail` and `queue.tick`
    create none and create no outbox. The SPEC-0529 AST walk still passes on
    `hosts.py`, `federation.py`, and `cli_hosts.py`. `federation_host.py` still
    references `queue` only as `queue_payload`.
- **Manual verification:**
  1. Create two empty directories and export `NW_DATA_DIR` and `NW_CONFIG_DIR` for the
     hub, then the host.
  2. On the hub, run `nw hosts add box --url http://127.0.0.1:8792` and pipe stdout
     into the host's `nw hosts enrol`.
  3. On the host, run `nw serve --federate`. On the host, run
     `nw mailbox send --to conductor --from human --campaign SPEC-1 "hello"`.
  4. On the hub, run `nw queue tick` once. The hub conductor inbox has one record with
     a `msg_uid` and `from` ending `@box`, and the hub `events.jsonl` has one
     `message` event with `host` `box` and campaign `SPEC-1@box`. A second tick adds
     neither.
  5. On the hub, run `nw mailbox send --to human@box --from conductor "ping"`, then
     `nw queue tick`. The host `human` inbox has one record. A second tick does not
     add another. `nw mailbox send --to steward:Missing@box --from conductor "no"`
     then one tick appends one `host_mail_rejected`, and the next tick does not append
     a second.
  6. On the hub, `nw mailbox send --to human --from conductor "local"` appends the
     hub's local `human` inbox and no `outbox-box.jsonl` line.
  7. Record the commands and the output under an `As shipped` heading in this spec.
  Enrolling a second physical machine is rollout, not part of this spec's definition of
  done.
- **Regression guard:** at the close gate, `python3 tools/affected_tests.py` plus
  `python3 tools/spec_lint.py`. `tests/test_agentic_mailbox.py`,
  `tests/test_federation_channel.py`, and `tests/test_federation_relay.py` cover the
  local mailbox, the channel, and the event relay. The relay test that reads the
  record after `relay_tick` still sees no `mail_seq` and no `hub_inbox_offset`.

## Clock Log

Use `nw idea clock-in IDEA-562` at the start of an implementation session and
`nw idea clock-out IDEA-562` when pausing or finishing.

## Rollout / migration

1. With no `hosts.json` and no `federation.json`, mailbox send and `validate_address`
   are unchanged, and `queue.tick` does not open a mail connection.
2. After SPEC-0529 enrolment, the next hub `nw queue tick` lets `relay_tick` create the
   event cursor and then lets `relay_mail` set `mail_offset` to `0`. Conductor lines
   that already lack `msg_uid` are skipped. Later host sends to `conductor` carry a
   `msg_uid` and are delivered once.
3. There is no remote verb in this spec. A message whose body contains `action` is
   stored and left for part 4.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass; manual steps performed and recorded.
- [ ] No regressions (affected tests at the close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:

- `msg_uid` is `uuid4().hex`. A shared counter is not used.
- `delivered`, `dup`, and `rejected` all ack. A lost response or a 5xx retries, and the
  retry is `dup` when the inbox already has the id. A signature-verified 4xx on
  `POST /v1/mail` is terminal.
- `GET /v1/mail` seeks to a byte offset. A verified 409 on that GET resets
  `mail_offset` to `0` and does not ack the outbox. `mail_seq` is not written.
- `POST /v1/mail` only writes the inbox line and the dedupe line.
- Bare addresses stay on `validate_address`. Remote addresses require `@<host>`.
- The one hub `message` event for host mail is written here. SPEC-0530 keeps skipping
  `message` events on the event relay.
