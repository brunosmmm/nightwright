---
id: SPEC-0530
title: "Hub event relay, health, and read-only host views"
status: done
owner: user
created: 2026-11-10
kind: feature
parent: SPEC-0310
depends_on: ["SPEC-0529"]
supersedes: ["SPEC-0527"]
source_idea: IDEA-562
---

## Context

[SPEC-0527](./0527-federation-bounded-relay-mail-and-two-remote-verbs.md) replaced
[SPEC-0335](./0335-federation-over-a-networked-mailbox.md) after two campaigns diverged
(`SPEC-0335-20261101-02a3` at about 7,300 lines, `SPEC-0335-20261108-5cbb` at about 2,400
lines). Reviewers kept finding the same event-log gaps: a cursor moved past a record that
was never written, a poll parsed the whole of `events.jsonl`, and a watchdog event such as
`runner_dead` was dropped.

This spec is part 2 of the split of SPEC-0527. It depends on the signed channel in
[SPEC-0529](./0529-federation-enrolment-and-the-signed-host-channel.md): `hosts.json`,
`HubClient`, and `FederateServer`. The hub copies each enabled host's queue events into
the hub's `events.jsonl` and shows the host's cached status read-only. Mail and remote
verbs are later parts, listed under Non-goals.

What exists today and this spec reuses:

- `events.jsonl` (`src/nightwright/agentic/events.py`), an append-only `nw.event.v1` log
  with a monotonic `id`. `events.append` takes `events.lock`, writes one line, and does
  not fsync. A type missing from `EVENT_TYPES` is rejected. A type missing from
  `quiet.RULES` wakes the conductor.
- `queue.tick` (`src/nightwright/agentic/queue.py`), one bounded iteration of `nw queue run`.
- `queue.queue_payload` and `nw queue status`, an alias of `nw queue list`. Neither knows
  about hosts.
- `brief.conductor_brief` (`nw.conductor.brief.v1`) and `report.hub_payload`
  (`nw.hub.v1`). `nw hub` prints that payload only with `--json`.
- SPEC-0529's `status_cache` of `{fetched_at, projects, campaigns}`, where each campaign
  is `{campaign, project, state, spec_id, kind}`, and `HubClient`'s 2 second connect
  timeout and 2 second read timeout.

With no `hosts.json`, queue, mailbox, brief, and hub output stay as they are.

## Goals / Non-goals

**Goals**

- `GET /v1/events` seeks to a byte offset. A poll does not parse lines before that offset.
  A bad offset is `409`.
- The hub relay writes a host event, fsyncs, and only then checkpoints. A crash between
  the append and the checkpoint does not duplicate the event. A failed fsync does not
  move the cursor.
- Copied events are tagged `host` and `origin`. A non-empty campaign is rewritten to
  `<campaign>@<host>`. `message` is not copied. A type this hub cannot validate is stored
  as `host_event` and still advances the cursor.
- Network calls for every enabled host run together. Appends and cursor updates run on
  the calling thread. A call that is still running at 2.5 seconds is not applied.
- `host_unreachable` is emitted once when `last_ok` is at least 10 minutes old, and that
  timer survives a new process because `last_ok` is stored in `hosts.json`. The next
  success emits `host_reachable`.
- `nw queue status --all-hosts` shows host rows, honoring `--project`, `--state`, and
  `--json`. The conductor brief and `hub_payload()["federation_hosts"]` each carry one
  line per host, including a stale mark when the cache is older than 10 minutes.

**Non-goals**

- Part 1, federation enrolment and the signed host channel: `hosts.json` and the 0600
  secrets, `nw hosts add|list|rm|on|off|check|enrol|unenrol`, HMAC on requests and on
  enrolled responses, the unsigned 401 after unenrol, SPEC-0334 bind rules, and
  `GET /v1/status` mapped from `queue.queue_payload` items. Inert without `hosts.json`
  and `federation.json`. No event relay, mail, or verbs.
- Part 3, exactly-once mail between hub and host: uuid `msg_uid`, outbox acks for
  `delivered`, `dup`, and `rejected`, relayed conductor delivery that stores `msg_uid`
  and one host-tagged message event with campaign `<campaign>@<host>`, and
  `validate_address` unchanged for bare addresses. Mail reads seek to a byte offset. A
  lost response or a 5xx retries into `dup`; a 4xx is terminal. `POST /v1/mail` only
  writes mail. Depends on the channel spec.
- Part 4, remote park and resume: the action line is fsynced before the dedupe record,
  `dup` repairs a missing action, and `drain_actions` runs in the host runner tick. Park
  uses `plan_park`/`apply_park` and resume uses `plan_resume`/`apply_resume`, with `by
  human`. A deferred park stays started until state is `PARKED`, then answers `ok` once.
  Answers retry until the conductor inbox contains `re`. A refused action is also copied
  to the host human. The conductor treats only events with no `host` field as local; a
  remote event is mailbox park/resume or plain mail, and the 30 minute escalation is a
  delivered `conductor_ping` `data.federation` row. Depends on the mail spec.
- Mail cursor keys (`mail_seq`, `hub_inbox_offset`), the outbox, and `GET` or `POST`
  `/v1/mail`. This spec does not emit `host_mail_rejected`. It only reserves that type.
