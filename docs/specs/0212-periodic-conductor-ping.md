---
id: SPEC-0212
title: "Periodic conductor ping: a digest of blocked, stuck, parked and waiting items"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
depends_on: [SPEC-0181, SPEC-0200]
tags: [agentic, events, watchdog, conductor]
source_idea: IDEA-079
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-079`. The conductor is woken only by discrete events and watchdog conditions
([SPEC-0181](./0181-queue-event-stream-watchdog-signals-and-heartbeat.md)). Items that sit
without any new event go unnoticed: spec merges stalled 30+ min behind a CLOSING hold, and a
backlog item stayed blocked 36 min, with nothing to wake the conductor. It needs to be brought
to attention periodically, with a digest of what to look at.

## Goals / Non-goals

**Goals**
- Wake the conductor every N minutes with a digest of blocked, stuck, parked and waiting items.
- One-shot watchdog signals when an item crosses a "too long" threshold.
- Exactly-once across followers and restarts; no bursts.

**Non-goals**
- Open merge requests in the digest (no such queue yet; add when it exists).
- Acting on anything: the conductor decides ([SPEC-0182](./0182-event-driven-wt-conduct-monitor-armed-conductor.md)).
- Ledger schema changes: the new types are stream events only, not ledger events.

## Decision

The events follower appends a `conductor_ping` every `ping_interval_s` (default 600), always,
whatever else happens. Three watchdog conditions, `backlog_blocked_long`, `state_stuck` and
`ready_not_admitted`, fire once per condition. All four types are registered in `events.EVENT_TYPES` (the stream's registry;
`schemas.py` holds only ledger types and is unchanged).

Decided details (Autonomy rule; tune later from evidence):
- **Ping always fires**, even with an empty digest: it doubles as proof the follower is alive,
  and the skill says to do nothing when the digest is clean.
- **Only the follower pings** (`sync(..., ping=True)` from `follow`); `conduct-status` and
  plain `wt queue events` sync without pinging so reads never write pings.
- **Interval state** `last_ping` (ISO ts) lives in `events.projector.json`, under `events.lock`.
  First ever sync seeds `last_ping = now` and does not ping (a fresh conductor reads state
  itself on arm). A follower returning after downtime pings once at the first sync (never a
  burst of missed pings). `ping_interval_s <= 0` disables.
- **Blocked** = QUEUED with stored `backlog.ready == false`, blocked longer than
  `blocked_after_s` (default 1200), measured from `backlog.since` (falls back to
  `created_at`). `since` resets when the reasons change; accepted, since a changed reason is
  still a blocked item. Uses stored state, not a spec re-read, so the follower stays cheap.
- **Stuck** = ACTIVE state whose last `transition` into it is older than `stuck_after_s`
  (default 900), excluding (a) an open `run.start` within `timeout_s + overdue_grace_s`, and
  (b) GATING/CLOSING with no open run within `gate_budget_s` of entering the state (gates run
  in-process; same budget as the `lease_stale` exemption). A run past budget is
  `run_overdue`'s job but the item is stuck and listed.
- **Parked** lists every PARKED item with `reason`, `detail`, `since`, `parked_for_s` and
  `decided`: true when a `note` ledger entry follows the last `park` entry (only humans and
  agents write notes; a `resume`/`abandon` leaves PARKED, so it needs no flag).
- **Ready not admitted** (user addition 2026-10-25): QUEUED, stored `backlog.ready == true`
  for longer than `ready_grace_s` (default 60, from `backlog.since`), not `park_requested`,
  while active < `slots_for(cfg)`. Data: `ready_for_s`, `active`, `slots`, `runner_pid`
  (None when no runner holds the lease) and `runner_busy` (the runner holds the lease and a
  campaign has an open persona run or sits in GATING/CLOSING, i.e. its tick is on a long
  step; derived from ledgers, no new runner journal). The digest lists them under
  `ready_not_admitted`. Known head-of-line limitation: IDEA-081 / SPEC-0213.
- **Waiting** lists WAITING items with `waiting_for_s` and `resume_to`.

## Design

`events.py`:
- `DEFAULTS` gains `ping_interval_s: 600`, `blocked_after_s: 1200`, `stuck_after_s: 900`
  (config `agentic.events.*`).
- `blocked_items(cfg, now, sett)`, `stuck_items(cfg, now, sett)`, `parked_items`,
  `waiting_items`, and `ping_digest(cfg, now=None)` returning
  `{blocked, parked, stuck, waiting, counts}`:
  - `blocked[]`: `{campaign, spec_id, project, blocked_by, blocked_for_s}`;
  - `parked[]`: `{campaign, spec_id, reason, detail, since, parked_for_s, decided}`;
  - `stuck[]`: `{campaign, spec_id, state, stuck_s}`;
  - `waiting[]`: `{campaign, spec_id, waiting_for_s, resume_to}`;
  - `counts`: `{active, queued, ready, blocked, parked, undecided_parks, stuck, waiting}`
    (`blocked` here = all not-ready QUEUED items, `blocked[]` only the long ones).
- `watchdog_checks` adds `backlog_blocked_long:<cid>` (data `spec_id`, `blocked_for_s`,
  `blocked_by`) and `state_stuck:<cid>:<state>` (data `state`, `stuck_s`).
- `_ping(cfg, proj, now)` called from `sync(cfg, now, ping=True)` inside the lock: if
  `last_ping` is absent seed it; else if `now - last_ping >= interval` append
  `conductor_ping` (`by=watchdog`, data = digest) and set `last_ping`. `now` is injectable.
- `EVENT_TYPES`: `conductor_ping` requires `blocked parked stuck waiting ready_not_admitted counts`;
  `backlog_blocked_long` requires `blocked_for_s blocked_by`; `state_stuck` requires
  `state stuck_s`; `ready_not_admitted` requires `ready_for_s active slots`.
- `summary_line` renders a ping as one line, e.g.
  `#42 conductor_ping blocked=1 parked=2(undecided 1) stuck=0 waiting=0 active=3 queued=4`.
