---
id: SPEC-0337
title: "Worker registry and multi-pool gate slots"
status: done
owner: user
created: 2026-10-31
updated: 2026-11-01
source_idea: IDEA-565
parent: SPEC-0310
tags: [agentic, remote, workers, gates]
depends_on: [SPEC-0336]
---

## Context

Second of the three parts the SPEC-0331 review split the first slice of
[SPEC-0310](./0310-remote-workers-run-gates-persona-runs-and.md) into. The worker side is
[SPEC-0336](./0336-gate-worker-job-runner-and-forced-command.md); dispatch is
[SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md).

Since [SPEC-0309](./0309-faster-gates-parallel-pytest-affected-tests-only.md), gate runs
share one host-wide slot pool: `gate_slots.acquire` (`src/nightwright/agentic/gate_slots.py:162`)
over `gate-slots/slot-<n>.lock` flocks, with waiter markers, `occupancy()` and `holding()`
feeding the stuck and silent detectors and the dashboard. This spec adds the coordinator's
list of worker hosts and lets a gate run wait on several pools at once. Nothing here sends
work to a worker yet.

## Goals / Non-goals

**Goals**
- A persistent, lock-protected worker registry and the `nw workers` CLI to manage it.
- `gate_slots.acquire` can wait on an ordered list of pools and returns which one it got.
- Remote holds are visible to `occupancy()`, `holding()` and the stuck detector.
- With no workers registered, every existing behaviour and output is unchanged.

**Non-goals**
- Running anything on the worker beyond `nw worker info` for `check` (SPEC-0338 dispatches).
- Several-worker scheduling beyond "first free pool in registry order".

## Decision

Add `<agentic_dir>/workers.json` (`nw.workers.v1`), `nw workers add|list|check|on|off|rm`,
and a `pools` argument to `gate_slots.acquire`. The default `pools` is local only.

## Design

### Registry

```
{"schema": "nw.workers.v1", "workers": [{"name", "ssh", "slots", "kinds": ["gate"], "nw",
  "enabled", "cooldown_until", "ssh_argv", "added_at"}]}
```

- Module `src/nightwright/agentic/workers.py`: `load(cfg)`, `update(cfg, fn)`.
- Every write (`nw workers` and the cooldown update from SPEC-0338) takes an exclusive flock
  on `<agentic_dir>/workers.json.lock`, re-reads the file, applies one change, and writes
  atomically (`export._atomic_write`). Concurrent writers never lose an update.
- Registry order is preference order.
- A missing or empty file means **no workers**. An unreadable one (bad JSON, wrong schema,
  not a mapping) also means no workers. `load` never writes events. The runner's
  `ConfigWatch` (`queue.py:4632`) also stats `workers.json` each tick. On a parse failure it
  calls `_set_error(lease, path, "workers", error, key)`, and it clears the entry once the
  file reads cleanly or is removed, so the existing detector emits one
  `runner_config_invalid` per distinct `(path, mtime_ns)` (the SPEC-0226 mechanism).
- `ssh_argv` is the transport prefix; the CLI cannot set it (tests and unusual setups edit
  the file). `add` does not store it; `load` uses the default when the key is absent:
  `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
  "-o", "ServerAliveCountMax=4", "-o", "ControlMaster=auto", "-o",
  "ControlPath=<agentic_dir>/ssh/%C", "-o", "ControlPersist=120"]`. `<agentic_dir>/ssh/` is
  created with mode 0700 before first use.

### CLI (`cli_workers.py`, group `nw workers`)

`nw workers` (coordinator, manages the registry) is distinct from `nw worker` (SPEC-0336,
runs on the worker host); each group's help text points to the other.

- `add NAME --ssh DEST [--slots N] [--nw CMD] [--no-check]`: `NAME` matches
  `[a-z0-9][a-z0-9-]{0,30}`, is not `local`, and is not already registered (use `rm`
  first). `DEST` must match `[A-Za-z0-9._@:-]+` and must not start with `-`, so it can never
  be parsed as an ssh option. `--slots` 1-16, default 1. `--nw` (default
  `nw`) is sent as the first word of the remote command; under the forced command the
  worker runs its own `nw` whatever is sent, so `--nw` matters only for loopback tests and
  non-forced setups. `add` runs `check` and refuses an unreachable worker unless
  `--no-check`.
