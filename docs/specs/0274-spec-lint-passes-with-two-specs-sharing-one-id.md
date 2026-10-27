---
id: SPEC-0274
title: "spec_lint fails on duplicate spec ids and duplicate number prefixes"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-102
tags: [specs, lint]
---

## Context

Promoted from idea `IDEA-102` (spec_lint passes with two specs sharing one id (two SPEC-0219
files on a rebased branch); it must fail on duplicate ids).

Still present on main. `tools/spec_lint.py:validate` checks that each file's `id` matches its
own filename number (check 2) but never compares files with each other. Two files
`0219-a.md` and `0219-b.md`, both `id: SPEC-0219`, pass every check; `by_id = {s["id"]: s}`
silently keeps the last one, so references and the epic rollup resolve to an arbitrary file
and the generated ledger lists the id twice. This happens when two branches allocate the
same number and one is rebased onto the other.

## Goals / Non-goals

**Goals**
- `python3 tools/spec_lint.py` (and therefore `tests/test_specs.py` and the spec-lint gate)
  exits non-zero when two spec files share an `id`, or share a 4-digit filename prefix.

**Non-goals**
- Preventing the collision at allocation time (`wt spec new` / `spec_numbers.py`).
- Auto-renumbering.

## Decision

Add check 11 to `validate`: group specs by `id` (non-empty) and by `num` (filename prefix);
for each group with more than one file, report every file in it:
`"<file>: duplicate id SPEC-NNNN (also in <other files>)"` and
`"<file>: duplicate number prefix NNNN (also in <other files>)"`. The prefix check catches the
case where one of the colliding files has broken or missing frontmatter.

## Design

`tools/spec_lint.py`: after the per-spec loop, before ledger freshness, build
`collections.defaultdict(list)` maps over `specs` keyed by `s["id"]` and `s["num"]`; emit
problems for groups of size ≥ 2 (sorted file names, deterministic). Add item 11 to the module
docstring's check list. No change to `load_specs` or the ledger renderer.

## Alternatives considered

- **Only the id check** — misses a duplicate prefix whose second file has no frontmatter or a
  typo'd id (that file gets the id-mismatch error, but the collision itself is not named).

## Acceptance criteria

- [ ] Two files with the same `id` make `validate` report `duplicate id` naming both files, and
      `main()` returns 1.
- [ ] Two files with the same 4-digit prefix (different or missing ids) report
      `duplicate number prefix`.
- [ ] The current `docs/specs/` tree still lints clean (`python3 tools/spec_lint.py` exit 0).

## Test plan

- **Automated tests** (`tests/test_spec_lint_duplicates.py`, new): load the linter module like
  `tests/test_specs.py` does (`importlib.util.spec_from_file_location`), write minimal valid
  specs into `tmp_path`, and `monkeypatch.setattr(mod, "SPECS", tmp_path)`.
  - `test_duplicate_id_fails` — `0001-a.md` and `0001-b.md` both `id: SPEC-0001`;
    `mod.validate(mod.load_specs())` contains a `duplicate id SPEC-0001` problem naming both
    files.
  - `test_duplicate_prefix_fails` — `0001-a.md` (`SPEC-0001`) and `0001-b.md` with no
    frontmatter → a `duplicate number prefix 0001` problem.
  - `test_unique_ids_no_duplicate_problem` — `0001-a.md` and `0002-b.md` → no problem mentions
    `duplicate`.
- **Sabotage check:** comment out the new check → the two failing-case tests fail. Restore.
- **Manual verification:** in a scratch worktree, copy any spec to a second file with the same
  prefix and run `python3 tools/spec_lint.py` → exit 1 with `duplicate id`; remove the copy.
- **Regression guard:** `tests/test_specs.py::test_all_specs_pass_lint` and full
  `uv run pytest`.

## Rollout / migration

None; the live tree has no duplicates today.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
