---
id: SPEC-0374
title: "Gates never overflow to the local pool while a worker is placeable"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, workers]
depends_on: [SPEC-0337, SPEC-0338, SPEC-0342, SPEC-0343]
---

## Context

`workers.gate_pools` returned the placeable, non-cooldown gate workers followed by the local
pool, and `gate_slots._try_take` takes the first free slot in that order. When every worker
slot was momentarily busy, a gate overflowed to a free local slot, so gates ran on the
coordinator although the remote worker was enabled and available. The human's rule: never
gate locally while a remote worker is available.

## Goals / Non-goals

**Goals**
- While at least one gate worker is registered, enabled, not draining and out of cooldown,
  a gate waits for a worker slot and never takes a local slot.
- A waiter re-evaluates placement while it waits: a worker that goes into cooldown, is
  disabled or drained leaves the list; with none left the local pool comes in.
- An opt-out, `agentic.gates.local_overflow: true`, restores the old local-last overflow.

**Non-goals**
- Changing the local-only cases (`remote_gates: false`, a gate `remote: false`, untracked
  files, a header refusal, `--local`), the transport-failure fallback (SPEC-0338) or the
  sentinel's local gate run.
- FIFO fairness among waiters (slot polling stays as is).

## Decision

`gate_pools` returns the worker pools alone when any is placeable (local only when none is,
local appended last only with `local_overflow`). `gate_slots.acquire` gains a `repool()`
callback evaluated before each retry while waiting; `gates.run_all` (loop, close re-gate,
`nw queue gates`) and the SPEC-0342 `_retry` pass `WK.gate_pools` (minus the excluded
workers for a retry). The pinned `--worker` and adoption waits keep a fixed pool.

## Design

- `workers.local_overflow(cfg)`: true only for a literal `true`.
- `workers.gate_pools(cfg, now=None, *, exclude=())`: the rule above; `exclude` drops workers.
- `gate_slots.acquire(..., repool=None)`: `_repooled` replaces the list with `repool()`'s
  when it is non-empty and every name is valid; an exception keeps the current list.
- `retry_pools` is unchanged in code: it inherits the box-only list, so a lost job's retry
  waits for another placeable worker and runs locally only when none is left.
- Dashboards (`nw queue gate-slots`, the watch workers strip, SPEC-0373 progress) read
  occupancy and waiter markers, not the pool list; a waiting campaign shows as waiting.

## Alternatives considered

- **Blocking flock on the worker slot** - cannot re-evaluate placement, strands waiters.

## Acceptance criteria

- [x] Worker busy: the gate waits (waiter marker published) and takes no local slot.
- [x] Worker free: the gate takes the worker slot.
- [x] No placeable worker: the local pool.
- [x] Worker turning unplaceable (cooldown / off) while a gate waits: the gate takes local.
- [x] `agentic.gates.local_overflow: true`: local is appended last and a busy worker overflows.

## Test plan

- **Automated tests:** `tests/test_no_local_gate_overflow.py` covers the five criteria
  against real flock slots and a real registry; existing `gate_pools`/retry expectations in
  `tests/test_workers_registry.py`, `tests/test_workers_drain.py`,
  `tests/test_worker_job_retry.py` updated to the box-only list (the old local-at-once retry
  test runs with the opt-out).
- **Regression guard:** `tests/test_gate_slots.py`, `tests/test_remote_gates.py`,
  `tests/test_cli_gate_slots.py`, `tests/test_agentic_watch_idle_cost.py`.

## As shipped

As designed. `gate_pools` is worker-only while any worker is placeable; `acquire(repool=)`
re-reads it each poll in `run_all` and `_retry` (not for `--worker` or adoption, which are
pinned). Existing tests updated: `gate_pools`/`retry_pools` expectations lose the trailing
local pool, the old "busy second worker runs the retry locally at once" test now runs with
`local_overflow: true`, and the drain waiter test counts `gate_pools` re-reads. A lost job's
retry follows the same rule: it waits for another placeable worker and runs locally only
when none is left. Transport-failure fallback (`_fallback`) is unchanged and still local.
Known limits: a gate that started on the local pool because no worker was placeable does not
move to a worker that comes back while it waits; waiters poll (1 s) without FIFO order, so a
long box queue can delay a given gate; a worker that is placeable but whose slots are held by
hung remote runs keeps waiters waiting until those runs time out or the worker is put in
cooldown/off. Tests: `tests/test_no_local_gate_overflow.py` (new), plus
`test_workers_registry`, `test_workers_drain`, `test_worker_job_retry`, `test_gate_slots`,
`test_remote_gates`, `test_cli_gate_slots`, `test_agentic_watch_idle_cost`, `test_test_waits`,
`test_agentic_gates` all pass. Per the coordinator's expedite instruction the broader
affected-tests run was skipped; the full suite was not run.

## Definition of done

- [x] Acceptance criteria met.
- [x] Test plan executed (directly affected tests; full suite left to the close gate).
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.
