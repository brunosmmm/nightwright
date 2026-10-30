---
id: SPEC-0292
title: "Idea linkage: `:CAMPAIGN:` for spec campaigns, cleared on terminal states, and a hub agentic slice"
status: accepted
owner: user
created: 2026-10-30
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-041
parent: SPEC-0169
depends_on: [SPEC-0267]
tags: [agentic, ideas, hub]
---

## Context

Promoted from idea `IDEA-041` (epic SPEC-0169: "campaigns visible from ideas and the hub").
Today the idea ↔ campaign link is partial:

- `src/wt/agentic/queue.py:apply_add_idea` sets `:CAMPAIGN: <cid>` on the idea of an **idea
  campaign** (SPEC-0209). `queue._unlink_idea` clears it on abandon. It clears the property
  even when its value names another campaign.
- A **spec campaign** (`queue.apply_add`, `wt queue add SPEC-X`) never touches the spec's
  source idea. From the idea side (`wt idea show`, `wt ideas --json`, `wt hub --json`) you
  cannot tell that work is running.
- No campaign clears `:CAMPAIGN:` on MERGED or HANDED_OFF. A shipped idea keeps pointing at
  a dead campaign.
- Resolving a spec's idea safely is solved. SPEC-0267's identity rule
  (`specs._pick_idea(cands, spec_created)` over the ideas carrying the `source_idea` id, with
  `_idea_predates_spec`) refuses reused ids. `queue._reconcile_closed` is the best-effort
  post-close hook.
- `src/wt/report.py:hub_payload` (`wt hub --json`, `wt.hub.v1`) lists ideas and specs but
  nothing agentic. `report.idea_row` omits `:CAMPAIGN:`.

## Goals / Non-goals

**Goals**
- A spec campaign links its source idea (`:CAMPAIGN: <cid>`) at add time. The idea is resolved
  by the SPEC-0267 identity rule. A link is never written to a mismatched or ambiguous idea,
  and never overwrites a link held by another live campaign.
- Every campaign that reaches a terminal state (MERGED, HANDED_OFF, ABANDONED) clears
  `:CAMPAIGN:` on its idea **only if the property still names that campaign**. This holds for
  both idea and spec campaigns, best-effort.
- `wt hub --json` gains an `agentic` slice: the active campaigns, with state, cost and the
  idea each belongs to, plus a per-idea index.
