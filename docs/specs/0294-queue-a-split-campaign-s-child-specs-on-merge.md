---
id: SPEC-0294
title: "Queue a split campaign's child specs on merge, in dependency order"
status: done
owner: user
created: 2026-10-30
updated: 2026-10-30
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
- When a `kind: "split"` campaign reaches MERGED, by either route (a local merge in the close
  apply, or `HANDED_OFF` -> `MERGED` in the PR sweep), its children are queued automatically
  as one SPEC-0276 batch, in dependency order.
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
- Binding the admission to the child's text. `split_child` holds for the ids the split
  recorded, whatever the child says now. A child edited after the merge (including a relaxed
  `depends_on`, which SPEC-0290 allows) is admitted all the same, as long as it is `accepted`
  and has no review of its own. A human who wants a fresh look runs `wt spec review`, and
  that review then takes precedence.
- Making the PR sweep wait for the child files. Under `merge.policy: pr` the main checkout may
  not have pulled the merged files when the sweep sees the merge. The affected parts are
  refused (a finding each) and re-queued by hand later (see Design).

## Decision

Add `queue._queue_split_children(cfg, state, events)`, called when a split campaign becomes
MERGED, from both places that do that: the close apply (local merge) and `_sweep_write`
(`HANDED_OFF` -> `MERGED`). It builds `plan_add_batch(cfg, part_ids, project=…,
priority=state priority, split_origin=state)` and applies it with `skip_invalid=True`. A
refused part is recorded and does not block its siblings, except that the SPEC-0276 cascade
refuses its dependents.

The hook runs **before** the campaign is persisted as MERGED (both call sites run
`_reconcile_closed` first and write the new state afterwards, under the item lock). So the
admission cannot read the origin from disk at that point. The in-flight campaign is passed
explicitly as `split_origin`, and it is accepted as origin when its `kind` is `"split"` and its
`state.split.parts` lists the spec id. The durable lookup of MERGED campaigns is kept for the
by-hand path only.

Admission: `check_admissible` stays pure over reviews. `plan_add` gets one extra rule. When
the spec has **no** review at all and an origin is found (the explicit `split_origin`, or else
a durable MERGED campaign of `kind: "split"` that lists the spec id in `state.split.parts`),
the admission is `split_child`, with `review_doc = None` and the default profile. The durable
lookup is `store.list_campaigns(cfg, include_terminal=True, state="MERGED")`, filtered to
`kind == "split"`. This makes the by-hand re-queue work too.

The admission is tied to the id, not to the child's text (see Non-goals). Status
`accepted` is still required by `plan_add`.

## Design

- `src/wt/agentic/queue.py`:
  - New `_split_origin(cfg, spec_id, inflight=None) -> dict | None`. With `inflight` (a state
    dict) it returns that state when `inflight["kind"] == "split"` and
    `inflight["split"]["parts"][*]["spec_id"]` contains `spec_id`, else `None`: no disk read,
    because the in-flight campaign is not MERGED on disk yet. Without `inflight` it returns
    the durable MERGED split campaign whose `state.split.parts[*].spec_id` contains
    `spec_id`.
  - `plan_add` and `plan_add_batch` get a keyword `split_origin: dict | None = None` (the
    batch passes it to each `plan_add`). In `plan_add`'s `else` (not `force`) branch, catch
    the `ValueError` from `check_admissible`. When `SR.latest_review(cfg, spec_id, path) is
    None` and `_split_origin(cfg, spec_id, split_origin)` returns a campaign, set `review =
    "split_child"`, `review_doc = None` and `plan["split_origin"] = <campaign id>`. Otherwise
    re-raise. When `split_origin` is passed and does not list the id, the durable lookup is
    not used as a fallback.
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
    - Exception path: any exception (from planning or from partway through
      `apply_add_batch`) is caught. `split_queued` is **still emitted**, with `queued`
      rebuilt from what exists: every campaign whose `state.split_origin == state["id"]`
      (`store.list_campaigns(cfg, include_terminal=True)`, which covers children created
      before the exception), and `refused` listing the remaining part ids with the error text.
      One `major` bookkeeping finding records the exception and names the queued children, so
      nothing is queued without a record. The rebuild is itself wrapped: if it fails, emit
      `split_queued` with empty lists. The state stays MERGED.
  - Call sites (both only when the new state is `MERGED` and `state.get("kind") == "split"`):
    - The close apply that runs `_reconcile_closed(cfg, state, events, oc.next_state)`
      (queue.py ~2055): call `_queue_split_children(cfg, state, events)` after it. `events`
      is persisted by `finish(...)`.
    - `_sweep_write`, in the `action == "merge"` branch after `_reconcile_closed(cfg, st,
      events, "MERGED")`: the same call, with `events` passed on to `_transition`. A
      `HANDED_OFF` split campaign (policy `pr`) therefore queues its children when the sweep
      sees the merge. `plan_add` reads the spec files through `SP.resolve_spec_path` in the
      main checkout. If that checkout has not pulled the merged child files yet, the part is
      refused ("spec not found"), with a `major` bookkeeping finding, and its dependents are
      cascade-refused. Once the checkout is updated, `wt queue add <ids>` queues them with
      the durable `split_child` admission, because the campaign is MERGED by then.
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

