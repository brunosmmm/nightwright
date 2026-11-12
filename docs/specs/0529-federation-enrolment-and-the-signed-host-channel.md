---
id: SPEC-0529
title: "Federation enrolment and the signed host channel"
status: done
updated: 2026-11-10
owner: user
created: 2026-11-10
kind: feature
parent: SPEC-0310
depends_on: ["SPEC-0334"]
supersedes: ["SPEC-0527"]
source_idea: IDEA-562
---

## Context

[SPEC-0527](./0527-federation-bounded-relay-mail-and-two-remote-verbs.md) replaced
[SPEC-0335](./0335-federation-over-a-networked-mailbox.md) after two campaigns diverged
(`SPEC-0335-20261101-02a3` at about 7,300 lines, `SPEC-0335-20261108-5cbb` at about 2,400
lines). One gap those campaigns kept hitting belongs to enrolment: `token --rotate` and
`rm` deleted the hub's credential before the host knew.

This spec is part 1 of the split of SPEC-0527. It is the signed channel a later part can
call: the hub's host registry, the 0600 secrets, `nw hosts`, request and response HMAC,
the [SPEC-0334](./0334-pull-workers-against-a-coordinator-job-api.md) bind rules, and
`GET /v1/status`. The host serves and the hub calls. Event relay, mail, and remote verbs
are the other parts, listed under Non-goals.

What exists today and this spec reuses:

- [SPEC-0334](./0334-pull-workers-against-a-coordinator-job-api.md) `PullServer`
  (`src/nightwright/agentic/pull_jobs.py`), its bearer-token pattern (the registry stores
  `token_sha256`, the secret file is mode 0600), and `check_bind`.
- `queue.queue_payload` (`src/nightwright/agentic/queue.py`), which `nw queue status --json`
  already returns. `nw queue status` is an alias of `nw queue list`.
- The worker registry lock in `src/nightwright/agentic/workers.py` (`NAME_RE`, `update`
  under an exclusive flock). `export._atomic_write` does not fsync, so the host registry
  does not use it.

With no `hosts.json` and no `federation.json`, queue, mailbox, `nw serve --jobs`, and
`nw serve --dashboard` stay as they are.

## Goals / Non-goals

**Goals**

- The hub keeps one `hosts.json` record per host and the bearer token and HMAC secret in
  mode-0600 files. `hosts.json` stores `token_sha256` and `secret_ref`, never the raw
  token or the raw secret.
- `nw hosts add|list|rm|on|off|check|enrol|unenrol` do what Design specifies. `enrol`
  reads the blob from stdin. `rm` never contacts the host. `unenrol` revokes a running
  server on the next request.
- Every request carries the bearer token and `X-NW-Req-Sig`. While `federation.json`
  exists, every response carries `X-NW-Sig`. After `unenrol`, the response is an unsigned
  401.
- `nw serve --federate` uses the SPEC-0334 bind rules and refuses to start without
  `federation.json`.
- `GET /v1/status` is mapped from `queue.queue_payload(cfg)["items"]`.
- With neither `hosts.json` nor `federation.json`, existing behaviour is unchanged.

**Non-goals**

- Part 2, hub event relay, health, and read-only host views: byte-offset `GET /v1/events`,
  write-then-checkpoint, crash rescan, 409 reset, host and origin tags, campaign rewritten
  to `<campaign>@<host>`, unknown types kept as `host_event`, and `quiet.RULES` for the
  five new types. Network calls run in parallel, hub event appends and cursor updates are
  serialized, and a result that arrives after the 2.5 s budget is dropped.
  `host_unreachable` once at 10 min, persisted across restart. `nw queue status
  --all-hosts`, the conductor-brief line, and `hub_payload` `federation_hosts`, including
  stale, `--state`, and `--json`. Depends on the channel spec.
- Part 3, exactly-once mail between hub and host: uuid `msg_uid`, outbox acks for
  `delivered`, `dup`, and `rejected`, relayed conductor delivery that stores `msg_uid` and
  one host-tagged message event with campaign `<campaign>@<host>`, and
  `validate_address` unchanged for bare addresses. Mail reads seek to a byte offset. A
  lost response or a 5xx retries into `dup`; a 4xx is terminal. `POST /v1/mail` only
  writes mail. Depends on the channel spec.
- Part 4, remote park and resume: the action line is fsynced before the dedupe record,
  `dup` repairs a missing action, and `drain_actions` runs in the host runner tick. Park
  uses `plan_park`/`apply_park` and resume uses `plan_resume`/`apply_resume`, with `by
  human`. A deferred park stays started until state is `PARKED`, then answers `ok` once.
  Answers retry until the conductor inbox contains `re`. A refused action is also copied
  to the host human. The conductor treats only events with no `host` field as local; a
  remote event is mailbox park/resume or plain mail, and the 30 min escalation is a
  delivered `conductor_ping` `data.federation` row. Depends on the mail spec.
