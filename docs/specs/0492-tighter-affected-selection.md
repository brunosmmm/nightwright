---
id: SPEC-0492
title: "Tighter affected selection for specs, hot modules and fixtures"
status: in-progress
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0489
tags: [agentic, gates, testing, performance]
depends_on: [SPEC-0490, SPEC-0328, SPEC-0357, SPEC-0359, SPEC-0378, SPEC-0421]
---

## Context

The SPEC-0489 measurements show three sources of selection widening:
- **New spec files.** A new spec selects 119 files: no test names its basename, so the
  selector falls back to the directory readers.
- **Hot modules.** `queue.py` selects 144 files and `store.py` 295, because 98 to 118 test
  files import these modules directly.
- **Fixture data.** A fixture data file that no test names sent 14 runs to the full suite.

With SPEC-0490 landed, a miss is caught by the full-tier sentinel (and later by the cadence
jobs of SPEC-0491 / SPEC-0493), so campaign selection can be tighter.

## Goals / Non-goals

**Goals**
- A spec or ledger change selects only the tests reached by its basename needle, plus the
  `--guards` modules when that flag is set.
- A change to a hot module selects the tests that name a changed definition.
- A fixture data file whose basename no test names selects the path-shaped readers of its
  fixture root.

**Non-goals**
- Changing `--strict` mode, which stays as it is.
- Coverage data.

## Decision

These rules apply in default mode only, as described in *Design*. Misses are caught by the
sentinel's full tiers (SPEC-0490, `NW_ALL_TIERS`) until the cadence jobs land, and by `fast-full`
after. Decided here: this spec depends on SPEC-0490 only, so 0490 -> 0492 can run in parallel
with 0491 -> 0493, because the sentinel is a strictly stronger net than `fast-full`. Every
narrowing rule falls back to the wider selection on any doubt (parse failure, git failure,
private name), never to the full suite on a git error.

## Design

