---
id: SPEC-0417
title: "Dashboard tailnet identity gate: tailscale whois Verdict/Gate, whois auth mode and tailnet default bind"
status: proposed
owner: user
created: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-918
tags: [agentic, dashboard, remote, web, auth, tailscale]
depends_on: [SPEC-0416]
---

## Context

This is the second slice of the tailnet half of child 1 of the epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md), the sibling that
[SPEC-0416](./0416-dashboard-tailnet-slice-spec-0400-child-follows.md) (done) announced in its
Non-goals and Rollout step 3. SPEC-0416 built the *bind gate*: this host's own tailnet addresses
are recognised, `--bind tailscale` resolves, `TailnetMissing` is exit 3. It deliberately ends in
one refusal, in `auth.resolve_bind`: an accepted own tailnet address is refused with "tailnet
serving needs the identity gate". This spec is the *identity gate* that makes that refusal
removable, and removes it in the same change, so the server is never reachable on the tailnet
without the gate. The epic decision (Decision 2) is: tailnet peers authenticate with
`tailscale whois --json <peer-ip>`; the loopback bearer token stays for loopback.

Two later epic children need this spec: SPEC-0402 (decisions inbox with authenticated answers)
has AC6 that a request from a node that is not allowed, from a tagged node or while whois fails
gets 403 and a node-named audit line; SPEC-0404 (lifecycle: systemd unit, `nw dashboard status`)
has AC3 that a unit started with no `--bind` exits 3 when the tailnet is missing, which needs the
tailnet to be the default bind.

What exists today (checked against the code on 2026-11-04):

- `src/nightwright/agentic/dashboard/auth.py`: `resolve_bind` (the refusal at the end),
  `check_bind(host, own)`, `own_tailnet_ips`, `run_tailscale(args, timeout=5.0)` (argv list, no
  shell; `TailscaleError`), `is_tailnet_ip`, `local_token(cfg)`, `host_values(bound_host, port)`
  (the bound address and `localhost:port`) and `check_host_origin`.
- `src/nightwright/agentic/dashboard/server.py`: `make_server(cfg, bind=None, *, cache=None,
  own=None)`; `DashboardServer(cfg, addr, cache, token)`; `_Handler._handle` runs Host/Origin
  (403), then the bearer token (401), then 405 for non-GET, then `/api/snapshot`; `_refuse` writes
  the audit line; `write_server_json` writes `"auth": "token"`; `DEFAULT_BIND` is the loopback
  `127.0.0.1:8791`.
- `src/nightwright/agentic/dashboard/audit.py`: `audit.jsonl` lines `nw.dashboard.audit.v1`;
  `FIELDS` already has `login`, `node`, `node_ip` (nothing fills them yet) and the `action` is
  hard-coded to `"refused"`.
- `src/nightwright/agentic/dashboard/__init__.py`: `settings(cfg)` returns `bind`
  (`tailscale:8791`) and `allowed_logins` (`[]`), which nothing reads yet.
- `src/nightwright/agentic/cli_worker.py` `_serve_dashboard` resolves `bind or DS.DEFAULT_BIND`
  (loopback) and prints `(token auth, token in ...)`.
- `tests/fixtures/fake_tailscale.py` models only `tailscale ip` and rejects every other
  subcommand (so a test fails if the server ever ran `status` or `whois`).
- `tailscale` 1.102.4 is installed on this host and **logged out**: the real binary's failure
  paths (`whois`, `status --json` with no `Self`) can be checked today; a successful whois needs
  the human's login (epic Rollout step 1, `sudo tailscale up` and `sudo tailscale set
  --operator=$USER`).

Nothing in this spec adds writes, CSRF or plan tokens (epic child 2), TLS or push (child 5), the
systemd unit or `nw dashboard status` (SPEC-0404). Because the server is still read-only
(`GET /api/snapshot`), the gate is the whole of the tailnet's protection and is pinned by tests
that fail when a rule is removed.

## Goals / Non-goals

**Goals**
- G1. On a tailnet bind every request, whatever its method or path, is authenticated by
  `tailscale whois --json <peer-ip>` and refused with 403 unless the peer's login is allowed.
  The gate fails closed: any whois error, unreadable output, tagged node or unknown own login
  denies.
- G2. `agentic.dashboard.allowed_logins` is honoured; empty (the default) means the host's own
  login only.
- G3. Verdicts are cached per peer IP (60 s allowed, 5 s denied) so a polling phone costs
  almost no `tailscale` calls and a denied peer cannot make the server spawn one per request.
- G4. Token mode and whois mode are exclusive. Loopback keeps the bearer token and never runs
  whois; a tailnet bind uses whois only, creates and reads no `local.token`, and a bearer token
  grants nothing there.
- G5. Host/Origin checks accept the host's MagicDNS name on a tailnet bind (so the phone can use
  `http://<host>.<tailnet>.ts.net:8791`) and nothing else beyond the bound address.
