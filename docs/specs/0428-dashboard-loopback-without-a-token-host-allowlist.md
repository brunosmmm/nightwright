---
id: SPEC-0428
title: "Dashboard auth without a loopback token: Host allowlist, same-origin, per-page CSRF; whois gate wired into the server"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
source_idea: IDEA-963
kind: feature
parent: SPEC-0400
depends_on: [SPEC-0414, SPEC-0416, SPEC-0417, SPEC-0418]
supersedes: [SPEC-0414]
tags: [agentic, dashboard, auth, csrf, tailscale, web]
---

## Context

Promoted from idea `IDEA-963`. Child of epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md).

The human decided on 2026-11-04:

> No token on loopback. Connections to 127.0.0.1/::1 need no login. Protect against drive-by web
> pages in the human's own browser without a token: a strict Host header allowlist (localhost /
> 127.0.0.1 / ::1 with the port, which also blocks DNS rebinding); for every state-changing
> request, require same-origin Origin/Referer plus a per-page CSRF value that the server embeds in
> the page it serves and validates; no CORS headers at all. Remote over Tailscale keeps the whois
> identity gate (wired into the running server, not just present in `auth.py`), and also gets CSRF
> and a confirm step. A token in the URL fragment is harmless and is ignored.

State of `main` on 2026-11-04, checked in the code:

- [SPEC-0414](./0414-dashboard-server-nw-serve-dashboard-spec-0400.md) shipped the loopback server
  with a bearer token from `<data_dir>/agentic/dashboard/local.token` (its Decision 3). The page
  (SPEC-0413/0418) takes the token from a `#token=` fragment into `localStorage`.
- [SPEC-0416](./0416-dashboard-tailnet-slice-spec-0400-child-follows.md) resolves `--bind
  tailscale` and own tailnet addresses, but `auth.resolve_bind` then **refuses** every tailnet
  address ("the identity gate is not available yet").
- [SPEC-0417](./0417-dashboard-tailnet-whois-identity-gate-and-tailnet.md) ships `auth.Gate`
  (`tailscale whois`, allowed logins, a fail-closed per-peer cache), and **nothing calls it**.
- The server has no write route, so there was nothing for CSRF to protect yet. SPEC-0429 adds
  the first write (answering escalations); this spec builds the protection it uses.

This spec **supersedes SPEC-0414** because it replaces SPEC-0414's Decision 3 (loopback bearer
token) and Decision 4 (Host/Origin values). Everything else SPEC-0414 decided (the second mode of
`nw serve`, the bind gate, auditing refusals, `GET /api/snapshot` from the cache with an ETag,
`server.json`, exit codes) carries forward unchanged and is restated by reference here.

## Goals / Non-goals

**Goals**
- G1. A loopback dashboard needs no login: `http://127.0.0.1:8791/` loads and works.
- G2. A web page from another origin, open in the human's browser, cannot read dashboard data
  (no CORS, Host allowlist against DNS rebinding) and cannot make a state-changing request
  (same-origin Origin/Referer and a CSRF value it cannot read).
- G3. `nw serve --dashboard --bind tailscale` (or an own tailnet IP) serves, and every request on
  it, static files included, passes `auth.Gate` (whois, allowed logins, fail closed) first.
- G4. Each request has an identity (`local`, or the whois login + device + node IP) that the CSRF
  value is bound to and that the audit log records.

**Non-goals**
- HTTPS (epic child 5), multi-user roles, cookies or sessions.
- The write routes themselves: SPEC-0429.

## Decision

