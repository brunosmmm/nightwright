---
id: SPEC-0386
title: "Queue-scheduled worker update: `nw workers update NAME --when-idle`"
status: in-progress
owner: user
created: 2026-11-02
updated: 2026-11-02
kind: feature
tags: [agentic, remote, workers, ops]
depends_on: [SPEC-0343, SPEC-0385]
---

## Context

[SPEC-0385](./0385-nw-workers-update-coordinator.md) updates a worker without sudo, but refuses
(exit 5, busy) while it has live jobs. The queue keeps placing new gates on the real `box`, so a
retry loop never finds it idle. [SPEC-0343](./0343-worker-bootstrap-and-drain.md) already has the
mechanism that stops placement and notices idleness (`draining`, `finish_drains` every tick); it
ends in "disable". The queue should be able to end in "update, then resume" instead.

## Goals / Non-goals

**Goals**
- `nw workers update NAME --when-idle`: drain now, update on the first tick that sees the worker idle,
  verify, return it to service. No sudo, no key change, no runner restart, never `--force`.
- Never strand work: any failure or timeout returns the worker to service and tells the conductor.

**Non-goals**
- Killing or moving a job; `--all`; updating a disabled worker (use `nw workers update` directly).

## Decision

Two raw registry fields on the worker record: `draining: true` (unchanged semantics: placement,
`list`, `on`/`off` all keep working) plus `update_pending: {reason: "update", since: <iso>}`. A
draining worker with `update_pending` is skipped by `finish_drains` (so it is not disabled) and
handled by a new tick step `_run_worker_updates` instead. `since` is also the idempotency token
for ending the update (an `on`, `off` or newer schedule in between wins).

## Design

- **CLI.** `--when-idle` is exclusive with `--force` and `--dry-run` (usage error). Disabled worker:
  message, nothing written. Already pending: message. A plainly draining worker is upgraded to
  pending. `nw workers on|off` also clear `update_pending`.
- **Tick** (`queue._run_worker_updates`, after `_finish_worker_drains`; `workers.due_updates`):
  enabled + draining + pending, idle by the SPEC-0343 test (`_drained`: no live job record and no
  held slot; unreadable job records skip the pass). Idle: journal `worker_update_started`, run
  `workers_update.perform` (the SPEC-0385 `send` + the post-update `check`, never `--force`) in a
  daemon thread, because the wheel build and upload take minutes and the tick must not block. One
  thread per worker (`_UPDATING`).
- **Outcomes** (`workers.end_update`, guarded by `since`; drops `draining` + `update_pending`,
  keeps `enabled`, slots, kinds; writes `update_last: {state, at, detail}`):
  - updated / unchanged (build id verified by `check`): back in service; journal `worker_updated`,
    event `worker_updated {worker, state, build_id}` (quiet class `hold`).
  - busy (a race after the idle test): stays draining; retry after 60 s (in-memory back-off).
  - failed (install/verify, ssh, build, check mismatch) or old worker without the verb: back in
    service, `update_last` carries the error, journal `worker_update_failed`, event
    `worker_update_failed {worker, reason: failed|old, error}` (quiet class `wake`). No retry: the
    marker is gone, so nothing repeats until `--when-idle` is run again.
- **Timeout.** `agentic.workers.update_drain_timeout_h` (default 6; non-positive/non-number falls back):
  a pending worker not idle that long after `since` is returned to service, journal
  `worker_update_timeout`, event `worker_update_failed {reason: timeout}`. No job is touched.
- **Auto update** (small, included). `agentic.workers.auto_update` (default false, only literal
  `true`): `nw workers check` schedules `--when-idle` for each enabled worker whose check shows
  the `worker stale:` warning. The queue itself does not probe workers, so it is check-driven.
- **Visibility.** `nw workers list`: `updating (draining, N jobs)`; `--json` rows gain `updating`
  and `update_last`. `queue watch` workers line shows `updating` instead of `draining`.
- Events are `nw.event.v1`, validated via `EVENT_TYPES`; `worker_updated` is added to the
  conductor skill's held list.

## Alternatives considered

- **`draining` as a dict** -- every placement/list check tests `draining is True`; a sibling field
  keeps all of that untouched.
- **Run the update on the tick thread** -- blocks every campaign for minutes.
- **Retry failed updates automatically** -- a deterministic install failure would loop; the
  conductor decides.

## Acceptance criteria

- [x] `--when-idle` marks `draining` + `update_pending`; the worker is not placeable and
      `finish_drains` does not disable it; `--force`/`--dry-run` are refused with it.
- [x] An idle pending worker is updated by a tick (never `--force`), verified, and returned to
      service enabled with the same slots/kinds; `worker_updated` journal + event.
- [x] Not idle: nothing is sent. Busy race: stays draining, no tight retry.
- [x] Failed or old-worker update: back in service, error in `update_last`, journal and
      `worker_update_failed` event, no retry loop.
- [x] Past `update_drain_timeout_h` the drain is cleared with a `worker_update_failed` timeout
      event; jobs are not touched.
- [x] `auto_update: true` makes `nw workers check` schedule a stale worker.
- [x] `workers list` / `--json` / `queue watch` show updating; docs and help updated.
- [ ] Real worker: `nw workers update box --when-idle` was run and the queue updated `box`
      (check shows the new build, box back in service).

## Test plan

- **Automated:** `tests/test_workers_update_schedule.py` (marker + finish_drains skip + list,
  usage errors, `on` cancel, updated end-to-end through the SPEC-0385 stub ssh with a tmp
  registry, not-idle, busy back-off, failed, old worker, timeout, config/event/quiet rules,
  auto_update). Regression: `test_workers_update_cmd.py`, `test_workers_drain.py`,
  `test_agentic_quiet.py`, `test_skills.py`, `test_workers_registry.py`, `test_tui_queue_model.py`.
- **Manual (outstanding):** with a ticking runner, `nw workers update box --when-idle`, watch
  `nw workers list` go `updating (draining, N jobs)` then plain, then `nw workers check box`.

## As shipped

- Shipped as designed in `workers.py`, `workers_update.py` (`perform`), `queue.py`
  (`_run_worker_updates`), `cli_workers.py`, `cli_queue.py`, `events.py`, `quiet.py`.
  `nw workers update` (non-scheduled) still has its own printing; it shares `send` with `perform`.
- Verified with fakes only (stub ssh, tmp_path registry); 322 passed over the touched files.
- NOT verified on the real `box`: the steward does that after merge. This spec stays `in-progress`.

## Definition of done

- [ ] Acceptance criteria all met (the real-box update is outstanding).
- [x] Test plan automated part executed.
- [x] No regressions in the touched files; the queue gate runs the rest.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
