---
id: SPEC-0414
title: "Dashboard server, loopback: nw serve --dashboard with token auth and GET /api/snapshot"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-903
tags: [agentic, dashboard, remote, web, auth]
depends_on: [SPEC-0401, SPEC-0334]
---

## Context

This is the second slice of child 1 of the epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md), promoted from `IDEA-903`.
[SPEC-0401](./0401-read-only-dashboard-server-and-nw-dashboard-v1.md) split the epic's child 1
into three specs: the data layer (done), this server, and the phone page. A review of the first
draft of this spec found it too large for one campaign (15 acceptance criteria across a bind
gate, a `tailscale whois` identity gate, audit, an HTTP server, a CLI mode switch and a fake
`tailscale` fixture). It is therefore **narrowed to the loopback server**: everything that can be
built and fully verified in CI today with a bearer token. The tailnet half (own-address bind gate,
`tailscale whois` identity, MagicDNS `Host` values, the fake `tailscale` fixture) is a sibling
spec, "Dashboard server, tailnet: own-address bind gate and tailscale whois identity", that
depends on this one (see Non-goals).

The human asked for a dashboard "to consult while I am away from home", reachable over Tailscale
only. Nothing may be exposed on the public internet. This slice does not open any non-loopback
socket, so it cannot expose anything; it builds the server, the request pipeline, the audit log
and the CLI mode that the tailnet slice then widens.

What exists today, checked against the code on 2026-11-04:

- SPEC-0401 shipped `nightwright.agentic.dashboard` with `settings(cfg)` (`bind:
  "tailscale:8791"`, `allowed_logins: []`, `refresh_s`, `max_stale_s`), `dashboard_dir(cfg)`
  (`<data_dir>/agentic/dashboard/`, mode 0700, created on call, called by nothing yet),
  `snapshot.build`, `snapshot.SnapshotCache` (lazy, one lock, no thread, zero work with zero
  callers) and `snapshot.compute_rev` (first 16 hex chars of a hash that ignores age-only
  movement). `rev` was designed to be the strong ETag. The cache hands out the same dict to every
  caller, and the doc says the server serializes it once per `rev`.
- `nw serve` (`src/nightwright/agentic/cli_worker.py:213`, `serve_cmd`) has one mode,
  `--jobs`, declared `required=True` with the help "the only mode so far" and the default bind
  `127.0.0.1:8790`. It builds `pull_jobs.make_server`, which refuses a bind through
  `pull_jobs.check_bind` and reports a refusal as a `click.ClickException` (exit 1). Its
  `--tls-cert`, `--tls-key` and `--client-ca` options are declared with
  `click.Path(exists=True, dir_okay=False)` (`cli_worker.py:218-220`), so click itself rejects a
  nonexistent path with exit 2 before `serve_cmd` runs.
- `pull_jobs.PullServer` / `_Handler` (`src/nightwright/agentic/pull_jobs.py:941`, `:1013`) are
  the hardening precedent: a stdlib `ThreadingHTTPServer` with `daemon_threads`, a per-request
  socket timeout set in `finish_request` and on the handler class, bounded bodies, a quiet
  `log_message`.
- `tests/fixtures/dashboard_data.py` seeds a data dir (active campaign, `human_decision` park
  with an open `ESC-0001`, `main_dirty` and `main_not_on_base` parks, a delivered campaign, a
  lease, a >256 KiB `events.jsonl`). This spec reuses it.
- The Tailscale-only decision, the loopback token and the Host/Origin rule were decided in the
  epic (Decision 2). This spec implements the loopback token and the Host/Origin rule for
  read-only requests. The write-side parts of that decision (CSRF header, plan token, the audit
  of plans and answers) belong to epic child 2.

## Goals / Non-goals

**Goals**
- G1. `nw serve --dashboard` serves `GET /api/snapshot` from SPEC-0401's `SnapshotCache`, with a
  strong `ETag` equal to the snapshot's `rev` and `304` on `If-None-Match`.
- G2. The server binds loopback only. The check runs before any socket opens, nothing bypasses
  it (`make_server` calls it itself), and TLS flags cannot lift it.
