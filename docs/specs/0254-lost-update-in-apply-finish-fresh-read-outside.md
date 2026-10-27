---
id: SPEC-0254
title: "Take item_lock around the fresh read and write in _apply.finish"
status: accepted
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

## Decision

Make read, decision and write one critical section: `finish` takes `store.item_lock(cfg, cid)`
around the whole body (the `_fresh` read, the key copy-in, the `park_requested` evaluation and the
`_park`/`_transition`/skipped-note branch). Reentrancy lets `_park`/`_transition` re-take it.
No CAS/retry is needed because the lock is held across the read-modify-write.

## Design

In `queue._apply.finish`: wrap the body (from `current = _fresh(...)` to the final `return row`)
in `with store.item_lock(cfg, cid):`; the existing inner `with` in the `current is None` branch
becomes redundant but harmless. `ESC.note_park` is called inside the lock; confirm it does not take
a lock ordering that inverts (escalations store lock vs item lock) - if it does, move that one call
after the `with` block (it only writes the ESC record). Keep `_fresh` itself unchanged. No schema
or event changes.

## Alternatives considered

- **Compare-and-swap on `updated_at`/seq with retry** - more code, retry loops and still needs the
  lock to be correct on write; the lock is already reentrant and cheap here.
- **Make `_park` re-read** - fixes only the park path, not `_transition`'s unconditional
  write-back of the stale copy.

## Acceptance criteria

- [ ] `finish` holds `item_lock` for the `_fresh` read through the state write.
- [ ] A `park_requested` (human_decision or human) written by another thread while `finish` is
      between its read and write is never overwritten; the tick parks on it.
- [ ] A `park_requested` cleared concurrently by a withdraw is not acted on from a stale copy.
- [ ] No deadlock with `ESC.note_park` or other callers; existing queue tests pass.

## Test plan

Automated (`tests/test_agentic_queue_finish_race.py`, using the fake runner and a campaign in a
step-applying state):
- Wrap `queue._fresh` with a shim that, right after the real read, starts a thread calling
  `store.item_lock` + write of `park_requested={"reason":"human_decision",...}` and joins it with
  a short timeout. Pre-fix the writer completes before `finish` writes and is lost; post-fix the
  writer blocks on the lock until `finish` is done (assert `thread.is_alive()` while inside the
  shim, then after `_apply` returns it completes) - and the resulting state still contains
  `park_requested`, as the next tick sees it.
- Same shim clearing `park_requested`: the tick must not park for the withdrawn request.
- Assert the lock is held: inside the shim `store.item_lock` from a second thread cannot be taken
  without blocking.
- **Sabotage check:** remove the `with item_lock` wrapper and confirm the race tests fail.
Regression: `tests/test_agentic_queue.py`, escalation tests, then the full `uv run pytest`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`).
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated.
