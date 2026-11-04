---
id: SPEC-0416
title: "Dashboard tailnet slice: own-address bind gate, tailscale whois identity gate and TailnetMissing (exit 3)"
status: proposed
owner: user
created: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-910
tags: [agentic, dashboard, remote, web, auth, tailscale]
depends_on: [SPEC-0414, SPEC-0401]
---

## Context

This is the tailnet half of child 1 of the epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md). [SPEC-0414](./0414-dashboard-server-nw-serve-dashboard-spec-0400.md)
(done) shipped the loopback server and deliberately listed this slice as its sibling in its
Non-goals: until this spec lands, `nw serve --dashboard` refuses `tailscale`, tailnet addresses and
every other non-loopback bind, so the dashboard cannot yet be reached from the phone. The human's
decision (epic Decision 2) is **Tailscale only, nothing on the public internet**. This spec is the
part that makes the server reachable over the tailnet and is therefore the security-critical
half: a defect in the bind gate or the identity gate exposes the queue's state, so every rule
below is fail closed and pinned by a test that fails when the rule is removed.

SPEC-0404 (epic child 4, the lifecycle spec: systemd unit, `nw dashboard status`) has an
acceptance criterion that the server **exits 3 when this host has no tailnet address**, so a unit
can tell "tailnet not up yet" from a crash. That exit code and the exception behind it are defined
here, which is why SPEC-0404's AC3 depends on this spec.

What exists today (checked against the code on 2026-11-04):

- `src/nightwright/agentic/dashboard/auth.py`: `BindRefused(ValueError)`, `parse_bind` (a host
  that is not an IP, e.g. `tailscale`, is returned as given), `check_bind(host)` (loopback only;
  wildcard, non-IP and everything else refused), `validate_bind(spec)`, `local_token(cfg)`,
  `host_values(bound_host, port)` (bound address and `localhost:port`) and `check_host_origin`.
- `src/nightwright/agentic/dashboard/server.py`: `make_server(cfg, bind=None, *, cache=None)`
  calls `validate_bind` itself, loads `local_token`, and builds `DashboardServer`. `_Handler._handle`
  runs Host/Origin (`403`), then the bearer token (`401`), then the method (`405`), then the route.
  `write_server_json` writes `auth: "token"`.
- `src/nightwright/agentic/dashboard/audit.py` already has the `login`, `node` and `node_ip`
  fields (always `null` so far) so the schema does not change.
- `src/nightwright/agentic/dashboard/__init__.py`: `settings(cfg)` already returns
  `bind: "tailscale:8791"` and `allowed_logins: []` (SPEC-0401); nothing reads them yet.
- `src/nightwright/agentic/cli_worker.py:252` `_serve_dashboard`: bind gate, then the TLS refusal,
  then `make_server`; `BindRefused` is exit 2, an `OSError` is exit 1; the default bind is the
  loopback `127.0.0.1:8791`.
- `tailscale` 1.102.4 is installed on this host and is **logged out** (`tailscale ip` prints `no
  current Tailscale IPs; state: NeedsLogin`). A real-binary check of the failure path is
  therefore possible today; the success path needs the human to log in (epic Rollout step 1).
- The Test plan of SPEC-0414 forbids running `tailscale` in the loopback server; its test puts a
  recording stub first on `PATH`. This spec is the only one that runs `tailscale`, and only the
  three read-only forms listed in Design.

SPEC-0414 named the new exception `TailscaleDown` (exit 1). This spec supersedes that sentence
only: the exception is `TailnetMissing` and its exit code is **3**, so a supervisor can tell it
from a bind failure (exit 1) and a usage or policy refusal (exit 2).

## Goals / Non-goals

**Goals**
- G1. `nw serve --dashboard` binds this host's own tailnet address by default
  (`--bind tailscale[:PORT]`, the `agentic.dashboard.bind` default), resolved with
  `auth.own_tailnet_ips()`.
- G2. The bind gate accepts loopback or one of this host's own tailnet addresses and nothing else.
  Wildcards, LAN addresses, another machine's tailnet address, host names and `tailscale` on a host
  without a tailnet address are refused before any socket opens, and TLS flags never lift a refusal.
