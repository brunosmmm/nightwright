---
id: SPEC-0414
title: "Dashboard server: nw serve --dashboard, bind gate, whois/token auth and GET /api/snapshot"
status: proposed
owner: user
created: 2026-11-04
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
into three specs: the data layer (done), this server, and the phone page. The human asked for a
dashboard "to consult while I am away from home", reachable over Tailscale only. Nothing may be
exposed on the public internet.

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
  `pull_jobs.check_bind` (loopback and the Tailscale ranges without TLS, a wildcard only with TLS)
  and reports a refusal as a `click.ClickException` (exit 1).
- `pull_jobs.PullServer` / `_Handler` (`src/nightwright/agentic/pull_jobs.py:941`, `:1013`) are
  the hardening precedent: a stdlib `ThreadingHTTPServer` with `daemon_threads`, a per-request
  socket timeout set in `finish_request` and on the handler class, bounded bodies, a quiet
  `log_message`.
- `pull_jobs.check_bind` accepts any address in the Tailscale ranges, including another node's.
  A dashboard must not: it may bind only this host's own tailnet address.
- No module shells out to `tailscale` yet. `tailscale` 1.102 is installed on the host but is
  logged out, so `tailscale ip` fails there today.
- `tests/fixtures/dashboard_data.py` seeds a data dir (active campaign, `human_decision` park
  with an open `ESC-0001`, `main_dirty` and `main_not_on_base` parks, a delivered campaign, a
  lease, a >256 KiB `events.jsonl`). This spec reuses it.
- The Tailscale-only decision, the identity gate (`tailscale whois --json`), the loopback token
  and the Host/Origin rule were decided in the epic (Decision 2). This spec implements them for
  read-only requests. The write-side parts of that decision (CSRF header, plan token, the audit
  of plans and answers) belong to epic child 2.

## Goals / Non-goals

**Goals**
- G1. `nw serve --dashboard` serves `GET /api/snapshot` from SPEC-0401's `SnapshotCache`, with a
  strong `ETag` equal to the snapshot's `rev` and `304` on `If-None-Match`.
- G2. The server cannot bind anywhere except loopback or one of this host's own tailnet
  addresses. The check runs before any socket opens and nothing bypasses it, including TLS flags.
- G3. Every request is authenticated before it reaches a route: `tailscale whois` on a tailnet
  bind (login must be allowed, tagged nodes refused, any failure denies), the per-host
  `local.token` on a loopback bind.
- G4. `Host` and `Origin` are checked on every request (DNS rebinding).
- G5. Every refusal is a line in `agentic/dashboard/audit.jsonl`. A runtime record
  `agentic/dashboard/server.json` says where the server is.
- G6. The server is read-only: over HTTP, nothing outside `agentic/dashboard/` changes, and the
  queue never imports the dashboard.
- G7. All of it is provable in CI without a tailnet: a fake `tailscale` executable fixture and
  real sockets, and real subprocesses for the CLI cases.

**Non-goals**
- The phone page (`static/index.html`, `app.js`, `app.css`, the 15 s conditional poll, the
  in-page self-test, the CSP checks and the headless Chromium test). That is the **dashboard page**
  sibling spec and it depends on this one. Until it lands `GET /` answers `404`.
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
   `agentic.dashboard.bind` (`tailscale:8791`). The server is a stdlib `ThreadingHTTPServer` with
   the SPEC-0334 hardening. No new dependency.
2. **The bind gate lives in the dashboard, not in `pull_jobs`.** `auth.check_bind` accepts
   loopback, or an address that is in this host's own `tailscale ip` output. Wildcards, LAN
   addresses, other nodes' tailnet addresses and host names are refused with exit 2 and a message
   naming the rule. `--bind tailscale[:PORT]` resolves to this host's first own tailnet IPv4
   address. `make_server` calls `check_bind` itself, so no caller can skip it. TLS flags cannot
   lift a refusal: they are checked after the bind gate and are refused too, because TLS for the
   dashboard arrives with epic child 5.