- `enabled` is stored here. Skipping a disabled host in the relay, and refusing a send to
  a disabled host, are part 2 and part 3.
- Remote revocation and token rotation. There is no `/v1/revoke` and no
  `nw hosts token --rotate`. Rotation is `nw hosts rm`, then `nw hosts add`, then a new
  `nw hosts enrol`.
- Cursor, health, event, and mail fields on the host record. This spec does not write
  them.
- Changes to `remote_gates.py` or `tui/campaign_detail.py`. The only `cli_worker.py`
  change is the `--federate` mode on `serve_cmd`.

## Decision

- The hub pulls and the host serves. This spec ships the registry, the secrets, the
  client, and one signed endpoint, `GET /v1/status`. It does not relay events, move mail,
  or run verbs.
- The hub stores the bearer token and the HMAC secret in mode-0600 files. `hosts.json`
  keeps `token_sha256` and `secret_ref`. `nw hosts add` prints the secret once, as a JSON
  blob on stdout. `nw hosts enrol` reads that blob from stdin and writes
  `federation.json` mode 0600.
- `nw hosts rm` deletes the hub's record and secret files and prints a reminder to run
  `nw hosts unenrol` on the host. It opens no connection. `unenrol` deletes
  `federation.json`. The running server reads that file on every request, so the next
  request gets an unsigned 401.
- Requests and enrolled responses use one HMAC construction, and both sides reject a `ts`
  more than 120 seconds from the local clock even when the MAC matches. Responses are
  signed only while `federation.json` is present.
- `GET /v1/status` is `queue.queue_payload(cfg)["items"]` mapped in process. The handler
  does not shell out to `nw queue status`.
- `nw serve --federate` refuses the same non-TLS binds as `nw serve --jobs`, via
  `pull_jobs.check_bind`. Its default bind is `127.0.0.1:8792`.
- With no `hosts.json` and no `federation.json`, queue, mailbox, `--jobs`, and
  `--dashboard` behaviour is unchanged.

## Design

### Modules

| Module | Role |
|---|---|
| `src/nightwright/agentic/hosts.py` | Hub registry `<agentic_dir>/hosts.json`, schema `nw.hosts.v1`. |
| `src/nightwright/agentic/federation.py` | `sign`, `verify`, and `HubClient`. No relay, outbox, or send. |
| `src/nightwright/agentic/federation_host.py` | `FederateServer`, per-request auth, `GET /v1/status`. |
| `src/nightwright/agentic/cli_hosts.py` | The `nw hosts` commands. |
| `src/nightwright/agentic/cli.py` | `register` adds the `nw hosts` group. |
| `src/nightwright/agentic/cli_worker.py` | `serve_cmd` gains `--federate` as a third mode. |

`hosts.py`, `federation.py`, and `cli_hosts.py` must not import `queue` or call through a
queue alias. `cli_hosts.check` reads the hub's own projects by calling
`federation_host.status_document(cfg)`. That function is the only queue call on this
channel, and it calls `queue.queue_payload` only.

The abandoned branch `nw/SPEC-0335-20261108-5cbb` may be copied for `hosts.py`, the HMAC
helpers, `HubClient`, and a `FederateServer` built on `PullServer`, and then cut down to
this Design. Do not copy relay, mail, `route_action`, `/v1/revoke`, or `token --rotate`.
Do not start from branch `nw/SPEC-0335-20261101-02a3`. This Design is enough when that
branch is absent.

`nw hosts add` prints the blob with `click.echo` on stdout and no Rich markup, so a pipe
into `nw hosts enrol` works. `list`, `rm`, and `check` also use `click.echo` for their
lines. Each of those functions is allowlisted in `tests/test_rich_output_guard.py` with a
reason that names this spec. `on`, `off`, `enrol`, and `unenrol` are silent on success.

### Registry (`<agentic_dir>/hosts.json`, `nw.hosts.v1`)

`<agentic_dir>` is `store.agentic_dir` (`<data_dir>/agentic`). The file is:

```json
{"schema": "nw.hosts.v1", "hosts": [{"name": "box", "url": "http://127.0.0.1:8792",
  "token_sha256": "<sha256 hex of the token>", "secret_ref": "secrets/hosts/box.key",
  "enabled": true, "status_cache": null}]}
```

A record this spec creates has exactly the keys `name`, `url`, `token_sha256`,
`secret_ref`, `enabled`, and `status_cache`. It has no raw `token`, no raw `secret`, and
no cursor, health, event, or mail key.

`name` must full-match `workers.NAME_RE` (`[a-z0-9][a-z0-9-]{0,30}`). `url` must parse
with `urllib.parse.urlsplit` as `http` or `https` with a non-empty hostname. `enabled` is
a bool. `status_cache` is `null` or
`{"fetched_at": "<store.now_iso()>", "projects": ["..."], "campaigns": [...]}`.

