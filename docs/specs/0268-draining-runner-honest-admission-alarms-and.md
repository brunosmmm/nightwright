---
id: SPEC-0268
title: "Draining runner: honest admission alarms and observable draining ticks"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-361
parent: SPEC-0169
tags: [agentic, queue, watchdog, drain]
---

## Context

IDEA-361 reported that during a code-change drain the runner "stops admitting". The report
came from runner pid 84847 at 2026-10-27 02:04–02:12. That runner detected `code_changed` at
02:04:08 after the SPEC-0261 merge. Its journal then showed only `drain` lines, with no
`tick N` and no `admitted`. `ready_not_admitted` fired for SPEC-0265 (`ready_for_s` 1060,
`active` 5/6). [SPEC-0229](./0229-runner-exit-paths-and-draining-ticks-with-detached-runs.md)
(Draining ticks, Interplay with SPEC-0224) says admission continues while draining.

The code and the data tell a different story from the report.

- **The loop keeps ticking while it drains.** `src/wt/agentic/queue.py:run()` calls
  `tick(..., draining=True)` once per `poll_interval_s`. The inter-tick wait (`while not
  stop.is_set()` with `if draining:`) breaks at `deadline`, and nothing in it blocks for longer
  than 0.2 s. `_tick` step 1 (admission) has no `draining` condition; only step 2 skips
  `gate` / `close` / `resolve` / `write_spec` while draining. The journal's tick counter shows
  this: it rose from 85 to 108 between 02:04:28 and 02:12:25.
- **The slots were full.** Idle ticks are not journaled (`emit` writes only when a tick has
  steps, admissions, errors or starts; see SPEC-0228). The `drain` lines are the
  harvests inside the wait loop: `emit(rep)` with `rep.drain=True`. `cli_queue.py` renders
  them as `drain` instead of `tick N` (`head.append("drain" if rec.get("drain") else …)`).
  The ledgers show that MERGE-20261027-0d7d parked `base_moving` at 02:08:32 and the
  conductor resumed it to CLOSING at 02:08:38. Between those two moments the occupancy was
  5/6, and the rest of the time it was 6/6: SPEC-0263/0264 IMPLEMENTING, SPEC-0262/0250
  GATING, SPEC-0260 and the MERGE in CLOSING. While draining, the tick did not plan the
  closes of the two CLOSING campaigns, but they still held their slots. SPEC-0265 was admitted
  at 02:17:15, the first tick after the MERGE parked again.
- **The alarm was wrong.** `events.py:ready_not_admitted_items` takes the item's age from
  `backlog.since`, which was 01:50:53, the moment it became *ready*. Any instant where
  `_occupied < slots` makes it fire. Here that instant was a six-second free slot, which the
  conductor's own resume refilled. `waiting_not_admitted_items` already handles this
  correctly. It uses `min(wait age, slot-free age)`, with the age of the free slot taken from
  the lease's `slot_freed_at` (SPEC-0228 / SPEC-0263).

So nothing ever stopped admission. Two things misled the conductor. The ready alarm ignores
how long the slot has been free, and while slots are full nothing in the journal says why a
ready item is waiting.

## Goals / Non-goals

**Goals**
- `ready_not_admitted` fires only when the item has been ready **and** a slot has been free
  for longer than `ready_grace_s`, the same rule as `waiting_not_admitted`.
- When ready backlog items wait because the slots are full, the runner journal says so once
  per change: who holds the slots, and which holders a draining tick is deferring.
- `wt queue runner-log` shows draining harvest records as `tick N (drain)`, so a draining loop
  no longer reads as "no ticks".
- A regression test pins SPEC-0229: a draining tick admits into a free slot even while a gate
  or close flight blocks the exec.

**Non-goals**
- Changing slot accounting. A CLOSING campaign keeps its slot while a draining tick defers its
  close, as SPEC-0200 and SPEC-0228 require. See Alternatives.
- Shortening drains. Gates of about 12 minutes bound a drain; that is a separate concern.
- Any change to `wt queue resume`.

## Decision

