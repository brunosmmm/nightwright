---
id: SPEC-0402
title: "Live updates over SSE and per-campaign drilldown with run tail"
status: proposed
owner: user
created: 2026-11-03
milestone: "M9: agentic workflow"
source_idea: IDEA-876
kind: feature
parent: SPEC-0400
tags: [agentic, dashboard, remote, sse, tail, web]
---

## Context

Child of epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md) (remote web
dashboard over Tailscale). It is the epic's third child and depends only on child 1, the
read-only server and the `nw.dashboard.v1` snapshot. Child 1 ships `nw serve --dashboard`, the
bind and identity gates, a shared watermark-driven snapshot cache, `GET /api/snapshot` with an
ETag, and a phone-first page that polls conditionally every 15 s. This spec adds the "live" half
of the epic's G1: the page updates by itself within a few seconds, and the human can open one
campaign and watch a persona work.

What already exists and is reused (checked against the code on 2026-11-03):

- **Run transcripts (SPEC-0362).** `tail.read_lines(path, offset)` returns the complete lines
  after a byte offset and the new offset (it leaves a trailing fragment for the next read).
  `tail.Renderer.feed_line(line)` turns one stream-json line into rich `Text` objects, keeping
  state across lines (tool-use/result pairing, running cost against the run budget).
  `tail.tail()` is the CLI loop over them. It stops when `_ended(run_dir)` is true, which is
  `runproc.run_status` in `finished|vanished|orphaned` or a `run.json` present, and it handles
  a run that has no `stream.jsonl`. `tail.resolve(cfg, cid, run)` picks a run dir,
  `tail.running_runs` lists the live ones.
- **Per-campaign data.** `store.read_ledger(cfg, cid, tail=N)`, `runs.campaign_runs(cfg, cid)`,
  `decisions.read(cfg, campaign=cid)` (conductor decision records), `queue.row_view` (title,
  `cost_usd`, `cap_usd`, park reason for one campaign) and the `active` / `needs_you` /
  `conductor_handling` entries of the shared snapshot.
- **The lesson of IDEA-008** (comfort-assistant, shipped): a web UI that rescanned a whole table
  on every client's 5 s poll. The remedy, fixed in the epic's Decision 5, is to compute one
  snapshot server side, only when something changed, and share it. Child 1 builds the cache;
  this spec adds the push on top of it and a test that proves the load shape.
- **The stdlib server's shape** (SPEC-0334, child 1): a `ThreadingHTTPServer`, one thread per
  connection, so a stream is a handler thread that blocks, which is fine for at most
  `max_clients` connections.

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
- G3. A per-campaign drilldown (`GET /api/campaigns/<cid>`): campaign header with state, loop,
  activity and cost vs cap; ledger tail; runs; conductor decisions for the campaign; its
  escalations. It is rendered by the page at `#/c/<cid>`.
- G4. `GET /api/campaigns/<cid>/tail`: SSE of plain rendered transcript lines from the run's
  `stream.jsonl`, from a byte offset, live until the run ends, using `tail.Renderer` so the
  phone shows exactly what `nw campaign tail` shows.
- G5. A load guard test that pins the IDEA-008 lesson: server work does not grow with the number
  of clients.

**Non-goals**
- Any write. Streams and drilldowns are read-only. The decisions inbox and answers are child 2.
- WebSocket or HTTP/2. SSE over HTTP/1.x from the stdlib server is enough for one human.
- Following a campaign across runs on the server (what `tail.follow_campaign` does for herdr).
  The page re-checks the drilldown when a tail ends and opens the next run itself.
- Raw stream-json (`nw campaign tail --raw`) over the web. Only rendered plain lines.
- Redacting run output. The transcript can contain whatever tools printed. It is readable by the
  same allowed login on the host with `nw campaign tail`, so the web adds no new audience; the
  gate is the epic's Decision 2.
- Auditing reads. `audit.jsonl` stays a write/refusal log; a refused stream request (403) is
  already audited by child 1's auth path.