`load` returns `[]` when the file is missing, empty, or unreadable, and does not create
it. `update(cfg, fn)` takes an exclusive flock on `hosts.json.lock`, re-reads, applies
`fn` to the record list, then writes. The write creates a temp file in the same
directory, writes the JSON, flushes, fsyncs that fd, and `os.replace`s it onto
`hosts.json`. All of that happens before the lock is released. An exception from `fn` or
from the fsync leaves the previous `hosts.json` bytes in place.

### Secrets

`nw hosts add NAME --url URL` creates `secrets.token_urlsafe(32)` and
`secrets.token_hex(32)`.

- Token file: `<config_dir>/secrets/hosts/<name>.token`, contents `token + "\n"`, mode
  0600 from `os.open` (parent directory mode 0700). The path is derived from the name and
  is not stored on the record.
- HMAC key file: `<config_dir>/secrets/hosts/<name>.key`, contents `secret + "\n"`, mode
  0600 the same way. `secret_ref` is the relative path `secrets/hosts/<name>.key`.
- `token_sha256` is `hashlib.sha256(token.encode("utf-8")).hexdigest()` of the token
  without the newline.
- Stdout is one compact JSON object and a newline, with exactly the string keys
  `hub_name`, `host_name`, `token`, and `secret`. `hub_name` is `socket.gethostname()`.
  `host_name` is `NAME`. No other stdout. A second `add` of the same name exits non-zero
  and leaves the existing files and record unchanged.

Readers strip one trailing newline. The HMAC key is the secret string's UTF-8 bytes,
with no hex decoding.

### `nw hosts` commands

- `list` prints `NAME\tURL\ton|off` in registry order, one host per line. A missing
  `hosts.json` prints nothing and exits 0. The token, the secret, and `token_sha256` are
  not printed.
- `off NAME` sets `enabled` false. `on NAME` sets `enabled` true. An unknown name exits
  non-zero and leaves the file unchanged. Success prints nothing.
- `rm NAME` removes that record, fsyncs the registry, then unlinks the token file and the
  key file. Stdout is `run nw hosts unenrol on the host`. It does not open a socket and
  does not send HTTP. An unknown name exits non-zero and does not print that reminder.
- `check` calls `GET /v1/status` on each `enabled` host through `HubClient`, in registry
  order. A disabled host is printed as `NAME off` and is not contacted. On a verified
  200, it stores `status_cache` from that body and `store.now_iso()`, and prints
  `NAME ok`. On a transport error, a non-200, a missing signature, a bad signature, or a
  stale signature, it prints `NAME unreachable: <reason>`, leaves any previous
  `status_cache` as it was, and remembers the failure.
- Overlap uses the projects from the status calls that succeeded in this run, plus the
  hub's projects from `status_document(cfg)["projects"]` on the hub's config. A disabled
  host's old cache is not an input. For a project on the hub and on host `NAME`, stdout
  includes `project P on hub and NAME`. For a project on two hosts, stdout includes
  `project P on A and B` with `A` and `B` sorted. Exit 1 when any overlap line was
  printed or any enabled host was unreachable. Exit 0 when every enabled host answered
  ok and the project sets are disjoint. No hosts exits 0.
- `enrol` takes no positional arguments and no blob option. It reads one line from
  stdin. An empty line, extra arguments, or a JSON value that is not an object of four
  non-empty strings `hub_name`, `host_name`, `token`, and `secret` exits non-zero and
  does not write the file. Success writes
  `<config_dir>/secrets/federation.json` mode 0600 with exactly those four keys and
  prints nothing. A second enrol replaces the file.
- `unenrol` unlinks `federation.json` if it exists and exits 0. A missing file is still
  exit 0.

### HMAC

The signed string is four fields joined by `|` (0x7C):

1. The HTTP method, uppercase.
2. The path and query as on the request line: the path alone when there is no query,
   otherwise `path + "?" + query` in the order received, not re-sorted. No scheme and no
   host.