3. **Identity gate by bind type.** The auth method follows the bound address:
   - **Tailnet bind: `tailscale whois --json <peer-ip>`.** The peer IP is the socket's remote
     address (never `X-Forwarded-For`). The login in `UserProfile.LoginName` must be in
     `allowed_logins`, which when empty means the host's own login (`tailscale status --json`,
     `Self.UserID` looked up in `User`). A node with `Node.Tags` and no user profile is refused.
     Any lookup failure (non-zero exit, timeout, bad JSON, missing fields, own login unknown)
     denies with `403`. Verdicts are cached per peer IP: 60 s for a resolved identity, 5 s for a
     failure.
   - **Loopback bind: bearer token.** `Authorization: Bearer <token>` is compared in constant time
     with `<data_dir>/agentic/dashboard/local.token` (mode 0600, created on first start with
     `secrets.token_urlsafe(32)`, reused afterwards). A missing or wrong token is `401`.
   - Local processes on the host that connect to the host's own tailnet address arrive from that
     address, and whois resolves it to the host's own login. They are treated as the host's
     owner. That is accepted: the owner's processes can already read the data dir.
4. **`Host` and `Origin` are checked before authentication, on every request.** `Host` must be
   the bound `address:port` (loopback binds also accept `localhost:port`), or on a tailnet bind
   this host's MagicDNS name with the port. `Origin`, when present, must be `http://` plus one of
   those same host values. Anything else is `403`. A tailnet bind with no resolvable MagicDNS name
   accepts the bound address only.
5. **Refusals are audited, reads are not.** Every `401`, `403` and `405` refusal is a
   line in `agentic/dashboard/audit.jsonl` (`nw.dashboard.audit.v1`). Successful reads are not
   logged. Epic child 2 adds plan and answer lines to the same file and schema. The file rotates
   once at 5 MiB (`audit.jsonl.1`, one backup) so unauthenticated traffic cannot fill the disk. No
   token, `Authorization` header or query string is ever written.
6. **`GET /api/snapshot` serves the cache and nothing else.** One `SnapshotCache` per server.
   The body is serialized once per `rev` and reused. `ETag: "<rev>"` is strong. `If-None-Match`
   matching the current `rev` (a list, `*`, or a `W/` form) returns `304` with no body. A `304`
   keeps the client's older body, which is correct because ages are derived from `generated_at`
   (SPEC-0401 Decision 2). The server calls no builder directly and writes no queue file.
7. **`server.json` is the runtime record.** It is written after the socket is bound and before
   serving starts, atomically, and removed on clean shutdown (SIGTERM or SIGINT) only if it still
   names this process.
8. **Tests do not need a tailnet.** A fake `tailscale` executable on `PATH`, driven by a scenario
   file, stands in for `ip`, `status --json` and `whois --json`. The whois path is exercised
   in-process through a keyword-only `peer_addr` hook of `make_server` that tells the server
   which peer address to look up. The CLI cannot set it. Real tailnet behavior is verified by the
   human on the real host (Test plan, manual).

## Design

### Modules

New files in `src/nightwright/agentic/dashboard/` (none of them is imported by `snapshot.py` or
by any queue-side module):