- A conductor decision-table row, a `conductor_digest` event for hosts, and any edit to
  `skills/nw-conduct/SKILL.md`. The 30 minute human escalation is part 4.
- Changes to `queue.queue_payload`, `remote_gates.py`, or `tui/campaign_detail.py`.

## Decision

- The hub pulls event pages and status with the SPEC-0529 client. The host serves
  `GET /v1/events` by seeking to a byte offset. The hub never parses the host file itself.
- Durability is write, fsync, then checkpoint. The checkpoint is
  `{events_offset, events_origin_id, hub_events_offset}` plus `health`. After a crash the
  hub rescans its own `events.jsonl` only from `hub_events_offset` and skips origin ids
  it already stored.
- `message` events are not copied. Part 3 delivers that mail. Every other returned record
  is copied, or stored as `host_event` when this hub's `events.validate` rejects it.
  Neither case stops the cursor.
- `relay_tick` submits every enabled host's `GET /v1/events` and `GET /v1/status` at
  once, waits at most 2.5 seconds, and drops whatever is still running. Appends, fsyncs,
  and `hosts.update` run afterwards on the calling thread, in registry order.
- `host_unreachable` fires once per outage when stored `last_ok` is at least 600 seconds
  behind the tick clock. A later successful call fires `host_reachable` once and clears
  the flag. A missing `last_ok` does not fire.
- Read-only views read `status_cache` only. They do not dial a host. `queue_payload` is
  unchanged. The host line and the stale bit live in the conductor brief and in
  `hub_payload`, and `nw hub --state` keeps filtering ideas only.
- The five new event types are explicit `quiet.RULES` entries, all `wake`. None is
  `hold` or `cond`, so the conductor skill's held lists stay as they are.

## Design

### Modules

| Module | Add |
|---|---|
| `src/nightwright/agentic/federation_host.py` | `read_events_page` and `GET /v1/events` on the existing `FederateServer`. |
| `src/nightwright/agentic/federation.py` | `relay_tick`, `host_views`, `host_rows`. Still no `queue` import. |
| `src/nightwright/agentic/events.py` | Five `EVENT_TYPES` entries. `append` accepts `host`, `origin`, and `ts`. |
| `src/nightwright/agentic/quiet.py` | The same five names in `RULES`, each `"wake"`. |
| `src/nightwright/agentic/queue.py` | `tick` calls `relay_tick` once, imported inside the function. |
| `src/nightwright/agentic/brief.py` | One hosts section and a `federation_hosts` key when the registry is non-empty. |
| `src/nightwright/agentic/cli_queue.py` | `--all-hosts` on `queue_list` before the `status` alias is copied. |
| `src/nightwright/report.py` | `hub_payload()["federation_hosts"]` when the registry is non-empty. |

`hosts.py`, `federation.py`, and `cli_hosts.py` still must not import `queue`. The relay
reads status from `GET /v1/status`. It does not call `queue_payload`.

Constants on `federation`:

- `POOL_WAIT_S = 2.5`
- `UNREACHABLE_S = 600`
- `STALE_S = 600`
- `EVENTS_LIMIT = 500`
- `EVENTS_MAX_BYTES = 1048576`

`nw hosts add` still writes a record whose keys are exactly the six from SPEC-0529.
The relay is what adds `cursor` and `health`.

### `GET /v1/events`

The route is authenticated and signed like every other enrolled route in SPEC-0529.
`GET /v1/events` is a success path next to `GET /v1/status`. Any other method on this
path is 405 `{"error": "method_not_allowed"}`, signed while enrolled.

`federation_host.read_events_page(path, offset, limit)` reads
`events.events_path(cfg)`. `offset` is an int and `limit` is an int in `1..500`. The
handler parses the query as follows. `offset` omitted means `0`. `limit` omitted, not a
base-10 integer, or outside `1..500` means `500`. An `offset` that is not a base-10
integer greater than or equal to `0` is `409` with body `{"error": "bad_offset"}` and
`Content-Type: application/json`. The same 409 is used for a well-formed offset that
fails the boundary check below.

The boundary check, when the offset is an int:

- Let `size` be the file length. A missing file has size `0`.
- `offset > size` is `BadOffset`.
- `offset == 0` is a boundary, including when the file is missing.
- Otherwise the only byte read before `offset` is the byte at `offset - 1`. It must be
  `\n` (0x0A). Any other byte is `BadOffset`. No earlier line is decoded.

`BadOffset` becomes the 409 above. A valid read then takes a snapshot of at most
`size - offset` bytes starting at `offset` and walks only complete lines (a line ends
with `\n`). It stops before a trailing partial line. It returns at most `limit` lines
and at most `EVENTS_MAX_BYTES` of raw line bytes, whichever comes first. If the first
complete line alone is larger than `EVENTS_MAX_BYTES`, that one line is still returned
so the cursor can pass it. A line that is not one JSON object is consumed and omitted.
`next_offset` is `offset` plus the number of raw bytes consumed, including omitted
lines, and not including a trailing partial line. The handler does not hold
`events.lock`.

A 200 body is
`{"schema": "nw.federation.events.v1", "events": [...], "next_offset": <int>}`.
`events` are the decoded objects, in file order, with no added fields. An empty file
at offset `0` returns `events: []` and `next_offset: 0`.

