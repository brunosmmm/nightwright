---
id: SPEC-0430
title: "Dashboard campaign drilldown: ledger timeline, current run and live run tail from stream.jsonl"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
milestone: "M9: agentic workflow"
source_idea: IDEA-965
kind: feature
parent: SPEC-0400
depends_on: [SPEC-0362, SPEC-0402, SPEC-0428]
tags: [agentic, dashboard, tail, sse, web]
---

## Context

Promoted from idea `IDEA-965`. Child of epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md).
The epic's G1 asks for "a per-campaign drilldown with a live run tail" and its HTTP API names
`GET /api/campaigns/<cid>` and `GET /api/campaigns/<cid>/tail?run=&offset=`. SPEC-0402 (live
snapshot stream) explicitly split both out as Non-goals "for their own specs under SPEC-0400";
none was written. The human asked on 2026-11-04 that the drilldown at least show the campaign
timeline and current run, reusing `nw campaign tail` / `stream.jsonl` (SPEC-0362).

Reused, checked in the code: `store.read_ledger(cfg, cid, tail=N)` and `render.campaign_event`
(what `nw campaign log` prints), `runs.campaign_runs` (`nw campaign runs`), `tail.running_runs`,
`tail.resolve`, `tail.read_lines`, `tail.Renderer.feed_line` and `tail._ended` (what `nw campaign
tail -f` uses), SPEC-0402's `stream.SlotPool` and SSE writers.

## Goals / Non-goals

**Goals**
- G1. Tapping a campaign on the page opens its drilldown: state, park, cost vs cap, the last
  ledger events as `nw campaign log` renders them, its runs, and its current (or newest) run.
- G2. The current run's transcript streams live, rendered exactly as `nw campaign tail` renders
  it (plain text), and the stream ends when the run ends.
- G3. Nothing runs when no drilldown is open; tails share SPEC-0402's `max_clients` slots.

**Non-goals**
- Raw stream-json, editing anything, following a campaign across runs (the page reopens the
  newest run when the snapshot changes).

## Decision

1. **`GET /api/campaigns/<cid>`** → `nw.dashboard.campaign.v1`:
   `{schema, generated_at, campaign, state: {state, loop, spec_id, spec_title, project, park,
   cost_usd, cap_usd}, timeline: [{ts, type, by, text}] (last 40 ledger events, text =
   render.campaign_event(ev).plain), runs: [{run, persona, status, cost_usd, started_at,
   exit}] (newest 20 of runs.campaign_runs), current_run, running}`. `current_run` is the newest
   running run, else the newest run, else null. `<cid>` must match `[A-Za-z0-9][A-Za-z0-9._-]{0,99}`
   and name an existing campaign, else `404`. Built per request (no background work); every
   section fails alone into `{error}`.
2. **`GET /api/campaigns/<cid>/tail?run=<run>&offset=<n>`** → SSE. `run` defaults to
   `current_run`. Each `tail_poll_s` (`agentic.dashboard.tail_poll_s`, default 1.0) it reads
   complete lines from `stream.jsonl` at `offset` (`tail.read_lines`), renders them with one
   `tail.Renderer` per stream, and sends `event: lines` / `data: {run, offset, lines}` (control
   characters other than tab removed; at most the last 400 lines of the initial backlog). When
   `tail._ended(run_dir)` it does a final read and sends `event: end` / `data: {run, status}` and
   closes. A run without stream capture sends its header line and `end`. Heartbeats, the send
   timeout, the identity re-check and slot accounting are SPEC-0402's; a tail takes one slot of
   the shared `SlotPool` (over the cap: `503` + `Retry-After`).
3. **The page.** Campaign rows (active, inbox entries, parks) link to `#/c/<cid>`. The drilldown
   view replaces the main cards with: a back link, the state block, the timeline, the runs list
   and a tail panel for `current_run` that opens the tail stream (with the same fetch-based SSE
   parser as SPEC-0402) and keeps at most 1000 lines. While it is open, a new snapshot `rev`
   refetches the drilldown, and a new `current_run` switches the tail. Closing the view or hiding
   the tab closes the tail stream.

## Design

