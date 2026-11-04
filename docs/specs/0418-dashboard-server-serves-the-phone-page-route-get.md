---
id: SPEC-0418
title: "Dashboard server serves the phone page: GET / /app.js /app.css through assets.ROUTES, bearer-exempt, behind Host/Origin"
status: proposed
owner: user
created: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-919
tags: [agentic, dashboard, remote, web, auth]
depends_on: [SPEC-0413, SPEC-0414, SPEC-0416]
---

## Context

[SPEC-0413](./0413-dashboard-phone-page-spec-0400-child-slice-3-of.md) (done) shipped the phone
page: `static/index.html`, `static/app.css`, `static/app.js` and
`nightwright/agentic/dashboard/assets.py` (`ROUTES`, `read_asset`, `CSP`). Its Rollout step 2 says
"the dashboard server spec's implementation serves the page through `read_asset` and sets the
headers of the contract", and its Design section ("Contract with the server") fixes those headers.
[SPEC-0414](./0414-dashboard-server-nw-serve-dashboard-spec-0400.md) (done) shipped the server,
and [SPEC-0416](./0416-dashboard-tailnet-slice-spec-0400-child-follows.md) (done) its bind gate.
Nobody ever did step 2, so the page cannot be opened from a phone today.

What exists today, checked against the code on 2026-11-04:

- `src/nightwright/agentic/dashboard/server.py` `_Handler._handle` (one handler bound to every
  method) runs, in order: drain a bounded body; `A.check_host_origin` (403 `bad_host` /
  `bad_origin`); `_token_ok` (401 `no_token` / `bad_token` with `WWW-Authenticate: Bearer`);
  non-`GET` (405 `method_not_allowed`, `Allow: GET`); a body on a `GET` (413); then
  `path = urlsplit(self.path).path` and `404 {"error": "not found"}` for anything but
  `/api/snapshot`. Every refusal goes through `_refuse`, which writes an `audit.jsonl` line.
  `_send` already adds `X-Content-Type-Options: nosniff` and `Cache-Control: no-cache` to every
  response, and `http.server` adds `Date`.
- `server.py` does not import `assets`. So `GET /` is a `401` without a token and a `404` with
  one, and the token check precedes the route, so an unauthenticated peer cannot even learn which
  paths exist.
- `assets.read_asset(path)` is an exact match against `ROUTES`; anything else (including `..`,
  `/index.html`, a query string left in the argument) returns `None`. `assets.CSP` is the one-line
  policy.
- The tailnet half of the bind gate is shipped (SPEC-0416: `--bind tailscale`, own-address
  resolution). The `tailscale whois` identity gate is not (`auth.resolve_bind` still refuses a
  non-loopback address with "tailnet serving needs the identity gate"). It is a later sibling.
  This spec must not make that sibling harder: static assets must sit behind it too, not beside
  it.
- A phone that opens `http://<host>:8791/` cannot send an `Authorization` header on a
  navigation, nor on the `<link>`/`<script>` fetches the HTML triggers. The page can only get its
  token from the URL fragment after it has loaded (SPEC-0413 Decision 5). So the three static
  files cannot be behind the bearer check, or the page can never load to receive the token.

## Goals / Non-goals

**Goals**
- G1. `GET /`, `GET /app.js` and `GET /app.css` on the dashboard server return the files of
  `assets.ROUTES`, through `assets.read_asset`, with the headers of the SPEC-0413 contract.
- G2. Those three routes need no bearer token. Nothing else changes its auth: `/api/snapshot`
  stays `401` without a valid token, and every other path stays `401` without one.
- G3. The static routes are still behind the bind gate (the server only exists on a socket the
  gate allowed), the `Host`/`Origin` check, and any later identity (whois) gate: they are reached
  only after every check that precedes the bearer check today.
- G4. SPEC-0413 records its dependency on SPEC-0414 (`depends_on`), as its own Rollout step 3
  prescribed.
- G5. Tests with the real loopback server over real sockets and no browser.