3. The lowercase hex SHA-256 of the raw body bytes. The empty body is
   `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
4. `ts`, decimal unix seconds, no fraction and no leading zeros except for `0`.

The MAC is lowercase hex HMAC-SHA256. The key is the secret's UTF-8 bytes.
`hmac.compare_digest` compares the MAC and, on the host, the bearer token.

A known vector, secret `abc`, method `GET`, path `/v1/status`, empty body, `ts`
`1700000000`, yields
`53f715f892cd9e350af654bcacaca46fd6dcedb104c690ffaa9060fac03a49b5`.

Requests send `Authorization: Bearer <token>`, `X-NW-Req-Ts: <ts>`, and
`X-NW-Req-Sig: <hex>`. Responses, while enrolled, send `X-NW-Sig-Ts: <ts>` and
`X-NW-Sig: <hex>` over the same four-tuple, using the request method, the request path
and query, and the raw response body. Header lookup is case-insensitive.

Skew is `abs(int(now) - ts) > 120` with `now` defaulting to `time.time()`. A difference
of 120 seconds is accepted. A difference of 121 seconds is rejected even when the MAC is
valid for that `ts`. `sign` and `verify` take `now` so tests pass a clock.

`HubClient` connects and reads with a 2 second timeout each. It builds the request URL as
`record["url"].rstrip("/") + path`. It reads the token and the secret from the 0600
files. It rejects a response whose signature is missing or wrong, whose `ts` fails the
skew rule, or whose status is not 200, and it does not return that body as a status
document.

### Host server (`nw serve --federate`)

`serve_cmd` takes exactly one of `--jobs`, `--dashboard`, and `--federate`. Two flags, or
none, is a usage error. `--jobs` and `--dashboard` keep their current binds and behaviour.
`--federate` rejects `--client-ca`. `--tls-cert` and `--tls-key` are still a pair.

Startup reads `federation.json` before it binds. A missing or invalid file raises
`click.ClickException` naming `<config_dir>/secrets/federation.json` and opens no port.
The default bind is `127.0.0.1:8792`. The bind check is `pull_jobs.check_bind`: without
TLS, loopback, `localhost`, and the Tailscale ranges `100.64.0.0/10` and
`fd7a:115c:a1e0::/48` are allowed; `0.0.0.0`, `::`, and any other address are refused at
startup. With a TLS pair, `check_bind` allows the address. Stderr gets
`nw serve: federation on <scheme>://<host>:<port>`.

`FederateServer` uses `ThreadingHTTPServer` the way `PullServer` does (daemon threads, the
same request-socket timeout and TLS handshake in `finish_request`). It installs its own
handler. It does not mount the jobs `_Handler` and it does not start `check_leases`.
`GET /v1/lease` on this server is 404.

Every request re-reads `federation.json`. Auth then proceeds in order:

1. The file is missing or invalid: 401 `{"error": "unauthorized"}` with no `X-NW-Sig`
   and no `X-NW-Sig-Ts`.
2. The bearer token is missing or fails `compare_digest` against the stored token: 401,
   and the response is signed.
3. `X-NW-Req-Ts` is missing, not a base-10 integer, or fails the skew rule: 401, signed.
4. `X-NW-Req-Sig` is missing or fails `compare_digest`: 401, signed.
5. Otherwise dispatch.

While enrolled, 401 and 404 responses are signed too. `GET /v1/status` is the only
success path. Any other path is 404 `{"error": "not_found"}`. Any other method on
`/v1/status` is 405 `{"error": "method_not_allowed"}`, signed while enrolled.
`Content-Type` on a JSON body is `application/json`.

### `GET /v1/status`

`federation_host.status_document(cfg)` calls `queue.queue_payload(cfg)` with its defaults
(in-flight rows, no project filter, no state filter) and maps `payload["items"]`:

- `campaigns` keeps item order. Each object is
  `{"campaign", "project", "state", "spec_id", "kind"}` copied from that item.
- `projects` is the sorted set of item `project` values that are non-empty strings.

The body is
`{"schema": "nw.federation.status.v1", "projects": projects, "campaigns": campaigns}`.
A terminal campaign, which `queue_payload` omits by default, is absent. The handler does
not call a queue mutation function (`park`, `resume`, `apply_park`, `apply_resume`, or
any other writer).

## Alternatives considered

- **Ship the whole of SPEC-0527 as one spec.** Two campaigns already diverged on a spec
  that left the channel, the relay, the mail, and the verbs in one document. A channel
  spec that stops at enrolment and `GET /v1/status` gives the later parts a stable
  client.
- **Shell out to `nw queue status --json` for the status body.** The CLI alias can change
  shape without `queue_payload` changing. Mapping `queue_payload` items in process keeps
  one source.
- **Put the enrol blob in argv.** It would show up in `/proc/*/cmdline`. Stdin keeps it
  off the command line.
- **Remote revocation and `token --rotate`.** They need a two-phase handshake so the hub
  cannot lock itself out. `rm`, `add`, and a local `unenrol` are enough for a handful of
  hosts, and `rm` never contacts the host.
- **Reuse `export._atomic_write` for `hosts.json`.** That helper does not fsync. A crash
  during the write could leave a partial registry, so this spec fsyncs its own temp file.

## Acceptance criteria

- [x] `nw hosts add NAME --url URL` writes `<config_dir>/secrets/hosts/<name>.token` and
      `<name>.key` mode 0600, with the parent directory mode 0700. The `nw.hosts.v1`
      record's keys are exactly `name`, `url`, `token_sha256`, `secret_ref`, `enabled`,
      and `status_cache`. `secret_ref` is `secrets/hosts/<name>.key`. `token_sha256` is
      the SHA-256 hex of the token file with the newline stripped. The record has no raw
      token or secret. Stdout is one JSON line whose keys are exactly `hub_name`,
      `host_name`, `token`, and `secret`, with `hub_name` equal to `socket.gethostname()`
      and `host_name` equal to `NAME`. A second `add` of the same name exits non-zero and
      leaves the files and the record unchanged. A name that fails `workers.NAME_RE`, or
      a URL that is not `http` or `https` with a hostname, exits non-zero and writes
      nothing.