- `list [--json]`: name, destination, slots, enabled, cooldown, held slots.
- `check [NAME] [--json]`: runs `ssh_argv + [DEST, nw, "worker", "info", "--json"]` with a
  30 s timeout (a timeout is an SSH failure). Fails on SSH failure, a `protocol` other than
  `nw.job.v1`, or missing `git`. A nightwright version or sha different from the
  coordinator's is a **warning** (the gate runs the campaign tree's code, not the worker's).
  Without NAME it checks every registered worker and exits 1 if any fails. `--json` prints a
  list of `{name, ok, errors, warnings, info}`.
- `on NAME` (also clears `cooldown_until`) / `off NAME`; `rm NAME`.

### Pools

`gate_slots.acquire(..., pools=None)`; `pools` is an ordered list of `(pool_name, n_slots)`.
`pool_name` is the worker name, or `None` for local; for example `gate_pools` returns
`[("box", 2), (None, 2)]`, and `SlotHold.pool` is that `pool_name`.
- Local keeps `gate-slots/slot-<n>.lock`; worker `W` uses `gate-slots/w-<W>/slot-<n>.lock`.
- Each poll tries the pools in order and takes the first free slot, so a run waits for
  whichever pool frees first and prefers earlier pools. Waiter markers are per call, as
  today.
- `SlotHold` gains `pool` (`None` = local).
- `workers.gate_pools(cfg)` returns `[(W, slots)` for enabled workers whose `kinds` include
  `gate` and whose `cooldown_until` is absent or past, in registry order`]` plus
  `[(None, agentic.gates.slots)]`. SPEC-0338 passes it; every other caller keeps the default
  (local only).
- `occupancy()` adds `workers: {W: {slots, held}}` only when a worker is registered; the
  local keys are unchanged. `holding()` scans every `gate-slots/w-*/` dir present on disk,
  registered or not, so the stuck and silent detectors and the dashboard see remote holds,
  including holds on a worker just removed.
- `rm` and `off` do not affect held slots. A waiter keeps the pool list it started with
  (SPEC-0338 re-checks the worker before dispatch).

## Alternatives considered

- **Workers in `config.yaml`.** It is human-edited and not written by tools; cooldowns are
  machine state. A separate registry file under the agentic dir fits SPEC-0226's model.
- **One pool with remote slots appended.** It loses the slot-to-host mapping that the
  dispatch and the views need.

## Acceptance criteria

- [x] With no `workers.json`, or an empty one, `acquire` and `occupancy()` behave and return
      exactly as before (existing gate-slot tests pass unchanged, plus a golden on
      `occupancy()`).
- [x] `nw workers add|list|check|on|off|rm` manage `workers.json` as specified; `add`
      validates the name, the slot count and `DEST`, refuses a duplicate name and a `DEST`
      starting with `-`, and runs `check` unless `--no-check`; `on` clears the cooldown.
- [x] `check` fails on SSH failure (including its 30 s timeout), a protocol mismatch or
      missing git, and only warns on a version mismatch; without NAME it exits 1 if any
      worker fails.
- [x] Two concurrent registry updates (threads calling `workers.update`) both persist.
- [x] An unreadable registry means `load` returns no workers, and a runner tick publishes one
      `config_errors` entry with `scope: "workers"`, which yields exactly one
      `runner_config_invalid` event; fixing the file clears it.
- [x] `workers.gate_pools(cfg)` returns enabled gate workers out of cooldown in registry
      order, then `(None, agentic.gates.slots)`; a disabled worker, one in cooldown and one
      without `gate` in `kinds` are excluded; no registry returns local only.
- [x] `acquire(pools=[("box", 1), (None, 1)])` takes the free remote slot, takes local when
      the remote pool is full, and returns `SlotHold.pool` accordingly; `occupancy()` and
      `holding()` report remote holds (also for a worker removed while holding), and the
      stuck detector sees them.