- G3. Every request is authenticated by the per-host `local.token` bearer token before it reaches
  a route.
- G4. `Host` and `Origin` are checked on every request (DNS rebinding).
- G5. Every refusal is a line in `agentic/dashboard/audit.jsonl`. A runtime record
  `agentic/dashboard/server.json` says where the server is.
- G6. The server is read-only: over HTTP, nothing outside `agentic/dashboard/` changes, and the
  queue never imports the dashboard.
- G7. Every exit code of `nw serve --dashboard` comes from a named exception type, not from
  matching message text.

**Non-goals** (the first group is the sibling **tailnet spec**, which `depends_on` this one;
the rest are other epic children)
- *Tailnet identity slice (sibling spec):* `--bind tailscale[:PORT]`; running `tailscale`
  (`run_tailscale`, `own_tailnet_ips`, `resolve_bind`, `magicdns_name`); a `check_bind` that
  accepts this host's own tailnet addresses; the `tailscale whois --json` `Gate`
  (`allowed_logins`, the host's own login, tagged nodes, the 60 s / 5 s verdict cache, peer
  normalization); MagicDNS `Host` values; the whois audit fields and reasons (`whois_failed`,
  `login_not_allowed`, `tagged_node`); the `TailscaleDown` exception (exit 1); the `make_server`
  seams `gate`, `peer_addr`, `own` and `auth` (the last one selects whois mode on a loopback test
  bind); the fake `tailscale` executable fixture and its self-test; the switch of the
  `--dashboard` default bind to `agentic.dashboard.bind`; and the manual real-tailnet check. Until
  that spec lands, `tailscale`, tailnet addresses and every other non-loopback bind are refused.
- The phone page (`static/index.html`, `app.js`, `app.css`, the 15 s conditional poll, the
  in-page self-test, the CSP checks and the headless Chromium test). That is the **dashboard
  page** sibling spec. Until it lands `GET /` answers `404`.
- Writes of any kind: `POST` routes, the CSRF header, plan tokens, the inbox payload and
  `escalations.apply_answer` (epic child 2). In this spec every method except `GET` gets `405`.
- SSE (`/api/stream`), a snapshotter thread, `max_clients`, the campaign drilldown and the run
  tail (epic child 3). The server calls `SnapshotCache.get()` from the request thread.
- The systemd unit, `nw dashboard status|unit`, the `conduct-status` `dashboard` key and the
  exit-0-on-code-change behavior (epic child 4). `server.json` is written here so child 4 can
  read it.
- TLS, `tailscale cert` and push (epic child 5). `--tls-cert/--tls-key/--client-ca` with
  `--dashboard` are refused.
- Any change to `snapshot.py`, `queue.dashboard_payload` or `pull_jobs.py` behavior.

## Decision

1. **A second mode of `nw serve`.** `nw serve --dashboard` is a separate process from `nw serve
   --jobs`. Exactly one of the two flags is required (neither or both is a usage error, exit 2).
   `--bind` loses its fixed default: `--jobs` keeps `127.0.0.1:8790`, `--dashboard` defaults to
   `127.0.0.1:8791` in this slice (the tailnet slice changes the default to
   `agentic.dashboard.bind`, which is `tailscale:8791` and is refused here). The server is a stdlib
   `ThreadingHTTPServer` with the SPEC-0334 hardening. No new dependency.
2. **The bind gate lives in the dashboard, not in `pull_jobs`.** `auth.check_bind` accepts
   loopback addresses only. Wildcards, LAN addresses, tailnet addresses, `tailscale[:PORT]` and
   host names are refused with a `BindRefused` whose message names the rule. `make_server` runs the
   same check itself, so no caller can skip it. TLS flags cannot lift a refusal: they are checked
   after the bind gate and are refused too (`TLS is not supported with --dashboard`), because TLS
   for the dashboard arrives with epic child 5.
3. **Loopback bind: bearer token.** `Authorization: Bearer <token>` is compared in constant time
   with `<data_dir>/agentic/dashboard/local.token` (mode 0600, created on first start with
   `secrets.token_urlsafe(32)`, reused afterwards). A missing or wrong token is `401`. The token
   is never read from the query string and never printed.
