---
id: SPEC-0335
title: "Federation over a networked mailbox"
status: accepted
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-562
parent: SPEC-0310
tags: [agentic, remote, federation, mailbox, conductor]
depends_on: [SPEC-0334]
---

## Context

SPEC-0332 to SPEC-0338 move individual **jobs** to other machines, while one coordinator
keeps every queue, ledger, worktree and merge. That is not enough when a project's
repository, credentials or toolchain live only on another machine. Examples are a work
laptop's project, or a repo that must not leave its host. Today such a project cannot be in
the queue at all.

The conductor already coordinates through a file mailbox (SPEC-0183,
`src/nightwright/agentic/mailbox.py`):
- addresses are `conductor`, `human` and `steward:<Project>`;
- each inbox is an append-only `nw.message.v1` JSONL with a per-inbox `seq`;
- conductor-bound mail is mirrored into `events.jsonl`.

Federation makes that mailbox networked. A project machine then runs its own queue for its
own projects, plus a steward, while **one** conductor oversees all machines.

## Goals / Non-goals

**Goals**
- A **project host** runs the queue (runner and steward) for the projects registered on it
  and keeps its own ledger and merges.
- The single conductor, on the hub, sees every host's queue events and mail, and can send
  them `request` and `answer` messages.
- The mailbox gains a network transport. Addresses get an optional host part
  (`steward:<Project>@<host>`). Delivery is at-least-once and messages are deduplicated by
  `id`.
- The hub shows each host's campaigns read-only: `nw queue status --all-hosts`, the digest
  and the dashboard.

**Non-goals**
- Moving a campaign from one host to another.
- A shared ledger, or merging across hosts. Each host's ledger stays authoritative for its
  own projects.
- More than one conductor.
- Replacing the job API. Federation reuses SPEC-0334's server, auth and bind rules.

## Decision

A project host is a nightwright install that:
- runs `nw queue run` for its projects;
- runs `nw serve --federate`, which uses SPEC-0334's server and adds mailbox and event
  endpoints;
- is enrolled on the hub with `nw hosts add NAME --url URL`. This gives it a per-host token
  and HMAC secret, exactly like a pull worker.

The hub runs a **relay**, `nw hosts relay`, which is part of the queue runner tick. The
relay:
1. pulls each host's new queue events from a cursor and appends them to the hub's
   `events.jsonl`, tagged `host`. The conductor's single Monitor therefore wakes on every
   host's events.
2. pulls conductor-bound mail and delivers it to the hub's `conductor` inbox.
3. pushes mail addressed `...@<host>` to that host.

Every relayed record gets a new local `seq`/`id` from the receiver and carries
`origin: {host, id}`. Messages created by `send` to a remote address get a globally unique
`msg_uid = <origin host>:<inbox>:<seq>`. The receiver drops a record whose
`(origin.host, origin.id)` (events) or `msg_uid` (mail) it already holds. The dedupe index
is a persisted set in `mailbox/seen-<host>.jsonl`, not a scan of the last N lines.

## Design

### Enrolment

- `nw hosts add NAME --url URL` on the hub creates the token and secret and prints a
  one-time blob `{hub_name, host_name, token, secret}`.
- On the host, `nw serve --federate --enrol BLOB` stores it in
  `<config_dir>/secrets/federation.json` (0600).
- The host verifies the hub's bearer token and request HMAC. Every host response carries
  `X-NW-Sig = HMAC(secret, method|path|sha256(body)|ts)`; the hub verifies it and drops
  unsigned or stale responses.
- `nw hosts token NAME --rotate` and `nw hosts rm` revoke immediately.

### Addresses and the hosts registry

- `<agentic_dir>/hosts.json` (schema `nw.hosts.v1`) holds `[{name, url, token_sha256,
  secret_ref, enabled, cursors: {events, mail}}]`. It is managed by `nw hosts
  add|list|rm|on|off|check`. `cursors.events` stores the last host event `id` applied, and
  is advanced only after the hub append is fsynced.
