---
id: SPEC-0262
title: "Runner lifecycle: no orphaned runs, no silent drops, no stale code"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-345
parent: SPEC-0169
tags: [agentic, runner, review-follow-up]
---

## Context

This spec comes from a triage of the accumulated `wtfug:` review follow-ups: minor findings
that reviewers raised on merged specs SPEC-0202..0259. Each finding kept here was re-checked
against `main` on 2026-10-27 and is still true. All of them concern the runner process's own
lifecycle: start, stop, crash and the code-change re-exec.

Kept findings:

- **IDEA-310** `wtfu:6e5c7b96eccc` (SPEC-0225): *A wrapper failure after Popen writes
  exit.json while `claude` keeps running.* An orphaned agent goes on editing the worktree
  and spending money while wt plans the next step.
- **IDEA-345** `wtfu:3e6c2c1d4784` and `wtfu:f3dbc53f1057` (SPEC-0229). The same finding
  appears again among the unlisted findings of campaign SPEC-0229-20261025-4997. Titles:
  *At stop bound expiry, in-process tasks are silently dropped* and *Bound-expiry clear()
  drops in-process flights and late finishers without a record.*
- **IDEA-320** `wtfu:8548af1c18f8` (SPEC-0228): *New lease writes and `_SlotWatch` run
  before the try/finally that releases the lease.*
- **IDEA-322** `wtfu:c09617c8731e` (SPEC-0231): *A git diff that keeps failing silently
  disables the watch forever.* The code check found the runner side is worse.
  `codewatch.code_changes` returns `_changed_paths(...) or []`, so a failed diff reads as
  "nothing changed". The runner then moves `code_sha` past the merge
  (`code_sha = head or code_sha`) and keeps running stale code.

The other findings in these ideas were dropped. Some were already fixed, for example the
signal-during-draining `restart_pending`, which now fires only on `max_ticks`/`idle`. The
rest were test-only or cosmetic.

## Goals / Non-goals

**Goals**
- A persona run's `claude` child never outlives a wrapper that has written `exit.json`.
- Every flight still unfinished when the stop bound expires is listed in the `stop` /
  `crash` journal record.
- Once `L.acquire` succeeds, every exit path from `queue.run` releases the runner lease.
- A failed `git diff` during code-change detection never counts as "no code changed".

**Non-goals**
- Changing the 30 s stop bound, the detach/adopt protocol or the drain policy.
- Killing in-process threads (gate, close, resolve, write_spec) at bound expiry. They are
  recorded, not killed. The process exit ends them, as it does today.
- Making followers notice merges that landed while no follower was alive (IDEA-322
  `2af5ab13750d`). That is a design limitation, out of scope.

## Decision

Each finding gets the smallest in-place fix:

- The wrapper kills its child's process group before it writes a failure `exit.json`.
- Bound expiry adds the unfinished hookless flights to `abandoned` with
  `phase: "in_process"`.
- `run()` puts everything after `L.acquire` under a `finally` that releases the lease.
- `code_changes` and `Watcher.poll` report a failed diff as a change with unknown paths.

A spurious re-exec or a spurious follower re-arm costs little. The costly failure is stale
code that keeps running unnoticed.

## Design

1. **Wrapper failure kills the child** (`src/wt/agentic/runproc.py` `main`, outer
   `except BaseException`).
   - Before `finish(-1, error=...)`, check whether `state.get("child")` is set and
     `child.poll() is None`.
   - If so, `_killpg(child.pid, SIGTERM)`, poll for up to `GRACE_S`, then
     `_killpg(child.pid, SIGKILL)`.
   - The error text gets the suffix `; child process group killed`.
   - If `exit.json` already exists, nothing changes.

2. **Bound expiry records in-process flights** (`src/wt/agentic/queue.py`).
   - Add `StepPool.unfinished_in_process()`, run under `self.lock`. It returns
     `{"campaign", "run_id", "phase": "in_process", "step"}` for each task with
     `hk is None and not fut.done()`.
     - `run_id` comes from `fl.runs` at the task's index, else None.
     - `step` is the task's `Step.kind`.
   - In `_drain`'s `bound_at` branch, extend `report.abandoned` with this list before
     `pool.flights.clear()`.
   - `_crash_sweep` extends the `abandoned` list it returns the same way.
   - The `stop` / `crash` journal records already carry `abandoned` (a list of dicts), so
     the schema does not change.

3. **Lease released on every path** (`queue.run`).
   - These steps run after `L.acquire` and before the main `try`:
     - `G.reset()`
     - `lease.update_info(poll_interval_s=..., slot_freed_at=...)`
     - signal installation
     - `ConfigWatch(...)`
     - `_SlotWatch(...)`
     - the `start` journal record
   - Wrap them in an outer `try/finally` that calls `lease.release()` only when the main
     `try` was never entered. Use an `entered` flag so the lease is never released twice.
   - The existing inner `finally` keeps its role, including the `code_changed` hand-over.

