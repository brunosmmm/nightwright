---
id: SPEC-0402
title: "Live snapshot stream over SSE with polling fallback and load guard"
status: in-progress
owner: user
created: 2026-11-03
updated: 2026-11-04
milestone: "M9: agentic workflow"
source_idea: IDEA-876
kind: feature
parent: SPEC-0400
depends_on: [SPEC-0401, SPEC-0413, SPEC-0414]
tags: [agentic, dashboard, remote, sse, web]
---

## Context

Child of epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md) (remote web
dashboard over Tailscale). It depends only on child 1, the read-only server and the
`nw.dashboard.v1` snapshot. Child 1 ships `nw serve --dashboard`, the bind and identity gates, a
shared watermark-driven snapshot cache, `GET /api/snapshot` with an ETag, and a phone-first page
that polls conditionally every 15 s. This spec adds the "live" half of the epic's G1: the page
updates by itself within a few seconds of a queue change, without every client recomputing
anything.

**Scope note.** The source idea (IDEA-876) bundled three subsystems: the snapshot stream, a
per-campaign drilldown, and a live run tail. A review found that too large for one campaign, so
this spec is narrowed to the first: the snapshot stream, the page's live client, and the load
guard. The drilldown and the run tail are listed under Non-goals and become their own specs under
SPEC-0400 (not yet created; the run tail will depend on this spec and on the drilldown).

What already exists and is reused (checked against the code on 2026-11-03):

- **The lesson of IDEA-008** (comfort-assistant, shipped): a web UI that rescanned a whole table
  on every client's 5 s poll. The remedy, fixed in the epic's Decision 5, is to compute one
  snapshot server side, only when something changed, and share it. Child 1 builds the cache; this
  spec adds the push on top of it and a test that proves the load shape.
- **The stdlib server's shape** (SPEC-0334, child 1): a `ThreadingHTTPServer`, one thread per
  connection, so a stream is a handler thread that blocks, which is fine for at most
  `max_clients` connections. Child 1 sets a per-request socket timeout, which streams keep as
  their send timeout.
- **Child 1's auth path**: whois with a 60 s per-peer cache on tailnet binds, the local bearer
  token on loopback binds, and the Host/Origin checks.

Two constraints from the browser side shape the design. First, `EventSource` cannot send an
`Authorization` header, and loopback binds require `Authorization: Bearer <local token>`;
putting the token in a URL would leak it into logs and history. Second, mobile Safari drops
background connections, so reconnect has to be a first-class path rather than an error path.

## Goals / Non-goals

**Goals**
- G1. `GET /api/stream`: Server-Sent Events carrying the shared `nw.dashboard.v1` snapshot, sent
  to a client only when `rev` changed, with a 15 s heartbeat, a `max_clients` cap, and no queue
  work at all while nobody is connected.
- G2. The page uses the stream when it can, reconnects on `visibilitychange`, and falls back to
  child 1's conditional polling when the stream is unavailable, so it keeps working on networks
  that drop SSE.
- G3. A load guard test that pins the IDEA-008 lesson: server work does not grow with the number
  of clients.
- G4. A stream-slot counter (`max_clients`) that is not specific to snapshot streams, so later
  stream types can share it.

**Non-goals**
- **Per-campaign drilldown** (`GET /api/campaigns/<cid>`, `nw.dashboard.campaign.v1`, the
  `#/c/<cid>` view, its memo, cid validation and text filtering). Separate spec under SPEC-0400.
- **Live run tail** (`GET /api/campaigns/<cid>/tail`, `tail.run_ended` / `tail.iter_run`, the
  tail panel, control-character stripping and the XSS test). Separate spec under SPEC-0400; it
  will acquire its slots from the counter built here. This spec makes no change to `tail.py`.
- Any write. Streams are read-only. The decisions inbox and answers are child 2.
- WebSocket or HTTP/2. SSE over HTTP/1.x from the stdlib server is enough for one human.
- Auditing reads. `audit.jsonl` stays a write/refusal log; a refused stream request (403) is
  already audited by child 1's auth path.
- Service worker, push, and the systemd unit (children 4 and 5).

## Decision

This spec builds on SPEC-0401's server, snapshot cache and auth gates, and implementation must not start before SPEC-0401 is done.

