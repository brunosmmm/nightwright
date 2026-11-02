---
id: SPEC-0343
title: "Worker drain"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, ops]
depends_on: [SPEC-0341]
---

## Context

> **Amendment 2026-11-01 (scope narrowed to drain only).** Bootstrap moved to
> [SPEC-0354](./0354-nw-workers-bootstrap.md) because it needs only SPEC-0337, not the job
> records of SPEC-0341, and the human wants it ahead of the job chain. We edit this spec in
> place rather than supersede it: it is `accepted` with no code or tests shipped, its id,
> parent, dependency and drain design are unchanged, and the epic already uses dated
> amendments for the same kind of re-split. Only the bootstrap content was removed (it lives
> on, expanded, in SPEC-0354); a supersede would orphan the drain design under a new id for
> no gain.

Taking a worker out of service means disabling it
and hoping no job is running. With jobs that outlive the connection
([SPEC-0340](./0340-worker-async-job-protocol.md),
[SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md)), the second needs a
graceful path.

This is the fourth part of the split of
[SPEC-0332](./0332-worker-owned-async-jobs.md). It is independent of the retry work
([SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md)) and can land in
parallel with it.

## Goals / Non-goals

**Goals**
- `nw workers drain` takes a worker out of service without losing running jobs.

**Non-goals**
- Persona prerequisites (`claude` login): SPEC-0344's `check --auth`.
- Automatic worker upgrades.

## Decision

Drain marks the worker `draining: true` in the registry. Placement skips a draining worker:
`workers.gate_pools` leaves it out, a gate already waiting skips it on every take attempt,
and the remote run checks the record once more after the slot is taken. Running and adopted
jobs on it finish. On each tick the queue disables the worker once it has no non-final
remote job record and no held slot.

## Design

**Registry field.** Worker records gain an optional boolean `draining`.
`workers.normalize` fills it in as `draining: rec.get("draining") is True`, so a
`workers.json` without the field loads with `draining: false`. `nw workers add` does not
write it. The registry `schema` value is unchanged.

**`nw workers drain NAME`** (new, `cli_workers.py`):
- Unknown name: prints `no worker named NAME` to stderr and exits 2. Nothing is written.
- Worker already `enabled: false`: no-op. Prints `NAME: already disabled; nothing to drain`
  and exits 0.
- Worker already `draining`: no-op. Prints `NAME: already draining (k jobs)` and exits 0.
- Otherwise it sets `draining: true` in one `workers.update` and prints
  `NAME: draining (k jobs)`.
- `--help` says: "The worker stays draining until a queue tick sees it idle and disables it.
  If no runner is ticking, it stays draining until one runs."

**`k`** is the number of non-final `remote_jobs` records (SPEC-0341,
`remote_jobs.live(cfg)`) whose `worker == NAME`. Held slots are not counted in `k`, but they
still block completion (see the tick).

**`on` / `off`.** `nw workers on NAME` and `nw workers off NAME` both clear `draining`
(the key is removed) in the same `workers.update` as their `enabled` write. `on` cancels a
drain. `off` disables the worker at once, which is the existing immediate path.

**Placement.** `workers.placeable(rec)` (new) is true when `rec["enabled"]`, not
`rec["draining"]`, and `"gate" in rec["kinds"]`.
1. `workers.gate_pools(cfg)` keeps only workers that are `placeable` and out of cooldown, so
   a gate that starts waiting after the drain never lists the worker.
2. A waiter that started before the drain still has the worker in its pool list.
   `gate_slots.acquire` gains an optional `skip(pool) -> bool` predicate, which `_try_take`
   evaluates before trying any slot of a non-local pool. `gates.py` passes
   `skip=lambda p: not WK.placeable_by_name(cfg, p)`. That helper reloads the registry and
   returns false for an unknown name. A waiter therefore falls through to the next pool,
   which is the local pool at the end of the list.
3. SPEC-0338's pre-dispatch re-check in `remote_gates.run` reloads the record after the
   slot is taken. It now treats a worker that is not `placeable` (draining or
   `enabled: false`) like an unregistered one: it raises `RemoteFailure("registry",
   "worker NAME is draining or disabled", cooldown=False)` before anything is sent. The
   slot is released and the gate runs through the SPEC-0338 local fallback. This closes
   the gap between a waiter's last `skip` check and its take. It also changes `off` for a
   waiter that lands on a just-disabled worker: before, the job was dispatched; now it
   falls back locally, which matches what `off` means.
