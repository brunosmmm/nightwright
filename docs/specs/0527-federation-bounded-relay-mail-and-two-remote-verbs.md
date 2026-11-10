---
id: SPEC-0527
title: "Federation, bounded: relay, mail and two remote verbs"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
source_idea: IDEA-562
parent: SPEC-0310
tags: [agentic, remote, federation, mailbox, conductor]
depends_on: [SPEC-0334]
supersedes: [SPEC-0335]
---

## Context

SPEC-0335 asked for federation: a **project host** runs the queue for its own projects, and
the single conductor on the **hub** sees every host's events and mail. Two campaigns failed
on it:
- `SPEC-0335-20261101-02a3` grew to about 7,300 lines;
- `SPEC-0335-20261108-5cbb` grew to about 2,400 lines.

The divergence triager of the second campaign ruled `rework`. The reviewers kept asking
for things SPEC-0335 does not say, or that it forbids:
- send a `GitError` from a remote verb to the human;
- stop retrying mail after a permanent error;
- flush the `POST /v1/mail` response before `route_action` runs;
- page `events.jsonl` without reading the whole file.

They also found real gaps:
- cursors moved past records that were never written;
- a `dup` reply was never acked;
- watchdog events were dropped;
- two concurrent sends could get the same `msg_uid`;
- `token --rotate` and `rm` deleted the hub's credential before the host knew;
- some tests passed without checking what they claimed to check.

This spec replaces SPEC-0335. It keeps the same goal with less machinery, and it decides
every one of the items above, either in scope with a rule or out of scope in the Non-goals.

The previous design came from the existing coordination pieces:
- the file mailbox (SPEC-0183, `src/nightwright/agentic/mailbox.py`);
- the queue event log (`src/nightwright/agentic/events.py`, append-only `events.jsonl`
  with monotonic `id`);
- the SPEC-0334 job server (`PullServer`, its bearer auth and its bind safety).

## Goals / Non-goals

**Goals**
- A project host runs `nw queue run` and `nw serve --federate` for its own projects. It
  keeps its own ledger and does its own merges.
- The hub's relay copies each host's queue events into the hub's `events.jsonl`, tagged
  `host`, so the conductor's one Monitor wakes on them. Every event type is copied except
  `message`, including watchdog events such as `runner_dead`.
- Mail moves both ways:
  - hub to host: `steward:<P>@<host>` and `human@<host>`;
  - host to hub: anything a host sends to `conductor`.
  Each message reaches its destination file once, even across a dropped connection or a
  crash.
- The conductor can **park** and **resume** a host's campaign. It sends a structured
  request, and the host's own runner carries it out.
- The hub shows hosts read-only:
  - `nw queue status --all-hosts`;
  - one line per host in the digest and in `nw hub`.

**Non-goals (decided out of scope; reviewers must not demand them)**
- **Remote verbs other than `park` and `resume`.** `stop`, `gates`, `rebase` and the rest
  are refused. They need a host human or a later spec. As a result, the `GitError` paths of
  `rebase` and `gates` do not exist here.
- **Running a verb inside the HTTP request.** `POST /v1/mail` only writes mail. Verbs run
  later in the host's runner tick (see "Remote verbs"). The order of the HTTP response
  relative to the verb is therefore not a concern.
- **Sending a verb's failure to the host's human.** A failed verb is answered to the
  conductor with the error text. The human copy happens only for a refused request.
- **Remote revocation or token rotation.** There is no `/v1/revoke` and no
  `nw hosts token --rotate`. To rotate, run `nw hosts rm`, then `nw hosts add`, then
  re-enrol. The host revokes for itself with `nw hosts unenrol`.
- **Resolving a bare `steward:<P>` across hosts.** A bare address is always local. Remote
  mail needs an explicit `@<host>`.
- Moving campaigns between hosts, a shared ledger, cross-host merges, more than one
  conductor, and hosts pushing to the hub.
- Anything outside federation. The abandoned branch also touched `remote_gates.py`,
  `cli_worker.py` and `tui/campaign_detail.py`. Changes there are out of scope unless a gate
  failure is shown to be caused by this spec's code.

## Decision