- `auth.py`:
  - `run_tailscale(args, timeout=5.0) -> subprocess.CompletedProcess | None` runs `tailscale
    <args>` found on `PATH`, no shell, `None` on `OSError`, timeout or non-zero exit.
  - `own_tailnet_ips() -> list[ipaddress._BaseAddress]` parses `tailscale ip` (one address per
    line), `[]` when tailscale is missing, logged out or prints junk.
  - `parse_bind(spec) -> (host, port)`: `tailscale`, `tailscale:PORT` (default port 8791),
    `ADDR:PORT`, `[v6]:PORT`; `localhost` means `127.0.0.1`; port `0` is allowed (ephemeral).
    `ValueError` on anything else.
  - `resolve_bind(spec, own) -> (host, port)`: `tailscale` becomes the first IPv4 of `own`;
    `ValueError("no tailnet address: tailscale is not up")` when `own` has none.
  - `check_bind(host, own) -> str | None`: why `host` may not be bound, else `None`. Rules, in
    order: not an IP address (after the `localhost` mapping) → "not an IP address"; unspecified →
    "is a wildcard address"; loopback → ok; in `own` → ok; otherwise "is neither loopback nor one
    of this host's own tailnet addresses (own: <list or none>)". An IPv4-mapped IPv6 address is
    unmapped first.
  - `local_token(cfg) -> str`: reads `dashboard_dir(cfg)/local.token`, or creates it with
    `os.open(..., 0o600)`; a file with looser permissions is chmod-ed to 0600; an empty file is
    regenerated.
  - `class Gate(cfg, run=run_tailscale, clock=time.monotonic, ttl=60.0, deny_ttl=5.0)` with
    `check(peer_ip) -> Verdict(ok, login, node, node_ip, reason)`. One lock serializes lookups, so
    a flood of unknown peers spawns at most one `tailscale` process at a time. `allowed()` returns
    `allowed_logins` lowercased, or the host's own login (cached for `ttl`) when the list is
    empty. Login comparison is case-insensitive. The peer string is normalized with `ipaddress`
    before it is passed to `whois`, so it can never be an option.
  - `host_values(bound_host, port, magicdns) -> set[str]` and `check_host_origin(headers, allowed)
    -> str | None`.
  - `magicdns_name() -> str | None`: `Self.DNSName` of `tailscale status --json` without the
    trailing dot.
- `audit.py`: `audit(cfg, **fields)` appends one JSON line under a module lock with `O_APPEND`
  and mode 0600, rotates at 5 MiB, and never raises into a request (a failed write is dropped).
  Fields: `{schema: "nw.dashboard.audit.v1", ts, action: "refused", reason, status, method, path,
  login, node, node_ip, user_agent, detail}`. `path` has no query string and is cut to 200
  characters, `user_agent` to 200. `login`, `node` and `node_ip` are `null` when not resolved.
  `reason` is one of `bad_host`, `bad_origin`, `no_token`, `bad_token`, `whois_failed`,
  `login_not_allowed`, `tagged_node`, `method_not_allowed`.
- `server.py`:
  - `make_server(cfg, bind=None, *, cache=None, gate=None, peer_addr=None, own=None) ->
    DashboardServer`: parses and resolves the bind, calls `check_bind`, creates
    `dashboard_dir`, chooses `auth = "token"` for a loopback host and `"whois"` otherwise, binds
    the socket, starts nothing else. `ValueError` for a refusal (the CLI maps it to exit 2).
    `cache`, `gate`, `peer_addr` and `own` are keyword-only test seams.
  - `DashboardServer(ThreadingHTTPServer)`: `daemon_threads`, `block_on_close = False`, a socket
    timeout of 30 s set in `finish_request`, `address_family` IPv6 when the host has a colon.
  - `_Handler`: `protocol_version = "HTTP/1.1"`, `timeout = 30`, quiet `log_message`
    (`NW_SERVE_LOG` turns it on, as in `pull_jobs`). Every response carries
    `X-Content-Type-Options: nosniff`, `Cache-Control: no-cache` and `Content-Length`.
    A request line, a header block or a `Content-Length` above 0 on a `GET` is refused by the
    stdlib limits or by an explicit `413` and closes the connection.
  - Request flow, in this order: (1) `Host`/`Origin` check (`403`); (2) authentication (`403`
    whois, `401` token); (3) method (`405` with `Allow: GET` for anything but `GET`; this is
    refusal-audited too); (4) route.
  - Routes: `GET /api/snapshot` as in Decision 6; every other path, including `/`, is `404
    {"error": "not found"}` after authentication. `cache.get()` raising returns `503 {"error":
    "<ExceptionType>: <message>"}` and the server keeps running.
  - `write_server_json(cfg, server, auth)` / `remove_server_json(cfg)`:
    `{schema: "nw.dashboard.server.v1", pid, start_ticks, started_at, host, port, url, auth:
    "token"|"whois"}`. `start_ticks` is field 22 of `/proc/<pid>/stat` (`null` where `/proc`
    is not available). The file is replaced atomically (`os.replace`) with mode 0600. `port` is
    the actual port, so `--bind 127.0.0.1:0` is discoverable.
