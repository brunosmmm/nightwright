---
id: SPEC-0327
title: "Spec ids are per project: a second project's SPEC-NNNN never resolves against wt's own specs"
status: proposed
owner: user
created: 2026-10-31
source_idea: IDEA-533
kind: feature
parent: SPEC-0298
milestone: "M10: agentic MVP"
tags: [agentic, queue, reconcile, spec-ids, second-project, mvp]
---

## Context

**IDEA-533 (bug).** On 2026-10-31 the first second-project campaign
(`IDEA-008-20261031-7de5`, project comfort-assistant, merge policy `pr`) wrote and accepted
its spec as **SPEC-0001 in the comfort-assistant repo**. wt's own `docs/specs/0001-package-refactor-and-xdg-layout.md`
also exists. Afterwards the wt **main checkout** had an uncommitted edit adding
`source_idea: IDEA-008` to wt's SPEC-0001. The steward reverted it before committing 7fa9e3f.
Besides being wrong, such an edit makes the main checkout dirty, which blocks queue closes
(`main_dirty`).

**Root cause.** Spec ids are only unique inside one repo, but most of wt resolves a bare
`SPEC-NNNN` against the one registered store (`specs._specs_dir(cfg)`, wt's own
`docs/specs`) and matches ideas to specs by that bare string. Three families of lookups do it:

1. **Idea reconcile.** `specs.reconcile_ideas` (`src/wt/specs.py`) walks every idea with a
   `:SPEC:` property and loads the spec through `workflow._load_spec_fm` → `_find_spec(_specs_dir(cfg), id)`.
   An idea of another project whose `:SPEC:` is `SPEC-0001` therefore reads wt's SPEC-0001,
   finds no `source_idea`, treats the idea as its "sole claimant" and, in the write pass
   (`backlinks=True`, the default of `wt idea reconcile --apply`), calls
   `_write_source_idea(cfg, "SPEC-0001", "IDEA-008")` on wt's file. This is the best match
   for the observed edit: it is the only code that writes only `source_idea` into a spec.
   The SPEC-0267 identity check (`_pick_idea`) is not applied on the backlink path, which is
   why wt's much older SPEC-0001 accepted a newer idea. SPEC-0325 fixed the *close* hook
   (`_reconcile_project_spec`) but not the global walk or the other lookups below.
2. **Idea-side bookkeeping keyed by a bare id.** `_mark_handed_off`,
   `_clear_handed_off_on_requeue` (`src/wt/agentic/queue.py`) and `specs.plan_spec_link` pick the idea
   with `_sole_claimant(ideas, spec_id)` or by bare id, so two projects' ideas that both link
   `SPEC-0001` collide (zero or two claimants: silently skipped, or the wrong idea is
   marked). `store.active_campaign_for_spec(cfg, spec_id)` has the same flaw: a second
   project's `SPEC-0001` is refused as "already has an active campaign" when wt's SPEC-0001 is
   active.
3. **Commands run inside a campaign.** A persona (or the conductor) running `wt spec status
   SPEC-0001 --set …`, `wt spec verify`, `wt spec review` etc. in a comfort-assistant
   worktree resolves the id through `resolve_spec_path` / `resolve_spec_path_for_cwd`, which
   fall back to wt's store whenever the cwd is not a checkout of wt's own repository. The write
   lands in wt's live main checkout.

Nothing records which project a bare id belongs to, so every one of these has to guess.

## Goals / Non-goals

**Goals**
- Every spec lookup made on behalf of a campaign or of a non-wt idea is scoped by project:
  the campaign's project repo (`state.repo.root` + `state.spec.rel_path`, or the worktree
  copy), never a bare id against wt's store.
- No wt command writes into wt's `docs/specs` for an id that belongs to another project,
  whichever way it was invoked (queue step, `wt idea …`, `wt spec …` from a campaign worktree).
- Two projects that both have a `SPEC-0001` coexist: queueing, reconciling, hand-off marks and
  active-campaign checks of one never touch the other.
- A refused cross-project resolution is visible (an error or a report-only row naming both
  projects), not silent.

