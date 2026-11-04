---
id: SPEC-0413
title: "Dashboard phone page: phone-first static UI with a 15 s conditional poll of /api/snapshot"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-904
tags: [agentic, dashboard, remote, web]
depends_on: [SPEC-0401]
---

## Context

This is slice 3 of the split of child 1 of the epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md) (the epic's first
child was cut into three specs; see Context of
[SPEC-0401](./0401-read-only-dashboard-server-and-nw-dashboard-v1.md)). The human asked for
"a live web-based dashboard of active items so that I can consult this while I am away from
home", reached over Tailscale only.

The three slices, in order:

1. **SPEC-0401 (done): the data layer.** `nw.dashboard.v1`, `SnapshotCache`, the watermark
   and `nw dashboard snapshot`. It opens no socket.
2. **The dashboard server** (a sibling spec written in its own campaign, from `IDEA-903`):
   `nw serve --dashboard`, the bind gate, whois/token auth, Host/Origin checks, audit, and
   `GET /api/snapshot` with a strong `ETag` equal to the snapshot's `rev` and `304` on
   `If-None-Match`. This spec depends on it at runtime, and its Rollout orders the two. It is
   not named in `depends_on` because that spec has no file yet and the linter rejects a
   `depends_on` that does not resolve; the campaign that lands the server spec adds
   `SPEC-NNNN` here in the same change that accepts it.
3. **This spec: the phone page.** The static HTML/CSS/JS the server serves at `GET /`, and
   the package code that locates those files.

What exists today, checked against the code on 2026-11-04:

- `src/nightwright/agentic/dashboard/` holds `__init__.py` and `snapshot.py` only. There is no
  `static/` directory, no `assets.py`, and no `server.py`.
- The `nw.dashboard.v1` document (SPEC-0401) already carries everything the page shows:
  `health.{runner,conductor,stewards,herdr_watch}`, `totals` (with `spend_today_usd`, `active`,
  `ready`, `attention`, `waiting`, `backlog`, `conductor_handling`), `active` rows (with
  `label`, `campaign`, `state`, `loops`, `activity`, `cost_usd` and `cap_usd`, plus `*_age_s`
  ages), `needs_you`, `waiting`, `backlog`, `delivered`, `recent_events`
  (`{id, ts, type, campaign, spec_id, project, by, summary}`), and `inbox.{escalations,parks}`.
  A section whose builder failed is `{"error": "<Type>: <message>"}`, and `health.herdr_watch`
  may be `null`.
- Ages in the document are ages at `generated_at`, and `rev` ignores them (SPEC-0401
  Decision 2). A client derives a live age as `age_s + (now − generated_at)`.
- Python's `BaseHTTPRequestHandler.send_response` emits a `Date` header on every response.
- The repo has `node` on the dev host (`/usr/bin/node`) and no headless-browser dependency in
  `pyproject.toml`. The wheel is built with hatch (`pyproject.toml:46`).

## Goals / Non-goals

**Goals**
- G1. A single-page, phone-first UI (390×844 down to 360×740, one column, no horizontal
  scroll) showing: a health dot, runner/conductor/steward/herdr-watch health, active
  campaigns with cost vs cap, counts, a **needs you** list, recent events and today's spend.
- G2. It keeps itself current with a **15 s conditional poll** of `GET /api/snapshot`
  (`If-None-Match: "<rev>"`, `304` keeps the screen), pauses while the tab is hidden, and
  refreshes at once when the tab becomes visible.
- G3. Ages tick on screen from `generated_at`, between polls and across `304`s, without a
  new snapshot.
- G4. Every failure is visible and never silent: `401`/`403`, a network error, a stale
  snapshot, and a section the builder reported as `{"error": …}`.
- G5. Safe by construction: the page renders every snapshot value with `textContent` only,
  loads nothing from outside the host, and runs under a strict CSP.
- G6. The page and its asset lookup ship as package data, with the CSP string and the asset
  allowlist defined here so the server spec only routes to them.
- G7. The page is testable without a browser binary in the gate, against real HTTP and the
  real snapshot builder.

**Non-goals**
- The server, bind gate, auth, audit, Host/Origin checks, the `GET /api/snapshot` route and
  the `ETag`/`304` logic: the dashboard server sibling spec. This spec defines only what the
  page expects from the route (see Design, "Contract with the server").