- `cli_worker.serve_cmd` (`src/nightwright/agentic/cli_worker.py:223`): options become `--jobs`
  and `--dashboard` (both plain flags, mutually exclusive, one required), `--bind` default `None`.
  With `--dashboard`, `--tls-cert`, `--tls-key` and `--client-ca` are a usage error after the bind
  gate has run. Dashboard path: `make_server` → on `ValueError` raise `click.UsageError` (exit 2);
  when `--bind tailscale` finds no tailnet address, raise `click.ClickException` (exit 1) so a
  supervisor can retry; `write_server_json`; print `nw serve: dashboard on http://HOST:PORT
  (token auth, token in <path>)` or `(tailscale whois auth)` to stderr, never the token itself;
  SIGTERM and SIGINT both stop `serve_forever`, then `remove_server_json` and `server_close`,
  exit 0. The `--jobs` path is unchanged in behavior (same default bind, same messages, same
  exit codes).
- `docs/product/reference/index.md`: the `serve` row gains `serve --dashboard [--bind
  tailscale[:PORT]|ADDR:PORT]`.

### Fake `tailscale` fixture

`tests/fixtures/fake_tailscale.py` exposes `install(tmp_path, scenario) -> Path` (a bin dir to
put first on `PATH`) and `env(...)`. The installed `tailscale` is a small script with the
test interpreter's shebang that reads the JSON scenario named by `FAKE_TAILSCALE_SCENARIO`:

```
{ "ip": ["100.64.0.5", "fd7a:115c:a1e0::5"] | null,         // null: exit 1 (logged out)
  "status": {"Self": {"UserID": 11, "DNSName": "host.tail1234.ts.net.", "TailscaleIPs": [...]},
             "User": {"11": {"LoginName": "owner@example.com"}}},
  "whois": {"100.64.0.7": {"Node": {"ComputedName": "phone", "Name": "phone.tail1234.ts.net.",
                                    "Tags": null},
                           "UserProfile": {"ID": 11, "LoginName": "owner@example.com"}},
            "100.64.0.8": {"exit": 1, "stderr": "no match"},
            "default": {"exit": 1}} }
```

Every invocation appends its argv to `FAKE_TAILSCALE_LOG`, so tests count `whois` calls (cache
tests) and prove that no `tailscale` ran for a refused-before-auth request.

## Alternatives considered

- **Reuse `pull_jobs.check_bind`.** It accepts any `100.64.0.0/10` address, so a typo or a
  config for another node would silently serve on an address that is not this host's, and it
  lets TLS lift a wildcard refusal. Both are against the epic's bind gate.
- **`tailscale serve` identity headers.** Need a loopback bind behind a proxy, where any local
  process can forge `Tailscale-User-Login` (rejected in the epic).
- **A shared secret for tailnet binds too.** Whois already gives a per-device identity that the
  audit log can name, and a token typed on a phone is worse than the tailnet's own login.
- **Auditing successful reads.** One line per poll per client grows without bound and records
  nothing that is not already in the snapshot. Refusals are the security signal.
- **A `--peer` test flag on the CLI.** It would be a spoofing seam in production. The hook is a
  Python keyword argument reachable only in-process.
- **Caching whois for the whole session.** A revoked share or a login change would stay valid
  for hours. 60 s bounds it.
- **Serving a placeholder page now.** The page spec owns `GET /`; a placeholder here would need
  its own CSP and tests.

## Acceptance criteria

- [ ] `nw serve --dashboard --bind 127.0.0.1:0` starts, writes `server.json` with the real port
      and `auth: "token"`, and `GET /api/snapshot` with the right bearer token returns `200`, a
      JSON document with `schema == "nw.dashboard.v1"` and every top-level key SPEC-0401 defines,
      and `ETag` equal to `"<rev>"` of that document.