- `mailbox.validate_address` accepts `steward:<Project>@<host>` and `human@<host>`.
  - Resolution order for a bare `steward:<Project>`: the hub registry, then each host's
    project list cached in `hosts.json`.
  - A project in both the hub registry and a host's list, or on two hosts, is a `hosts
    check` error, and `send` refuses it.
  - With no cached list (host never reached), a bare `steward:<P>` for a non-local project
    fails with `unknown steward project`.
- `send` to a remote address appends the message to the local **outbox**
  `mailbox/outbox-<host>.jsonl`. The relay delivers it and appends an `ack` line
  `{msg_uid, remote_seq}` to the outbox. A message is pending until its ack line exists.
- Relayed events and mail carry `campaign` as `<campaign>@<host>`. Hub code paths that read
  campaign state skip any id containing `@`, and the views read only the cached
  `/v1/status`.

### Host endpoints (`nw serve --federate`)

| Method and path | Purpose |
|---|---|
| `GET /v1/events?after=<id>&limit=500` | Host `events.jsonl` records with `id > after`, in id order. The hub dedupes on `(host, id)`; `event_uid` is not required. |
| `GET /v1/mail?to=conductor&after=<seq>` | Conductor-bound mail with inbox `seq > after`. |
| `POST /v1/mail` `{messages: [...]}` | Deliver mail to local inboxes. Returns `{results: [{msg_uid, seq, dup: bool}]}`; deduplicated by `msg_uid`. |
| `GET /v1/status` | `nw queue status --json` and the project list. |

- Requests are signed: the HMAC of the method, the path, the body sha256 and a timestamp,
  with 120 s of clock skew allowed.
- The host's bind safety is the same as SPEC-0334's.

### Relay (hub)

- The relay runs once per tick in a worker thread pool, one task per enabled host, each
  with a 2 s connect+read timeout. The tick waits at most 2.5 s total for the pool and does
  not wait on stragglers.
- The event relay skips host events of type `message`. Conductor-bound mail reaches the hub
  only through `/v1/mail`, and its delivery into the hub's `conductor` inbox mirrors exactly
  one `message` event tagged `host`, so the conductor wakes once.
- Events are appended with `host: <name>` and an `event_uid` prefixed by the host name.
  The conductor's follow cursor is unchanged.
- A host that is unreachable for 10 min yields one `host_unreachable {host, since}` event.
  The conductor decision table says: escalate to the human after 30 min.
- Mail is retried until it is acknowledged. Delivery is at-least-once with
  receiver-side dedupe, so the effect is exactly-once.

### Conductor and steward

- The conductor skill (`/nw-conduct`) treats `host`-tagged events like local ones, but sends
  its actions as mail to `steward:<Project>@<host>`. The hub cannot run `nw queue` verbs on
  another host's campaigns.
- The steward on the host runs the verb locally (`nw queue park|resume|...`) and answers.
- This keeps the rule "the conductor never implements" and adds "the hub never writes
  another host's state".
- A conductor request carries `action: {verb, campaign, args}`, a new optional field of
  `nw.message.v1`. On delivery to `steward:<P>` on the host, `federation.route_action(msg)`
  runs `nw queue <verb> <campaign>` itself when `verb` is in `{park, resume, stop, gates,
  rebase}`, the campaign belongs to project P, and `args` contains no other flags. Otherwise
  it forwards the message to `human` on the host. The steward skill only reports the
  outcome; it never executes verbs from free text.
- The conductor skill's decision table escalates `host_unreachable` older than 30 min to the
  human.

### Views

- `nw queue status --all-hosts` merges the cached `/v1/status` of each host into the table,
  with a `host` column.
- The digest and `nw hub` show a per-host line.

## Alternatives considered

- **A shared filesystem mailbox (NFS or Syncthing).** File locks are unreliable across
  hosts, and sync conflicts would corrupt the append-only inboxes.
- **One coordinator with remote worktrees.** This puts every merge and secret on the hub,
  which is exactly what project hosts exist to avoid.
- **Let the conductor run verbs remotely through the API.** That would give the hub write
  power over host state. Routing through the steward keeps each host in charge of itself.
- **Hosts push to the hub's API.** This would work for hosts behind NAT, but it makes the
  hub a server for host writes. Rejected: hosts are reached over Tailscale, and pull keeps
  the hub's attack surface unchanged.
- **A message broker (MQTT, NATS).** That is a new service and new dependencies. HTTP pull
  on the existing server is enough for event rates of tens per minute.

## Acceptance criteria

- [ ] A campaign on a project host appears in the hub's `nw queue status --all-hosts`. Its
      queue events reach the hub's `events.jsonl` tagged `host`, with no duplicates after
      relay restarts.
- [ ] Mail from the hub conductor to `steward:<Project>@<host>` is delivered exactly once,
      and so is the steward's answer to the hub conductor. This holds across a dropped
      connection.
- [ ] An allowlisted conductor request (`action.verb: park`) is executed by
      `federation.route_action` on the host. A non-allowlisted verb, a campaign of another
      project, or extra flags is forwarded to that host's human, not executed.
- [ ] Conductor-bound mail produces exactly one `message` event on the hub.
- [ ] `nw hosts add|list|rm|on|off|check` manage `hosts.json`; `check` reports a project
      registered on two hosts; `off` stops relaying to that host.
- [ ] The digest and `nw hub` show one line per host with last-contact time and campaign
      counts.
- [ ] The conductor skill's decision table escalates `host_unreachable` older than 30 min to
      the human.
- [ ] The hub has no code path that writes a remote host's campaign state.
- [ ] With 3 hosts, two of them unreachable, the relay adds at most 2.5 s to a tick; an
      unreachable host yields `host_unreachable` after 10 min.
- [ ] Bad signatures (request or `X-NW-Sig` response), revoked tokens and a stale timestamp
      are refused.

## Test plan

- **Automated tests** (`tests/test_federation.py`, offline):
  - The setup: two nightwright data dirs in one test, one for the hub and one for the host,
    with the host's `nw serve --federate` on `127.0.0.1:0` in a thread. A fake campaign on
    the host emits events.
  - Event relay: the hub receives them tagged `host`. A relay restart and a replayed cursor
    produce no duplicates.
  - Mail round trip in both directions, including a dropped request mid-delivery (the
    retried message is deduplicated).
  - Address resolution: bare `steward:X` resolves in order (hub, then host cache); a
    project on two hosts, or on the hub and a host, is an error; an uncached host gives
    `unknown steward project`.
  - Dedupe: `msg_uid` and `(origin.host, origin.id)` survive a restart via
    `seen-<host>.jsonl`; conductor mail yields exactly one hub `message` event; outbox `ack`
    lines.
  - `nw hosts` CLI (including `check` and `off`), digest and `nw hub` per-host lines.
  - Enrolment and `X-NW-Sig`: an unsigned or tampered response is dropped.
  - Steward allowlist: the routing function maps a `park` request to the local verb and an
    unknown verb to an escalation.
  - `host_unreachable` with a stopped server, and the tick-time bound with 3 hosts, two of
    them blackholed (the relay call is measured, at most 2.5 s).
  - A skill-doc check that the conductor decision table has the `host_unreachable` row.
  - Security cases reuse SPEC-0334's helpers.
  - A static check that the hub-side relay module imports no `queue` mutation functions.
- **Manual verification:**
  1. Enrol the human's second machine as a project host for one project.
  2. Run a campaign there.
  3. Watch it in the hub's `nw queue status --all-hosts`.
  4. Have the conductor park it through the steward.
  5. Unplug the host and see `host_unreachable`.
- **Regression guard:** the full `uv run pytest`. The single-host mailbox, conductor and
  queue tests run unchanged with no `hosts.json`.

## Clock Log

Use `nw idea clock-in IDEA-562` / `nw idea clock-out IDEA-562`.

## Rollout / migration

1. Inert without `hosts.json`.
2. Enrol one host, move one project's registration from the hub to that host (the human
   does this), and run both for a week before enrolling more.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:
- The hub pulls from the hosts through the relay; hosts never push to the hub.
- Remote actions go through `federation.route_action` with an allowlist, carried in a
  structured `action` field.
- Relayed records get new local ids plus `origin`; dedupe uses a persisted seen set.
- Each project has exactly one owning host.
- The relay uses a 2 s per-host timeout, `host_unreachable` comes after 10 min, and
  escalation after 30 min.
