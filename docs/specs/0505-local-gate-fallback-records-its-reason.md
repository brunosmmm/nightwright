---
id: SPEC-0505
title: "A gate run that ran locally instead of on a worker records why, in the gate event and in nw campaign show"
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, gates, workers, observability]
depends_on: [SPEC-0338, SPEC-0342, SPEC-0374]
source_idea: IDEA-1080
---

## Context

IDEA-1080: 23 of 210 queue gates since 2026-11-07 ran locally instead of on the box, mostly
FIX-/MERGE- campaigns, and the ledger does not say why. In `gates.py` a gate run reaches the
local path from seven places: `_fallback` (`gates.py:929`) is called after an adopt that neither
ran nor lost (`:819`), after a remote `run` with a transport/tree/protocol failure (`:850`), from
`_retry` when no retry pool is left or attempts are exhausted (`:886`) or after a transport
failure during a retry (`:924`); `_run_all_held` runs directly when `gate_slots.acquire` picked
the local pool (`hold.pool is None`, `:836`, `:898`) — local overflow (SPEC-0374) or
`pools_for` returning None (project not remote-eligible, `remote_gates.ineligible`, or
`--local`). `GateRun.worker` is None in all of them; only `remote_gates._record_ineligible` and
`rejection_notice` leave partial traces. The conductor and the human cannot tell a dead box from
a tree change from an ineligible project. Under Slurm (SPEC-0501 invariant 7) the local fallback
remains, so its reason must be visible.

## Goals / Non-goals

**Goals**
- Every gate run with `worker: null` carries a machine-readable reason.
- `nw campaign show` and `nw campaign show --json` show it.

**Non-goals**
- Changing when nw falls back (the fallback rules of SPEC-0338/0342/0374 are unchanged).

## Decision

Add `GateRun.placement: {"where": "local"|"remote", "reason": <code>, "detail": str,
"worker": str|None, "stage": str|None}` and pass an explicit reason into every local path.

## Design

Reason codes (closed set, `gates.LOCAL_REASONS`):

| code | where it is set |
|---|---|
| `requested_local` | caller passed `local=True` (`nw campaign gate --local`, tests) |
| `ineligible` | `pools_for` returned None; `detail` = `remote_gates.ineligible(...)`'s text |
| `no_placeable_worker` | `gate_slots.acquire` chose the local pool with remote pools offered (overflow, all cooled down/drained/off) |
| `transport` | `RemoteFailure` stage `ssh`/`submit`/`protocol`/`bundle`; `stage` = its stage, `detail` = its error |
| `tree` | `RemoteFailure` stage `tree` (the gates changed files on the worker; IDEA-1082 / SPEC-0506) |
| `rejected` | `RemoteFailure.rejected` (SPEC-0367) |
| `retries_exhausted` | `_retry` past `max_remote_attempts` |
| `no_retry_pool` | `_retry` found no remote pool left |
| `adopt_fell_back` | `adopt` returned neither a run nor a loss |

- `_fallback(...)` and both direct `_run_all_held` sites take `reason: dict`; `remote_gates.run`'s
  `Outcome.failure` already carries stage/error, mapped by `_reason_from_failure(f)`.
- The ledger `gate` event (written where `GateRun` is applied in `queue.py`) gains `placement`;
  remote runs record `{"where": "remote", "worker": <name>}`.
- `nw campaign show` (`cli_campaign.py:301`): the gate line of a local run ends with
  `· local: <code> (<detail clipped to 80>)`; `--json` includes `placement`.
- `gate_jobs` (standalone) already records `transport_error`/`unavailable`; unchanged.

## Alternatives considered

- **A separate `gate_fallback` event.** Splits one fact across two events; readers would have to
  join them. One field on the `gate` event is enough.

## Acceptance criteria

- [x] Each of the nine codes is produced by its path (unit tests force each path).
- [x] The `gate` ledger event of every run has `placement`; a local one has a non-empty `reason`.
- [x] `nw campaign show` shows `local: <code>` for a local gate run; `--json` carries `placement`.
- [x] Remote runs are unchanged except for `placement.where = "remote"`.

## Test plan

- **Automated tests:** `tests/test_gates_placement_reason.py` parametrized over the nine paths
  using the existing fake-worker fixtures of `tests/test_remote_gates*.py` (a refused SSH, a
  `tree` failure, exhausted retries, an ineligible project, overflow with all workers off,
  adopt-without-result); a `cli_campaign` render test for the `local:` suffix.
- **Manual verification (real binary):** on the laptop, with the box worker set `off`
  (`nw workers off box`), gate one throwaway campaign; `nw campaign show` prints
  `local: no_placeable_worker`; turn the worker back `on`.
- **Regression guard:** affected tests of `gates.py`, `queue.py`, `cli_campaign.py`.

## Rollout / migration

Additive. Old events lack `placement`; the renderer prints nothing for them. Supersedes nothing.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual step performed and noted.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Risks

- A path missed by the table records `reason: "unknown"`; a test asserts no gate event in the
  suite's fixtures ends up with `unknown`.

## Open questions

- None.

## As shipped

- `gates.GateRun.placement`, `gates.LOCAL_REASONS` (the nine codes), `gates.local_placement()` and
  `gates.failure_placement()` (maps a `RemoteFailure` to `rejected` / `tree` / `transport`;
  `stage` and `detail` come from it, `worker` is the pool that failed). Every local path passes
  its reason: `_fallback(..., why=)` and the two direct `_run_all_held` sites set
  `GateRun.placement`.
- Deviation: the `placement` field is added in `gates.as_payload` (shared by the `gate` ledger
  event and `last_gate`), not in `queue.py`; a remote run (`worker` set) always gets
  `{"where": "remote", "worker": <name>}`. An interrupted local run with no placement carries
  none (nothing ran).
- `remote_gates.pools_for(..., why_out=)` reports the ineligible reason. With no enabled remote
  worker at all `pools_for` is None and the code is `no_placeable_worker` ("no remote worker is
  enabled for gates"); `ineligible` needs a remote pool that was refused.
- `nw campaign show` (via `timeline._ev_gate`) ends the gate line with
  `· local: <code> (<detail, clipped to 80>)`; the story line in `--json` carries `placement`.
  Old events without `placement` render as before.
- Tests: `tests/test_gates_placement_reason.py` (one test per code, event/story/legacy render);
  `tests/data/remote_gates_golden.json` re-recorded (only the new `placement` key).
- Manual (real binary): a throwaway campaign gated with the worker registered but
  `enabled: false`, in an isolated data/config dir (not the shared registry, so no parallel
  lane's gate was disturbed): `uv run nw campaign show` printed
  `Gate #1 - passed (0s) · local: no_placeable_worker (no remote worker is enabled for gates)`
  and `--json` carried the `placement` object.