- Service worker, push, and the systemd unit (children 4 and 5).

## Decision

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
3. **Auth by header, not by URL.** The page reads streams with `fetch()` and a small
   `ReadableStream` SSE parser, not `EventSource`, so loopback binds send the same
   `Authorization` header as every other call and no token ever appears in a URL. Tailnet binds
   rely on whois as for every request. An open stream re-runs the (60 s cached) whois check on
   each heartbeat tick and closes itself on denial, so a node removed from the tailnet or the
   allow list loses its streams within about a minute.
4. **`max_clients` counts every open stream** (snapshot and tail). Over the cap the server answers
   `503` with `Retry-After: 5` and the page stays on polling. Only snapshot streams wake the
   snapshotter; a tail stream reads its own run's files and nothing else.
5. **Polling is kept.** Child 1's `GET /api/snapshot` with `If-None-Match` stays the fallback and
   is not changed. The page switches to it (every 15 s) when the stream errors, answers 503 or
   delivers nothing for 45 s (three missed heartbeats), retries the stream with backoff from 2 s
   to 30 s, and shows a "live" / "polling" indicator.
6. **Reconnect on `visibilitychange`.** When the page becomes hidden it closes its streams; when
   it becomes visible it fetches `/api/snapshot` conditionally at once and reopens the streams
   with `?rev=` and (for a tail) `offset=` of the last line it holds. A hidden tab therefore
   holds no client slot.
7. **Drilldown is a plain JSON GET, memoized.** `GET /api/campaigns/<cid>` builds
   `nw.dashboard.campaign.v1` from existing readers and carries an ETag (`If-None-Match` → 304).
   The page refetches it when a snapshot with a new `rev` arrives while the drilldown is open
   and the campaign's `events.jsonl`-driven activity may have changed; the 304 makes an
   unchanged refetch cheap. Builds are memoized per campaign on file stats so N clients cost one
   build.
8. **Tail replays from the start, emits from an offset.** The server always feeds the run's
   `stream.jsonl` into a fresh `tail.Renderer` from byte 0, so tool pairing and the running cost
   are correct, but only sends lines from the requested `offset`. Without `offset` it sends the
   last 200 rendered lines of the backlog. Each event carries the byte offset after it, which is
   the client's resume point after a reconnect. The tail ends with an `end` event when the run
   ends (same `_ended` rule as `nw campaign tail`), after one more read and the same final
   fragment handling.
9. **Run output is untrusted text.** The server strips control characters (C0 except tab, and
   DEL, so ANSI escapes cannot reach the phone), and the page sets text with `textContent`
   only, never `innerHTML`.

## Design

### Modules

- `src/nightwright/agentic/dashboard/stream.py` (new): `Hub`, `Subscriber`, the SSE writers
  (`write_event`, `write_comment`) and the tail event generator wiring.
- `src/nightwright/agentic/dashboard/server.py` (child 1): routes `GET /api/stream`,
  `GET /api/campaigns/<cid>` and `GET /api/campaigns/<cid>/tail`, the shared stream-slot
  accounting, and the extra response header handling for streams.
- `src/nightwright/agentic/dashboard/campaign.py` (new): `campaign_payload(cfg, cid, snap)` and
  its stat-keyed memo.
- `src/nightwright/agentic/tail.py`: two small additive changes, so the CLI and the web share one
  implementation instead of forking it. (a) `_ended` gets a public alias `run_ended`. (b) A pure
  generator `iter_run(cfg, run_dir, *, offset=None, backlog=200, poll_s=1.0, sleep=time.sleep,
  stop=None)` that yields dicts `{"kind": "meta"|"lines"|"end", ...}` with plain strings and no
  rich/Console. `tail.tail()` keeps its current behavior and output; a test pins that the plain
  text of `iter_run` equals what `tail()` prints for the same fixtures, so the two cannot drift.
- `src/nightwright/agentic/dashboard/static/{index.html,app.js,app.css}`: the stream client,
  the drilldown view and the tail panel.