4. **`Host` and `Origin` are checked before authentication, on every request.** `Host` must be
   the bound `address:port` (an IPv6 address in brackets) or `localhost:port`, using the actual
   port after the bind (so port `0` works). `Origin`, when present, must be `http://` plus one of
   those same host values; `Origin: null` is refused. Anything else is `403`.
5. **Refusals are audited, reads are not.** Every `401`, `403` and `405` refusal is a line in
   `agentic/dashboard/audit.jsonl` (`nw.dashboard.audit.v1`). Successful reads are not logged. The
   tailnet slice adds the whois reasons and epic child 2 adds plan and answer lines to the same
   file and schema. The file rotates once at 5 MiB (`audit.jsonl.1`, one backup) so unauthenticated
   traffic cannot fill the disk. No token, `Authorization` header or query string is ever written.
6. **`GET /api/snapshot` serves the cache and nothing else.** One `SnapshotCache` per server.
   The body is serialized once per `rev` and reused. `ETag: "<rev>"` is strong. `If-None-Match`
   matching the current `rev` (a list, `*`, or a `W/` form) returns `304` with no body. A `304`
   keeps the client's older body, which is correct because ages are derived from `generated_at`
   (SPEC-0401 Decision 2). The server calls no builder directly and writes no queue file.
7. **`server.json` is the runtime record.** It is written after the socket is bound and before
   serving starts, atomically, and removed on clean shutdown (SIGTERM or SIGINT) only if it still
   names this process.
8. **Exit codes come from exception types.** `auth.BindRefused(ValueError)` is raised for
   `parse_bind` and `check_bind` failures (including from `make_server`); `serve_cmd` maps it to
   `click.UsageError` (exit 2). An `OSError` from binding the socket (port in use, address the
   host does not own) maps to `click.ClickException` (exit 1). The tailnet slice adds
   `TailscaleDown(RuntimeError)` to the exit-1 side. No mapping inspects message text.
9. **Tests need no tailnet and run no `tailscale`.** The server never executes `tailscale` in this
   slice; a test puts a recording stub `tailscale` first on `PATH` and asserts it was never called.

## Design

### Modules

New files in `src/nightwright/agentic/dashboard/` (none of them is imported by `snapshot.py` or
by any queue-side module):

- `auth.py`:
  - `class BindRefused(ValueError)`.
  - `parse_bind(spec) -> (host, port)`: `ADDR:PORT`, `[v6]:PORT`, `localhost[:PORT]` (maps to
    `127.0.0.1`); port `0` is allowed (ephemeral), a missing port is `8791`. A host that is
    not an IP address after the `localhost` mapping (for example `tailscale`, `example.com`) is
    returned as given and refused by `check_bind`. `BindRefused` on a malformed spec or a port
    outside 0-65535.
  - `check_bind(host) -> str | None`: why `host` may not be bound, else `None`. Rules, in order:
    not an IP address → "`<host>` is not an IP address"; unspecified → "`<host>` is a wildcard
    address"; loopback → ok; otherwise "`<host>` is not a loopback address (this server binds
    loopback only)". An IPv4-mapped IPv6 address is unmapped first.
  - `validate_bind(spec) -> (host, port)`: `parse_bind` then `check_bind`, `BindRefused` with the
    reason. `serve_cmd` and `make_server` both call it.
  - `local_token(cfg) -> str`: reads `dashboard_dir(cfg)/local.token`, or creates it with
    `os.open(..., 0o600)`; a file with looser permissions is chmod-ed to 0600; an empty file is
    regenerated.
  - `host_values(bound_host, port) -> set[str]` (bound address with port, bracketed for IPv6, plus
    `localhost:port`) and `check_host_origin(headers, allowed) -> str | None` (the audit reason
    `bad_host` or `bad_origin`, else `None`).
- `audit.py`: `audit(cfg, **fields)` appends one JSON line under a module lock with `O_APPEND`
  and mode 0600, rotates at 5 MiB, and never raises into a request (a failed write is dropped).
  Fields: `{schema: "nw.dashboard.audit.v1", ts, action: "refused", reason, status, method, path,
  login, node, node_ip, user_agent, detail}`. `path` has no query string and is cut to 200
  characters, `user_agent` to 200. `login`, `node` and `node_ip` are `null` in this slice (the
  tailnet slice fills them) so the schema does not change later. `reason` is one of `bad_host`,
  `bad_origin`, `no_token`, `bad_token`, `method_not_allowed`.
