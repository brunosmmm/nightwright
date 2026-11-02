---
id: SPEC-0377
title: "A blocked implementer run with no new commit does not advance to GATING"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, implementer]
source_idea: IDEA-781
---

## Context

IDEA-781: campaign IDEA-714 run 025 ended with an `implementer blocker` major finding and
commit sha=None, yet the campaign went IMPLEMENTING -> GATING (reason `implemented`), burning a
gate on unchanged code. In `_apply` (`src/nightwright/agentic/queue.py`, `kind == "implement"`)
a `blocked` verdict only bumped `counters.blocked_streak` and parked at `BLOCKED_STREAK_MAX`;
below the cap it fell through to `finish("GATING", "implemented")`.

## Goals / Non-goals

**Goals**
- A blocked implementer that produced no commit never reaches GATING.

**Non-goals**
- Changing the streak cap or the park reason/resume target.

## Decision

A `blocked` verdict with no new commit stays in IMPLEMENTING (reason
`implementer_blocked_retry`; the next tick spawns a fresh implementer) until the existing
`blocked_streak` cap parks the campaign as `implementer_blocked` (steward/conductor triage). A
blocked verdict that nevertheless made a commit still proceeds to GATING (there is code to
verify).

## Design

In the `implement` branch of `_apply`: compute `blocked = status == "blocked"` and
`committed = bool(res.commit_sha)`. If blocked and not committed and below the cap, append a
note and `finish("IMPLEMENTING", "implementer_blocked_retry")` (same-state finish writes no
transition event). The cap/park path is unchanged.

## Alternatives considered

- **Park on the first block** -- harsher than the existing two-strike policy; rejected.

## Acceptance criteria

- [x] Blocked verdict, no commit, streak below cap: state stays IMPLEMENTING, reason
      `implementer_blocked_retry`, streak incremented, no GATING.
- [x] Second consecutive block: parks `implementer_blocked` as before.
- [x] Blocked verdict with a commit still goes to GATING.
- [x] Done verdict still goes to GATING and resets the streak.

## Test plan

- **Automated tests:** `tests/test_agentic_queue.py`:
  `test_apply_implementer_blocked_twice_parks` (updated), new
  `test_apply_implementer_blocked_with_commit_gates`.
- **Manual verification:** N/A -- covered by `_apply` unit tests; no live runner touched.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.