**Non-goals**
- No renumbering of any project's specs, and no change to the id format (`SPEC-NNNN`).
- No new storage of a "project" on the spec file. The project is derived from data wt has
  already: the idea's `:PROJECT:` property, the campaign state's `project`, and the cwd.
- Outbound `PROJ-NNNN` ids are unchanged; they are already unique across the outbox.
- No change to `_reconcile_project_spec` (SPEC-0325), beyond reusing the new helpers if useful.

## Decision

Spec identity inside wt becomes **(project, spec id)**. A bare `SPEC-NNNN` is only ever
resolved against wt's store when its owner is wt's own project (`rules.INTERNAL_PROJECT`,
`Meta-Tools`). Three rules, one helper module:

1. **Ideas are scoped by `:PROJECT:`.** An idea whose `:PROJECT:` is empty or
   `INTERNAL_PROJECT` is internal; any other is **foreign**. Idea↔spec matching against wt's
   store (`reconcile_ideas`, `_spec_claims`, `plan_spec_link`, `_sole_claimant`) only
   considers internal ideas. A foreign idea with a `:SPEC:` yields a report-only
   `flag-foreign` row and is never advanced or backlinked through wt's store (its reconcile
   is the campaign close path, `_reconcile_project_spec`, which reads the project's own repo).
