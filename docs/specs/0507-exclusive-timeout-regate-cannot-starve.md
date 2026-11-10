---
id: SPEC-0507
title: "An exclusive timeout re-gate cannot starve: close re-gates yield to it, its wait is visible and bounded"
status: in-progress
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

SPEC-0311: after a timeout-only gate run, `decide_gate_timeout` re-gates and the queue sets
`state["gate_exclusive"]`; the note says "re-gating alone (timeout_regates 1/2)".
`select_gate_starts` lets an exclusive gate start only when nothing is in flight, and holds back
other **GATING** starts while it is pending. `_gates_in_flight` counts gate steps **and close
steps whose project re-gates**, but close steps were never held back.

IDEA-1083 evidence (campaign SPEC-0476-20261109-ca46): 15:44:29 timeout note; 15:44:30
`gate_timeout_deferred` journaled; no gate of ca46 until the conductor escalated (16:15) and
parked/resumed it (16:17); ca46's ledger was silent for 33 minutes.

**What the runner journal actually shows** (`runner.jsonl`, every gate/close start between
14:10 and 16:30, read while implementing): no close started in the window, so the suspected
stream of close re-gates did not happen. Instead:

- 14:54:49 IDEA-1039 timed out and became exclusive. Five gates started 14:14–14:46 were in flight;
  it waited for them to drain (no gate started in the meantime, as SPEC-0311 intends).
- 15:44:30 ca46 became exclusive **behind IDEA-1039** (older `entered_at`).
- 15:47:45 the last in-flight gate (SPEC-0490) finished; IDEA-1039 started alone, as designed.
  Its worker job was lost at 16:01:57 and retried; it finished green at 16:24:27.
- 16:24:28 ca46 started alone, the next tick.

So ca46 did not starve: it queued behind another exclusive re-gate that itself waited 53 min for a
drain and then ran 37 min. The fault was that the wait was **invisible** (journal only, naming
nobody), so the conductor and its sub-agent concluded "no gate running" and parked it; and the
wait had **no bound** (a drain plus a lost-and-retried job can take arbitrarily long). The close
hole is real but latent (a close re-gate starting beside a pending exclusive extends the drain),
so it is closed too.

## Goals / Non-goals

**Goals**
- A pending exclusive re-gate starts within a bounded time.
- Its wait, and what it waits for, is visible in the campaign ledger.

**Non-goals**
- Removing SPEC-0311's exclusive mode on the local executor (it still sheds load there).
  Under Slurm admission it is unnecessary; SPEC-0510 skips setting the marker when the gate
  pools are all Slurm workers.

## Decision

1. Close steps that re-gate are held back while an exclusive gate is pending **or running**
   (symmetry with GATING starts, which are held back in both cases).
2. A pending exclusive gate waits at most `agentic.gates.exclusive_wait_max_s` (default
   **2700 s**); past it, the marker is cleared, a note "exclusive re-gate waited N s (max M s)
   for K gate step(s) (ids); starting non-exclusively" is appended and the gate starts as a
   normal one in that tick. It does not expire while an exclusive gate is running: starting
   beside it re-creates the load that timed that one out, and that run is bounded by its own
   gate timeouts.
3. The first deferral of each marker appends a ledger `note` naming the gate steps in flight and
   the older exclusive re-gates it queues behind, so `campaign_silent` and a reader see a reason.

## Design

- Pure: `exclusive_waited_s(state, now)`, `expired_exclusives(candidates, now, wait_max_s)`
  (ids past the bound, `(entered_at, id)` order; an unreadable `entered_at` counts as expired),
  `exclusive_wait_max_s(cfg)` (positive number, else the default). `select_gate_starts` keeps
  its SPEC-0311 signature and table: a separate function instead of a second return value keeps
  its callers and tests untouched.
- `_gate_starts`: when no exclusive gate is in flight, each expired candidate is cleared by
  `_expire_exclusive` (under the item lock, only if the marker on disk is still the same one:
  state write + note + journal `gate_exclusive_expired`) and started with the normal ones that
  `select_gate_starts` allows for the rest.
