---
id: SPEC-0350
title: "Per-gate path sensitivity for the close re-gate"
status: done
updated: 2026-11-01
owner: user
created: 2026-11-01
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, gates, close, merge]
depends_on: [SPEC-0234, SPEC-0328]
---

## Context

[SPEC-0234](./0234-docs-only-merge-requests-and-conductor-skill-routing.md) lets a project list
`merge.docs_only_paths`, paths no gate reads, so a docs-only merge may skip every gate. It is
all-or-nothing, so it excludes every path some test reads. A merge of `README.md`, `mkdocs.yml` or
`docs/product/**` therefore still runs the whole pytest suite (10-17 min) at the close re-gate,
although only the docs tests and spec-lint can be affected.

## Goals / Non-goals

**Goals**
- G1. A gate may declare `paths:` (globs). At the close re-gate it runs only when the branch
  changes a matching path. A gate without `paths` always runs (backward compatible).
- G2. Each skip is recorded in the campaign ledger.
- G3. This repo uses it: README/mkdocs/docs-only branches run spec-lint + a docs-tests gate, not
  the full suite.

**Non-goals**
- Changing `merge.docs_only_paths` / `--docs-only` (still skips all gates).
- Path selection for the loop tier (`pytest-affected` is already selective).

## Decision

`gates[].paths` is an optional non-empty list of globs. In `prepare_close`, after the rebase and
(re)generation, the changed paths are `git diff --name-only --no-renames TARGET...HEAD`. A close-tier
gate (`when: close` or `always`) whose `paths` match none of them is dropped from the run and a
`gate.skipped` ledger event is appended. **Fail closed:** if the changed paths cannot be computed
(git error) or are empty, every gate runs. A **required gate that is skipped counts as satisfied**:
it is not in the gate run, so it cannot fail the close.

Matcher (`project.path_matches`): anchored at the repo root, `/`-separated; `*` any run inside one
segment, `?` one non-`/` char, `**` any run including `/` (`a/**` = everything under `a/`, `**/x`
= `x` at any depth), trailing `/` = whole directory, and a pattern containing no `/` matches by
basename at any depth (gitignore-like; errs toward running the gate).

## Design

- `project.py`: `paths` accepted and validated (non-empty list of strings); `path_matches`,
  `split_gates_by_paths(gates, changed)` -> `(run, skipped)`.
- `close.py`: `_skip_unaffected_gates` filters the overlay config before `G.run_all(..., tier="close")`.
- `schemas.py`: ledger event `gate.skipped` with required keys `gate`, `patterns`, `changed_count`
  (extra `required`).
- Display: the event is a plain ledger event; no dedicated campaign-show rendering (not cheap).

**Which paths tests read (evidence, grep of `tests/`)**
- `tests/test_docs_*.py` read `README.md`, `mkdocs.yml`, `docs/product/**`, `scripts/docs-*`,
  `.github/workflows/docs-pages.yml`.
- `tests/test_specs.py` (+ `test_spec_lint_duplicates.py`) lint `docs/specs/**`; `spec-lint` does the same.
- `tests/test_agentic_profiles.py` reads `docs/product/capabilities/agentic.md`: put in docs-tests.
- Non-docs tests read real: `docs/specs/TEMPLATE.md`, `TEMPLATE-epic.md`, `SCHEMA.md`,
  `spec.schema.json` (spec_review, promote, hub, completion, export_schemes, idea_epic, seed_children),
  `docs/specs/0003-*.md`/`0004-*.md` (test_completion), `docs/examples/**` (test_export_schemes),
  `docs/WORKFLOW.md` (test_worker), `AGENTS.md` (test_skills), `skills/**` (conduct-skill tests),
  `scripts/**`, `.github/**`. All of these map to the full `pytest` gate.
- `docs/LEDGER.md` and ordinary specs are read only by spec-lint/test_specs.

**This repo's gates**: `spec-lint` (no paths); `docs-tests` (`when: close`, paths README.md,
mkdocs.yml, `docs/**`, `skills/**`, `scripts/**`, `.github/**`); `pytest` (`when: close`, paths
`src/** tests/** tools/** skills/** pyproject.toml uv.lock .nw/** AGENTS.md scripts/** .github/**`
plus the docs files listed above).

## Alternatives considered

- **Extend `docs_only_paths` with globs** - still all-or-nothing; cannot say "docs tests only".
- **Rely on `affected_tests.py` at close** - the static import graph does not see data-file reads.

## Acceptance criteria

- [x] A gate with no `paths` always runs; `paths` is validated (non-empty list of strings).
- [x] At close, a gate whose `paths` match no changed path is skipped and a `gate.skipped` event is recorded.
- [x] An uncomputable or empty changed-path list runs every gate.
- [x] The dogfood config: README-only and mkdocs/docs/product/docs/specs-only branches run spec-lint + docs-tests, not pytest; `src/**`, `tests/**`, `pyproject.toml`, `uv.lock`, `skills/**` run pytest.
- [x] `docs/product/capabilities/agentic.md` documents `paths:`.

## Test plan

- **Automated:** `tests/test_agentic_gate_paths.py` (matcher table, split semantics, validation,
  dogfood coverage guard and sample changed-path sets, close skip / run / fail-closed);
  `tests/test_agentic_dogfood_config.py`, `tests/test_agentic_project.py`, close/merge tests.
- **Regression:** `python3 tools/affected_tests.py --run uv run pytest -q` plus the close, merge,
  project and docs-product tests; `python3 tools/spec_lint.py` exits 0.

## Clock Log

CLOCK-IN: n/a
CLOCK-OUT: n/a

## Rollout / migration

Additive. Takes effect when `main`'s `.nw/project.yaml` carries `paths`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

## As shipped

Shipped as designed. `tools/affected_tests.py` fell back to the full suite for this change
(`project.py` is widely imported), so the Test plan ran as an explicit set: `test_docs_*`,
`test_spec_lint*`, `test_specs`, `test_agentic_profiles`, `test_agentic_close*`,
`test_agentic_queue_merge`, `test_agentic_logs`, `test_agentic_gitops`, `test_agentic_resolver`,
`test_agentic_gate_paths`, `test_agentic_dogfood_config`, `test_agentic_project` (572 passed).
The starter template in `project.py` shows `paths` as a comment (an empty list is invalid). The
`gate.skipped` event has no dedicated `campaign show` rendering.

## Open questions

None.
