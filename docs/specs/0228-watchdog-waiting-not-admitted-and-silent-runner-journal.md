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
  SIGTERM during an open or a close that is within its budget.

**Non-goals**
- Changing admission, the drain (SPEC-0224, SPEC-0225), or where closes run
  ([SPEC-0220](./0220-close-regate-off-main-thread.md)).
- Restarting the runner from the watchdog. The watchdog observes, and the conductor acts.
- Changing `lease_stale` / `runner_dead` semantics.
- Alerting on a stalled drain within its budget. The drain is a declared `busy` step
  (kind `drain`), so `runner_journal_silent` stays quiet while it is within budget.
  Within the drain, `slot_freed_at` only moves on the drain's own harvests: an outside
  park or abandon goes unseen until the re-exec. This gap is accepted. During a drain the
  conductor gets SPEC-0224's `runner_draining` and the `drain` field on
  `ready_not_admitted` / `waiting_not_admitted`.
  [SPEC-0229](./0229-runner-exit-paths-and-draining-ticks-with-detached-runs.md) makes
  drains keep ticking.

## Decision

1. **Loop state in the lease.** Every value is written through SPEC-0224's
   `lease.update_info` / `pop_info`:
   - at start, `poll_interval_s`;
   - after each tick, before `emit`, `tick_at` and `in_flight`, the sorted
     `pool.ids()`;
   - `slot_freed_at`, the time the runner last saw its occupied count drop. It is stamped
     at start too;
   - around every main-thread step that can take long, `busy: {kind, campaign?, since,
     budget_s}` with `kind` in `open` | `close` | `drain`. One context manager,
     `_busy(lease, kind, ...)`, writes it and clears it in `finally`.

   `lease_status()` returns all of these, plus `draining`.
2. **`waiting_not_admitted`.** A WAITING campaign (not `park_requested`) gets a row when
   two conditions hold for longer than `waiting_grace_s` (new setting, default 60): the
   campaign has been waiting (`waiting.since`), and a slot has been free (the lease's
   `slot_freed_at`). The age is measured from the later of the two. A slot is free when
   `occupied < slots`. `occupied` is the union of the active-state campaign ids and the lease's
   `in_flight` ids, the same count the tick uses (`n_busy`).
3. **`runner_journal_silent`.** Silence runs from the newer of `tick_at` and the holder's
   last journal record. The journal alone is not enough, because idle ticks are not
   journaled. The event fires when the silence exceeds `3 × poll_interval_s`, the lease is
   held and not stale, and no in-budget `busy` is set. A `busy` step past `since +
   budget_s + overdue_grace_s` does not suppress it, and the event carries `busy`. One
   event fires per episode: the key carries the last-activity timestamp.
4. **Drain info.** `events._drain_info(lease)` builds `{reason, since, draining_s,
   waiting_on}` or `None`. It goes on `ready_not_admitted`, `waiting_not_admitted` and
   `runner_journal_silent`.
5. **Ping.** `ping_digest` gains the `waiting_not_admitted` rows, `runner` and
   `counts.waiting_not_admitted`. `runner` comes from `events.runner_silence(cfg, now,
   sett)`, the same helper the watchdog uses.

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
- **`slot_freed_at`.** `run()` stamps `slot_freed_at = now_iso()` right after `acquire`.
  A fresh runner has just started, so a slot freed at that moment is the safe reading.
  The loop keeps the last occupied count it saw in memory. That count is
  `len({active-state ids} | pool.ids())`, the tick's `n_busy`. Whenever the count drops,
  the loop stamps `slot_freed_at` in the same `update_info` call. It checks after each tick
  (a harvest, a close or a park inside the tick) and after each harvest in `_drain`. A park
  or abandon from outside the runner shows up as a drop at the next tick. While the loop
  ticks, every way a slot frees is caught within one poll interval, with no guess from
  state rows. During a drain the loop does not tick, so only the drain's own harvests
  re-stamp it (see Non-goals).
