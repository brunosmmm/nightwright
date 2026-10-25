---
id: SPEC-0228
title: "Watchdog: waiting_not_admitted and runner_journal_silent, with the drain in ready_not_admitted"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, watchdog, conductor, events]
source_idea: IDEA-103
depends_on: [SPEC-0224]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-103` ("Watchdog gaps: WAITING items with free slots and a silent
runner journal raise no alert").

The incident, 2026-10-25 18:17. SPEC-0202 and SPEC-0219 were WAITING
([SPEC-0200](./0200-waiting-state-started-campaigns-wait-for-a-slot.md)) with slots free, and the runner
did not pick them up. The human noticed it, not the conductor. The cause was a code-change
re-exec drain (`IDEA-100`, [SPEC-0224](./0224-honest-run-cost-on-interrupt-and-a-runner-draining.md)).
The watchdog ([SPEC-0212](./0212-periodic-conductor-ping.md), `events.watchdog_checks`)
missed it in three ways:

1. `ready_not_admitted_items` only looks at QUEUED items with stored readiness. It listed
   SPEC-0222 and never 0202 or 0219. WAITING campaigns are not covered by any check, and
   the `conductor_ping` `waiting` list shows them without saying whether a slot is free.
2. Nothing fires when the loop stops ticking but the process lives. The heartbeat thread
   (SPEC-0204) keeps beating during a drain, so `lease_stale` never fires. `runner_dead`
   needs the flock to be free.
3. `ready_not_admitted` carries `runner_busy: true` during a drain. The conductor reads
   that as "fine, wait".

SPEC-0224 adds `runner_draining` once a drain is 60 s old, the lease's `draining` info, and
`runner_draining: bool` on `ready_not_admitted` rows. This spec builds on those. It adds
the two missing conditions and the drain details the conductor needs to act at once. The
conductor skill already acts at once on a stalled queue (commit a43fef6, rows
`ready_not_admitted` and `merged`). The two new events get matching rows.

## Goals / Non-goals

**Goals**
- A WAITING campaign that is not resumed although a slot has been free for over a grace
  raises `waiting_not_admitted`.
- A runner whose loop has not ticked for 3 poll intervals, while its lease is held and its
  heartbeat is fresh, raises `runner_journal_silent`. It fires once per silence episode.
- `ready_not_admitted` and the new events carry the lease's drain info. The conductor can
  then tell "busy" from "draining".
- The `conductor_ping` digest lists these conditions.
- `skills/wt-conduct/SKILL.md` has rows for both new events: act at once.

**Non-goals**
- Changing admission or the drain itself (SPEC-0224, SPEC-0225).
- Restarting the runner automatically from the watchdog. The watchdog only observes, and
  the conductor acts.
- Changing `lease_stale` / `runner_dead` semantics.

## Decision

1. **`waiting_not_admitted`.** `events.waiting_not_admitted_items(cfg, now, sett)` returns
   one row for each WAITING campaign (not `park_requested`) when `active < slots`. The
   campaign must have waited longer than `waiting_grace_s` (new setting, default 60), and
   the free slot must have existed that long too. The watchdog emits one event per
   campaign per wait.
2. **`runner_journal_silent`.** The loop stamps `tick_at` into its lease info after every
   tick. The journal alone cannot be used, because idle ticks are not journaled. The
   watchdog measures silence from the latest of the last journal record of the holding pid
   and `tick_at`. It fires when the lease is held, the heartbeat is not stale, and the
   silence exceeds `3 × interval_s`. The key includes that last-activity timestamp, so the
   event fires once per episode and re-arms after the next record.
3. **Drain info.** One helper, `events._drain_info(lease)`, builds `drain` = `{reason,
   since, draining_s, waiting_on}` from SPEC-0224's `lease_status()["draining"]`, or `None`.
   `ready_not_admitted` rows, `waiting_not_admitted` and `runner_journal_silent` carry it.
4. **Ping.** `ping_digest` gains `waiting_not_admitted` (rows) and `runner` (the current
   silence row or `None`). `counts` gains `waiting_not_admitted`.

## Design

### Settings and event types (`events.py`)

- `DEFAULTS["waiting_grace_s"] = 60`, a separate setting from `ready_grace_s`. WAITING
  items resume ahead of the backlog (SPEC-0200), so a site may want to tune them on their
  own. The silence factor is `DEFAULTS["journal_silent_factor"] = 3`. Overrides go through
  `agentic.events` like the other settings.
- `EVENT_TYPES["waiting_not_admitted"] = ("waiting_for_s", "active", "slots")`.
- `EVENT_TYPES["runner_journal_silent"] = ("last_journal_ts", "silent_s",
  "heartbeat_age_s")`.
- `ready_not_admitted` required keys are unchanged. `drain` is additive.
- `conductor_ping` required keys are unchanged. `waiting_not_admitted` and `runner` are
  additive, so pings already in the stream keep validating.

### `waiting_not_admitted`

- `waiting_for_s` is the age of `st["waiting"]["since"]`.
- `slot_free_s` estimates how long a slot has been free. A slot frees when a campaign
  leaves an active state. Reading every ledger on each 2 s sync costs too much, so the
  estimate uses state rows only: the age of the newest `updated_at` among campaigns in
  neither `ACTIVE_STATES` nor QUEUED/WAITING (parked, merged, handed off, abandoned).
  When no such campaign exists, the slot has been free since the wait began
  (`slot_free_s = waiting_for_s`).
- A later `updated_at` on such a row (for example a note on a parked item) can make
  `slot_free_s` too small. That only delays the alert, by at most one grace, and never
  raises a false one. A long wait plus a slot that freed seconds ago must not fire before
  the next tick, so this direction of error is the safe one. Raising `slots` in the config
  can make `slot_free_s` too large. The runner reloads config at each tick (SPEC-0226), so
  this case is rare and lasts less than one poll interval. It is accepted.
- The row fires when `min(waiting_for_s, slot_free_s) > waiting_grace_s`. Data:
  `{waiting_for_s, slot_free_s, resume_to, active, slots, runner_pid, drain}`.
  `runner_pid` is `None` when the lease is not held.
- Key: `waiting_not_admitted:<cid>:<waiting.since>`. A second wait re-fires.

### `runner_journal_silent`

- **`tick_at` (`queue.run`).** After each `tick()` the loop calls
  `lease.update_info(tick_at=store.now_iso())`. This is SPEC-0224's locked lease info API.
  The heartbeat thread never writes `tick_at`, so the value stays old during a drain or a
  hung main thread, which is the case to catch. `Lease.handover` / adopt keep `tick_at`.
  The new process overwrites it on its first tick.
- **Interval.** `interval_s` comes from the newest journal `start` record of the holding
  pid (`queue.read_journal(cfg, tail=200)`). That record has the real interval, including a
  `--poll-interval` override. The fallback is `agentic.poll_interval_s` (default 20).
- **Silence.** `last = max(ts of the newest journal record with pid == holder pid,
  lease.tick_at)`. `silent_s = age(last)`. The check is skipped when the lease is not held,
  when it is `stale` (`lease_stale` / `runner_dead` cover that), or when `tick_at` is
  absent. A runner from before this spec has no `tick_at`, and skipping avoids a false
  alarm until it re-execs.
- It fires when `silent_s > journal_silent_factor × interval_s`. The heartbeat is fresh,
  since the lease is not stale. Key: `runner_journal_silent:<pid>:<last>`. The condition
  holds for one episode and clears when a new record or `tick_at` moves `last`. `_watch`
  then re-arms it, so it fires once per episode.
- Data: `{pid, last_journal_ts, last_tick_at, silent_s, interval_s, heartbeat_age_s,
  draining}`. `draining` is the `drain` dict or `None`. `last_journal_ts` is the newest
  journal record's ts, or `None`.
- **Why 3×.** One interval is the normal gap. A tick that admits and opens worktrees can
  take one more. With a 20 s poll the alarm comes at 60 s, the same as the other graces.
  So the stall in the incident would have been reported within about a minute, not when
  the human asked.
- **Overlap with `runner_draining`.** Both can fire for one drain, at about the same 60 s.
  That is intended. `runner_journal_silent` covers any loop stall, drain or not, and both
  conductor rows lead to the same action.

### Drain info in `ready_not_admitted`

- `ready_not_admitted_items` rows gain `drain` (the helper above). SPEC-0224's
  `runner_draining: bool` is kept. `runner_busy` keeps its meaning. The skill row says
  that `drain` set means the runner is stalled, not busy.

### Ping (`ping_digest`)

- `waiting_not_admitted`: the rows from the helper.
- `runner`: the `runner_journal_silent` data when the condition holds now, else `None`.
- `counts.waiting_not_admitted`: the number of those rows.

### Conductor skill (`skills/wt-conduct/SKILL.md`)

- New row `waiting_not_admitted`: same action as `ready_not_admitted`. Act within one
  event. If `drain` is set or the runner journal is silent, SIGTERM the runner and restart
  it (step 2). If the runner is dead, start it. Note the action on the campaign. No
  report to the human unless admission still fails after a restart.
- New row `runner_journal_silent`: the loop is stalled while alive. SIGTERM the runner and
  restart it immediately (step 2). An interrupted persona run costs less than a stalled
  queue: its cost is marked estimated (SPEC-0224), and its worktree edits survive. If the
  stall recurs within minutes of a restart, read `wt queue runner-log --tail 30` and
  dispatch a sub-agent to diagnose.
- The `ready_not_admitted` row mentions `drain`. The `conductor_ping` row lists
  `waiting_not_admitted` and `runner`.
- Relation to SPEC-0224's planned `runner_draining` row ("do not SIGTERM"). The human
  decided after 0224 was accepted that a stalled queue is worse than an interrupted run
  (commit a43fef6). When both events fire, the `runner_journal_silent` row decides. Whoever
  builds 0224's row adds that precedence note instead of a separate wait policy.

## Alternatives considered

- **Journal every idle tick.** That gives a pure journal signal, but adds about 4300
  lines/day to `runner.jsonl` for no other use. A lease `tick_at` costs one write that the
  heartbeat already makes. Rejected.
- **Track when a condition was first seen, in the projector.** `_watch` could store when
  each pending condition first appeared, which gives an exact "slot free for N s". It
  makes `watchdog_checks` stateful, unlike every other check. The `updated_at` estimate
  only errs toward delay. Rejected for now.
- **Fold into SPEC-0224.** 0224 is accepted. Rewriting it in place would break the
  timeline. Rejected. This spec depends on it.
- **Reuse `ready_not_admitted` for WAITING items.** Its data model is backlog readiness
  (`backlog.since`), which WAITING items do not have. A separate type keeps the conductor
  rows clear. Rejected.

## Acceptance criteria

- [ ] `watchdog_checks` emits `waiting_not_admitted` for a WAITING campaign when
      `active < slots` and `min(waiting_for_s, slot_free_s) > waiting_grace_s` (default
      60). Data: `waiting_for_s, slot_free_s, resume_to, active, slots, runner_pid, drain`.
      It is not emitted within the grace, with slots full, for a `park_requested` item, or
      when a slot freed less than the grace ago. It is emitted once per wait across
      repeated syncs.
- [ ] `queue.run` stamps `tick_at` in the lease info after every tick through
      `lease.update_info`. The heartbeat thread does not change it.
- [ ] `watchdog_checks` emits `runner_journal_silent` (`pid, last_journal_ts, last_tick_at,
      silent_s, interval_s, heartbeat_age_s, draining`) when the lease is held and not
      stale, `tick_at` is present, and the newest of the holder's journal records and
      `tick_at` is older than `3 × interval_s`. `interval_s` comes from the holder's
      `start` record.
- [ ] It fires exactly once per silence episode across repeated syncs. It does not fire
      when the lease is free or stale, or when `tick_at` is absent. After a new journal
      record or `tick_at`, a later silence fires again.
- [ ] `ready_not_admitted` rows, `waiting_not_admitted` and `runner_journal_silent` carry
      the lease drain info (`reason, since, draining_s, waiting_on`) when SPEC-0224's
      `draining` is present, else `None`.
- [ ] `ping_digest` includes `waiting_not_admitted`, `runner` and
      `counts.waiting_not_admitted`. Older `conductor_ping` events still validate.
- [ ] `skills/wt-conduct/SKILL.md` has rows for `waiting_not_admitted` and
      `runner_journal_silent` (act at once: restart a stalled runner). The
      `ready_not_admitted` and `conductor_ping` rows mention the new fields.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests**, all holding the real flock through `L.acquire(cfg, ["test"])` as in
  `tests/test_agentic_events.py`. A faked `lease_status` would hide the
  `is_held` / `stale` interplay. Fixed `now` values, no sleeps.
  - `tests/test_agentic_watchdog_gaps.py`:
    - `test_waiting_not_admitted_after_grace`: profile `slots: 2`, one active campaign,
      one WAITING with `waiting.since` 30 s ago gives no event. At 90 s it gives one with
      the listed data. A second `sync` gives no duplicate.
    - `test_waiting_not_admitted_quiet_when_full_or_park_requested`: slots full gives
      nothing. `park_requested` gives nothing.
    - `test_waiting_not_admitted_waits_for_slot_age`: a WAITING item that has waited an
      hour, plus a campaign that went PARKED (`updated_at`) 10 s ago, gives nothing. At
      90 s it fires with `slot_free_s` about 90.
    - `test_runner_journal_silent_once_per_episode`: a held lease with `tick_at` and a
      journal `start` record (`interval_s: 20`, holder pid). At 50 s there is nothing. At
      70 s one event fires (`silent_s` about 70, `heartbeat_age_s` fresh). A repeat sync
      gives nothing. After `lease.update_info(tick_at=…)` the condition clears, and a
      later silence fires a second event.
    - `test_runner_journal_silent_skips_free_stale_or_legacy_lease`: lease released, lease
      stale (old `heartbeat_at`), or no `tick_at` all give nothing.
    - `test_interval_from_start_record`: a `start` record with `interval_s: 5` gives the
      alarm at more than 15 s.
    - `test_drain_info_on_rows`: a lease with `update_info(draining={…})` shows `drain` on
      a `ready_not_admitted` row, on `waiting_not_admitted` and on `runner_journal_silent`
      (`draining`). Without it the value is `None`.
    - `test_run_stamps_tick_at`: `queue.run(once=True)` with a FakeRunner leaves `tick_at`
      in the lease info (read through a hook before release). An `update_info` spy shows
      it was called from the loop.
  - `tests/test_agentic_ping.py`: `test_ping_digest_lists_waiting_and_runner` covers the new
    keys and counts. `test_old_ping_event_validates` checks that a ping without the new
    keys passes `validate`.
- **Existing suites unchanged:** `tests/test_agentic_events.py`,
  `tests/test_agentic_heartbeat.py`, `tests/test_agentic_lease.py`,
  `tests/test_agentic_drain_signal.py` (SPEC-0224), `tests/test_agentic_reexec.py`.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** on the next code merge while a persona run is in flight,
  `wt queue events --tail 10` shows `runner_journal_silent` about 60 s into the drain. If a
  campaign is WAITING, `waiting_not_admitted` (with `drain`) follows 60 s after the slot
  freed.

## Rollout / migration

Lands after SPEC-0224 (it needs `update_info` and `draining`). No data migration. All new
keys are additive. A runner from before this spec has no `tick_at` until its re-exec, so
the silence check stays quiet for it. The live runner re-execs into this code on merge
(SPEC-0194).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None. Decisions locked above._