- **The hub pulls and the host serves.** The relay runs once per hub runner tick. It uses
  a thread pool with one task per enabled host. Each host call has a 2 s connect and read
  timeout, and the tick waits at most 2.5 s for the whole pool.
- **Write first, then checkpoint, with a bounded rescan.** This is the one durability rule.
  Every receiving path follows it:
  1. append the record, carrying its dedupe key, to the destination file;
  2. fsync that file;
  3. only then advance the cursor or reply success.
  If the write or the fsync fails, the cursor does not move and the request fails. After a
  crash, the receiver rescans its destination file only from the byte offset saved at the
  last checkpoint, so recovery reads at most one page.
- **Event cursor = a byte offset into the host's `events.jsonl`, plus the last origin
  `id`.** The host seeks straight to the offset, so a poll never reads the whole file.
- **`msg_uid` is a random UUID (`uuid4().hex`)** chosen by the sender. No sequence file is
  shared between processes, so concurrent sends cannot collide.
- **Every per-message result ends the retry.** `delivered`, `dup` and `rejected` all ack the
  outbox line. Only a transport failure (connection error, timeout, or a non-2xx reply to
  the whole request) is retried.
- **Remote verbs run in the host's runner tick, not in the request.** The verbs are
  `park` and `resume`. Each runs at most once, and every outcome is answered to the
  conductor.

## Design

### Modules

The hub and host code are split so the hub cannot touch host state:
- `hosts.py`: the hub registry.
- `federation.py`: hub-side code (client, relay, outbox, send).
- `federation_host.py`: host-side code (server endpoints, inbound mail, action drain).
- `cli_hosts.py`: the `nw hosts` commands.

The hub modules (`hosts.py`, `federation.py`, `cli_hosts.py`) must not import or call queue
mutation code. A test enforces this (see the Test plan).

**Reuse from the abandoned branch** `nw/SPEC-0335-20261108-5cbb`:
- copy as-is: `hosts.py` (`nw.hosts.v1`, `NAME_RE`, locked `update`), the
  `sign_*`/`verify_*` HMAC helpers, `HubClient`, `FederateServer` built on `PJ.PullServer`,
  the `relay_tick` pool constants, and `nw hosts add|list|on|off`;
- adapt: the `--all-hosts` status merge and the per-host brief lines;
- drop: everything else (`route_action`, `seen-*.jsonl`, `outbox-*.seq`, `/v1/revoke`,
  `token --rotate`, `HOST_LOCAL_EVENTS`).

Do not start from branch `nw/SPEC-0335-20261101-02a3`.

### Enrolment and auth

- On the hub, `nw hosts add NAME --url URL` creates a bearer token and an HMAC secret.
  - Both are stored under `<config_dir>/secrets/hosts/<name>.{token,key}` with mode 0600.
  - `hosts.json` keeps only `token_sha256` and `secret_ref`.
  - The command prints a one-time blob `{hub_name, host_name, token, secret}`.
- On the host, `nw hosts enrol` reads the blob from **stdin**, never from argv, so it never
  shows up in `/proc/*/cmdline`. It writes `<config_dir>/secrets/federation.json` (0600).
  - `nw serve --federate` refuses to start without that file.
  - `nw hosts unenrol` deletes the file. This revokes the hub at once.
- **Requests.** Every request carries the bearer token and
  `X-NW-Req-Sig = HMAC(secret, method|path_and_query|sha256(body)|ts)`.
- **Responses.** Every response carries `X-NW-Sig` in the same form. `HubClient` rejects:
  - a missing or wrong signature;
  - a `ts` more than 120 s from the local clock, even when the MAC is valid.
  The host applies the same 120 s skew rule to requests.
- **Removal.** `nw hosts rm NAME` deletes only the hub's entry and secrets. It prints a
  reminder to run `nw hosts unenrol` on the host, and it never contacts the host.
- The host's bind safety is the same as SPEC-0334's.

### Registry (`<agentic_dir>/hosts.json`, `nw.hosts.v1`)

Each host has one record:

```
{name, url, token_sha256, secret_ref, enabled,
 cursor:  {events_offset, events_origin_id, hub_events_offset,
           mail_seq, hub_inbox_offset},
 health:  {last_ok, last_error, unreachable_reported},
 status_cache: {fetched_at, projects: [...], campaigns: [...]}}
```