- [x] When a `kind: "split"` campaign reaches MERGED in the close apply, its parts are queued
      as one SPEC-0276 batch in dependency order, although the campaign is still CLOSING on
      disk when the hook runs, and a `split_queued` event lists the queued campaigns and the
      refusals.
- [x] The same holds for a `HANDED_OFF` split campaign that the PR sweep moves to MERGED. If
      the child files are not in the main checkout yet, the parts are refused with `major`
      bookkeeping findings, the campaign still becomes MERGED, and a later `wt queue add`
      admits them.
- [x] An in-flight `split_origin` is accepted only when its `kind` is `"split"` and its parts
      list the spec id. The durable MERGED lookup serves only calls without `split_origin`.
      A child is admitted by id alone, whatever its text now says, if it is `accepted`.
- [x] `plan_add` admits a spec with no review as `split_child` only when a MERGED split
      campaign lists it as a part. Any other review-less spec is still refused. A child that
      has its own review is admitted by that review.
- [x] A refused part becomes a `major` bookkeeping finding and does not stop its non-dependent
      siblings. Its dependents are refused by the cascade. An exception in the hook leaves the
      campaign MERGED, still emits `split_queued` (listing any children already created), and
      adds one `major` bookkeeping finding.
- [x] An applied `split_child` admission persists `state["split_origin"]` (the split
      campaign id) and appends an `admitted_split_child` `{origin}` event on the child.
- [x] A by-hand `wt queue add <child>` after a failed automatic queue uses the same
      `split_child` admission.
- [x] The wt-conduct skill's "One spec review round" bullet describes the automatic split
      path.

## Test plan

- `tests/test_agentic_queue.py`, using the fake runner and the SPEC-0290 fixture flow:
  - `test_split_merge_queues_children_in_order`: 3 parts; the split campaign goes through the
    real close apply (CLOSING -> MERGED), not a hand-built MERGED state. There are three QUEUED
    campaigns, the batch order is part 1, 2, 3, the `split_queued` event lists all three, and
    part 2 is not ready while part 1 is `accepted`. This is the ordering guard: a monkeypatch
    asserts the on-disk state is still CLOSING when `_queue_split_children` runs, so a
    durable-only lookup would refuse every part.
  - `test_split_origin_inflight_vs_durable`: `_split_origin(cfg, id, inflight=state)` returns
    the state for a CLOSING split that lists the id, and `None` for a non-split state or one
    that does not list it; `_split_origin(cfg, id)` finds none while the campaign is CLOSING
    and finds it once MERGED.
  - `test_split_sweep_queues_children`: a `HANDED_OFF` split campaign, the sweep `view`
    returns a merged PR. The children (files present) are queued, `split_queued` is on the
    campaign's events, and the campaign is MERGED.
  - `test_split_sweep_missing_files_refused`: same, but the child files are absent from the
    main checkout. The campaign is MERGED, each part is refused with a `major` bookkeeping
    finding, nothing is queued, and after the files appear `plan_add(child)` gives
    `review == "split_child"`.
  - `test_split_child_text_not_bound`: a child whose body was edited after the merge (no
    review) is still admitted as `split_child`.
  - `test_split_child_admission_requires_origin`: a review-less accepted spec that no split
    campaign lists is refused with the existing "no spec review" message.
  - `test_split_child_own_review_wins`: a child with an `approve` review has admission
    `approved`.
  - `test_split_queue_refusal_is_finding`: part 2 already has an active campaign. Part 2 is
    refused, part 3 is cascade-refused, part 1 is queued, and there are `major` bookkeeping
    findings for each refusal.
  - `test_split_queue_exception_keeps_merged`: `plan_add_batch` is monkeypatched to raise. The
    state is MERGED, there is one bookkeeping finding, and a `split_queued` event is present
    with empty `queued`. A second variant monkeypatches `apply_add_batch`'s per-item apply to
    raise on the second part: the first part's campaign exists, `split_queued.queued` lists
    it, `refused` lists the others, the state is MERGED, and there is one bookkeeping finding
    naming the queued child.
  - `test_manual_requeue_uses_split_child`: the automatic queue fails, then `plan_add(child)`
    gives `review == "split_child"` and `split_origin`. Then `apply_add(plan)` persists
    `state["split_origin"] == <split campaign id>` on the new campaign and its events contain
    `admitted_split_child` with `{origin}`.
  - `test_split_events_validate`: `admitted_split_child` and `split_queued` pass `schemas`.