- `_gate_flights` lists `(campaign id, exclusive)` of the gate steps in flight (close re-gates
  included); `_gates_in_flight` is its count, unchanged in meaning.
- Close admission: `_tick` computes `_exclusive_held` (GATING campaigns with a marker, plus
  running exclusive gates) after `_gate_starts`; a `close` step whose project re-gates
  (`_close_regates`) is skipped while it is non-empty, with `_note_close_deferred` journaling
  `close_deferred_for_exclusive` and appending one note per campaign per head exclusive re-gate.
  Started closes are untouched and finish.
- `_note_gate_deferred(ctx, pool, st, busy)` keeps the SPEC-0311 journal record and, once per
  marker, appends the note "exclusive re-gate waiting: N gate step(s) in flight (ids); queued
  behind exclusive re-gate(s) ids" and stores `gate_exclusive.noted = true`, so a runner
  restart does not repeat it; a new marker (resume) is noted again.
- Default 2700 s rather than the 1200 s first proposed: the drain of gates already running is
  bounded by their `timeout_s` (2400 s for `pytest-affected`); 1200 s would expire the exclusive
  mode in exactly the loaded case it exists for (IDEA-1039 waited 53 min for such a drain).

## Alternatives considered

- **Drop exclusive mode now.** It is the only load shedding the local executor has; Slurm makes
  it redundant, so it goes with SPEC-0520, not before.
- **Bound 1200 s.** Rejected above: shorter than one gate timeout, it turns most loaded drains
  into non-exclusive re-gates that time out again.
- **Expire even beside a running exclusive gate.** Rejected: it loads the run that is trying to
  pass alone; that run is bounded anyway.

## Acceptance criteria

- [x] With an exclusive gate pending, no new close re-gate starts; started ones finish.
- [x] A pending exclusive gate older than `exclusive_wait_max_s` starts non-exclusively, with a note.
- [x] The first deferral writes a campaign `note` listing the in-flight steps.
- [x] The ca46 reproduction test fails before the fix and passes after.

## Test plan

- **Automated tests:** `tests/test_queue_exclusive_regate.py`: the ca46 reproduction (tick
  level: B in flight, older exclusive X, exclusive CA, CLOSING K that re-gates; order B, X, CA,
  then K; one note each); pure tables for `expired_exclusives` and the setting; expiry starts
  non-exclusively with a note, not beside a running exclusive gate, configured bound honoured;
  close deferral note once; gate deferral note once across a runner restart, again for a new
  marker. SPEC-0311's tick test in `tests/test_agentic_queue.py` now expects A's one note.
- **Manual verification:** replay of the ca46 runner journal (above) against the new rules: the
  reproduction test encodes that timeline. After deploy, the conductor confirms the next
  `gate_timeout_regate` is followed by a note naming what it waits for and a `gate` start, or a
  "starting non-exclusively" note, within 45 min.
- **Regression guard:** affected tests of `queue.py` (SPEC-0311 tests stay green).

## Rollout / migration

Deploy with the runner restart. Supersedes nothing (amends SPEC-0311's starvation rule). Markers
written before the deploy have no `noted` flag: they get one note on the first tick.

## Risks

- Holding back closes delays merges while an exclusive re-gate is pending or running; bounded by
  `exclusive_wait_max_s` plus that run's gate timeouts.

## Open questions

- None.

## As shipped

Reproduction first: the journal showed head-of-line waiting behind IDEA-1039's exclusive
re-gate, not close re-gates; Context, Decision and Design above were rewritten to match. The
ca46 reproduction test failed on the pre-fix `queue.py` (`['B', 'K:close'] == ['B']`: a close
re-gate joined the drain) and passes after. Shipped in `queue.py` (admission helpers and the
`_tick` close check) and `schemas.py` (two runner journal kinds). Local: 14 new tests and the
touched `test_agentic_queue.py` / `test_agentic_quiet.py` pass (323).

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