- G3. A host with no tailnet address (tailscale missing, logged out, stopped, or failing) makes the
  server exit with code **3** through a named exception, `TailnetMissing`, with a message that says
  what to do.
- G4. On a tailnet bind every request is authenticated by `tailscale whois --json <peer-ip>`: the
  peer's login must be in `allowed_logins` (default: only this host's own login), tagged nodes are
  refused, and any failure of the check denies the request (fail closed). Verdicts are cached 60 s.
- G5. On a loopback bind nothing changes: the bearer `local.token` stays the authentication.
- G6. Every identity refusal is an audit line carrying the login, node name and node address that
  SPEC-0414 reserved, with the reasons `whois_failed`, `login_not_allowed`, `tagged_node`.
- G7. All of it is provable in CI with a fake `tailscale` executable and an injected peer address,
  without a tailnet, plus a short manual check with the real binary.

**Non-goals**
- Writes, CSRF, plan tokens, the inbox payload (epic child 2). Every method except `GET` still
  gets `405`.
- TLS, `tailscale cert`, MagicDNS HTTPS and push (epic child 5). `--tls-cert/--tls-key/--client-ca`
  with `--dashboard` stay refused, after the bind gate.
- The systemd unit, `nw dashboard status`, the whois self-check command and the `conduct-status`
  `dashboard` key (SPEC-0404). This spec only provides the exit code they rely on.
- A per-device token, `tailscale serve` identity headers (rejected in the epic: any local process
  can forge them) and ACL tag allow-lists. Tagged nodes are refused, not configurable.
- Any change to `snapshot.py`, `queue.dashboard_payload`, `pull_jobs.py` or the `nw serve --jobs`
  mode. The queue still never imports the dashboard.
- Reading `allowed_logins` or the own login again while the server runs; a change needs a restart.

## Decision

1. **Two authentication modes, chosen by the bound address, not by a flag.** A loopback bind uses
   `auth = "token"` (SPEC-0414, unchanged). A bind on one of this host's own tailnet addresses uses
   `auth = "whois"`. The two modes are exclusive: on a tailnet bind a bearer token is neither
   needed nor accepted (no `local.token` is read or created), and on a loopback bind `whois` is
   not consulted. There is no mode where neither check runs.
2. **The bind gate learns the tailnet, and nothing else.** `auth.check_bind(host, own=())` keeps its
   order and adds one acceptance: after the wildcard and non-IP refusals and the loopback pass, an
   address inside the Tailscale ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) is accepted only if
   it is in `own`, the set from `own_tailnet_ips()`; any other address (LAN, public, a link-local
   one) is refused without running `tailscale`. A tailnet address that is not this host's own is
   refused ("foreign tailnet address"). `make_server` runs the same resolution itself, so no caller
   can skip it, and the CLI TLS check still runs after it.
3. **`--bind tailscale[:PORT]` is resolved, not parsed as a host name.** It resolves to this host's
   first own IPv4 tailnet address (an IPv6 one if there is no IPv4), port default 8791. The
   `--dashboard` default bind becomes `settings(cfg)["bind"]` (`tailscale:8791`), so the default is
   the tailnet. An operator who wants the old behavior sets `agentic.dashboard.bind:
   127.0.0.1:8791` or passes `--bind`.
4. **No tailnet address is `TailnetMissing`, exit 3.** `own_tailnet_ips()` raises
   `auth.TailnetMissing(RuntimeError)` when `tailscale` is not on `PATH`, exits non-zero (logged
   out, stopped), times out (5 s), prints nothing usable, or prints no address in the Tailscale
   ranges. `serve_cmd` maps it to a `click.ClickException` whose `exit_code` is 3. `BindRefused`
   stays exit 2 and a bind `OSError` stays exit 1. No mapping inspects message text. The order of
   checks is: parse, bind gate and resolution (2, or 3), TLS refusal (2), bind (1).