### Relay tick

`queue.tick` calls `federation.relay_tick(cfg)` once, after `_tick` returns and still
inside the tick's `try`. An exception from `relay_tick` is logged and does not fail the
tick. The import of `federation` is inside `tick`, so `federation` importing `events`
cannot cycle.

`relay_tick(cfg, *, now=None)` uses `now` or `datetime.now().astimezone()`. When
`hosts.load` returns `[]` it returns without creating a file, opening a socket, or
starting a pool.

For each enabled host that has no `cursor` object, before any call is submitted and in
registry order, `hosts.update` writes:

```json
{"events_offset": 0, "events_origin_id": 0, "hub_events_offset": <size>}
```

`<size>` is the current length of the hub `events.jsonl`, or `0` when that file is
absent. A missing `health` object is written in that same update as
`{"last_ok": null, "last_error": null, "unreachable_reported": false}`. An existing
`health` or `status_cache` is left alone. A disabled host is not initialized and is not
called.

The pool then submits, for every enabled host, both of these calls at once:

- `GET /v1/events?offset=<events_offset>&limit=500` (query order is `offset` then
  `limit`, no extra encoding of the integers).
- `GET /v1/status`.

The calls are independent. A failing events call does not cancel status. Each call uses
`HubClient` (2 second connect, 2 second read, signature required). `concurrent.futures.wait`
is given `POOL_WAIT_S`. A future that is not done when `wait` returns is dropped: the
tick never calls `result()` on it, and the worker itself must not append or call
`hosts.update`. `Executor.shutdown(wait=False, cancel_futures=True)` follows the wait
so a dropped call cannot hold the tick. Dropping a call does not change the cursor or
`status_cache`.

A verified 200 with schema `nw.federation.events.v1` is an events page. A verified 409
whose JSON `error` is `bad_offset` is a reset. Anything else on the events call
(transport error, bad or missing or stale signature, any other status) is an events
failure. A verified 200 status body is a status success. Any other status outcome is a
status failure. A dropped call is neither success nor failure.

Apply then runs on the calling thread, in registry order. Event pages and resets are
applied first, then status caches, then health. Hosts are not applied concurrently.

### Copying a page

Before appending, rescan the hub `events.jsonl` from `cursor.hub_events_offset` to the
end. The effective origin id is the maximum of `cursor.events_origin_id` and every
`origin.id` whose `origin.host` equals this host. The rescan parses only that tail.

For each object in the page, in order:

- No integer `id`: do not append.
- `id` less than or equal to the effective origin id, or equal to an id already appended
  earlier in this same page: do not append.
- `type` equal to `message`: do not append. Part 3 delivers it.
- Otherwise build one hub record and append it. If `events.validate` rejects the host
  type or `data` is not a dict, the hub type is `host_event` and `data` is
  `{"type": "<host type>"}` merged under the original data when that data is a dict
  (`type` is the host type). A known valid type keeps its type and its data.

The appended record's keys are exactly `schema`, `id`, `ts`, `type`, `campaign`,
`spec_id`, `project`, `by`, `data`, `host`, and `origin`. `schema` is `nw.event.v1`.
`id` is the hub's next monotonic id, not the host id. `ts` is the host `ts` when that
value is a string, otherwise `store.now_iso(now)`. `campaign` is `None` unless the host
campaign is a non-empty string, in which case it is that string plus `@` plus the
registry name. `spec_id` and `project` are copied when they are strings, else `None`.
`by` is the host `by` when that is a non-empty string, else `"host"`. `host` is the
registry name. `origin` is `{"host": "<name>", "id": <host id>}`.

`events.append` gains optional keyword-only `host`, `origin`, and `ts`. Callers that
omit them write the same record they write today, with no `host` and no `origin`, and
with `ts` still `store.now_iso()`.

The page's appends, the events-file fsync, and the `stat` of the new size all hold
`events._locked`. The fsync opens the events file read-only and `os.fsync`s it. If
nothing was appended, there is no events fsync. If the fsync raises, `hosts.update` is
not called for this page and this host's cursor is unchanged. If it succeeds,
`hosts.update` sets `events_offset` to the page's `next_offset`, `events_origin_id` to
the maximum of the effective origin id and every integer `id` in the page (skipped
`message` records included), and `hub_events_offset` to the size just measured. A
checkpoint that raises leaves the new bytes in the log. The next tick's rescan skips
origin ids already present, so the retry appends each copied event once.

A page that appends nothing still checkpoints `events_offset`, `events_origin_id`, and
`hub_events_offset` (the current size) so a skipped `message` is not read forever.

### `409` reset

A verified `bad_offset` does not apply a page. Under the same lock-and-fsync rule, the
hub appends one `host_cursor_reset` unless the rescan tail already contains a
`host_cursor_reset` whose `data.host` is this host. The record has `campaign`,
`spec_id`, and `project` null, `by` `"federation"`, top-level `host` set to the
registry name, no `origin`, and `data` `{"host": "<name>"}`. Then the checkpoint sets
`events_offset` to `0`, leaves `events_origin_id` at the effective origin id, and sets
`hub_events_offset` to the size after that append. The next tick reads from offset `0`
and the origin id skips records already copied. A later 409, after a successful page
has moved `hub_events_offset` past the previous reset, emits another reset.

