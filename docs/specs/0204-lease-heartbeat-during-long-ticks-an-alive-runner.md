---
id: SPEC-0204
title: "Lease heartbeat during long ticks: an alive runner never looks stale"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, lease, reliability]
source_idea: IDEA-072
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-072`. On 2026-10-25 at 15:05, `wt queue list` showed
`lease pid 943099 (heartbeat 163.8s ago) STALE`, and `conduct-status` showed
`heartbeat 171.6s`, while that runner was alive with two campaigns in GATING.

`queue.run` heartbeats only between ticks (`lease.heartbeat()` after `tick()` returns). A
tick that runs gates (pytest ≈115 s, plus the close step's re-gate) or waits on persona runs
outlasts `ttl_s` (120). SPEC-0181 taught the watchdog that in-flight runs and gates are proof
of life. But `lease_status().stale`, and everything that shows it (`wt queue list`, the
SPEC-0185 dashboard, `conduct-status`), still said STALE, inviting a wrong "runner dead" call
or a second runner.

## Goals / Non-goals

**Goals**
- An alive runner's lease is never stale, however long a tick takes.

**Non-goals**
- Detecting a hung tick: a thread that keeps beating while the main thread is stuck masks a
  hang. The watchdog's `run_overdue` / `campaign_silent` (SPEC-0181) cover that.
- Removing the watchdog's in-flight exemptions; they stay as a second guard.

## Decision

While each tick runs, a daemon thread heartbeats the lease every `ttl/4` seconds (at least
1 s); it is stopped and joined when the tick returns.

## Design

- `queue._heartbeat_while(lease, interval_s=None)` is a context manager:
  - it starts a daemon thread `wt-lease-heartbeat` that loops on `Event.wait(interval)` and
    calls `lease.heartbeat()`, ignoring `OSError`;
  - `__exit__` sets the event and joins the thread (5 s timeout).
- In `queue.run`, `report = tick(...)` runs inside `with _heartbeat_while(lease):`. The
  between-tick heartbeat stays.
- Thread safety: `Lease.heartbeat` replaces `queue.lease.json` atomically. The thread is
  joined before the loop can `release()` or `handover()` (SPEC-0194), so no beat races the
  release.

## Alternatives considered

- **Let `lease_status` treat in-flight runs or gates as alive** — duplicates the watchdog's
  logic in every reader, and keeps the on-disk heartbeat wrong.
- **Raise the TTL** — gate and run budgets vary per project; any fixed TTL can be outlasted.

## Acceptance criteria

- [x] With `lease_ttl_s: 2` and a tick that takes 3.5 s, `lease_status` sampled every 0.25 s
      during the tick is always `held` and never `stale`, with heartbeat age < 2 s.
- [x] The heartbeat thread is gone after the tick.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_heartbeat.py` — the slow-tick sampling test above
  (via `Q.run(once=True)` with a patched `tick`); the thread-stops test (no
  `wt-lease-heartbeat` thread remains).
- **Sabotage check:** with the thread's `heartbeat()` call removed, the slow-tick test fails.
- **Regression guard:** full `uv run pytest`.

### Executed (2026-10-25)

- `tests/test_agentic_heartbeat.py`: 2 passed; under sabotage, 1 failed (the slow-tick test);
  the code was then restored.
- Full `uv run pytest`: 1665 passed, 1 skipped; spec_lint exits 0.

## Rollout / migration

Built on branch `idea-072-heartbeat` (worktree `/home/bruno/work/wt-idea-072`), merged
through the conductor; the runner re-execs on the merge (SPEC-0194).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
