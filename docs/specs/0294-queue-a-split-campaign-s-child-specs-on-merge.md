---
id: SPEC-0294
title: "Queue a split campaign's child specs on merge, in dependency order"
status: accepted
owner: user
created: 2026-10-30
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-065
parent: SPEC-0169
depends_on: [SPEC-0290, SPEC-0276]
tags: [agentic, queue, specs]
---

## Context

Promoted from idea `IDEA-065`. Part 2 of 2, after
[SPEC-0290](./0290-automatic-spec-splitting-from-a-split-review.md). SPEC-0290 ends with the
child specs merged to base as `accepted`, wired by `depends_on`. Nobody queues them yet:

- `src/wt/agentic/queue.py:plan_add_batch` / `apply_add_batch` (SPEC-0276) already queue
  several specs in their in-batch `depends_on` order, refuse cycles, and cascade a refused
  item to its dependents.
- `src/wt/agentic/queue.py:plan_add` calls `SR.check_admissible`, which requires a spec review
  per spec. The children have none. Their scope was set by the split review of the original,
  and the conductor rule "one spec review round" counts that review as the round.
- `queue._reconcile_closed` is the best-effort post-merge hook. Its failures become minor
  `bookkeeping` findings and never change the terminal state.

## Goals / Non-goals

**Goals**
- When a `kind: "split"` campaign reaches MERGED, its children are queued automatically as
  one SPEC-0276 batch, in dependency order.
- The children are admitted on the strength of the split review, through a narrow new
  admission `split_child`. It holds only for spec ids that the merged split campaign recorded
  as its parts.
- Failures are visible and do not undo the merge. A conductor can re-queue by hand
  (`wt queue add <ids>`) with the same admission.
- The wt-conduct skill's split rule describes the automatic path.

**Non-goals**
- Reviewing each child separately. The split review already set the scope. A human can still
  run `wt spec review` on a child before it is admitted, and an existing review of the child
  takes precedence.
- Splitting the children further.

## Decision

Add `queue._queue_split_children(cfg, state, events)`, called right after `_reconcile_closed`
when a split campaign is MERGED. It builds `plan_add_batch(cfg, part_ids, project=…,
priority=state priority)` and applies it with `skip_invalid=True`. A refused part is recorded
and does not block its siblings, except that the SPEC-0276 cascade refuses its dependents.

Admission: `check_admissible` stays pure over reviews. `plan_add` gets one extra rule. When
the spec has **no** review at all, and a MERGED campaign of `kind: "split"` lists the spec id
in `state.split.parts`, the admission is `split_child`, with `review_doc = None` and the
default profile. The lookup is `store.list_campaigns(cfg, include_terminal=True,
state="MERGED")`, filtered to `kind == "split"`. This makes the by-hand re-queue work too.

## Design

- `src/wt/agentic/queue.py`:
  - New `_split_origin(cfg, spec_id) -> dict | None` returns the MERGED split campaign whose
    `state.split.parts[*].spec_id` contains `spec_id`.
  - `plan_add`: in the `else` (not `force`) branch, catch the `ValueError` from
    `check_admissible`. When `SR.latest_review(cfg, spec_id, path) is None` and
    `_split_origin` returns a campaign, set `review = "split_child"`, `review_doc = None`
    and `plan["split_origin"] = <campaign id>`. Otherwise re-raise.
  - `apply_add`: it already stores `admission = plan["review"]`. It also stores
    `state["split_origin"]` and appends `admitted_split_child` `{origin}`. Register the event
    in `schemas.EVENT_KEYS` as `("origin",)`.
  - New `_queue_split_children(cfg, state, events)`:
    - The ids are `[p["spec_id"] for p in state["split"]["parts"]]`.
    - It calls `plan_add_batch(..., project=state["project"], priority=state["priority"])`
      and `apply_add_batch(..., skip_invalid=True, by="queue")`.
    - It appends `split_queued` `{queued: [{spec_id, campaign}], refused: [{spec_id,
      error}]}` (register it in `EVENT_KEYS` as `("queued", "refused")`).
    - Each refusal also becomes a `major` finding with `gate: bookkeeping`, so the conductor's
      event stream surfaces it.
    - Any exception becomes one `major` bookkeeping finding. The state stays MERGED.
  - Call site: the close apply that runs `_reconcile_closed(cfg, state, events,
    oc.next_state)`. When `oc.next_state == "MERGED"` and `state.get("kind") == "split"`, call
    `_queue_split_children` after it.