### Status cache and health

A status success writes `status_cache` to
`{"fetched_at": "<store.now_iso(now)>", "projects": <body projects>, "campaigns": <body campaigns>}`
in one `hosts.update`. A status failure or a dropped status call leaves the previous
cache in place. `projects` and `campaigns` are copied from the verified body. A body
that is not a JSON object with those two keys is a status failure.

Health is updated after that, per enabled host:

- If the events result was a page or a reset, or the status result was a success, this
  tick reached the host. If `unreachable_reported` is true, append `host_reachable`
  (deduped by the rescan tail the same way as the reset), fsync, and set the flag
  false. Set `last_ok` to `store.now_iso(now)`. Set `last_error` null when both
  finished calls succeeded. If one finished call failed, `last_error` is that failure's
  type name and message, truncated to 200 characters.
- Otherwise leave `last_ok` as stored. Set `last_error` from a finished failure when
  there was one. If `last_ok` parses with `decisions.parse_ts` and
  `(now - last_ok) >= UNREACHABLE_S` and `unreachable_reported` is false, append
  `host_unreachable` unless the rescan tail already has one for this host with no later
  `host_reachable` for this host, fsync, and set the flag true. `data` is
  `{"host": "<name>", "since": "<stored last_ok>"}`. The top-level shape matches the
  reset event.
- A missing or unparseable `last_ok` does not emit `host_unreachable`. A disabled host
  is not updated and does not emit either event.

`host_reachable` data is `{"host": "<name>"}`. Both health events use `by`
`"federation"` and have no `origin`. A health append uses the same lock, fsync, and
checkpoint as a page. That checkpoint stores the flag and `last_ok` and sets
`hub_events_offset` to the size after the append. The rescan tail is the bytes after
the offset saved before this append, so an event that was fsynced and then lost its
checkpoint is still visible on the next tick.

### Event types and quiet rules

Add these required `data` keys to `events.EVENT_TYPES`:

| type | required data keys | `quiet.RULES` |
|---|---|---|
| `host_event` | `type` | `wake` |
| `host_unreachable` | `host`, `since` | `wake` |
| `host_reachable` | `host` | `wake` |
| `host_cursor_reset` | `host` | `wake` |
| `host_mail_rejected` | `host`, `msg_uid`, `to`, `reason` | `wake` |

`host_mail_rejected` is not appended by this spec. Part 3 emits it. Because none of the
five is `hold` or `cond`, the "Always held" and "Held while" lines in
`skills/nw-conduct/SKILL.md` stay unchanged.

### Read-only views

`federation.host_views(cfg, *, now=None, project=None, state=None)` reads the registry
and does not open a socket. `now` defaults to `datetime.now().astimezone()`. One object
per registry host, in registry order, including disabled hosts:

```json
{"host": "box", "enabled": true, "stale": false, "fetched_at": "<iso or null>",
 "last_contact_s": 90, "n": 3, "counts": {"PARKED": 1, "RUNNING": 2},
 "line": "host box: last contact 90s, 3 campaigns (PARKED: 1, RUNNING: 2)"}
```

`last_contact_s` is `int(total_seconds)` from `decisions.parse_ts(fetched_at)`, or
`null` when `fetched_at` is missing or unparseable. `stale` is true when
`last_contact_s` is null or `last_contact_s > STALE_S`. An age of exactly 600 is not
stale. An age of 601 is stale. Campaigns that are not objects, or whose `state` is not
a string, are ignored. `project` keeps campaigns whose `project` equals that string.
`state` keeps campaigns whose `state` equals that string. `counts` maps each remaining
state to its count. `n` is the sum. States in the line are sorted by name. The line is:

- `host <name>: last contact never, 0 campaigns` when there is no age and `n` is 0;
- `host <name>: last contact <seconds>s, 0 campaigns` when `n` is 0 and there is an age;
- `host <name>: last contact <ago>, <n> campaigns (<State>: <count>, ...)` otherwise,
  with `<ago>` either `never` or `<seconds>s`.

When `stale` is true the line ends with `, stale`.

`federation.host_rows(...)` takes the same filters and returns one thin item per
remaining campaign, host order then cache order:

```json
{"campaign": "<bare id>", "project": "<or null>", "state": "<or null>",
 "spec_id": "<or null>", "kind": "<or null>", "host": "<name>", "stale": false}
```

The campaign id is the cache value, not suffixed. `stale` is the host's stale bit.

`nw queue list` and therefore `nw queue status` gain `--all-hosts`. Without the flag,
the command calls `queue_payload` and renders it as today, with no host column and no
host key. With the flag and an empty registry, the output matches the flag-less
command. With the flag and at least one host:

- each local item gains `"host": null` and `"stale": false`;
- `host_rows` for the command's `--project` and `--state` are appended after the local
  items;
- the schema stays `nw.queue.v1`;
- the text table gains a fixed `host` column (not dropped when the width is tight).
  A local cell is empty. A remote cell is the host name, plus ` stale` when that row's
  `stale` is true.

`queue.queue_payload` is not modified. `--all-hosts` does not dial a host.