- `dashboard/campaign.py` (new): `valid_cid`, `build(cfg, cid)`, `Tailer(cfg, cid, run, offset)`
  with `poll() -> (lines, offset)`, `ended()`, `status()`.
- `dashboard/server.py`: the two GET routes; the tail stream loop next to `_stream`.
- `static/app.js` / `app.css` / `index.html`: the drilldown view and tail panel.
- `__init__.settings`: `tail_poll_s` (positive number, default 1.0).

## Alternatives considered

- **Polling the tail with offsets.** Works, but the epic chose SSE and SPEC-0402 built the
  machinery; a 1 s poll per open tail is the same disk work without the request overhead.
- **Sending raw stream-json to the browser and rendering in JS.** Forks the renderer; the epic's
  invariant 5 says reuse.

## Acceptance criteria

- [x] `GET /api/campaigns/<cid>` returns the state, the last ledger events with the same text as
      `nw campaign log`, the runs and `current_run`; an invalid or unknown cid is `404`; it writes
      nothing.
- [x] `GET /api/campaigns/<cid>/tail` streams lines appended to a run's `stream.jsonl` within
      `tail_poll_s` + delivery, rendered as `tail.Renderer` renders them, and sends `end` and
      closes once the run has ended; it takes and frees a stream slot.
- [x] The page opens the drilldown from a campaign row, shows the timeline and current run, shows
      tail lines live, and closes the tail stream when leaving the view or hiding the tab.
- [x] Serving drilldowns and tails leaves the data dir byte-identical outside
      `agentic/dashboard/`.

## Test plan

- **Automated tests**
  - `tests/test_dashboard_campaign.py` (new): `build` over a seeded campaign (ledger events, two
    run dirs, one running) equals `nw campaign log --json` texts; cid validation; over real
    sockets: the drilldown JSON; the tail stream receives lines appended to a fixture
    `stream.jsonl` (from `tests/fixtures/stream_json/`) and an `end` event after `run.json` is
    written; slot release; read-only hash check.
  - `tests/js/` page tests: the drilldown view renders the timeline and the tail lines from a
    scripted stream; leaving the view aborts the stream.
- **Manual verification:** the real server against the scratch data dir: open
  `/api/campaigns/<cid>` and a tail with curl while appending lines; the headless shell renders
  `#/c/<cid>` when available.
- **Regression guard:** affected tests; `python3 tools/spec_lint.py`.

## As shipped

Shipped as designed (`dashboard/campaign.py`, routes in `server.py`, the `#drill` view in
`index.html` / `app.js`). Details:

- The state block comes from `queue.row_view` (title, cost, cap) plus state, loop, label and park.
- The tail stream sends `event: start` (`{run, offset, header, status}`, the CLI's header line
  without the state suffix), then `event: lines`, then `event: end` (`{status}`) and closes. The
  run is checked for its end on every `tail_poll_s`; a run without stream capture sends its
  result line at the end. The tail's slot is released on every exit path (`client_count()` drops
  back to 0).
- `tail_poll_s` is read by `server.tail_poll_s(cfg)` (positive, ≤ 60, default 1.0).

Real-process evidence (2026-11-04), all against a SCRATCH data dir under
`/home/bruno/work/.nw-tmp/dash-ui/` with escalations opened by the real CLI (`nw queue escalate
--yes`), never the real data dir:

- `GET /api/campaigns/<cid>` returned `nw.dashboard.campaign.v1` with the state, the timeline (the
  `nw campaign log` lines) and `current_run`. A tail of a running run (a live `wrapper_pid`)
  received the lines appended to `stream.jsonl` within 0.5 s, then `end` after `exit.json`; the
  dashboard's lines equal `nw campaign tail`'s output line for line (the header aside).
- Headless Chromium: `#/c/<cid>` hid the main view, showed 1+ timeline rows and the tail ending in
  "■ run ended (finished)"; clearing the hash returned to the main view.

Automated: `tests/test_dashboard_campaign.py` (CLI parity of the timeline, cid validation, live tail
to `end` with slot release, read-only hash) and the drilldown test in `tests/js/answer.test.js`.

## Rollout / migration

No data migration, no new dependency. `tail_poll_s` is the epic's documented key.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._