- [x] Registry updates fsync the temp file before `rename`. When that fsync raises, the
      previous `hosts.json` bytes are unchanged. Two successful updates in a row both
      persist.
- [x] `nw hosts list` prints `NAME\tURL\ton|off` and does not print the token, the
      secret, or `token_sha256`. A missing `hosts.json` prints nothing, exits 0, and
      does not create the file.
- [x] `nw hosts rm NAME` removes the record and both secret files, prints
      `run nw hosts unenrol on the host`, and completes without a connection to the
      host's URL. An unknown name exits non-zero and does not print that line.
- [x] `nw hosts off` sets `enabled` false and `nw hosts on` sets it true. `check` prints
      `NAME off` for a disabled host and does not connect to it.
- [x] `nw hosts enrol` writes `federation.json` mode 0600 with exactly the four blob
      keys, from stdin only. A positional blob, an empty stdin, or a blob missing a
      field exits non-zero and leaves any existing file unchanged. A second enrol
      replaces the file. `nw hosts unenrol` unlinks the file and exits 0, including when
      the file is already absent. `nw hosts` has exactly the commands `add`, `list`,
      `rm`, `on`, `off`, `check`, `enrol`, and `unenrol`.
- [x] `nw serve --federate` exits non-zero and binds no socket when `federation.json` is
      absent. The message names that path.
- [x] After `unenrol` deletes `federation.json`, a server that is already running answers
      401 with neither `X-NW-Sig` nor `X-NW-Sig-Ts`, for an unsigned request and for a
      request still signed with the old secret.
- [x] While enrolled, an unsigned request, a tampered body, a wrong MAC, a wrong bearer
      token, and a `ts` 121 seconds off each get 401, and that 401 carries a valid
      `X-NW-Sig`. A `ts` exactly 120 seconds off is accepted. A non-GET on `/v1/status`
      is 405 and any other path is 404, and both of those responses are signed while
      enrolled. There is no `/v1/revoke`. The vector secret `abc`, `GET`, `/v1/status`,
      empty body, `ts` `1700000000` signs as
      `53f715f892cd9e350af654bcacaca46fd6dcedb104c690ffaa9060fac03a49b5`.
- [x] `HubClient` rejects an unsigned response, a tampered response, and a response whose
      MAC is valid but whose `ts` is 121 seconds off. It accepts a matching signature at
      a skew of 120 seconds. Its connect timeout and its read timeout are each 2 seconds.
      The request URL is `record["url"].rstrip("/") + path`.
- [x] `GET /v1/status` returns `Content-Type: application/json` and
      `nw.federation.status.v1` whose `campaigns` are `queue.queue_payload(cfg)["items"]`
      in order, each with the same `campaign`, `project`, `state`, `spec_id`, and
      `kind`, and whose `projects` is the sorted unique non-empty `project` values. A
      terminal campaign is absent. The handler does not shell out. `GET /v1/lease` on
      the federate server is 404, and the server starts no `check_leases` thread.
- [x] `nw hosts check` stores `status_cache.fetched_at`, `projects`, and `campaigns` from
      a verified 200. It exits 1 when a project is on the hub and a host or on two hosts,
      and the overlap lines are `project P on hub and NAME` and `project P on A and B`
      with the two host names sorted. It exits 1 when an enabled host is unreachable,
      and that host's previous `status_cache` is unchanged. It exits 0 when every enabled
      host answers and the sets are disjoint, and when there are no hosts. A disabled
      host whose old `status_cache` names a hub project does not by itself cause exit 1.
- [x] Without TLS, `nw serve --federate` refuses `0.0.0.0`, `::`, and a public address
      at startup, and accepts a loopback bind. The default bind is `127.0.0.1:8792`.
      Stderr on a successful start contains `nw serve: federation on`. `--tls-cert`
      without `--tls-key` is refused. `--federate` with `--client-ca` is a usage error.
      Exactly one of `--jobs`, `--dashboard`, and `--federate` is required.
- [x] An AST walk of `hosts.py`, `federation.py`, and `cli_hosts.py` fails on any
      `queue` import, including `from . import queue as Q`, and on any call through such
      an alias. The only `queue` attribute call in `federation_host.py` is
      `queue_payload`, and that module does not reference `subprocess`.
      `tests/test_rich_output_guard.py` passes with allowlist entries for the `add`,
      `list`, `rm`, and `check` functions in `cli_hosts.py`.
- [x] With no `hosts.json` and no `federation.json`, `queue.queue_payload` still returns
      schema `nw.queue.v1` and creates neither file, and `nw hosts list` exits 0 without
      creating them. `nw serve --jobs` and `nw serve --dashboard` still start their
      existing servers.

## Test plan

