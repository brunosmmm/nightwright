---
id: SPEC-0391
title: A gate job carries the worker's registry slots as the worker host's slot limit
status: in-progress
owner: user
created: 2026-11-02
updated: 2026-11-02
tags: [workers, remote-gates, gate-slots]
---

## Context

IDEA-813 (bug). A registered worker advertises its gate capacity as the registry entry's
`slots` (1..16; `nw workers set NAME --slots N`, SPEC-0390). The coordinator takes one of those
by flock on `gate-slots/w-<W>/slot-<n>.lock` (SPEC-0337) and dispatches a `nw.job.v1` gate job
(SPEC-0336). The worker process then takes a **second**, host-wide slot on its own host:
`gate_slots.acquire(cfg, "remote:<campaign>", ...)` in `worker._run_gates`, whose limit is the
worker host's own `agentic.gates.slots` (default `DEFAULT_SLOTS = 2`). The worker box is
registered with 10 slots but its account's config is not ours to edit, so it runs 2 gate jobs at
once and the rest sit in its local queue (`slot_wait` 67-599 s observed). Editing the box's
config by hand or with sudo is not acceptable.

## Goals / Non-goals

**Goals**
- The registry's `slots` for a worker is the number of gate jobs that worker host runs at once,
  with no config change on the box.
- Old coordinators (no new field) keep today's behavior on a new worker; an old worker ignores
  the new field.

**Non-goals**
- Persona jobs (they take no host gate slot on the worker).
- A persistent host-side record of advertised slots; changing the local pool of a non-worker
  host; the coordinator-side `w-<W>` pool (already uses the registry).

## Decision

- The coordinator's gate-job header carries `payload.host_slots`: the dispatching worker
  record's normalized `slots` (1..16), next to `payload.gate_budget_s` (the same
  "coordinator's value, worker's own as fallback" pattern).
- The worker validates it when present (an int, not a bool, 1..16; anything else is a
  `validate` refusal, like a bad `gate_budget_s`) and uses it as the `n_slots` of its local
  pool for that job's acquire: `pools=[(None, host_slots)]`. Absent: `settings(cfg)["slots"]`
  as today.