- Writes are atomic: write a temp file, fsync, then rename, all under the registry lock.
- `health.last_ok` is persisted, so restarting the runner does not reset the timer behind
  `host_unreachable`.
- `nw hosts add|list|rm|on|off|check`:
  - `off` makes the relay skip the host, and `send` to that host refuses;
  - `on` restores both;
  - `check` calls `GET /v1/status` on each enabled host, refreshes `status_cache`, and
    prints reachability. It exits 1 when a project is registered on both the hub and a
    host, or on two hosts.

### Host endpoints (`nw serve --federate`, `federation_host.py`)

| Method and path | Behaviour |
|---|---|
| `GET /v1/events?offset=N&limit=500` | Seeks to byte `N` of the host's `events.jsonl` and returns up to `limit` records or 1 MiB, whichever comes first, plus `next_offset`. `N` must be 0 or sit right after a `\n`, and must not be past the end of the file; otherwise the reply is `409 bad_offset`. The host never parses lines before `N`. |
| `GET /v1/mail?after=<seq>&limit=200` | Records of the host's `conductor` inbox with `seq > after`, in order. |
| `POST /v1/mail` `{messages:[...]}` | Writes each message into its local inbox. Returns `{results:[{msg_uid, result: delivered\|dup\|rejected, seq?, reason?}]}`. Does nothing else. |
| `GET /v1/status` | `nw queue status --json` and the host's project list. |

**Inbound mail on the host.** Inbound mail follows the write-first rule.
- The host keeps `federation/received.jsonl`, one line per committed `msg_uid`.
- Each inbox record carries `msg_uid`.
- A message is `dup` when either of these holds:
  - its `msg_uid` is in `received.jsonl`;
  - a rescan of the target inbox finds the `msg_uid` after that inbox's last-checkpoint
    offset (stored with each `received.jsonl` line).
- The order is: append to the inbox and fsync, then append to `received.jsonl` and fsync,
  then reply.
- If the fsync fails, the request fails with 5xx, and the hub will retry.
- A message is `rejected` (terminal) when:
  - the address is not `steward:<P>` for a project on this host, and not `human`;
  - or the message fails `nw.message.v1` validation.
- A message that carries an `action` is also appended to `federation/actions.jsonl` as
  `pending` (see "Remote verbs").

### Hub relay (`federation.py`)

For each enabled host, `relay_host` runs these steps in order. It stops at the first
transport failure.

1. **Events.**
   - Call `GET /v1/events?offset=cursor.events_offset`.
   - Skip each record whose `id <= cursor.events_origin_id`.
   - Skip records of type `message`. They are not lost: that mail comes through step 2.
   - Append every other record to the hub's `events.jsonl`, with `host: <name>`,
     `origin: {host, id}`, and the campaign as `<campaign>@<host>`.
   - A type the hub's `EVENT_TYPES` does not know is appended as `host_event` with
     `data.type` set to the original type. It is never dropped, and it never blocks the
     cursor.
   - Fsync, then checkpoint `events_offset = next_offset`, `events_origin_id`, and
     `hub_events_offset` (the hub file size after the fsync).
   - **Recovery after a crash.** Before the first page, rescan the hub's `events.jsonl` from
     `hub_events_offset`. Raise the effective `events_origin_id` to the largest
     `origin.id` seen there for this host.
   - **On `409 bad_offset`.** Emit `host_cursor_reset {host}` once, restart at offset 0, and
     rely on the `id` high-water mark to skip records already relayed.
2. **Conductor-bound mail.**
   - Call `GET /v1/mail?after=cursor.mail_seq`.
   - Deliver each message into the hub's `conductor` inbox through `mailbox.send`, keeping
     the original `msg_uid` and setting `from` to `<sender>@<host>`. This mirrors exactly one
     `message` event on the hub.
   - Then apply the same write-first rule as step 1:
     - fsync, then checkpoint `mail_seq` and `hub_inbox_offset`;
     - after a crash, rescan the inbox from `hub_inbox_offset` and skip any `msg_uid`
       already there.