The queue modules never import any of this (the epic's invariant 2). The only change outside
`agentic/dashboard/` is the additive `tail.py` change above, and `tail` does not import the
dashboard.

### The hub (`stream.py`)

```
class Hub:
    def __init__(self, cache, refresh_s, max_clients, clock=time.monotonic): ...
    def subscribe(self, known_rev) -> Subscriber | None    # None when over max_clients
    def unsubscribe(self, sub) -> None
    def stats(self) -> {snapshot_clients, tail_clients, builds_published}
```

- `subscribe` registers a `Subscriber` (a one-slot mailbox: the latest snapshot not yet written,
  plus a `threading.Event`). The first subscriber starts the snapshotter thread (daemon, named
  `nw-dashboard-snapshotter`); `unsubscribe` of the last one lets the thread exit after its
  current wait. Zero subscribers means zero ticks and zero cache calls.
- Snapshotter loop: `wait(refresh_s)` on a stop event, `snap = cache.current()` (child 1's
  accessor, which stats the watermark and rebuilds only when it moved or `max_stale_s` passed),
  and if `snap.rev != last_rev` set `last_rev` and put `snap` into every mailbox (replacing an
  unsent older one, so a slow client skips intermediate snapshots and never queues them). A
  failure of the cache call is logged to stderr and retried next tick; it never kills the thread
  or the server.
- A new subscriber's initial snapshot comes from `cache.current()` (not a rebuild), so a burst of
  reconnects after a phone wakes costs at most the one rebuild the cache already allows.
- The client handler loop: write `retry:`; write the initial snapshot unless `known_rev ==
  snap.rev`; then loop `sub.event.wait(15)`: on a snapshot, `write_event`; on timeout,
  `write_comment("hb")` and re-check auth (Decision 3). Any socket error (including the
  per-request send timeout, 10 s, which the epic's handler hardening already sets and streams
  keep) or auth denial exits the loop, unsubscribes in a `finally`, and closes.
- The cap is one counter under a lock shared by snapshot and tail streams; `503` is returned
  before any headers are sent for the stream.

### `GET /api/campaigns/<cid>` (`campaign.py`)

`cid` is checked against the campaign-id pattern and must resolve through `store.read_state`;
otherwise `404 {"error": "unknown campaign"}` (no path is ever built from the raw string).

```
{schema: "nw.dashboard.campaign.v1", generated_at, rev,
 campaign: {id, spec_id, title, project, kind, state, park: {reason, owner, escalated_from,
            resume_to}|null, loop, activity, cost_usd, cap_usd, cost_pct},
 ledger_tail: [≤ 40 of {ts, type, summary}],        # newest last
 runs: [≤ 20 of {run_id, persona, status, running, started, ended, cost_usd, verdict}],  # newest first
 decisions: [≤ 20 of {ts, kind, rule, rationale, by}],   # decisions.read(cfg, campaign=cid)
 escalations: [{id, status, kind, question, created, answered_by}],  # escalations.list_all, this campaign
 tail: {default_run, live}}                          # newest running run, else newest run, else null
```

- `campaign`, `cost_usd`, `cap_usd`, `loop` and `activity` are taken from the campaign's entry in
  the current snapshot (`active`, `needs_you`, `conductor_handling`, `waiting`); a campaign not
  in the snapshot (delivered, abandoned) falls back to `queue.row_view(cfg, state, {})` and the
  state file. Nothing is recomputed with a second formula, so the cost shown equals the
  dashboard card's and `nw queue watch`'s. `cost_pct` is `cost_usd / cap_usd` when the cap is
  set, else null.
- `ledger_tail` is `store.read_ledger(cfg, cid, tail=40)` with each event reduced to its time,
  type and a one-line summary produced from the event's fields (long values clipped to 160
  characters). Free-text fields go through the control-character filter of Decision 9.