- `tests/test_skills.py`:
  `test_conduct_skill_split_rule_mentions_split_campaign`.
- **Sabotage check:**
  - Drop the origin check, so any review-less spec is admitted.
    `test_split_child_admission_requires_origin` must fail.
  - Call `apply_add_batch` with `skip_invalid=False`. `test_split_queue_refusal_is_finding`
    must fail, because part 1 is no longer queued.
  - Make `_split_origin` ignore `inflight` (durable lookup only).
    `test_split_merge_queues_children_in_order` and `test_split_sweep_queues_children` must
    fail.
- **Regression guard:** full `uv run pytest`. The SPEC-0276 batch tests and the admission
  tests are unchanged.

### Test record (2026-10-30)

- `uv run pytest tests/test_agentic_split_queue.py` — 13 passed (the tests of this plan).
- Related modules: `test_agentic_queue.py`, `test_agentic_queue_cli.py`,
  `test_agentic_idea_campaign.py`, `test_spec_review_outbound_root.py`, `test_skills.py`,
  `test_agentic_pr_sweep.py`, `test_agentic_idea_link.py`, `test_agentic_split.py`,
  `test_agentic_spec_review.py` — 447 passed together.
- Sabotage: dropping the origin check failed `test_split_child_admission_requires_origin` and
  `test_split_origin_inflight_vs_durable`; `skip_invalid=False` failed
  `test_split_queue_refusal_is_finding` (and the sweep missing-files test); ignoring `inflight`
  failed `test_split_merge_queues_children_in_order`, `test_split_sweep_queues_children` and
  three more. All restored afterwards.
- `python3 tools/spec_lint.py` passes and the ledger is regenerated. The full `uv run pytest`
  was left to gating.
- Review round 1 fix: both call sites now run `_queue_split_children` after `_release_idea_link`.
  `test_split_children_link_the_idea_the_split_released` (close and sweep routes) checks that
  the idea's `:CAMPAIGN:` names the first child and no child has an `idea_link_skipped`
  "held by <split>" event; with the old order the close route fails.

- Rebase round: the branch was rebased onto main (the `wt` -> `nightwright` package rename and
  `skills/wt-conduct` -> `skills/nw-conduct`; the skill edit and its test moved to
  `nw-conduct`). After `uv sync`, `uv run pytest -n 4` over the split queue, split, skills,
  PR sweep, idea link, queue and queue CLI, and spec review tests — 472 passed. The full suite
  is left to gating.

- Review round 2 fix: the durable `split_child` lookup is scoped to the spec's project
  (`_split_origin(..., project)` filters `list_campaigns(..., project=...)`; an in-flight origin
  must carry the same project), because spec ids are per project (SPEC-0327). New tests:
  `test_split_child_origin_is_scoped_to_the_project`, `test_split_child_inadmissible_own_review_is_not_rescued`
  (reject, stale approve), `test_split_close_handed_off_does_not_run_the_hook` (a real `merge.policy: pr`
  close with the real hook). Sabotage (drop the project argument, drop the own-review guard, drop the
  MERGED guard at the close call site) fails each new test. `tests/test_agentic_split_queue.py` — 19 passed;
  the full suite is left to gating.

### As shipped (2026-10-30)

- The tests are in `tests/test_agentic_split_queue.py` (reusing the `test_agentic_split.py`
  flow), not `tests/test_agentic_queue.py`. The skill test is in `tests/test_skills.py`.
- `test_split_queue_exception_keeps_merged` has the midway variant as a separate test,
  `test_split_queue_exception_midway_lists_created_children` (it patches `queue.apply_add`).
  The sweep tests mark the campaign `HANDED_OFF` by rewriting its state after a normal merge
  (the hook is stubbed for that merge), as `test_agentic_idea_link.py` does.
- `plan_add_batch` keeps a minimal copy of the origin (`id`, `kind`, `split.parts[*].spec_id`)
  in `params["split_origin"]`, so `apply_add_batch`'s re-plan accepts the same in-flight origin
  and the batch stays JSON-serializable. `plan_add` returns `split_origin` (the campaign id, or
  `None`) in every plan.
- Both call sites run the hook after `_release_idea_link`: the children inherit the original's
  `source_idea`, and the split campaign holds the idea's `:CAMPAIGN:` until it is released.
- In the close apply the hook runs only when the outcome is `MERGED`, not for `HANDED_OFF`;
  the `HANDED_OFF` -> `MERGED` edge is the sweep's.

## Rollout / migration

It takes effect for split campaigns that merge after this lands. Children of an earlier
split, if any, can be queued by hand with the same admission.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