5. **Identity gate: `tailscale whois --json <peer-ip>`, fail closed.** The peer IP is the TCP peer
   address (assigned by Tailscale inside the WireGuard tunnel, not spoofable by the peer). The
   verdict is allow only when `UserProfile.LoginName` is in the allowed set and the node is not
   tagged. The allowed set is `agentic.dashboard.allowed_logins` verbatim when non-empty, else only
   this host's own login (`tailscale status --json`: `Self.UserID` → `User[<id>].LoginName`). A
   configured list **replaces** the default, it does not extend it. Logins compare case-insensitively
   and exactly. Anything unexpected (non-zero exit, timeout, bad JSON, a missing field, an
   unresolvable own login, a peer address that is not an IP) is a denial with reason
   `whois_failed`, never an allow and never a `500`.
6. **Verdict cache.** Per peer IP, 60 s for allow and for the definite denials
   (`login_not_allowed`, `tagged_node`), 5 s for `whois_failed` (so a broken `tailscale` is retried
   quickly but not on every request). The own login is cached 60 s with the same clock. The cache is
   bounded (256 peers, oldest dropped). One lock serializes the subprocess calls, each capped at 5 s.
7. **Request flow.** Host/Origin (`403`), then authentication (`401` for the token mode, `403` for
   the whois mode, body `{"error": "forbidden"}` that never names the reason), then the method
   (`405`), then the route. Host values on a tailnet bind are the bound `address:port` and the
   host's MagicDNS name with the port when `tailscale status --json` reports one (`Self.DNSName`
   without the trailing dot); `localhost` is not accepted there. Not knowing the MagicDNS name is
   not an error: only the address form is accepted.
8. **The server may run three `tailscale` forms, all read-only:** `tailscale ip`,
   `tailscale status --json`, `tailscale whois --json <ip>`. Always an argv list (no shell), never
   with a value taken from a request other than the already-validated peer IP, with a 5 s timeout.
9. **Test seams are keyword-only on `make_server` and unreachable from outside.**
   `make_server(cfg, bind=None, *, cache=None, own=None, gate=None, peer_addr=None, auth=None)`:
   `own` replaces `own_tailnet_ips`, `gate` replaces the real `Gate`, `peer_addr` replaces the
   address taken from the socket, and `auth` (`"token"` or `"whois"`) forces the mode on a
   loopback test bind. `serve_cmd` passes none of them, and no flag, environment variable or config
   key reaches them.

## Design

### `auth.py` additions

- `class TailnetMissing(RuntimeError)`.
- `TAILNET_V4 = 100.64.0.0/10`, `TAILNET_V6 = fd7a:115c:a1e0::/48`; `is_tailnet_ip(addr)` (an
  IPv4-mapped IPv6 address is unmapped first).
- `run_tailscale(args, *, timeout=5.0) -> str`: `subprocess.run(["tailscale", *args],
  capture_output=True, text=True, timeout=timeout, check=False)`, no shell. Raises
  `TailscaleError(RuntimeError)` for a missing binary, a timeout, a non-zero exit or non-UTF-8
  output. `TailscaleError` is a private detail: it is turned into `TailnetMissing` in
  `own_tailnet_ips` and into a `whois_failed` denial in the `Gate`.
- `own_tailnet_ips(run=run_tailscale) -> list[str]`: runs `tailscale ip`, one address per line,
  keeps the lines that are tailnet IPs (IPv4 first), de-duplicates. `TailscaleError` or an empty
  result raises `TailnetMissing` with "no tailnet address on this host (`tailscale` is missing,
  logged out or stopped): run `tailscale up`, or bind loopback with `--bind 127.0.0.1:PORT`".
- `check_bind(host, own=())`: as in Decision 2. The messages carry the rule words the tests
  assert: `wildcard`, `not an IP`, `not a loopback or tailnet address`, `foreign tailnet address`.
- `resolve_bind(spec, own_fn=own_tailnet_ips) -> (host, port)`: `parse_bind`; a host equal to
  `tailscale` (case-insensitive) resolves through `own_fn()` and is then checked like any address;
  an address inside the tailnet ranges calls `own_fn()` once to decide; every other address is
  decided by `check_bind` without calling `own_fn`. SPEC-0414's `validate_bind` stays as the
  loopback-only function it is (its unit tests are unchanged); `serve_cmd` and `make_server` call
  `resolve_bind` instead.
