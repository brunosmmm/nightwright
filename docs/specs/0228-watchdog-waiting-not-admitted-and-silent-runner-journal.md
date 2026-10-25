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
([SPEC-0200](./0200-waiting-state-started-campaigns-wait-for-a-slot.md)) with slots free,
and the runner did not pick them up. The human noticed, not the conductor. The cause was a
code-change re-exec drain (`IDEA-100`,
[SPEC-0224](./0224-honest-run-cost-on-interrupt-and-a-runner-draining.md)). The watchdog
([SPEC-0212](./0212-periodic-conductor-ping.md), `events.watchdog_checks`) missed it in
three ways:

1. `ready_not_admitted_items` only covers QUEUED items with stored readiness. It listed
   SPEC-0222 and never 0202 or 0219. No check covers WAITING campaigns. The
   `conductor_ping` `waiting` list shows them but does not say whether a slot is free.
2. Nothing fires when the loop stops ticking but the process stays alive. The heartbeat
   thread (SPEC-0204) keeps beating during a drain, so `lease_stale` never fires.
   `runner_dead` needs the flock to be free.
3. During a drain, `ready_not_admitted` carries `runner_busy: true`, which the conductor
   reads as "fine, wait".

SPEC-0224 adds three things this spec builds on: the `runner_draining` event for a drain
older than 60 s, the lease's `draining` info with the locked `update_info`/`pop_info` API,
and `runner_draining: bool` on `ready_not_admitted` rows. This spec adds the two missing
conditions and the runner-state facts the conductor needs to act at once, safely. The
conductor skill already acts at once on a stalled queue (commit a43fef6, rows
`ready_not_admitted` and `merged`). The new events get matching rows, plus a guard so the
conductor never kills a runner in the middle of a merge.

## Goals / Non-goals

**Goals**
- A WAITING campaign that is not resumed although a slot has been free longer than a
  grace raises `waiting_not_admitted`.
- A runner whose loop has not ticked for 3 poll intervals raises `runner_journal_silent`,
  once per silence episode. This applies while its lease is held, its heartbeat is fresh,
  and it is not inside a declared long main-thread step that is still within its budget.
- The runner publishes its loop state in the lease info, and `lease_status()` exposes it.
  The fields are `tick_at`, `poll_interval_s`, `in_flight` and `busy`, plus SPEC-0224's
  `draining`. The watchdog reads facts, not guesses.
- `ready_not_admitted` and the new events carry the drain info.
- The `conductor_ping` digest lists the new conditions.
- `skills/wt-conduct/SKILL.md` has rows for both new events: act at once, except never
  SIGTERM during a close that is within its budget.

**Non-goals**
- Changing admission, the drain (SPEC-0224, SPEC-0225), or where closes run
  ([SPEC-0220](./0220-close-regate-off-main-thread.md)).
- Restarting the runner from the watchdog. The watchdog observes, and the conductor acts.
- Changing `lease_stale` / `runner_dead` semantics.

## Decision

1. **Loop state in the lease.** Every value is written through SPEC-0224's
   `lease.update_info` / `pop_info`:
   - at start, `poll_interval_s`;
   - after each tick, before `emit`, `tick_at` and `in_flight`, the sorted
     `pool.ids()`;
   - around every long main-thread step, `busy: {kind, campaign, since, timeout_s}`,
     cleared in `finally`. Today that step is the close.

   `lease_status()` returns all of these, plus `draining`.
2. **`waiting_not_admitted`.** A WAITING campaign (not `park_requested`) gets a row when
   two conditions hold for longer than `waiting_grace_s` (new setting, default 60): the
   campaign has been waiting, and a slot has been free. A slot is free when `occupied <
   slots`. `occupied` is the union of the active-state campaign ids and the lease's
   `in_flight` ids, the same count the tick uses (`n_busy`).
3. **`runner_journal_silent`.** Silence runs from the newer of `tick_at` and the holder's
   last journal record. The journal alone is not enough, because idle ticks are not
   journaled. The event fires when the silence exceeds `3 × poll_interval_s`, the lease is
   held and not stale, and no in-budget `busy` is set. A `busy` step past `since +
   timeout_s + overdue_grace_s` does not suppress it, and the event carries `busy`. One
   event fires per episode: the key carries the last-activity timestamp.
