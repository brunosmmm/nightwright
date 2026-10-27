---
id: SPEC-0256
title: "Treat idea-link failure in spec_writer as best-effort, not a tick abort"
status: done
owner: user
created: 2026-10-26
updated: 2026-10-26
kind: feature
source_idea: IDEA-259
tags: [agentic, spec-write, bug]
---

## Context

IDEA-259, from SPEC-0215's final review (major finding). `queue._apply_spec_write`
(src/wt/agentic/queue.py) calls `_link_idea` inside the `item_lock` after writing
`spec`/`spec_id` to state. `_link_idea` uses `org_write.set_property` / `set_state`, which raise
`ValueError` on drift (ideas file edited during the writer run) or when the file has no `SPECCED`
keyword. Nothing catches it, so the tick aborts with state already holding `spec_id`: the
`spec_written` / `commit` / `run.end` / cost events are lost, `spec_write_retries` and
`pending_brief` are not reset, the campaign stays `SPEC_WRITING`, and a later tick may run a paid
revise of an accepted spec. SPEC-0209's abandon semantics already treat the link as optional.

## Goals / Non-goals

**Goals:** an idea-link failure never aborts the tick and the spec-write bookkeeping always
completes.
**Non-goals:** retry or auto-repair of the link; escalation or park on link failure; any change to
the revise-mode path (linking only happens for `mode == "write"`); changing how `_do_write_spec`
worker exceptions are handled (this spec only adds tests for that).

## Decision

`_link_idea` becomes best-effort: its whole body (including both `_idea_task` reads, which parse
the ideas file and can raise `OSError`) is wrapped to catch `(ValueError, OSError)`, and it
returns an error string (None on success); it never raises. `_apply_spec_write` appends a `note`
(`author: queue`, text `could not link idea to SPEC-NNNN: <error>`) and an `idea_link_failed`
event `{spec_id, idea, error}` and carries on with the normal bookkeeping (events, retry/brief
reset, park `spec_review_unavailable`). Additionally, a crash escaping `_do_write_spec` itself is
already a step error; this spec adds the missing tests to pin it.

## Design

Edit `_link_idea` and the call site inside the `with store.item_lock` block of
`_apply_spec_write`; collect the error and append events after the lock is released (the events
list is flushed by `finish`). Register the event by adding `"idea_link_failed": ("spec_id", "idea", "error")` to
`schemas.EVENT_KEYS` (required: `ledger_problems` treats an unknown type or missing key as an
error). The failure is not escalated: the spec is
committed and linking can be redone with `wt idea` tooling.

## Alternatives considered

- **Park on link failure** - blocks a good, committed spec on bookkeeping.
- **Retry the link** - drift is persistent until a human re-saves; a note suffices.

## Acceptance criteria

- [x] `OW.set_property` or `set_state` raising `ValueError` does not abort the tick.
- [x] After such a failure the campaign has `spec`/`spec_id`, the `spec_written`, `commit`,
      `run.end` and cost events exist, `spec_write_retries == 0`, `pending_brief is None`, and the
      campaign is PARKED `spec_review_unavailable`.
- [x] A note (`author: queue`, text starting `could not link idea to SPEC-NNNN:`) and an
      `idea_link_failed` event record the error, and the recorded ledger passes
      `schemas.ledger_problems`.
- [x] A test pins that an exception from `_do_write_spec` leaves `state["step"]` unchanged
      (same step kind and run_id still recorded) and parks `spec_write_failed` with resume_to
      SPEC_WRITING.
- [x] A test covers revise mode with deletion of `rel_path` plus a second changed file.
- [x] The committed-then-deleted and amended-base tests assert the problem text.

## Test plan

Automated (extend `tests/test_agentic_spec_writer.py`):
- Monkeypatch `org_write.set_property` to raise `ValueError("drift")`; run a write-mode tick to
  completion; assert the ACs above (events via `store.read_ledger`).
- Same with `set_state` raising; and the no-SPECCED-keyword case (fixture: an ideas file whose
  `#+TODO` line lacks `SPECCED`). Assert the note text prefix `could not link idea to SPEC-NNNN:`
  with author `queue`, and that the ledger passes `schemas.ledger_problems`.
- An `OSError` from `_idea_task` (ideas file moved during the run) is also swallowed.
- Monkeypatch `_do_write_spec` to raise; assert the step stays submitted and the park reason.
- Revise-mode deletion test and the two strengthened problem-text assertions.
- **Sabotage check:** remove the try/except in `_link_idea`; the drift tests must fail.
Regression: full `uv run pytest`.

### Test record (2026-10-26)

- `uv run pytest -q tests/test_agentic_spec_writer.py`: 72 passed (new: set_property / set_state
  drift, no-SPECCED `#+TODO`, `OSError` from `_idea_task`, `_link_idea` success/idempotent,
  `_do_write_spec` raising, revise-mode deletion plus a second file; two strengthened
  problem-text assertions).
- Sabotage check: replacing the `(ValueError, OSError)` handler in `_link_idea` made the four
  link-failure tests fail; restored.
- Full `uv run pytest -q -x` and spec-lint run as the gates (see the run record).

### As shipped

As designed. The step-unchanged AC is asserted through the ledger `run.start` event (step kind
`write_spec`, persona `spec_writer`), `counters.runs == 1`, the `spec_write` request kept and no
`spec_id` recorded, because campaign state has no `step` field.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated.
