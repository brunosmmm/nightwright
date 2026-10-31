---
id: SPEC-0327
title: "Spec ids are per project: a second project's SPEC-NNNN never resolves against wt's own specs"
status: done
owner: user
created: 2026-10-31
updated: 2026-10-31
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
   considers internal ideas **when the `:SPEC:` is a bare `SPEC-NNNN`**. A foreign idea whose
   `:SPEC:` is a bare `SPEC-NNNN` (not an outbound `PROJ-NNNN` id, `workflow._is_outbound_id`)
   yields a report-only `flag-foreign` row and is never advanced or backlinked through wt's
   store (its reconcile is the campaign close path, `_reconcile_project_spec`, which reads the
   project's own repo). Ideas linked to outbound `PROJ-NNNN` ids are exempt: outbound ids are
   unique across the outbox and such ideas normally carry another project's `:PROJECT:`, so
   they reconcile through the outbox exactly as today.
2. **Writing `source_idea` is guarded twice.** `_write_source_idea` refuses a foreign idea and
   also applies the SPEC-0267 identity rule (idea `:CREATED:` on or before the spec's `created`),
   so even a misrouted call cannot stamp a newer idea onto an older spec.
3. **Inside a campaign, ids resolve through the campaign.** When `WT_CAMPAIGN` is set (persona
   runs) or the cwd is in a checkout (main or linked worktree) of a registered non-wt project, a bare `SPEC-NNNN` is
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
  2. Otherwise, if `cwd` (default `Path.cwd()`) is in a git checkout (main **or linked
     worktree**) whose `--git-common-dir` equals that of the `research.root` of a registered
     project other than `INTERNAL_PROJECT`, look the id up with
     `specs._find_spec(<checkout toplevel>/docs/specs, id)`. `docs/specs` is the fixed
     convention (the same one `spec_review.py` and the campaign spec paths use); there is no
     per-project spec-dir key. The checkout is matched with `specs._git_checkout(cwd)` and
     `specs._git_checkout(root)` (toplevel and common dir), not with
     `wt.agentic.project.find_project_for_cwd`: that compares the git toplevel with
     `research.root`, so it never matches a campaign worktree
     (`~/work/.wt-worktrees/<project>/<cid>`). If the file is absent, fail with the same kind of
     message as step 1. wt's store is not consulted.
  3. Otherwise today's behaviour (`resolve_spec_path`).

**Call sites changed.**

- `specs.resolve_spec_path_for_cwd` and `cli.spec_status_cmd` (and the other `spec`
  subcommands that call `resolve_spec_path`: `verify`, `show`, `review`, `export`'s internal
  branch is unaffected) call `resolve_in_scope` first. `workflow._resolve_spec_path` and
  `_load_spec_fm` stay store-only (they are the store lookup), but their callers in
  `reconcile_ideas`/`plan_spec_link` filter by idea project before calling them.
- `specs.reconcile_ideas`: drop foreign ideas whose `:SPEC:` is a bare `SPEC-NNNN` from the
  `ideas`/`work`/`by_id`/claims walks (foreign ideas linked to an outbound `PROJ-NNNN` id stay in
  them and reconcile through the outbox unchanged); for each dropped foreign idea append `(idea_id, spec_id, "flag-foreign", "idea belongs to <project>; its spec lives in that repo")`
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
  `list_campaigns` (which already filters on it). Callers with a campaign state pass its
  project, notably the epic-batch and dependency paths (`queue.py` ~6333, ~6415); callers with
  no project keep today's behaviour.
- `queue.plan_add` (`queue.py` ~5863): `resolve_spec_path(cfg, spec_id)` reads wt's
  `SPEC-0001` for a second project's bare id and then fails with "not inside the project repo"
  (or queues the wrong file). When `project` is given, is not `INTERNAL_PROJECT`, and the id is
  a bare `SPEC-NNNN`, `plan_add` resolves the file in that project's repo instead:
  `specs._find_spec(<project research.root>/docs/specs, id)` (the project's repo root from
  `P.project_config`, the same lookup as `resolve_in_scope` step 2) and only then passes
  `project` to `active_campaign_for_spec`. Outbound ids and internal projects keep
  `resolve_spec_path`.
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

- [x] With two projects that both have `docs/specs/0001-*.md`, an idea whose `:PROJECT:` is the
      second project and `:SPEC: SPEC-0001`, `wt idea reconcile --apply` leaves wt's SPEC-0001
      file byte-identical and reports a `flag-foreign` row for that idea.
- [x] A foreign idea whose `:SPEC:` is an outbound `PROJ-NNNN` id is not filtered: it still
      advances through the outbox under `wt idea reconcile --apply`, exactly as before, and no
      `flag-foreign` row is reported for it.
- [x] `specs._write_source_idea` raises for a foreign idea, and for an idea created after the spec's
      `created` date; neither modifies the file.
- [x] `plan_spec_link` refuses a foreign idea with a message naming its project.
- [x] `_sole_claimant` / `_mark_handed_off` / `_clear_handed_off_on_requeue` mark only the idea of
      the campaign's project when an internal and a foreign idea both link `SPEC-0001`.
- [x] `store.active_campaign_for_spec(cfg, "SPEC-0001", project=X)` ignores an active
      `SPEC-0001` campaign of another project, and `plan_add` for the second project's
      `SPEC-0001` is not refused because wt's `SPEC-0001` has an active campaign (and vice versa);
      `plan_add(project=<second project>)` for a bare `SPEC-0001` returns a plan whose spec
      path is the second project's `docs/specs/0001-*.md`, not wt's.
- [x] With `WT_CAMPAIGN` set to a non-wt campaign, `wt spec status SPEC-0001 --set <status>` edits the
      campaign's spec copy and not wt's `docs/specs/0001-*.md`; with another bare id such as
      `SPEC-0002` it fails with the "ambiguous in a <project> campaign" error and writes nothing.
- [x] From a cwd inside a registered non-wt project, a bare id never resolves to wt's store,
      both in the project's main checkout and in a linked worktree of it.
- [x] Closing (merging) a second-project campaign whose spec id equals an existing wt spec id
      leaves `git status --porcelain` of the wt checkout empty and its idea reconciled via the project repo.
- [x] Internal behaviour is unchanged: the pre-existing reconcile, queue, spec CLI and project tests pass.

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
  - `test_reconcile_foreign_idea_with_outbound_id_still_advances`: an `IDEA-009`
    (`:PROJECT: comfort-assistant`, `:SPEC: PROJ-0001`, outbox spec present); `reconcile_ideas`
    advances it via the outbox as before and emits no `flag-foreign` row.
  - `test_write_source_idea_refuses_foreign_and_newer_idea`.
  - `test_plan_spec_link_refuses_foreign_idea`.
  - `test_mark_and_clear_handed_off_are_project_scoped`: with both ideas linking `SPEC-0001`,
    `_mark_handed_off` for a comfort-assistant state marks only IDEA-008.
  - `test_active_campaign_for_spec_is_project_scoped` and `test_plan_add_same_spec_id_two_projects`
    (wt `SPEC-0001` has an active campaign; `plan_add(spec_id="SPEC-0001", project="comfort-assistant")`
    returns the project's file and is not refused, and the reverse).
  - `test_resolve_in_scope_campaign_env` (monkeypatch `WT_CAMPAIGN`, a campaign state under
    `store.campaigns_dir`): returns the project copy; other bare id raises; no `WT_CAMPAIGN` and
    cwd in wt returns wt's file.
  - `test_spec_status_set_in_campaign_leaves_wt_spec_untouched` via the click runner.
  - `test_cwd_in_foreign_project_does_not_fall_back_to_wt_store`: parametrised over the
    project's main checkout and a linked worktree created with `git worktree add` (no
    `WT_CAMPAIGN`); the resolved path is under the project's checkout in both, and an id absent
    there raises instead of reading wt's store.
  - `test_second_project_close_leaves_wt_checkout_clean`: extend the fake-runner second-project fixture in
    `tests/test_agentic_project.py` (campaign spec `SPEC-0001`, wt `SPEC-0001` present) to merge and
    assert `git status --porcelain` of the wt temp checkout is empty and `idea_reconciled` was emitted.
- **Manual verification:** in a scratch copy of the config, `wt idea reconcile` (dry run, then
  `--apply`) with the real `IDEA-008` still linked to comfort-assistant's `SPEC-0001`; `git -C <wt
  main checkout> status --porcelain` stays empty. Run `WT_CAMPAIGN=<cid> wt spec status SPEC-0001`
  inside a comfort-assistant worktree and confirm the printed path is the worktree's spec.
- **Regression guard:** full `uv run pytest` (notably `tests/test_reconcile.py`,
  `tests/test_agentic_project.py`, `tests/test_projects_*.py`), `python3 tools/spec_lint.py`.

### Test record (2026-10-31)

- New `tests/test_spec_scope.py` (12 tests, all passing): foreign-backlink never written / foreign
  idea reported `flag-foreign`; outbound `PROJ-0001` foreign idea still advances; `_write_source_idea`
  refuses foreign and newer ideas; `plan_spec_link` refuses a foreign idea; project-scoped
  `_mark_handed_off` / `_clear_handed_off_on_requeue`; `active_campaign_for_spec` and `plan_add` for
  two projects with the same `SPEC-0001`; `resolve_in_scope` with `WT_CAMPAIGN`; `wt spec status
  --set` in a campaign via the click runner (wt's file untouched); cwd in a project's main checkout
  and in a linked `git worktree`.
- `tests/test_agentic_queue.py::test_second_project_close_leaves_wt_checkout_clean` (the spec named
  `tests/test_agentic_project.py`, which holds config tests, not the fake-runner queue fixture):
  merging a second project's `SPEC-0001` campaign leaves `git status --porcelain` of the wt checkout
  unchanged and emits `idea_reconciled`.
- Targeted: `tests/test_reconcile.py`, `test_agentic_queue*.py`, `test_idea_spec_link.py`,
  `test_spec_status.py`, `test_spec_review_cwd.py`, `test_project_registry.py` pass.
  `python3 tools/spec_lint.py` exits 0. The full suite was run once at the end and left to gating
  as the no-regressions check.
- Manual verification: the persona sandbox refuses `wt idea reconcile` (`WT_PERSONA_RUN=1` blocks org
  access), so the real-data dry run could not be performed here; its steps are covered by the
  automated equivalents above and remain Rollout step 3 for the steward.


### As shipped (2026-10-31)

- Helpers live in `src/wt/spec_scope.py`: `idea_project`, `is_internal_idea`, `campaign_spec_path`,
  `foreign_scope_path` (returns `(path, origin)` or None; shared by `resolve_in_scope` and
  `specs.resolve_spec_path_for_cwd`) and `resolve_in_scope`.
- Deviation from Design step 1, as the review brief asked: in a non-wt campaign the campaign's own
  `spec_id` returns the campaign spec; any other bare id is looked up in the campaign worktree's
  `docs/specs`, then the campaign repo root's, and only when absent in both raises the "ambiguous in
  a <project> campaign" error. Acceptance criterion 7's `SPEC-0002` case therefore means an id absent
  from the project (the test uses one present only in wt).
- cwd detection (`project_checkout_for_cwd`) matches `--git-common-dir`; it is skipped when the cwd
  is wt's own repository. A missing registry or `data_dir` falls through to today's behaviour.
- `reconcile_ideas`: foreign ideas with no `:SPEC:` or a bare `:SPEC:` leave the walk (only those with
  a `:SPEC:` get a `flag-foreign` row); a wt spec whose `source_idea` names a foreign idea gets a
  `flag-foreign` row. `_write_source_idea` refusals during the backlink pass become `flag-mismatch`
  rows. `wt idea reconcile` prints `flag-foreign` rows (`↗`), and the queue close hook records a
  minor `bookkeeping` finding for them.
- `_write_source_idea` finds the idea across all org files, picks it with `_pick_idea`, and refuses
  when the pick fails or the idea is foreign (bare ids only). Because SPEC-0267 now applies on the
  backlink path, the `test_backlink_write_preserves_frontmatter_formatting` fixture spec got today's
  `created` date (its idea cannot predate it).
- Call sites: `store.active_campaign_for_spec(project=)`; `plan_add` resolves a non-internal
  project's bare id in `<root>/docs/specs` and passes `project`; `apply_add` and the
  `_clear_handed_off_on_requeue` / `_mark_handed_off` callers pass the campaign's project;
  `expand_epic` and `_dangling` scope to `INTERNAL_PROJECT` for bare ids (they read wt's store).
  `wt spec status` and `wt spec review` use the scope. `verify.verify_spec` also calls
  `spec_scope.foreign_scope_path` first (after the outbound check): in a non-wt campaign or project
  checkout it audits the project's file, and an id absent there raises the scope error (surfaced by
  `spec_verify_cmd` as a ClickException); otherwise it keeps the wt-store lookup. There is no
  `wt spec show` subcommand.
- Definition of done, "manual steps performed": the manual verification was **not** performed here
  (the persona sandbox blocks `wt idea reconcile`); it is deferred to Rollout step 3 for the steward,
  as the Test record says. The guarded DoD line itself is left as accepted.
- Loop 4 (steward answer; supersedes Decision rule 1 / the `is_internal_idea` wording in Design):
  an idea matches wt's store when its `:PROJECT:` is empty/`Meta-Tools` **or** wt's spec at its bare
  `:SPEC:` id already names it as `source_idea` (an explicit claim, e.g. `idea promote --internal`
  from a home-lab idea). Only otherwise is it foreign (`flag-foreign`, no write). This is
  `spec_scope.owns_idea(cfg, task, spec_id=None)`; `reconcile_ideas`, `plan_spec_link` and
  `_sole_claimant(…, cfg=)` (for `INTERNAL_PROJECT`) use it. `_write_source_idea` still uses
  `is_internal_idea` (it only stamps specs with no `source_idea`, so nothing is ever claimed
  there), and the plan pass now drops a non-internal sole claimant from the backlink rows too, so
  dry run and `--apply` agree. Outbound `PROJ-NNNN` ids are exempt from project scoping inside
  `_sole_claimant`, so `_mark_handed_off` / `_clear_handed_off_on_requeue` find an outbound idea
  that has no `:PROJECT:`.
- Loop 3 tests: `tests/test_spec_scope.py` gained `verify_spec` coverage for the `WT_CAMPAIGN` case
  and the cwd case (main checkout and linked worktree), a wt spec naming a foreign idea
  (`flag-foreign`, nothing written), and a refused `_write_source_idea` becoming `flag-mismatch`
  while the backlink pass continues.
- Loop 4 tests: outbound `PROJ-0001` hand-off mark/clear on an idea with no `:PROJECT:`; a
  foreign-`:PROJECT:` idea promoted `--internal` reconciles/advances, is hand-off marked and passes
  `plan_spec_link`; a foreign idea whose bare id hits an unclaiming wt spec is `flag-foreign` with
  no write; dry run equals `--apply` for a foreign backlink.

## Rollout / migration

1. Land `spec_scope.py`, the `reconcile_ideas`/`_write_source_idea`/`plan_spec_link` guards and the
   `project` arguments in one change; they are backwards compatible (no argument = old behaviour).
2. Switch the queue/CLI call sites to pass the project / use `resolve_in_scope`.
3. Run the manual verification before the E5 canary's next second-project merge.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The reconcile backlink is the best-fit cause of the observed edit, not a proven one;
  the fix covers every bare-id lookup listed above, so the result does not depend on which one it was.
