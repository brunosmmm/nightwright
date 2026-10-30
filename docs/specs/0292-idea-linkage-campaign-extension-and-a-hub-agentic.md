---
id: SPEC-0292
title: "Idea linkage: `:CAMPAIGN:` for spec campaigns, cleared on terminal states, and a hub agentic slice"
status: accepted
owner: user
created: 2026-10-30
updated: 2026-10-30
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
  cannot tell that work is running. (`wt idea show`'s `wt.idea.v1` payload does not carry
  `:CAMPAIGN:` today; see Non-goals.)
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
- Adding `campaign` to `wt idea show` (`explore.idea_show_payload`). Its payload whitelists
  properties and omits `:CAMPAIGN:`; the JSON rows (`wt ideas --json`, hub) are the v1
  visibility surface, and `idea show` can follow in a later spec.
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
    `store.create_campaign` (which persists the state once, under `add_lock`):
    - Persistence: the link is resolved after the campaign exists, so `state["idea_link"]` is
      a second write under `store.item_lock(cfg, cid)` (re-read the state, set the key, write
      it back), and the events go through `store.append_event`. Resolving before
      `create_campaign` is rejected: if `create_campaign` then raised, the org property
      would be orphaned. If the second write fails, the property is already set; the failure
      becomes `idea_link_skipped` with the error and the terminal release (which needs
      `idea_link`) will not find the idea. Release therefore also falls back to the idea
      whose `:CAMPAIGN:` equals the cid, see `_release_idea_link`.
    - It resolves the idea through `SP.source_idea_task`.
    - It does not write when the idea is unresolved. It emits `idea_link_skipped` `{spec_id,
      reason}`, and not for "no source_idea".
    - It does not write when the idea's `:CAMPAIGN:` names a campaign that is not terminal
      (read through `store.read_state`; a missing state counts as terminal). It emits
      `idea_link_skipped` with the reason "held by <cid>".
    - Otherwise it writes `OW.set_property(cfg, task, "CAMPAIGN", cid)`, records
      `state["idea_link"] = {"id": <idea id>}` (written with the state), and emits
      `idea_linked` `{idea_id}`.
    - The whole body after `create_campaign` (resolution, `store.read_state`, the property
      write, the state write) runs in `try/except Exception:  # noqa: BLE001`, as
      `_clear_handed_off_on_requeue` does. Any error becomes `idea_link_skipped` with the
      error text, so a created campaign is never reported as a failed add.
    - `plan_add` needs `fm`. It already reads it, so it passes `source_idea` and `created`
      through the plan as `plan["spec_fm"]`.
  - New `_release_idea_link(cfg, state, events)`:
    - It does not look the idea up by id, because `_idea_task` returns the first idea carrying
      an id and rotated files can hold two (SPEC-0267). Instead it scans the idea tasks and
      handles **every** task whose `task.properties.get("CAMPAIGN") == state["id"]`, calling
      `OW.clear_property(..., "CAMPAIGN")` on each and appending `idea_unlinked` `{idea_id}`.
      The ownership check is the equality itself, so a foreign value is never cleared, and a
      twin idea with the same `:ID:` is handled correctly. This also covers a campaign whose
      `idea_link` was never persisted.
    - Each clear is isolated: an error becomes a `minor` `bookkeeping` finding (message names
      the idea id) and the loop continues. The whole helper is wrapped, so it never raises.
    - It never touches the campaign's terminal state.
  - Call sites. The release must not depend on the reconcile's early returns:
    `_reconcile_closed` returns early with no `spec_id`, and with an info finding when the
    spec store is not inside `state.repo.root` (outbound or other-project spec campaigns).
    - MERGED and HANDED_OFF: `_release_idea_link` is called from the transition site, next to
      the `_reconcile_closed` call, unconditionally and before/independent of it, with its
      own error isolation. It is not placed inside `_reconcile_closed`.
    - The abandon path: `_unlink_idea` replaces its unconditional
      `clear_property(…, "CAMPAIGN")` with a call to the helper (ownership check). For a
      spec campaign, abandon calls `_release_idea_link` directly.
  - Register `idea_linked` `("idea_id",)`, `idea_link_skipped` `("spec_id", "reason")` and
    `idea_unlinked` `("idea_id",)` in `schemas.EVENT_KEYS`.