- `runs` comes from `runs.campaign_runs`; `running` comes from `tail.running_runs`.
- Memo: keyed by `cid`, valid while the stat tuple (size, mtime_ns) of the campaign's ledger and
  of `decisions.jsonl`, the mtime of the campaign's `runs/` directory, the escalations signature
  and the snapshot `rev` are unchanged. At most 16 entries (LRU). `rev` is the hash of the
  payload without `generated_at`, and is the ETag.
- The route performs no write and never calls `events.sync`.

### `GET /api/campaigns/<cid>/tail?run=&offset=`

- `run` is a run id or unique prefix (`tail.resolve` semantics: default is the newest run
  preferring a running one); an unknown run is `404`, an ambiguous prefix `400` with
  `tail.resolve`'s message. `offset` is a non-negative integer byte offset into `stream.jsonl`,
  `400` when malformed or larger than the file.
- Events (all `data:` are compact JSON):
  - `meta` once: `{run_id, persona, model, budget_usd, status, streaming, offset, skipped}`.
    `streaming` is false for a run launched without stream capture; `skipped` is the number of
    backlog lines not sent because only the last 200 are.
  - `lines`: `{offset, lines: ["…"]}` with at most 200 lines, `id:` set to `offset` (the byte
    offset after the last line in the event). Each element is `Text.plain` of what
    `Renderer.feed_line` returned, with control characters stripped and each line clipped to
    2000 characters.
  - `end` once, then the server closes: `{reason: finished|vanished|orphaned|no_stream,
    offset, cost_line, summary}`. `cost_line` is the Renderer's closing cost line when the run
    produced a result, else null. `summary` is, for a run without a stream, the same short result
    summary `nw campaign tail` prints, produced by a shared helper as plain lines; otherwise an
    empty list.
- Polling: every `tail_poll_s` (1.0) the generator calls `tail.read_lines(stream, offset)`. A
  `stream.jsonl` that does not exist yet while the run is not ended yields `meta` with
  `streaming` true, no `lines`, and keeps waiting (heartbeats continue). At the end it applies
  the CLI's final-fragment rule: a trailing fragment that parses as a JSON object is rendered;
  an unparseable one is reported as one line `[incomplete last line]`.
- A tail stream does not wake the snapshotter and reads nothing but `runs/<run>/` files
  (`request.json`, `run.json`, `proc.json`, `exit.json`, `stream.jsonl`) and the pricing lookup
  the CLI uses.

### The page (`static/`)

- `app.js` gets a `Live` object: `start()` opens `/api/stream?rev=<last>` with `fetch()` and an
  SSE line parser (handles `event:`, `id:`, `data:`, comments, split chunks, `retry:`); updates
  the same render function that child 1's poll uses; maintains the indicator ("live", "polling",
  "reconnecting"); implements the 45 s no-data watchdog, backoff and the `visibilitychange`
  handling of Decisions 5 and 6. Polling and streaming write through one `applySnapshot(snap)`
  that ignores a snapshot whose `rev` it already shows, so the two paths cannot fight.
- Drilldown view at `#/c/<cid>` (hash routing, so the static shell stays one file and the back
  button works): header with state, loop, activity and a cost-vs-cap meter; sections "Run tail",
  "Runs", "Conductor decisions", "Ledger", "Escalations". Cards in the existing views link to
  it. An escalation row shows its id, status and question and links to the inbox entry when the
  inbox exists (child 2); without it, the row is plain text.
- Tail panel: monospace, `white-space: pre-wrap; overflow-wrap: anywhere` (no horizontal
  scroll), appends lines via `textContent`, keeps at most 2000 lines in the DOM (oldest dropped,
  with a "… N earlier lines" marker), auto-scrolls only while pinned to the bottom and shows a
  "Jump to live" button otherwise, shows "live · run <id>" and, on `end`, "ended (<reason>)"
  plus the cost line. On `end` it refetches the drilldown; if a newer running run appeared it
  opens that run's tail, otherwise it shows "waiting for the next run" and relies on snapshot
  updates (a run start changes `rev`).
- Every control is at least 44 px tall. `?selftest=1` (child 1) is extended with the checks in
  the Test plan.