4. **A failed diff is a change** (`src/wt/agentic/codewatch.py`, `queue.run`, `events.py`).
   - `code_changes(root, since, paths)` returns `(head, None)` when `head != since` and
     `_changed_paths` returns None. The return type becomes
     `tuple[str | None, list[str] | None]`.
   - `queue.run` treats `changed is None` like a non-empty change:
     - `restart = {"from_sha": code_sha, "to_sha": head, "paths": [], "diff_failed": True}`
     - enter draining; the `draining` record carries `diff_failed: true`
     - the `watch.user_invalid()` deferral still applies first
     - the docs-only branch (`code_sha = head or code_sha`) runs only when `changed == []`
   - `Watcher.poll`, when `_changed_paths` returns None:
     - moves `self.sha` to the new head
     - returns `{"from_sha", "to_sha", "paths": [], "skills_changed": [],
       "diff_failed": True}`
     - so it never retries the same failing diff forever
   - Add `diff_failed` as an optional field of the `code_changed` type in `events.py`
     (`EVENT_TYPES` / `validate`). The text form (`summary_line` near line 867) appends
     ` diff_failed` when it is set.

## Alternatives considered

- **Retry the diff N times before acting.** This adds state and a threshold, and the runner
  still runs stale code while it retries. Rejected: a re-exec is cheap and safe.
- **Kill in-process threads at bound expiry.** Python threads cannot be killed safely, and
  `_terminate_children` already ends their gate subprocesses. Recording them is enough.

## Acceptance criteria

- [ ] When `runproc.main` raises after `Popen` (for example, the `child.json` write
      fails), the child's process group is gone before `exit.json` is written, and
      `exit.json.error` ends with `child process group killed`.
- [ ] When the stop bound expires with an unfinished hookless flight (gate, close, resolve,
      write_spec or FakeRunner), the `stop` record's `abandoned` lists it with
      `phase: "in_process"` and its `step`. The crash path does the same.
- [ ] When anything between `L.acquire` and the main loop raises, the lease is released and
      a fresh `L.acquire` succeeds at once.
- [ ] A failed `git diff` in `code_changes` makes the runner enter draining with
      `diff_failed: true`, and `code_sha` never moves to the new head without a re-exec.
- [ ] A failed `git diff` in `Watcher.poll` yields exactly one `code_changed` report with
      `diff_failed: True`, and the baseline moves to the new head.
- [ ] `uv run pytest` is green and each sabotage check below fails its test.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_runproc.py::test_wrapper_failure_after_popen_kills_child`:
    - Monkeypatch `runproc.write_json_atomic` so the `child.json` write raises
      `OSError(errno.ENOSPC)`.
    - Run `main` with a `sleep 30` launch and `WT_RUNPROC_GRACE_S=0.5`.
    - Assert that `exit.json` has the suffix.
    - Assert that `os.killpg(pgid, 0)` raises `ProcessLookupError` within 10 s.
  - `tests/test_agentic_exit_paths.py::test_bound_expiry_lists_in_process_flights`:
    - Monkeypatch `_STOP_BOUND_S = 0.3`.
    - Put a pool flight whose hookless task blocks on an `Event`.
    - Set `stop`, then call `_drain(cfg, pool, lease, stop=stop)`.
    - Assert that `report.abandoned` contains `phase == "in_process"` and `step == "gate"`.
    - Add a twin test through `_crash_sweep`.
  - `tests/test_agentic_loop_state.py::test_lease_released_when_startup_raises`:
    - Monkeypatch `Q._SlotWatch.__init__` to raise `OSError`.
    - Assert that `run()` raises.
    - Assert that `L.lease_status(cfg)["held"]` is False and that `L.acquire` succeeds.
  - `tests/test_agentic_reexec.py::test_diff_failure_counts_as_code_change`:
    - Commit under `src/wt` in the watched checkout.
    - Monkeypatch `codewatch._changed_paths` to return None.
    - Assert that a `draining` journal record has `diff_failed: true` and that `_EXEC` is
      called with the new head.
  - `tests/test_agentic_codewatch.py::test_watcher_reports_diff_failure_once`:
    - With `_changed_paths` patched to return None, the first `poll` returns
      `diff_failed: True`.
    - A second `poll` at the same head returns None.
- **Sabotage checks.** Each change must make its test fail; record the outcome in the spec.
  - Remove the `_killpg` calls from the wrapper's `except`.
  - Drop the `unfinished_in_process()` extension in `_drain`.
  - Move `_SlotWatch(...)` back above the outer `try`.
  - Restore `or []` in `code_changes`.
- **Regression guard:** run the full `uv run pytest`. In particular
  `test_agentic_exit_paths.py`, `test_agentic_adopt.py`, `test_agentic_reexec.py` and
  `test_agentic_codewatch.py` must pass unchanged.
- **Manual verification:** none. Every path is driven in-process.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log