1. **One shared fan-out, no per-client work.** A `Hub` in
   `src/nightwright/agentic/dashboard/stream.py` sits on child 1's snapshot cache. A single
   snapshotter thread exists only while at least one `/api/stream` client is connected. Each
   `refresh_s` it asks the cache for the current snapshot (the cache itself recomputes only when
   the watermark moved or `max_stale_s` passed), compares `rev` with the last one it published,
   and on a change hands the snapshot to every subscriber. Writing to sockets is done by each
   client's own handler thread, never by the snapshotter, so a slow phone cannot stall the
   others.
2. **Wire format.** `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-store`,
   `X-Accel-Buffering: no`, HTTP/1.0-style (no `Content-Length`; the connection closing ends the
   stream). The stream starts with `retry: 3000`. A snapshot is
   `event: snapshot` / `id: <rev>` / `data: <compact json>`. A heartbeat is the comment line
   `: hb` every 15 s. A client that supplies `?rev=<rev>` equal to the current snapshot's rev
   gets no initial event, only heartbeats until the next change; otherwise it gets the current
   snapshot immediately on connect.
3. **Auth by header, not by URL.** The page reads the stream with `fetch()` and a small
   `ReadableStream` SSE parser, not `EventSource`, so loopback binds send the same
   `Authorization` header as every other call and no token ever appears in a URL. Tailnet binds
   rely on whois as for every request. An open stream re-runs the whois check on each heartbeat
   tick; the check goes through child 1's per-peer cache (60 s TTL), so a denial is noticed at
   the first heartbeat tick after the peer's cache entry has expired. A node removed from the
   tailnet or the allow list therefore loses its stream within the whois TTL plus one heartbeat
   (at most 60 s + 15 s with the defaults). The stream closes itself on denial.
4. **`max_clients` counts every open stream** through one slot counter (`SlotPool`) under a lock.
   Over the cap the server answers `503` with `Retry-After: 5` before any stream headers are
   sent, and the page stays on polling. This spec's only users of the pool are snapshot streams;
   the pool's `acquire` / `release` interface is what the later run-tail spec will use. Only
   snapshot streams wake the snapshotter.
5. **Polling is kept.** Child 1's `GET /api/snapshot` with `If-None-Match` stays the fallback and
   is not changed. The page switches to it (every 15 s) when the stream errors, answers 503 or
   delivers nothing for 45 s (three missed heartbeats), retries the stream with backoff from 2 s
   to 30 s, and shows a "live" / "polling" indicator.
6. **Reconnect on `visibilitychange`.** When the page becomes hidden it closes its stream; when
   it becomes visible it fetches `/api/snapshot` conditionally at once and reopens the stream
   with `?rev=` of the snapshot it holds. A hidden tab therefore holds no client slot.
7. **A slow client skips, never queues.** Each subscriber has a one-slot mailbox holding only the
   latest unsent snapshot. A client that stops reading is dropped by the per-request send
   timeout once its socket buffers are full; until then it simply lags and other clients are
   unaffected.

## Design

### Modules

- `src/nightwright/agentic/dashboard/stream.py` (new): `Hub`, `Subscriber`, `SlotPool`, and the
  SSE writers (`write_event`, `write_comment`).
- `src/nightwright/agentic/dashboard/server.py` (child 1): the `GET /api/stream` route, the
  slot accounting, and the extra response-header handling for streams.
- `src/nightwright/agentic/dashboard/static/{index.html,app.js,app.css}`: the stream client and
  the indicator.