- The children's `depends_on` gate admission as usual (`backlog_readiness`). With
  SPEC-0290's chain, part k stays QUEUED (blocked) until part k-1 is `done`.
- `skills/wt-conduct/SKILL.md`, in the "One spec review round" bullet: replace
  "`split` ⇒ split, and each part gets its own single review" with:
  "`split` ⇒ `wt queue add` the spec as is. A split campaign writes the parts (SPEC-0290) and
  queues them on merge (SPEC-0294). A `split_queued` event with refusals, or a split refused at
  add (outbound, stale review, wrong part count), goes to the steward if one is armed,
  otherwise to the human."

## Alternatives considered

- **Run a headless spec review per child before queueing.** Rejected for now. It costs one
  reviewer run per part and contradicts the one-review-round rule. A child that a human
  reviews still uses its own review, because a review of the child takes precedence.
- **Queue children at finalize time, before the merge.** Rejected. Their spec files are not
  on base yet, and `backlog_readiness` reads base.
- **A `--split-of` flag on `wt queue add`.** Rejected. The admission derives from durable
  campaign state, so a by-hand re-queue needs no flag.

## Acceptance criteria

- [ ] When a `kind: "split"` campaign reaches MERGED, its parts are queued as one SPEC-0276
      batch in dependency order, and a `split_queued` event lists the queued campaigns and the
      refusals.
- [ ] `plan_add` admits a spec with no review as `split_child` only when a MERGED split
      campaign lists it as a part. Any other review-less spec is still refused. A child that
      has its own review is admitted by that review.
- [ ] A refused part becomes a `major` bookkeeping finding and does not stop its non-dependent
      siblings. Its dependents are refused by the cascade. An exception in the hook leaves the
      campaign MERGED.
- [ ] A by-hand `wt queue add <child>` after a failed automatic queue uses the same
      `split_child` admission.
- [ ] The wt-conduct skill's "One spec review round" bullet describes the automatic split
      path.

## Test plan

- `tests/test_agentic_queue.py`, using the fake runner and the SPEC-0290 fixture flow:
  - `test_split_merge_queues_children_in_order`: 3 parts merge. There are three QUEUED
    campaigns, the batch order is part 1, 2, 3, the `split_queued` event lists all three, and
    part 2 is not ready while part 1 is `accepted`.
  - `test_split_child_admission_requires_origin`: a review-less accepted spec that no split
    campaign lists is refused with the existing "no spec review" message.
  - `test_split_child_own_review_wins`: a child with an `approve` review has admission
    `approved`.
  - `test_split_queue_refusal_is_finding`: part 2 already has an active campaign. Part 2 is
    refused, part 3 is cascade-refused, part 1 is queued, and there are `major` bookkeeping
    findings for each refusal.
  - `test_split_queue_exception_keeps_merged`: `plan_add_batch` is monkeypatched to raise. The
    state is MERGED and there is one bookkeeping finding.
  - `test_manual_requeue_uses_split_child`: the automatic queue fails, then `plan_add(child)`
    gives `review == "split_child"` and `split_origin`.
  - `test_split_events_validate`: `admitted_split_child` and `split_queued` pass `schemas`.
- `tests/test_skills.py`:
  `test_conduct_skill_split_rule_mentions_split_campaign`.
- **Sabotage check:**
  - Drop the origin check, so any review-less spec is admitted.
    `test_split_child_admission_requires_origin` must fail.
  - Call `apply_add_batch` with `skip_invalid=False`. `test_split_queue_refusal_is_finding`
    must fail, because part 1 is no longer queued.
- **Regression guard:** full `uv run pytest`. The SPEC-0276 batch tests and the admission
  tests are unchanged.

## Rollout / migration

It takes effect for split campaigns that merge after this lands. Children of an earlier
split, if any, can be queued by hand with the same admission.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