### Config

No new keys. The feature uses the epic's `agentic.dashboard` keys: `refresh_s`, `max_stale_s`,
`max_clients`, `tail_poll_s`. The 15 s heartbeat, the 10 s send timeout, the 200-line backlog,
the 40/20/20 drilldown limits and the 45 s page watchdog are constants in the code. Values
that are not positive numbers fall back to the defaults, as in the epic.

### Read-only and isolation

All routes here are `GET`, call no function that writes (`events.sync`, `events.follow`,
`store.write_state`, `mailbox.send`, any `queue` mutation), and write nothing outside the
socket. The `tail` reads use `read_lines` (read-only open). Nothing in `queue`, `runner`,
`runproc`, `events` or `escalations` imports the dashboard.

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
- **Server-side follow across runs** (reuse `tail.follow_campaign`). It is a Console/herdr
  viewer that prints separators and state lines, and one stream would hide run boundaries from
  the phone. One run per stream with an `end` event is clearer and bounded.
- **Starting the tail at the end of the file.** Cheaper, but `Renderer` state (tool pairing,
  running cost) would be wrong. Replaying silently from byte 0 and emitting from the offset is
  correct and costs one local file read per connection.
- **Raw stream-json to the browser, rendered in JS.** It would fork the renderer into a second
  implementation that would drift from `nw campaign tail`.

## Acceptance criteria

- [ ] `GET /api/stream` answers `text/event-stream`, sends a `snapshot` event with `id` equal to
      the snapshot `rev` on connect, sends another only when `rev` changed (a ledger or
      escalation change), and sends a `: hb` comment at least every 15 s.
- [ ] A client connecting with `?rev=` equal to the current rev gets no initial snapshot event.
- [ ] The snapshotter thread exists only while at least one `/api/stream` client is connected;
      with zero clients the server performs zero snapshot builds and zero watermark stats, and
      the thread is gone within `2 × refresh_s` after the last client leaves.
- [ ] With N snapshot clients (N = 1 and N = 8) and an unchanging watermark, builds over a
      fixed window are at most `window / max_stale_s + 1` and the same for N = 1 and N = 8;
      after one watermark change every client receives exactly one new event and exactly one
      build happened. `queue.dashboard_payload` is never called from a request or stream
      handler, only by the snapshot builder.
- [ ] A client that stops reading does not delay events to other clients; its handler exits
      within the 10 s send timeout and frees its slot.
- [ ] Opening a stream beyond `max_clients` (snapshot and tail streams counted together) answers
      503 with `Retry-After`, and `/api/snapshot` polling still works.
- [ ] An open stream is closed after the next heartbeat tick when whois starts denying its peer;
      streams require the same authentication as every other API call (401 on loopback without
      the token, 403 on a denied tailnet peer) and neither puts a token in a URL.
- [ ] The page uses the stream when available, updates without reload, shows "live", falls back to
      15 s conditional polling when the stream fails, answers 503 or is silent for 45 s, and
      recovers; a snapshot with an already-shown `rev` is ignored.
- [ ] The page closes its streams when hidden and, when visible again, refreshes at once and
      reopens them with `?rev=` and the tail `offset=`; a hidden tab holds no client slot.
- [ ] `GET /api/campaigns/<cid>` returns `nw.dashboard.campaign.v1` with the campaign header
      (state, loop, activity, `cost_usd`, `cap_usd`), the ledger tail (≤ 40), runs (≤ 20),
      conductor decisions (≤ 20) and the campaign's escalations; `cost_usd` and `cap_usd` equal
      the snapshot entry's and `nw queue watch`'s; an unknown or malformed `cid` is 404 and no
      filesystem path is built from it; `If-None-Match` with the current `rev` is 304.
- [ ] N clients fetching the drilldown for an unchanged campaign cause one build; a ledger append
      invalidates it.