1. **Two auth modes, chosen by the bound address.** A loopback bind is mode `local`: no
   credential, every request's identity is `{login: "local", node: "local", node_ip: <peer>}`.
   Any other bind (only an own tailnet IP can pass the bind gate) is mode `whois`: every request,
   including `/`, `/app.js` and `/app.css`, first runs `Gate.check(peer_ip)`; a denial is `403`
   with the gate's reason in the audit log. `allowed_logins` comes from
   `agentic.dashboard.allowed_logins` (empty: the host's own login). Open streams re-run the check
   every heartbeat (through the gate's 60 s cache), so a revoked device loses its stream within
   60 s + one heartbeat.
2. **`resolve_bind` stops refusing own tailnet addresses.** The interim refusal SPEC-0416 left
   "for the identity-gate slice to remove" is removed. Wildcards, LAN addresses, foreign tailnet
   addresses and TLS flags stay refused (SPEC-0416 unchanged otherwise). The default bind stays
   `127.0.0.1:8791`; remote use is `--bind tailscale`.
3. **Strict Host allowlist on every request**, before anything else. Loopback binds accept
   `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>` (and the bound address); tailnet binds
   accept the bound address, the host's MagicDNS name and its first label (from `tailscale status
   --json`), all with the port. Anything else (a rebinding domain, a missing Host) is `403
   bad_host`. `Origin`, when present, must be `http://` + an allowed host (`null` refused).
4. **No CORS.** No response carries any `Access-Control-*` header; `OPTIONS` is `405`. A
   cross-origin page can therefore not read any response, and its JSON POST with a custom header
   needs a preflight that always fails.
5. **Writes need same-origin and a per-page CSRF value.** Every `POST` must carry:
   - `Content-Type: application/json` (else `415`);
   - an `Origin` that is allowed, or, when the browser sent no `Origin`, a `Referer` that starts
     with `http://<allowed host>/` (neither: `403 no_origin`);
   - `X-NW-CSRF: <nonce>.<mac>`, where the server minted `nonce` (16 random bytes) when it served
     `/` and `mac = HMAC-SHA256(secret, "csrf" | nonce | identity_key)`. `secret` is 32 random
     bytes per server process; `identity_key` is `local` on loopback and `login|node_ip` on the
     tailnet. A missing or wrong value is `403 bad_csrf`. The value is valid for the server
     process's life (a restart invalidates open pages; the page says "reload").
   The page gets the value from `<meta name="nw-csrf" content="…">`, which the server fills in
   when it serves `/` (`index.html` holds the placeholder `__NW_CSRF__`; `/` is sent with
   `Cache-Control: no-store`). A cross-origin page cannot read it (no CORS, and `frame-ancestors
   'none'` already forbids framing).
6. **The local token is gone.** The server no longer creates or reads `local.token`; an old file
   is left alone and unused. The page no longer sends `Authorization`; a `#token=` fragment is
   removed from the address bar and ignored, and an old stored token is deleted. Requests that do
   send an `Authorization` header are not refused for it. `auth.local_token` is removed.
7. **Audit identity.** Refusals keep their `nw.dashboard.audit.v1` line and now carry `login`,
   `node` and `node_ip` when known (`local` on loopback; whois' login and `ComputedName` on the
   tailnet). SPEC-0429 adds the `plan` / `answer` lines.

## Design

- `auth.py`: `bind_mode(host)` returns `"local"` / `"whois"` (was `"token"` / `"whois"`);
  `host_values(bound, port, mode, magic=None)` builds the allowlist of Decision 3;
  `check_host_origin` unchanged in shape; new `Csrf(secret)` with `mint(identity_key) -> str` and
  `check(value, identity_key) -> bool` (constant-time); new `check_same_origin(headers, allowed)`
  for Decision 5; `resolve_bind` loses its final refusal; `local_token` removed.
- `server.py`: `DashboardServer(cfg, addr, cache, *, mode=None, gate=None, peer=None)`; `mode`
  defaults to `bind_mode(bound host)`; in `whois` mode `gate` defaults to
  `A.Gate(allowed_logins=settings(cfg)["allowed_logins"])` and the MagicDNS name is added to the
  Host allowlist at start. `peer` (a test seam, never set by the CLI) maps the socket's peer
  address to the IP given to the gate. `_Handler._identity()` returns an `Identity` or audits and
  answers `403`. The handler order is: bounded body read → Host/Origin → identity → route →
  (POST) content type, same-origin, CSRF.
- `assets.py`: `render_index(csrf)` substitutes the placeholder; `ROUTES` unchanged.
- `static/app.js`: no token, no `Authorization`; the CSRF value comes from `env.csrf()` (the
  browser reads the meta tag) and is sent as `X-NW-CSRF` on POSTs only.
- `lifecycle.build_record`: `auth` is the server's mode (`local` / `whois`).
- `cli_worker.serve_cmd`: help texts and the start line say "no login on loopback" / "tailnet,
  whois identity gate".

## Alternatives considered

- **Keep the token on loopback.** Rejected by the human: the token in a URL fragment and
  `localStorage` is friction for a local tool, and does not add protection the Host allowlist,
  same-origin and CSRF checks do not already give against browser attackers. A local process can
  read the data dir directly anyway.
- **A cookie session with SameSite=Strict.** Adds state and a login step for no gain over a
  per-page header value that only same-origin script can read.
- **CSRF value = HMAC of the identity only (no nonce).** Simpler, but it is the same value for
  every page; a per-page nonce limits a leaked value to that page and costs nothing (stateless
  check).

## Acceptance criteria

- [x] On a loopback bind, `GET /`, `/app.js`, `/app.css`, `/api/snapshot` and `/api/stream`
      answer 200 with no `Authorization` header and no token anywhere; `local.token` is not
      created; a request that sends `Authorization: Bearer junk` is served the same.
- [x] A request whose `Host` is not in the allowlist (e.g. `evil.example:8791`, a missing Host)
      is `403` with a `bad_host` audit line; a foreign `Origin` is `403 bad_origin`. On loopback
      `127.0.0.1:P`, `localhost:P` and `[::1]:P` are accepted.
- [x] No response of any route (200, 304, 4xx, 5xx, SSE, OPTIONS) carries an
      `Access-Control-*` header; `OPTIONS` is `405`.
- [x] A `POST` without `X-NW-CSRF`, with a wrong or foreign-identity value, with a foreign
      `Origin`, with neither `Origin` nor `Referer`, or with a non-JSON content type is refused
      (403 / 403 / 403 / 403 / 415), audited, and writes nothing else.
- [x] `GET /` embeds a fresh CSRF value per load (two loads differ, both validate); a value from
      another server process fails.
- [x] `--bind tailscale` and an own tailnet IP are no longer refused by `resolve_bind`
      (wildcard / LAN / foreign tailnet / TLS still are). On a `whois`-mode server every route,
      static files included, is gated by `Gate.check`: an allowed login is served (and its CSRF
      value is bound to login + node IP), an unknown login, a tagged node and a failing `whois`
      get `403` with the reason and device in the audit line; an open stream is closed after the
      gate starts denying.
- [x] `nw serve --help` no longer says "loopback only" or names a token; `server.json` `auth`
      is `local` or `whois`; the page sends no `Authorization` header and ignores a `#token=`
      fragment.

## Test plan

- **Automated tests**
  - `tests/test_dashboard_server.py`, `tests/test_dashboard_http_readonly.py`,
    `tests/test_dashboard_server_assets.py`, `tests/test_dashboard_stream.py`,
    `tests/test_dashboard_serve_cli.py`, `tests/test_dashboard_auth.py`,
    `tests/test_dashboard_tailnet_*.py`: updated from "401 without the token" to "200 with no
    credential"; Host allowlist cases (`127.0.0.1`, `localhost`, `[::1]`, foreign, missing);
    no `Access-Control-*` on any response.
  - `tests/test_dashboard_csrf.py` (new): `Csrf` unit tests (mint/check, wrong identity, wrong
    secret, malformed values); the page embeds a fresh value per load; POST refusal matrix
    (missing / wrong CSRF, foreign Origin, no Origin and no Referer, Referer-only accepted,
    text/plain body) over real sockets; refused POSTs leave the data dir byte-identical apart
    from `audit.jsonl`.
  - `tests/test_dashboard_whois_server.py` (new): an in-process server in `whois` mode on real
    sockets with a fake `tailscale` run function and an injected peer IP: allowed → 200 and the
    page's CSRF value works for that identity only; unknown login / tagged / whois failure →
    403 with login, node and node IP in the audit line; a stream is closed after the fake gate
    switches to deny (shortened cache TTL and heartbeat).
  - `tests/js/page_logic.test.js`: no `Authorization` header; a `#token=` fragment is removed
    and not used.
- **Manual verification (real processes, recorded in As shipped):** a real `nw serve
  --dashboard --bind 127.0.0.1:<free port>` against a scratch data dir: curl the page and the
  snapshot with no token; a foreign Host → 403; a POST without CSRF and one with a foreign Origin
  → 403; no `Access-Control-*` header in any response.
- **Regression guard:** affected tests (`python3 tools/affected_tests.py --run …`), the
  read-only and import-graph dashboard guards, `python3 tools/spec_lint.py`.

## As shipped

Shipped as designed. Notes on what the code does where the Design left room:

- Refusals carry `{"error", "reason"}`; a refusal before the identity is known (bad Host / Origin)
  still records the peer `node_ip`. `OPTIONS` is `405` (or `403` when it carries a foreign
  `Origin`, which is checked first). A `GET` with a body is `413 body_on_get` on every route.
- In `whois` mode the server reads the MagicDNS name once at start (`Gate.magic_name()`) for the
  Host allowlist. `localhost` is not accepted on a tailnet bind (it is not the bound address).
- `server.json` `auth` is `local` / `whois`. `nw serve --help` and the start line name the mode
  (`(loopback, no login)` / `(tailnet, tailscale whois identity gate)`). `docs/WORKFLOW.md`'s
  Remote dashboard section describes both.
- `make_server(..., mode=, gate=, peer=)` are test seams only; `serve_cmd` never passes them.

Real-process evidence (2026-11-04), all against a SCRATCH data dir under
`/home/bruno/work/.nw-tmp/dash-ui/` with escalations opened by the real CLI (`nw queue escalate
--yes`), never the real data dir:

- `nw serve --dashboard --bind 127.0.0.1:<free port> --no-code-watch` (start line `(loopback, no
  login)`, `server.json` `auth: local`, no `local.token` created): `GET /` 200 with a
  `<meta name="nw-csrf">` value and `Cache-Control: no-store`; `/api/snapshot` 200 with no
  credential, 200 with `Authorization: Bearer junk`, 200 for `Host: localhost:P` and
  `Host: [::1]:P`; `Host: evil.example:P` → `403 bad_host`; zero `Access-Control-*` headers on
  `/`, `/api/snapshot` and a 404.
- POST `/api/escalations/ESC-0001/plan|answer`: no `X-NW-CSRF` → 403 `bad_csrf`; a wrong value →
  403 `bad_csrf`; `Origin: http://evil.example` → 403 `bad_origin`; neither Origin nor Referer →
  403 `no_origin`; `text/plain` → 415; Referer-only from the page's origin → 200. A hash of every
  file outside `agentic/dashboard/` was unchanged after all refused writes.
- **Tailnet, real process**: an unprivileged network namespace (`unshare -rn`) whose `lo` carries
  100.101.0.5–.8, a fake `tailscale` on `PATH` (`ip` → 100.101.0.5; `status --json` → own login
  `me@example.com`, MagicDNS `box.tail1234.ts.net`; `whois --json`: .5 = phone/me, .6 = another
  login, .7 = tagged node, .8 = whois exit 1). `nw serve --dashboard --bind tailscale:8791` started
  with `(tailnet, tailscale whois identity gate)`, `server.json` `auth: whois`. From .5: page and
  snapshot 200, `Host: box.tail1234.ts.net:8791` 200, a foreign Host 403; plan + answer of a CLI
  escalation 200 (record `answered_by: human`); a CSRF value not minted for this identity 403.
  From .6 / .7 / .8: `/`, `/app.js`, `/api/snapshot` all 403, audited as `login_not_allowed`
  (`eve@example.com`, `laptop-of-eve`), `tagged_node` (`ci-runner`) and `whois_failed`; from
  127.0.0.1 to the tailnet address 403 `not_tailnet_peer`. The audit lines for the answer carry
  `login me@example.com, node phone, node_ip 100.101.0.5`. The human's real Tailscale was not
  touched (it stays logged out).

Automated: `tests/test_dashboard_csrf.py`, `tests/test_dashboard_whois_server.py` (new), and the
updated server, assets, stream, serve-CLI, tailnet, audit and read-only tests; the page's token
removal in `tests/js/page_logic.test.js` and `tests/js/live.test.js`.

## Rollout / migration

1. No data migration. An existing `local.token` is left on disk, unused. Old bookmarks with
   `#token=` keep working (the fragment is ignored).
2. Tailnet serving needs the human's Tailscale login (`sudo tailscale up`, `sudo tailscale set
   --operator=$USER`); until then `--bind tailscale` exits 3 (SPEC-0416, unchanged).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None. Decided above: mode by bind address, per-page nonce + HMAC CSRF bound to the identity,
Referer accepted only when Origin is absent, default bind stays loopback._