- `magicdns_name(run=run_tailscale) -> str | None`: `Self.DNSName` from `tailscale status --json`,
  trailing dot removed, lower-cased; any failure is `None`.
- `host_values(bound_host, port, magicdns=None, *, loopback=True)`: adds `<magicdns>:<port>` and
  drops `localhost:<port>` when `loopback=False`.
- `Verdict` (frozen dataclass: `allowed`, `reason`, `login`, `node`, `node_ip`) and
  `class Gate(run=run_tailscale, allowed_logins=(), *, ttl=60.0, fail_ttl=5.0,
  clock=time.monotonic)` with `check(peer_ip) -> Verdict`:
  1. normalize the peer: strip a `%zone`, unmap IPv4-mapped IPv6; if it is not an IP, or is not a
     tailnet address, deny `whois_failed`;
  2. cache hit within its ttl → the cached verdict;
  3. `allowed = {l.lower() for l in allowed_logins} or {own_login}`; `own_login` from
     `tailscale status --json` (cached); failure → deny `whois_failed`;
  4. `tailscale whois --json <ip>`; parse `UserProfile.LoginName`, `Node.ComputedName` (else
     `Node.Name`), `Node.Tags`. A `Node.Tags` that is non-empty, or a login that is empty or
     `tagged-devices`, is `tagged_node`; a login not in `allowed` is `login_not_allowed`; any parse
     problem is `whois_failed`; else allow. `login`, `node` and `node_ip` are filled whenever they
     are known so the audit line names the device even for a refusal.
- The module still imports nothing from the queue, and `snapshot.py` still does not import `auth`.

### `server.py` changes

- `make_server` resolves the bind with `resolve_bind(bind or settings(cfg)["bind"],
  own_fn=own or own_tailnet_ips)`, then picks `auth` (`"whois"` when the host is a tailnet address,
  else `"token"`, unless the `auth` seam forces it). Token mode loads `local_token` as before;
  whois mode does not call it. It builds `Gate(run_tailscale, settings(cfg)["allowed_logins"])`
  unless `gate` is given, resolves `magicdns_name()` once for the Host values, and stores
  `auth`, `gate`, `hosts` and `peer_addr` on `DashboardServer`.
- `_Handler._handle`: after the Host/Origin check, `if server.auth == "token": _token_ok()` (as
  today) `else: verdict = server.gate.check(peer)` where `peer = server.peer_addr(client_address)`
  (default `self.client_address[0]`). A denial audits `reason`, `status=403`, `login`, `node`,
  `node_ip=peer` and answers `403 {"error": "forbidden"}`; a Host/Origin refusal on a tailnet bind
  audits `node_ip=peer` with the other identity fields `null` (the gate has not run). Allowed
  requests are not audited, as before. An exception inside the gate is a `whois_failed` denial.
- `write_server_json`: `auth` is the real mode (`"whois"` on a tailnet bind); `host` is the tailnet
  address and `url` is `http://<address>:<port>`.
- `DEFAULT_BIND` stays the loopback constant used by tests; the CLI no longer uses it.

### `cli_worker._serve_dashboard`

1. `host, port = DA.resolve_bind(bind or settings(cfg)["bind"])`: `BindRefused` →
   `click.UsageError` (exit 2); `TailnetMissing` → `click.ClickException` subclass with
   `exit_code = 3` (message printed to stderr, no traceback).
2. A TLS flag set → `click.UsageError("TLS is not supported with --dashboard")` (exit 2). Because
   step 1 ran first, a refused or missing-tailnet bind always reports its own message, never the
   TLS one.
3. `make_server(cfg, f"{host}:{port}")` (it resolves again, so a caller cannot skip the gate; the
   second `tailscale ip` call is cheap and also catches the address vanishing in between);
   `OSError` → exit 1.
4. Start line: token mode prints what it prints today; whois mode prints `nw serve: dashboard on
   http://HOST:PORT (tailnet identity via tailscale whois; allowed: <logins or "this host's login">)`.
   No token or `tailscale` output beyond that.
