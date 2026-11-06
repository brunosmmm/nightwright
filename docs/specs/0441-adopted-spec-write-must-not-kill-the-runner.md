---
id: SPEC-0441
title: An adopted spec write must not kill the runner
status: done
owner: user
created: 2026-11-06
---

## Context

On 2026-11-05 `nw queue run` died with `TypeError: 'NoneType' object is not subscriptable`
at `_apply_spec_write` (`if w["blocked"]`). A new runner adopts every open ledger run
([SPEC-0225](./0225-detached-persona-runs-adopted-across-runner-restarts.md)). `_adopt`
always submitted `_do_persona`, including for a `write_spec` run (`_adopt_class` trusts the
`step.kind` recorded by `run.start`, even though `spec_writer` is deliberately absent from
`ADOPT_REGISTRY`). `_do_persona` never sets `StepResult.write`, so when the adopted spec
writer finished, the normal (unguarded) `_harvest` called `_apply_spec_write` with
`res.write is None` and the `TypeError` killed the whole process.

Detached persona runs are separate processes, so other campaigns kept running with the loop
dead; the replacement runner died again on the same open `write_spec` run.

## Goals / Non-goals

**Goals**
- An adopted `write_spec` run is handled by the same worker as a freshly submitted one.
- A missing spec-write payload parks the campaign instead of raising.

**Non-goals**
- Changing `_harvest`'s unguarded default (only the crash sweep guards).
- Adopting `spec_review` or `resolve` through their own workers (`_apply_spec_review` and
  `_apply_resolve` already tolerate a missing payload; `_do_resolve` restores the branch to
  `pre_head` and must not be used for adoption here).
- Making `spec_writer` detachable.

## Decision

1. On adopt, a `write_spec` step is submitted to `_do_write_spec` (the worker `_submit` uses),
   not `_do_persona`. `runner.run` on an existing run dir attaches or collects; `_do_write_spec`
   then validates, commits or rolls back, and fills `res.write`. Every other kind stays on
   `_do_persona`.
2. `_apply_spec_write` treats a missing `write` (None or not a dict) as a park
   `spec_write_failed`, detail "spec writer returned no write result", `resume_to`
   `SPEC_WRITING`. It never raises. This is the backstop so a missing payload cannot kill the
   process. The run is still recorded (the `_record_run` call stays above the check).

## Design

`src/nightwright/agentic/queue.py`:

- `_adopt`: when `step.kind == "write_spec"` (and the run is not part of a review group), the
  flight task is `pool.submit(_do_write_spec, ctx, snap, step, rid, req, budget)` with `hooks`
  `None` in the task tuple, exactly as in `_submit` (in-process, never detached). Other kinds
  keep `_do_persona(..., hooks, adopted)`.
- `_apply_spec_write`: after `_record_run` and the interrupted early return, and before
  `w["blocked"]`, `if not isinstance(w, dict): return finish("PARKED", "spec_write_failed",
  park={"reason": "spec_write_failed", "detail": "spec writer returned no write result",
  "resume_to": "SPEC_WRITING"})`.

## Alternatives considered

- **Guard only in `_apply_spec_write`** — stops the crash but leaves an adopted writer's work
  uncommitted and unvalidated, and parks a campaign whose writer actually succeeded.
- **Make `_harvest` guarded by default** — hides every apply bug from the loop and changes
  behavior for all steps; out of scope.
- **Route adoption through `_do_resolve`/`_do_spec_review`** — wrong worker; `_do_resolve`
  resets the branch.

## Acceptance criteria

- [x] A tick that adopts a finished open `write_spec` run returns without raising.
- [x] That run gets a `run.end`, and the campaign follows the normal spec-write outcome
  (problems retry, or park `spec_write_failed`), not a dead runner.
- [x] `_apply_spec_write` with `write is None` parks `spec_write_failed` and does not raise.
- [x] Existing adopt tests for implementer runs still pass.

## Test plan

- **Automated tests:** `tests/test_agentic_spec_writer.py`: an open `write_spec` run is left by
  a runner that never finished it (blocked, pool abandoned); a new pool with a detachable
  runner adopts it on its first tick. Asserts no exception, a `run.end` for the run, and the
  campaign in the normal spec-write outcome (spec committed, in `SPEC_REVIEW`). A second
  assertion drives `_apply_spec_write` with `res.write = None` directly: parks
  `spec_write_failed` with the stated detail and `resume_to`, run still recorded, no raise.
  Both fail on the old code with the `TypeError`. As shipped: the second test wraps
  `_do_write_spec` to null `res.write` and ticks normally (no direct call). The test's adopting
  runner is a detachable `FakeRunner` that is given the worktree as `cwd`, because an adopted
  request is rebuilt from `request.json`, which `FakeRunner` never writes.
- **Manual verification:** N/A — covered by the automated reproduction.
- **Regression guard:** `tests/test_agentic_adopt.py` and `tests/test_agentic_spec_writer.py`
  pass; `python3 tools/spec_lint.py` exits 0.

## As shipped

`_adopt` submits `_do_write_spec` (task hooks `None`) for `step.kind == "write_spec"`;
`_apply_spec_write` parks `spec_write_failed` on a non-dict `write`. Verified:
`tests/test_agentic_spec_writer.py` + `tests/test_agentic_adopt.py` (130 passed) and the
adopt/spec-review/resolve/reexec/remote-persona/runproc/crash/detach/split/amend agentic test
files (346 passed). The full suite was not run (sentinel/queue gate).

## Rollout / migration

None. A runner restart picks up the fix; an already open `write_spec` run is adopted and
finished by the normal writer path.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