- **Automated tests:** `tests/test_federation_channel.py`, module-marked
  `pytest.mark.fast`. Two configs in one test (hub data dir and host data dir). The
  federate server listens on `127.0.0.1:0` in a thread. Clocks are passed into `sign`,
  `verify`, and `HubClient`; tests do not sleep for the skew window.
  - `test_add_stores_0600_secrets_and_hash_only_record` — file modes are 0600, the
    parent directory is 0700, the record's key set is exactly the six fields,
    `secret_ref` is `secrets/hosts/<name>.key`, `token_sha256` matches the token file,
    and the stdout blob's `hub_name` is `socket.gethostname()` and its `token` and
    `secret` match the files.
  - `test_add_rejects_duplicate_bad_name_and_bad_url` — a second add, a name that fails
    `NAME_RE`, and a non-http URL each exit non-zero and leave the directory unchanged.
  - `test_list_hides_secrets_and_missing_file_is_empty` — the list line is
    `NAME\tURL\ton`, the token is absent from stdout, and a missing file does not create
    `hosts.json`.
  - `test_registry_fsync_failure_keeps_previous_bytes` — monkeypatch `os.fsync` to raise
    once and assert the previous file bytes. A follow-up update after a successful write
    keeps both records.
  - `test_rm_deletes_secrets_prints_unenrol_and_makes_no_http` — bind `127.0.0.1:0`,
    point the record's URL at it, run `rm`, assert the listening socket accepted nothing,
    the reminder line is present, and both secret files and the record are gone.
  - `test_on_off_sets_enabled` — `off` then `on` round-trips `enabled`.
  - `test_enrol_stdin_only_0600` — a stdin blob writes mode 0600 and the four keys; a
    positional argument and an empty stdin do not write; a second enrol replaces the
    file; `unenrol` exits 0 when the file is already gone. `nw hosts --help` lists
    exactly `add`, `list`, `rm`, `on`, `off`, `check`, `enrol`, and `unenrol`.
  - `test_serve_federate_refuses_without_federation_json` — non-zero and no listen
    socket.
  - `test_unenrol_running_server_returns_unsigned_401` — delete the file under a live
    server; an unsigned request and a still-valid signed request both get 401 with no
    signature headers.
  - `test_hmac_test_vector` — the `abc` vector above.
  - `test_enrolled_request_auth` — unsigned, tampered body, wrong token, skew 121
    rejected with a signed 401; skew 120 accepted; a POST on `/v1/status` is a signed
    405; `GET /nope` is a signed 404; `POST /v1/revoke` is 404.
  - `test_hubclient_rejects_bad_responses` — unsigned, tampered, and skew-121 responses
    rejected; skew 120 accepted; the client timeout attributes are 2 and 2; a URL with
    a trailing slash requests `.../v1/status` with a single slash.
  - `test_status_maps_queue_payload_items` — seed one in-flight campaign and one
    terminal campaign; the body matches the mapping and the content type is
    `application/json`; `GET /v1/lease` is 404; no thread is running `check_leases`.
  - `test_bind_and_serve_flags` — `0.0.0.0`, `::`, and a public address are refused
    without TLS; loopback binds; the default bind string is `127.0.0.1:8792`; stderr
    contains `nw serve: federation on`; `--federate --client-ca` and `--jobs
    --federate` are usage errors; `--jobs` alone still constructs the job server.
  - `test_check_overlap_unreachable_and_disabled` — two hosts sharing a project exit 1
    with `project P on A and B` in sorted name order; a host project equal to a hub
    `queue_payload` project exits 1 with `project P on hub and NAME`; a closed port
    exits 1, prints `unreachable`, and leaves `status_cache` null; a disabled host is
    `off`, is not connected, and an overlapping project left in its old cache does not
    cause exit 1; no hosts exits 0.
  - `test_hub_modules_do_not_import_queue` — the AST walk described in the acceptance
    criteria, including the `queue_payload`-only check and the absence of `subprocess`
    in `federation_host.py`.
  - `test_inert_without_registry_files` — `queue_payload` schema `nw.queue.v1` creates
    neither file; `nw hosts list` exits 0 and creates neither file.
  - `tests/test_rich_output_guard.py` passes after the new `click.echo` allowlist
    entries for the `add`, `list`, `rm`, and `check` functions.
- **Manual verification:**
  1. Create two empty directories and export `NW_DATA_DIR` and `NW_CONFIG_DIR` for each
     (hub, then host).
  2. On the hub, run `nw hosts add box --url http://127.0.0.1:8792` and pipe stdout into
     the host's `nw hosts enrol`.
  3. On the host, queue a toy in-flight campaign and run `nw serve --federate`.
  4. On the hub, run `nw hosts check` and confirm a line `box ok` and a `status_cache`
     campaign whose id, project, and state match the host's `nw queue status --json`.
  5. On the host, run `nw hosts unenrol`. The next hub `nw hosts check` prints
     `box unreachable`. A raw unsigned `GET /v1/status` to the still-running server
     returns 401 and no `X-NW-Sig`.
  6. Record the commands and the output under an `As shipped` heading in this spec.
  Enrolling a second physical machine is rollout, not part of this spec's definition of
  done.