- `server.py`:
  - `make_server(cfg, bind=None, *, cache=None) -> DashboardServer`: calls `validate_bind`
    (default `127.0.0.1:8791`), creates `dashboard_dir`, loads `local_token`, binds the socket,
    starts nothing else. `BindRefused` for a refusal; the bind `OSError` propagates. `cache` is a
    keyword-only test seam (a `SnapshotCache` with an injected clock or builder). The auth mode is
    always `"token"` here; the tailnet slice adds the other test seams.
  - `DashboardServer(ThreadingHTTPServer)`: `daemon_threads`, `block_on_close = False`, a socket
    timeout of 30 s set in `finish_request`, `address_family` IPv6 when the host has a colon.
  - `_Handler`: `protocol_version = "HTTP/1.1"`, `timeout = 30` (a module constant tests patch),
    quiet `log_message` (`NW_SERVE_LOG` turns it on, as in `pull_jobs`). Every response carries
    `X-Content-Type-Options: nosniff`, `Cache-Control: no-cache` and `Content-Length`. A request
    line or header block over the stdlib limits, or a `Content-Length` above 0 on a `GET`, is
    refused by the stdlib or by an explicit `413` and closes the connection.
  - Request flow, in this order: (1) `Host`/`Origin` check (`403`); (2) authentication (`401`);
    (3) method (`405` with `Allow: GET` for anything but `GET`; this is refusal-audited too);
    (4) route.
  - Routes: `GET /api/snapshot` as in Decision 6; every other path, including `/`, is `404
    {"error": "not found"}` after authentication. `cache.get()` raising returns `503 {"error":
    "<ExceptionType>: <message>"}` and the server keeps running.
  - `write_server_json(cfg, server)` / `remove_server_json(cfg)`:
    `{schema: "nw.dashboard.server.v1", pid, start_ticks, started_at, host, port, url, auth:
    "token"}`. `start_ticks` is field 22 of `/proc/<pid>/stat` (`null` where `/proc` is not
    available). The file is replaced atomically (`os.replace`) with mode 0600. `port` is the
    actual port, so `--bind 127.0.0.1:0` is discoverable. `remove_server_json` deletes the file
    only when its `pid` is this process. A `server.json` left by a dead process is simply
    overwritten at start.
- `cli_worker.serve_cmd` (`src/nightwright/agentic/cli_worker.py:223`): options become `--jobs`
  and `--dashboard` (both plain flags, mutually exclusive, one required), `--bind` default `None`.
  The existing `--tls-cert`, `--tls-key` and `--client-ca` declarations are unchanged (still
  `click.Path(exists=True, ...)`, so click validates the path first and the `--dashboard` TLS
  refusal below is reachable only with existing files). Dashboard path, in this order:
  1. `auth.validate_bind(bind or "127.0.0.1:8791")`; `BindRefused` → `click.UsageError` with the
     rule message (exit 2);
  2. if any TLS flag is set → `click.UsageError("TLS is not supported with --dashboard")` (exit 2).
     Because step 1 ran first, a refused bind always reports the bind-rule message, never the TLS
     one;
  3. `make_server`; an `OSError` from binding → `click.ClickException` (exit 1);
  4. `write_server_json`; print `nw serve: dashboard on http://HOST:PORT (token auth, token in
     <path>)` to stderr, never the token itself;
  5. SIGTERM and SIGINT both stop `serve_forever`, then `remove_server_json` and `server_close`,
     exit 0.
  The `--jobs` path is unchanged in behavior (same default bind, same messages, same exit codes).
- `docs/product/reference/index.md`: the `serve` row gains `serve --dashboard [--bind
  ADDR:PORT]`.

## Alternatives considered

- **Reuse `pull_jobs.check_bind`.** It accepts the Tailscale ranges and lets TLS lift a wildcard
  refusal; the dashboard's rule is narrower (loopback now, own tailnet address in the tailnet
  slice, never TLS-lifted).
