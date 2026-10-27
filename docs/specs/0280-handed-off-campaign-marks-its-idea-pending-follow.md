---
id: SPEC-0280
title: "A HANDED_OFF campaign marks its idea pending its PR"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-351
parent: SPEC-0169
depends_on: [SPEC-0257]
tags: [agentic, queue, close, ideas, reconcile]
---

## Context

Promoted from idea `IDEA-351`, the follow-up that
[SPEC-0257](./0257-close-step-does-the-spec-and-idea-bookkeeping.md) filed. SPEC-0257 made the
close step reconcile the merged spec's idea, but only on `MERGED`. In `queue._apply` on a
close outcome, `_reconcile_merged(cfg, state, events)` runs only when `oc.next_state ==
"MERGED"`. SPEC-0257's Non-goals say why: for `HANDED_OFF` (`merge.policy: pr` or
`draft-pr`; `close.py` returns `CloseOutcome("HANDED_OFF", close={"pr_url": url, ...})`), the
spec is not `done` on the base yet.

As a result, after a hand-off the idea looks untouched. It may still be IDEA or INCUBATE, and
nothing records that a PR carries its work, so `wt ideas` and `wt next` keep offering it as
open work. Nothing records the PR URL on the idea either.

## Goals / Non-goals

**Goals**
- On `HANDED_OFF`, the close step pulls the spec's idea up by the existing reconcile rules
  (an accepted spec gives SPECCED) and writes `:HANDED_OFF: <pr_url>` on the idea.
- `wt ideas`, `wt idea show` (JSON and table) and `wt next` show that the idea is pending a
  PR.
- When reconcile later advances the idea past SPECCED (the PR merged and the spec is
  `done`), it clears `:HANDED_OFF:`.

**Non-goals**
- Polling GitHub for PR state, or reacting to a merge on the forge. The existing `wt spec
  reconcile` after the merge does the advance.
- A new org keyword: "pending" is a property, not a TODO state, so `org_idea_keywords` stays
  unchanged.
- Merge-kind campaigns (SPEC-0233); they have no spec.
- Detecting a PR closed without merging. The mark stays until the spec is re-queued (a new
  campaign clears it, see Design) or reconcile advances the idea; the `wt next` hint names
  the re-queue command.

## Decision

Generalize `queue._reconcile_merged` into `queue._reconcile_closed(cfg, state, events,
outcome)`, with the same best-effort contract: failures are minor `bookkeeping` findings and
never change the state. For `HANDED_OFF` it runs the same scoped reconcile, then sets
`:HANDED_OFF:` on the sole claimant idea. `specs.reconcile_ideas` clears the property
whenever it applies an `advance` to PROMOTED, SHIPPED or DROPPED.

## Design

- **`queue._apply`** (close branch): `if oc.next_state in ("MERGED", "HANDED_OFF"):
  _reconcile_closed(cfg, state, events, oc.next_state)`.
- **`queue._reconcile_closed`** (renamed from `_reconcile_merged`; no test references the
  old name, so there is no alias):
  - It keeps the existing steps: skip with no spec, skip with an info finding when the spec
    store is not this repo, and run `specs.reconcile_ideas(cfg, apply=True,
    spec_ids={spec_id}, backlinks=False)` with the `idea_reconciled` events.
  - For `HANDED_OFF`, it also resolves the idea with `specs._sole_claimant(ideas, spec_id)`
    over a fresh `_ideas(cfg)` taken **after** `reconcile_ideas` (which may just have written
    `:SPEC:`; `_ideas` invalidates the task cache). There is no fallback to the campaign's
    own idea: an idea without `:SPEC: <spec_id>` would never be advanced by reconcile, so
    its mark could never be cleared. It calls `org_write.set_property(cfg, task, "HANDED_OFF", pr_url or
    f"branch {repo.branch}")` and appends the ledger event `idea_handed_off {spec_id,
    idea_id, pr_url}`.
  - No claimant, or an ambiguous one, gives an `info` finding "no idea to mark handed off".
    A `ValueError` or `OSError` gives a `minor` finding. The campaign state is never
    changed.
- **`schemas.EVENT_KEYS`** gains `"idea_handed_off": ("spec_id", "idea_id", "pr_url")`, and
  `render.campaign_event` renders it next to `idea_reconciled` (`idea IDEA-N handed off →
  <pr_url>`).
- **`specs.reconcile_ideas`**: in the write pass, for an `advance` whose target is
  `PROMOTED`, `SHIPPED` or `DROPPED`, both the pinned (duplicate-id) path and the plain
  `EX.set_idea_state` path first do `ORG.invalidate_tasks_cache()` and `task =
  _resolve_idea(cfg, idea_id, pin)`, then `org_write.clear_property(cfg, task,
  "HANDED_OFF")` when the property is set. This runs **before** the state write, because
  those states archive the subtree. The dry-run detail text gets the suffix `(clears handed-off)`.