4. `pools_for(..., worker=NAME)` (`nw queue gates --worker`) raises its existing
   `unknown or disabled` ValueError for a draining worker too, worded `is unknown,
   disabled or draining`.
5. Adoption and re-attach (SPEC-0341: `remote_gates.adopt`, the re-attach loop, the cancel
   path) ignore `draining`. A job already on the worker keeps running there, and its
   coordinator re-attaches to it after a restart until the job reaches a final state.

**Drain completion (queue tick).** Each `queue._tick` calls `workers.finish_drains(cfg)`.
That function runs one `workers.update`. For every record with `draining: true` it checks
two things:
- no non-final `remote_jobs` record has `worker == NAME`;
- no slot in `gate-slots/w-<NAME>/` is held (`gate_slots.held(cfg, NAME)`, a new public
  wrapper over `_probe_held(pool_dir(cfg, NAME))`; a missing dir means none).

When both hold, it sets `enabled: false` and removes `draining` in that same update, then
emits the queue event `worker_drained {worker: NAME}`. A gate that holds a slot but has not
yet written its job record (between `acquire` and `submit`) keeps the worker draining. If
that gate then reloads the record in step 3 it sees the worker is still draining and falls
back locally, so the slot frees on the next tick. Because the checks run inside the
registry lock, a concurrent `on` / `off` / `drain` cannot interleave with the write. An
unreadable registry or `remote-jobs` dir skips the drain pass for that tick and is logged.
It never disables anything.

**`nw workers list`.** The `enabled` column shows `draining (k jobs)` for a draining worker,
e.g. `draining (2 jobs)`, `draining (0 jobs)`. `--json` rows gain `draining` (bool) and
`draining_jobs` (`k`, or `null` when not draining).

**Bootstrap findings (moved to SPEC-0354).** Review 0de8b28bfbaa raised several findings
against bootstrap text that this spec no longer contains. Each is resolved in
[SPEC-0354](./0354-nw-workers-bootstrap.md), which has shipped:
- *Dedicated key.* The worker key is `agentic.workers.key`, default
  `~/.ssh/nw_worker_ed25519` (not `~/.ssh/id_ed25519`). A missing key stops bootstrap and
  prints the `ssh-keygen -t ed25519 -N ''` command. `--install-key` refuses when the key's
  blob is already authorized by any other line. A record's `identity` makes
  `workers.normalize` append `-i <identity> -o IdentitiesOnly=yes` to the default
  `ssh_argv`, so every coordinator-to-worker connection (`check`, gates) offers only that
  key.
- *Line format.* The printed line is exactly
  `command="<bin>/nw worker ssh-command",restrict <pubkey>`. `<bin>` is the absolute path
  from `uv tool dir --bin` on the worker. A non-absolute `<bin>`, or any other option set,
  is refused. Its tests assert the printed line.
- *Ordering.* SPEC-0354 runs install, then prints the line, then (with `--install-key`)
  appends it over the unrestricted bootstrap session. A duplicate is detected by key blob,
  and `~/.ssh` is created 0700 with `authorized_keys` 0600. Then it runs `add` with check;
  without `--install-key` it runs `add --no-check` and prints the `nw workers check NAME`
  hint.
- *Prerequisites and paths.* `uv` must be on the worker; otherwise bootstrap stops with
  `uv not found ...`. The wheel is built locally, copied, installed, and removed. Later
  steps call `<bin>/nw` by absolute path. SPEC-0354's Test plan lists the asserted call
  sequence.

## Alternatives considered

- **Disable immediately on drain.** That would orphan running jobs.
- **Let a waiter that started before the drain keep the worker.** This is how `off`
  behaved under SPEC-0337. It lost because a drain could then never finish while gates
  keep queuing, and the worker would be disabled under a newly placed job.
- **Only the post-take re-check, no `skip` predicate.** Rejected because every waiter that
  lands on the draining worker would pay a release and a local re-acquire. The predicate
  lets the waiter fall through to the next pool instead, and the re-check stays as a
  backstop for the race.
- **Count held slots in `k`.** Rejected because `k` is meant to be "jobs on the worker",
  and a held slot without a record is a transient state lasting seconds. It blocks
  completion but is not reported.

## Acceptance criteria

- [ ] `nw workers drain NAME` sets `draining: true`; `workers.gate_pools` omits the worker,
      so a gate started after the drain is not placed on it.
- [ ] A waiter that started before the drain (its pool list includes the worker) is not
      dispatched to the draining worker. It takes the next pool's slot (`skip`), or, if it
      took the worker's slot in the race, `remote_gates.run` raises a non-cooldown
      `RemoteFailure("registry", ...)` and the gate runs locally.