- `follow` calls `sync(cfg, ping=True)`.

`skills/wt-conduct/SKILL.md`: four decision-table rows. Docs: capability page "Events and
watchdog" and the agent-surface `wt.event.v1` row.

## Alternatives considered

- **A timer inside the conductor session** — it has no timer; the Monitor stream is its only
  wake-up, so the ping must be an event.
- **Ping only when the digest is non-empty** — loses the liveness signal and makes "no ping"
  ambiguous; an empty ping is cheap (the skill says no-op).
- **Ping from the runner** — a hung runner could not ping; the follower runs outside it.

## Acceptance criteria

- [x] `conductor_ping` is appended by the follower every `ping_interval_s` with the digest.
- [x] Two syncs/followers inside the interval produce one ping; a restart resumes the interval.
- [x] Blocked and stuck thresholds hold; an in-budget run/gate excludes a stuck state.
- [x] `backlog_blocked_long`, `state_stuck` and `ready_not_admitted` fire once per condition.
- [x] The three types validate in `EVENT_TYPES`; `summary_line` renders a ping on one line.
- [x] Skill rows (incl. `ready_not_admitted`), capability page and agent-surface guide updated.

## Test plan

- **Automated tests:** `tests/test_agentic_ping.py` with an injected clock: seed then fire at
  the interval; digest contents; no duplicate across two syncs; restart; blocked/stuck
  thresholds and in-budget exclusion; parked `decided`; watchdog once per condition; schema
  validation; `summary_line`; non-follower sync does not ping.
- **Manual verification (performed):** full `uv run pytest -q` run, 0 failed; `wt spec verify SPEC-0212` ok; `spec_lint` exit 0.
- **Regression guard:** existing `tests/test_agentic_events.py`, `tests/test_skills.py`.

## Clock Log

CLOCK-IN: via `wt idea clock-in IDEA-079`
CLOCK-OUT: via `wt idea clock-out IDEA-079`

## Rollout / migration

None: new event types are additive; old projector files lack `last_ping` and get it seeded.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

As designed. The event-type registry is `events.EVENT_TYPES` (`schemas.py` only holds ledger
types, so it is unchanged). Tests: `tests/test_agentic_ping.py` (12).

## Open questions

(none; all details decided above)