- **`busy`.** One context manager, `queue._busy(lease, kind, *, campaign=None,
  budget_s)`. On entry it calls `update_info(busy={kind, campaign, since: now_iso(),
  budget_s})`, and in `finally` it calls `pop_info("busy")`. `tick()` and `_Ctx` gain an
  optional `lease`. It is None in tests and one-shot ticks, and then the manager does
  nothing. Every main-thread step that can take long is wrapped:
  - **`open`.** Each `_do_open` (step 1) runs as `_busy(lease, "open", campaign=cid,
    budget_s=…)`. `_do_open` creates the worktree and then runs every
    `worktrees.setup` command with `subprocess.run`, one after another and with no timeout.
    wt's own setup is `uv sync --extra tui`, and other projects may run `npm ci`. So
    `budget_s` is the sum of the per-command `timeout_s` when setup entries carry one.
    Today they are plain strings, so it is the new events setting `open_budget_s`
    (default 600) for the whole open.
  - **`close`.** Step 4 wraps each `_do_close` in `_busy(lease, "close", campaign=cid,
    budget_s=sett["gate_budget_s"])`. A close rebases and runs the project gates
    in-process, and `gate_budget_s` (1800) is already their budget. Once SPEC-0220 moves
    the close regate onto the pool, the window shrinks to the main-checkout merge phase.
    SPEC-0220 then narrows the wrap to that phase.
  - **`drain`.** `_drain` runs inside `_busy(lease, "drain", budget_s=…)`. The budget is
    the largest `waiting_on[].timeout_s` of SPEC-0224's draining info, or
    `gate_budget_s` for gates. It is computed at drain start.

  Nested use does not happen: steps 1 and 4 run in sequence, and the drain runs outside
  `tick`. The drain's readiness refresh and admission bookkeeping take seconds and are not
  wrapped. A new long main-thread step must use `_busy`.