- **Regression guard:** at the close gate, `python3 tools/affected_tests.py` plus
  `python3 tools/spec_lint.py`. `tests/test_agentic_mailbox.py`,
  `tests/test_agentic_conduct_cli.py`, and `tests/test_agentic_conductor_guard.py` need
  no `hosts.json`; they cover the inert mailbox and conductor paths.

### Test record (2026-11-10)

- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 20 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` — exit 0.
- Full suite and `python3 tools/affected_tests.py --run uv run pytest -q` left to the close gate.

### Test record (2026-11-10, loop 1)

- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 20 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to the close gate.

### Test record (2026-11-10, loop 2)

- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 21 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (2026-11-10, loop 3)

- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 22 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Manual two-directory enrolment (below) — `box ok`, then after unenrol `box unreachable` and
  unsigned `GET /v1/status` HTTP 401.
- Full suite left to gating.

### Test record (2026-11-10, review loop 0)

- `uv run pytest tests/test_federation_channel.py::test_https_connect_failure_is_unreachable -q` — passed.
- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 23 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (2026-11-10, review loop 1)

- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 26 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (2026-11-10, review loop 2)

- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 29 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (2026-11-10, review loop 3)

- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 43 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Manual two-directory replay (below) re-run against the final code — `box ok`, then `box unreachable: HTTP 401`.
- Full suite left to gating.

### Test record (2026-11-10, security loop)

- Tests only, no code change: added `test_request_mac_is_enforced` (wrong MAC, missing and empty
  signature header, valid MAC over a tampered body / other path / other method each 401 with a
  signed 401; correct MAC 200 and signed; after `unenrol` every one of those is an unsigned 401
  with no `X-NW-Sig*` header). Overlap, cache and no-host checks stay in
  `test_check_overlap_unreachable_and_disabled`.
- `test_bind_and_serve_flags` no longer binds the real default port 8792 (it records the requested
  bind and serves on an ephemeral port), so a host already listening on 8792 cannot fail it.
- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 44 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- Full suite left to gating.

### Test record (2026-11-10, review loop 4)

- Tests only, no code change: `test_bind_and_serve_flags` now invokes `nw serve` with no mode
  flag, `--jobs --dashboard`, `--jobs --federate`, `--dashboard --federate` and all three, and
  asserts exit 2, the `exactly one of` message, and that neither `make_federate_server` nor
  `PJ.make_server` is called; it also refuses `--federate --tls-key` alone.
- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 44 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- Full suite left to gating.

### Test record (2026-11-11, review loop 5)

- Closed the signed-401 reflection oracle: `federation_host._dispatch` refuses a GET/HEAD with a
  body (signed 401); `test_enrolled_request_auth` now expects 401 for a bodied GET with a matching
  MAC and replays a captured signed 401 as a request MAC (401, no status document).
- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 44 passed.
- Full suite left to gating.

### Test record (2026-11-11, review loop 6)

- `nw hosts check` now reads the registry with `read_strict` (a corrupt or wrong-schema file is a
  one-line error, exit non-zero); `hosts.find` and the cache writer skip non-dict records;
  `on`, `off`, `check` turn `TypeError`/`AttributeError`/`RecursionError` into a one-line error or an
  `unreachable` line. `status_cache` stores only the five documented campaign keys as bounded
  (256 chars, at most 1000 items) printable strings, and drops non-printable project names.
- Tests added: request-MAC body binding (signed POST body passes the MAC, signed over `b""` or
  other bytes is a signed 401), HEAD/Transfer-Encoding/oversize-body refusals, positional-blob
  rejection with a valid blob, 0600 after a second enrol, a spy proving a disabled host is never
  contacted, the payload-only AST walker self-test, `check` reasons for bad and stale signature,
  missing credentials, nameless record and cache write failure, sanitised campaigns, and a
  registry with corrupt records for `check`/`on`/`off`.
- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 47 passed.
- Full suite left to gating.

### Test record (2026-11-12, review loop 7)

- Domain-separated the MACs: the wire signs `nw-req|METHOD|PATH|BODYSHA|TS` for requests and
  `nw-resp-<status>|METHOD|PATH|BODYSHA|TS` for responses; `verify` requires the direction tag.
  Added `test_response_mac_is_not_a_request_mac` (a captured signed 401 replayed as the request MAC
  of a POST, which reaches 405 if the MAC were accepted, is 401; cross-direction, other status,
  other method and other path all `bad signature`; a request MAC over a tampered body is a signed
  401). `test_enrolled_request_auth` keeps the GET reflection replay (401).
- `uv run pytest tests/test_federation_channel.py tests/test_rich_output_guard.py -q` — 48 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- Full suite left to gating.

### As shipped (2026-11-10)

Replay with two empty directory pairs (`HUB_*` and `HOST_*` below stand for the `NW_DATA_DIR` /
`NW_CONFIG_DIR` of the hub and of the host); `hub`/`host` mean `nw` run with that pair exported.
The hub's registry URL is the host's real bind (`127.0.0.1:8792`), so no re-pointing is needed.

```
$ hub  nw hosts add box --url http://127.0.0.1:8792 > blob.json
$ cat blob.json
{"hub_name":"<hub-hostname>","host_name":"box","token":"…","secret":"…"}
$ host nw hosts enrol < blob.json
$ host nw queue status --json     # the toy in-flight campaign, items mapped to five keys:
[{'campaign': 'SPEC-9001-20261110-0001', 'project': 'Toy', 'state': 'QUEUED', 'spec_id': 'SPEC-9001', 'kind': 'implement'}]
$ host nw serve --federate --bind 127.0.0.1:8792
nw serve: federation on http://127.0.0.1:8792          # stderr
$ hub  nw hosts check
box ok
$ python -c 'print(json.load(open("HUB_DATA/agentic/hosts.json"))["hosts"][0]["status_cache"])'
{'fetched_at': '2026-11-10T18:24:19-05:00', 'projects': ['Toy'], 'campaigns': [{'campaign': 'SPEC-9001-20261110-0001', 'project': 'Toy', 'state': 'QUEUED', 'spec_id': 'SPEC-9001', 'kind': 'implement'}]}
$ host nw hosts unenrol
$ hub  nw hosts check
box unreachable: HTTP 401                              # exit 1
$ curl -s -D - -o /dev/null http://127.0.0.1:8792/v1/status
HTTP/1.1 401 Unauthorized                              # no X-NW-Sig, no X-NW-Sig-Ts header
```

The toy campaign was created with `store.create_campaign` in the host's data dir (state
`QUEUED`, project `Toy`), so its `campaign`, `project`, `state`, `spec_id` and `kind` in
`status_cache` equal the `nw queue status --json` item.

Deviations from the Design wording, recorded here because Design is frozen:

- `hosts.validate_url` accepts `http(s)://HOST[:PORT][/PREFIX]` and stores the URL exactly as
  given (a hub behind a path-prefix reverse proxy can be registered). It additionally refuses
  userinfo, a query, a fragment, and whitespace or control characters, which the Design does
  not mention.