- [ ] A repeat request with `If-None-Match: "<rev>"` returns `304` with an empty body and the
      same `ETag`; a stale value returns `200`; `*` and `W/"<rev>"` return `304`. Two requests with
      no queue change cause exactly one JSON serialization (counter) and, within `refresh_s`, one
      snapshot build.
- [ ] `nw serve --dashboard` refuses (exit 2, message naming the rule, nothing listening, no
      `server.json`) on `0.0.0.0`, `::`, `[::]`, a LAN address (`192.168.1.20`), a tailnet address
      that is not in this host's `tailscale ip` output, a host name, and each of these combined
      with `--tls-cert/--tls-key`. A valid loopback bind combined with TLS flags is also refused.
- [ ] With `--bind tailscale` and the fake `tailscale ip` printing `100.64.0.5`, the server binds
      that address (a bind to an address the test host does not own fails with the OS error and
      exit 1, not a refusal). With `tailscale ip` failing, it exits 1 and says tailscale is not up.
      With an explicit address equal to an own address, `check_bind` accepts it.
- [ ] `nw serve` with neither `--jobs` nor `--dashboard`, or with both, exits 2. `nw serve --jobs`
      keeps its default bind `127.0.0.1:8790` and its existing behavior (the SPEC-0334 tests pass
      unchanged).
- [ ] On a loopback bind, a request with no `Authorization`, a wrong token, a non-`Bearer` scheme
      or the token in the query string gets `401`, and `local.token` exists with mode 0600 in a
      0700 directory. The token value never appears in the audit log, `server.json` or stderr.
- [ ] On a whois-authenticated server, a peer whose login is allowed gets `200`; an unlisted
      login, a tagged node, a whois non-zero exit, a whois timeout and malformed whois JSON each
      get `403`; with `allowed_logins` empty only the host's own login passes; with it set, only
      the listed logins pass (case-insensitively). Repeated requests from one peer within 60 s run
      `tailscale whois` once; a failure is re-checked after 5 s, not on every request.
- [ ] A request with a wrong `Host`, a foreign `Origin` (`http://evil.example`), or a `Host` equal
      to a DNS-rebinding name is `403` before any authentication or `tailscale` call; the matching
      `Host`/`Origin` (and `localhost:port` on loopback) passes.
- [ ] `POST`, `PUT`, `DELETE` and `OPTIONS` to any path get `405` with `Allow: GET` and write
      nothing but an audit line; `GET /` and `GET /nope` get `404`.
- [ ] Every refusal appends one valid `nw.dashboard.audit.v1` line with the `reason`, `status`,
      method, path (no query), and for whois refusals the resolved login/node/IP; successful reads
      append none; `audit.jsonl` rotates to `audit.jsonl.1` past 5 MiB and never exceeds one backup.
- [ ] Over HTTP, after a mix of authenticated reads, conditional reads, refusals and a snapshot
      rebuild, every file under the data dir outside `agentic/dashboard/` is byte-identical and
      no file other than `local.token`, `audit.jsonl`, `audit.jsonl.1` and `server.json` exists
      under `agentic/dashboard/`.
- [ ] On SIGTERM the process exits 0 and `server.json` is gone; a `server.json` naming another
      pid is left alone. A server left over from a killed process does not stop a new one from
      starting and replacing `server.json`.
- [ ] No queue-side module (`queue`, the runner, `runproc`, `events`, `escalations`, `lease`,
      `store`, `mailbox`, `pull_jobs`) imports `nightwright.agentic.dashboard`, and importing
      `nightwright.agentic.dashboard.snapshot` (so `nw dashboard snapshot`) does not import
      `auth` or `server`.
- [ ] The fake `tailscale` scenario file reproduces all of the above without a tailnet, and the
      real-binary manual checks of the Test plan were run on this host and recorded.
- [ ] `docs/product/reference/index.md` lists `serve --dashboard`; `python3 tools/spec_lint.py`
      exits 0.

## Test plan