- [ ] `GET /api/campaigns/<cid>/tail` streams `meta`, then `lines` events whose lines equal the
      plain text `nw campaign tail` prints for the same `stream.jsonl`, in order and without
      duplicates across reconnects (`offset` resume), then an `end` event and closes when the run
      ends; the lines of a run appended while connected arrive within `tail_poll_s` plus
      delivery.
- [ ] Without `offset` the tail sends at most the last 200 backlog lines and reports `skipped`;
      with `offset` it sends exactly the lines after that byte offset, with the Renderer state
      (running cost) as if read from the start.
- [ ] A run with no `stream.jsonl` that has ended gets `meta` with `streaming: false` and an `end`
      with `reason: no_stream` and the result summary; a run whose `stream.jsonl` has not been
      created yet is waited for. A run that vanished ends the stream with `vanished`.
- [ ] Control characters (including ESC) never reach the client in tail lines or drilldown text,
      and the page renders run output and ledger text only through `textContent` (a line
      `<img src=x onerror=…>` appears as literal text and creates no element).
- [ ] Unknown run is 404, ambiguous prefix is 400 with `tail.resolve`'s message, a malformed or
      oversized `offset` is 400.
- [ ] Serving streams, drilldowns and tails leaves every file under the data dir except
      `agentic/dashboard/` byte-identical.
- [ ] No queue, runner, `runproc`, `events` or `escalations` module imports `agentic.dashboard`,
      and `tail` does not either; `tail.tail()` output is unchanged by the `iter_run` addition.
- [ ] In a headless mobile viewport (390×844 and 360×740) the drilldown and its tail panel have no
      horizontal overflow with a long unbroken line, all their controls are at least 44 px
      tall, and the self-test reports the stream connected and the tail receiving lines.