5. Shutdown as today (SIGTERM and SIGINT, exit 0, `server.json` removed only if it is ours).

Docs: `docs/product/reference/index.md` documents `--bind tailscale[:PORT]` and exit 3;
`docs/WORKFLOW.md` (the `agentic.dashboard` section SPEC-0401 started) documents `bind`,
`allowed_logins`, the default own login, the 60 s cache and the Tailscale steps of the epic's
Rollout (`sudo tailscale up`, `sudo tailscale set --operator=$USER`, same account on the phone).

### Fake `tailscale` fixture

`tests/fixtures/fake_tailscale.py`, in the style of `fake_herdr.py`. A helper
`install(tmp_path, scenario) -> env` writes an executable `tailscale` first on `PATH` and a JSON
scenario file named by `$FAKE_TAILSCALE_STATE`; every call appends its argv as one JSON line to
`$FAKE_TAILSCALE_LOG`; `$FAKE_TAILSCALE_MODE` is `ok` | `fail` (exit 1) | `hang` (sleeps past the
timeout) | `garbage` (non-JSON). The scenario holds `own_ips`, `self` (`UserID`, `DNSName`), `users`
(`UserID → LoginName`) and `peers` (`ip → {login, node, tags}`), or `logged_out: true`, which makes
`ip` print `no current Tailscale IPs; state: NeedsLogin` and exit 1, as the real binary does. It
models only `ip`, `status --json` and `whois --json <ip>` and **rejects any other subcommand** with
exit 2, so a test fails if the server ever runs `up`, `set`, `serve` or `cert`. The JSON shapes
(`Self.UserID`, `Self.DNSName`, `User.<id>.LoginName`, `UserProfile.LoginName`, `Node.ComputedName`,
`Node.Name`, `Node.Tags`) are copied from `tailscale` 1.102 output and re-checked in the manual
step below.

## Alternatives considered

- **Keep the loopback default and make the tailnet opt-in.** The point of the dashboard is the
  phone; an opt-in bind means the default `nw serve --dashboard` cannot be used as the unit's
  ExecStart (SPEC-0404) without a flag, and the epic decided `tailscale` is the default.
- **Bind the wildcard and filter by source address.** One mistake in the filter exposes the LAN; the
  bind gate keeps the socket itself off every other interface. Rejected in the epic as well.
- **Trust `tailscale serve` identity headers.** Any local process can send them behind a loopback
  proxy (epic Decision 2).
- **Accept the bearer token on a tailnet bind too.** Two ways in doubles the surface and a leaked
  token would bypass the identity gate; modes are exclusive.
- **Allow-list extends the own login.** A silent union makes "who can see my queue" depend on a
  default not visible in the config; a configured list is the whole list.
- **A separate `TailscaleDown` (exit 1) and `TailnetMissing`.** One exception, one exit code, one
  remedy; the message distinguishes the cause.
- **Cache allow verdicts longer, or never.** Never means a subprocess per poll; hours means a
  revoked share keeps working. 60 s bounds both; failures are retried after 5 s.
- **Pass `own`/`gate` through an environment variable for tests.** An env var is reachable from
  outside the test; keyword-only seams on `make_server` are not.

## Acceptance criteria

- [ ] `auth.own_tailnet_ips()` returns this host's tailnet addresses from `tailscale ip` (IPv4
      first, only addresses in `100.64.0.0/10` or `fd7a:115c:a1e0::/48`), and raises
      `TailnetMissing` when `tailscale` is not on `PATH`, exits non-zero, times out, prints
      garbage, or prints no tailnet address.
- [ ] `nw serve --dashboard` with no `--bind` uses `agentic.dashboard.bind` (default
      `tailscale:8791`) and resolves it to the host's own tailnet address (at server level, with the
      `own` seam and a loopback test bind, the server serves with `auth: "whois"`, and `server.json`
      says so); `--bind tailscale:PORT` and `--bind <own tailnet ip>:PORT` resolve to the same
      address, and through the CLI the gate accepts it (the OS then refuses the bind in CI, exit 1,
      because the address is not local); `--bind 127.0.0.1:PORT` still serves with
      `auth: "token"` and never runs `tailscale`.