- **Ship the whole server in one spec.** The first draft did, with 15 acceptance criteria and a
  network-facing identity gate; a defect in a gate is what this server must not have, so the
  loopback server is verified on its own first.
- **Match exception messages in the CLI to pick exit 1 or 2.** Fragile; named exception types
  make each exit code a test of a type.
- **Let click reject TLS paths for `--dashboard`.** The options are shared with `--jobs`; changing
  their declaration would change the `--jobs` surface, so the dashboard refusal is a check inside
  `serve_cmd` and its tests use existing files.
- **Auditing successful reads.** One line per poll per client grows without bound and records
  nothing that is not already in the snapshot. Refusals are the security signal.
- **A token in the query string.** It lands in logs and browser history; header only.
- **Serving a placeholder page now.** The page spec owns `GET /`; a placeholder here would need
  its own CSP and tests.

## Acceptance criteria

- [x] `nw serve --dashboard --bind 127.0.0.1:0` starts, writes `server.json` with the real port
      and `auth: "token"`, and `GET /api/snapshot` with the right bearer token returns `200`, a
      JSON document with `schema == "nw.dashboard.v1"` and every top-level key SPEC-0401 defines,
      and `ETag` equal to `"<rev>"` of that document.
- [x] A repeat request with `If-None-Match: "<rev>"` returns `304` with an empty body and the
      same `ETag`; a stale value returns `200`; `*` and `W/"<rev>"` return `304`. Two requests
      with no queue change cause exactly one JSON serialization (counter) and, within
      `refresh_s`, one snapshot build.
- [x] CLI surface: `nw serve` with neither `--jobs` nor `--dashboard`, or with both, exits 2;
      `nw serve --jobs` keeps its default bind `127.0.0.1:8790` and existing behavior (the
      SPEC-0334 tests pass unchanged); `--dashboard` without `--bind` binds `127.0.0.1:8791`; a
      bind that fails at the OS (port in use) exits 1 via `OSError`; and
      `docs/product/reference/index.md` lists `serve --dashboard`.
- [x] Bind gate: `nw serve --dashboard` refuses with `BindRefused` (exit 2, stderr names the rule,
      nothing listening, no `server.json`) on `0.0.0.0`, `::`, `[::]`, a LAN address
      (`192.168.1.20`), a tailnet address (`100.64.0.9`), `tailscale`, and a host name, each alone
      and each combined with `--tls-cert/--tls-key` (existing files), in which case the stderr is
      the bind-rule message. A valid loopback bind combined with TLS flags exits 2 with stderr
      `TLS is not supported with --dashboard`, and the two messages differ.
- [x] On a loopback bind, a request with no `Authorization`, a wrong token, a non-`Bearer` scheme
      or the token in the query string gets `401`; `local.token` exists with mode 0600 in a 0700
      directory and is reused across starts. The token value never appears in the audit log,
      `server.json` or stderr.
- [x] A request with a wrong `Host`, a foreign `Origin` (`http://evil.example`), `Origin: null`
      or a DNS-rebinding `Host` is `403` before authentication, even with a valid token; the bound
      `address:port` and `localhost:port` (with a matching `Origin`) pass.
- [x] `POST`, `PUT`, `DELETE` and `OPTIONS` to any path get `405` with `Allow: GET` and write
      nothing but an audit line; `GET /` and `GET /nope` get `404`; a `GET` with a body gets
      `413`; a cache that raises gives `503` and the next request works.
- [x] Every refusal (`bad_host`, `bad_origin`, `no_token`, `bad_token`, `method_not_allowed`)
      appends one valid `nw.dashboard.audit.v1` line with `reason`, `status`, method and path (no
      query); successful reads (`200`/`304`) append none; `audit.jsonl` rotates to `audit.jsonl.1`
      past 5 MiB and never exceeds one backup.