**Automated tests.** Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps. Requests go
over real sockets (`urllib` / `http.client`). CLI cases run the venv's `nw` entry point as a real
subprocess (as `tests/test_dashboard_cli.py` does) with `NW_DATA_DIR` set to the
`tests/fixtures/dashboard_data.py` seed and `PATH` set by `fake_tailscale.install`.

- `tests/fixtures/fake_tailscale.py`: the scenario-driven fake described in Design, with a call
  log. Its own test (`tests/test_dashboard_fake_tailscale.py`) pins each subcommand's output and
  exit code so a fixture bug cannot hide a server bug.
- `tests/test_dashboard_auth.py` (unit, no sockets):
  - `parse_bind`: `tailscale`, `tailscale:9000`, `127.0.0.1:0`, `[::1]:8791`, `localhost:8791`,
    garbage → `ValueError`;
  - `check_bind` table: loopback v4/v6 ok, wildcards refused (v4, v6, `[::]`), LAN refused, own
    tailnet IP ok, foreign tailnet IP refused, IPv4-mapped IPv6 unmapped, host name refused,
    each refusal message contains its rule word (`wildcard`, `own tailnet`, `not an IP`);
  - `own_tailnet_ips` with the fake: two addresses, logged out → `[]`, junk output → `[]`;
  - `Gate` with an injected `run` and clock: allowed login, unlisted, tagged (`Tags` set), exit 1,
    timeout (`None`), bad JSON, missing `UserProfile`, own-login resolution through `status`,
    own login unknown → deny, case-insensitive logins, 60 s positive and 5 s negative cache
    (call counter), a peer string with a leading `-` or non-IP text is denied without running
    `tailscale`;
  - `local_token`: created 0600 in a 0700 dir, reused, loosened mode is repaired, empty file
    regenerated;
  - `check_host_origin`: bound address, `localhost`, MagicDNS name, foreign host, foreign origin,
    missing `Host`, `Origin: null`.
- `tests/test_dashboard_audit.py` (unit): schema keys, no query string, truncation, no secret
  fields, `0600`, rotation at the cap with one backup, a read-only directory does not raise.
- `tests/test_dashboard_server.py` (real server in a thread on `127.0.0.1:0`, the seeded data
  dir; a second class drives whois mode on loopback through `peer_addr` and the fake):
  - snapshot `200` with schema keys and `ETag == '"' + doc["rev"] + '"'`; `If-None-Match` hit,
    miss, `*`, `W/`, and a list of tags; the body bytes are identical for two requests of one
    `rev`; a ledger-only change (cost change appended to the active campaign's `ledger.jsonl`)
    followed by a clock step past `refresh_s` (injected cache clock) gives a new ETag;
  - auth matrix on loopback (no header, wrong token, wrong scheme, query token) → `401`; whois
    matrix (allowed, unlisted, tagged, exit 1, timeout, bad JSON) → `200` / `403`; `tailscale`
    call counts from the fake's log (one whois per peer within the TTL, none for bad-`Host`
    requests);
  - `Host`/`Origin` matrix, evaluated before auth (a wrong `Host` with a valid token is `403`);
  - methods: `POST/PUT/DELETE/OPTIONS` → `405` + `Allow: GET`; `GET /` and `GET /nope` → `404`;
    a `GET` with a body → `413`; a builder that raises → `503` and the next request works;
  - an idle connection is closed after the handler timeout (the module constant is patched to
    0.3 s);
  - audit lines for each refusal reason, with login/node populated for whois refusals; none for
    `200`/`304`.
