---
id: SPEC-0576
title: "seed-children: match an 'existing IDEA-NNN' Breakdown bullet by id across all ideas files"
status: proposed
owner: user
created: 2026-11-12
kind: feature
source_idea: IDEA-548
depends_on: [SPEC-0043, SPEC-0097, SPEC-0423]
tags: [seed-children, ideas, idempotency]
---

## Context

`nw spec seed-children` ([SPEC-0043](./0043-epic-promote-seeds-child-ideas-known-epics.md),
[SPEC-0097](./0097-seed-children-per-bullet-project-name-for-multi.md)) turns each Breakdown
bullet of an epic into an INCUBATE idea. Idempotency is by heading only:
`seed_ideas_from_epic` (`src/nightwright/specs.py`) builds `existing` from ideas whose
`:EPIC:` equals the epic id, keyed by casefolded, dependency-annotation-stripped heading,
and creates a new idea for any bullet whose title is not a key.

Epics are often written over work that was already captured as an idea. The author then
words the bullet as "existing IDEA-450 — …" so that no new idea is meant. That bullet's
title (the text before the first ` — `) is not the heading of IDEA-450, and IDEA-450 normally
carries no `:EPIC:` for this epic yet, so the heading match misses. The result is a duplicate
idea: JEROME-0007 seeded IDEA-547 for a bullet that named IDEA-450, which lives in a different
org ideas file (`ideas-20261030.org`). `load_tasks(cfg)` already parses every configured org
file, so the idea is visible; the code just never looks the bullet's named id up.

## Goals / Non-goals

**Goals**
- A Breakdown bullet of the form `existing IDEA-NNN …` is resolved to idea `IDEA-NNN`
  wherever it lives (any configured org file, including ideas files other than
  the capture/ideas file), and no new idea is created for it.
- A bullet that names an id which resolves to no idea fails loudly instead of seeding a
  duplicate.
- The matched idea participates in `(depends_on child N)` `:AFTER:` stamping like any seeded
  child (a bullet may be depended on by, or itself depend on, others).
- Re-running `seed-children` stays idempotent.

**Non-goals**
- Rewriting the matched idea: `:EPIC:`, `:PROJECT:`, heading, Summary and state are left
  exactly as they are. Linking an existing idea to the epic is a separate, human decision.
- Treating any `IDEA-NNN` mention as a match: only the explicit `existing IDEA-NNN` form
  triggers it, so a bullet that merely cites an id in passing still seeds a new idea.
- Changing the heading-based idempotency or the `(project: Name)` / `(depends_on …)` syntax.
- Outbound/portable epics get no special handling beyond sharing the same function.

## Decision

Add an id-based match ahead of the heading match in `seed_ideas_from_epic`. A bullet is an
"existing idea" bullet when its text contains `existing` followed (optionally via the word
`idea` and/or backticks) by an `IDEA-NNN` token, case-insensitive. The id is looked up among
**all** ideas returned by `load_tasks(cfg)` regardless of file or `:EPIC:`. A hit means the
bullet maps to that idea (nothing created, nothing mutated). A miss raises `ValueError`
naming the bullet and the id, before any idea is created, mirroring the SPEC-0097 hard-fail
for unknown projects (no soft-warn, no duplicate). Bullets without the form behave exactly
as today.

## Design