- G6. Audit lines carry the identity (`login`, `node`, `node_ip`); `server.json` says
  `auth: "whois"` on a tailnet bind.
- G7. The own-tailnet-address refusal in `resolve_bind` is lifted and the CLI default bind
  becomes `settings(cfg)["bind"]` (`tailscale:8791`).
- G8. All of it is provable in CI with a fake `tailscale` and injected seams, plus one
  real-binary check.

**Non-goals**
- Writes, CSRF, plan tokens, the inbox payload (SPEC-0400 child 2), TLS, `tailscale cert`,
  MagicDNS HTTPS, push (child 5). `--tls-cert/--tls-key/--client-ca` with `--dashboard` stay
  refused (exit 2), after the bind gate.
- The systemd unit, `nw dashboard status`, the `conduct-status` `dashboard` key and a whois
  self-check command (SPEC-0404).
- A hot reload of `allowed_logins` or of the own login: both are read when the server starts
  (the own login also refreshed every 60 s as below); a changed config needs a restart, which
  the unit gives.
- Roles or per-device permissions: one human, their own logins. Any change to `snapshot.py`,
  `queue.dashboard_payload`, `pull_jobs.py` or the `nw serve --jobs` mode; the queue still never
  imports the dashboard.
- The real-phone check (epic manual checks; needs the human's login and a phone). It is listed
  under Manual verification and recorded as pending until then.

## Decision

1. **One new decision point: a `Gate` that returns a `Verdict`.** `auth.Gate(run=run_tailscale,
   allowed_logins=(), clock=time.monotonic)` has one method, `check(peer_ip) -> Verdict`.
   `Verdict` is a frozen dataclass: `allowed: bool`, `reason: str`, `login`, `node`, `node_ip`
   (strings or None), `fresh: bool` (true when this call ran `tailscale`, false when it came from
   the cache). `check` never raises: every exception becomes a denied verdict with reason
   `gate_error`.
2. **The whois form and its input.** `Gate` runs exactly `tailscale whois --json <ip>` (argv
   list, no shell, the 5 s `run_tailscale` timeout). `<ip>` is the peer address from the socket,
   IPv4-mapped IPv6 unmapped, re-rendered by `ipaddress` (never a header, never request text),
   and only if `is_tailnet_ip(ip)`; any other peer is denied `not_tailnet_peer` without running
   `tailscale`. The result is parsed from JSON; the verdict is:
   - the command fails, times out or prints non-JSON / a non-object → denied `whois_failed`;
   - `Node.Tags` is a non-empty list, or `UserProfile.LoginName` is missing, empty or
     `tagged-devices` → denied `tagged_node` (a node with no user cannot be an allowed login);
   - the login (casefolded, stripped) is not in the allowed set → denied `login_not_allowed`;
   - otherwise allowed, reason `ok`. `login` is `UserProfile.LoginName`; `node` is
     `Node.ComputedName`, else `Node.Name` without a trailing dot; `node_ip` is the peer.
3. **The allowed set.** `agentic.dashboard.allowed_logins` (`settings(cfg)`), casefolded, blanks
   dropped. If the result is empty, the set is the host's **own login only**, from
   `tailscale status --json`: `User[str(Self.UserID)].LoginName`, with `Self` having no
   `Tags` and a user id. If the own login cannot be determined (the command fails, no `Self`, a
   tagged host, no matching `User`), every request is denied `self_login_unknown`. An explicit
   non-empty list is exactly the set; the own login is not added to it. The own login is read
   lazily and re-read at most every 60 s (it is only needed while `allowed_logins` is empty).
4. **Cache.** Per peer IP: an allowed verdict for 60 s, any denied verdict (including
   `whois_failed`, `tagged_node`, `self_login_unknown`) for 5 s. At most 512 entries (expired
   first, then the oldest evicted). Concurrent requests from one peer share one `tailscale`
   call (a per-peer lock around the miss path; the cache lock is not held while `tailscale`
   runs). `clock` is injectable.
5. **Auth modes are exclusive and derived from the bind, not chosen.** `auth.bind_mode(host)` is
   `"token"` for a loopback address and `"whois"` for every other address `resolve_bind`
   accepts (in practice only this host's own tailnet address). `resolve_bind` stops refusing an
   accepted own tailnet address. In whois mode the server never calls `local_token`, never
   creates `local.token`, and an `Authorization` header is ignored (it neither helps nor
   hurts). In token mode no `Gate` exists, `whois` and `status` are never run, and SPEC-0414's
   behaviour is unchanged.
6. **The handler order.** `_handle` keeps its drain of the body, then: Host/Origin (403
   `bad_host`/`bad_origin`) → in token mode the token (401) → in whois mode the gate (403, JSON
   body `{"error": "forbidden"}` with no reason, identity or version in it) → 405 for non-GET →
   413 → routing. The gate runs before routing, so a 404 and any future static asset on a tailnet
   bind are gated too.
7. **MagicDNS in Host/Origin, in whois mode only.** `host_values(bound_host, port,
   magic_name=None, loopback=True)` adds `<magic_name>:port` (lowercased, trailing dot removed)
   when given, and `localhost:port` only when `loopback` is true. The server resolves the name
   once at start from `tailscale status --json` `Self.DNSName` (through the `Gate`'s memoised
   status read); if it cannot, it serves with the IP form only. Whois mode passes
   `loopback=False`: the tailnet socket is not reachable as `localhost`.
8. **Audit.** The refused-request audit line gets `login`, `node` and `node_ip` whenever the
   gate got far enough to know them (`login_not_allowed` and `tagged_node` know the login/node;
   `whois_failed` knows only the `node_ip`). `audit.record` accepts an `action` field
   (`refused`, the default, or `allowed`). In whois mode a *fresh* allowed verdict writes one
   `allowed` line (`reason: "ok"`, status 200, identity fields): one line per peer per 60 s
   window, not one per poll. No line ever contains a token, header value other than
   `User-Agent`, the raw whois output or the query string.
9. **`server.json` and the start line.** `auth` is `"whois"` on a tailnet bind (`"token"` on
   loopback, as today). The start line is `nw serve: dashboard on <url> (tailnet identity auth,
   allowed: <logins or "own login">)`; in token mode it is unchanged.
10. **The CLI default is the configured bind.** With no `--bind`, `nw serve --dashboard` and
    `make_server(cfg)` use `settings(cfg)["bind"]` (default `tailscale:8791`). `--bind` still
    wins. A host with no tailnet therefore exits **3** with no flags (SPEC-0404 AC3); a user who
    wants loopback passes `--bind 127.0.0.1:8791` or sets `agentic.dashboard.bind`. The
    `DEFAULT_BIND` constant in `server.py` is removed (the `settings` default replaces it).
11. **Test seams are keyword-only and unreachable from outside.** `make_server(cfg, bind=None,
    *, cache=None, own=None, gate=None, peer_addr=None, auth=None)`: `gate` replaces the `Gate`
    built from the config; `peer_addr` is a callable `handler -> str` replacing the socket's
    peer address; `auth` (`"whois"` or `"token"`) overrides `bind_mode` so a test can run whois
    mode on a loopback socket (real tailnet addresses cannot be bound in CI). `auth="token"`
    with a non-loopback bind raises `ValueError`: a seam can add strictness, never remove it.
    `serve_cmd` / `_serve_dashboard` pass none of `own`, `gate`, `peer_addr`, `auth`, and no flag,
    environment variable or config key reaches them.
12. **The `tailscale` forms the server may run are exactly three:** `ip` (SPEC-0416), `status
    --json` and `whois --json <peer-ip>`, all read-only argv lists with the 5 s timeout and no
    shell. Never `up`, `set`, `serve`, `cert`, `login`, `ping` or `file`.

## Design

### `auth.py`

- `Verdict` (frozen dataclass) and `Gate` as in Decisions 1-4. Private helpers:
  `_whois(ip) -> Verdict`, `_self_info() -> (login | None, dns_name | None)` memoised 60 s
  (a failed read is not memoised longer than the 5 s denied TTL), `_allowed_set()`.
  `Gate.magic_name()` returns the memoised `Self.DNSName` (`None` when unknown).
  The peer is normalised by `_peer_ip(addr) -> str | None` (strips an IPv6 zone `%...`,
  unmaps IPv4-mapped, `None` when it is not an IP).
- `bind_mode(host) -> str` (Decision 5).
- `resolve_bind`: the final `if not loopback: raise BindRefused(...)` block and its comment are
  deleted; the function returns `(host, port)` for an accepted own tailnet address. Nothing
  else about it changes (`own_fn` call counts, `TailnetMissing`, rule words).
- `host_values(bound_host, port, magic_name=None, loopback=True)` (Decision 7).
  `check_host_origin` is unchanged (it already takes the `allowed` set).
- `local_token` is unchanged and simply not called in whois mode.
- The module still imports nothing from the queue; `snapshot.py` does not import `auth`.

### `server.py`

- `DashboardServer(cfg, addr, cache, token, *, auth, gate=None, peer_addr=None, hosts=None)`
  stores `auth` (`"token"`/`"whois"`) and `gate`; `token` is `None` in whois mode.
  `make_server` computes `mode = auth or A.bind_mode(host)`, builds the gate from
  `settings(cfg)["allowed_logins"]` in whois mode (or uses `gate`), resolves the MagicDNS name,
  calls `local_token(cfg)` only in token mode, and passes the `hosts` set.
- `_Handler._peer()` is `peer_addr(self)` if injected, else `self.client_address[0]`.
- `_Handler._gate()` runs `gate.check(peer)` and returns `None` when allowed (writing the
  `allowed` audit line when `verdict.fresh`), else refuses 403 through `_refuse` with the
  identity fields. `_refuse` takes optional `login=`, `node=`, `node_ip=`.
- `write_server_json` writes `"auth": self.auth`. `_token_ok` is only reached in token mode.
- `DEFAULT_BIND` removed; `make_server` resolves `bind or settings(cfg)["bind"]`.

### `audit.py`

`record` takes `action` from the fields (`refused` | `allowed`, anything else becomes
`refused`); `FIELDS` is unchanged. Nothing else changes.

### `cli_worker._serve_dashboard`

`bind or settings(cfg)["bind"]` replaces `bind or DS.DEFAULT_BIND`; the exit mapping (2 for
`BindRefused` and TLS flags, 3 for `TailnetMissing`, 1 for an `OSError`) and the check order are
unchanged: resolution, then TLS refusal, then bind. The start line follows Decision 9 using
`srv.auth`. The `--dashboard` and `--bind` help text are updated (default `tailscale:8791`,
tailnet identity auth).

### Fake `tailscale` fixture

`tests/fixtures/fake_tailscale.py` gains `status --json` and `whois --json <ip>` (still
rejecting every other subcommand with exit 2). The scenario adds:
`self: {"user_id": 7, "login": "me@example.com", "dns_name": "host.tail1234.ts.net.", "tags": []}`
and `peers: {"100.64.0.20": {"login": "me@example.com", "name": "phone.tail1234.ts.net.",
"computed_name": "phone", "tags": []}}`; `status --json` prints `Self` and `User` in the real
shape (`Self.UserID`, `Self.DNSName`, `Self.Tags`, `User{"<id>": {"LoginName": ...}}`),
`whois --json` prints `Node` (`ComputedName`, `Name`, `Tags`, `Addresses`) and `UserProfile`
(`LoginName`) for a known peer and exits 1 for an unknown one. `logged_out` makes `status
--json` print `{"BackendState": "NeedsLogin"}` with no `Self` (exit 0, as the real binary
does) and `whois` exit 1. `$FAKE_TAILSCALE_MODE` (`ok`/`fail`/`hang`/`garbage`) applies to every
subcommand and the scenario key `modes: {"whois": "fail"}` overrides it per subcommand. Every
call is still logged as one JSON argv line.

### Docs

`docs/product/reference/index.md` (the `serve` row): default bind is `tailscale:8791`; tailnet
binds use `tailscale whois` identity auth (own login by default, `agentic.dashboard.allowed_logins`
to widen), loopback keeps the bearer token; the three `tailscale` forms; `server.json`
`auth: "whois"`; exit codes unchanged (2, 3, 1); the MagicDNS name works in the browser.
The "serving on the tailnet is still refused" sentence is removed.

## Alternatives considered

- **`tailscale serve` identity headers (`Tailscale-User-Login`).** Needs a loopback bind behind
  a proxy; any local process could forge the headers. Rejected in the epic.
- **Keep the bearer token on the tailnet as well.** Two ways in, and the phone would have to
  carry a secret; the tailnet already authenticates the peer. Exclusive modes mean no bearer
  token is a way in over the tailnet.
- **Query the LocalAPI over its Unix socket instead of the CLI.** Avoids a subprocess but adds a
  protocol client; the cache makes the CLI cost negligible and the fake-binary test is simple.
- **Allowed = own login plus the list.** An explicit list that silently also admits the own
  login is surprising; an explicit list is exactly the list.
- **Cache denials for 60 s as well.** A freshly fixed whois failure (for example after `tailscale
  set --operator`) would keep the phone locked out for a minute; 5 s still bounds the spawn rate.
- **Audit every allowed request.** A phone polling every 5 s would write ~17k lines a day; one
  line per peer per 60 s window keeps the "who looked" record readable.
- **A `--auth` flag to choose the mode.** A flag could turn the gate off on a tailnet socket;
  the mode is derived from the bind and the only override is a test seam that cannot weaken it.
- **Fail open when `tailscale` is unavailable once the server is up.** Violates the epic's
  fail-closed rule; the server keeps running and answers 403 until whois works again.

## Acceptance criteria

- [ ] `Gate.check` table (fake `run`, no sockets): an allowed login → `Verdict(allowed=True,
      reason="ok")` with `login`, `node` (`ComputedName`, else `Name` without the trailing dot)
      and `node_ip`; another login → `login_not_allowed`; non-empty `Node.Tags` or a missing,
      empty or `tagged-devices` login → `tagged_node`; a failing, hanging (`TailscaleError`),
      non-JSON, non-object or exception-raising `run` → `whois_failed` / `gate_error`, never an
      exception; a non-tailnet peer (`127.0.0.1`, `192.168.1.5`, `8.8.8.8`, garbage) →
      `not_tailnet_peer` with `run` never called; an IPv4-mapped tailnet peer is unmapped
      before whois; login comparison is case-insensitive.
- [ ] Allowed set: empty `allowed_logins` → the own login from `status --json` (`Self.UserID` →
      `User`); a tagged host, no `Self` (logged out), a failing `status` or a missing `User`
      entry → every request denied `self_login_unknown`; a non-empty list is exactly the set
      (the own login is refused if it is not in it); a list of blanks behaves as empty.
- [ ] Cache (injected clock): a second request for one peer within 60 s runs no second
      `tailscale` call (`fresh` is false); at 60 s it re-runs; a denied verdict is reused for
      4.9 s and re-run at 5 s; 20 threads asking for one peer at once cause exactly one whois
      call; the cache never holds more than 512 peers; a login that stops being allowed is denied
      once its entry expires.
- [ ] Whois mode over real sockets (`make_server(..., auth="whois", gate=..., peer_addr=...)`,
      fake `tailscale`): an allowed peer gets 200 and the snapshot; an unknown login, a tagged
      node, `whois` failing (`fail`, `hang`, `garbage`) and a logged-out own login each get 403
      with body `{"error": "forbidden"}` (no login, node or reason in it) and an audit line with
      `reason`, status 403 and the identity fields available; the snapshot builder is not called
      for any denied request; a 404 path, a non-GET method and a request with an `Authorization`
      bearer holding the (nonexistent) token are gated first (403 for a denied peer, never 401,
      404 or 405).
- [ ] Mode exclusivity: in whois mode `local.token` is not created or read (absent from the
      dashboard dir; a pre-existing one is untouched) and a valid-looking bearer token gives a
      denied peer nothing; in token mode (loopback, the default for a loopback bind) no `Gate` is
      built, the fake's log has no `status` or `whois` call over a whole request run, the bearer
      token behaves exactly as in SPEC-0414 (401 without it) and a peer address is never
      consulted. `auth="token"` with a non-loopback bind raises `ValueError`.
- [ ] `resolve_bind` returns `(own address, port)` for `tailscale`, `TAILSCALE`,
      `tailscale:9000` and an own tailnet address (the identity-gate refusal is gone, the string
      "identity gate" appears nowhere in `auth.py`); every other row of the SPEC-0416 table is
      unchanged (wildcard, LAN, public, link-local, foreign tailnet, host name → `BindRefused`;
      no tailnet → `TailnetMissing`; `own_fn` call counts); `bind_mode` is `"token"` for loopback
      and `"whois"` for a tailnet address.
- [ ] Host/Origin: `host_values("100.64.0.5", 8791, "Host.Tail1234.ts.net.", loopback=False)` is
      `{"100.64.0.5:8791", "host.tail1234.ts.net:8791"}`; the default call is still
      `{"127.0.0.1:8791", "localhost:8791"}`. In whois mode a request with `Host:
      host.tail1234.ts.net:PORT` (and `Origin: http://` the same) from an allowed peer gets 200;
      `Host: localhost:PORT`, another name, or a wrong `Origin` gets 403 `bad_host` /
      `bad_origin` **before** any whois call (the fake's log has no `whois`); if `status` fails
      the server still starts and serves the IP form.
- [ ] Audit and `server.json`: a fresh allowed verdict writes exactly one `action: "allowed"`
      line with `login`, `node`, `node_ip`, status 200; repeated requests inside 60 s write none;
      the refused lines of the previous criteria carry the identity fields; `audit.record` maps
      an unknown `action` to `refused`; no audit line contains a token, `Authorization`, the raw
      whois JSON or a query string. `server.json` has `"auth": "whois"` in whois mode and
      `"auth": "token"` on loopback.
- [ ] CLI default bind: with no `--bind` and no config, `nw serve --dashboard` resolves
      `tailscale:8791`: no tailnet (logged out, missing binary, `fail`, `hang`, `garbage`) exits
      **3** naming `tailscale up`, nothing listening, no `server.json`; with the config
      `agentic.dashboard.bind: "127.0.0.1:0"` and no flag it serves on loopback with
      `auth: "token"` and an empty fake log; `--bind` overrides the config; a malformed config
      bind (for example `0.0.0.0:8791`) exits 2 with the rule; `make_server(cfg)` follows the same
      default. All SPEC-0416 refusals still hold: wildcard / LAN / public / foreign tailnet /
      host name exit 2 alone and with TLS flags (the bind message, not the TLS one), a loopback
      bind with TLS flags exits 2 with `TLS is not supported`, and a wildcard, LAN, public or
      host-name bind runs no `tailscale`.
- [ ] A tailnet socket never exists without the gate: a tailnet address cannot be bound in CI, so
      a test stubs `DashboardServer` and asserts that `make_server` with a bind that resolves to
      an own tailnet address (via an `own` stub) constructs it only with `auth="whois"` and a
      `Gate`; `DashboardServer` raises `ValueError` for a non-loopback address with
      `auth="token"`; a real subprocess bind of the fake's own address `100.64.0.5` exits 1
      (the bind `OSError`) with no `server.json`.
- [ ] Seams: an `ast` check shows `serve_cmd` and `_serve_dashboard` pass none of `own`, `gate`,
      `peer_addr`, `auth` to `make_server`, and `inspect.signature` shows all four keyword-only;
      no `os.environ` read feeds them; the only `tailscale` argv forms over a full CLI + server
      test run (fake log) are `["ip"]`, `["status", "--json"]` and `["whois", "--json",
      "<tailnet ip>"]`, with a peer that is not a tailnet address never reaching the log.
- [ ] The fake `tailscale` self-test covers `status --json`, `whois --json` (known and unknown
      peer, tagged peer, `logged_out`, per-subcommand `modes`) and still exits 2 for `up`,
      `set`, `serve`, `cert`, `login`; the SPEC-0414 loopback guarantees and the SPEC-0416 tests
      pass **except the rows this spec deliberately changes**, which are listed in the Test
      plan; the import-graph (queue never imports the dashboard) and read-only invariants hold;
      docs list the new default bind and whois auth; `python3 tools/spec_lint.py` exits 0.
- [ ] Manual, real binary (this host, logged out): `Gate().check("100.64.0.5")` with the real
      `tailscale` returns a denied verdict (`whois_failed` or `self_login_unknown`) and
      `uv run nw serve --dashboard` with no flags exits 3. After the human logs in:
      `Gate().check(<own tailnet ip>)` is allowed with the human's login and node name,
      `uv run nw serve --dashboard` serves on `tailscale ip -4` with `server.json` `auth:
      "whois"`, and the phone loads the snapshot (and a node on a second account gets 403). Steps
      that need the login or the phone are recorded as pending in As shipped, not claimed.

## Test plan

**Automated tests.** Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps. CLI cases
run the venv's `nw` as a real subprocess with `NW_DATA_DIR` set to the
`tests/fixtures/dashboard_data.py` seed and the fake `tailscale` first on `PATH`. Whois-mode
serving is driven in-process with the seams (`auth="whois"`, `gate`, `peer_addr`) over real
loopback sockets, because a tailnet address cannot be bound in CI; a request's apparent peer is
chosen per test.

- `tests/test_fake_tailscale.py` (extended): `status --json` (own login, logged out, tagged
  self), `whois --json` (known, unknown, tagged), modes and per-subcommand `modes`, an unknown
  subcommand (`up`, `set`, `serve`, `cert`, `login`) → exit 2, argv log.
- `tests/test_dashboard_whois_gate.py` (unit, no sockets, fake `run` + injected clock): the
  `Verdict` table of criterion 1, the allowed-set table of criterion 2, the cache table of
  criterion 3 (TTL boundaries at 59.9/60 and 4.9/5 s, the 20-thread single-flight, the 512 cap),
  `_peer_ip` (IPv4-mapped, zone id, garbage), `check` never raising (a `run` that raises
  `KeyError`), and a test through the real `run_tailscale` with the fake binary (`ok`, `fail`,
  `hang`, `garbage`).
- `tests/test_dashboard_whois_server.py` (real sockets, seams): the whois-mode table of
  criterion 4 (200 for an allowed peer; 403 and no snapshot build for unknown login, tagged,
  `fail`, `hang`, `garbage`, logged-out own login; 404/405/bearer-token requests gated first),
  mode exclusivity of criterion 5 (no `local.token`; token mode runs no `status`/`whois`;
  `ValueError` for `auth="token"` on a tailnet bind), Host/Origin of criterion 7 (MagicDNS
  accepted, `localhost` and other names refused before whois, `status` failing still serves), the
  audit and `server.json` of criterion 8 (one `allowed` line per window using the injected
  clock, identity fields on refusals, `action` fallback, no secret in any line).
- `tests/test_dashboard_tailnet_auth.py` (updated): the `resolve_bind` table rows `tailscale`,
  `TAILSCALE`, `tailscale:9000`, `tailscale:0` and `100.64.0.5:0` now return the own address and
  port (they were "identity gate" refusals), the `own_fn` call counter column unchanged;
  `bind_mode` rows; `host_values` rows with `magic_name`/`loopback`.
- `tests/test_dashboard_tailnet_server.py` (updated): the rows `100.64.0.5:0`, `tailscale` and
  `TAILSCALE:9000` that expected "identity gate" move to a test that stubs `DashboardServer` and
  asserts it is built with `auth="whois"` and a `Gate` (no real tailnet bind exists in CI);
  every other refused row is unchanged.
- `tests/test_dashboard_tailnet_cli.py` (updated): the "own tailnet address → exit 2 identity
  gate, with and without TLS flags" rows are replaced. With the fake reporting own address
  `100.64.0.5` the real subprocess cannot bind it, so those two rows assert exit **1** (the bind
  `OSError`, `EADDRNOTAVAIL`) with no `server.json`, not exit 2 and not the TLS message. Added: the
  no-`--bind` rows of criterion 9 (logged out/missing/`fail`/`hang`/`garbage` → 3; config
  loopback bind serves with `auth: "token"` and an empty fake log; flag overrides config;
  malformed config bind → 2).
- `tests/test_dashboard_serve_cli.py` (updated): `test_dashboard_without_bind_binds_8791`
  (no `--bind` served on `127.0.0.1:8791`) now passes `--bind 127.0.0.1:8791` (the tailnet
  default is covered by the CLI file above); every other case already passes `--bind`.
- `tests/test_dashboard_tailnet_seams.py` (extended): the `ast` check and `inspect.signature`
  of criterion 12 for the four seams; the fake-log allow-list of `tailscale` argv forms.
- `tests/test_dashboard_audit.py` (extended): the `action` field.
- Docs: a grep of `docs/product/reference/index.md` finds `tailscale:8791` as the default,
  `whois`, `allowed_logins` and the absence of "until the identity gate lands"; `python3
  tools/spec_lint.py` exits 0.
- Regression guard: the SPEC-0414 / SPEC-0416 / SPEC-0401 dashboard tests, the SPEC-0334
  `--jobs` tests, `tests/test_dashboard_imports.py`, and `python3 tools/affected_tests.py --run
  uv run pytest -q`. The queue's close gate runs the affected set.

**Manual verification (this host; the real-binary rule).**
1. Logged out (today): `uv run nw serve --dashboard; echo $?` prints the `tailscale up` message
   and `3`; `uv run python -c "from nightwright.agentic.dashboard import auth; print(
   auth.Gate().check('100.64.0.5'))"` prints a denied verdict with the real `tailscale`
   (`whois_failed` or `self_login_unknown`); `ss -ltn` shows nothing on 8791.
2. `uv run nw serve --dashboard --bind 127.0.0.1:8791` still serves with the token and runs no
   `tailscale`; `--bind 0.0.0.0:8791` exits 2 naming the wildcard rule.
3. After the human runs `sudo tailscale up` and `sudo tailscale set --operator=$USER`: `uv run nw
   serve --dashboard` serves on `tailscale ip -4`; `curl -s http://$(tailscale ip -4):8791/
   api/snapshot` from the host is allowed (own login), `cat <data_dir>/agentic/dashboard/
   server.json` shows `"auth": "whois"`, there is no `local.token` use, and `audit.jsonl` has an
   `allowed` line naming the login and node; from the phone (same account) the snapshot loads by
   IP and by the MagicDNS name; from a node on another account (or a tagged node) the request
   gets 403 and a refused audit line. If the login or phone is not available the step stays
   pending and the shipped record says so.

## Rollout / migration

1. Land the gate, the mode split, the default-bind change, the fake and the tests together: the
   refusal in `resolve_bind` is removed in the same change that adds the gate, so no commit
   serves the tailnet without it.
2. The CLI default changes from loopback to the tailnet. Anyone who ran `nw serve --dashboard`
   with no flag and relied on `127.0.0.1:8791` now needs `--bind 127.0.0.1:8791` (or
   `agentic.dashboard.bind`); on a host with no tailnet the old command now exits 3 with a
   message naming `tailscale up` and the loopback alternative. No unit is installed yet
   (SPEC-0404), so nothing running breaks.
3. Human steps are the epic's Rollout step 1 (`tailscale up`, `set --operator`, phone login);
   until `--operator` is set whois fails and the server answers 403 (fail closed).
4. No data migration: `audit.jsonl` lines gain an `action: "allowed"` value and identity
   fields that were already in the schema; `server.json` gains `auth: "whois"`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass (`python3 tools/affected_tests.py --run
      uv run pytest -q`); manual steps performed or recorded as pending with the reason.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped (As shipped section).
- [ ] `docs/LEDGER.md` updated (status + date) by regenerating it.

## Open questions

None. The design questions are decided above (own-login default, cache TTLs, exclusive modes,
default bind). Whether `tailscale whois` works without root depends on the human's
`tailscale set --operator=$USER`; the server fails closed (403) until then.