- **Several coordinators sharing one worker: the limit is per job, no shared state.** Each job
  scans `slot-0..slot-<host_slots-1>` of the same host lock dir. So at any moment the host runs
  at most the **max** `host_slots` among the jobs present, and a job advertised `k` never runs
  while slots `0..k-1` are all held. With all coordinators reading the same registry value
  (the normal case) that is exactly the registry count; a coordinator advertising less (or an
  old one, falling back to the box's default) only ever uses the low slots and cannot push the
  host above the larger value. Chosen over "latest advertised wins" because it needs no host
  state, cannot race, and stays correct across restarts.
- **Lowering slots** (SPEC-0390 semantics): jobs already holding `slot-k` with `k >= N` keep it
  until they finish; new jobs dispatched after the change carry `N` and are granted only
  `0..N-1`, so the excess drains. A job already waiting keeps the value it was dispatched with
  (the acquire's pool list is fixed for the wait), bounded anyway by the coordinator's own
  `w-<W>` pool, which already reflects the new `N`.
- `gate_slots.acquire` is unchanged: it already takes `pools=[(pool, n)]` and a hold is a
  flock, so a crashed job never leaks a slot regardless of `n`.

## Design

- `remote_gates._header(..., host_slots=None)` adds `payload.host_slots` when given;
  `prepare(worker, ...)` passes `worker.get("slots")` (the registry record is normalized by
  `workers.normalize`, so 1..16). `header_refusal`'s dry header passes none (the worker is not
  known there) — the field is optional.
- `worker.validate_header`: `payload.host_slots`, if not None, must be an int (not bool) in
  `1..gate_slots.MAX_SLOTS`.
- `worker._run_gates`: `n = payload.get("host_slots") or gate_slots.settings(cfg)["slots"]`;
  `gate_slots.acquire(..., pools=[(None, n)])`.
- Async jobs (`worker_job`, built by the same `prepare`) store and replay the same header, so they get the field
  too.

## Alternatives considered

- **Edit the box's `agentic.gates.slots`** — not ours to edit, and two sources of truth.
- **Drop the worker-side slot** — loses the host's protection against several coordinators
  oversubscribing it.
- **Persist the latest advertised value on the host** — extra state and a write race for no
  gain over the per-job max semantics.
- **Top-level header field** — the payload is the gate-specific part and already carries the
  coordinator's `gate_budget_s`; persona payloads are validated separately.

## Acceptance criteria

- [x] A gate header built by `prepare` for a worker with registry `slots: N` has
      `payload.host_slots == N`; the dry `header_refusal` header still validates.
- [x] `validate_header` refuses `payload.host_slots` of 0, 17, `True`, `"4"`, `2.0` and accepts an
      absent one and 1..16.
- [x] A gate job with `payload.host_slots: 2` on a worker whose own config says `slots: 1` runs
      while another holder has slot 0 (no `slot_wait`); without `host_slots` it waits as before.
- [x] The existing worker slot tests (held slot gives `slot_wait`, EOF while waiting) and the
      gate_slots tests pass unchanged.
- [ ] On the real worker box (registry 10) up to 10 gate jobs run at once with no `slot_wait`
      — requires a worker update only the human can approve; not done by the implementer.

## Test plan

- **Automated tests:**
  - `tests/test_worker.py`: new parametrized refusals for bad `payload.host_slots`; new test
    that a job with `host_slots: 2` (own config 1, slot 0 held by another) goes straight to
    `started`, and a job with `host_slots: 1` under the same hold waits (`slot_wait`).
  - `tests/test_remote_gates.py`: `prepare`/`_header` carries the worker's `slots` as
    `payload.host_slots`; `header_refusal` still returns None for a valid config.
- **Regression guard:** `tests/test_gate_slots.py`, the rest of `tests/test_worker.py` and
  `tests/test_remote_gates.py` (`-n 4`).
- **Manual verification:** on the box after the human approves a worker update: dispatch
  several gate jobs and check `nw workers list`/occupancy shows more than 2 held host slots and
  no `slot_wait` frames below 10 concurrent jobs.

## As shipped

- `remote_gates.prepare` passes `worker.get("slots")` to `_header(..., host_slots=)`, which adds
  `payload.host_slots`; `header_refusal` builds without it.
- `worker.validate_header` refuses a present `payload.host_slots` that is not an int (bool and
  float refused) in 1..16; `worker._run_gates` acquires with `pools=[(None, host_slots or
  settings(cfg)["slots"])]`.
- Tests run (`uv run pytest -q -n 4 tests/test_worker.py tests/test_remote_gates.py
  tests/test_gate_slots.py`): 255 passed. New: `test_payload_host_slots_is_the_hosts_slot_limit`,
  five `host_slots` refusals in `test_bad_header_fields_are_refused_before_anything_runs`
  (test_worker.py); `test_registry_slots_raise_the_worker_hosts_own_limit` and a `host_slots`
  header assertion in `test_stat_dirty_tree_runs_remotely` (test_remote_gates.py). Both new
  behavior tests fail with the `_run_gates` change reverted.
- Deflake: `test_registry_slots_raise_the_worker_hosts_own_limit` uses `slot_wait_s: 20` (was 1, which expired under load and fell back to local); `test_payload_host_slots_is_the_hosts_slot_limit` needed no change (its worker-side env is only an event interval, not a timeout).
- Not done: the real-box AC and manual step. They need the worker box updated to this version
  (`nw workers update`), which only the human approves; the spec stays `in-progress` until then.

## Rollout / migration

No schema version bump: the field is optional both ways. The coordinator change works as soon
as the worker box runs this version (`nw workers update`, human-approved).

## Definition of done

- [ ] Acceptance criteria all met (real-box AC pending human-approved update).
- [x] Test plan executed; automated tests pass (manual box step pending).
- [x] No regressions (the three affected files).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.