- [ ] A job already running on the draining worker, and a job adopted on it after a
      coordinator restart (SPEC-0341), runs to a final state on that worker.
- [ ] The tick sets `enabled: false` and removes `draining` in one `workers.update` only
      when the worker has no non-final `remote_jobs` record and no held slot in
      `gate-slots/w-<NAME>/`; a held slot without a record keeps it draining.
- [ ] `nw workers list` shows `draining (k jobs)`, where `k` is the number of non-final
      records with `worker == NAME`; `--json` carries `draining` and `draining_jobs`.
- [ ] `drain` on an unknown name exits 2 and writes nothing; on a disabled worker it is a
      no-op with `already disabled` and exits 0; `--help` documents that the worker stays
      draining until a runner ticks.
- [ ] `nw workers on NAME` and `nw workers off NAME` both clear `draining`.
- [ ] A `workers.json` without `draining` loads with `draining: false`, `add` does not
      write the field, and SPEC-0337's registry tests pass unchanged.

## Test plan

- **Automated tests** (`tests/test_workers_drain.py`, offline: temp `cfg`, fake worker
  records, `remote_jobs` records written directly, slot holds taken with
  `gate_slots.acquire` in the test process; waits via `tests/waits.py`):
  - `test_drain_excludes_from_gate_pools`: after `drain`, `gate_pools(cfg)` has no entry
    for the worker and still ends with the local pool.
  - `test_waiter_started_before_drain_skips_worker`: the worker's single slot is held. A
    waiter thread calls `acquire` with pools `[(box, 1), (LOCAL, 1)]` and the local slot is
    also held. The test drains `box`, releases `box`'s slot, then the local one. The waiter
    gets the local pool, never `box`.
  - `test_recheck_after_take_falls_back`: the worker is marked draining between the take
    and `remote_gates.run` (`skip` patched to return false). `run` returns `Outcome(None,
    ...)` with stage `registry`, no cooldown is set, and no ssh call is made (fake
    `subprocess` asserts zero calls).
  - `test_running_and_adopted_job_finish`: a live record `worker == box`, then `drain`.
    The adopt path (fake `poll`/`attach`) drives it to `done`, and the worker stays
    draining until then.
  - `test_held_slot_without_record_blocks_completion`: hold a slot in `w-box/` with no
    record, then call `finish_drains`. The worker stays `draining`, `enabled: true`.
    Release the slot and call `finish_drains` again: `enabled: false`, no `draining` key,
    and a `worker_drained` event.
  - `test_live_record_blocks_completion`: a non-final record keeps the worker draining.
    Marking it `done` lets the next `finish_drains` disable it.
  - `test_list_shows_draining_count`: two live records and one final record →
    `draining (2 jobs)` in the table, `draining_jobs: 2` in `--json`.
  - `test_drain_unknown_exits_2`, `test_drain_disabled_is_noop`,
    `test_drain_twice_is_noop`, `test_drain_help_mentions_runner`.
  - `test_on_off_clear_draining`: `drain` then `on` → enabled, no `draining`. `drain` then
    `off` → disabled, no `draining`.
  - `test_registry_without_draining_loads_false`: a SPEC-0337-era `workers.json` loads
    with `draining: false`, and the file after `add` has no `draining` key.
  - `test_queue_gates_worker_refuses_draining`: `pools_for(..., worker="box")` raises for a
    draining worker.
- **Manual verification:**
  1. With a registered worker `box`, start a gate on it (`nw queue gates CAMPAIGN --worker
     box`). Run `nw workers drain box` and check that `nw workers list` shows
     `draining (1 jobs)`.
  2. Start a second gate without `--worker` and confirm it runs locally.
  3. Once the first gate finishes, let the runner tick. `nw workers list` should show
     `box` as `no` (disabled), and the queue events should include `worker_drained`.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` while
  iterating. `tests/test_workers*.py`, `tests/test_gate_slots*.py` and
  `tests/test_remote_gates*.py` (SPEC-0337/0338/0341) must pass unchanged.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. New command plus an optional `draining` registry field (absent means false); no data
   migration.
2. Behavior change for existing users: a waiter that lands on a worker disabled with `off`
   now falls back locally instead of being dispatched (Design, Placement step 3).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided:
- The queue (not the human) disables a drained worker, on a tick (while splitting
  SPEC-0332).
- A held slot blocks drain completion but is not counted in `k` (review 0de8b28bfbaa).