**Non-goals**
- The `tailscale whois` identity gate (its own later spec). This spec only fixes where the static
  routes sit relative to it.
- Any change to `assets.py`, the three static files, `/api/snapshot`, `ETag`/`304` handling,
  the bind gate, the audit file format, the token mechanism or `nw serve`'s CLI surface.
- `HEAD`, `OPTIONS`, conditional (`ETag`/`If-None-Match`) or compressed responses for assets:
  three small files, `no-cache`, a phone on a tailnet. `HEAD` stays a `405` as for every
  non-`GET`.
- Serving anything but the three `ROUTES` entries (no directory listing, no `favicon.ico`, no
  `/static/...`).
- A real-browser test (the human opens the page once, manually, below).

## Decision

1. **The server routes the three asset paths through `assets.read_asset`, and only those.**
   After the `Host`/`Origin` check, `_handle` computes `path = urlsplit(self.path).path` (the
   query is dropped exactly as it is for `/api/snapshot`; the fragment never reaches a server)
   and asks `assets.read_asset(path)`. A non-`None` answer is a static request. `read_asset`
   stays the only allowlist: the server holds no copy of the route table and never builds a
   filesystem path from the request.
2. **Static requests skip the bearer check; every other request keeps it.** A static request is
   served before `_token_ok` runs, so an `Authorization` header on it, valid or not, is ignored
   (a stale token in a phone's stored request does not break the page). For any path
   `read_asset` rejects the pipeline is unchanged: `401` without a valid token, then `405`, `413`,
   `404`, `/api/snapshot`. An unauthenticated peer therefore still learns nothing about unknown
   paths, and `/api/snapshot` is as gated as before. The static files carry no data (SPEC-0413
   Decision 8, "No data" in `index.html`); the snapshot is the data.
3. **The static branch is placed after every identity/origin check and before the token
   check, in the same `_handle`.** The order is: body drain, `Host`/`Origin`, (a future whois
   gate is inserted here, ahead of the static branch), static routes, bearer, method, body,
   `/api/snapshot`. A request that fails an earlier check gets that check's refusal and audit
   line and never a byte of an asset.
4. **Static responses are `GET` only.** A non-`GET` on an asset path is `405` with `Allow: GET`
   and a `method_not_allowed` audit line, without a token (the path is public, the refusal
   reveals nothing). A `GET` carrying a body is `413` as today.
5. **Headers per the SPEC-0413 contract.** `200` with the body and `Content-Type` of the route,
   `Content-Security-Policy: <assets.CSP>`, `Cache-Control: no-cache`,
   `X-Content-Type-Options: nosniff`, `Content-Length`, and the stdlib `Date`. The CSP header is
   sent only with static responses (it governs documents; the JSON API and error bodies are not
   documents). The page's `Date`-derived age logic (SPEC-0413 Decision 4) relies on `Date` being
   present, which the stdlib guarantees.
6. **Serving a page is not an audited event; refusing one is.** A `200` static response writes
   no `audit.jsonl` line (as a `200` snapshot does not); `403` / `405` / `413` on a static path
   go through the existing `_refuse` and are audited like any other refusal. The path in the audit
   line is `self.path` as today.
7. **A broken install is a `500`, not a crash.** If `read_asset` raises (`OSError`, packaged file
   missing), the response is `500 {"error": "<ExcType>: <msg>"}` and the server keeps serving.
8. **SPEC-0413's `depends_on` gains `SPEC-0414`** in the same change (`depends_on: [SPEC-0401,
   SPEC-0414]`, with `updated`), as its Rollout step 3 prescribed and its Context announced for
   the change that lands the server. No other line of SPEC-0413 changes: this is the recorded
   dependency, not a revision of its decisions.

## Design

### `server.py`

- `from . import assets as AS` (stdlib-only module, already free of queue-side imports).
- In `_Handler._handle`, between the `check_host_origin` refusal and `_token_ok`:

  ```python
  path = urlsplit(self.path).path
  try:
      asset = AS.read_asset(path)
  except Exception as e:                      # noqa: BLE001 - a broken install must not kill the server
      return self._send(500, {"error": f"{type(e).__name__}: {e}"})
  if asset is not None:
      if method != "GET":
          return self._refuse(405, "method_not_allowed", "method not allowed",
                              headers=[("Allow", "GET")])
      if n != 0:
          self.close_connection = True
          return self._send(413, {"error": "a GET takes no body"})
      body, ctype = asset
      return self._send(200, raw=body, ctype=ctype,
                        headers=[("Content-Security-Policy", AS.CSP)])
  ```

  The existing `path = urlsplit(self.path).path` further down is removed (it is computed once,
  above). `read_asset` returns `None` for every non-route path, so the rest of `_handle` is
  byte-for-byte what it is today for `/api/snapshot` and unknown paths. The exceptions of
  `read_asset` are only reachable for a route (the lookup precedes any read), so the `500` is
  specific to a missing packaged file.
- `_send` already supplies `Content-Type` (from `ctype`), `Content-Length`, `nosniff`,
  `Cache-Control: no-cache`, and the extra `Content-Security-Policy`. Its `HEAD` suppression is
  unreachable for assets (a `HEAD` is refused `405` first).
- The module docstring changes from "`GET /api/snapshot`" to "the phone page and
  `GET /api/snapshot`", and states that the static routes are the only unauthenticated ones.
- The request-time cost is a small file read per request. No caching layer: three files of a few
  KiB, one phone, `Cache-Control: no-cache`. Not worth a cache to invalidate.

### Request pipeline after this spec

| step | refusal | applies to |
|---|---|---|
| body drain | closes the connection for an unreadable/oversize body | all |
| `Host` / `Origin` | `403` `bad_host` / `bad_origin` | all |
| (later) whois identity | `403` | all |
| static route | `405` non-`GET`, `413` body, `500` read failure | asset paths only |
| bearer token | `401` `no_token` / `bad_token` | everything else |
| method / body | `405` / `413` | everything else |
| route | `404` unknown path, `200`/`304`/`503` snapshot | everything else |

### SPEC-0413

Frontmatter only: `depends_on: [SPEC-0401, SPEC-0414]` and `updated: 2026-11-04`.

### Tests that change

`tests/test_dashboard_server.py::test_404_and_413_and_503` loops over `("/", "/nope",
"/api/snapshot/x")` and expects `404` for `/`. `/` is now an asset, so `/` leaves that tuple
(it is covered by the new file).

## Alternatives considered

- **Keep the bearer on the assets and carry the token in a cookie or query string.** The page
  could not load from a bare URL, and a token in a query string is sent in `Referer`/history
  logs. SPEC-0413 chose the URL fragment, which a server never sees, so the assets must be
  public.
- **Serve assets before the `Host`/`Origin` check.** A DNS-rebinding page could then read the
  JS/HTML. Low value to an attacker, but it breaks "behind everything except the bearer", and a
  later whois gate would have to be threaded around it. Rejected for a uniform rule.
- **Apply the bearer check to the assets but return the page on `401`.** Conflates an error with
  content and leaves the 401 audit noisy on every phone load. Rejected.
- **Serve unauthenticated `404` for unknown paths too.** Would reveal that a path is not a route
  without a token. Keeping `401` for everything non-static preserves today's no-enumeration
  behaviour.
- **A separate static handler class / second port.** More surface and a second bind to gate for
  three files. Rejected.

## Acceptance criteria

- [ ] `GET /`, `GET /app.js` and `GET /app.css`, with a correct `Host` and no `Authorization`,
      return `200` with a body byte-identical to `assets.read_asset(path)[0]` and the matching
      `Content-Type` from `assets.ROUTES`.
- [ ] Each of those responses carries `Content-Security-Policy` equal to `assets.CSP`,
      `Cache-Control: no-cache`, `X-Content-Type-Options: nosniff`, a `Content-Length` equal to
      the body length and a `Date` header.
- [ ] An `Authorization` header (valid or wrong) on a static request does not change the `200`.
- [ ] `GET /api/snapshot` without a token is still `401` (`no_token`) with
      `WWW-Authenticate: Bearer`, with a wrong token `401` (`bad_token`), and with the token
      `200` (with `ETag`) and `304` on a matching `If-None-Match`: unchanged.
- [ ] Any other path without a token (`/nope`, `/index.html`, `/static/app.js`, `/../app.js`,
      `//app.js`, `/%61pp.js`, `/app.js/`) is `401`; with the token it is `404
      {"error": "not found"}` and no asset bytes.
- [ ] A query string on a static path does not change the response (`/app.js?v=1` and
      `/?x=1` are `200` with the asset); the server never reads a fragment.
- [ ] A static request with a bad, missing or non-allowed `Host`, or a bad/`null` `Origin`, is
      `403` with a `bad_host` / `bad_origin` audit line and a JSON error body that contains none of
      the asset's bytes.
- [ ] `POST`, `PUT`, `DELETE`, `PATCH`, `OPTIONS` and `HEAD` on `/`, with no token, are `405`
      with `Allow: GET` and a `method_not_allowed` audit line and an empty or JSON-only body (no
      asset bytes); a `GET /` with a request body is `413`.
- [ ] A successful static response adds no line to `audit.jsonl`; each refusal above adds
      exactly one.
- [ ] If `assets.read_asset` raises, the response is `500` with a JSON `{"error": …}` body, and a
      later request to `/api/snapshot` is still `200`.
- [ ] The Host/Origin check precedes the static branch: with `A.check_host_origin` patched to
      refuse, `GET /` is `403` and no asset is returned.
- [ ] Serving the assets writes nothing outside `agentic/dashboard/` and adds no file inside it
      beyond the existing four (`local.token`, `audit.jsonl`, `audit.jsonl.1`, `server.json`).
- [ ] The bind gate is unchanged: `make_server` still refuses a public/non-tailnet bind with
      `BindRefused` before any socket opens, and the SPEC-0414/SPEC-0416 server and tailnet tests
      pass untouched, apart from the one listed edit to `test_404_and_413_and_503`.
- [ ] `nw serve --dashboard --bind 127.0.0.1:0` started as a subprocess answers `GET /` with the
      page (contains the `index.html` bytes) without a token.
- [ ] SPEC-0413's frontmatter `depends_on` is `[SPEC-0401, SPEC-0414]` and nothing else in its
      body changed; `python3 tools/spec_lint.py` exits 0.
- [ ] `server.py`'s docstring names the static routes as the only unauthenticated ones.

## Test plan

- **Automated tests** (new `tests/test_dashboard_server_assets.py`, reusing the `Srv` loopback
  helper of `tests/test_dashboard_server.py` on `127.0.0.1:0` over real sockets and
  `fixtures.dashboard_data.seed`; no browser, no `tailscale`; waits via `tests/waits.py`):
  - `test_static_routes_unauthenticated_200`: each `assets.ROUTES` path with `auth=False`:
    status, body equals `read_asset`, `Content-Type`, CSP, `no-cache`, `nosniff`,
    `Content-Length`, `Date`.
  - `test_static_ignores_authorization`: a valid and a wrong bearer both give the same `200`.
  - `test_api_snapshot_still_gated`: `401` `no_token`, `401` `bad_token` (+ `WWW-Authenticate`),
    `200` with `ETag`, `304` on `If-None-Match`.
  - `test_non_route_paths_stay_401_then_404`: the path list of the criteria, without a token →
    `401`; with the token → `404`, body has none of the asset bytes.
  - `test_query_string_on_asset_path`: `/app.js?v=1`, `/?x=1` → `200` asset.
  - `test_static_behind_host_and_origin`: wrong `Host`, no `Host`, `Origin: http://evil`,
    `Origin: null`, each `403` with the `bad_host`/`bad_origin` audit line and no asset bytes; a
    `localhost:<port>` `Host` is accepted; and `A.check_host_origin` monkeypatched to refuse makes
    `GET /` `403`.
  - `test_static_methods_and_body`: the five non-`GET` methods plus `HEAD` on `/` without a token
    → `405`, `Allow: GET`, audit line; `GET /` with a body → `413`.
  - `test_audit_only_refusals`: count `audit.jsonl` lines around a run of `200` and refused static
    requests (a `200` adds none, each refusal adds one).
  - `test_read_asset_failure_is_500`: monkeypatch `server.AS.read_asset` to raise `OSError`;
    `500` JSON, then `/api/snapshot` is `200`.
  - `test_serving_assets_writes_nothing`: before/after `tree()` (from
    `tests/test_dashboard_readonly.py`) of the data dir, request every route, only the allowed
    files under `agentic/dashboard/` change.
  - `test_header_parity_with_page_stub`: the headers the real server sends for each route equal
    those the stub of `tests/test_dashboard_page.py` sends (`Content-Type`, CSP, `nosniff`,
    `no-cache`), so SPEC-0413's contract and the real server cannot drift.
  - `test_spec_0413_depends_on_includes_0414`: parse the SPEC-0413 frontmatter, assert
    `depends_on == ["SPEC-0401", "SPEC-0414"]`.
  - `test_serve_cli_serves_page`: using the helpers of `tests/test_dashboard_serve_cli.py`
    (`start`, `wait_server`, `get`, `stop`), run `nw serve --dashboard --bind 127.0.0.1:0` and
    `GET /` without a token; assert it returns `index.html`'s bytes.
