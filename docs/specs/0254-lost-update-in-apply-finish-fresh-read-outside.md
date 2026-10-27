---
id: SPEC-0254
title: "Take item_lock around the fresh read and write in _apply.finish"
status: done
owner: user
created: 2026-10-26
updated: 2026-10-26
kind: feature
source_idea: IDEA-333
tags: [agentic, queue, bug]
---

## Context

IDEA-333, from SPEC-0232's final review. In `queue._apply` (src/wt/agentic/queue.py), the inner
`finish()` calls `current = _fresh(cfg, state)` with no lock, decides, then hands `current` to
`_park` / `_transition`, which take `store.item_lock` only for their own write-back
(`_transition`, queue.py ~755) and write the whole stale copy unconditionally. Any write landing
between the read and the write-back is lost: a deferred escalation's
`park_requested={reason: human_decision}` (the campaign carries on, the ESC record stays open but
not live and is later auto-withdrawn after the human was notified), a human `wt queue park`
mid-tick, or an `escalations withdraw` that clears the request (the tick then parks for a
withdrawn escalation). `item_lock` is reentrant per SPEC-0176.

## Goals / Non-goals

**Goals:** `queue._apply.finish` reads, decides and writes campaign state atomically with respect
to other writers, and decides `park_requested` only from the fresh on-disk copy.
**Non-goals:** any other read-then-write site (`_fresh` callers at queue.py ~1541, the close
finish phase at ~1633, other `_apply_*` helpers) unless it is listed here; CAS/seq fields;
schema or event changes.

## Decision

Make read, decision and write one critical section: `finish` takes `store.item_lock(cfg, cid)`
around the whole body (the `_fresh` read, the key copy-in, the `park_requested` evaluation and the
`_park`/`_transition`/skipped-note branch). Reentrancy lets `_park`/`_transition` re-take it.
No CAS/retry is needed because the lock is held across the read-modify-write.

Under the lock the `park_requested` decision uses ONLY the fresh disk copy: the line
`requested = current.get("park_requested") or state.get("park_requested")` (queue.py ~1440)
becomes `requested = current.get("park_requested")`. The queue never sets `park_requested` on the
in-memory tick-start `state` (the only writer, `wt queue park`, writes disk under `item_lock`;
verified by grep), so the fallback could only resurrect a request that a withdraw had cleared on
disk, and the lock alone cannot fix that. A park_requested written before `finish` takes the lock
is acted on by this tick; one written while the lock is held is serialized after `finish`'s write,
survives it (finish writes `current`, which was read under the lock and does not carry it), and
is acted on by the next tick. If `finish` itself moves the campaign to a terminal state or parks
for its own reason, the later-written request stays on disk unchanged (a terminal transition
already pops it via `_transition`; a park for another reason keeps the request for resume): this
spec accepts that behaviour and changes nothing there.

## Design

In `queue._apply.finish`: wrap the body (from `current = _fresh(...)` to the final `return row`)
in `with store.item_lock(cfg, cid):`; the existing inner `with` in the `current is None` branch
becomes redundant but harmless. `ESC.note_park` stays inside the lock: the documented order
(escalations.py header) is always the campaign's `item_lock`, then the escalations lock;
`ESC.withdraw`/`withdraw_for_campaign` already take `item_lock` first and `_transition` already
calls `withdraw_for_campaign` while holding it, and `note_park` only mutates ESC records. No
caller passes a request through `state` (`_apply_spec_write`/`_apply_reviews` do not set it). Keep `_fresh` itself unchanged. No schema
or event changes.

## Alternatives considered

- **Compare-and-swap on `updated_at`/seq with retry** - more code, retry loops and still needs the
  lock to be correct on write; the lock is already reentrant and cheap here.
- **Make `_park` re-read** - fixes only the park path, not `_transition`'s unconditional
  write-back of the stale copy.

## Acceptance criteria

- [x] `finish` holds `item_lock` for the `_fresh` read through the state write.
- [x] A `park_requested` (human_decision or human) written before `finish` takes the lock is
      acted on by this tick; one written while the lock is held survives `finish`'s write and is
      acted on by the next tick; it is never overwritten.
- [x] `finish` decides only from the fresh disk copy (the `or state.get("park_requested")`
      fallback is gone): a `park_requested` cleared on disk by a withdraw before `finish` reads is
      not acted on even though the in-memory `state` still carries it.
- [x] No deadlock with `ESC.note_park` or other callers; existing queue tests pass.

## Test plan

Automated (`tests/test_agentic_queue_finish_race.py`, using the fake runner and a campaign in a
step-applying state):
- Wrap `queue._fresh` with a shim that, right after the real read, starts a thread calling
  `store.item_lock` + write of `park_requested={"reason":"human_decision",...}` and joins it with
  a short timeout. Pre-fix the writer completes before `finish` writes and is lost; post-fix the
  writer blocks on the lock until `finish` is done (assert `thread.is_alive()` inside the shim,
  which also proves the lock is held; after `_apply` returns it completes) - and the resulting
  state still contains `park_requested`, as the next tick sees it. Drive the writer through the
  real entry points (the `wt queue park` handler, the ESC escalation write, `ESC.withdraw`) rather
  than a hand-written write, so the test also proves those paths take `item_lock`.
- Clearing test (fallback fix): put `park_requested` into the in-memory `state` passed to the
  tick, clear it on disk through the real `ESC.withdraw` path (or `item_lock` + `write_state`)
  BEFORE `finish`'s `_fresh` read, and assert the tick does not park. (A clear racing after the
  read blocks on the lock until `finish` is done, so `finish` parks and the clear lands on a PARKED
  campaign; that is correct serialization and is not what this test checks.)
- **Sabotage checks:** remove the `with item_lock` wrapper and confirm the race test fails; restore
  the `or state.get("park_requested")` fallback and confirm the clearing test fails.
Regression: `tests/test_agentic_queue.py`, escalation tests, then the full `uv run pytest`.

### Test record (2026-10-26)

`tests/test_agentic_queue_finish_race.py` (3 tests): the real `Q.apply_park` and
`ESC.apply_escalate` run in a thread started right after `finish`'s `_fresh` read; the thread is
still blocked on `item_lock` after the join timeout, and after `_apply` the request is on disk
with `finish`'s own write intact and the next `plan_step` parks. `ESC.withdraw` clears the request
before the read while the tick-start `state` still carries it: the tick does not park. Sabotage:
without the `with item_lock` wrapper both race tests fail; with the `or state.get("park_requested")`
fallback restored the clearing test fails. Ran `uv run pytest -q -x` (full suite) and
`python3 tools/spec_lint.py`.

### As shipped

As designed. The tests use a malformed review in REVIEWING (the campaign stays in REVIEWING) so
an escalation plan made before `finish`'s write stays valid; with a state-changing step
`ESC.apply_escalate` correctly refuses the then-stale plan ("changed since the plan").

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated.