4. **Drain info.** `events._drain_info(lease)` builds `{reason, since, draining_s,
   waiting_on}` or `None`. It goes on `ready_not_admitted`, `waiting_not_admitted` and
   `runner_journal_silent`.
5. **Ping.** `ping_digest` gains the `waiting_not_admitted` rows, `runner` (the current
   silence row or `None`) and `counts.waiting_not_admitted`.

## Design

### Lease info and `lease_status` (`lease.py`, `queue.py`)

- **`poll_interval_s`.** `run()` calls `lease.update_info(poll_interval_s=interval)`
  right after `acquire`. The value is the real interval, including a CLI override. This
  replaces any scan of `runner.jsonl` for the `start` record. An adopted lease (re-exec)
  gets it rewritten by the new process.
- **`tick_at`, `in_flight`.** In the `run()` loop, after `tick()` returns and before
  `emit(report)`, the loop calls `lease.update_info(tick_at=store.now_iso(),
  in_flight=sorted(pool.ids()))`. Writing before `emit` means a slow `on_tick` hook (the
  live `wt queue run` display) never delays the stamp. The heartbeat thread never writes
  either key. They stay old during a drain or a hung main thread, which is the case to
  catch. The drain (`_drain`) refreshes `in_flight` whenever it harvests a flight, using
  the same call without `tick_at`.
- **`busy`.** A context manager, `queue._main_busy(lease, kind, campaign, timeout_s)`,
  calls `update_info(busy={kind, campaign, since: now_iso(), timeout_s})` on entry and
  `pop_info("busy")` in `finally`. `tick()` and `_Ctx` gain an optional `lease` (None in
  tests and one-shot ticks, where the manager does nothing). Step 4 of `_tick` wraps
  each `_do_close` in `_main_busy(lease, "close", cid, sett["gate_budget_s"])`. A close
  rebases and runs the project gates in-process, and `gate_budget_s` is already their
  budget (1800). The close is the only long main-thread step today. Opening a worktree and
  the drain's readiness refresh take seconds. Any future long main-thread step uses the
  same manager. Once SPEC-0220 moves the close regate onto the pool, the window shrinks to
  the main-checkout merge phase. SPEC-0220 then moves `_main_busy` to wrap only that phase.