- `src/wt/report.py`:
  - `idea_row` adds `"campaign"` when `:CAMPAIGN:` is non-empty.
  - `hub_payload` adds `out["agentic"] = _hub_agentic(cfg, project=project)`. Only `project`
      is honoured (passed to `store.list_campaigns(project=…)`, which supports it). The idea
      filters `state`, `kind`, `tag`, `workstream`, `priority` do not apply: they describe
      ideas/tasks, not campaign states. With no `project`, every active campaign is listed.
      The slice:
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
      Errors become a minor bookkeeping finding and never change the terminal state. The
      release runs even when `_reconcile_closed` returns early (no `spec_id`, or a spec store
      outside the campaign's repo), and clears every idea carrying this campaign's id, so a
      reused `:ID:` twin cannot hide the linked idea.
- [ ] A failure anywhere in `_link_spec_idea` (including non-`ValueError`/`OSError`) leaves the
      add successful, with the campaign created and an `idea_link_skipped` event.
- [ ] `wt hub --json` has an `agentic` key with the active campaigns (state, cost, cap, park
      reason, idea id), `by_idea` and `counts`. A failure there yields `{"error": …}` and
      the rest of the hub output is unchanged. `wt hub --json --project P` lists only
      project P's campaigns in `agentic`; other idea filters do not affect it.
- [ ] `wt ideas --json` / the hub's `ideas` rows include `campaign` when the idea has
      `:CAMPAIGN:`.

## Test plan

- `tests/test_reconcile.py`:
  - `test_source_idea_task_resolves`;
  - `test_source_idea_task_mismatch` (the idea was created after the spec);
  - `test_source_idea_task_ambiguous`;
  - `test_source_idea_task_none`.
- `tests/test_agentic_queue.py`, using a temp org ideas file and the fake runner:
  - `test_spec_add_links_idea`: the idea has `:CAMPAIGN:` and the persisted `state.idea_link`
    (re-read from disk, proving the second write), and there is an `idea_linked` event.
  - `test_spec_add_mismatch_no_write`: the idea is created after the spec. There is no
    property and there is `idea_link_skipped`.
  - `test_spec_add_does_not_steal_live_link`: an idea campaign holds the idea. The spec add
    leaves it, with the reason "held by".
  - `test_spec_add_replaces_dead_link`: the property names an ABANDONED campaign, so it is
    replaced.
  - `test_merged_clears_own_link`: drive a spec campaign to MERGED. The property is gone and
    there is `idea_unlinked`.
  - `test_handed_off_clears_own_link`: drive a spec campaign to HANDED_OFF. The property is
    gone and there is `idea_unlinked`.
  - `test_terminal_keeps_foreign_link`: the property was changed by hand to another id before
    the merge, so it is kept.
  - `test_release_runs_when_reconcile_skips`: regression guard for the early returns. A
    campaign whose spec store is outside `state.repo.root` (outbound branch), and one with no
    `spec_id`, each go MERGED. `_reconcile_closed` returns early, yet the property is cleared.
  - `test_release_clears_reused_id_twin`: two ideas carry the same `:ID:`, the twin first in
    file order, and the linked one has `:CAMPAIGN: <cid>`. After MERGED the linked one is
    cleared and the twin is untouched.
  - `test_abandon_spec_campaign_releases_link`: abandon a spec campaign that linked its idea.
    The property is gone and there is `idea_unlinked`.
  - `test_release_error_is_bookkeeping_finding`: `OW.clear_property` is monkeypatched to
    raise. A minor `bookkeeping` finding is recorded and the state is still MERGED (and, in a
    second case, ABANDONED).
  - `test_link_failure_never_refuses_add`: `SP.source_idea_task` raises a `RuntimeError`.
    `apply_add` still succeeds, the campaign exists, and there is `idea_link_skipped`.
  - `test_abandon_idea_campaign_ownership_check`: after an idea-campaign abandon where the
    property names a different campaign, it is kept. This is the changed behaviour of
    `_unlink_idea`.
  - `test_link_events_validate` (`schemas`).
- `tests/test_hub.py`:
  - `test_hub_agentic_slice`: two active campaigns in different projects, one of them parked.
    The `campaigns` rows, `by_idea` and `counts` are right. With `project=` set, only that
    project's campaign is listed, and passing `tag`/`state` idea filters leaves `agentic`
    unchanged.
  - `test_hub_agentic_error_isolated`: `store.list_campaigns` is monkeypatched to raise. The
    result is `agentic == {"error": …}` and `ideas` is still present.
  - `test_idea_row_campaign_field`.
- **Sabotage check:**
  - Remove the ownership check in `_release_idea_link`.
    `test_terminal_keeps_foreign_link` must fail.
  - Move the release call back inside `_reconcile_closed`.
    `test_release_runs_when_reconcile_skips` must fail.
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