**Specs and ledger.** For `docs/specs/NNNN-*.md` and `docs/LEDGER.md`, use the existing basename
needle (the basename contained in a module's string constants) and do **not** add
parent-directory readers (`_referencing`'s directory fallback). Expand each `tests/` module that
hits with `nearest_tests`, as the reference scan does today. No hit adds no tests for that file
and is not the full suite. `--guards` modules are still added when the flag is set. Tree readers
stay limited to a changed file under `tests/` or `src/nightwright/`; a spec or ledger change
adds none. `docs/specs/SCHEMA.md`, `docs/specs/spec.schema.json` and `docs/specs/TEMPLATE*.md`
keep today's rule, directory fallback included.

**Hot-module narrowing.** It runs in default mode only, for a changed `src/nightwright/**/*.py`
that still exists. A removed module stays on the SPEC-0378 removed-module rule. Let N be that
module's `nearest_tests`.
- When N has at most `--hot-threshold` files (default 30), the module keeps N.
- Above the threshold, hunks come from `git diff -U0 <merge-base> -- <path>` against the
  worktree, which covers committed, staged and unstaged edits. An untracked path is one hunk
  covering every line. `main()` resolves the merge-base on the gate path and on `--changed`, so
  SPEC-0359's `close._affected` narrows from the same diff.
- A git failure skips narrowing for that module (it keeps N) and never selects the full suite or
  raises (the replay's non-git `select(copy, paths)` calls stay as they are).
- Spans come from the worktree AST: top-level functions, classes and single-name assignments,
  from the first decorator line (or the statement line) through `end_lineno`. A hunk inside a
  method counts as the class name. A word match is `\bname\b` on the test file's source, the
  same test `_removed` uses.
- Keep N when the file does not parse, when any hunk intersects no span (an import, a deletion,
  a module statement, a trailing comment), or when any changed name starts with `_` (a private
  helper may be called from public functions not in the diff, so the rule is simply "private
  keeps N"). Both sides of the diff are checked: a changed old line must lie in a span of the
  merge-base version (its name joins the set), so a pure deletion keeps N too. Changed lines
  that are blank are ignored.
- Keep N also when some changed name appears as a word in no file of N: a public definition no
  test names is reached only through other code, so dropping to the name readers would select
  nothing that exercises it.
- Otherwise keep the files in N whose source contains one of those names, plus the module's
  name-rule tests and the `src/nightwright` tree readers the selector already adds. `--guards`
  stays the union in `main()`.

The JSON report's `hot` object maps the repo-relative path to the names used, for modules that
narrowed; modules that kept N are omitted, and the key is always present (`{}` when nothing
narrowed). On `--changed`, the merge-base is resolved only to read hunks; the report's
`base_sha` stays null as before.

**Fixture data.** A fixture data file whose basename no test names selects the path-shaped
readers of its fixture root. For a non-Python file under `tests/fixtures/<dir>/...`, `<dir>` is
the first path component under `tests/fixtures/`, including files nested deeper.
- The basename needle is unchanged: when any module names the basename, the selection is that
  reference scan and stays tight.
- When none does, the directory needle is `has_dir_ref(<dir>)` (a constant ending in `<dir>/` or
  `/<dir>`, or containing `/<dir>/`, or a `Path / '<dir>'`), not the immediate parent and not a
  bare substring. Test modules that hit are expanded with `nearest_tests` as today.
- When that needle also hits nothing, the result is the full suite and the reason is
  `no test references <rel>` (today's reason).
- A file directly in `tests/fixtures/`, and every data file outside `tests/fixtures/<dir>/`,
  keep today's rule (`tests/fixtures/a.txt` and `tests/data/` stay as they are).
- The wide closure (the readers of a changed file's readers) adds both the immediate parent's
  and the fixture root's directory readers, so it never shrinks.

**Measurement.** For each of the last 200 first-parent commits on main, check out that commit
and run the selector under test with `--repo` at that checkout, `--base <commit>^`, `--guards`,
`--include tests/test_agentic_dogfood_config.py` and `--include tests/test_agentic_project.py`.
Do not pass `--changed` (a file list cannot carry historical hunks and would not exercise the
hot-module rule). Run it once with the selector from the parent of this change and once with the
new one, and record both median percentages of that commit's test files in *As shipped*.

## Alternatives considered

- **Coverage-based selection.** Rejected in SPEC-0328: it needs per-worktree seeding and goes
  stale after a rebase.
- **A fraction cap.** Rejected in SPEC-0378: it skips arbitrary tests.

## Acceptance criteria

- [ ] A new spec file or `docs/LEDGER.md` selects only the tests reached by its basename needle
  (expanded with `nearest_tests`) plus the `--guards` modules when set. It adds no tree readers
  and no parent-directory readers, and an empty basename hit is not the full suite.
  `SCHEMA.md`, `spec.schema.json` and `TEMPLATE*.md` still fall back to the directory.
- [ ] A body-only edit of one public top-level function, in a module whose nearest tests exceed
  `--hot-threshold` (default 30), selects the nearest tests whose source contains that name as a
  word, plus that module's name-rule tests and the `src/nightwright` tree readers, and the
  report's `hot` maps the path to that name. At or below the threshold the whole nearest set is
  kept.
- [ ] The whole nearest set is kept when a hunk lies outside the spans (including a trailing
  comment), when any changed name starts with `_`, when the file does not parse, and when the
  diff cannot be read, including when the diff also edits a public function. A git failure on
  `--changed` skips narrowing and is not the full suite.
- [ ] `--strict` does not narrow and is unchanged.
- [ ] A non-Python fixture file under `tests/fixtures/<dir>/...` whose basename no test names
  selects the tests with a `has_dir_ref(<dir>)` match, `<dir>` being the first component, also
  for files nested deeper. A basename hit stays the basename readers. No basename hit and no
  `<dir>` reference selects the full suite with reason `no test references <rel>`.
  `tests/fixtures/a.txt` and `tests/data/` are unchanged.
- [ ] The 200-commit replay is recorded in *As shipped*: for each commit it checks out that
  commit and runs the selector with `--repo`, `--base <commit>^`, `--guards` and the two
  `--include`s (no `--changed`), before and after. The after median is lower than the before
  median and below the SPEC-0489 figure of 44 %.

## Test plan

- **Automated tests:** `tests/test_affected_tests.py` covers:
  - spec and ledger basename needles: an empty basename hit, absent directory readers,
    `--guards` still added, and `SCHEMA.md`, `spec.schema.json` and `TEMPLATE*.md` still falling
    back to the directory;
  - hot-module narrowing: the edit lands **inside** a span (body-only edit of a public
    function), a trailing comment keeps the whole nearest set, a private name, an import edit,
    an unparsable file, the threshold, and a `--changed` git failure;
  - the existing `_change` helper appends a trailing comment (outside every span); hot-module
    tests edit inside the span instead;
  - the fixture root (nested, first component, `has_dir_ref` not bare substring, basename win)
    and the empty fixture root with its reason;
  - `--strict` unchanged;
  - `tests/test_affected_tests_replay.py` still passes with no base on a non-git copy.
- **Manual verification:** the replay, recorded in *As shipped*.
- **Regression guard:** the existing `test_affected_tests.py` cases pass. Run the affected
  tests.

## Rollout / migration

This change lands after SPEC-0490, so that the sentinel keeps running every tier through
`NW_ALL_TIERS`. It may run in parallel with SPEC-0491 and SPEC-0493.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