- [x] Over HTTP, after a mix of authenticated reads, conditional reads, refusals and a snapshot
      rebuild, every file under the data dir outside `agentic/dashboard/` is byte-identical and no
      file other than `local.token`, `audit.jsonl`, `audit.jsonl.1` and `server.json` exists under
      `agentic/dashboard/`; and no queue-side module (`queue`, the runner, `runproc`, `events`,
      `escalations`, `lease`, `store`, `mailbox`, `pull_jobs`, `cli_worker` at module level)
      imports `nightwright.agentic.dashboard`, and importing
      `nightwright.agentic.dashboard.snapshot` does not import `auth` or `server`.
- [x] On SIGTERM the process exits 0 and `server.json` is gone; a `server.json` naming another
      pid is left alone; a `server.json` left by a killed process does not stop a new start and is
      replaced. The server never executes `tailscale` (a recording stub on `PATH` is not called).

## Test plan

**Automated tests.** Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps. Requests go
over real sockets (`urllib` / `http.client`). CLI cases run the venv's `nw` entry point as a real
subprocess (as `tests/test_dashboard_cli.py` does) with `NW_DATA_DIR` set to the
`tests/fixtures/dashboard_data.py` seed.

- `tests/test_dashboard_auth.py` (unit, no sockets):
  - `parse_bind`: `127.0.0.1:0`, `[::1]:8791`, `localhost:8791`, `localhost` (default port),
    `tailscale` and `example.com` (parsed, not an IP), garbage and a port above 65535 →
    `BindRefused`;
  - `check_bind` table: loopback v4/v6 ok, wildcards refused (v4, v6, `[::]`), LAN refused,
    tailnet IP refused, IPv4-mapped IPv6 unmapped (`::ffff:127.0.0.1` ok, `::ffff:192.168.1.20`
    refused), host name and `tailscale` refused; each refusal message contains its rule word
    (`wildcard`, `loopback`, `not an IP`); `validate_bind` raises `BindRefused` with that message;
  - `local_token`: created 0600 in a 0700 dir, reused, loosened mode is repaired, empty file
    regenerated;
  - `check_host_origin`: bound address, `localhost`, bracketed IPv6, foreign host, foreign origin,
    missing `Host`, `Origin: null`.
- `tests/test_dashboard_audit.py` (unit): schema keys, no query string, truncation, no secret
  fields, `0600`, rotation at the cap with one backup, a read-only directory does not raise.
- `tests/test_dashboard_server.py` (real server in a thread on `127.0.0.1:0`, the seeded data dir,
  `make_server(cfg, "127.0.0.1:0", cache=...)`):
  - snapshot `200` with schema keys and `ETag == '"' + doc["rev"] + '"'`; `If-None-Match` hit,
    miss, `*`, `W/`, and a list of tags; the body bytes are identical for two requests of one
    `rev`; a serialization counter reads 1 after two requests; a ledger-only change (a cost line
    appended to the active campaign's `ledger.jsonl`) followed by a clock step past `refresh_s`
    (injected cache clock) gives a new ETag;
  - auth matrix (no header, wrong token, wrong scheme, query token) → `401`;
  - `Host`/`Origin` matrix, evaluated before auth (a wrong `Host` with a valid token is `403`);
  - methods: `POST/PUT/DELETE/OPTIONS` → `405` + `Allow: GET`; `GET /` and `GET /nope` → `404`;
    a `GET` with a body → `413`; a cache whose `get` raises → `503` and the next request works;
  - an idle connection is closed after the handler timeout (the module constant is patched to
    0.3 s);
  - audit lines for each refusal reason; none for `200`/`304`.
- `tests/test_dashboard_serve_cli.py` (real subprocess). TLS-flag cases pass real, existing
  (dummy) cert and key files created under `tmp_path`, so click's `exists=True` check passes and
  `serve_cmd` runs; they assert the stderr message, not only the exit code:
  - `--bind 0.0.0.0:0`, `[::]:0`, `192.168.1.20:0`, `100.64.0.9:0`, `tailscale`,
    `example.com:8791`, each alone and with `--tls-cert/--tls-key` (existing files) → exit 2, the
    bind-rule message in stderr (and `TLS is not supported` absent), no `server.json`, and a
    connect to the port fails;
  - `--bind 127.0.0.1:0` plus TLS flags (existing files) → exit 2 and stderr contains `TLS is not
    supported with --dashboard`; nothing listening, no `server.json`;
  - a port already in use → exit 1 (an `OSError`, not a usage error);
  - neither mode flag, both mode flags → exit 2; `--jobs` default bind unchanged (reads `--help`
    and the existing SPEC-0334 serve tests run unchanged); `--dashboard` with no `--bind` binds
    `127.0.0.1:8791` (or skips if that port is busy);
  - `--bind 127.0.0.1:0`: poll `server.json` (via `tests/waits.py`) for the port; `GET
    /api/snapshot` with the token read from `local.token` → `200`, no token → `401`; the token
    value is absent from stderr, `server.json` and `audit.jsonl`; a recording stub `tailscale`
    first on `PATH` is never called; `SIGTERM` → exit 0 and `server.json` removed;
  - a stale `server.json` (dead pid) is replaced on start; a `server.json` written by another
    live process is not removed by this server's shutdown.
- `tests/test_dashboard_http_readonly.py` (real subprocess server, seeded data dir): hash every
  file and list every directory outside `agentic/dashboard/`; run 100 authenticated and
  conditional reads and a set of refused requests of every `reason`; then have the test append one
  cost-changing line to the active campaign's `ledger.jsonl` itself (a queue-side write made by
  the test, not the server), re-take the hash as the new baseline, and run more reads so the
  server rebuilds the snapshot. Afterwards the outside tree equals that baseline and
  `agentic/dashboard/` holds only the four allowed files. This re-runs SPEC-0401's read-only
  check over HTTP.