The queue modules never import any of this (the epic's invariant 2). Nothing outside
`agentic/dashboard/` changes.

### The hub (`stream.py`)

```
class SlotPool:
    def __init__(self, max_clients): ...
    def acquire(self) -> bool       # False when at the cap
    def release(self) -> None

class Hub:
    def __init__(self, cache, pool, refresh_s, clock=time.monotonic): ...
    def subscribe(self, known_rev) -> Subscriber | None    # None when the pool is full
    def unsubscribe(self, sub) -> None
    def stats(self) -> {snapshot_clients, builds_published}
```

- `subscribe` acquires a slot, registers a `Subscriber` (a one-slot mailbox: the latest snapshot
  not yet written, plus a `threading.Event`). The first subscriber starts the snapshotter thread
  (daemon, named `nw-dashboard-snapshotter`); `unsubscribe` releases the slot, and when the last
  subscriber leaves the thread exits after its current wait. Zero subscribers means zero ticks
  and zero cache calls.
- Snapshotter loop: `wait(refresh_s)` on a stop event, `snap = cache.current()` (child 1's
  accessor, which stats the watermark and rebuilds only when it moved or `max_stale_s` passed),
  and if `snap.rev != last_rev` set `last_rev` and put `snap` into every mailbox (replacing an
  unsent older one). A failure of the cache call is logged to stderr and retried next tick; it
  never kills the thread or the server.
- A new subscriber's initial snapshot comes from `cache.current()` (not a forced rebuild), so a
  burst of reconnects after a phone wakes costs at most the one rebuild the cache already allows.
- The client handler loop: write `retry:`; write the initial snapshot unless `known_rev ==
  snap.rev`; then loop `sub.event.wait(heartbeat_s)`: on a snapshot, `write_event`; on timeout,
  `write_comment("hb")` and re-check auth (Decision 3). Any socket error (including the
  per-request send timeout, 10 s by default) or auth denial exits the loop, unsubscribes in a
  `finally`, and closes.
- The heartbeat interval (15 s), the send timeout (10 s) and the whois cache TTL (60 s) are
  constants with injectable overrides used only by tests.

### The page (`static/`)

- `app.js` gets a `Live` object: `start()` opens `/api/stream?rev=<last>` with `fetch()` and an
  SSE line parser (handles `event:`, `id:`, `data:`, comments, split chunks, CRLF, `retry:`);
  feeds the same render function that child 1's poll uses; maintains the indicator ("live",
  "polling", "reconnecting"); implements the 45 s no-data watchdog, the backoff, and the
  `visibilitychange` handling of Decisions 5 and 6. Polling and streaming write through one
  `applySnapshot(snap)` that ignores a snapshot whose `rev` it already shows, so the two paths
  cannot fight.
- Every control is at least 44 px tall. Child 1's `?selftest=1` is extended to report the
  indicator state (`live` / `polling`) and whether the stream connected.

### Config

No new keys. The feature uses the epic's `agentic.dashboard` keys: `refresh_s`, `max_stale_s`,
`max_clients`. The 15 s heartbeat, the 10 s send timeout and the 45 s page watchdog are
constants in the code. Values that are not positive numbers fall back to the defaults, as in
the epic.

### Read-only and isolation

The route is a `GET`, calls no function that writes (`events.sync`, `events.follow`,
`store.write_state`, `mailbox.send`, any `queue` mutation), and writes nothing outside the
socket. Nothing in `queue`, `runner`, `runproc`, `events` or `escalations` imports the dashboard.

## Alternatives considered

- **`EventSource` in the page.** Simplest client, built-in reconnect, but it cannot send the
  loopback bearer header; the alternatives were a token in the query string (leaks into logs,
  history and referrers) or an auth cookie (a new CSRF surface for no benefit on the tailnet).
  A 40-line `fetch` stream parser keeps one auth path.
- **A per-client thread that polls and rebuilds.** This is the IDEA-008 failure: cost grows with
  clients. Rejected in the epic; the load guard test pins the replacement.
- **A snapshotter thread that always runs.** Simpler, but it would read the queue with nobody
  watching, which the epic's G5 and acceptance criteria forbid.
- **WebSocket.** Bidirectional, but the stdlib has no server for it, the page only reads here,
  and it would need a new dependency or a hand-written framing layer.
- **Bypassing the whois cache for open streams** (re-check on every heartbeat). Tighter
  revocation, but it turns N streams into N whois subprocesses every 15 s. The 60 s bound is
  enough for one human's own devices.

## Acceptance criteria

- [x] `GET /api/stream` answers `text/event-stream`, sends a `snapshot` event with `id` equal to
      the snapshot `rev` on connect, sends another only when `rev` changed (a ledger or
      escalation change), and sends a `: hb` comment at least every 15 s. A client connecting
      with `?rev=` equal to the current rev gets no initial snapshot event.
- [x] The snapshotter thread exists only while at least one `/api/stream` client is connected;
      with zero clients the server performs zero snapshot builds and zero watermark stats, and
      the thread is gone within `2 × refresh_s` after the last client leaves.