- `Lease.handover` / adopt drop `busy` (with SPEC-0224's `draining`). They keep `tick_at`
  and `in_flight`, and the new process overwrites both on its first tick.
- **`lease_status()`** adds `tick_at`, `poll_interval_s`, `in_flight`, `busy` and
  `draining` to its dict. Each is the info value when `held`, else `None`. If SPEC-0224
  already added `draining`, it is reused. Legacy lease files without the keys read as
  `None`.

### Settings and event types (`events.py`)

- `DEFAULTS["waiting_grace_s"] = 60`. This is separate from `ready_grace_s`, because
  WAITING items resume ahead of the backlog (SPEC-0200) and may be tuned on their own.
  `DEFAULTS["journal_silent_factor"] = 3`. Overrides go in `agentic.events`.
- `EVENT_TYPES["waiting_not_admitted"] = ("waiting_for_s", "active", "slots")`.
- `EVENT_TYPES["runner_journal_silent"] = ("last_journal_ts", "silent_s",
  "heartbeat_age_s")`.
- `ready_not_admitted` and `conductor_ping` keep their required keys. The new keys are
  additive, so events already in the stream keep validating.

### Occupied slots

- `events._occupied(rows, lease)` returns the size of the union of two id sets: campaigns
  in `ACTIVE_STATES`, and `lease["in_flight"]`. The watchdog runs out of process and cannot
  see the pool, so the runner publishes its flights. A campaign parked while its run is
  still in flight therefore counts, as it does in `_tick`. Without `in_flight` (legacy
  lease, runner down) the count is the state rows alone. `ready_not_admitted_items`
  switches to the same helper, and its `active` field becomes this count.

### `waiting_not_admitted`

- `waiting_for_s` is the age of `st["waiting"]["since"]`.
- `slot_free_s` estimates how long a slot has been free. Reading every ledger on each 2 s
  sync costs too much. The estimate is the age of the newest `updated_at` among campaigns
  that are neither in `ACTIVE_STATES` nor QUEUED/WAITING (parked, merged, handed off,
  abandoned). With none, it is `waiting_for_s`. A later `updated_at` on such a row (a note
  on a parked item) only delays the alert, by at most one grace. That is the safe
  direction: a long wait plus a slot freed seconds ago must not fire before the next tick.
  Raising `slots` in the config can overstate `slot_free_s`. The runner reloads config
  each tick (SPEC-0226), so the error lasts less than one poll interval. It is accepted.
- The row fires when `occupied < slots` and `min(waiting_for_s, slot_free_s) >
  waiting_grace_s`. Data: `{waiting_for_s, slot_free_s, resume_to, active (= occupied),
  slots, runner_pid, drain}`. Key: `waiting_not_admitted:<cid>:<waiting.since>`. A
  second wait fires again.

### `runner_journal_silent`

- The check is skipped when the lease is not held, when it is `stale` (`lease_stale` /
  `runner_dead` cover that), or when `tick_at` is absent. A runner from before this spec
  has no `tick_at`, and skipping it avoids a false alarm until it re-execs.
- `interval = lease.poll_interval_s` (fallback `agentic.poll_interval_s`, default 20).
- `last = max(lease.tick_at, ts of the newest journal record with pid == holder)`. The
  journal is read from its tail with a bounded backwards read, and only the newest record
  of the holder pid is needed. `silent_s = age(last)`.
- **Busy guard.** When `lease.busy` is set and `age(busy.since) <= busy.timeout_s +
  overdue_grace_s`, the condition is suppressed. The loop is in a declared step within its
  budget. A close that takes minutes is the main case. Past that budget the condition
  fires, and its data includes `busy`.
- It fires when `silent_s > journal_silent_factor × interval`. Key:
  `runner_journal_silent:<pid>:<last>`. The condition holds for one episode and clears
  when `last` moves, and `_watch` re-arms it, so it fires once per episode. A busy
  suppression does not change the key. If the busy step ends and the silence goes on, the
  condition fires then.
- Data: `{pid, last_journal_ts, last_tick_at, silent_s, interval_s, heartbeat_age_s,
  draining, busy}`. `draining` is the drain dict or `None`. `busy` is set only when the
  step is over its budget, else `None`.
- **Why 3×.** One interval is the normal gap, and a tick that admits and opens worktrees
  can take one more. At a 20 s poll the alarm comes at 60 s, in line with the other
  graces.
- **Overlap with `runner_draining`.** Both events can fire for one drain, at about 60 s.
  That is intended. `runner_journal_silent` covers any loop stall, drain or not.

### Drain info in `ready_not_admitted`

- The rows gain `drain`. SPEC-0224's `runner_draining: bool` stays. `runner_busy` keeps
  its meaning, and the skill row says that `drain` set means stalled, not busy.

### Ping (`ping_digest`)

- `waiting_not_admitted`: the rows.
- `runner`: the `runner_journal_silent` data when it holds now, else `None`.
- `counts.waiting_not_admitted`: the number of those rows.

### Conductor skill (`skills/wt-conduct/SKILL.md`)

- New row **`waiting_not_admitted`**: same action as `ready_not_admitted`, within one
  event. If `drain` is set or the journal is silent, SIGTERM and restart the runner (step
  2). If it is dead, start it. Note the action on the campaign. Escalate only if admission
  still fails after a restart.
- New row **`runner_journal_silent`**: the loop is stalled while alive. SIGTERM and
  restart the runner immediately (step 2). An interrupted persona run is cheaper than a
  stalled queue: its cost is marked estimated (SPEC-0224), and its worktree edits survive.
  If it recurs within minutes of a restart, read `wt queue runner-log --tail 30` and
  dispatch a sub-agent to diagnose.
- **Close guard**, stated in both new rows and in `ready_not_admitted`: never SIGTERM
  while `wt queue list --json` (`.lease`) shows `busy.kind == "close"` within `since +
  timeout_s`. A killed close can leave the main checkout mid-merge. Wait for the next
  event. A close past its budget arrives as `runner_journal_silent` with `busy` set: dispatch
  a sub-agent to check the main checkout and the gate process before any restart.
- The `ready_not_admitted` row mentions `drain`. The `conductor_ping` row lists
  `waiting_not_admitted` and `runner`.
- **Relation to SPEC-0224's planned `runner_draining` row** ("do not SIGTERM"). After
  0224 was accepted, the human decided that a stalled queue is worse than an interrupted
  persona run (commit a43fef6). When both events fire, the `runner_journal_silent` row
  decides, within the close guard. Whoever builds 0224's row adds that precedence note.

## Alternatives considered

- **Journal every idle tick.** This would give a pure journal signal, but it adds about
  4300 lines a day for no other use. The lease stamp is one extra write. Rejected.
- **Find the interval from the journal `start` record.** A tail window can miss it on a
  long-lived runner. The lease holds it directly. Rejected (review).
- **Count occupancy from state rows only.** Flights the watchdog cannot see would be
  missing, and alerts would come late or wrong. Publishing `in_flight` is one list per
  tick. Rejected.
- **Track when each condition was first seen, in the projector.** This gives an exact
  "slot free for N s", but makes `watchdog_checks` stateful, and the `updated_at` estimate
  only errs toward delay. Rejected for now.
- **Fold into SPEC-0224.** 0224 is accepted, so rewriting it would break the timeline.
  Rejected.
- **Reuse `ready_not_admitted` for WAITING items.** WAITING items have no
  `backlog.since`, and a separate type keeps the rows clear. Rejected.

## Acceptance criteria

- [ ] `queue.run` writes `poll_interval_s` at start. After each tick, before `emit`, it
      writes `tick_at` and `in_flight`. Around every main-thread close it writes `busy:
      {kind: "close", campaign, since, timeout_s}`, cleared in `finally` even when the
      close raises. All of these go through `update_info` / `pop_info`. Handover drops
      `busy`.
- [ ] `lease_status()` returns `tick_at`, `poll_interval_s`, `in_flight`, `busy` and
      `draining` while the lease is held, and `None` for each otherwise or when the key is
      absent.
- [ ] `watchdog_checks` emits `waiting_not_admitted` (`waiting_for_s, slot_free_s,
      resume_to, active, slots, runner_pid, drain`) for a WAITING campaign when `occupied <
      slots` and `min(waiting_for_s, slot_free_s) > waiting_grace_s`. `occupied` counts
      the lease's `in_flight` ids. It does not fire within the grace, with slots
      occupied, for `park_requested`, or soon after a slot freed. It fires once per wait.
- [ ] `watchdog_checks` emits `runner_journal_silent` (`pid, last_journal_ts,
      last_tick_at, silent_s, interval_s, heartbeat_age_s, draining, busy`) when the lease
      is held and not stale and `tick_at` is present. The newer of `tick_at` and the
      holder's last journal record must be older than `3 × lease.poll_interval_s`. It
      fires exactly once per episode, and fires again after `last` moves.
- [ ] `runner_journal_silent` is suppressed while `busy` is within `timeout_s +
      overdue_grace_s`. Past that budget it fires, with `busy` in its data.
- [ ] `ready_not_admitted` rows, `waiting_not_admitted` and `runner_journal_silent` carry
      the drain info (`reason, since, draining_s, waiting_on`) or `None`.
      `ready_not_admitted.active` uses the same occupied count.
- [ ] `ping_digest` includes `waiting_not_admitted`, `runner` and
      `counts.waiting_not_admitted`. Older `conductor_ping` events still validate.
- [ ] `skills/wt-conduct/SKILL.md` has rows for `waiting_not_admitted` and
      `runner_journal_silent` (act at once by restarting a stalled runner). It has the
      close guard (never SIGTERM while `busy.kind == "close"` within its budget), and the
      `ready_not_admitted` and `conductor_ping` rows mention the new fields.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests.** Every test holds the real flock through `L.acquire(cfg, ["test"])`,
  as `tests/test_agentic_events.py` does, and sets lease fields through `update_info`. A
  faked `lease_status` would hide the `is_held` / `stale` interplay. `now` values are
  fixed, with no sleeps.
  - `tests/test_agentic_lease.py`:
    - `test_lease_status_exposes_loop_state`: after `update_info(tick_at=…,
      poll_interval_s=20, in_flight=["c1"], busy={…}, draining={…})`, `lease_status()`
      returns each value. After `release()` each is `None`. A legacy info file without the
      keys gives `None`.
    - `test_handover_drops_busy`: an adopted lease has no `busy` but keeps `tick_at`.
  - `tests/test_agentic_watchdog_gaps.py`:
    - `test_waiting_not_admitted_after_grace`: `slots: 2`, one active campaign, one
      WAITING. At 30 s there is no event. At 90 s there is one, with the data. A repeat
      sync gives no duplicate.
    - `test_waiting_not_admitted_counts_in_flight`: same setup with `in_flight` listing a
      parked campaign's id gives no event, because both slots are occupied.
    - `test_waiting_not_admitted_quiet_when_full_or_park_requested`.
    - `test_waiting_not_admitted_waits_for_slot_age`: a WAITING item that has waited an
      hour, plus a campaign parked 10 s ago, gives nothing. At 90 s it fires.
    - `test_runner_journal_silent_once_per_episode`: `poll_interval_s: 20`, `tick_at` set.
      At 50 s nothing fires, at 70 s one event fires, and a repeat sync gives nothing. A
      new `tick_at` clears it, and a later silence fires again.
    - `test_runner_journal_silent_skips_free_stale_or_legacy_lease`.
    - `test_runner_journal_silent_suppressed_during_close`: `busy` with kind close, since
      5 min ago and `timeout_s: 1800`, gives nothing at 10 min of silence. With since 35
      min ago it fires, and `busy` is in its data.
    - `test_journal_record_counts_as_activity`: an old `tick_at` plus a fresh holder-pid
      journal record gives nothing. A record from another pid does not count.
    - `test_drain_info_on_rows`: a lease with `draining` shows `drain` on all three event
      types. Without it the value is `None`.
  - `tests/test_agentic_runner.py` (or `test_agentic_nonblocking.py`):
    - `test_run_stamps_loop_state_before_emit`: `queue.run(once=True)` with a FakeRunner
      and an `on_tick` hook that reads `read_lease()`. The hook sees `tick_at`,
      `in_flight` and `poll_interval_s`, which shows they were written before `emit`.
    - `test_close_sets_and_clears_busy`: a campaign in CLOSING with a patched `_do_close`
      reads `read_lease()["busy"]` (kind `close`, campaign id, `timeout_s` =
      `gate_budget_s`) from inside the close. After the tick `busy` is gone. A
      `_do_close` that raises also leaves no `busy`.
  - `tests/test_agentic_ping.py`: `test_ping_digest_lists_waiting_and_runner` and
    `test_old_ping_event_validates`.
- **Existing suites unchanged:** `tests/test_agentic_events.py`,
  `tests/test_agentic_heartbeat.py`, `tests/test_agentic_drain_signal.py` (SPEC-0224),
  `tests/test_agentic_reexec.py`, `tests/test_agentic_e2e.py`.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** on the next code merge while a persona run is in flight,
  `wt queue events --tail 10` shows `runner_journal_silent` about 60 s into the drain. A
  WAITING campaign then shows `waiting_not_admitted` with `drain`. During a normal close
  that takes several minutes, no `runner_journal_silent` appears.

## Rollout / migration

This spec lands after SPEC-0224, because it needs `update_info`, `pop_info` and
`draining`. No data migration is needed, and all keys are additive. A runner from before
this spec has no `tick_at` until its re-exec, so the silence check stays quiet for it. The
live runner re-execs into this code on merge (SPEC-0194). When SPEC-0220 lands, its
implementer narrows `_main_busy` to the merge phase.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None. Decisions locked above._