- [ ] `uv run pytest` on the affected tests passes; `python3 tools/spec_lint.py` exits 0.

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
      append produces one new event with a new `rev`; heartbeat comment arrives (the heartbeat
      constant is injectable in the test).
    - Load guard (IDEA-008): zero clients → builds and watermark stats stay 0 over several
      ticks and no `nw-dashboard-snapshotter` thread exists; 1 vs 8 clients with a constant
      watermark → equal build counts within `window / max_stale_s + 1`; one appended event →
      exactly one build and each of the 8 clients receives exactly one new event; all clients
      disconnect → thread gone within `2 × refresh_s`; `queue.dashboard_payload` call count
      equals build count (no per-request calls).
    - Backpressure: one client that never reads while events are published does not delay a
      second client; its handler frees the slot after the (shortened) send timeout.
    - Cap: `max_clients=2` → third stream (snapshot or tail) gets 503 with `Retry-After`;
      `/api/snapshot` still 200.
    - Auth: loopback stream without the token → 401; with it → 200; a fake `tailscale` on `PATH`
      (as in the epic) with an injected peer: allowed login streams, then switching the fixture
      to deny closes the stream after the next heartbeat tick; denied/unknown/tagged → 403 on
      stream, drilldown and tail.
    - Drilldown: payload shape against a seeded campaign (ledger events, two runs one running,
      decision records, an escalation); `cost_usd`/`cap_usd` equal the `dashboard_payload` entry;
      a delivered campaign uses the fallback; `If-None-Match` → 304; memo: 8 fetches → 1 build,
      ledger append → rebuild; unknown id, `../x`, and overlong id → 404.
    - Tail: append lines to a fixture `stream.jsonl` (fixtures from `tests/fixtures/stream_json/`)
      while connected and assert they arrive in order within `tail_poll_s` + wait; mark the run
      finished (`exit.json`) and assert `end` then close; reconnect with the last `id` as
      `offset` yields no duplicates and a correct cost line; default backlog limit and `skipped`;
      no-stream run; not-yet-created stream; vanished run; trailing fragment cases (parseable and
      unparseable); ESC and C0 bytes stripped; 404/400 cases.
  - `tests/test_tail_iter_run.py` — `tail.iter_run` plain lines equal `tail.tail()` output
    (stripped of its header and footer) for every fixture in `tests/fixtures/stream_json/`,
    live and finished, and `tail.tail()`'s own existing tests (`tests/test_tail*.py`) still pass
    unchanged.
  - `tests/test_dashboard_e2e.py` (extends the epic's file) — start `nw serve --dashboard
    --bind 127.0.0.1:0` as a subprocess against a seeded data dir, read the port from
    `server.json`; over real HTTP with `urllib`: `/api/stream` delivers a `snapshot` and a second
    one after a ledger change; `/api/campaigns/<cid>/tail` streams lines appended to the fixture
    `stream.jsonl` and ends when the run is marked finished; the drilldown matches the seeded
    campaign.
  - `tests/test_dashboard_readonly.py` (extends child 1's guard) — after the streams, drilldowns
    and tails above, a hash of every file in the data dir except `agentic/dashboard/` is
    unchanged; the import-graph test still passes with `tail.py` and the new modules.
  - `tests/test_dashboard_mobile.py` (extends child 1's headless test; same browser discovery and
    skip rule, and this child's Definition of done requires the run on this host) — load
    `?selftest=1#/c/<cid>` at 390×844 and 360×740 against a server with a seeded running
    campaign whose `stream.jsonl` contains a very long unbroken line and a
    `<img src=x onerror=document.title='pwned'>` line; the page self-test writes into the DOM,
    read via `--dump-dom`: no horizontal overflow, all controls ≥ 44 px, stream state "live", the
    tail panel holds the expected lines as text, no `img` element exists inside it and the
    document title is unchanged. A second run with the stream route made to answer 503 shows the
    "polling" indicator and still renders the snapshot.
  - Static JS check in `tests/test_dashboard_stream.py`: the SSE line parser extracted from
    `app.js` is exercised under Node when `node` is on `PATH` (split chunks, multi-line `data:`,
    comments, `retry:`, CRLF); skipped when Node is absent, since the headless-browser test
    covers the parser end to end.
- **Manual verification** (human, real tailnet; recorded in this spec's As shipped when done):
  1. Open the dashboard on the phone over cellular with the Tailscale app connected; confirm
     the indicator reads "live" and a queue change (e.g. a campaign changing state) appears
     within a few seconds without reloading.
  2. Lock the phone for a minute, unlock it: the page refreshes at once and the indicator returns
     to "live" without a manual reload.
  3. Open a running campaign's drilldown: the tail scrolls live as the persona works; when the
     run ends the panel says "ended" with the cost line; compare the visible lines with `nw
     campaign tail <cid>` on the host.
  4. Turn off the phone's Tailscale connection mid-stream: the indicator leaves "live"; turning it
     back on resumes without duplicated tail lines.
- **Regression guard**
  - The affected-tests run (`python3 tools/affected_tests.py --run uv run pytest -q`) plus the
    files above; the existing `tail` tests prove `nw campaign tail` and the herdr run pane
    (`follow_campaign`) are unchanged.
  - The import-graph and read-only tests above guard the epic's invariants 1 and 2.
  - `python3 tools/spec_lint.py` exits 0 and the ledger is regenerated.

## Rollout / migration

1. Land after child 1 of SPEC-0400 (the server, auth and snapshot cache). Reconcile the cache
   accessor name used above (`cache.current()`) with what child 1 shipped, and record the actual
   name in As shipped.
2. The `tail.py` additions (`run_ended`, `iter_run`) are additive; land them with their test
   first.
3. No data migration and no new config keys. The page keeps child 1's polling path, so an old
   cached page or a network that blocks SSE still works.
4. Children 2 and 4 are independent of this one; if child 2 lands first, the drilldown's
   escalation rows link to the inbox.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; the headless-browser test ran on this
      host (not skipped); manual steps performed and noted.
- [ ] No regressions (`nw campaign tail` and the herdr run pane unchanged; read-only and
      import-graph guards green).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

_None. Decided here: fetch-based SSE instead of `EventSource` (header auth), stream slots shared
between snapshot and tail streams under `max_clients`, one run per tail stream, and a 200-line
default backlog._
