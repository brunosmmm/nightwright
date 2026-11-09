---
id: SPEC-0492
title: "Tighter affected selection for specs, hot modules and fixtures"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0489
tags: [agentic, gates, testing, performance]
depends_on: [SPEC-0491, SPEC-0328, SPEC-0378, SPEC-0421]
---

## Context

The SPEC-0489 measurements show three sources of selection widening:
- **New spec files.** A new spec selects 119 files: no test names its basename, so the
  selector falls back to the directory readers.
- **Hot modules.** `queue.py` selects 144 files and `store.py` 295, because 98 to 118 test
  files import these modules directly.
- **Fixture data.** A fixture data file that no test names sent 14 runs to the full suite.

With the `fast-full` cadence job (SPEC-0491) as the safety net, campaign selection can be
tighter.

## Goals / Non-goals

**Goals**
- A spec or ledger change selects only the tests that name it, plus guards and readers.
- A change to a hot module selects the tests that name a changed definition.
- A fixture data file selects the readers of its fixture root.

**Non-goals**
- Changing `--strict` mode, which stays as it is.
- Coverage data.

## Decision

These rules apply in default mode only, as described in *Design*. Misses are caught by
`fast-full` within 5 merges.

## Design

**Specs and ledger.** For `docs/specs/NNNN-*.md` and `docs/LEDGER.md`, the selector keeps only
the test modules whose text contains the file's exact basename. It no longer falls back to the
parent-directory readers. The tree readers and `--guards` modules the selector already adds
stay. `docs/specs/SCHEMA.md`, `spec.schema.json` and `TEMPLATE*.md` keep today's rule.

**Hot-module narrowing.** It applies when:
- a changed src module's nearest tests exceed `--hot-threshold` (default 30 files); and
- the merge-base diff can be read.

The selector extracts the top-level names (functions, classes and module constants) whose
definition lines intersect the diff hunks. It keeps:
- the nearest tests whose source contains one of those names as a word;
- the module's name-rule tests;
- the tree readers and guards.

It keeps the whole nearest set in these cases:
- no name is found (a module-level change or an import edit);
- a changed name is private and referenced only inside the module;
- the diff cannot be read.

The report adds `hot: {module: [names]}`.

**Fixture data.** A data file under a `tests/fixtures/<dir>/` tree selects the test modules
that reference `<dir>`. If no test references `<dir>` either, the result stays the full suite,
with the reason `no test references that path`.

**Measurement.** Replay the last 200 first-parent commits on main through the selector, before
and after the change, using the method in SPEC-0489 (`--changed` with each commit's files,
`--guards` and the gate's `--include`s).

## Alternatives considered

- **Coverage-based selection.** Rejected in SPEC-0328: it needs per-worktree seeding and goes
  stale after a rebase.
- **A fraction cap.** Rejected in SPEC-0378: it skips arbitrary tests.

## Acceptance criteria

- [ ] A new spec file or the ledger selects only the tests that name its basename, plus the
  tree readers and `--guards` modules.
- [ ] A hot-module change that touches only named definitions selects only the nearest tests
  that name one of them, plus name-rule tests, tree readers and guards.
- [ ] These hot-module cases keep the whole nearest set:
  - a module-level change;
  - a private-only change;
  - an unreadable diff.
- [ ] `--strict` is unchanged.
- [ ] A fixture data file selects its fixture root's readers. With no readers, it selects the
  full suite with reason `no test references that path`.
- [ ] The 200-commit replay is recorded in *As shipped*, before and after. Its median is below
  today's 44 %.

## Test plan

- **Automated tests:** `tests/test_affected_tests.py` covers:
  - spec and ledger needles, with guards and readers retained;
  - each hot-module narrowing case;
  - the threshold;
  - the fixture root and the empty fixture root;
  - `--strict` unchanged.
- **Manual verification:** the replay, recorded in *As shipped*.
- **Regression guard:** the existing `test_affected_tests.py` cases pass. Run the affected
  tests.

## Rollout / migration

This change lands after SPEC-0491, so that `fast-full` is already running.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