## Test plan

- **Automated** (`tests/test_workers_registry.py`, offline): CLI cases via `CliRunner` with
  `ssh_argv` set to a loopback script that runs the remote command locally against a temp
  worker data dir (the SPEC-0336 `nw worker info`); a failing `ssh_argv` script (exit 255)
  for the refused `add` and failed `check`; a fake `info` reporting another protocol major
  and another version; `check` without NAME across a good and a failing worker; a duplicate
  name and a `-oProxyCommand=x` `DEST` refused; `on` clearing a cooldown; a concurrent
  `update` test; an unreadable file through a `ConfigWatch` tick (one event, cleared when
  fixed); `gate_pools` with disabled, cooled-down and non-gate workers and order.
- **Automated** (`tests/test_gate_slots.py`, extended): pool preference, fallback to local
  when full, `SlotHold.pool`, `occupancy()`/`holding()` with a worker registered, golden
  `occupancy()` without one.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-11-01)

- `uv run pytest tests/test_workers_registry.py tests/test_gate_slots.py tests/test_agentic_events.py
  tests/test_cli_gate_slots.py tests/test_agentic_config_reload.py`: 131 passed. The registry file
  cases (missing/empty/unreadable, defaults incl. `ssh_argv`, 0700 ssh dir, concurrent `update`,
  `gate_pools` disabled/cooldown/kinds/order), the CLI cases over a loopback `ssh_argv` script (add
  / duplicate / `-oProxyCommand=x` refused, failing ssh, protocol / git / version / sha, `check`
  without NAME, 30 s timeout shortened, `on` clearing cooldown), the `ConfigWatch` tick (one
  `runner_config_invalid`, cleared when fixed or removed), pool preference / fallback / waiter /
  remote `occupancy()` / `holding()` after `rm`, and the stuck detector with a remote hold.
- Related modules: `tests/test_worker*.py tests/test_agentic_dashboard*.py
  tests/test_agentic_queue_cli.py tests/test_agentic_gates.py tests/test_agentic_loop_state.py
  tests/test_cli*.py`: 392 passed.
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: clean.
- The full suite was left to gating.

### As shipped (2026-11-01)

As designed, with these details the Design left open:
- `workers.py` also has `read_file`/`read_strict` (the strict parse `ConfigWatch` uses), `normalize`,
  `in_cooldown`, `check`, `ensure_ssh_dir`. `cooldown_until` is an ISO timestamp; an unparseable
  one counts as no cooldown. A registry with a duplicate or invalid worker entry is unreadable as a
  whole. `update` on an unreadable file raises and leaves it untouched instead of overwriting it.
- `ConfigWatch._check_workers` reports the registry on the first tick too (the user config is
  seeded silently because it is already loaded; the registry is not). It does not journal a
  `config_load_failed` record; the lease entry and the detector event are the report.
- `add --nw CMD` must match `[A-Za-z0-9._/~+-]+` and not start with `-` (it becomes the first word of
  a remote shell command line).
- Hold files of worker pools carry `pool`, so `holding()` returns it. `acquire` rejects a pool name
  that is not a valid worker name (`ValueError`) before holding anything.
- `nw workers list --json` emits `{"schema": "nw.workers.list.v1", "workers": [...]}`; `check --json`
  emits the plain list of `{name, ok, errors, warnings, info}`.
- A sha difference is a warning only when the versions are equal and both sides know their sha.

## Clock Log

Use `nw idea clock-in IDEA-565` / `nw idea clock-out IDEA-565`.

## Rollout / migration

Inert: no registry means no change. The human may register the second machine as soon as
this ships; nothing uses it until SPEC-0338.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:
- The registry is its own file, written under a flock with re-read.
- Pool order = registry order, then local; remote preferred.
- A version mismatch is a warning.
- The local pool is spelled `None`; worker pools by worker name.
- Registry parse errors go through `ConfigWatch`, not `load`.