`brief.conductor_brief` calls `host_views` with its `now` and with no project or state
filter. When the list is empty the brief has no `federation_hosts` key and the text has
no hosts section. When it is non-empty the JSON gains `federation_hosts`, each object
the view object above, and `render_text` inserts a section immediately after `counts`
in `SECTION_ORDER`:

```text
hosts:
- host box: last contact 90s, 3 campaigns (PARKED: 1, RUNNING: 2)
```

Each bullet is the view `line`. Truncation still drops later sections first.

`report.hub_payload` gains the same `federation_hosts` list under the same empty rule.
It does not pass the idea `state` argument into `host_views`. `nw hub --state` still
filters ideas only. `nw hub --json` prints the key because it prints `hub_payload`.

## Alternatives considered

- **Page events by id with a scan from the start of the file.** Each poll would cost
  more as the file grew, inside a 2 second read timeout. A byte offset keeps a poll to
  one page.
- **A persisted set of every origin id.** It grows without a bound, and the abandoned
  campaigns still advanced the cursor when the write failed. A byte checkpoint plus a
  rescan of the last page dedupes a crash without that set.
- **Apply a call that finishes after the 2.5 second wait.** The tick bound would depend
  on the slowest host. Dropping the late result leaves the cursor where it was, and the
  next tick reads the same page.
- **One thread per host that also appends.** Two hosts would interleave `events.jsonl`
  and the cursor file. The pool only fetches. The tick thread appends and checkpoints.
- **Copy `message` events here.** Part 3 writes the one hub `message` event for host
  mail. Copying them here as well would double that event.
- **Hold `host_reachable` in `quiet.RULES`.** A `hold` value has to be mirrored on the
  conductor skill's "Always held" line. All five new types wake, which matches an
  unknown type today and leaves that line alone. Part 4 owns the 30 minute escalation.

## Acceptance criteria

- [x] `GET /v1/events` returns `Content-Type: application/json` and
      `nw.federation.events.v1`. Offset `0` on a missing or empty file returns
      `events: []` and `next_offset: 0`. A response seeks to `offset` and the only read
      before that offset is the one boundary byte. With 10,000 complete lines before
      the cursor, the page parser decodes only the lines from `offset` onward. The page
      stops at `limit` lines, or at 500 when `limit` is omitted or outside `1..500`, or
      at 1,048,576 raw bytes, and a single first line longer than that byte limit is
      still returned. A trailing partial line is not returned and is not consumed. A
      non-JSON line is consumed and omitted. A mid-line offset, an offset past the end,
      and a negative or non-integer offset each return signed `409`
      `{"error": "bad_offset"}` while enrolled. `POST /v1/events` is a signed 405
      `{"error": "method_not_allowed"}` while enrolled.
- [x] A relayed `runner_dead` keeps that type and its data, and gains top-level `host`,
      `origin` `{host, id}` with the host id, and `campaign` `<campaign>@<host>`. An
      unknown type, and a known type that fails `events.validate`, is stored as
      `host_event` with `data.type` equal to the host type, and the cursor still
      advances. A `message` record is absent from the hub log, and `events_origin_id`
      moves past its id. A record with no integer id is not appended. The same host id
      twice in one page is appended once. The events request line is
      `/v1/events?offset=<n>&limit=500` with `offset` before `limit`. A second
      `relay_tick` adds no duplicate origin id.
- [x] If the events-file fsync raises, the cursor is unchanged. The next successful tick
      has each copied origin id exactly once. If the checkpoint raises after a durable
      append, the next tick's rescan from `hub_events_offset` also leaves each origin id
      exactly once. The rescan does not parse bytes before `hub_events_offset`.
- [x] A verified `409` `bad_offset` appends one `host_cursor_reset` whose `data.host` is
      the registry name, sets `events_offset` to `0`, and keeps `events_origin_id`. The
      following page from offset `0` appends only host ids greater than that origin id.
      A crash after the reset append and before the checkpoint does not append a second
      reset. `HubClient` still rejects an unsigned events response: the cursor does not
      move and no host event is appended.
- [x] `POOL_WAIT_S` is `2.5`. Fetches for three enabled hosts overlap in time. Appends
      and `hosts.update` run on the calling thread after `wait`, in registry order, and
      do not overlap each other. A fetch that is still running when `wait` returns is
      not applied: that host's cursor and `status_cache` stay as they were, including
      when the fetch finishes later. The timeout passed to `wait` is `POOL_WAIT_S`, and
      a blocked call does not extend it. A disabled host is not called and does not
      gain a `cursor`. After `on`, the next tick copies a new host event. `queue.tick`
      calls `relay_tick` once, and a raising `relay_tick` does not fail the tick. A
      status body that is not an object with `projects` and `campaigns`, or a non-200
      status response, leaves `status_cache` unchanged.
- [x] With `last_ok` 599 seconds before `now`, no `host_unreachable` is written. At 600
      seconds, exactly one is written, `data.since` equals the stored `last_ok`, and
      `unreachable_reported` becomes true. A second `relay_tick` in a new call does not
      write another while the flag is true, and initializing a missing `cursor` does not
      clear `last_ok`. The next successful call writes one `host_reachable` and sets the
      flag false. A missing `last_ok` does not emit `host_unreachable`. A disabled host
      with an old `last_ok` does not emit it either.