- `report.idea_row` (`wt ideas --json`, the hub's `ideas`) shows `campaign` when it is set.

**Non-goals**
- A Rich (non-JSON) hub view. `wt hub` is JSON-only in v1.
- Back-filling `:CAMPAIGN:` for campaigns that are already running. The next add or terminal
  transition fixes them naturally.
- Linking merge-request campaigns (SPEC-0233), which have no spec.
- Changing which idea state a merge advances to (SPEC-0257/0280 own that).

## Decision

Add one resolver and one release helper in `queue`, and call them from the existing add,
close and abandon paths. Both are best-effort. A failure becomes an event or a finding, never
a refused add or a changed terminal state, because the link is bookkeeping, not control flow.

The hub slice reads `store.list_campaigns(cfg)` (non-terminal only). That is cheap: one
`state.json` per active campaign. The slice never makes `wt hub` fail. On any error it is
`{"error": "<text>"}`.

## Design

- `src/wt/specs.py`: new `source_idea_task(cfg, spec_fm) -> tuple[task | None, str | None]`.
  - It reads `source_idea`, collects the idea tasks whose `:ID:` (canonical case) equals it,
    and normalises `created` the way `_spec_claims` does.
  - It returns `_pick_idea(cands, created)`'s task, or `(None, reason)`, where the reason is
    "no source_idea", "idea not found", or the mismatch or ambiguity detail.
- `src/wt/agentic/queue.py`:
  - New `_link_spec_idea(cfg, state, fm) -> list[events]`, called from `apply_add` after
    `store.create_campaign` and appended to the ledger:
    - It resolves the idea through `SP.source_idea_task`.
    - It does not write when the idea is unresolved. It emits `idea_link_skipped` `{spec_id,
      reason}`, and not for "no source_idea".
    - It does not write when the idea's `:CAMPAIGN:` names a campaign that is not terminal
      (read through `store.read_state`; a missing state counts as terminal). It emits
      `idea_link_skipped` with the reason "held by <cid>".
    - Otherwise it writes `OW.set_property(cfg, task, "CAMPAIGN", cid)`, records
      `state["idea_link"] = {"id": <idea id>}` (written with the state), and emits
      `idea_linked` `{idea_id}`.
    - `ValueError` or `OSError` becomes `idea_link_skipped` with the error.
    - `plan_add` needs `fm`. It already reads it, so it passes `source_idea` and `created`
      through the plan as `plan["spec_fm"]`.
  - New `_release_idea_link(cfg, state, events)`:
    - The idea id is `_idea_id_of(state)` for an idea campaign, otherwise
      `(state.get("idea_link") or {}).get("id")`.
    - It finds the task with `_idea_task`. If `task.properties.get("CAMPAIGN") == state["id"]`,
      it calls `OW.clear_property(..., "CAMPAIGN")` and appends `idea_unlinked` `{idea_id}`.
    - Errors become a `minor` `bookkeeping` finding.
  - Call sites:
    - `_reconcile_closed` (MERGED and HANDED_OFF), at its end.
    - The abandon path: `_unlink_idea` replaces its unconditional
      `clear_property(…, "CAMPAIGN")` with the ownership check, through the helper. For a
      spec campaign, abandon calls `_release_idea_link` directly.
  - Register `idea_linked` `("idea_id",)`, `idea_link_skipped` `("spec_id", "reason")` and
    `idea_unlinked` `("idea_id",)` in `schemas.EVENT_KEYS`.
- `src/wt/report.py`:
  - `idea_row` adds `"campaign"` when `:CAMPAIGN:` is non-empty.
  - `hub_payload` adds `out["agentic"] = _hub_agentic(cfg)`:
    - `{"campaigns": [{id, kind, state, spec_id, idea_id, project, priority, cost_usd,
      cap_usd, park_reason, updated_at}], "by_idea": {idea_id: [cid, …]}, "counts":
      {state: n}}`.
    - `idea_id` is the idea campaign's idea, otherwise `idea_link.id`. A campaign with
      neither is still listed, with `idea_id: null`.
    - It is wrapped in `try/except Exception` → `{"error": str(e)}`.
  - The `wt.hub.v1` schema stays v1, because the change is additive.

## Alternatives considered

- **Resolve the idea by its `:SPEC:` property instead of `source_idea`.** Rejected. `:SPEC:` is
  set by promotion and reconcile and can lag. `source_idea` plus the SPEC-0267 identity rule is
  the vetted path.
- **Make a link failure refuse the add.** Rejected. Bookkeeping must never block work
  (SPEC-0256's precedent for `_link_idea`).
- **Keep `:CAMPAIGN:` after MERGED as history.** Rejected. The property means "the campaign
  working on this", and the ledger and `wt campaign list --all` hold the history.
- **Put the slice in a new `wt hub --agentic` flag.** Rejected. The hub is the one triage
  union, and an additive key costs readers nothing.

## Acceptance criteria

- [ ] `wt queue add SPEC-X` sets `:CAMPAIGN: <cid>` on the idea resolved from SPEC-X's
      `source_idea` by the SPEC-0267 rule, records `state.idea_link` and emits `idea_linked`.
      A mismatched, ambiguous or missing idea gets no write and an `idea_link_skipped` event,
      and the add still succeeds.
- [ ] The add never overwrites `:CAMPAIGN:` held by a live campaign (`idea_link_skipped`,
      "held by …"). A link to a terminal or missing campaign is replaced.
- [ ] On MERGED, HANDED_OFF or ABANDONED, the campaign's idea (idea or spec campaign) loses
      `:CAMPAIGN:` only when it still names this campaign, with an `idea_unlinked` event.
      Errors become a minor bookkeeping finding and never change the terminal state.
- [ ] `wt hub --json` has an `agentic` key with the active campaigns (state, cost, cap, park
      reason, idea id), `by_idea` and `counts`. A failure there yields `{"error": …}` and
      the rest of the hub output is unchanged.
- [ ] `wt ideas --json` / the hub's `ideas` rows include `campaign` when the idea has
      `:CAMPAIGN:`.

## Test plan

- `tests/test_reconcile.py`:
  - `test_source_idea_task_resolves`;
  - `test_source_idea_task_mismatch` (the idea was created after the spec);
  - `test_source_idea_task_ambiguous`;
  - `test_source_idea_task_none`.
- `tests/test_agentic_queue.py`, using a temp org ideas file and the fake runner:
  - `test_spec_add_links_idea`: the idea has `:CAMPAIGN:` and `state.idea_link`, and there is
    an `idea_linked` event.
  - `test_spec_add_mismatch_no_write`: the idea is created after the spec. There is no
    property and there is `idea_link_skipped`.
  - `test_spec_add_does_not_steal_live_link`: an idea campaign holds the idea. The spec add
    leaves it, with the reason "held by".
  - `test_spec_add_replaces_dead_link`: the property names an ABANDONED campaign, so it is
    replaced.
  - `test_merged_clears_own_link`: drive a spec campaign to MERGED. The property is gone and
    there is `idea_unlinked`.
  - `test_terminal_keeps_foreign_link`: the property was changed by hand to another id before
    the merge, so it is kept.
  - `test_abandon_idea_campaign_ownership_check`: after an idea-campaign abandon where the
    property names a different campaign, it is kept. This is the changed behaviour of
    `_unlink_idea`.
  - `test_link_events_validate` (`schemas`).
- `tests/test_hub.py`:
  - `test_hub_agentic_slice`: two active campaigns, one of them parked. The `campaigns` rows,
    `by_idea` and `counts` are right.
  - `test_hub_agentic_error_isolated`: `store.list_campaigns` is monkeypatched to raise. The
    result is `agentic == {"error": …}` and `ideas` is still present.
  - `test_idea_row_campaign_field`.
- **Sabotage check:**
  - Remove the ownership check in `_release_idea_link`.
    `test_terminal_keeps_foreign_link` must fail.
  - Bypass `_pick_idea` (link by id only). `test_spec_add_mismatch_no_write` must fail.
- **Regression guard:** full `uv run pytest`. The existing idea-campaign add, abandon and hub
  tests pass unchanged, except the one asserting the unconditional clear, if any. That test is
  updated to the ownership rule, and the change is noted in As shipped.

## Rollout / migration

Additive. Spec campaigns that are already running have no `idea_link`. Their terminal
transition finds no idea id and does nothing.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