- `write_host_secrets` and `write_federation` fsync the temp file before the rename, like the
  registry, so a crash cannot leave a registry naming a host whose secret file is empty.
- `HubClient` enforces one 2 s wall-clock budget from the end of connect for the TLS handshake,
  the request, the headers and the body (a watchdog shuts the socket), in addition to the
  per-recv 2 s socket timeout. A peer that drips bytes is cut off with `ConnectionError`.
- The host answers a GET or HEAD that carries a body (positive `Content-Length` or any
  `Transfer-Encoding`) with a signed 401 before the MAC check. Responses are signed with the same
  construction as requests, so without this a signed error body for `GET /v1/status` was a valid
  request MAC for that method and path, and a bearer-token holder without the secret could replay
  it. `test_enrolled_request_auth` now covers the replay. Superseded in review loop 7: the MACs are now domain-separated on the wire (see the next item).
- Request and response MACs are domain-separated (review loop 7). `federation.sign(..., tag=)`
  prepends a direction tag to the four-field string: `nw-req` on requests, `nw-resp-<status>` on
  responses (so a response MAC also binds its status code); `verify` requires the tag. `sign` with
  no tag is the bare four-field construction, so the frozen `abc` vector still holds for it, but
  the wire never uses it. The wire MACs differ from the Design's four-field text; this is a
  deliberate deviation to close the signed-401 reflection oracle. The GET/HEAD-with-body refusal
  stays as defence in depth.
- `nw hosts check` reports a status document whose `projects` is not a list as
  `NAME unreachable: bad status document`, and a registry record without a `name` as
  `? unreachable: malformed host record`; it continues with the remaining hosts. `add`/`rm`
  on a corrupt `hosts.json` exit non-zero with a one-line error.

## Clock Log

Use `nw idea clock-in IDEA-562` at the start of an implementation session and
`nw idea clock-out IDEA-562` when pausing or finishing.

## Rollout / migration

1. With no `hosts.json` and no `federation.json`, nothing on the queue, mailbox, job
   server, or dashboard path changes.
2. The human enrols one host for one project and runs it before adding another. That
   enrolment is rollout. It is not required to mark this spec done.
3. There is no backfill. A hub that has never run `nw hosts add` has no registry file.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed and recorded.
- [x] No regressions (affected tests at the close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:

- Revocation is local (`unenrol`). Rotation is `rm`, then `add`, then a new enrol.
- `rm` never dials the host.
- The status body is mapped from `queue.queue_payload` items, not from a CLI subprocess.
- The federate default bind is `127.0.0.1:8792`, so it does not share the job port 8790
  or the dashboard port 8791.
- A 401 after `unenrol` is unsigned. A 401 while enrolled is signed.