- [x] `EVENT_TYPES` and `quiet.RULES` both contain `host_event`, `host_unreachable`,
      `host_reachable`, `host_cursor_reset`, and `host_mail_rejected`, with the required
      data keys and the `wake` values in Design. The conductor skill's held sets are
      unchanged. A relay tick does not append `host_mail_rejected`. Existing `append`
      callers that pass no `host` write no `host` field.
- [x] Without `--all-hosts`, `nw queue status` JSON matches `queue_payload` and has no
      host column. With `--all-hosts` and no hosts, it matches that same payload.
      `queue_payload` itself gains no host key. With a seeded cache of 2 `RUNNING` and
      1 `PARKED`, `--all-hosts` appends those three rows with the bare campaign id, the
      host name, and `stale`, local items gain `"host": null`, and `--project` and
      `--state` filter the host rows. A cache `fetched_at` 600 seconds old is not
      stale. One 601 seconds old is stale, and the text cell contains `stale`. The host
      column is fixed.
- [x] The conductor brief and `hub_payload()["federation_hosts"]` each omit the key when
      the registry is empty. With the seeded cache they each include one object per host
      whose `line`, `n`, and `counts` match Design, and the brief text contains that
      line under a `hosts:` section. A stale host's line ends with `, stale`.
      `nw hub --state` does not change `federation_hosts`. Neither view dials a host.
- [x] With no `hosts.json`, `relay_tick` and `queue.tick` create no `hosts.json`, the
      brief has no `federation_hosts` key, and `hub_payload` has no `federation_hosts`
      key. An AST walk of `hosts.py`, `federation.py`, and `cli_hosts.py` still fails on
      any `queue` import, including `from . import queue as Q`. The relay does not add
      `mail_seq` or `hub_inbox_offset`.

## Test plan

- **Automated tests:** `tests/test_federation_relay.py`, module-marked
  `pytest.mark.fast`. Two configs (hub data dir, host data dir). The host
  `FederateServer` listens on `127.0.0.1:0` in a thread and is enrolled with the
  SPEC-0529 files. Clocks are the `now` argument of `relay_tick`, `host_views`, and
  `conductor_brief`. Tests do not sleep for the 10 minute timer. Host lines are written
  straight into the host `events.jsonl` unless a test says otherwise.
  - `test_events_page_seeks_and_caps` — 10,000 lines before `offset`, then 3 short
    lines. The parser's decoded-line count is 3, not 10,003. `next_offset` sits after
    those 3. A first line larger than `EVENTS_MAX_BYTES` is returned alone. A trailing
    partial line is absent and `next_offset` does not pass it. A non-JSON line is
    skipped and consumed. An omitted limit and a limit of `0` both cap at 500.
    `POST /v1/events` is a signed 405.
  - `test_events_bad_offset_is_409` — a mid-line offset, an offset past the end, and
    `offset=-1` each get a signed 409 `{"error": "bad_offset"}` and
    `Content-Type: application/json`. Offset `0` on a missing file is an empty page.
  - `test_relay_tags_unknown_and_skips_message` — one page holds `runner_dead` with a
    campaign, a type `widget` that is not in `EVENT_TYPES`, a `runner_dead` missing
    `active`, a `message`, and an object with no `id`. The hub log has three copied
    records: the valid `runner_dead` with `campaign` ending in `@<host>`, `host`, and
    `origin.id`; the two `host_event` records with `data.type` `widget` and
    `runner_dead`. No `message`. A repeated host id in that page is stored once. The
    request path is `/v1/events?offset=<n>&limit=500`. A second `relay_tick` adds no
    origin id.
  - `test_fsync_failure_retries_once` — monkeypatch the relay's events fsync to raise
    once. The cursor is unchanged. The next tick stores each origin id once.
  - `test_crash_before_checkpoint_rescans_tail` — monkeypatch `hosts.update` to raise
    once after the append. Pad the hub log before `hub_events_offset` with 1,000
    unrelated lines and assert the rescan decodes none of them. The retry stores each
    origin id once.
  - `test_409_resets_once_and_skips_known_ids` — a mid-line cursor gets a 409. The hub
    log gains one `host_cursor_reset`, `events_offset` is `0`, and `events_origin_id`
    is unchanged. The next page appends only newer ids. A checkpoint failure after the
    reset append retries without a second reset. An unsigned events response leaves the
    cursor unchanged and appends nothing.
  - `test_pool_overlaps_and_drops_late_results` — three enabled hosts. Fetch stubs
    record start times and sleep. With `POOL_WAIT_S` left at `2.5` for the constant
    assertion and patched to `0.05` for the race, the starts overlap, apply order is
    registry order and runs on the main thread after every finished fetch returns, and
    a stub that sleeps past the wait does not change that host's cursor or
    `status_cache` even after the stub returns. `relay_tick` returns while that stub
    is still blocked. A spy on `concurrent.futures.wait` sees `timeout` equal to
    `POOL_WAIT_S`. A disabled host's stub is not called and the record gains no
    `cursor`. `off` then a new host event is not copied; `on` then the next tick copies
    it. A status body of `[]`, and a status 500, each leave `status_cache` as it was.
  - `test_queue_tick_calls_relay_once` — monkeypatch `relay_tick` and run `queue.tick`.
    It is called once. A `relay_tick` that raises `RuntimeError` still lets `tick`
    return a report.
  - `test_unreachable_once_across_calls` — `last_ok` at 599 seconds writes nothing; at
    600 seconds writes one `host_unreachable` with `data.since` equal to `last_ok` and
    sets the flag. Another `relay_tick` writes no second event. A missing cursor with
    that same `health` still shows the old `last_ok` after the tick. A later 200 writes
    one `host_reachable` and clears the flag. `last_ok` null writes neither event. A
    disabled host at 600 seconds writes neither event.
  - `test_event_types_and_quiet_rules` — the five types have the required keys and
    `RULES[type] == "wake"`. The hold and cond sets still match
    `skills/nw-conduct/SKILL.md` via the existing
    `test_conduct_skill_quiet_rules_match_code` assertions (import `quiet.RULES` and
    compare). No relay test's hub log contains `host_mail_rejected`. An `events.append`
    of `note` without `host` has no `host` key.
  - `test_status_all_hosts_filters` — seed `status_cache` with 2 `RUNNING` and 1
    `PARKED` on project `P`, and one `RUNNING` on project `Q`. `--all-hosts --json`
    includes the four rows, local items have `"host": null`, and the bare campaign ids
    are unsuffixed. `--project P` drops the `Q` row. `--state PARKED` keeps the one
    parked row. `fetched_at` exactly 600 seconds ago is `"stale": false`. 601 seconds
    ago is true, and the text render contains the host name and `stale`. Without the
    flag, and with the flag and an empty registry, the JSON equals `queue_payload`.
    The `host` column is built with `fixed=True`.
  - `test_brief_and_hub_lines` — the same seed. `conductor_brief(...)["federation_hosts"]`
    and `hub_payload()["federation_hosts"]` equal the view objects, the line is
    `host box: last contact 90s, 3 campaigns (PARKED: 1, RUNNING: 2)` for a 90 second
    age with those counts, and `render_text` contains that line under `hosts:`. The
    stale line ends with `, stale`. An empty registry omits the key on both payloads.
    `hub_payload(..., state="INCUBATE")` returns the same `federation_hosts` as a call
    without `state`. A socket-binding spy records no connection during either view.
  - `test_inert_and_no_queue_import` — no `hosts.json`: `relay_tick` and `queue.tick`
    create none, and the brief and hub payloads have no `federation_hosts` key. The
    AST walk from SPEC-0529 still passes on `hosts.py`, `federation.py`, and
    `cli_hosts.py`. After a relay, the host record has `cursor` and `health` and does
    not have `mail_seq` or `hub_inbox_offset`.