- Answering escalations, the inbox detail view (options, recommendation, facts, findings,
  effects) and any `POST`: epic child 2. This page lists open escalations and human-owned
  parks as read-only lines.
- SSE, `EventSource`, the campaign drilldown and the run tail: epic child 3. Polling stays as
  the fallback after child 3 lands.
- The systemd unit, `nw dashboard status|unit`: epic child 4. Service worker, manifest, push
  and HTTPS: epic child 5.
- A headless Chromium test (no browser binary in `pyproject.toml` or on the dev host). The
  browser-level check is manual (Test plan).
- Any change to `nw.dashboard.v1`, `SnapshotCache`, `queue.dashboard_payload` or `nw queue
  watch`.

## Decision

1. **Plain static files, no build step, no framework, no dependency.** The page is
   `static/index.html`, `static/app.css` and `static/app.js` in
   `src/nightwright/agentic/dashboard/static/`, shipped as package data. Vanilla JS, one
   script file, no module imports, no `fetch` to any origin but its own.
2. **Poll, don't push.** The page polls `GET /api/snapshot` every 15 s with
   `If-None-Match: "<rev>"` (the strong ETag, quoted, equal to the snapshot's `rev`). A `200`
   replaces the screen. A `304` keeps it. The page never opens an `EventSource`.
3. **Pause when hidden, refresh when shown.** Polling uses one `setTimeout` chain, never a
   `setInterval`. While `document.visibilityState === "hidden"` no timer is armed and no
   request is made. On `visibilitychange` to `visible` the page polls immediately (cancelling
   any pending timer) and re-arms 15 s after that request settles.
4. **Ages are derived on the client from server time.** A snapshot carries ages at
   `generated_at`. The page keeps `serverNow`, the `Date` response header of the latest
   response (`200` or `304`) plus the local milliseconds elapsed since it arrived, falling back
   to `Date.now()` when the header is absent or unparsable. The displayed age of an `age_s`
   field is `age_s + max(0, serverNow − generated_at)`. A 1 s timer re-renders only the age
   text nodes (not the whole tree), and it too is paused while hidden. Using the server's
   clock keeps a phone with a wrong clock from showing negative or inflated ages.
5. **The token lives in `localStorage`, taken from the URL fragment once.** The first load
   from `http://<host>:8791/#token=<value>` stores `<value>` under
   `localStorage["nw-dashboard-token"]` and immediately clears the fragment with
   `history.replaceState`, so it is not left in the address bar or sent in a `Referer`. When a
   token is stored, every request sends `Authorization: Bearer <token>`. On a tailnet bind
   (whois-gated) no token is stored or needed and the header is not sent. The token is never
   written to the DOM.
6. **Failures are shown as banners, with an exact rule each.** One banner area, at most one
   banner at a time, highest priority first:
   - `401`: "Not signed in. Open the dashboard link that carries the token." Polling continues
     (every 15 s) so a corrected token is picked up on the next visit.
   - `403`: "This device is not allowed to view the dashboard." Polling continues at 15 s.
   - network error / non-2xx/304 other than the above: "Can't reach the dashboard." with the
     last good snapshot kept on screen, greyed.
   - **stale:** no `200`/`304` for more than 45 s (3 polls) while visible: "Last update N s
     ago." The health dot turns grey.
   A banner clears on the next `200`/`304`.
7. **The health dot is one of four states, computed by a pure function.** `healthLevel(snap,
   conn)` returns, in this order: `grey` if `conn` is stale or errored or there is no snapshot
   yet; `red` if the runner lease is not `held` or the conductor has no pid, or either of
   `health.runner`/`health.conductor` is an `{"error": …}` section; `amber` if the lease is
   `stale`, the runner is `draining`, `runner_code.stale` is true, there is an open
   escalation or human-owned park (`needs_you` non-empty), or any other section is an error
   section; `green` otherwise. The text next to the dot names the reason (for example
   "runner down", "2 need you").
8. **Everything is `textContent`.** The page builds DOM only with `createElement`,
   `textContent`, `setAttribute` of fixed attribute names and `classList`. It never uses
   `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval` or `new Function`.
   Attribute values set from snapshot data are limited to `title` (a string) and
   `aria-valuenow`/`aria-valuemax` (finite numbers clamped to 0–100); no URL-bearing attribute
   (`href`, `src`, `action`, `style`) is ever set from snapshot data. A static test pins this.
9. **A strict CSP, defined once here.** `assets.CSP` is
   `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src
   'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'` (one line).
   `index.html` therefore carries no inline `<script>`, no inline `<style>`, no `style=`
   attributes and no `on…=` handlers. The page references `/app.css` and `/app.js` by absolute
   path and nothing external.
10. **The package owns the asset allowlist; the server only routes.** `assets.py` exposes
    `ROUTES` (`"/"` → `index.html` `text/html; charset=utf-8`, `"/app.js"` → `application/
    javascript; charset=utf-8`, `"/app.css"` → `text/css; charset=utf-8`),
    `read_asset(path) -> (bytes, content_type) | None` (exact match against `ROUTES`; any other
    path, including `..` segments, query-stripped variants and `index.html`, returns `None`)
    and `CSP`. It reads with `importlib.resources`, so it works from the wheel. It opens no
    socket and writes nothing.
11. **The page code is injectable so it can be tested without a browser.** `app.js` exposes
    `createApp(env)` where `env` supplies `fetch`, `document`, `storage`, `location`,
    `history`, `now()`, `setTimeout`, `clearTimeout` and a visibility accessor. The browser
    bootstrap (building `env` from `window` and calling `createApp(env).start()`) runs only
    when `typeof window !== "undefined"`; otherwise the file does `module.exports = {createApp,
    healthLevel, displayAge, formatAge, formatUsd}`. This is the one concession to testing and
    adds no behavior in the browser.

## Design

### Files

New, under `src/nightwright/agentic/dashboard/`:

- `assets.py`: `ROUTES`, `CSP`, `read_asset(path)`, as in Decision 10.
- `static/index.html`: the shell. `<meta name="viewport" content="width=device-width,
  initial-scale=1">`, `<meta name="color-scheme" content="light dark">`, `<link rel="stylesheet"
  href="/app.css">`, `<script src="/app.js" defer>`, a banner region, and one empty
  `<section>` per area below with fixed ids. No data.
- `static/app.css`: mobile-first, one column, system font stack, `prefers-color-scheme` dark
  mode through custom properties, no fixed widths (`max-width: 100%`, `overflow-wrap:
  anywhere` on text rows), tap targets at least 44 px tall, and no `@import` or `url()` to an
  external host.
- `static/app.js`: the code of Decisions 2–8 and 11.

Edited: `pyproject.toml` only if hatch's wheel target does not already include
non-Python files under `src/nightwright` (checked by the packaging test below, not assumed).

### Sections (top to bottom)

1. **Header.** Health dot and its reason text; "updated Ns ago" (derived, ticking); the banner
   region beneath it.
2. **Needs you.** One line per open escalation (`ESC-NNNN`, campaign label, first line of the
   question) and per human-owned park (campaign label, park reason, `detail` when present),
   from `inbox.escalations` and `inbox.parks`. Empty: "Nothing needs you." Read-only.
3. **Health.** Rows: *runner* (`pid@host` or pid, heartbeat age, "stale lease", "draining",
   "stale code"; "down" when the lease is not held), *conductor* (pid or "down", plus lag when
   non-zero), *stewards* (count and addresses), *herdr watch* ("n/a" for `null`).
4. **Active.** One card per `active` row: label and `spec_id`, `state`, loop count, an
   activity line (`activity.kind == "run"` shows `persona`, with the run's elapsed time as a
   ticking age; `gates` shows "gates"; otherwise "idle"), and **cost vs cap**: `$cost / $cap`
   with a bar whose fill is `min(100, cost/cap·100)`; a null or zero cap shows `$cost` with
   "no cap" and no bar. The bar is a native `<progress max="100">` element whose `value`
   property is set to `min(100, cost/cap·100)` (or, if a div is used, a fill whose width is set
   with `el.style.width = n + '%'`, which the CSSOM allows under the CSP); `setAttribute("style",
   …)` is never used, because `style-src 'self'` blocks it. The bar is `amber` at ≥ 80 % and
   `red` at ≥ 100 % through a fixed class name via `classList`, and carries `aria-valuenow` and
   `aria-valuemax="100"` (finite numbers clamped to 0–100; `role="progressbar"` when a div is
   used).
5. **Counts.** One line like the CLI's: `active N · queued N · parked N · waiting N · backlog
   N · conductor-handling N`, from `totals` (`ready` → queued, `attention` → parked).
6. **Spend today.** `$X.XX` from `totals.spend_today_usd` (two decimals).
7. **Recent events.** The `recent_events` list newest first, at most 20, each as `HH:MM type
   label` plus `summary` (time in the phone's locale; the full ISO `ts` goes in a `title`
   attribute, a fixed attribute name with a text value).

An `{"error": "<msg>"}` section renders as a single muted line `<section>: <msg>` and does
not stop the others from rendering.

### Contract with the server

The page expects exactly this of the dashboard server spec, and the test stub (below)
implements exactly this:

- `GET /`, `GET /app.js`, `GET /app.css` serve `assets.read_asset(path)` with its content type
  and a `Content-Security-Policy: <assets.CSP>` header, `Cache-Control: no-cache` and
  `X-Content-Type-Options: nosniff`. Any other path is `404`.
- `GET /api/snapshot` returns the `SnapshotCache.get()` document as JSON with `ETag: "<rev>"`
  and `Cache-Control: no-cache`. `If-None-Match: "<rev>"` of the current rev returns `304`
  with the same `ETag`. `401` on a missing/wrong loopback token and `403` for a denied tailnet
  peer, both with a small JSON `{"error": …}` body.
- Every response carries `Date` (the stdlib default).

If the server spec deviates, it amends this section by superseding this spec, not by silent
drift.

### Client state machine

```
start():  read token from fragment -> storage (clear fragment) ; poll()
poll():   if hidden: return (no timer)
          GET /api/snapshot  (Authorization if token, If-None-Match if rev)
          200 -> snapshot=body ; rev=ETag/body.rev ; conn=ok ; render()
          304 -> conn=ok ; serverNow updated (no render of data, ages still tick)
          401/403 -> conn=denied(code) ; error -> conn=error
          arm setTimeout(poll, 15000)
visibilitychange: hidden -> clear poll+age timers ; visible -> clear, poll() now, restart age timer
age tick (1 s): update age text nodes ; recompute stale ; update banner/dot
```

At most one request is in flight: a poll that fires while one is pending is dropped, and the
next one is armed when the pending one settles.

## Alternatives considered

- **SSE now.** Epic child 3 owns it; mobile Safari drops background streams anyway, so the
  conditional poll is the base layer that stays.
- **A framework (React/Preact/htmx) or a bundler.** Rejected by the epic's "no build step, no
  external fetch, no new dependency"; the screen is seven small sections.
- **`setInterval(…, 15000)`.** It keeps firing while a slow request is pending and in
  background tabs. A `setTimeout` chain armed after each request settles, paused when hidden,
  cannot pile up requests.
- **Local clock for ages.** A phone with a skewed clock would show negative or hours-off ages.
  Anchoring to the server's `Date` header costs one line.
- **Token in a cookie.** A cookie would be sent on every request automatically, including
  cross-site ones, which the epic's CSRF reasoning avoids. A `localStorage` token sent as a
  header is only sent by this page's own script.
- **A headless-Chromium test in the gate.** No browser dependency exists in the repo or on
  the dev host, and adding one is a larger decision than this slice. The injectable `createApp`
  with a fake DOM, run under `node` against real HTTP, covers the logic; layout is covered by
  static CSS checks plus a manual device-mode check.
- **Hand-written one-off server in this spec for the page.** The real server is the sibling
  spec's job; this spec ships only a test stub that implements the contract above, in
  `tests/`, never in the package.

## Acceptance criteria

- [x] `assets.read_asset("/")`, `("/app.js")` and `("/app.css")` return the shipped bytes with
      the content types of Decision 10; every other path (`"/index.html"`, `"/../x"`,
      `"/app.js?x=1"`, `"/static/app.js"`, `""`) returns `None`. `assets.CSP` equals the string
      of Decision 9, and the three files are present in a built wheel.
- [x] Static checks pass: `app.js` contains none of `innerHTML`, `outerHTML`,
      `insertAdjacentHTML`, `document.write`, `eval(`, `new Function`, `EventSource`,
      `setInterval`, `XMLHttpRequest`, `setAttribute("style"`, `setAttribute('style'`, or a
      `setAttribute` call whose first argument is `href`, `src` or `action` (single or double
      quoted); none of the three files contains `http://` or `https://`
      or a protocol-relative `//host` URL; `index.html` has no inline `<script>` body, no inline
      `<style>`, no `style=` attribute and no `on…=` attribute, and carries the viewport meta.
- [x] Against the real snapshot builder on the SPEC-0401 fixture, the page logic renders: the
      `spend today $X.XX` value equal to `totals.spend_today_usd`; the counts line equal to
      the CLI's counts line; one card per `active` row with `$cost / $cap` and a progress bar
      whose `aria-valuenow` is `min(100, round(cost/cap·100))`; the fixture's open `ESC-0001`
      and `main_dirty` park under "Needs you"; the health rows; and at most 20 recent events,
      newest first.
- [x] A snapshot whose values contain markup (`<img src=x onerror=…>` in a label, a question
      and an event summary) renders that text literally: the fake DOM records no element
      created from it, and its `innerHTML` setter is never called (it throws if it is).
- [x] Polling: the first request is sent at start with no `If-None-Match`; subsequent requests
      send `If-None-Match: "<rev>"`; a `304` leaves the rendered tree unchanged; a `200` with a
      new `rev` re-renders; requests are 15 000 ms apart on the injected clock; there is never
      more than one request in flight.
- [x] Visibility: while hidden no request is made and no timer is armed; on `visibilitychange`
      to visible one request is sent immediately and the 15 s timer is re-armed after it
      settles; hiding again cancels both the poll and the age timers.
- [x] Ages: with `generated_at = T` and `age_s = 10`, a response whose `Date` is `T + 5 s`
      displays 15 s, and 20 s of injected clock later displays 35 s with no new request; a
      `Date` header earlier than `generated_at` never displays a negative age; a missing `Date`
      falls back to the local clock.
- [x] Banners and dot: `401` shows the sign-in banner, `403` the not-allowed banner, a thrown
      fetch the unreachable banner with the last snapshot kept; 46 s without a `200`/`304`
      shows the stale banner and a grey dot; the next `200`/`304` clears the banner. `healthLevel`
      returns `green`, `amber`, `red` and `grey` for the fixtures of Decision 7, including the
      error-section cases, and a `{"error": …}` section renders as one muted line while the
      other sections still render.
- [x] Token handling: a start URL with `#token=abc` stores `abc` in
      `localStorage["nw-dashboard-token"]`, clears the fragment through `history.replaceState`,
      and sends `Authorization: Bearer abc` on every request; with no stored token no
      `Authorization` header is sent; the token string appears in no DOM node.
- [x] Layout: the CSS has no `width`/`min-width` in `px` above 360 on any rule that applies at
      narrow widths, defines `overflow-wrap` on text rows, gives interactive elements
      `min-height: 44px`, and has a `prefers-color-scheme: dark` block. Manually, at 390×844 and
      360×740 the page has no horizontal scroll (Test plan, manual 2).
- [x] The page is read-only and adds no coupling: the Python tests perform only `GET`s, the
      package modules added by this spec (`assets.py`) are imported by no queue-side module
      (`SPEC-0401`'s import-graph test still passes with `assets` included), and serving the
      assets and snapshot through the stub leaves every file under the data dir byte-identical.
- [x] Every JS-side criterion above is exercised by a pytest file: `tests/test_dashboard_page_logic.py`
      runs `node --test tests/js/page_logic.test.js` and fails on a non-zero exit (with node's
      output) or a missing `node`; and `affected_tests.py --changed
      src/nightwright/agentic/dashboard/static/app.js` selects that file and
      `tests/test_dashboard_page.py` (or the full suite).
- [x] `python3 tools/spec_lint.py` exits 0, and the affected tests pass.

## Test plan

**Automated tests.** Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps. The
JS tests need `node` (the dev host and the worker box have it); a missing `node` fails the
test with a message naming the requirement, it is not skipped.

- `tests/test_dashboard_assets.py` (unit):
  - `read_asset` allow/deny table of the first acceptance criterion; `CSP` string; each
    file's bytes are non-empty and its content type matches;
  - the static checks of the second criterion over the three files (forbidden tokens, no
    external URLs, no inline script/style/handlers, viewport meta);
  - the layout CSS checks of the layout criterion (parse the stylesheet with a small regex
    pass: px widths, `overflow-wrap`, `min-height: 44px`, dark-mode block);
  - packaging: build a wheel into a temp dir (`uv build --wheel` in a subprocess) and assert
    the three files are in it; if hatch omits them the test fails and the fix is to add the
    include to `pyproject.toml`.
- `tests/js/fakedom.js` (test support): a minimal DOM (`createElement`, `createTextNode`,
  `appendChild`, `replaceChildren`, `setAttribute`, `classList`, `getElementById`,
  `textContent`) whose `innerHTML`, `outerHTML` and `insertAdjacentHTML` throw, and which
  records every element created, so the markup-literal criterion can assert on it.
- `tests/js/page_logic.test.js`, run by `node --test` (from pytest, see the next item): drives `createApp` with an injected
  clock, `setTimeout` queue, visibility toggle, in-memory storage and a scripted `fetch`:
  - polling cadence, `If-None-Match`, `304` vs `200`, one request in flight;
  - visibility pause/resume; the age tick and the `Date`-header age arithmetic (including the
    earlier-than-`generated_at` and missing-`Date` cases);
  - banners, stale at 45 s, and `healthLevel` across all four levels and the error sections;
  - token fragment → storage → `Authorization`, fragment cleared, token absent from the DOM;
  - markup-literal rendering with the hostile snapshot.
- `tests/test_dashboard_page_logic.py` (the gate's entry to the node tests; pytest does not run
  `.js` files): runs `node --test tests/js/page_logic.test.js` in a subprocess with its timeout
  from `tests/waits.py` (`WAIT_S`) and fails on a non-zero exit with node's stdout/stderr in the
  assertion message; a missing `node` binary fails it with a message naming the requirement. It
  imports `nightwright.agentic.dashboard.assets` and names `app.js`, `fakedom.js` and
  `page_logic.test.js` in string constants, so `affected_tests.py` selects it both when
  `assets.py` changes (import graph) and when a file under `static/` or `tests/js/` changes
  (SPEC-0357 reference scan of non-Python files, which matches on basename; with no referencing
  test it would fall back to the full suite, never to no tests). A test in this file asserts
  `affected_tests.py --changed src/nightwright/agentic/dashboard/static/app.js --json` selects
  both node-driving files (this one and `tests/test_dashboard_page.py`) or the full suite, so
  an edit to `app.js` alone can never select none.
- `tests/test_dashboard_page.py` (integration, real sockets, real builder; also imports
  `assets` and names `app.js` and `page_http.test.js` in string constants):
  - starts a stub `ThreadingHTTPServer` defined in the test file that implements exactly the
    "Contract with the server" section: `assets.read_asset` for the three routes with the
    CSP header, and `/api/snapshot` from a real `SnapshotCache` over the SPEC-0401 fixture
    (`tests/fixtures/dashboard_data.py`) with `ETag`/`304` and a toggle for `401`/`403`;
  - runs `node tests/js/page_http.test.js <port>` as a subprocess. That script uses node's
    real `fetch` against the stub as the page's `fetch`, drives `createApp` through a start,
    a `304` poll, a ledger change on the fixture (append a cost change to the active
    campaign's ledger; advance the `SnapshotCache` clock past `refresh_s`) followed by a poll
    that re-renders the new cost, and the `401`/`403` toggles; it prints a JSON summary the
    pytest asserts on (spend equals `dashboard_payload`'s, `ESC-0001` listed, the new cost
    shown);
  - asserts the CSP, `nosniff` and content-type headers on the three asset responses, `404`
    on any other path, and that only `GET`s reached the stub;
  - hashes every file under the data dir before and after and asserts them identical (the
    read-only invariant, over HTTP for the page's requests).
- `tests/test_dashboard_imports.py` (extended): the module list gains `agentic/dashboard/
  assets.py` as a *dashboard* module that no queue-side module imports (the existing assertion
  already covers the package; add a case that `assets` is not in `sys.modules` after importing
  the queue-side modules).
- **Regression guard:** SPEC-0401's five dashboard test files, the TUI escalation tests and
  the `dashboard_payload` tests run unchanged (`python3 tools/affected_tests.py --run uv run
  pytest -q`).

**Manual verification (recorded in this spec's As shipped when performed).**
1. With the dashboard server spec landed: `uv run nw serve --dashboard`, open the printed
   URL on the phone over Tailscale. The page loads, the dot and the counts match `uv run nw
   dashboard snapshot` at the same moment, "Needs you" lists what `nw dashboard snapshot`
   lists, and changing the queue (park a campaign) shows on the phone within about 20 s
   without a reload. Locking the phone for a minute and unlocking refreshes at once.
2. In a desktop browser's device mode at 390×844 and 360×740 (and a real phone if available),
   with the test stub server (`NW_DASHBOARD_STUB_HOLD=1 uv run pytest -q -n 0
   tests/test_dashboard_page.py -k manual_serve` starts it on a fixed port, prints the URL and
   keeps it up until interrupted; the test does nothing without that variable) check: no horizontal scroll, cards readable, tap targets comfortable, dark mode
   follows the OS setting, and the 401/403/offline banners appear when the stub is toggled or
   stopped.
3. Before the server spec lands, only manual 2 is possible; mark manual 1 pending in
   As shipped and complete it in the server spec's campaign.

### Test record (2026-11-04)

- `uv run pytest -q -n 0 tests/test_dashboard_assets.py tests/test_dashboard_page_logic.py
  tests/test_dashboard_packaging.py tests/test_dashboard_imports.py tests/test_dashboard_page.py`:
  all pass. `node --test tests/js/page_logic.test.js` (13 tests, the real-snapshot one runs
  through the pytest wrapper): all pass. The wheel built with `uv build --wheel --offline` holds
  `assets.py` and the three `static/` files, so `pyproject.toml` needed no include.
- `uv run pytest -q -n 4` over the new files plus `tests/test_specs.py`, `test_test_waits.py`,
  `test_affected_tests.py` and SPEC-0401's five dashboard test files: 191 passed, 1 skipped (the
  manual stub server).
- `python3 tools/affected_tests.py --changed .../static/app.js --json` selects
  `tests/test_dashboard_page.py` (reference scan), and `assets.py` selects it by import. The
  `--run` selection for the whole change is ~110 files and did not finish inside this run's time
  limit; the full affected run and the suite are left to gating.
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: exit 0.
- Manual 2 (partial, headless Chromium 1208 from the Playwright cache against the test stub):
  the page loads under the CSP, renders the fixture, and shows no horizontal scroll at 360×740
  and 390×844 (screenshots read by eye). Not checked in a browser: dark mode, tap targets and
  the 401/403/offline banners (the CSS rules and the banner logic are covered by the static and
  node tests). Manual 1 is pending the dashboard server spec.

### As shipped (2026-11-04)

Built to Design. Details the Design left open or that differ in form:

- `assets.ROUTES` maps a path to `(file name, content type)`; `read_asset` returns `(bytes,
  content_type)` or `None` as specified.
- `createApp(env)` takes the visibility accessor as `env.visibility()` (returns `"visible"` or
  `"hidden"`) and registers `visibilitychange` on `env.document`. `app.idle()` (the in-flight
  request, or a resolved promise) exists for tests.
- The health dot counts "needs you" from `inbox.escalations` + `inbox.parks`, the lists the
  page shows. `conn` is `ok | stale | error | denied | none`; `none` with no snapshot is grey.
- The stale rule measures from the later of the last `200`/`304` and the moment the tab last
  became visible, so a phone unlocked after a long hide does not flash "stale" before its first
  poll settles. On `error`/`denied` the stale banner never replaces their own banner (priority
  order of Decision 6), the dot is grey in all three cases.
- The page has no request timeout: a request that never settles blocks later polls (the stale
  banner then shows). Not in the spec; see the follow-up idea in the campaign verdict.
- The stub's control route (`GET /__test/<cmd>`) is a test-only addition to the Contract
  stub, used by `page_http.test.js` to change the fixture and toggle `401`/`403`; the
  read-only test runs a scenario without the change and hashes the data dir.
- The "ledger change" in `tests/test_dashboard_page.py` writes the active campaign's
  `state.json` cost and appends a `cost` ledger event (the queue's own pair).

## Rollout / migration

1. Land `assets.py`, `static/`, the tests and the `pyproject.toml` include (if the packaging
   test demands it) in one change. No data migration: nothing is written, and the page has no
   effect until a server routes to `assets.ROUTES`.
2. The dashboard server spec's implementation serves the page through `read_asset` and sets
   the headers of the contract. Until it lands, `nw dashboard snapshot` remains the way to
   see the dashboard from an SSH session.
3. Add the server spec's id to this spec's `depends_on` when that spec exists (Context).
4. Epic child 3 adds SSE in front of the same snapshot; the poll in this page stays as the
   fallback, so no change to this spec's behavior is needed then.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the new tests and the affected tests pass; manual step 2 performed
      and noted (and step 1 once the server spec has landed, or noted as pending there).
- [x] No regressions (SPEC-0401 dashboard tests, TUI escalation tests and `dashboard_payload`
      tests green).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._