- [x] With N snapshot clients (N = 1 and N = 8) and an unchanging watermark, builds over a fixed
      window are at most `window / max_stale_s + 1` and the same for N = 1 and N = 8; after one
      watermark change every client receives exactly one new event and exactly one build
      happened. `queue.dashboard_payload` is never called from a request or stream handler, only
      by the snapshot builder.
- [x] A client that stops reading does not delay events to other clients; once its socket send
      buffer is full, its handler exits within the send timeout and frees its slot.
- [x] Opening a stream beyond `max_clients` answers 503 with `Retry-After` before any stream
      headers, `/api/snapshot` polling still works, and a slot is released whenever a stream
      ends for any reason.
- [ ] Streams require the same authentication as every other API call (401 on loopback without
      the token, 403 on a denied tailnet peer) and no token is put in a URL. An open stream is
      closed at the first heartbeat tick after the whois cache entry for its peer expires and
      whois denies it (at most whois TTL + 15 s).
- [ ] The page uses the stream when available, updates without reload, shows "live", falls back
      to 15 s conditional polling when the stream fails, answers 503 or is silent for 45 s, and
      recovers; a snapshot with an already-shown `rev` is ignored. The page closes its stream
      when hidden and, when visible again, refreshes at once and reopens it with `?rev=`, so a
      hidden tab holds no client slot. In a headless mobile viewport (390×844 and 360×740) the
      self-test reports the stream connected and "live", there is no horizontal overflow and all
      controls are at least 44 px tall; with the stream route answering 503 it reports "polling"
      and still renders the snapshot.
- [x] Serving streams leaves every file under the data dir except `agentic/dashboard/`
      byte-identical, and no queue, runner, `runproc`, `events` or `escalations` module imports
      `agentic.dashboard`.

## Test plan

Real sockets and real processes where the behavior depends on them, per the epic's Test plan
(fakes alone have shipped broken integrations in this repo). Waits use `tests/waits.py`; there
is no fixed sleep before a positive assertion.

