---
id: SPEC-0507
title: "An exclusive timeout re-gate cannot starve: close re-gates yield to it, its wait is visible and bounded"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, queue, gates, bug]
depends_on: [SPEC-0311]
source_idea: IDEA-1083
---

## Context

SPEC-0311: after a timeout-only gate run, `decide_gate_timeout` (`queue.py:463`) re-gates and
the queue sets `state["gate_exclusive"]` (`queue.py:3548`); the note says "re-gating alone
(timeout_regates 1/2)". `select_gate_starts` (`queue.py:506`) lets an exclusive gate start only
when `in_flight == 0`, and holds back other **GATING** starts while it is pending.
`_gates_in_flight` (`queue.py:5251`) counts gate steps **and close steps whose project
re-gates**, but close steps are never held back by `select_gate_starts`.

IDEA-1083 evidence (campaign SPEC-0476-20261109-ca46, read from its ledger and `runner.jsonl`):
15:44:29 timeout note; 15:44:30 `gate_timeout_deferred` journaled (other gate steps in flight);
no gate started until the conductor escalated at 16:15 and parked at 16:17; after the resume
(which re-sets the exclusive marker) another `gate_timeout_deferred` at 16:17:35; meanwhile other
campaigns' gates and closes kept completing (e.g. IDEA-1039 gates_green 16:24). The campaign sat
in GATING silent for 33 minutes. Most likely cause: a steady stream of close re-gates (not held
back) keeps `in_flight > 0`; the journal-only deferral (runner memory, no ledger write) makes the
wait invisible to the conductor's `campaign_silent` check.

## Goals / Non-goals

**Goals**
- A pending exclusive re-gate starts within a bounded time.
- Its wait is visible in the campaign ledger.

**Non-goals**
- Removing SPEC-0311's exclusive mode on the local executor (it still sheds load there).
  Under Slurm admission it is unnecessary; SPEC-0510 skips setting the marker when the gate
  pools are all Slurm workers.

## Decision

1. Close steps that re-gate are held back while an exclusive gate is pending (symmetry with
   GATING starts).
2. A pending exclusive gate waits at most `agentic.gates.exclusive_wait_max_s` (default 1200 s,
   one `pytest-affected` p90 at low load is ~950 s); past it, the marker is cleared, a note
   "exclusive re-gate waited 1200 s for N gate steps; starting non-exclusively" is appended and
   the gate starts as a normal one.
3. The first deferral of each marker appends a ledger `note` (not only the journal) naming the
   in-flight campaigns, so `campaign_silent` sees a reason.

## Design

- `select_gate_starts(candidates, in_flight, exclusive_in_flight, now, wait_max_s)` stays pure;
  it also returns `expired` ids (pending exclusive markers whose `entered_at` is older than
  `wait_max_s`) which the caller converts to normal starts after clearing the marker.
- Close admission: the place where a CLOSING campaign's close step is started consults
  `_exclusive_pending(busy)` and defers the close when the project re-gates
  (`_close_regates`), journaling `close_deferred_for_exclusive` once per campaign/marker.
- `_note_gate_deferred` (`queue.py:5280`) additionally appends a campaign `note` once per marker
  with the ids of the in-flight gate/close steps.
- First task of the implementation: reproduce the ca46 state in a unit test (one exclusive
  GATING candidate, a close re-gate started every tick); if it does not reproduce the stall,
  record the actual root cause in As shipped before changing behaviour.

## Alternatives considered

- **Drop exclusive mode now.** It is the only load shedding the local executor has; Slurm makes
  it redundant, so it goes with SPEC-0520, not before.

## Acceptance criteria

- [ ] With an exclusive gate pending, no new close re-gate starts; started ones finish.
- [ ] A pending exclusive gate older than `exclusive_wait_max_s` starts non-exclusively, with a note.
- [ ] The first deferral writes a campaign `note` listing the in-flight steps.
- [ ] The ca46 reproduction test fails before the fix and passes after.

## Test plan

- **Automated tests:** `tests/test_queue_exclusive_regate.py`: the ca46 reproduction; pure
  `select_gate_starts` table tests incl. expiry; close deferral; the ledger note appears once.
- **Manual verification:** replay check on the live queue: after deploy, the next
  `gate_timeout_regate` in `nw queue events` is followed by a `gate` start within 20 min, or a
  "starting non-exclusively" note.
- **Regression guard:** affected tests of `queue.py` (SPEC-0311 tests stay green).

## Rollout / migration

Deploy with the runner restart. Supersedes nothing (amends SPEC-0311's starvation rule).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- Holding back closes delays merges by up to `exclusive_wait_max_s`; bounded by design.

## Open questions

- None.
