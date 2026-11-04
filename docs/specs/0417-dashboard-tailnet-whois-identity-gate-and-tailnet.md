---
id: SPEC-0417
title: "Dashboard whois Gate and Verdict, and fake tailscale status/whois"
status: accepted
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-918
tags: [agentic, dashboard, remote, web, auth, tailscale]
depends_on: [SPEC-0416]
---

## Context

This is the first of two specs that finish the tailnet half of child 1 of the epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md), the sibling that
[SPEC-0416](./0416-dashboard-tailnet-slice-spec-0400-child-follows.md) (done) announced in its
Non-goals and Rollout step 3. SPEC-0416 built the *bind gate* and deliberately ends in one
refusal in `auth.resolve_bind`: an accepted own tailnet address is refused with "tailnet serving
needs the identity gate". The epic decision (Decision 2) is that tailnet peers authenticate with
`tailscale whois --json <peer-ip>`.

The whole identity-gate work was reviewed as too large for one campaign (12 acceptance criteria
over the gate, the server, Host/Origin, audit, `server.json`, the CLI default and the docs). It
is split in two:

- **This spec (A): the decision logic.** `auth.Verdict` and `auth.Gate` (whois parsing,
  tagged-node refusal, the allowed set with its own-login default, the verdict cache), the small
  pure helpers `_peer_ip`, `Gate.magic_name` and `auth.bind_mode`, and the fake `tailscale`
  gaining `status --json` and `whois --json`. It is built and fully unit-tested with **no server
  or CLI change**: `resolve_bind` keeps its refusal, so the tailnet stays unservable and nothing
  weakens. Nothing calls the gate yet.
- **The second spec (B), written after this one lands, `depends_on: SPEC-0417`:** wiring the gate
  into the server (whois mode), lifting the `resolve_bind` refusal, the default bind, Host/Origin
  with the MagicDNS name, audit and `server.json`, the test seams, docs and the manual
  phone check. See Non-goals for the exact list and the decisions B inherits.

SPEC-0402 (decisions inbox; AC6: a request from a node that is not allowed, from a tagged node
or while whois fails gets 403 and a node-named audit line) and SPEC-0404 (lifecycle; AC3: a unit
with no `--bind` exits 3 when the tailnet is missing) need B, which needs A.

What exists today (checked against the code on 2026-11-04):

- `src/nightwright/agentic/dashboard/auth.py`: `resolve_bind` (the refusal at the end),
  `check_bind(host, own)`, `own_tailnet_ips`, `run_tailscale(args, *, timeout=5.0)` (argv list,
  no shell; raises `TailscaleError` for a missing binary, a timeout, a non-zero exit or non-UTF-8
  output), `is_tailnet_ip` (IPv4-mapped IPv6 unmapped first), `local_token(cfg)`,
  `host_values(bound_host, port)` and `check_host_origin`.
- `src/nightwright/agentic/dashboard/__init__.py`: `settings(cfg)` returns `allowed_logins`
  (`[]`), which nothing reads yet.
- `tests/fixtures/fake_tailscale.py` models only `tailscale ip` and rejects every other
  subcommand with exit 2 (so a test fails if code ever ran `status` or `whois`); it takes
  `$FAKE_TAILSCALE_MODE` (`ok`/`fail`/`hang`/`garbage`) and a scenario with `logged_out`, and logs
  every call's argv as one JSON line to `$FAKE_TAILSCALE_LOG`.
- `tailscale` 1.102.4 is installed on this host and **logged out**: the real binary's failure
  paths (`whois`, `status --json` with no `Self`) can be checked today; a successful whois needs
  the human's login (epic Rollout step 1).

## Goals / Non-goals

**Goals**
- G1. A `Gate` that decides, for one peer IP, whether the peer may see the dashboard, from
  `tailscale whois --json <peer-ip>`. It fails closed: any whois error, unreadable output, tagged
  node or unknown own login denies.
- G2. `allowed_logins` is honoured by the gate: empty means the host's own login only.
- G3. Verdicts are cached per peer IP (60 s allowed, 5 s denied) so a polling phone costs almost
  no `tailscale` calls and a denied peer cannot make the process spawn one per request.