- **Edited test:** `tests/test_dashboard_server.py::test_404_and_413_and_503` drops `/` from its
  `404` tuple.
- **Manual verification** (once; this is also SPEC-0413's manual step 1, pending until the server
  served it): on a host with a queue data dir, run `uv run nw serve --dashboard --bind
  127.0.0.1:8791`; `curl -si http://127.0.0.1:8791/ | head` shows `200`, `text/html;
  charset=utf-8`, the CSP and `no-cache` headers with no token; `curl -si
  http://127.0.0.1:8791/api/snapshot` shows `401`. Then open
  `http://127.0.0.1:8791/#token=$(cat <data_dir>/agentic/dashboard/local.token)` in a phone-sized
  browser window (or via an SSH tunnel from the phone) and confirm the page loads, the fragment
  disappears from the address bar and the snapshot renders. Note the result in the spec.
- **Regression guard:** run the affected tests
  (`python3 tools/affected_tests.py --run uv run pytest -q`), which must include
  `tests/test_dashboard_server.py`, `test_dashboard_http_readonly.py`, `test_dashboard_auth.py`,
  `test_dashboard_tailnet_*.py`, `test_dashboard_serve_cli.py`, `test_dashboard_assets.py`,
  `test_dashboard_page.py`, `test_dashboard_page_logic.py`, `test_dashboard_imports.py` (the queue
  never imports the dashboard) and `test_dashboard_packaging.py`; plus `python3 tools/spec_lint.py`.
  Heavy gates go to the box with `nw gate run`, not a local full suite.

## Rollout / migration

1. One change: `server.py` branch, the one edited test, the new test file, SPEC-0413's
   `depends_on`/`updated`. No data migration; nothing is written. A running dashboard server
   must be restarted to serve the page.
2. After it lands, `http://<host>:8791/` is the phone entry point, and the bearer token (loopback)
   still goes in the URL fragment. Tailnet-bind exposure of the page follows the whois gate
   sibling: until it exists, `auth.resolve_bind` still refuses non-loopback serving, so the page
   is reachable only on loopback (for example through an SSH tunnel).
3. When the whois gate spec lands it inserts its check ahead of the static branch (Decision 3 and
   the pipeline table) and adds its own test that an unauthorized peer gets no asset.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (affected tests via
      `python3 tools/affected_tests.py --run uv run pytest -q`); manual verification performed and
      noted.
- [ ] No regressions (the SPEC-0414 and SPEC-0416 server, tailnet and bind-gate tests green).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._
