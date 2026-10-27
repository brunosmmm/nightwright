---
id: SPEC-0257
title: "Reconcile walks spec source_idea; close reconciles the merged spec's idea"
status: accepted
owner: user
created: 2026-10-26
updated: 2026-10-26
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, close, reconcile, ideas, bug]
source_idea: IDEA-067
depends_on: [SPEC-0078]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md); fixes
`IDEA-067` (kind: bug). After a merge the conductor still does idea bookkeeping by hand. The
evidence (IDEA-067 Log, 2026-10-26): about 40 `status: done` specs (SPEC-0200..0252) carry
`source_idea`, yet their ideas sit at `IDEA`/`INCUBATE`, which pollutes the open
priority-A idea list with shipped work.

Cause: `specs.reconcile_ideas` (src/wt/specs.py) starts from `ideas = [... if
properties.get("SPEC")]`, so it walks only ideas that carry a `:SPEC:` property. A spec whose
`source_idea` names an idea that never got the reverse `:SPEC:` (campaign-written specs, ideas
promoted by hand) is invisible, and `wt spec reconcile` reports "every linked idea already
matches". Separately, nothing in the close path (`agentic/queue.py` `_apply` on
`oc.next_state == "MERGED"`) runs reconcile.

## Goals / Non-goals

**Goals**
- G1. `reconcile_ideas` also walks specs' `source_idea`, writes the missing `:SPEC:` onto the
  idea, then advances it by the existing rules (`_implied_state`).
- G2. After a successful merge, the queue runs the same reconcile for the merged spec and
  records the outcome on the campaign. Failure is a finding, never a park.
- G3. The one-time repair of the ~40 stale ideas is `wt spec reconcile --apply`, no new code.

**Non-goals**
- HANDED_OFF campaigns (PR not merged): the spec is not `done` on the base yet, so nothing
  advances. The idea's "pending" state for hand-offs is out of scope.
- Closing open questions (SPEC-0080 stays report-only) and rewinding ideas already past
  `SPECCED`.

## Decision

Reconcile gains a second source of links: every internal spec whose frontmatter has
`source_idea: IDEA-N`. If that idea exists and has no `:SPEC:` (or an empty one), reconcile
plans a `link` action, writes `:SPEC: SPEC-NNNN` (apply only), and then evaluates the
idea/spec pair exactly like a linked one. An idea that already has a *different* `:SPEC:` is
left alone and reported as `flag-conflict`. The close step calls the same function restricted
to the merged spec.

## Design

- **`specs.reconcile_ideas(cfg, *, apply=False, spec_ids=None)`**: new optional `spec_ids`
  (iterable) restricts both passes to those specs. Before the existing loop, a new pass builds
  the list of unlinked pairs: for each `docs/specs/NNNN-*.md` (via `_specs_dir` +
  `split_frontmatter`, the loader `_load_spec_fm` uses) with a non-empty `source_idea`, look
  the idea up among `filter_tasks(load_tasks(cfg), is_idea=True)` by `properties["ID"]`. When
  the idea has no `:SPEC:`, add the row `(idea_id, spec_id, "link", "set :SPEC: <spec_id>")`
  and treat the idea as linked for the rest of the pass (state evaluation uses the same
  `_implied_state`, so `done` -> `PROMOTED`, `accepted` -> `SPECCED`, `superseded` ->
  `DROPPED`; terminal ideas are never rewound). When the idea has a different `:SPEC:`, add
  `(idea_id, spec_id, "flag-conflict", "idea links <other>")` and do nothing. A missing idea
  adds nothing (spec_lint already covers dangling `source_idea`).
- **Write pass order**: `W.set_property(cfg, task, "SPEC", spec_id)` for each `link` (as
  `scaffold_from_idea` does, specs.py:206), re-resolving the idea per write, then the existing
  advances and backlinks. `PROMOTED` archives the subtree (SPEC-0073), so the property write
  must precede the state write.
- **`wt spec reconcile`** (cli.py `spec_reconcile_cmd`) prints `link` rows like the other
  actions; `stale_idea_count` also counts `link` rows whose spec implies an advance (it counts
  `advance` rows already, and `link` rows are followed by an `advance` row in the plan).