- **Display**:
  - `report.idea_row` and `explore.idea_show_payload` add `"handed_off": <value>` when the
    property is set.
  - The `wt ideas` table adds `⇢ PR` to the state cell.
  - `workflow.next_step_for_idea` returns `PR open (<value>); after it merges: wt spec
    reconcile --apply; if it was closed: wt queue add <SPEC>` for a SPECCED idea with
    `:HANDED_OFF:`, so `wt next` stops offering it as work to start.
- **Re-queue clears the mark**: `queue.apply_add` clears `:HANDED_OFF:` on the spec's sole
  claimant idea (best-effort; a failure is a ledger `note`, never a refusal).

## Acceptance criteria

- [x] A campaign that closes `HANDED_OFF` leaves its spec's idea SPECCED (from IDEA or
      INCUBATE, when the spec is accepted) with `:HANDED_OFF: <pr_url>`, and an
      `idea_handed_off` ledger event.
- [x] A bookkeeping failure on hand-off is a `minor` finding, and the campaign still ends
      `HANDED_OFF`.
- [x] `wt ideas --json`, `wt idea show --json` and the `wt ideas` table show the hand-off,
      and `wt next` gives the "PR open" hint instead of a start step.
- [x] `wt spec reconcile --apply`, after the spec becomes `done`, advances the idea and
      clears `:HANDED_OFF:`; re-queueing the spec also clears it. With no claimant idea,
      nothing is marked (an `info` finding).
- [x] `MERGED` behaviour is unchanged: no `:HANDED_OFF:` is written.

## Test plan

New tests in `tests/test_agentic_queue.py` and `tests/test_reconcile.py`, using the existing
close fakes (`CloseOutcome("HANDED_OFF", close={"pr_url": ...})`) and a tmp ideas file:

- `test_handed_off_marks_idea_pending`: an idea in INCUBATE with `source_idea` linkage, and
  an accepted spec, gives state SPECCED, the property equal to the PR URL, and the ledger
  event (which passes `schemas.ledger_problems`). The fixture idea's `:CREATED:` is on or
  before the spec's `created`, as the SPEC-0267 link check requires (otherwise reconcile
  reports `flag-mismatch` and writes nothing).
- `test_handed_off_bookkeeping_failure_is_minor`: `set_property` raises `OSError`, which gives
  a minor finding, and the state is `HANDED_OFF`.
- `test_handed_off_no_claimant_info_finding`: an idea campaign whose idea does not carry the
  spec's `:SPEC:` and is not claimed gets no mark and an `info` finding (no fallback).
- `test_requeue_clears_handed_off`: `apply_add` for the spec removes the property.
- `test_render_idea_handed_off_event`: `render.campaign_event` output.
- `test_merged_does_not_write_handed_off` (regression for the MERGED path).
- `test_reconcile_done_clears_handed_off`: the spec flips to `done`, `reconcile_ideas(apply=True)`
  is run, and the property is gone and the idea is PROMOTED (plain path and a duplicate-id
  pinned path).
- `test_idea_row_and_next_show_handed_off`: `idea_row`, `idea_show_payload` and
  `next_step_for_idea` output, plus the rendered `wt ideas` table (CliRunner) showing `⇢ PR`
  in the state cell.
- **Sabotage check:** keep the old `== "MERGED"` guard in `_apply`;
  `test_handed_off_marks_idea_pending` must fail. Drop the `clear_property` call;
  `test_reconcile_done_clears_handed_off` must fail.
- Regression guard: full `uv run pytest`, including the existing SPEC-0257 reconcile tests,
  which are unchanged.

### Test record (2026-10-27)

- New tests: `tests/test_agentic_queue.py` (`test_handed_off_marks_idea_pending`,
  `test_handed_off_bookkeeping_failure_is_minor`, `test_handed_off_no_claimant_info_finding`,
  `test_merged_does_not_write_handed_off`, `test_requeue_clears_handed_off`,
  `test_render_idea_handed_off_event`), `tests/test_reconcile.py`
  (`test_reconcile_done_clears_handed_off`, `..._on_a_duplicate_id`), `tests/test_ideas.py`
  (`test_idea_row_and_next_show_handed_off`, including the rendered `wt ideas` state cell).
- Sabotage: with the `== "MERGED"` guard back in `_apply` and the `clear_property` call dropped,
  six of these tests failed; restored, they pass.
- `uv run pytest -q -x`: all pass; `python3 tools/spec_lint.py --write-ledger` then
  `python3 tools/spec_lint.py` exit 0.

### As shipped (2026-10-27)

- `queue._clear_handed_off_on_requeue` and `queue._mark_handed_off` are small helpers next to
  `_reconcile_closed`; `specs._clear_handed_off` does the pre-state-write clear for both the
  pinned and plain advance paths.
- `wt idea show` (table) is the raw org text, which already shows the `:HANDED_OFF:` property;
  the JSON payload gains `handed_off`.


## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