1. `ready_not_admitted_items` computes `free_s` from the held lease's `slot_freed_at`, as
   `waiting_not_admitted_items` does. A row fires only when `min(ready_for_s, free_s) >
   ready_grace_s`, and it carries `slot_free_s`. When there is no held lease, or the lease has
   no `slot_freed_at`, the alarm keeps today's behaviour and uses the ready age alone. A dead
   runner must still raise the alarm.
2. A new runner journal kind, `admission_blocked`, is written by `run()` after a tick. It is
   written when that tick had ready QUEUED items, admitted none of them, and had no free slot.
   It is written only when the set of slot holders or the set of waiting ready ids changed
   since the last such record (dedupe). Fields: `tick`, `ready` (ids), `slots`, `occupied`,
   `holders` (`[{campaign, state, deferred}]`, where `deferred` is true when this draining tick
   skipped that campaign's step) and `draining` (bool). The dedupe key resets when a tick
   admits or a slot is free, so a later recurrence of the condition is journaled again.
3. `cli_queue.py`'s runner-log row renders a record with `drain: true` and a `tick` as
   `tick N (drain)`. A record with `drain: true` and no tick, from `_drain` or the crash sweep,
   still renders as `drain`. It also renders `admission_blocked` as one line: `⏳ admission
   blocked: N ready, slots S/S (held: id STATE[ deferred], …)`.

## Design

- **`src/wt/agentic/events.py:ready_not_admitted_items`.** After `lease =
  L.lease_status(...)`, compute `freed = _age_s(lease.get("slot_freed_at"), now) if
  lease.get("held") else None`. In the per-row loop, set `age_eff = age if freed is None else
  min(age, freed)` and require `age_eff > sett["ready_grace_s"]`. Add `"slot_free_s":
  round(freed) if freed is not None else None` to the row. The `ready_not_admitted` entry in
  `EVENT_TYPES` (line ~52) stays `("ready_for_s", "active", "slots")`; the extra key goes
  through the existing `{k: v for k, v in r_.items() …}` pass-through in the watchdog (line
  ~701).
- **`src/wt/agentic/queue.py`.**
  - `TickReport` gains `blocked: dict | None = None`. It is set in `_tick` step 1 when `ready`
    holds at least one ready, non-`park_requested` QUEUED id that is not in `admitted_ids` and
    `n_busy() >= slots`. Its value is `{"ready": [...], "slots": slots, "occupied": n_busy(),
    "holders": [...]}`, where the holders are `busy` ids ∪ `flying`, each with its state.
  - Step 2's draining `continue` (the `if draining and (step.kind in (...)` branch) appends
    the id to a local `deferred` set, and `blocked["holders"][i]["deferred"]` is filled from
    that set at the end of `_tick`. `to_dict` does not emit `blocked`, so tick records keep
    their shape.
  - `run()` keeps `last_blocked_key = None`. After `emit(report)`, if `report.blocked`, it
    builds `key = (frozenset(ready), frozenset(holder ids), frozenset(deferred ids))`. When
    `key != last_blocked_key` it calls `journal(cfg, "admission_blocked", tick=ticks,
    draining=draining, **report.blocked)` and stores the key. A tick with `report.admitted` or
    without `blocked` resets the key to `None`.
- **`src/wt/agentic/cli_queue.py`** (runner-log row, line ~613): `head.append(f"tick
  {rec['tick']} (drain)" if rec.get("drain") and rec.get("tick") is not None else ("drain" if
  rec.get("drain") else f"tick {rec.get('tick')}"))`, plus a branch for `kind ==
  "admission_blocked"` placed before the generic tick rendering.
- **`skills/wt-conduct/SKILL.md`**, `ready_not_admitted` row: say that the alarm now means "a
  slot has been free for over `ready_grace_s`" (`data.slot_free_s`). Before restarting
  anything, check `wt queue runner-log --tail 5` for `admission_blocked` (the slots are full,
  and the holders are listed).

## Alternatives considered

- **Release the slots of CLOSING campaigns whose close a draining tick defers.** Rejected.
  The new process resumes those closes at once, and with them their in-process re-gates, so
  the queue would run more gates than slots right after every re-exec. On 8 cores that is the
  overload SPEC-0200 slots exist to prevent.
- **Journal every idle draining tick.** Rejected as noise: 30 lines per 10-minute drain. A
  deduped `admission_blocked` record says the same thing once.
- **Suppress `ready_not_admitted` while `draining`.** Rejected. SPEC-0229 makes a draining
  `ready_not_admitted` a real admission problem, and the slot-age rule fixes the false
  positive without hiding real ones.

## Acceptance criteria

- [ ] `ready_not_admitted_items` returns no row while the held lease's `slot_freed_at` is
      younger than `ready_grace_s`, even when the item has been ready for longer. Once both
      ages exceed the grace it returns the row, with `slot_free_s`. Without a held lease it
      falls back to the ready age.
- [ ] A draining `run()` loop with an in-flight gate (a step that blocks the exec) and a free
      slot admits and opens a ready QUEUED item. No re-exec happens before the gate finishes.
- [ ] While ready items wait on full slots, the journal holds exactly one `admission_blocked`
      record per distinct (ready, holders, deferred) set. Holders whose close or gate the
      draining tick skipped have `deferred: true`.
- [ ] `wt queue runner-log` renders a draining harvest record as `tick N (drain)` and an
      `admission_blocked` record as a single line naming the holders.
- [ ] The `wt-conduct` skill's `ready_not_admitted` row names `slot_free_s` and
      `admission_blocked`; `tests/test_skills.py` still passes.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_watchdog_gaps.py::test_ready_not_admitted_waits_for_slot_age`. A held
    lease with `slot_freed_at` = now − 5 s and a QUEUED item that has been ready for 1000 s
    gives `[]`. With `slot_freed_at` = now − 120 s it gives one row with `slot_free_s == 120`.
  - `tests/test_agentic_watchdog_gaps.py::test_ready_not_admitted_without_lease_uses_ready_age`.
    With no lease, the row fires on the ready age alone.
  - `tests/test_agentic_exit_paths.py::test_draining_tick_admits_while_a_gate_blocks_exec`.
    Use the existing reexec/world fixtures. Set slots 2, a campaign in GATING whose gate step
    blocks on an `Event`, and a ready QUEUED item. Trigger `code_changed`, then assert that the
    QUEUED item reaches OPENING/IMPLEMENTING while the gate is still blocked, and that no
    `stop` with `reason: code_changed` was journaled before the event is released.
  - `tests/test_agentic_exit_paths.py::test_admission_blocked_journaled_once_per_change`.
    Slots 1 are held by a CLOSING campaign whose close is deferred by draining, with a ready
    item waiting. Run several ticks and assert exactly one `admission_blocked` record, with
    `holders[0].deferred is True`. Then park the holder, re-queue another holder, and assert a
    second record.
  - `tests/test_agentic_logs.py::test_runner_log_renders_drain_tick_and_admission_blocked`. A
    `{"kind": "tick", "tick": 90, "drain": true}` record renders `tick 90 (drain)`, and an
    `admission_blocked` record renders the one-line form.
- **Sabotage check:** revert the `min(age, freed)` line in `ready_not_admitted_items`.
  `test_ready_not_admitted_waits_for_slot_age` must fail. Remove the dedupe-key comparison in
  `run()`. `test_admission_blocked_journaled_once_per_change` must fail on the record count.
- **Manual verification:** during the next code-change drain with full slots, `wt queue
  runner-log --tail 10` shows `tick N (drain)` lines and one `admission_blocked` line.
- **Regression guard:** the full `uv run pytest`, in particular
  `test_agentic_drain_signal.py::test_ready_not_admitted_fires_during_drain` (adjust its
  fixture to set an old `slot_freed_at`; the behaviour it pins is unchanged) and
  `test_agentic_exit_paths.py::test_draining_ticks_admit_exec_promptly_and_new_process_adopts`.

## Rollout / migration

None. The new journal kind is additive. Older runner-log readers render unknown kinds through
the generic path.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