- **Close hook**: new `queue._reconcile_merged(ctx, state, events)`, called in `_apply` right
  after `state["close"] = dict(oc.close)` when `oc.next_state == "MERGED"` and `spec_id` is
  set. It runs `reconcile_ideas(cfg, apply=True, spec_ids=[spec_id])` inside try/except
  `Exception`, then appends `("idea_reconciled", {"spec_id", "idea_id", "from", "to"})` for
  each applied `link`/`advance` row (registered in `schemas.py`, rendered by `render.py` as
  `idea IDEA-N: INCUBATE -> PROMOTED`), or `("finding", {severity: "minor", gate:
  "bookkeeping", title: "idea reconcile failed", detail})` on error. The merge is already
  done, so no outcome of this hook changes the campaign state. The merged spec is read from the
  main checkout, which holds the merged tree after `ff_merge`.
- **One-time repair**: after this ships, run `wt spec reconcile` (review) then
  `wt spec reconcile --apply`. Idempotent: a second run reports "every linked idea already
  matches".

## Alternatives considered

- **Backfill `:SPEC:` with a one-off script.** Rejected: the missing-CLI rule; and the gap
  would reopen for every future hand-promoted idea.
- **Reconcile in the conductor only.** Rejected: that is today's manual step, and the close
  already knows exactly which spec merged.
- **Park on reconcile failure.** Rejected: the merge is irreversible and the idea state is
  derivable later.

## Acceptance criteria

- [ ] `reconcile_ideas` returns a `link` row for a spec with `source_idea: IDEA-N` whose idea
      has no `:SPEC:`, and with `apply=True` writes `:SPEC:` onto the idea and then advances
      it (`done` spec -> `PROMOTED`, archived; `accepted` spec -> `SPECCED`).
- [ ] An idea with a different `:SPEC:` yields `flag-conflict` and is not modified; a terminal
      idea is never rewound; a second `apply` run is a no-op.
- [ ] `spec_ids=[...]` restricts the pass to those specs.
- [ ] After a merged campaign, `_apply` runs the scoped reconcile and records an
      `idea_reconciled` event; an exception in it becomes a `minor` `bookkeeping` finding and
      the campaign still ends `MERGED`.
- [ ] `wt spec reconcile` prints `link` rows; `stale_idea_count` counts them.
- [ ] The Test plan passes and `uv run pytest` is green.

## Test plan

- **Automated tests** (`tests/test_reconcile.py`, `tests/test_agentic_close.py`; tmp org
  fixtures already used by test_reconcile):
  - `test_source_idea_without_spec_property_links_and_advances`: done spec + INCUBATE idea
    without `:SPEC:` -> dry run lists `link` + `advance`; apply sets `:SPEC:` and the idea is
    `PROMOTED`.
  - `test_accepted_spec_links_to_specced`; `test_conflicting_spec_property_flagged_untouched`;
    `test_terminal_idea_not_rewound`; `test_reconcile_apply_idempotent`.
  - `test_spec_ids_scope`: two unlinked specs, scope to one, the other idea unchanged.
  - Queue: a merge-flow test (existing close fixtures) asserts an `idea_reconciled` event and
    the idea's new state; a second test monkeypatches `reconcile_ideas` to raise and asserts
    the `bookkeeping` finding and `MERGED` state.
  - CLI: `wt spec reconcile` output contains the `link` row (CliRunner).
- **Sabotage checks:** removing the source_idea pass fails the link tests; writing the state
  before `:SPEC:` fails the archive/link test; removing the try/except fails the
  raise-in-reconcile test; ignoring `spec_ids` fails the scope test.
- **Manual verification:** `wt spec reconcile` in the real data dir lists the ~40 stale ideas;
  `--apply` clears them; `wt idea list` no longer shows them open.
- **Regression guard:** full `uv run pytest`; existing SPEC-0078/0080 reconcile tests unchanged.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; `uv run pytest` passes; manual repair run noted.
- [ ] Spec body updated to match what shipped; ledger regenerated.

## Open questions

None.