3. **Outbox.**
   - POST the pending lines of `mailbox/outbox-<host>.jsonl` as one batch.
   - For each `delivered`, `dup` or `rejected` result, append an ack line
     `{msg_uid, result, seq?, reason?}`.
   - For each `rejected` result, also emit one `host_mail_rejected {host, msg_uid, to,
     reason}` event.
   - A transport failure leaves every line pending.
4. **Status.** Call `GET /v1/status` and store the result in `status_cache`.

**Health.**
- After any successful call, set `health.last_ok`.
- If `now - last_ok` reaches 10 min, emit exactly one `host_unreachable {host, since}`
  event per outage.
- On the first success after that, emit `host_reachable {host}` and clear the flag.

**Event types.** `host_event`, `host_unreachable`, `host_reachable`, `host_cursor_reset`
and `host_mail_rejected` are added to `EVENT_TYPES`.

### Sending (`nw mailbox send --to steward:<P>@<host>|human@<host>`)

- Parse the address. Refuse it when:
  - the host is unknown or `off`;
  - `status_cache.projects` exists and does not contain `P`.
- Append `{msg_uid: uuid4().hex, ...message}` to `outbox-<host>.jsonl` under the existing
  flock helper (`E._locked`), in a single write. This is safe across processes.
- `nw mailbox send` gains `--action park|resume --campaign C [--reason TEXT]`. These fill
  the optional `action: {verb, campaign, reason}` field of `nw.message.v1`.
- Bare addresses go through `mailbox.validate_address` unchanged. Nothing changes when
  `hosts.json` is absent.

### Remote verbs (host runner tick, `federation_host.drain_actions`)

- On a host with `federation.json`, the runner tick calls `drain_actions(cfg)` once per
  tick, after its own campaign steps.
- For each `pending` action, the host runs these checks:
  1. `verb` is `park` or `resume`;
  2. the campaign exists locally, belongs to the addressed project `P`, and has no `@`;
  3. the only other field is `reason`.
- If any check fails, the action is **refused**:
  - it is marked `done refused: <why>`;
  - the conductor gets an answer;
  - a copy goes to the host's `human`.
- Otherwise the host runs the action:
  1. append `started` and fsync;
  2. call the same function that `nw queue park` or `nw queue resume` calls, in process;
  3. append `done` with outcome `ok` or `failed: <ExcType>: <msg>`. Any `Exception` is
     caught.
  4. Send an answer with `re: <msg_uid>` to `conductor`. The hub pulls it in step 2.
- A `started` line with no `done` (the host crashed mid-verb) is never re-run. It becomes
  `done unknown: host restarted during the verb`, and the conductor gets an answer.

### Conductor and steward skills

- `/nw-conduct`:
  - treats `host`-tagged events like local ones;
  - acts on them only through `nw mailbox send --to steward:<P>@<host> --action
    park|resume ...`, or by asking in plain mail;
  - escalates `host_unreachable` that is older than 30 min to the human, and
    `host_mail_rejected` at once.
- `/nw-steward` on a host:
  - reports action outcomes, which the runner has already answered;
  - never runs verbs from free text;
  - sends to `conductor` as usual, and that mail reaches the hub.

### Views

- `nw queue status --all-hosts` merges each host's `status_cache.campaigns` into the
  table:
  - it adds a `host` column;
  - `--project` and `--state` filter host rows too;
  - `--json` includes them.
- The digest and `nw hub` show one line per host. `hub_payload()["federation_hosts"]`
  carries the same data:
  `host <name>: last contact <ago>, <n> campaigns (<state>: <count>, ...)`.
- A host whose cache is older than 10 min is marked `stale`.

## Alternatives considered

- **Keep SPEC-0335 and fix its findings.** Two campaigns diverged on it, and the triager
  showed that the spec left the hard parts unstated. A smaller spec that says them is
  cheaper than a third loop.
