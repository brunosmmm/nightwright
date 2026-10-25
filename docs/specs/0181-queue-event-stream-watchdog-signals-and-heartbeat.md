---
id: SPEC-0181
title: "Queue event stream, watchdog signals and heartbeat during runs"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, events, watchdog, observability]
source_idea: IDEA-047
depends_on: [SPEC-0180]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-047`).

The user's model of the conductor (2026-10-25): a long-lived session that is **woken by
events fired by the queue** — a park, a merge, a stuck or stale item, a steward message —
not one that acts only when invoked. Claude Code's Monitor tool provides the wake-up: every
stdout line of a monitored command is a notification to the session. What is missing is the
command to monitor: a durable stream of queue events plus signals that a hung loop cannot
emit about itself.

The transport is **file-based** (decided with the user): an append-only JSONL file with
per-reader cursors, reusing the store's atomic-append and flock primitives. Two observed
defects belong here too: the loop's heartbeat only updates between steps, so a long persona
run makes a healthy lease look stale; and saved logs must be machine-readable and
consistent (IDEA-045) — the event stream is the first log built to that rule.

## Goals / Non-goals

**Goals**
- `<data_dir>/agentic/events.jsonl`: one record per queue-level event, envelope
  `{schema: "wt.event.v1", id, ts, type, campaign?, spec_id?, project?, by, data{}}`
  with a monotonic integer `id`; per-type required `data` keys validated on append.
- The queue appends events at: `parked`, `park_requested`, `resumed`, `admitted`,
  `backlog_blocked` / `backlog_ready` (from SPEC-0180's backlog events), `merged`,
  `handed_off`, `abandoned`, `cost_warning` (crossing 80% of cap, once per campaign),
  `run_failed` (persona run error, with `transient: bool` — HTTP 429 / `api_error` are
  transient), `runner_started`, `runner_stopped`.
- `wt queue events [--since ID] [--types …] [--json]` prints events;
  `wt queue events --follow --for READER` streams new events forever, one JSON line each,
  storing the reader's cursor in `agentic/cursors/<reader>.json` after each emitted line, so
  a re-armed Monitor resumes exactly after the last delivered event.
- **Watchdog in the follower** (out of the loop's process): every `poll_s` (default 2 s)
  the follower checks and emits synthetic events, each once per condition until it
  clears: `runner_dead` (items active or ready in backlog and no held lease),
  `lease_stale` (held, heartbeat older than `ttl_s`), `run_overdue` (a `run.start` with no
  `run.end` older than the run's `timeout_s` + 60 s), `campaign_silent` (active campaign
  with no ledger event for `silent_after_s`, default 900 s). Watchdog events are appended
  to `events.jsonl` (`by: "watchdog"`) so every reader sees them.
- The loop heartbeats from a background thread every `heartbeat_s` (default 10 s) while
  a tick is in progress, not only between ticks.

**Non-goals**
- The conductor's reaction logic (SPEC-0182), the mailbox (SPEC-0183).
- Converting `runner.log`, run results and gate logs to the consistent format (IDEA-045
  follow-up), beyond writing new data in that format here.
- Retrying transient run failures automatically (reported here as `run_failed{transient}`;
  the retry policy is a separate small spec).

## Decision

Add `src/wt/agentic/events.py` (append with flock + monotonic id, read since cursor, schema
validation, cursor files, watchdog checks as pure functions over store state) and wire
`append` calls into `queue.py` at the listed points. Add the `events` subcommand. Run the
heartbeat in a daemon thread owned by `queue.run`.

## Design

- **`events.py`:** `EVENT_TYPES` with required data keys, e.g. `parked: {reason, detail,
  resume_to}`, `merged: {merged_sha}`, `run_failed: {persona, run_id, transient, error}`,
  `cost_warning: {total_usd, cap_usd}`, `runner_dead: {active, backlog_ready}`,
  `lease_stale: {pid, heartbeat_age_s}`, `run_overdue: {persona, run_id, age_s,
  timeout_s}`, `campaign_silent: {state, silent_s}`. `append(cfg, type, *, campaign=None,
  by, data) -> dict` validates, takes a flock on `events.lock`, reads the last id, writes one
  line (`O_APPEND`, single `write`). `read_since(cfg, after_id, types=None)`.
  `cursor_get/set(cfg, reader)`.
- **Watchdog:** `watchdog_checks(cfg, now) -> list[(key, type, campaign, data)]` pure over
  `store.list_campaigns`, `read_ledger` tails and `lease_status`; the follower keeps an
  "active conditions" set per key (persisted in `cursors/<reader>.watch.json`) and emits on
  appearance only; a condition clearing emits nothing (its resolution shows up as a normal
  event).
- **Follower:** `follow(cfg, reader, poll_s)`: loop { new = read_since(cursor); print each
  as one JSON line, flush, advance cursor; run watchdog; sleep poll_s }. SIGTERM/SIGPIPE
  exit cleanly. With `--json` off it prints a compact one-line human summary per event
  (still one line per event, so Monitor-friendly).
- **Queue wiring:** a single helper `_emit(type, state, data)` called next to the existing
  ledger writes in `_transition` (park/resume/merge/handoff/abandon/admit), in run
  application (`run_failed`, `cost_warning`), and in `run()` start/stop.
- **Heartbeat thread:** `queue.run` starts `threading.Thread(daemon=True)` calling
  `lease.heartbeat()` every `heartbeat_s` until stopped; joined on exit.
- **Config:** `agentic.events: {poll_s: 2, silent_after_s: 900, heartbeat_s: 10}` (defaults
  in code, overridable).
- **Docs:** capability page "Events and watchdog"; agent-surface rows for `wt.event.v1`.

## Alternatives considered

- **A broker (Redis/NATS/ZeroMQ)** — rejected with the user: another process to supervise,
  non-durable by default; files are durable, replayable and already the store's model.
- **Watchdog inside the loop** — rejected; a hung loop cannot report its own hang. The
  follower runs in the conductor's process tree.
- **Emit everything in the ledger to the stream** — rejected; the conductor wants
  attention-worthy events, not every run start. Ledgers stay the full record.

## Acceptance criteria

- [ ] `events.append` validates per-type keys, assigns strictly increasing ids under
      concurrent appenders (threads), and never interleaves lines.
- [ ] The queue emits each listed event at the right moment (e2e with the fake runner:
      merged path; parked path; cost_warning at 80%; run_failed with `transient: true` for
      a fake 429 result).
- [ ] `wt queue events --follow --for conductor` prints new events as single JSON lines,
      persists its cursor, and after being killed and restarted prints only events after
      the last one delivered.
- [ ] Watchdog emits `runner_dead`, `lease_stale`, `run_overdue` and `campaign_silent`
      exactly once per condition (injected clock / fabricated state), and again only after
      the condition cleared and reappeared.
- [ ] During a fake run with `sleep_s` longer than `heartbeat_s`, the lease heartbeat age
      stays below `heartbeat_s + 2`.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_events.py` (append/read/cursors/validation,
  threads), watchdog pure tests with fabricated campaigns and lease files, follower test as
  a subprocess reading N lines then restarted, queue e2e assertions on emitted events,
  heartbeat-during-run test.
- **Manual verification:** run `wt queue events --follow --for test` in a Monitor while the
  loop processes a fake-runner campaign on a scratch data dir; kill the loop and see
  `runner_dead`.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Built by the agentic loop; idea bookkeeping via `wt spec reconcile --apply` after merge.

## Rollout / migration

New file created on first event. Nothing reads it until SPEC-0182.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