- [ ] A host with no tailnet address (`tailscale` missing from `PATH`, logged out, mode `fail`,
      `hang`, `garbage`) makes `nw serve --dashboard --bind tailscale` and the no-`--bind` default
      exit **3** with a message that names `tailscale up`; nothing listens and no `server.json`
      exists. The exit code comes from `TailnetMissing`, not from message text (a unit test raises
      it directly from a stubbed `own_tailnet_ips`).
- [ ] The bind gate exits **2**, stderr naming its rule word, nothing listening and no
      `server.json`, for `0.0.0.0`, `::`, `[::]`, a LAN address (`192.168.1.20`), a public address,
      a foreign tailnet address (`100.64.0.9` when the own address is `100.64.0.5`), and a host
      name, each alone and each combined with `--tls-cert/--tls-key` (existing files), in which
      case the stderr is the bind-rule message and not the TLS one. A wildcard, LAN or public bind
      never runs `tailscale` (the fake's log is empty).
- [ ] A valid bind combined with TLS flags exits 2 with `TLS is not supported with --dashboard`,
      for both a loopback bind and an own tailnet bind; a missing tailnet combined with TLS flags
      exits 3, not 2.
- [ ] `make_server` applies the same gate: calling it directly with each refused bind raises
      `BindRefused` (or `TailnetMissing`) before a socket is created, and the `auth`, `gate`,
      `peer_addr` and `own` seams are keyword-only and are not passed by `serve_cmd` (checked by
      an AST test) nor reachable from a flag, an environment variable or a config key.
- [ ] On a whois-mode server, a request from a peer whose login is in `allowed_logins`, or equal to
      the host's own login when the list is empty, gets `200` on `GET /api/snapshot` with the same
      `ETag` and `304` behavior as the loopback server, with no `Authorization` header; a
      non-empty `allowed_logins` that omits the host's own login refuses the host's own login.
- [ ] A peer with a login not in the allowed set → `403` and an audit line `login_not_allowed`; a
      tagged node (non-empty `Node.Tags`, or login `tagged-devices`) → `403` and `tagged_node`;
      `whois` exiting non-zero, hanging past the timeout, printing non-JSON, or JSON without
      `UserProfile.LoginName`, an unresolvable own login with an empty list, and a peer address
      that is not a tailnet address → `403` and `whois_failed`. The response body is
      `{"error": "forbidden"}` in every case and never names the reason.
- [ ] Each identity refusal audit line is a valid `nw.dashboard.audit.v1` line with `status: 403`,
      `node_ip` equal to the peer, and `login` and `node` filled when whois answered; an allowed
      request appends no line; a Host/Origin refusal on a tailnet bind has `node_ip` filled and
      `login`/`node` `null`; no line contains the `Authorization` header, a query string or a token.
- [ ] Verdict cache: with the injected clock, two requests from one peer within 60 s run `whois`
      once; after 60 s it runs again; a `whois_failed` verdict is retried after 5 s and not before;
      a login removed from the allowed set stays allowed until the entry expires and is refused after;
      the cache never holds more than 256 peers; a cached denial is never turned into an allow
      by a later failure.
- [ ] A whois-mode server does not accept the bearer token as an alternative (a request with a
      valid `local.token` from a denied peer is `403`), does not read or create `local.token`, and
      a loopback token-mode server never calls `Gate` or `tailscale`.
- [ ] Host/Origin on a tailnet bind: the bound tailnet `address:port` and `<magicdns>:<port>` (when
      the fake reports one) pass, with a matching `http://` Origin; `localhost:<port>`, a foreign
      Host, a foreign Origin and `Origin: null` are `403` `bad_host`/`bad_origin` before the gate
      runs (the fake's whois log is unchanged); an unknown MagicDNS name leaves only the address form.
- [ ] The server only runs `tailscale ip`, `tailscale status --json` and `tailscale whois --json
      <ip>`, with an argv list and no shell: over a full run the fake's log holds no other
      subcommand, and the whois argument is always the normalized peer IP (a peer string with
      shell metacharacters or a `%zone` never reaches the command line).
- [ ] The loopback guarantees of SPEC-0414 still hold unchanged: all of its tests pass, including
      read-only over HTTP (nothing outside `agentic/dashboard/` changes; only `local.token` in token
      mode, `audit.jsonl`, `audit.jsonl.1` and `server.json` are created there), the no-import
      invariants, and `nw serve --jobs` behavior.
- [ ] `tests/fixtures/fake_tailscale.py` has a self-test and rejects every subcommand other than
      the three above; docs list `--bind tailscale`, exit 3 and `allowed_logins`; `python3
      tools/spec_lint.py` exits 0.
- [ ] Manual, real binary: on this host (logged out) `nw serve --dashboard` exits 3 with the real
      `tailscale`; after the human logs in, one real `tailscale whois --json <own ip>` parses into
      the same fields the fake provides, and a phone on the same account reaches the dashboard
      while a different login gets `403`. Anything not possible yet is recorded as pending in
      As shipped, not claimed.

## Test plan

**Automated tests.** Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps. CLI cases
run the venv's `nw` as a real subprocess with `NW_DATA_DIR` set to the
`tests/fixtures/dashboard_data.py` seed and the fake `tailscale` first on `PATH`. A real tailnet
address cannot be bound in CI, so server-level cases use the `own`/`peer_addr`/`auth` seams on a
loopback test bind. CLI cases cannot serve on a tailnet address either: the fake reports own IP
`100.64.0.5`, the gate accepts it, and the OS then refuses the bind (`EADDRNOTAVAIL`, exit 1), which
proves the gate accepted the address; the accepted-and-serving path is covered at server level.

- `tests/test_fake_tailscale.py` (fixture self-test): `ip` (own IPs, `logged_out`), `status
  --json` (Self + User map), `whois --json` (known peer, unknown peer exit 1, tagged peer), modes
  `fail`/`hang`/`garbage`, an unknown subcommand (`up`, `set`, `serve`, `cert`) → exit 2, argv log.
- `tests/test_dashboard_tailnet_auth.py` (unit, no sockets): `is_tailnet_ip` table (both ranges,
  edges `100.63.255.255`/`100.64.0.0`/`100.127.255.255`/`100.128.0.0`, IPv4-mapped);
  `own_tailnet_ips` for each failure mode (missing binary, logged out, fail, hang, garbage, only
  non-tailnet lines) → `TailnetMissing`, success ordering IPv4 first; `check_bind`/`resolve_bind`
  table (loopback, wildcard v4/v6/`[::]`, LAN, public, link-local, own tailnet, foreign tailnet,
  host name, `tailscale`, `tailscale:0`, `TAILSCALE`), the rule word in each message, and that only
  tailnet-range addresses and `tailscale` call `own_fn` (a counter); `Gate` table with a stub `run`
  and injected clock: allowed own login by default, allowed list, list that omits the own login,
  case-insensitive login, unknown login, tagged node by tags and by `tagged-devices`, each
  `whois_failed` cause, own-login failure, zoned/mapped/garbage peers, ttl 60 s / fail_ttl 5 s,
  cache bound 256, denial not overturned.
- `tests/test_dashboard_tailnet_server.py` (real server in a thread on `127.0.0.1:0` with
  `auth="whois"`, `own=lambda: [...]`, a `Gate` over the fake, `peer_addr` injected per request via
  a mutable cell): allowed → `200`/`304` with ETag and no `Authorization`; every identity-refusal
  row of the acceptance criteria → `403` `{"error": "forbidden"}` plus the audit line fields;
  Host/Origin matrix with MagicDNS on and off, whois not called on a Host refusal (fake log);
  token not accepted and `local.token` not created; a token-mode server never invokes the fake;
  `server.json` `auth` value; `POST` from an allowed peer → `405`, from a denied peer → `403`
  (the gate precedes the method).
- `tests/test_dashboard_tailnet_cli.py` (real subprocess): exit 3 for logged out, missing binary
  (empty `PATH` dir), `fail`, `hang`, `garbage`, with and without `--bind tailscale`, and with
  TLS flags (still 3); exit 2 with the rule word for `0.0.0.0:0`, `[::]:0`, `192.168.1.20:0`,
  `8.8.8.8:0`, `100.64.0.9:0` (own is `100.64.0.5`), `example.com:8791`, each alone and with TLS
  flags (existing dummy files), stderr lacking `TLS is not supported`, the fake's log empty for the
  non-tailnet ones; own tailnet bind passes the gate (exit 1 from the OS for a non-local address,
  stderr from `OSError`, not a usage error) and with TLS flags exits 2 with the TLS message;
  `--bind 127.0.0.1:0` serves with `auth: "token"` and an empty fake log; no `--bind` uses the
  config's `agentic.dashboard.bind` (a data dir config with `bind: 127.0.0.1:0`).
- `tests/test_dashboard_tailnet_seams.py`: an `ast` check that `serve_cmd`/`_serve_dashboard` pass
  none of `own`, `gate`, `peer_addr`, `auth`; `make_server`'s signature has them keyword-only;
  grep that no `os.environ` read feeds them.
- Updated SPEC-0414 tests: the CLI cases that relied on the loopback default now pass `--bind
  127.0.0.1:...` or set the config `bind`; the `--help`/docs assertions gain `tailscale`.
  `tests/test_dashboard_http_readonly.py` additionally runs one whois-mode server over the fake and
  checks the outside tree is unchanged and that `agentic/dashboard/` holds only `audit.jsonl`,
  `audit.jsonl.1` and `server.json`.
- Docs: a grep of `docs/product/reference/index.md` finds `--bind tailscale` and the exit code 3;
  `python3 tools/spec_lint.py` exits 0.
- Regression guard: the SPEC-0414 and SPEC-0401 dashboard tests, the SPEC-0334 `--jobs` tests and
  `python3 tools/affected_tests.py --run uv run pytest -q`. The queue's close gate runs the
  affected set.

**Manual verification (this host, recorded in As shipped; the real-binary rule).**
1. Logged out (today): `uv run nw serve --dashboard` and `uv run nw serve --dashboard --bind
   tailscale` print the `tailscale up` message and `echo $?` is 3; `ss -ltn` shows nothing on 8791.
2. `uv run nw serve --dashboard --bind 0.0.0.0:8791` exits 2 naming the wildcard rule.
3. After the human runs `sudo tailscale up` and `sudo tailscale set --operator=$USER`: `tailscale
   whois --json $(tailscale ip -4)` output is compared with the fake's shape, `uv run nw serve
   --dashboard` binds the tailnet address, the phone (cellular, same account) loads
   `http://<tailnet-ip>:8791`, a device on another login gets `403`, and `audit.jsonl` names the
   login and node. Step 3 needs the human's login; if not done yet it stays an unchecked item in
   the epic, and this spec's shipped record says so.

## Rollout / migration

1. Land the code, the fake fixture and the tests; update the SPEC-0414 tests that assumed the
   loopback default.
2. The default bind of `nw serve --dashboard` changes from loopback to the tailnet. There is no
   installed unit yet (SPEC-0404), so nothing running changes; anyone starting it by hand without a
   tailnet now gets exit 3 and the message names the loopback alternative.
3. The human's one-time Tailscale steps are in the epic Rollout and in `docs/WORKFLOW.md`; until
   they are done the server fails closed (exit 3, or `403` for every peer if `whois` is not
   permitted without `--operator`).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run pytest
      -q`) plus the files touched; manual steps 1-2 performed and step 3 recorded as done or pending.
- [ ] No regressions (SPEC-0414 / SPEC-0401 dashboard tests, SPEC-0334 `--jobs` tests).
- [ ] Spec body updated to match what shipped (including any difference in the `tailscale` JSON
      shapes found in the real-binary check).
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

- None. Whether `tailscale whois` works without root on this host's `tailscaled` setup can only be
  checked after the human logs in; until then the gate fails closed (`403`), which is the intended
  behavior, and the Rollout names the `--operator` step.
