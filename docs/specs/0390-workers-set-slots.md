---
id: SPEC-0390
title: nw workers set --slots - change a worker's gate slots in place
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
tags: [workers, remote-gates]
---

## Context

IDEA-812. `nw workers set` (SPEC-0344) changes `--kinds` and `--persona-slots` but not the gate
`slots`; changing them took `rm` + `add`, which drops `draining`, `update_pending`, cooldown and
re-checks the worker. A registered worker's gate capacity is the registry entry's `slots`
(1..16). The coordinator takes a slot by `flock` on `gate-slots/w-<W>/slot-<n>.lock` for `n` in
`range(slots)` (`gate_slots._try_take`), reading the registry on each attempt; a held slot is a
flock owned by the holding process.

## Goals / Non-goals

**Goals**: `nw workers set NAME --slots N` (1 <= N <= 16) edits only `slots`, safely both ways.
**Non-goals**: touching slot lock files, killing holders, changing `add`/`bootstrap`.

## Decision

- `--slots N` on `set` (click `IntRange(1, 16)`), combinable with the other options; "nothing
  to set" now lists it too.
- Only `slots` of that record changes (`enabled`, `draining`, `update_pending`, cooldown,
  kinds are untouched). No lock file is created, removed or signalled.
- **Raising** takes effect on the next acquire attempt (the tick), which scans `range(N)`.
- **Lowering** never kills or orphans a holder: a job holding `slot-k.lock` with `k >= N`
  keeps its flock until it finishes; no new grant is made on `k >= N`, so those slots drain
  naturally. `nw workers list` / occupancy still probe every `slot-*.lock`, so such holds stay
  visible (`held` may exceed `slots` until they drain).
- Confirmation: `NAME: kinds ..., gate slots N (was M), persona slots P`.

## Design

`cli_workers.py` `workers_set`: new option, `fn` records `old`, sets `r["slots"]`; help text of
the group and `set`; `docs/product/reference/index.md`.

## Acceptance criteria

- [x] `set NAME --slots N` updates the registry slots and prints the new count (and the old).
- [x] Out-of-range N (0, 17) is refused and nothing changes.
- [x] `draining`, `update_pending`, `enabled`, `cooldown_until` are unchanged after a set.
- [x] After lowering below a held slot's index the hold persists, is still reported by
      occupancy, and no new acquire gets a slot `>= N`; raising allows the new slots.

## Test plan

- **Automated:** `tests/test_workers_set_slots.py` (temp data dir, seeded registry).
- **Regression:** `tests/test_workers_registry.py`, `tests/test_workers_drain.py`.
- **Manual:** none (never against the real box).

## Rollout / migration

None; no registry schema change.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions (affected files above).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.