- **Manual verification:**
  1. Create two empty directories and export `NW_DATA_DIR` and `NW_CONFIG_DIR` for the
     hub, then the host.
  2. On the hub, run `nw hosts add box --url http://127.0.0.1:8792` and pipe stdout
     into the host's `nw hosts enrol`.
  3. On the host, append one `runner_dead` line to `agentic/events.jsonl` and run
     `nw serve --federate`.
  4. On the hub, run `nw queue tick` once. `agentic/events.jsonl` on the hub contains
     that event with `host` `box` and a campaign ending in `@box`.
     `nw queue status --all-hosts` shows a `host` column. `nw queue conductor-brief`
     contains a `hosts:` line for `box`. `nw hub --json` contains `federation_hosts`.
  5. Stop the federate server. Set that host's `health.last_ok` to 11 minutes ago and
     `unreachable_reported` to false, then run `nw queue tick` once. The hub log gains
     one `host_unreachable`. A second tick adds no second one.
  6. Record the commands and the output under an `As shipped` heading in this spec.
  Enrolling a second physical machine is rollout, not part of this spec's definition of
  done.
- **Regression guard:** at the close gate, `python3 tools/affected_tests.py` plus
  `python3 tools/spec_lint.py`. `tests/test_agentic_conductor_brief.py`,
  `tests/test_hub.py`, and `tests/test_federation_channel.py` cover the brief, the hub,
  and the channel when no host has been added.

### Test record (2026-11-12)

- `uv run pytest -q tests/test_federation_relay.py`: 17 passed (all Test plan cases, plus
  view-boundary and dropped-status cases).
- Targeted related files (brief, hub, events, quiet, queue CLI/visibility, channel, docs,
  tier/wait guards, TUI queue parity/hub, skills): 576 passed. `test_agentic_queue`,
  `test_agentic_e2e`, `test_agentic_ping`, docs capability/guide/capture and guard-registry
  files: 274 passed. `python3 tools/test_tiers.py --check` exits 0.
- Review round (2026-11-12): `tests/test_federation_relay.py` now 29 tests, all passing: a seeded
  local campaign pins `host: null`/`stale: false` and the narrow-width host column; health-append
  crash windows (unreachable and reachable), a missing cursor with existing health, an unparseable
  `last_ok`, a hostile status body, a forged unsigned 409, partial failures, a lost page checkpoint
  followed by a health event, a relayed `host_*` event with an origin, a disabled-host view,
  brief truncation order, and an oversized first line. Overlap is proved with a barrier and the
  late-result check joins the pool thread instead of sleeping.