- **Run the verb inside `POST /v1/mail` (SPEC-0335's `route_action`).** This ties the HTTP
  timeout, the dedupe and the verb's failure modes together. That combination produced
  most of the findings. Running the verb in the tick separates them.
- **A persisted seen set of every key (SPEC-0335).** It grows forever, and the hub still
  advanced cursors past failed writes. Checkpointing a byte offset bounds recovery to one
  page.
- **Event paging by `id` with a scan.** Each poll costs time that grows with the size of the
  file, inside a 2 s timeout. A byte offset makes each poll cost the same.
- **Remote revocation and rotation.** These need a two-phase protocol to avoid locking the
  hub out of its own host. `rm`, `add` and a local `unenrol` are enough, given a handful of
  hosts.
- **No remote verbs at all.** Rejected: the conductor's decision table mostly parks and
  resumes. Without them, every host campaign decision would need a human.

## Acceptance criteria

- [ ] A host campaign shows up in the hub's `nw queue status --all-hosts` with a `host`
      column, and `--project` filters host rows.
- [ ] Every host event type except `message` reaches the hub's `events.jsonl`, tagged
      `host`. This includes `runner_dead` and an unknown type, which arrives as
      `host_event`. No event is duplicated after a relay restart, or after a crash between
      the append and the checkpoint.
- [ ] A poll reads the host's `events.jsonl` only from the cursor offset. A bad offset gives
      `409`, and recovery causes no duplicates.
- [ ] If the hub's append or fsync fails, no cursor moves, and the record arrives on the
      next tick.
- [ ] Hub-to-host mail and host-to-conductor mail each land exactly once. This holds when
      the host commits and the response is then lost. A `dup` result acks the outbox line.
- [ ] A `rejected` message is acked, is not retried, and emits one `host_mail_rejected`.
- [ ] Two concurrent `nw mailbox send` processes to the same host produce two different
      `msg_uid`s, and both messages are delivered.
- [ ] Host-to-conductor mail produces exactly one `message` event on the hub.
- [ ] A `park` action is executed by the host's runner tick and answered `ok` to the
      conductor. A raising verb is answered `failed: ...` and is not re-run. A
      `started`-only action is answered `unknown` and is not re-run. A refused action is
      answered to the conductor and copied to the host's human. `POST /v1/mail` never runs
      a verb.
- [ ] `nw hosts add|list|rm|on|off|check` work as designed:
  - `off` stops the relay and refuses sends; `on` restores both;
  - `check` exits 1 when a project is on two hosts or on the hub and a host;
  - `rm` never contacts the host.
- [ ] `nw hosts enrol` reads the blob from stdin only; `unenrol` makes the host refuse the
      hub.
- [ ] Unsigned, tampered or stale-but-validly-signed responses are refused. So are bad,
      stale or unenrolled requests.
- [ ] The digest and `nw hub` (including `hub_payload()["federation_hosts"]`) show one line
      per host, with last contact and per-state campaign counts.
- [ ] With 3 hosts, two of them unreachable, the relay adds at most 2.5 s to a tick.
      `host_unreachable` fires once after 10 min, even across a relay restart.
      `host_reachable` follows recovery.
- [ ] The conductor skill escalates `host_unreachable` older than 30 min, and
      `host_mail_rejected`.
- [ ] The hub modules neither import nor call queue mutation code.
- [ ] With no `hosts.json` and no `federation.json`, behaviour is unchanged.

## Test plan

- **Automated tests** (`tests/test_federation.py`, offline, marked `fast` where the tier
  check requires it). The setup uses two data dirs in one test, one for the hub and one for
  the host. The host's federate server runs on `127.0.0.1:0` in a thread.
  - **Events.**
    - Relay a mix of types, including `runner_dead` and an unknown type.
    - Assert the hub's records: the `host` tag, `origin`, `<campaign>@<host>` and
      `host_event`.
    - Restart the relay and check there are no duplicates.
    - Simulate a crash after the hub append but before the checkpoint (monkeypatch the
      checkpoint to raise once), then check there are no duplicates.
  - **Paging bound.**
    - Pad the host log with 10,000 records before the cursor. Spy on the reader and assert
      it parses only the lines after `offset`.
    - A mid-line offset gives `409`. The reset path emits `host_cursor_reset` and adds no
      duplicates.
  - **Durability.** Monkeypatch the hub fsync to fail once. Assert the cursor did not move
    and the record arrives on the next tick. Do this for both events and conductor mail.
  - **Lost response.**
    - Wrap `HubClient` so the real `POST /v1/mail` completes on the host and its response
      is then discarded (raise after the real call).
    - The retry gets `dup`, the outbox is acked, and the inbox holds exactly one copy.
    - Show that the test is strong enough: with `msg_uid` dedupe removed, it must fail.
      Note the result in the spec.
  - **Rejected mail.** A message to a project the host lacks is acked `rejected`, is never
    re-POSTed, and emits one `host_mail_rejected`.
  - **Concurrent sends.** Two `multiprocessing` processes each send 20 messages to one
    host. Assert 40 distinct `msg_uid`s, and assert that all 40 are delivered.
  - **Conductor mail.** One host message produces exactly one hub `message` event.
  - **Actions.** Cover each case:
    - `park` gives `ok`, and the campaign really is parked;
    - a verb monkeypatched to raise `RuntimeError` gives `failed`, and a second drain does
      not re-run it;
    - a `started`-only journal gives `unknown`;
    - `rebase`, a foreign project and extra fields are each refused, with a human copy;
    - an AST check that the `POST /v1/mail` handler calls no `drain_actions` or queue
      function.
  - **`nw hosts` CLI.**
    - `add` and `list`.
    - `off`: relay skips the host and send refuses. Then `on`: a new host event is relayed
      and send succeeds.
    - `check` with a duplicate project exits 1.
    - `rm`: assert that no HTTP call is made.
    - `enrol` reads stdin; an argv blob is not accepted. After `unenrol`, requests get 401
      or 403.
  - **Signatures.**
    - A request that is unsigned, tampered or stale.
    - A response that is unsigned or tampered.
    - A response that is correctly signed but older than 120 s; it is refused.
  - **Views.**
    - Seed a known `status_cache` (2 running, 1 parked).
    - Assert that `--all-hosts` shows the rows and that `--project` filters them.
    - Assert the brief line and `hub_payload()["federation_hosts"]` have the exact counts.
  - **Health.**
    - A stopped server leads to `host_unreachable` at 10 min (clock injected), and fires
      once.
    - The timer survives a new relay process, because `last_ok` is persisted.
    - `host_reachable` follows recovery.
    - With 3 hosts, two of them blackholed, the measured `relay_tick` takes at most 2.5 s.
  - **Guards.**
    - An AST walk of `hosts.py`, `federation.py` and `cli_hosts.py` fails on any `queue`
      import (`from . import queue as Q` included) and on any call through such an alias.
    - A skill-doc check finds the `host_unreachable` and `host_mail_rejected` rows in
      `/nw-conduct`.
    - The existing mailbox and conductor tests run unchanged with no `hosts.json`.
- **Manual verification (agent-executable):**
  1. On one machine, create two data dirs.
  2. Enrol the second as a host of the first through the real CLI
     (`nw hosts add`, then `nw hosts enrol` reading stdin).
  3. Run a real `nw serve --federate` and a real `nw queue run` for the host.
  4. Open a toy campaign on the host. Watch it in the hub's `nw queue status --all-hosts`.
  5. Send `--action park` from the hub and see the `ok` answer.
  6. Stop the server and see `host_unreachable` (with the threshold lowered for the run).
  7. Record the commands and output in "As shipped".
  Enrolling the human's second machine is rollout, not part of DoD.
- **Regression:** the affected tests (`python3 tools/affected_tests.py`) at the close gate,
  plus `spec_lint`.

## Clock Log

Use `nw idea clock-in IDEA-562` / `nw idea clock-out IDEA-562`.

## Rollout / migration

1. Inert without `hosts.json` and `federation.json`.
2. The human enrols one real host for one project and runs it for a week before enrolling
   more.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass; manual steps performed and recorded.
- [ ] No regressions (affected tests at the close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:
- The triager's four items are each resolved:
  - `GitError` cannot happen, because `rebase` and `gates` are not allowed, and any verb
    failure goes to the conductor;
  - permanent errors are terminal;
  - verbs never run in the request;
  - paging uses byte offsets.
- Revocation is local (`unenrol`), and rotation is `rm` + `add` + enrol.
- Remote mail needs an explicit `@<host>`.