- `tests/test_dashboard_serve_cli.py` (real subprocess):
  - `--bind 0.0.0.0:0`, `[::]:0`, `192.168.1.20:0`, `100.64.0.9:0` (fake `ip` prints
    `100.64.0.5`), `example.com:8791`, each alone and with `--tls-cert/--tls-key` → exit 2, the
    rule in stderr, no `server.json`, and a connect to the port fails;
  - `--bind 127.0.0.1:0` plus TLS flags → exit 2;
  - `--bind tailscale` with the fake logged out → exit 1, `tailscale is not up` in stderr;
  - neither mode flag, both mode flags → exit 2; `--jobs` default bind unchanged (reads `--help`
    and the existing SPEC-0334 serve tests run unchanged);
  - `--bind 127.0.0.1:0`: poll `server.json` (via `tests/waits.py`) for the port; `GET
    /api/snapshot` with the token read from `local.token` → `200`, no token → `401`; the token
    value is absent from stderr, `server.json` and `audit.jsonl`; `SIGTERM` → exit 0 and
    `server.json` removed;
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
- Regression guard: the SPEC-0334 `nw serve --jobs` tests, the SPEC-0401 dashboard tests
  (`test_dashboard_snapshot`, `_cache`, `_cli`, `_readonly`, `_imports`) and
  `python3 tools/affected_tests.py --run uv run pytest -q` run unchanged.

**Manual verification (on this host, recorded in this spec's As shipped).** Per the repository's
rule that fakes are not verification of an external-CLI integration, one real call of each
`tailscale` subcommand the server relies on is made before the integration is claimed to work.
1. Run the real `tailscale ip`, `tailscale status --json` and (when a peer exists) `tailscale
   whois --json <peer-ip>`. This host is logged out today: record the real exit codes and stderr
   and confirm `own_tailnet_ips()` returns `[]` and `nw serve --dashboard` exits 1 ("tailscale is
   not up") and `nw serve --dashboard --bind 100.64.0.9:0` exits 2 against the real binary.
2. Once the human has logged in (epic Rollout, human steps): diff the key paths the code reads
   (`Self.UserID`, `Self.DNSName`, `User.<id>.LoginName`, `UserProfile.LoginName`,
   `Node.ComputedName`, `Node.Name`, `Node.Tags`) against the real JSON, fix the fixture if it
   differs, and run `nw serve --dashboard` on the real tailnet address. From the phone (cellular,
   Tailscale app on) `http://<tailnet-ip>:8791/api/snapshot` returns the JSON; from a device on
   another login it returns `403`; `audit.jsonl` names the refusal. If `tailscale whois` needs
   root, the code fails closed (`403`) and the Rollout's `tailscale set --operator=$USER` is the
   fix. This step is the human's real-tailnet check and the only one that waits on the login.
3. `uv run nw serve --dashboard --bind 127.0.0.1:0`; with the token from `local.token`, `curl -si
   -H "Authorization: Bearer $(cat <data_dir>/agentic/dashboard/local.token)"
   http://127.0.0.1:<port>/api/snapshot` shows `200` and an `ETag`; repeating with
   `-H 'If-None-Match: "<rev>"'` shows `304`; `curl -si http://127.0.0.1:<port>/api/snapshot`
   shows `401`. `find <data_dir> -newer <marker>` lists nothing outside `agentic/dashboard/`
   except what a live runner or follower touched.

**Regression guard.** Affected tests via `python3 tools/affected_tests.py --run uv run pytest
-q`. The queue's close gate runs the affected set.

## Rollout / migration

1. Land `dashboard/auth.py`, `audit.py`, `server.py`, the `serve_cmd` mode switch, the fake
   `tailscale` fixture, the docs row and the tests in one change. No data migration:
   `agentic/dashboard/` is created on first start.
2. Until the human logs in to Tailscale, the server runs only on loopback with the local token.
   Once they do (epic Rollout, step 1), `nw serve --dashboard` serves the tailnet address.
3. The page spec then adds `GET /` and the static assets behind the same gates. Epic children 2,
   3 and 4 build on this server; child 2 adds the write routes and the CSRF/plan-token layer to
   the same audit file.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (affected set via `tools/affected_tests.py`);
      manual steps 1 and 3 performed and noted (step 2 recorded when the human has logged in).
- [ ] No regressions (`nw serve --jobs`, SPEC-0401 dashboard tests green).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None. Whether `tailscale whois` works without root on this host's tailscaled setup can only be
checked after the human logs in; the server fails closed (403) until then and manual step 2
records the result._
