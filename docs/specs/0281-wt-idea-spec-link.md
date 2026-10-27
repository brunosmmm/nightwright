---
id: SPEC-0281
title: "wt idea spec-link: set or replace an idea's :SPEC:, identity-checked"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-056
depends_on: [SPEC-0257, SPEC-0267]
tags: [ideas, specs, reconcile, cli]
---

## Context

Promoted from idea `IDEA-056` ("wt spec renumber / wt idea spec-link: move a spec to a new id
and re-point its idea's :SPEC:"). Its log entry of 2026-10-25 states the need. Specs written by
hand, or by a session that did not use `wt spec new --from-idea`, leave their idea with no
`:SPEC:`. Then `wt spec reconcile` cannot advance the idea, and shipped work stays listed as
open in `wt ideas` and `wt next`.

What exists today:

- `specs.reconcile_ideas` (SPEC-0257) links an unlinked idea only when a spec names it in
  `source_idea`. If a spec does not name its idea, no tool can link the two.
- SPEC-0267 added an identity rule because ideas ids were reused after the ideas file rotated.
  `specs._idea_predates_spec(task, spec_created)` requires the idea's `:CREATED:` day to be on
  or before the spec's `created`. `specs._pick_idea(cands, spec_created)` takes the newest
  date-valid idea among several that carry one id, and refuses on a mismatch or a tie.
- The only manual routes are a hand edit of the org file or `wt spec new --force`, which
  mints a new spec. A hand edit skips the identity check and the shared-file write lock.
- `org_write.resolve_selector` raises "ambiguous selector" when two ideas in rotated files
  carry the same `:ID:`. Commands that take a selector, such as `wt idea state IDEA-NNN`,
  cannot target such an idea at all.

## Goals / Non-goals

**Goals**
- `wt idea spec-link IDEA SPEC` sets or replaces an idea's `:SPEC:`. Without `--yes` it is a
  dry run.
- It finds the idea across every configured org file, rotated ones included. When an id
  repeats, it picks the idea with the SPEC-0267 rule.
- It refuses in these cases:
  - the idea fails the identity check (created after the spec, or no parseable date on
    either side);
  - the pick is ambiguous;
  - the spec's `source_idea` names a different idea;
  - `--heading` does not match.
- After the write, it runs the existing reconcile scoped to that spec, so the idea's state
  follows the spec's status.

**Non-goals**
- `wt spec renumber` (moving a spec file to a new id). Renumbering is rare since the
  SPEC-0208 allocator and needs a rewrite of cross-references, so it stays in `IDEA-056`.
- Writing `source_idea` into the spec file. The command never edits spec files, because an
  edit in the main checkout would park later merges as `main_dirty`. The reconcile row
  `flag-no-backlink` is printed as a hint instead.
- Unlinking (`:SPEC:` removal), and a Log entry on the idea.

## Decision

Add `wt idea spec-link IDEA SPEC [--heading TEXT] [--yes] [--json]` to `src/wt/cli.py`
(`idea_group`). It is backed by a new read-only planner `specs.plan_spec_link(cfg, idea_id,
spec_id)` and a writer `specs.apply_spec_link(cfg, plan)`. The planner reuses the SPEC-0267
helpers unchanged, so the manual link and the automatic walk share one identity rule.

`--yes` requires `--heading`, a case-insensitive substring of the picked idea's headline.
Several sessions edit the shared ideas file at once, so a write must name what it expects to
touch. The dry run prints the file, line, heading and `:CREATED:`, so the operator can copy
the heading from it.

## Design

- `src/wt/specs.py:plan_spec_link(cfg, idea_id, spec_id) -> dict` (new, read-only):
  1. Normalize `idea_id` with `org_write._normalize_selector`. Resolve the spec with
     `workflow._load_spec_fm(cfg, spec_id)`, which handles both internal and outbound ids. If
     it returns None, raise `ValueError("spec not found")`.
  2. Collect `cands`: the ideas from `org.filter_tasks(org.load_tasks(cfg), is_idea=True)`
     whose `:ID:` equals the id. `load_tasks` walks every path from `org._iter_org_paths`, so
     rotated files are included. If there are none, raise `ValueError`.
  3. Call `task, bad = _pick_idea(cands, _as_date(fm.get("created")))`. If `bad` is set,
     raise `ValueError` with its `flag-mismatch` or `flag-ambiguous` detail.
  4. Set `named = str(fm.get("source_idea") or "").strip()`. If it is set and differs from
     the id, raise `ValueError(f"{spec_id} names {named} as its source_idea")`.
  5. Return `{idea_id, spec_id, file, line, heading, created, current_spec, action}`.
     `action` is `link` (no `:SPEC:`), `replace` (another spec) or `unchanged` (same spec).
     When `len(cands) > 1`, the plan also carries `pin = (task.file, task.heading)`.
- `src/wt/specs.py:apply_spec_link(cfg, plan) -> list` (new):
  1. When `action == "unchanged"`, do nothing.
  2. Otherwise call `org.invalidate_tasks_cache()` and re-find the idea with
     `_resolve_idea(cfg, idea_id, pin)`, the SPEC-0267 helper. None means the idea moved or
     vanished since the plan, so raise.
  3. Re-check that `:SPEC:` still equals `plan["current_spec"]`; on drift, raise.
  4. Call `org_write.set_property(cfg, task, "SPEC", spec_id)`. It holds the SPEC-0190 write
     lock and re-locates the headline by identity (`_locate_headline`).
  5. Return the rows of `reconcile_ideas(cfg, apply=True, spec_ids=[spec_id],
     backlinks=False)` for this idea.
- `src/wt/cli.py`, new `@idea_group.command("spec-link")` with `idea_spec_link_cmd`:
  - It prints the plan: `file:line`, heading, `:CREATED:` and `current → new`.
  - With `--yes`, a missing `--heading` is a `UsageError`. A heading that is not a substring
    of `plan["heading"]` is a `ClickException` that shows the real heading.
  - It then applies the link and prints the reconcile rows: `advance`, `unchanged`, and
    `flag-no-backlink` as a hint.
  - Any `ValueError` becomes a `ClickException` (exit 1).
  - `--json` emits `{"plan": …, "applied": bool, "reconcile": [...]}`.
- Shell completion uses `C.complete_idea_id` for IDEA and `C.complete_spec_id` for SPEC.

## Alternatives considered

- **Let `reconcile` infer links by title similarity.** Rejected: the match is fuzzy, and
  headings are edited after capture. SPEC-0267 rejected it for the same reason.
- **Write `source_idea` into the spec instead.** Rejected: it needs a spec-file edit in the
  main checkout (`main_dirty`), and it does nothing for outbound specs in other repos.
- **No `--heading` guard; trust the id.** Rejected: ids were reused after the rotation. The
  guard costs one copy-paste from the dry run, and it stops a write to the wrong idea.

## Acceptance criteria

- [x] Without `--yes`, `wt idea spec-link IDEA SPEC` prints the target idea (file:line,
      heading, `:CREATED:`) and the `link`/`replace`/`unchanged` action, and writes nothing.
- [x] With `--yes --heading <substring>`, it sets or replaces `:SPEC:` on exactly that idea,
      and the scoped reconcile advances the idea to match the spec's status. With `--yes`,
      a missing or non-matching `--heading` exits non-zero and writes nothing.
- [x] When ideas in different org files (after a rotation) carry the same id, the SPEC-0267
      pick chooses the newest date-valid one, and only that idea is written.
- [x] It refuses with exit 1 and writes nothing when:
      - the idea was created after the spec;
      - either date is unparseable;
      - the pick is ambiguous;
      - the spec's `source_idea` names another idea;
      - the spec does not exist.
- [x] If the idea's `:SPEC:` changed between plan and apply, the command refuses and writes
      nothing.
- [x] The command never modifies a spec file.

## Test plan

New `tests/test_idea_spec_link.py`. It uses the tmp org and specs fixtures from
`tests/test_reconcile.py`: a `cfg` with `org_files` and `specs_dir` under `tmp_path`.

- `test_dry_run_writes_nothing`: the org file bytes are unchanged, and the output names
  file:line, heading and action `link`.
- `test_yes_links_and_reconciles`: an INCUBATE idea and an `accepted` spec end with `:SPEC:`
  set and state SPECCED. With a `done` spec, the idea makes the transition that
  `reconcile_ideas` gives done internal specs.
- `test_replace_existing_spec`: `:SPEC: SPEC-0001` becomes `SPEC-0002`, with action
  `replace`.
- `test_yes_requires_matching_heading`: no `--heading` gives a usage error, and a wrong
  heading exits 1. In both cases the file is unchanged.
- `test_rotated_duplicate_ids_pick_newest_valid`: two org files carry `IDEA-007`. The old one
  was created before the spec and the new one after. The old one is linked and the new one
  is untouched. In a variant, both are valid with different dates, and the newer one is
  linked.
- `test_refuses_reused_id_mismatch`: one case where the idea was created after the spec's
  `created`, and one where the idea has no `:CREATED:`.
- `test_refuses_ambiguous_and_foreign_source_idea`: one case with two date-valid twins that
  have the same `:CREATED:`, and one with a spec whose `source_idea` names another idea.
- `test_drift_between_plan_and_apply`: `:SPEC:` is changed after `plan_spec_link`;
  `apply_spec_link` raises and writes nothing more.
- `test_spec_files_untouched`: the spec file bytes are unchanged after `--yes`.
- **Sabotage check:**
  - Replace the `_pick_idea` call in `plan_spec_link` with "first candidate":
    `test_rotated_duplicate_ids_pick_newest_valid` and `test_refuses_reused_id_mismatch` must
    fail.
  - Drop the `--heading` check: `test_yes_requires_matching_heading` must fail.
- **Regression guard:** the full `uv run pytest`. `tests/test_reconcile.py` is unchanged and
  passes.
- **Manual:** in a scratch data dir, run a dry run, then `--yes --heading`, and confirm the
  link with `wt idea show`.

### Test record (2026-10-27)

- `uv run pytest -q tests/test_idea_spec_link.py`: 15 passed (every test named above, plus
  unchanged, unparseable-spec-date, missing spec/idea and `--json` cases).
- Sabotage: `_pick_idea` replaced by "first candidate" failed
  `test_rotated_duplicate_ids_pick_newest_valid`, `test_refuses_reused_id_mismatch` (both
  params), `test_refuses_unparseable_spec_date` and `test_refuses_ambiguous_and_foreign_source_idea`;
  dropping the `--heading` check failed `test_yes_requires_matching_heading`. Both restored.
- Full `uv run pytest -q`: 3468 passed, 3 skipped; `tests/test_reconcile.py` is unchanged.
  `tests/test_rich_output_guard.py` gained an allowlist entry for the `--json` emitter.
- Manual: the dry run → `--yes --heading` → confirm flow was exercised end to end through
  `CliRunner` against tmp org/specs dirs (`test_yes_links_and_reconciles`, `test_json_output`);
  no run against a real data dir, to keep the shared ideas file untouched.

### As shipped (2026-10-27)

As designed. The `ValueError` from a refused pick is prefixed with its reconcile action
(`flag-mismatch: …` / `flag-ambiguous: …`). `--json` is emitted with `click.echo`, so
`idea_spec_link_cmd` is on the SPEC-0185 raw-output allowlist.

## Rollout / migration

None; this is a new command. Once it ships, the conductor or a human links the shipped ideas
that are still unlinked, for example those found by the audit of 2026-10-27.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