2. **Writing `source_idea` is guarded twice.** `_write_source_idea` refuses a foreign idea and
   also applies the SPEC-0267 identity rule (idea `:CREATED:` on or before the spec's `created`),
   so even a misrouted call cannot stamp a newer idea onto an older spec.
3. **Inside a campaign, ids resolve through the campaign.** When `WT_CAMPAIGN` is set (persona
   runs) or the cwd is a checkout of a registered non-wt project, a bare `SPEC-NNNN` is
   resolved by one function that returns the campaign's own spec file, or fails. It never
   falls back to wt's store for a non-wt campaign.

Campaign/queue lookups keyed by a bare spec id (`active_campaign_for_spec`, `_sole_claimant`
users) gain a `project` argument and pass the campaign's project.

## Design

**Helpers (new module `src/wt/spec_scope.py`, so the shared CLI/queue modules only gain
one-line calls).**

- `idea_project(task) -> str`: `task.properties["PROJECT"]` stripped, else `INTERNAL_PROJECT`.
- `is_internal_idea(task) -> bool`.
- `campaign_spec_path(cfg, state) -> Path`: `Path(state.repo.worktree) / state.spec.rel_path`
  when the worktree exists, else `Path(state.repo.root) / state.spec.rel_path`; raises
  `ValueError` if neither file exists.
- `resolve_in_scope(cfg, spec_id, *, campaign=None, cwd=None) -> Path`: the one resolver for
  commands that take a bare id.
  1. `campaign` defaults to `$WT_CAMPAIGN`. If it names a campaign whose `project` is not
     `INTERNAL_PROJECT`: the id must equal the campaign's `spec_id` (case-insensitive),
     return `campaign_spec_path`; any other bare `SPEC-NNNN` raises
     `ValueError("SPEC-NNNN is ambiguous in a <project> campaign: it would resolve against wt's own specs; use --path")`.
  2. Otherwise, if `cwd` (default `Path.cwd()`) is inside a project registered in the project
     registry other than `INTERNAL_PROJECT` (`projects.find_project_for_cwd`), look the id up
     in that project's own specs directory (`<repo root>/<spec dir of the project>`, found by
     globbing `**/specs/NNNN-*.md` only under the project's configured spec dir; the
     project-config key already used for `spec_rel_path`'s parent), and fail with the same
     kind of message when absent. wt's store is not consulted.
  3. Otherwise today's behaviour (`resolve_spec_path`).

**Call sites changed.**

- `specs.resolve_spec_path_for_cwd` and `cli.spec_status_cmd` (and the other `spec`
  subcommands that call `resolve_spec_path`: `verify`, `show`, `review`, `export`'s internal
  branch is unaffected) call `resolve_in_scope` first. `workflow._resolve_spec_path` and
  `_load_spec_fm` stay store-only (they are the store lookup), but their callers in
  `reconcile_ideas`/`plan_spec_link` filter by idea project before calling them.
- `specs.reconcile_ideas`: drop foreign ideas from `ideas`/`work`/`by_id`/claims walks; for each
  dropped foreign idea with a `:SPEC:` append `(idea_id, spec_id, "flag-foreign", "idea belongs to <project>; its spec lives in that repo")`
  once. `_spec_claims` skips a wt spec whose `source_idea` names a foreign idea and reports it
  as `flag-foreign` too (a wt spec naming a foreign idea is a mis-link for a human to fix).
  `wt idea reconcile` prints `flag-foreign` rows as report-only, like `flag-mismatch`.
- `specs._write_source_idea(cfg, spec_id, idea_id)`: look the idea up; raise `ValueError` if
  it is foreign or fails the SPEC-0267 created-date rule; `reconcile_ideas` records the
  refusal as `flag-mismatch` instead of aborting the pass.
- `specs.plan_spec_link`: a foreign idea raises `ValueError("<IDEA> belongs to <project>; its
  spec ids are not in wt's store. The queue links it when its campaign merges")`.
- `specs._sole_claimant(ideas, spec_id, project=None)`: when `project` is given, only ideas
  with `idea_project(t) == project` count. `queue._mark_handed_off` and
  `queue._clear_handed_off_on_requeue` pass `state["project"]` / the admitted campaign's
  project.
- `store.active_campaign_for_spec(cfg, spec_id, project=None)`: forwards `project` to
  `list_campaigns` (which already filters on it). `queue.plan_add` passes the resolved
  `project`; other callers that have a campaign state pass its project. Callers with no project
  keep today's behaviour.
- `queue._reconcile_closed` keeps its SPEC-0325 split; the internal branch now also
  passes `backlinks=False` (unchanged) and its scoped `spec_ids=[spec_id]` implies the
  campaign's idea is internal, which the new idea filter makes explicit.

**Observability.** A refused cross-project resolution inside a queue step is a `finding`
(`gate: bookkeeping`, severity `minor`) naming the id and the two projects. Command-line
refusals are `click.ClickException`s with the same text.

**No data migration.** Existing `:SPEC:` values on foreign ideas stay as they are (they
are correct within their project). If wt's SPEC-0001 already carries a wrong `source_idea`, that
is repaired by hand once (the steward already reverted the observed edit).

## Alternatives considered

- **Namespace the id (`comfort-assistant/SPEC-0001`, or a `project:` field on the spec).** Would be
  unambiguous but changes the id format, the lint, every spec reference and the ledger. The
  project is already derivable from the idea and campaign records, so it was not worth it.
- **Only guard `_write_source_idea`.** Stops the observed edit, but leaves the wrong idea
  being advanced or hand-off-marked from the wrong spec, and the false "active campaign"
  refusal. It is kept as the second line of defence (rule 2) rather than the fix.
- **Make all second-project specs use a `P-NNNN` prefix like outbound ids.** The comfort-assistant
  repo already owns `SPEC-0001`; renumbering a live project to avoid a tooling bug is backwards.

## Acceptance criteria

- [ ] With two projects that both have `docs/specs/0001-*.md`, an idea whose `:PROJECT:` is the
      second project and `:SPEC: SPEC-0001`, `wt idea reconcile --apply` leaves wt's SPEC-0001
      file byte-identical and reports a `flag-foreign` row for that idea.
- [ ] `specs._write_source_idea` raises for a foreign idea, and for an idea created after the spec's
      `created` date; neither modifies the file.
- [ ] `plan_spec_link` refuses a foreign idea with a message naming its project.
- [ ] `_sole_claimant` / `_mark_handed_off` / `_clear_handed_off_on_requeue` mark only the idea of
      the campaign's project when an internal and a foreign idea both link `SPEC-0001`.
- [ ] `store.active_campaign_for_spec(cfg, "SPEC-0001", project=X)` ignores an active
      `SPEC-0001` campaign of another project, and `plan_add` for the second project's
      `SPEC-0001` is not refused because wt's `SPEC-0001` has an active campaign (and vice versa).
- [ ] With `WT_CAMPAIGN` set to a non-wt campaign, `wt spec status SPEC-0001 --set <status>` edits the
      campaign's spec copy and not wt's `docs/specs/0001-*.md`; with another bare id such as
      `SPEC-0002` it fails with the "ambiguous in a <project> campaign" error and writes nothing.
- [ ] From a cwd inside a registered non-wt project, a bare id never resolves to wt's store.
- [ ] Closing (merging) a second-project campaign whose spec id equals an existing wt spec id
      leaves `git status --porcelain` of the wt checkout empty and its idea reconciled via the project repo.
- [ ] Internal behaviour is unchanged: the pre-existing reconcile, queue, spec CLI and project tests pass.

## Test plan

- **Automated tests (new `tests/test_spec_scope.py`, offline, temp dirs and git repos):**
  fixture builds a wt-like `specs_dir` with `0001-wt.md` (created 2026-01-01, no
  `source_idea`), a second project repo registered in the project config with its own
  `docs/specs/0001-proj.md` (created 2026-10-31), an org file with `IDEA-008`
  (`:PROJECT: comfort-assistant`, `:SPEC: SPEC-0001`, created 2026-10-31) and an internal idea
  `IDEA-001` also linking `SPEC-0001`. Tests:
  - `test_reconcile_never_writes_foreign_backlink`: `reconcile_ideas(cfg, apply=True)` with the
    default `backlinks=True`; assert wt `0001` bytes unchanged, a `flag-foreign` row for IDEA-008,
    IDEA-001 handled as before.
  - `test_write_source_idea_refuses_foreign_and_newer_idea`.
  - `test_plan_spec_link_refuses_foreign_idea`.
  - `test_mark_and_clear_handed_off_are_project_scoped`: with both ideas linking `SPEC-0001`,
    `_mark_handed_off` for a comfort-assistant state marks only IDEA-008.
  - `test_active_campaign_for_spec_is_project_scoped` and `test_plan_add_same_spec_id_two_projects`.
  - `test_resolve_in_scope_campaign_env` (monkeypatch `WT_CAMPAIGN`, a campaign state under
    `store.campaigns_dir`): returns the project copy; other bare id raises; no `WT_CAMPAIGN` and
    cwd in wt returns wt's file.
  - `test_spec_status_set_in_campaign_leaves_wt_spec_untouched` via the click runner.
  - `test_cwd_in_foreign_project_does_not_fall_back_to_wt_store`.
  - `test_second_project_close_leaves_wt_checkout_clean`: extend the fake-runner second-project fixture in
    `tests/test_agentic_project.py` (campaign spec `SPEC-0001`, wt `SPEC-0001` present) to merge and
    assert `git status --porcelain` of the wt temp checkout is empty and `idea_reconciled` was emitted.
- **Manual verification:** in a scratch copy of the config, `wt idea reconcile` (dry run, then
  `--apply`) with the real `IDEA-008` still linked to comfort-assistant's `SPEC-0001`; `git -C <wt
  main checkout> status --porcelain` stays empty. Run `WT_CAMPAIGN=<cid> wt spec status SPEC-0001`
  inside a comfort-assistant worktree and confirm the printed path is the worktree's spec.
- **Regression guard:** full `uv run pytest` (notably `tests/test_reconcile.py`,
  `tests/test_agentic_project.py`, `tests/test_projects_*.py`), `python3 tools/spec_lint.py`.

## Rollout / migration

1. Land `spec_scope.py`, the `reconcile_ideas`/`_write_source_idea`/`plan_spec_link` guards and the
   `project` arguments in one change; they are backwards compatible (no argument = old behaviour).
2. Switch the queue/CLI call sites to pass the project / use `resolve_in_scope`.
3. Run the manual verification before the E5 canary's next second-project merge.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The reconcile backlink is the best-fit cause of the observed edit, not a proven one;
  the fix covers every bare-id lookup listed above, so the result does not depend on which one it was.