- **Automated tests**
  - `tests/test_dashboard_stream.py` — in-process server (`make_server(cfg, "127.0.0.1:0")` on a
    thread, real sockets, loopback token) over a seeded temp data dir, with the snapshot builder
    wrapped by a counter and `refresh_s` / `max_stale_s` shortened through the data dir's
    config:
    - SSE framing: content type, `retry:`, first `snapshot` event with `id == rev`, JSON parses
      and matches `nw.dashboard.v1`; `?rev=` equal to current gives no initial event; a ledger
      append produces one new event with a new `rev`; a heartbeat comment arrives (the heartbeat
      interval is injected short).
    - Load guard (IDEA-008): zero clients → builds and watermark stats stay 0 over several ticks
      and no `nw-dashboard-snapshotter` thread exists; 1 vs 8 clients with a constant watermark
      → equal build counts within `window / max_stale_s + 1`; one appended event → exactly one
      build and each of the 8 clients receives exactly one new event; all clients disconnect →
      thread gone within `2 × refresh_s`; `queue.dashboard_payload` call count equals build
      count (no per-request calls).
    - Backpressure: the non-reading client sets a small `SO_RCVBUF` and never reads; the send
      timeout is shortened through injection, and the test publishes snapshots large enough (or
      enough of them, by changing the seeded ledger repeatedly with a padded field) to fill the
      kernel send and receive buffers before asserting. A second, reading client receives every
      event without added delay meanwhile, and the slot count (`Hub.stats()` / pool) drops back
      within the shortened send timeout after the buffers are full.
    - Cap: `max_clients=2` → the third stream gets 503 with `Retry-After` and no event-stream
      headers; `/api/snapshot` still 200; after one stream closes (client disconnect and server
      side auth denial both), a new stream is accepted.
    - Auth: loopback stream without the token → 401; with it → 200; no token in any request URL
      the page code builds (checked on the page's request builder). A fake `tailscale` on `PATH`
      (as in the epic) with an injected peer: allowed login streams; the whois cache TTL and the
      heartbeat are shortened through injection, the fixture is switched to deny, and the test
      asserts the stream is closed within one shortened TTL + heartbeat (and not before the
      cache entry expired); denied / unknown / tagged → 403.
    - Static JS check: the SSE line parser extracted from `app.js` is exercised under Node when
      `node` is on `PATH` (split chunks, multi-line `data:`, comments, `retry:`, CRLF); skipped
      when Node is absent, since the headless-browser test covers the parser end to end.
  - `tests/test_dashboard_e2e.py` (extends the epic's file) — start `nw serve --dashboard
    --bind 127.0.0.1:0` as a subprocess against a seeded data dir, read the port from
    `server.json`; over real HTTP with `urllib`: `/api/stream` delivers a `snapshot` and a second
    one after a ledger change.
  - `tests/test_dashboard_readonly.py` (extends child 1's guard) — after the streams above, a
    hash of every file in the data dir except `agentic/dashboard/` is unchanged; the import-graph
    test still passes with `stream.py`.
  - `tests/test_dashboard_mobile.py` (extends child 1's headless test; same browser discovery
    and skip rule, and this spec's Definition of done requires the run on this host) — load
    `?selftest=1` at 390×844 and 360×740 against a server with a seeded campaign; the page
    self-test writes into the DOM, read via `--dump-dom`: no horizontal overflow, all controls ≥
    44 px, stream connected and indicator "live". A second run with the stream route made to
    answer 503 shows the "polling" indicator and still renders the snapshot. A visibility test
    in the same page dispatches a hidden then visible `visibilitychange` and the self-test
    records that the stream closed (server-side slot count 0) and reopened.
- **Manual verification** (human, real tailnet; recorded in this spec's As shipped when done):
  1. Open the dashboard on the phone over cellular with the Tailscale app connected; confirm the
     indicator reads "live" and a queue change (e.g. a campaign changing state) appears within a
     few seconds without reloading.
  2. Lock the phone for a minute, unlock it: the page refreshes at once and the indicator returns
     to "live" without a manual reload.
  3. Turn off the phone's Tailscale connection mid-stream: the indicator leaves "live"; turning
     it back on returns to "live".
- **Regression guard**
  - The affected-tests run (`python3 tools/affected_tests.py --run uv run pytest -q`) plus the
    files above.
  - The import-graph and read-only tests above guard the epic's invariants 1 and 2.
  - `python3 tools/spec_lint.py` exits 0 and the ledger is regenerated.

### Test record (2026-11-04)

Ran `uv run pytest -q tests/test_dashboard_stream.py tests/test_dashboard_e2e.py` (17 passed, three
consecutive runs) and the related dashboard tests (`test_dashboard_server`, `_http_readonly`,
`_imports`, `_readonly`, `_serve_cli`, `_auth`, `_cache`: 150 passed, 1 skipped), plus
`python3 tools/spec_lint.py`. The full suite is left to gating. Covered: SSE framing and `?rev=`
gating, the load guard (zero clients, N = 1 and 8, one change = one build and one event per
client, thread gone after the last client leaves, `Q.dashboard_payload` calls equal builds),
backpressure with a non-reading client, the cap and slot release, 401/403, the heartbeat auth
re-check, read-only, and a real `nw serve --dashboard` subprocess.

Not done (tests not written because the code they test does not exist): the Node check of the
page's SSE parser, `tests/test_dashboard_mobile.py`, the tailnet whois/fake-`tailscale` test, and
the manual phone steps. Acceptance criteria 6 (whois half) and 7 (the page) are therefore left
unticked and the status stays `in-progress`.

### As shipped (2026-11-04, partial)

- SPEC-0414 shipped only the loopback server with bearer-token auth. It has no tailnet bind, no
  whois check and no page (`static/`, `?selftest=1`, the 15 s poll). This spec's whois-expiry
  behaviour and its page / Live client / indicator / visibility handling were therefore not built.
- The cache accessor is `SnapshotCache.get()` (not `current()`); the hub calls it.
- `stream.py` has `SlotPool`, `Hub`, `Subscriber`, the SSE writers, `peer_gone` and
  `max_clients(cfg)` (the `settings()` validator in `__init__.py` is untouched, so the key is read
  directly; default 8).
- `server.py`: route `GET /api/stream?rev=`, `DashboardServer.hub`, `heartbeat_s` and
  `send_timeout_s` (injectable), and `stream_auth_ok(handler)`, the hook re-run on every heartbeat
  that closes the stream when it returns false. The later tailnet slice should plug its cached
  whois check in there. The stream body reuses the server's per-`rev` encoded snapshot (not
  re-compacted).
- A stream handler also detects a client that closed its socket (`peer_gone`, checked each
  `refresh_s` slice) so the slot is freed and the snapshotter stops promptly instead of at the
  next heartbeat write; the spec's "gone within `2 × refresh_s`" needs this.

### Test record (2026-11-04, round 2)

Ran `uv run pytest -q tests/test_dashboard_stream.py` (19 passed) and, with the related dashboard
tests (`test_dashboard_e2e`, `_server`, `_http_readonly`, `_imports`, `_readonly`, `_serve_cli`,
`_auth`, `_cache`), all green. Mutation-checked: dropping the stream's `settimeout(send_timeout_s)`
fails the backpressure test, and dropping the `try/except` in `Hub._run` fails the new
snapshotter-resilience test. The full suite is left to gating.

New in this round: the backpressure test now measures from the first blocked event write (a spy on
`write_event`) and requires the drop within `send_timeout_s` + 2 s; a builder that fails for a few
ticks and then recovers (the snapshotter survives, the client gets the later event, one stderr
line); N = 1 vs N = 8 build and watermark-check counts compared in one test; thread-exit bound
tightened to `2 × refresh_s + 0.5`; `max_clients` set through the seeded config; `server_close()`
ends open streams and frees their slots; auth re-check on a stream whose snapshots arrive faster
than the heartbeat; a stale initial offer cannot replace a newer delivered snapshot.

Still not done, because the code they test does not exist on this branch or on `main` (SPEC-0414
shipped a loopback server only: no tailnet bind, no whois, no `static/` page): the fake-`tailscale`
whois test, the Node check of the page's SSE parser, `tests/test_dashboard_mobile.py`, the manual
phone steps. This host also has no Chromium/Chrome (only Firefox, which has no `--dump-dom`).
Acceptance criteria 6 and 7 stay unticked and the status stays `in-progress`.

### As shipped (2026-11-04, round 2)

- `_Handler._stream` re-runs `stream_auth_ok` on its own `last_auth` clock every `heartbeat_s`,
  before writing, whether the iteration carries a snapshot or a heartbeat; a busy queue can no
  longer starve the revocation check.
- `Subscriber.offer(snap, initial=True)` (the subscribe-time cache read) is ignored once the
  snapshotter has delivered something; the snapshotter's repeated identical stderr error is
  logged at most once per 60 s (`ERROR_LOG_EVERY_S`).
- `Hub.__init__(cache, pool, refresh_s=None)` differs from the Design's signature: there is no
  `clock` parameter (tests drive real time with short `refresh_s`), and `refresh_s` defaults to
  the cache's own `refresh_s`.

### As shipped (2026-11-04, round 3: blocked on missing sibling specs)

Acceptance criteria 6 (whois half) and 7 stay unticked and the status stays `in-progress`. This
round built nothing new, deliberately:

- **The page does not belong to this spec's dependencies.** SPEC-0414 ("loopback server") names
  the phone page and the tailnet slice (own-address bind gate, `tailscale whois` identity, MagicDNS
  hosts, fake `tailscale` fixture) as sibling specs that do not exist yet in `docs/specs/`. This
  spec's Context assumes child 1 ships `static/`, the 15 s poll, `?selftest=1` and whois; on this
  branch and on `main` none of that exists. Building a polling page, a static route and a
  tailnet bind inside SPEC-0402 would write two other specs' scope under this one's id.
- **The headless test cannot run on this host** (no Chromium/Chrome; Firefox has no `--dump-dom`),
  and this spec's Definition of done requires `tests/test_dashboard_mobile.py` to run, not skip,
  here. So AC7 cannot be closed in this campaign even with the page built.
- The server half (AC1–5, AC8) is complete and covered with real sockets. The `stream_auth_ok`
  hook (re-run each heartbeat, closes the stream on denial) is the integration point for the
  tailnet slice's cached whois check; the fake-`tailscale` revocation test and the 403 for
  denied/unknown/tagged peers belong with that slice.
- Needed from the human: create the phone-page spec and the tailnet-slice spec under SPEC-0400,
  then revise SPEC-0402 to either depend on them (and on a host with Chromium) or move AC6's
  whois half and AC7 into a follow-up spec that does. Recorded as a campaign note.

### Update (2026-11-04, round 4: phone page now on main)

SPEC-0413 (the phone page) has merged and is now a dependency: `static/index.html`, `static/app.js`
and `static/app.css` under `src/nightwright/agentic/dashboard/` exist on `main`. The round 3 note
above that the page spec does not exist is historical; the page is no longer missing. The tailnet
slice (own-address bind, whois) is still not on `main`.

### As shipped (2026-11-04, round 5: the page's live client)

With SPEC-0413's page on `main`, the Live client is built in `static/app.js`; AC6's whois half and
AC7 are still open for the reasons below.

- `createSseParser(on)`: an incremental SSE parser (`event:`, `id:`, multi-line `data:`, comments,
  `retry:`, CRLF/CR, chunks split anywhere); exported for node.
- `createApp(env)` gains an opt-in `env.live` (+ `env.abort()`); the browser bootstrap sets it
  when `AbortController` and `TextDecoder` exist, so SPEC-0413's tests (no `env.live`) still
  see polling only. With it: `fetch("/api/stream?rev=<held>")` with the bearer header (never in the
  URL), a 45 s no-data watchdog (any bytes, heartbeats included, count as a successful check),
  backoff 2 s → 30 s doubling (reset by a good connect or a visible tab), polling paused while
  live and resumed at once when a live stream fails, and one `applySnapshot` that ignores an
  already-shown `rev` for polls and stream alike.
- Visibility: hidden aborts the stream and cancels the retry (no slot held); visible refreshes
  `/api/snapshot` conditionally, then reopens the stream with `?rev=`.
- Indicator: `<span id="live">` in the header (`index.html`, `app.css`): "live", "polling", or
  "reconnecting" (a retry in flight). `createApp(...)` returns `liveMode()` and `rev()` for tests.
- **Not built, and why.** (a) `?selftest=1` and the headless-browser test
  (`tests/test_dashboard_mobile.py`): this host has no Chromium/Chrome (only Firefox, no
  `--dump-dom`), and the Definition of done requires that run to happen here. (b) The server does
  not serve the page: `server.py` has no `/`, `/app.js` or `/app.css` route and never imports
  `assets`, and its auth gate rejects every request without the bearer header, which a browser's
  page load cannot send. Routing the assets (and deciding whether they are exempt from the token) is a
  server design decision outside this spec; until it lands the page cannot load from `nw serve
  --dashboard`. (c) The tailnet slice (whois, fake `tailscale`) does not exist on `main`.

### Test record (2026-11-04, round 5)

Ran `node --test tests/js/live.test.js` (15 passed) and `uv run pytest -q tests/test_dashboard_live.py
tests/test_dashboard_page_logic.py tests/test_dashboard_page.py tests/test_dashboard_assets.py
tests/test_dashboard_packaging.py` (all green; `test_dashboard_live.py` three consecutive runs).
`test_dashboard_live.py` also drives `tests/js/live_http.js`, which runs the page's real client code
with node's own `fetch` against a real `make_server` and the real snapshot builder: the page goes
"live", gets a second snapshot after a ledger append with no extra poll, hides (stream closed) and
reopens with `?rev=`, leaves no slot behind, and with `max_clients` exhausted by another stream falls
back to "polling" and still renders. The SSE parser (split chunks, multi-line `data:`, comments,
`retry:`, CRLF) is covered by the node tests. The full suite is left to gating.

## Rollout / migration

1. Land after child 1 of SPEC-0400 (the server, auth and snapshot cache). Reconcile the cache
   accessor name used above (`cache.current()`) and child 1's whois-cache TTL injection point
   with what child 1 shipped, and record the actual names in As shipped.
2. No data migration and no new config keys. The page keeps child 1's polling path, so an old
   cached page or a network that blocks SSE still works.
3. The drilldown and run-tail specs (split out of the source idea) build on `SlotPool`, the SSE
   writers and the page's SSE parser from this spec; children 2 and 4 are independent of it.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; the headless-browser test ran on this
      host (not skipped); manual steps performed and noted.
- [ ] No regressions (read-only and import-graph guards green).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

_None. Decided here: fetch-based SSE instead of `EventSource` (header auth), revocation bounded
by the whois cache TTL plus one heartbeat, a shared slot pool for all stream types, and the
drilldown and run tail split into their own specs._