- `specs.py`: add `_EXISTING_IDEA_RE = re.compile(r"\bexisting\s+(?:idea\s+)?`?(IDEA-\d+)\b",
  re.IGNORECASE)` and `parse_breakdown_existing(item) -> str | None`, returning the
  upper-cased id of the first match, or `None`. It is applied to the raw bullet text, after
  `parse_breakdown_project` / `parse_breakdown_deps` strip their annotations (so an
  annotation can neither hide nor fake a match).
- `seed_ideas_from_epic`: during the validation pass that already builds `parsed`, resolve
  each bullet's existing id against `{ID: task}` built once from
  `[t for t in load_tasks(cfg) if t.is_idea]` (all files). Unknown id → `ValueError(f"Breakdown
  bullet {n} ({item.strip()[:60]!r}): existing {id} is not an idea in any org file")`. All
  validation happens before the creation loop, so a failing bullet leaves no partial seeding.
  `parsed` gains the resolved id (or `None`).
- Creation loop: a bullet with a resolved id appends that id to `idea_of` and `continue`s —
  no `add_idea`, no `set_summary`, no entry in `added`, no `existing` heading key. Order of
  `idea_of` still equals bullet order, so the SPEC-0423 `:AFTER:` stamping works unchanged
  and fills `:AFTER:` on the matched idea only when it is blank (existing rule: never
  overwrite).
- A bullet with a project annotation and an existing id: the annotation is still validated
  (unknown name still hard-fails) but is not applied to the matched idea.
- Return value stays the list of newly created ids, so `spec_seed_children_cmd` in
  `src/nightwright/cli.py` needs only a docstring line; its "no new child ideas" message is
  correct when every bullet was matched or already seeded.
- Docs: one line in `docs/specs/TEMPLATE-epic.md` (Breakdown section, next to the
  `(project: Name)` note) and the `seed-children` docstring describing the
  `existing IDEA-NNN` form and that it matches across all ideas files.

## Alternatives considered

- **Match any `IDEA-NNN` mentioned in the bullet** — rejected: bullets legitimately cite
  related ideas (`supersedes IDEA-12`), and a false match would silently drop a child.
- **Soft-skip when the id is unknown (seed a new idea anyway)** — rejected: that is the
  duplicate-seeding bug again; a typo should stop the run, as unknown `(project:)` does.
- **Stamp `:EPIC:` on the matched idea** — rejected for this spec: it mutates a human-owned
  idea and could overwrite a different epic link; left as a separate, explicit step.
- **Restrict the lookup to the ideas file the epic's idea came from** — rejected: the bug is
  precisely a cross-file idea.

## Acceptance criteria

- [ ] A Breakdown bullet `existing IDEA-NNN — …` whose idea sits in a second org ideas file
      (not the capture/ideas file) creates no new idea; `seed_ideas_from_epic` returns `[]`
      for it and the total idea count is unchanged.
- [ ] The matched idea's `:EPIC:`, `:PROJECT:`, heading, Summary and state are unchanged.
- [ ] Matching is case-insensitive and accepts `existing IDEA-N`, `existing idea IDEA-N`
      and ``existing `IDEA-N` ``.
- [ ] A bullet that only mentions an id without `existing` (e.g. `supersedes IDEA-12`)
      still seeds a new idea.
- [ ] A bullet naming `existing IDEA-999` where no such idea exists raises `ValueError`
      naming the bullet number and the id, and no ideas are created by that call (including
      for other bullets in the same Breakdown).
- [ ] Mixed Breakdown (one matched bullet, one new bullet): only the new bullet is seeded;
      a second run returns `[]`.
- [ ] `(depends_on child N)` on a new bullet pointing at a matched bullet stamps the matched
      idea's id into `:AFTER:`; a matched idea with a non-empty `:AFTER:` is not overwritten.
- [ ] Bullets without the `existing IDEA-NNN` form behave exactly as before (existing
      `tests/test_seed_children.py` tests pass unchanged).
- [ ] `TEMPLATE-epic.md` and the `seed-children` help text document the form.

## Test plan

- **Automated tests:** extend `tests/test_seed_children.py` (already carries its single
  module-level `pytestmark = pytest.mark.fast`; no new module and no second marker) with:
  `test_seed_existing_idea_in_other_ideas_file_not_duplicated` (second org file in the
  fixture's `org` dir holds `IDEA-450`; assert no new idea, returns `[]`, target idea
  properties byte-identical), `test_seed_existing_idea_form_variants`,
  `test_seed_plain_idea_mention_still_seeds`, `test_seed_existing_unknown_id_hard_fails_before_seeding`
  (asserts the error text and that a sibling new bullet was not created),
  `test_seed_mixed_existing_and_new_is_idempotent`, `test_seed_existing_idea_participates_in_after`
  (stamps and never-overwrite), and a `parse_breakdown_existing` unit test.
  `python3 tools/test_tiers.py --check` exits 0.
- **Manual verification:** in a scratch config, create two ideas files, put an idea in the
  second, write an epic with an `existing IDEA-N` bullet, run
  `uv run nw spec seed-children <EPIC>` and confirm "no new child ideas"; repeat with a
  bogus id and confirm a non-zero exit naming the bullet.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`
  (covers `tests/test_seed_children.py`, `tests/test_idea_ordering_e2e.py`) and
  `python3 tools/spec_lint.py` exit 0.

## Rollout / migration

No data migration. Epics already seeded with duplicates (e.g. IDEA-547 for IDEA-450) are
not touched; the human closes the duplicate. The fix applies on the next `seed-children`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated via `python3 tools/spec_lint.py --write-ledger`.

## Open questions

None.