- The full suite and the whole affected set (211 files) were left to the close gate.
- Manual run (hub and host in separate `NW_DATA_DIR`/`NW_CONFIG_DIR`, host on
  `127.0.0.1:18792` because 8792 was taken): see As shipped.

### As shipped (2026-11-12)

Built to Design. Deviations and choices:

- `HubClient.request` gained `allow=(200,)`; the relay passes `(200, 409)` so a `409` is
  signature-verified before it counts as a `bad_offset` reset. Other statuses still raise.
- The fetch seams are `federation._fetch_events(rec, cfg, offset)` and
  `federation._fetch_status(rec, cfg)` (pool side, no file access); the events fsync is
  `federation._fsync_events(cfg)`.
- The rescan runs inside `events._locked` together with the appends, the fsync and the size
  `stat`. The fsync also runs when the tail holds records this host's relay wrote earlier
  (an append whose fsync or checkpoint failed), so a checkpoint never follows unsynced bytes.
- If a host's page or reset failed to checkpoint in a tick, that tick's health update does
  not advance `hub_events_offset`, so the next rescan still covers the page.
- `queue.tick` runs the relay through `queue._relay_hosts`, which imports `federation`
  inside the function, logs nothing itself and journals `federation_relay_failed` on an
  exception (a journal entry in addition to not failing the tick).
- The status cache keeps only the five documented campaign keys as bounded printable
  strings (the `hosts check` sanitizer, moved to `federation.clean_str` /
  `clean_campaigns`).
- Review hardening (loop 2): a page or reset checkpoint keeps `hub_events_offset` where it is
  while the rescan tail holds this hub's own `host_unreachable`/`host_reachable` for the host
  (its flag checkpoint may be lost), and a health checkpoint does not move the offset while the
  tail holds copied records past `events_origin_id` (a page whose checkpoint failed in an earlier
  tick). A reached host with the flag false still appends `host_reachable` when the tail shows an
  open outage whose flag checkpoint was lost. Health dedupe (`host_unreachable`,
  `host_reachable`, `host_cursor_reset`) counts only records with no `origin`, so a `host_*`
  event relayed from a peer hub never suppresses this hub's own.
- `read_events_page` reads at most one capped line at a time. A first line over half of
  `MAX_RESPONSE_BYTES` (2 MiB; the hub could not read it back, which would stall the cursor)
  is consumed unread and omitted; a first line up to that size is still returned alone.
- The status cache's sanitiser is pinned by `test_hostile_status_body_is_sanitised_in_the_cache`:
  `projects` keeps only bounded printable strings, each campaign only its five keys, at most
  `MAX_CAMPAIGNS` entries, non-dict campaign entries dropped.
- `nw queue status --all-hosts` text: remote rows are listed in the Active table with a
  fixed `host` column (empty for local rows); the other tables are local only.

Manual verification (temp dirs, host on port 18792):

```
hub:  nw hosts add box --url http://127.0.0.1:18792 > blob.json
host: nw hosts enrol < blob.json
host: (one runner_dead line appended to agentic/events.jsonl)  nw serve --federate --bind 127.0.0.1:18792
hub:  nw queue tick
      -> hub events.jsonl: {"id": 1, "type": "runner_dead", "campaign": "SPEC-1-20261112-aaaa@box",
         ..., "host": "box", "origin": {"host": "box", "id": 1}}
hub:  nw queue status --all-hosts      -> Active table with a host column
hub:  nw queue conductor-brief         -> "hosts:\n- host box: last contact 4s, 0 campaigns"
hub:  nw hub --json                    -> federation_hosts: [{"host": "box", ..., "stale": false}]
(server stopped; health.last_ok set 11 minutes back, unreachable_reported false)
hub:  nw queue tick; nw queue tick
      -> exactly one host_unreachable (data.since = the stored last_ok); the second tick adds none
```

## Clock Log

Use `nw idea clock-in IDEA-562` at the start of an implementation session and
`nw idea clock-out IDEA-562` when pausing or finishing.

## Rollout / migration

1. With no `hosts.json`, `queue.tick` does not open a connection and the brief and hub
   payloads are unchanged.
2. After SPEC-0529 enrolment, the next hub `nw queue tick` initializes the cursor at the
   current hub log size and reads the host log from offset `0`, one page per tick.
3. There is no backfill of mail and no remote verb in this spec. A host that was never
   reached has `last_ok` null and does not emit `host_unreachable` until a success is
   recorded and then ages out.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed and recorded.
- [x] No regressions (affected tests at the close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:

- `message` events are skipped on this relay. Part 3 writes the one hub message event.
- A late call is dropped rather than applied after the wait. The cursor stays put.
- `host_unreachable` compares with `>= 600` seconds ("reaches 10 minutes"). The view
  marks a cache stale only when the age is greater than 600 seconds ("older than 10
  minutes"). An age of exactly 600 seconds is not stale.
- A host with no stored `last_ok` does not emit `host_unreachable`.
- All five new quiet rules are `wake`, so the conductor skill text is not edited here.
- `nw hub --state` remains the idea filter. It does not filter `federation_hosts`.
  `nw queue status --state` does filter host rows.
