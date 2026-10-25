---
id: SPEC-0194
title: "Runner follows merged code: re-exec between ticks, lease handover, config_invalid parks"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, reliability]
source_idea: IDEA-062
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-062` (Runner/code skew: merging src/wt/agentic or .wt/project.yaml
under a live runner must restart it in the same step).

`wt queue run` is a long-lived process that imports wt from the main checkout (an editable
install). On 2026-10-25, merging SPEC-0191 added `merge.regenerate` to `.wt/project.yaml`
while the runner still ran pre-0191 code. Its strict loader rejected the new key, and both
active campaigns parked as `open_failed`. More generally, merged code took effect only after
someone restarted the runner, so the conductor had to time "merge plus restart" by hand at a
quiet window (a skill rule plus background shells). A bad project config was also
indistinguishable from a real open failure and needed a manual resume.

## Goals / Non-goals

**Goals**
- The runner picks up merged wt code by itself, at a point where nothing is in flight, and
  the queue is never without a lease holder while it does so.
- A project-config load failure parks with its own reason (`config_invalid`) and the
  campaign resumes by itself once the config loads, without exceeding the profile's slots.
- The conductor stops restarting the runner by hand.

**Non-goals**
- Hot-reloading modules in-process.
- Uncommitted edits in the checkout (only commits trigger; see the never-edit-main rule).
- Supervising a runner that died (IDEA-040); a slot-safe resume for other park reasons
  (IDEA-061).

## Decision

Between ticks, `queue.run(..., reexec=True)` compares the wt checkout's HEAD with the sha it
started on. If the new commits touch `src/wt/` or `pyproject.toml`, it journals
`stop {reason: code_changed, from_sha, to_sha, paths}`, hands its lease over, and
`os.execve`s the same command. `wt queue run` enables this unless `--once` or `--no-reexec`.
Project-config `ValueError`s become `ProjectConfigError` and park as `config_invalid`; tick
step 0 resumes such campaigns once the config loads.

## Design

- **Where:** a tick waits for all of its agent steps (ThreadPoolExecutor), so between ticks no
  persona or gate is in flight in this process. The check runs right after the tick,
  heartbeat and `on_tick`, before the sleep.
- **Change detection:** `code_checkout()` is the repo the package is imported from
  (`src/wt/agentic/queue.py` → parents[3], with `.git` and `src/wt` present), else None
  (non-checkout installs: the feature is off). `code_changes(root, since)` returns
  `(HEAD, git diff --name-only since HEAD -- src/wt pyproject.toml)`. Docs-only commits move
  the recorded sha without restarting. The `start` journal record carries `code_sha`.
- **Lease handover:** `Lease.handover()` makes the flock descriptor inheritable, returns
  `{WT_RUNNER_LEASE_FD: fd}`, and detaches it so `release()` is a no-op. `os.execve` keeps the
  pid and the open file description, so the flock stays held. In the new image,
  `lease.acquire` adopts the descriptor: it pops the env var, checks the inode matches
  `queue.lock`, re-flocks (succeeds for the holder), makes it non-inheritable again, and
  rewrites `queue.lease.json` with `restarted_at`. A stale or foreign descriptor is ignored,
  and acquisition proceeds normally. The watchdog never sees `runner_dead`.
- **Exec:** after the `finally` (journal, handover, signal handlers restored), it flushes
  stdio and calls `os.execve(sys.executable, [sys.executable, *sys.argv], env + handover)`.
  `queue._EXEC` is the seam tests replace.
- **config_invalid:** `_Ctx.project` wraps loader `ValueError`s in `ProjectConfigError`.
  `StepResult.config_error` is set by `_do_open` and by the tick's planning guard, and
  `_apply` maps it to `PARKED config_invalid`, with `resume_to` QUEUED for a failed admission
  open or else the state it was in. Tick step 0 walks PARKED `config_invalid` campaigns; when
  the project config loads, it clears `park` and transitions to `resume_to` with reason
  `config_recovered` (row action `recover`). It does this only when `resume_to` is not an
  active state or a slot is free. `config_invalid` joins `PARK_REASONS`.
- **CLI / skill:** `wt queue run --no-reexec`. The conduct skill's "Restart with the merge"
  rule becomes "The runner follows merges by itself": check `wt queue runner-log`, and
  restart by hand only if no `start` follows a `code_changed` stop within a minute. The
  `merged` row says the same.

## Alternatives considered

- **Conductor-timed restart (the old rule)** — depends on a session watching for a quiet
  window; it already failed once.
- **Release the lease, then exec** — a window where `runner_dead` fires and another runner could
  take the queue.
- **Watch file mtimes** — would also fire on in-progress edits to the checkout; commits are
  the unit of "merged".

## Acceptance criteria

- [x] Docs-only commits never restart the loop; a commit touching `src/wt` restarts it after
      the tick, with `stop code_changed` (from/to sha, paths) journaled.
- [x] The lease stays held across the exec (a third process cannot take it) and the new image
      adopts it (`queue.lease.json` pid = the same pid, `restarted_at` set).
- [x] `--once` / `--no-reexec` / library default: no re-exec.
- [x] A bad `.wt/project.yaml` parks `config_invalid` (at open → resume QUEUED; mid-campaign →
      resume to that state); fixing it resumes the campaign by itself (`config_recovered`),
      and a recovering campaign waits when no slot is free.
- [x] The conduct skill no longer tells the conductor to restart after merges.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_reexec.py`:
  - `code_changes` on a scratch repo (docs vs code commits).
  - The loop with commits injected after ticks (`_EXEC` replaced): one exec after the code
    commit, a docs commit before it ignored, `stop` record contents, lease still held, then
    adoption via the env var.
  - A docs-only run ends `max_ticks` and releases.
  - Re-exec off by default.
  - A real `os.execve` subprocess: the first image hands over, the second adopts, and an
    intruder `acquire` fails throughout.
  - `config_invalid` at open → recover → full run to MERGED.
  - Mid-campaign → resume IMPLEMENTING.
  - No free slot → stays parked.
- **Sabotage check:** with adoption disabled and the change check disabled, the re-exec and
  real-exec tests fail.
- **Regression guard:** full `uv run pytest`; `test_skills` pins the new conduct rule.

### Executed (2026-10-25)

- `tests/test_agentic_reexec.py`: 8 passed. Under sabotage, 2 failed (`loop_reexecs…`,
  `real_execve…`); the code was then restored.
- Full `uv run pytest`: 1658 passed, 1 skipped; spec_lint exits 0.
- One fixture fix: the open-time recovery test scripts a conformance reviewer, since
  SPEC-0187 added REVIEWING to the loop.

## Rollout / migration

Built on branch `idea-062-runner-reexec` (worktree `/home/bruno/work/wt-idea-062`), merged
through the conductor. **This merge itself still needs one manual restart:** the live runner
predates the feature. After that, merges are followed automatically. `wt queue runner-log`
shows `stop code_changed` → `start` for each one.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