- `tests/test_dashboard_imports.py` (extended): the listed queue-side modules plus `pull_jobs`
  and `cli_worker` never import `nightwright.agentic.dashboard` at module level (`ast` and a
  fresh interpreter); a fresh interpreter importing `nightwright.agentic.dashboard.snapshot` has
  neither `...dashboard.auth` nor `...dashboard.server` in `sys.modules`.
- Docs: `python3 tools/spec_lint.py` exits 0, and a grep of
  `docs/product/reference/index.md` finds `serve --dashboard`.
- Regression guard: the SPEC-0334 `nw serve --jobs` tests, the SPEC-0401 dashboard tests
  (`test_dashboard_snapshot`, `_cache`, `_cli`, `_readonly`, `_imports`) and
  `python3 tools/affected_tests.py --run uv run pytest -q` run unchanged.

**Manual verification (on this host, recorded in this spec's As shipped).** This slice calls no
external CLI, so the "one real call of the real binary" rule has nothing to cover here; the tailnet
slice owns the real `tailscale` checks.
1. `uv run nw serve --dashboard --bind 127.0.0.1:0`; with the token from `local.token`, `curl -si
   -H "Authorization: Bearer $(cat <data_dir>/agentic/dashboard/local.token)"
   http://127.0.0.1:<port>/api/snapshot` shows `200` and an `ETag`; repeating with
   `-H 'If-None-Match: "<rev>"'` shows `304`; `curl -si http://127.0.0.1:<port>/api/snapshot`
   shows `401`. `find <data_dir> -newer <marker>` lists nothing outside `agentic/dashboard/`
   except what a live runner or follower touched.
2. `uv run nw serve --dashboard --bind 0.0.0.0:8791` exits 2 naming the wildcard rule and
   `ss -ltn` shows nothing on 8791.

**Regression guard.** Affected tests via `python3 tools/affected_tests.py --run uv run pytest
-q`. The queue's close gate runs the affected set.

### Test record (2026-11-04)

Ran, all green: `uv run pytest -q tests/test_dashboard_auth.py tests/test_dashboard_audit.py
tests/test_dashboard_server.py tests/test_dashboard_serve_cli.py
tests/test_dashboard_http_readonly.py tests/test_dashboard_imports.py` (new or extended; 109 tests);
the SPEC-0401 dashboard tests (`test_dashboard_snapshot|_cache|_cli|_readonly|_imports`,
`test_agentic_dashboard*`), the SPEC-0334 pull-worker tests (`test_pull_workers`,
`test_pull_workers_security`, `test_worker_jobs`, `test_workers_drain`) and
`test_docs_product_reference`, `test_docs_product_ia`, `test_rich_output_guard` (278 + 221 passed
across the two runs). `python3 tools/spec_lint.py` exits 0 after `--write-ledger`. The full
`tools/affected_tests.py` set is large on a spec/docs change and was left to the close gate, as
was the full suite.

Manual (this host, seeded data dir, `nw serve --dashboard --bind 127.0.0.1:0`): with the token
from `local.token`, `curl` showed `200` + `ETag`; `If-None-Match` with that ETag gave `304`;
no token gave `401`; `find <data_dir> -newer <marker>` listed nothing outside
`agentic/dashboard/`; after SIGTERM `server.json` was gone. `nw serve --dashboard --bind
0.0.0.0:8791` exited 2 ("refusing to bind: 0.0.0.0 is a wildcard address") and `ss -ltn` showed
nothing on 8791. No `tailscale` was run (a recording stub on `PATH` stayed uncalled in the CLI
test).

Review loop 1 (tests only): added a direct `make_server` bind-gate test (six refused binds, rule
word asserted, nothing listening, no `server.json`), rule words and the bare `::` spec in the CLI
refusal test, a SIGINT shutdown case (exit 0, `server.json` mode 0600 then removed) and a grep
for `serve --dashboard` in `docs/product/reference/index.md`; `uv run pytest -q
tests/test_dashboard_server.py tests/test_dashboard_serve_cli.py` passed (53).

Review loop 2 (tests only): `nw serve --jobs` is now run through the CLI with no `--bind`
(listens on 127.0.0.1:8790, skipped if busy; SIGINT exits 0) and with `--bind 127.0.0.1:0`; a
bad Host / foreign Origin / `Origin: null` with no token, a wrong token and a non-Bearer scheme is
`403` with `bad_host`/`bad_origin` (never `401`); an unauthenticated `POST` is `401`; `server.json`
`schema`/`url`/`started_at`; an oversize or chunked refused body gets its status and
`Connection: close`; a `[::1]:0` server test (skipped here: no `::1`). Mutation-checked: dropping
`bind or PJ.DEFAULT_BIND` and swapping the auth and Host/Origin checks each fail the new tests.
`uv run pytest -q tests/test_dashboard_server.py tests/test_dashboard_serve_cli.py`: 71 passed, 1
skipped.

### As shipped

Shipped to Design with these notes:

- `tests/test_rich_output_guard.py` allowlists `cli_worker._serve_dashboard` (daemon startup
  line on stderr, like `serve_cmd`). It also allowlists the already-present
  `cli_dashboard.snapshot_cmd` plain output (SPEC-0401), which the guard flagged on this branch.
- `DashboardServer.handle_error` drops `OSError` (a peer that resets its connection) without a
  traceback; not in Design.
- A refused request's body (up to 64 KiB) is read and dropped so the response is not lost to a
  reset; larger or chunked bodies close the connection (`Connection: close`).
- `HEAD` is routed like any non-`GET` method (`405`, audited).
- `auth.parse_bind` also accepts a bare IP (`::1`, `127.0.0.1`) and a bare bracketed IPv6
  (`[::]`) with the default port; `audit.record` drops keys outside the schema.

## Rollout / migration

1. Land `dashboard/auth.py`, `audit.py`, `server.py`, the `serve_cmd` mode switch, the docs row and
   the tests in one change. No data migration: `agentic/dashboard/` is created on first start.
2. The server runs only on loopback with the local token until the tailnet sibling spec lands; it
   then adds `--bind tailscale`, the whois gate and the fake `tailscale` fixture on top of the
   same request pipeline, audit file and `server.json`.
3. The page spec then adds `GET /` and the static assets behind the same gates. Epic children 2,
   3 and 4 build on this server; child 2 adds the write routes and the CSRF/plan-token layer to
   the same audit file.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (affected set via `tools/affected_tests.py`);
      manual steps 1 and 2 performed and noted.
- [x] No regressions (`nw serve --jobs`, SPEC-0401 dashboard tests green).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None. The tailnet identity half (including whether `tailscale whois` works without root on this
host's tailscaled setup) is the sibling spec's scope and is not decided here._