- G4. The gate exposes the host's MagicDNS name (`Gate.magic_name()`) and `bind_mode` classifies a
  bind host as `token` (loopback) or `whois` (anything else), for B to use.
- G5. All of it is provable in CI with the fake `tailscale` and an injected clock, plus one
  real-binary check, with `resolve_bind`, `server.py` and the CLI untouched.

**Non-goals** (the second spec B carries these; none is done here)
- Server whois mode: the handler order (Host/Origin, then token or gate, then 405/413/routing),
  the 403 body `{"error": "forbidden"}`, no `local.token` on a tailnet bind, an ignored
  `Authorization` header.
- Removing the refusal in `resolve_bind` ("the same change that wires the gate into the
  server") and replacing `DEFAULT_BIND` with `settings(cfg)["bind"]` in `make_server` and
  `_serve_dashboard`; the changed SPEC-0416 test rows that follow from both.
- Host/Origin with the MagicDNS name. Inherited design correction: `DashboardServer` computes
  `self.hosts = A.host_values(addr[0], self.server_address[1], magic_name,
  loopback=(auth == "token"))` **after binding**, as it does today (the real port is only known
  after bind, and every test binds `127.0.0.1:0`); `make_server` passes `magic_name` (from
  `gate.magic_name()` in whois mode), never a precomputed hosts set.
- Audit identity fields and the `allowed` action, `server.json` `auth: "whois"`, the start line,
  the `gate` / `peer_addr` / `auth` test seams (`auth="token"` with a non-loopback bind is a
  `ValueError`), the server-wide allow-list of `tailscale` argv forms, docs, and the manual
  phone check.
- Distinguishing local users on the host from the owner on a tailnet bind. Inherited decision
  for B (Decision 5 there): a connection from this host to its own tailnet address is
  whois-attributed to this host's node and login, so on a tailnet bind any local process can read
  the snapshot (SPEC-0414's local-user boundary for `local.token` no longer applies there); this
  is accepted because the dashboard is read-only and the host is single-user, and SPEC-0400
  child 2 (writes) must revisit it. A here only pins the Gate side of it: it does not special-case
  the host (Decision 2).
- Writes, CSRF, plan tokens (SPEC-0400 child 2), TLS, push (child 5), the systemd unit and
  `nw dashboard status` (SPEC-0404), roles or per-device permissions, a hot reload of
  `allowed_logins`.
- Any change to `resolve_bind`, `server.py`, `cli_worker.py`, `audit.py`, `snapshot.py`,
  `queue.dashboard_payload` or `pull_jobs.py`.

## Decision

1. **One decision point: a `Gate` that returns a `Verdict`.** `auth.Gate(run=run_tailscale,
   allowed_logins=(), clock=time.monotonic)` has `check(peer_ip) -> Verdict` and
   `magic_name() -> str | None`. `Verdict` is a frozen dataclass: `allowed: bool`, `reason: str`,
   `login`, `node`, `node_ip` (strings or None), `fresh: bool` (true when this call ran
   `tailscale`, false when it came from the cache). `check` never raises: every exception becomes
   a denied verdict with reason `gate_error`. The gate takes `allowed_logins` as an argument; it
   reads no config and no environment.
2. **The whois form and its input.** `Gate` runs exactly `tailscale whois --json <ip>` (argv
   list, no shell, `run_tailscale`'s 5 s timeout). `<ip>` is the peer address given to `check`,
   normalised by `_peer_ip` (IPv6 zone `%...` stripped, IPv4-mapped IPv6 unmapped, re-rendered by
   `ipaddress`; `None` when it is not an IP), and only if `is_tailnet_ip(ip)`; any other peer is
   denied `not_tailnet_peer` without running `tailscale`. The output is parsed as JSON and the
   verdict is:
   - the command fails, times out or prints non-JSON / a non-object → denied `whois_failed`;
   - `Node.Tags` is a non-empty list, or `UserProfile.LoginName` is missing, empty or
     `tagged-devices` → denied `tagged_node` (a node with no user cannot be an allowed login);
   - the login (casefolded, stripped) is not in the allowed set → denied `login_not_allowed`;
   - otherwise allowed, reason `ok`. `login` is `UserProfile.LoginName`; `node` is
     `Node.ComputedName`, else `Node.Name` without a trailing dot; `node_ip` is the normalised
     peer.

   The gate does not special-case this host: a peer whose whois is this host's own node and own
   login is allowed like any other peer.
3. **The allowed set.** `allowed_logins`, casefolded, blanks dropped. If the result is empty, the
   set is the host's **own login only**, from `tailscale status --json`:
   `User[str(Self.UserID)].LoginName`, with `Self` having no non-empty `Tags` and a user id. If the
   own login cannot be determined (the command fails, no `Self`, a tagged host, no matching
   `User`), every request is denied `self_login_unknown`. An explicit non-empty list is exactly
   the set; the own login is not added to it. The own login is read lazily and re-read at most
   every 60 s (a failed read is kept no longer than the 5 s denied TTL); it is only needed while
   `allowed_logins` is empty.
4. **Cache.** Per peer IP: an allowed verdict for 60 s, any denied verdict (including
   `whois_failed`, `tagged_node`, `self_login_unknown`; `not_tailnet_peer` is not cached, it costs
   nothing) for 5 s. At most 512 entries (expired first, then the oldest evicted). Concurrent
   calls for one peer share one `tailscale` call (a per-peer lock around the miss path; the cache
   lock is not held while `tailscale` runs). `clock` is injectable.
5. **`Gate.magic_name()`** returns `Self.DNSName` from the same memoised `status --json` read
   (lowercased, trailing dot removed), `None` when unknown (failed read, no `Self`, empty name).
   It never raises.
6. **`auth.bind_mode(host)`** is `"token"` for a loopback address and `"whois"` for every other
   host string. It is a pure function; `resolve_bind` is unchanged and still refuses an accepted
   own tailnet address, so no caller exists yet and nothing is served on the tailnet.
7. **The `tailscale` forms the gate may run are exactly two:** `status --json` and
   `whois --json <peer-ip>`, read-only argv lists through `run_tailscale`. Never `up`, `set`,
   `serve`, `cert`, `login`, `ping` or `file`. (`ip` remains SPEC-0416's.)

## Design

### `auth.py` (additions only)

- `Verdict` (frozen dataclass) and `Gate` as in Decisions 1-5. Private helpers:
  `_whois(ip) -> Verdict`, `_self_info() -> (login | None, dns_name | None)` memoised as in
  Decision 3, `_allowed_set()`, `_peer_ip(addr) -> str | None`.
- `bind_mode(host) -> str` (Decision 6).
- `resolve_bind`, `check_bind`, `own_tailnet_ips`, `host_values`, `check_host_origin`,
  `local_token` and `run_tailscale` are **not modified**; `host_values` keeps its two-argument
  form (its `magic_name` / `loopback` parameters belong to B).
- The module still imports nothing from the queue; `snapshot.py` does not import `auth`;
  `server.py` and `cli_worker.py` do not reference `Gate`, `Verdict` or `bind_mode`.

### Fake `tailscale` fixture

`tests/fixtures/fake_tailscale.py` gains `status --json` and `whois --json <ip>` (every other
subcommand besides `ip` still exits 2). The scenario adds
`self: {"user_id": 7, "login": "me@example.com", "dns_name": "host.tail1234.ts.net.", "tags": []}`
and `peers: {"100.64.0.20": {"login": "me@example.com", "name": "phone.tail1234.ts.net.",
"computed_name": "phone", "tags": []}}`. `status --json` prints `Self` and `User` in the real
shape (`Self.UserID`, `Self.DNSName`, `Self.Tags`, `User{"<id>": {"LoginName": ...}}`);
`whois --json` prints `Node` (`ComputedName`, `Name`, `Tags`, `Addresses`) and `UserProfile`
(`LoginName`) for a known peer and exits 1 for an unknown one. The own address is a known peer
too (its node is the host, its login the own login) so the same-host case can be tested.
`logged_out` makes `status --json` print `{"BackendState": "NeedsLogin"}` with no `Self` (exit
0, as the real binary does) and `whois` exit 1. `$FAKE_TAILSCALE_MODE` applies to every
subcommand and the scenario key `modes: {"whois": "fail"}` overrides it per subcommand. Every
call is still logged as one JSON argv line. The existing `ip` behaviour is unchanged.

### Tests

New `tests/test_dashboard_whois_gate.py`; extended `tests/test_fake_tailscale.py`. No existing
test file changes.

## Alternatives considered

- **Land the whole identity gate in one campaign.** Rejected by review: 12 acceptance criteria
  over several subsystems. The gate logic is independently buildable and testable, and leaving
  `resolve_bind` alone keeps the invariant that no commit serves the tailnet without the gate.
- **`tailscale serve` identity headers (`Tailscale-User-Login`).** Needs a loopback bind behind
  a proxy; any local process could forge the headers. Rejected in the epic.
- **Query the LocalAPI over its Unix socket instead of the CLI.** Avoids a subprocess but adds a
  protocol client; the cache makes the CLI cost negligible and the fake-binary test is simple.
- **Allowed = own login plus the list.** An explicit list that silently also admits the own
  login is surprising; an explicit list is exactly the list.
- **Cache denials for 60 s as well.** A freshly fixed whois failure (for example after `tailscale
  set --operator`) would keep the phone locked out for a minute; 5 s still bounds the spawn rate.
- **Fail open when `tailscale` is unavailable.** Violates the epic's fail-closed rule; the gate
  denies until whois works again.

## Acceptance criteria

- [ ] `Gate.check` verdict table (fake `run`, no sockets): an allowed login →
      `Verdict(allowed=True, reason="ok")` with `login`, `node` (`ComputedName`, else `Name`
      without the trailing dot) and `node_ip`; another login → `login_not_allowed`; non-empty
      `Node.Tags` or a missing, empty or `tagged-devices` login → `tagged_node`; a failing,
      hanging (`TailscaleError`), non-JSON, non-object or exception-raising `run` →
      `whois_failed` / `gate_error`, never an exception; a non-tailnet peer (`127.0.0.1`,
      `192.168.1.5`, `8.8.8.8`, garbage) → `not_tailnet_peer` with `run` never called; an
      IPv4-mapped tailnet peer is unmapped before whois; login comparison is case-insensitive; a
      peer whose whois is the host's own node and login is allowed.
- [ ] Allowed set: empty `allowed_logins` → the own login from `status --json` (`Self.UserID` →
      `User`); a tagged host, no `Self` (logged out), a failing `status` or a missing `User`
      entry → every peer denied `self_login_unknown`; a non-empty list is exactly the set (the
      own login is refused if it is not in it, and `status` is never run); a list of blanks
      behaves as empty.
- [ ] Cache (injected clock): a second `check` for one peer within 60 s runs no second
      `tailscale` call (`fresh` false); at 60 s it re-runs; a denied verdict is reused at 4.9 s
      and re-run at 5 s; 20 threads asking for one peer at once cause exactly one whois call; the
      cache never holds more than 512 peers; a login that stops being allowed is denied once its
      entry expires; a failed own-login read is retried after 5 s, a good one is not re-read for
      60 s.
- [ ] Helpers: `_peer_ip` maps an IPv4-mapped address to its IPv4 form, strips an IPv6 zone id
      and returns `None` for garbage; `Gate.magic_name()` returns `host.tail1234.ts.net` for
      `Self.DNSName` `Host.Tail1234.ts.net.` and `None` for a failing read, no `Self` or an empty
      name, never raising; `bind_mode` is `"token"` for `127.0.0.1`, `::1` and `127.0.0.2`, and
      `"whois"` for `100.64.0.5` and `fd7a:115c:a1e0::1`.
- [ ] Through the real `run_tailscale` and the fake binary (`ok`, `fail`, `hang`, `garbage`): the
      `Gate` returns an allowed verdict only in `ok` for a known allowed peer, a denied verdict
      in every other mode, and the fake's log over the whole run contains only
      `["status", "--json"]` and `["whois", "--json", "<tailnet ip>"]`, never a non-tailnet peer.
- [ ] The fake `tailscale` self-test covers `status --json` (own login, `logged_out`, tagged
      self), `whois --json` (known, unknown, tagged peer, the own address) and per-subcommand
      `modes`, and still exits 2 for `up`, `set`, `serve`, `cert`, `login`; the existing `ip`
      behaviour and every existing SPEC-0414 / SPEC-0416 dashboard test pass **unchanged**.
- [ ] Nothing is wired: `resolve_bind` still refuses an own tailnet address with the "identity
      gate" message (the SPEC-0416 tests pass without edits); an `ast` check shows `server.py` and
      `cli_worker.py` do not reference `Gate`, `Verdict` or `bind_mode`; `auth.py` does not import
      the queue (`tests/test_dashboard_imports.py` passes); `python3 tools/spec_lint.py` exits 0.
- [ ] Manual, real binary (this host, logged out): `Gate().check("100.64.0.5")` with the real
      `tailscale` returns a denied verdict (`whois_failed` or `self_login_unknown`),
      `Gate().magic_name()` returns `None`, and neither raises. A check with a logged-in node
      (allowed login and node name for the own tailnet IP) needs the human's login and is
      recorded as pending in As shipped, not claimed.

## Test plan

**Automated tests.** Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps; the cache
tests use the injected clock, not time. The fake `tailscale` is first on `PATH` for the tests that
go through the real `run_tailscale`.

- `tests/test_fake_tailscale.py` (extended): `status --json` (own login, logged out, tagged
  self), `whois --json` (known, unknown, tagged, own address), `$FAKE_TAILSCALE_MODE` and
  per-subcommand `modes`, an unknown subcommand (`up`, `set`, `serve`, `cert`, `login`) → exit 2,
  the argv log. The existing `ip` rows stay as they are.
- `tests/test_dashboard_whois_gate.py` (new; unit, fake `run` + injected clock): the verdict table
  (criterion 1), the allowed-set table (2), the cache table (3: TTL boundaries at 59.9/60 and
  4.9/5 s, the 20-thread single-flight, the 512 cap, own-login retry timing), the helpers
  (`_peer_ip`, `magic_name`, `bind_mode`; criterion 4), `check` never raising (a `run` raising
  `KeyError`), the real-`run_tailscale` rows with the fake binary in all four modes and its argv
  allow-list (criterion 5), and the not-wired checks (criterion 7: `ast` scan of `server.py` and
  `cli_worker.py`, `resolve_bind` still refusing `tailscale` with the "identity gate" text).
- Regression guard: the SPEC-0414 / SPEC-0416 / SPEC-0401 dashboard tests and
  `tests/test_dashboard_imports.py` unchanged and passing; `python3 tools/spec_lint.py`;
  `python3 tools/affected_tests.py --run uv run pytest -q`. The queue's close gate runs the
  affected set.

**Manual verification (this host; the real-binary rule).**
1. Logged out (today): `uv run python -c "from nightwright.agentic.dashboard import auth; g =
   auth.Gate(); print(g.check('100.64.0.5')); print(g.magic_name())"` prints a denied verdict
   (`whois_failed` or `self_login_unknown`) and `None` with the real `tailscale`, no traceback.
2. `uv run nw serve --dashboard --bind tailscale` still exits 3 (logged out) and nothing listens
   on 8791: the server is untouched.
3. After the human runs `sudo tailscale up` and `sudo tailscale set --operator=$USER`:
   `Gate().check(<own tailnet ip>)` is allowed with the human's login and node name, and
   `Gate().magic_name()` is the host's MagicDNS name. If the login is not available the step stays
   pending and the shipped record says so.

## Rollout / migration

1. Land the gate, `bind_mode`, the fake and the tests together. No user-visible behaviour
   changes: nothing calls the gate, `resolve_bind` still refuses the tailnet, the CLI default is
   still loopback.
2. Write and land the second spec B (`depends_on: SPEC-0417`), which wires the gate in and
   removes the refusal in the same change, so no commit serves the tailnet without the gate.
3. No data migration; nothing is written to disk by this spec.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass (`python3 tools/affected_tests.py --run
      uv run pytest -q`); manual steps performed or recorded as pending with the reason.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped (As shipped section).
- [ ] `docs/LEDGER.md` updated (status + date) by regenerating it.

## Open questions

None. The design questions are decided above (own-login default, cache TTLs, exclusive modes
derived from the bind, same-host connections treated as the owner on a tailnet bind, the
magic-name-not-hosts-set correction), the server-side ones being recorded under Non-goals for B.
Whether `tailscale whois` works without root depends on the human's `tailscale set
--operator=$USER`; the gate fails closed (denied) until then.