- `Lease.handover` / adopt drop `busy` (with SPEC-0224's `draining`). They keep `tick_at`
  and `in_flight`, and the new process overwrites both on its first tick. It also
  re-stamps `slot_freed_at` at its start.
- **`lease_status()`** adds `tick_at`, `poll_interval_s`, `in_flight`, `slot_freed_at`,
  `busy` and `draining` to its dict. Each is the info value when `held`, else `None`. If SPEC-0224
  already added `draining`, it is reused. Legacy lease files without the keys read as
  `None`.

### Settings and event types (`events.py`)

- `DEFAULTS["open_budget_s"] = 600` (see `busy`).
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
  switches to the same helper in two places: its early return (`if _occupied(rows, lease)
  >= slots: return []`, replacing `len(active) >= slots`) and its `active` field. A parked
  campaign's in-flight run therefore suppresses that alert as well.

### `waiting_not_admitted`

- `waiting_for_s` is the age of `st["waiting"]["since"]`.
- `slot_free_s` is the age of `lease.slot_freed_at`. The check is skipped when the lease
  is not held (`runner_dead` covers that) or has no `slot_freed_at` (a runner from before
  this spec, until it re-execs). Take a WAITING item that has waited an hour while a
  campaign merged 10 s ago. Then `slot_free_s` is about 10, so nothing fires before the
  next tick's step 0b resumes the item.
- Raising `slots` in the config adds a free slot without a drop in the count, so
  `slot_freed_at` can be old. The runner reloads config each tick (SPEC-0226) and resumes
  the item at once. The alert can only be early within that one interval. This is
  accepted.
- The row fires when `occupied < slots` and `min(waiting_for_s, slot_free_s) >
  waiting_grace_s`. Data: `{waiting_for_s, slot_free_s, resume_to, active (= occupied),
  slots, runner_pid, drain}`. Key: `waiting_not_admitted:<cid>:<waiting.since>`. A
  second wait fires again.

### `runner_journal_silent`

- One helper, `events.runner_silence(cfg, now, sett) -> dict | None`, evaluates the whole
  condition: lease read, journal tail, busy guard. It returns the event data or `None`.
  `watchdog_checks` and `ping_digest` both call it, so the two cannot drift apart.
- The check is skipped when the lease is not held, when it is `stale` (`lease_stale` /
  `runner_dead` cover that), or when `tick_at` is absent. A runner from before this spec
  has no `tick_at`, and skipping it avoids a false alarm until it re-execs.
- `interval = lease.poll_interval_s` (fallback `agentic.poll_interval_s`, default 20).
- `last = max(lease.tick_at, ts of the newest journal record with pid == holder)`. The
  journal is read from its tail with a bounded backwards read, and only the newest record
  of the holder pid is needed. If the bounded tail holds no record of that pid,
  `last = tick_at` alone. That is not an error, and the whole file is not read.
  `silent_s = age(last)`.
- **The reader.** It is a new helper, `queue.journal_tail_for_pid(cfg, pid,
  max_bytes=262144) -> dict | None`. It reads `runner.jsonl` backwards, at most 256 KiB
  from the end, skips the partial first line and unparseable lines, and returns the newest
  record whose `pid` matches, or `None`. It does not use `read_journal`, which reads the
  whole file. `last_journal_ts` is that record's `ts`, or `None` when it is absent.
  `last_tick_at` is `lease.tick_at`. `silent_s` is measured from the newer of the two.
- **Busy guard.** When `lease.busy` is set and `age(busy.since) <= busy.budget_s +
  overdue_grace_s`, the condition is suppressed, for every kind. The loop is in a declared
  step within its budget: a slow open (setup), a close (gates) or a drain. Past that budget the condition
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
- **Relation to `runner_draining`.** Within its budget a drain is reported only by
  SPEC-0224's `runner_draining`. Past `budget_s + overdue_grace_s`,
  `runner_journal_silent` fires as well, with `busy.kind == "drain"`.
  `runner_journal_silent` also covers any undeclared stall, such as a hung main thread.

### Drain info in `ready_not_admitted`

- The rows gain `drain`. SPEC-0224's `runner_draining: bool` stays. `runner_busy` keeps
  its meaning, and the skill row says that `drain` set means stalled, not busy.

### Ping (`ping_digest`)

- `waiting_not_admitted`: the rows.
- `runner`: `runner_silence(cfg, now, sett)`.
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
  while `wt queue list --json` (`.lease`) shows `busy.kind` `open` or `close` within
  `since + budget_s`. A killed close can leave the main checkout mid-merge, and a killed
  open leaves a half-built worktree in OPENING. Wait for the next event. An open or close
  past its budget arrives as `runner_journal_silent` with `busy` set. Dispatch a sub-agent
  to check the worktree or main checkout and the process before any restart. A `drain`
  busy is not guarded. The existing restart policy for drains applies.
- The `ready_not_admitted` row mentions `drain`. The `conductor_ping` row lists
  `waiting_not_admitted` and `runner`.
- **Relation to SPEC-0224's planned `runner_draining` row** ("do not SIGTERM"). After
  0224 was accepted, the human decided that a stalled queue is worse than an interrupted
  persona run (commit a43fef6). When both events fire, the `runner_journal_silent` row
  decides, within the close guard. This spec depends on 0224, so that row exists when
  this spec is built. This spec edits it to add: "if `runner_journal_silent` also fired,
  follow that row (with the close guard)". The skill then has no two contradictory rows.

## Alternatives considered

- **Journal every idle tick.** This would give a pure journal signal, but it adds about
  4300 lines a day for no other use. The lease stamp is one extra write. Rejected.
- **Find the interval from the journal `start` record.** A tail window can miss it on a
  long-lived runner. The lease holds it directly. Rejected (review).
- **Count occupancy from state rows only.** Flights the watchdog cannot see would be
  missing, and alerts would come late or wrong. Publishing `in_flight` is one list per
  tick. Rejected.
- **Estimate when a slot freed from state rows (newest `updated_at` of non-active
  campaigns).** `store.list_campaigns` hides terminal rows by default, so the common
  merge case false-alarmed, and the estimate was indirect anyway. The runner knows when
  its count drops. Rejected (review).
- **Track when each condition was first seen, in the projector.** This makes
  `watchdog_checks` stateful. `slot_freed_at` gives the same answer without that.
  Rejected.
- **Fold into SPEC-0224.** 0224 is accepted, so rewriting it would break the timeline.
  Rejected.
- **Reuse `ready_not_admitted` for WAITING items.** WAITING items have no
  `backlog.since`, and a separate type keeps the rows clear. Rejected.

## Acceptance criteria

- [ ] `queue.run` writes `poll_interval_s` and `slot_freed_at` at start. After each
      tick, before `emit`, it writes `tick_at` and `in_flight`. It re-stamps
      `slot_freed_at` whenever its occupied count drops, after a tick or after a drain
      harvest. Around every main-thread open, close and drain it writes `busy: {kind,
      campaign?, since, budget_s}` through one `_busy` manager, cleared in `finally` even
      when the step raises. All of these go through `update_info` / `pop_info`. Handover drops
      `busy`.
- [ ] `lease_status()` returns `tick_at`, `poll_interval_s`, `in_flight`,
      `slot_freed_at`, `busy` and `draining` while the lease is held, and `None` for each otherwise or when the key is
      absent.
- [ ] `watchdog_checks` emits `waiting_not_admitted` (`waiting_for_s, slot_free_s,
      resume_to, active, slots, runner_pid, drain`) for a WAITING campaign when `occupied <
      slots` and `min(waiting_for_s, age(slot_freed_at)) > waiting_grace_s`. `occupied`
      counts the lease's `in_flight` ids, and so does `ready_not_admitted_items`' early
      return. It does not fire within the grace, with slots occupied, for
      `park_requested`, or soon after a slot freed (merge included). It fires once per
      wait.
- [ ] `watchdog_checks` emits `runner_journal_silent` (`pid, last_journal_ts,
      last_tick_at, silent_s, interval_s, heartbeat_age_s, draining, busy`) when the lease
      is held and not stale and `tick_at` is present. The newer of `tick_at` and the
      holder's last journal record must be older than `3 × lease.poll_interval_s`. It
      fires exactly once per episode, and fires again after `last` moves.
- [ ] `runner_journal_silent` is suppressed while `busy` (any kind) is within `budget_s +
      overdue_grace_s`. Past that budget it fires, with `busy` in its data.
- [ ] `ready_not_admitted` rows, `waiting_not_admitted` and `runner_journal_silent` carry
      the drain info (`reason, since, draining_s, waiting_on`) or `None`.
      `ready_not_admitted.active` uses the same occupied count.
- [ ] `ping_digest` includes `waiting_not_admitted`, `runner` (from the shared
      `runner_silence` helper) and `counts.waiting_not_admitted`. Older `conductor_ping` events still validate.
- [ ] `skills/wt-conduct/SKILL.md` has rows for `waiting_not_admitted` and
      `runner_journal_silent` (act at once by restarting a stalled runner). It has the
      close guard (never SIGTERM while `busy.kind` is `open` or `close` within its
      budget). The
      `ready_not_admitted` and `conductor_ping` rows mention the new fields. SPEC-0224's
      `runner_draining` row points to the `runner_journal_silent` row when both fire.
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
      sync gives no duplicate. Changing `waiting.since` (the campaign resumed and waited
      again) and syncing past the grace gives a second event.
    - `test_waiting_not_admitted_counts_in_flight`: same setup with `in_flight` listing a
      parked campaign's id gives no event, because both slots are occupied.
    - `test_ready_not_admitted_counts_in_flight`: the same `in_flight` setup with a ready
      QUEUED item gives no `ready_not_admitted`, because of the early return.
    - `test_waiting_not_admitted_quiet_when_full_or_park_requested`.
    - `test_waiting_not_admitted_waits_for_slot_age`: a WAITING item that has waited an
      hour, with `slot_freed_at` 10 s ago (a campaign MERGED 10 s ago), gives nothing. At
      90 s it fires. A held lease without `slot_freed_at` gives nothing.
    - `test_run_stamps_slot_freed_at_on_drop`: a FakeRunner run whose campaign closes
      (merges) during a tick moves `slot_freed_at` past its start value. A tick with no
      drop leaves it unchanged.
    - `test_runner_journal_silent_once_per_episode`: `poll_interval_s: 20`, `tick_at` set.
      At 50 s nothing fires, at 70 s one event fires, and a repeat sync gives nothing. A
      new `tick_at` clears it, and a later silence fires again.
    - `test_runner_journal_silent_skips_free_stale_or_legacy_lease`.
    - `test_runner_journal_silent_suppressed_during_close`: `busy` with kind close, since
      5 min ago and `budget_s: 1800`, gives nothing at 10 min of silence. With since 35
      min ago it fires, and `busy` is in its data.
    - `test_journal_record_counts_as_activity`: an old `tick_at` plus a fresh holder-pid
      journal record gives nothing. A record from another pid does not count. A tail with
      no holder record falls back to `tick_at`, and `last_journal_ts` is `None`.
    - `test_journal_tail_for_pid_bounded`: in a journal larger than 256 KiB, a holder
      record past the window gives `None`, and the newest holder record inside it is
      returned. A partial or garbage line is skipped.
    - `test_drain_info_on_rows`: a lease with `draining` shows `drain` on all three event
      types. Without it the value is `None`.
  - `tests/test_agentic_runner.py` (or `test_agentic_nonblocking.py`):
    - `test_run_stamps_loop_state_before_emit`: `queue.run(once=True)` with a FakeRunner
      and an `on_tick` hook that reads `read_lease()`. The hook sees `tick_at`,
      `in_flight` and `poll_interval_s`, which shows they were written before `emit`.
    - `test_close_sets_and_clears_busy`: a campaign in CLOSING with a patched `_do_close`
      reads `read_lease()["busy"]` (kind `close`, campaign id, `budget_s` =
      `gate_budget_s`) from inside the close. After the tick `busy` is gone. A
      `_do_close` that raises also leaves no `busy`.
    - `test_open_sets_and_clears_busy`: a project whose `worktrees.setup` runs a command
      that reads the lease file sees `busy.kind == "open"` with `budget_s` =
      `open_budget_s`, and `busy` is gone after the open. A failing setup also leaves no
      `busy`.
    - `test_drain_stamps_busy_in_flight_and_slot_freed_at`: a FakeRunner `sleep_s` flight
      is in flight when a code change is patched in. During `_drain`, `busy.kind ==
      "drain"`. When the flight is harvested and its campaign parks, `in_flight` loses it,
      `slot_freed_at` moves, and `tick_at` stays unchanged. After the drain `busy` is
      gone.
    - `test_runner_journal_silent_suppressed_during_open_and_drain`: in-budget `busy`
      kinds `open` and `drain` suppress the event. Past budget, it fires.
  - `tests/test_agentic_ping.py`: `test_ping_digest_lists_waiting_and_runner` (its
    `runner` equals `runner_silence(...)`) and `test_old_ping_event_validates`.
  - `tests/test_skills.py`, which reads `skills/wt-conduct/SKILL.md`:
    - `test_new_watchdog_rows_present`: rows exist for `waiting_not_admitted` and
      `runner_journal_silent`.
    - `test_close_guard_in_rows`: `busy.kind` guard text (open/close) appears in both new
      rows and in the `ready_not_admitted` row.
    - `test_rows_mention_new_fields`: the `ready_not_admitted` row mentions `drain`, and
      the `conductor_ping` row mentions `waiting_not_admitted` and `runner`.
    - `test_runner_draining_row_points_to_silent_row`.
- **Existing suites unchanged:** `tests/test_agentic_events.py`,
  `tests/test_agentic_heartbeat.py`, `tests/test_agentic_drain_signal.py` (SPEC-0224),
  `tests/test_agentic_reexec.py`, `tests/test_agentic_e2e.py`.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** on the next code merge while a persona run is in flight,
  `wt queue events --tail 10` shows `runner_draining` (SPEC-0224) and, for a WAITING
  campaign, `waiting_not_admitted` with `drain`, and no `runner_journal_silent` within the
  drain budget. During a normal close or a slow `uv sync` open, no `runner_journal_silent`
  appears.

## Rollout / migration

This spec lands after SPEC-0224, because it needs `update_info`, `pop_info` and
`draining`. No data migration is needed, and all keys are additive. A runner from before
this spec has no `tick_at` until its re-exec, so the silence check stays quiet for it. The
live runner re-execs into this code on merge (SPEC-0194). When SPEC-0220 lands, its
implementer narrows the `close` `_busy` wrap to the merge phase.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None. Decisions locked above._
