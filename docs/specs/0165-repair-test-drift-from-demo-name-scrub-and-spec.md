---
id: SPEC-0165
title: "Repair test drift from demo-name scrub and SPEC-0135"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M0: process"
kind: feature
tags: [tests, hygiene, bug]
source_idea: IDEA-011
---

## Context

Promoted from idea `IDEA-011`.

On clean HEAD `740e9eb`, `uv run pytest` has **15 failures** (1076 passed, 82 skipped). That
blocks closing any spec, because the DoD in `AGENTS.md` needs a green suite (SPEC-0164 is
waiting on this). Every failure is drift in tests or docs. No product code is wrong. There
are six causes:

| Tests | Cause |
|---|---|
| `test_outbox_migrate.py` ×7 | The history-wide demo-name scrub (its token replacement shows in commit trailers as `Co-audemoed-by`) mapped the rename's **old and new** project names both to `DemoCo`. `plan_project_rename` correctly raises "old and new project names are the same". |
| `test_routing.py` ×2 | The scrub replaced the expected outbound id prefix with `DEMO-`. The prefix is derived from the project name (`_proj_code("AI-workstreams")` = `AIWORKSTREAMS`, `src/wt/specs.py:357`). |
| `test_idea_project.py::test_known_project_choices_comes_from_the_registry` | A mapping bucket renamed to `Example` now sorts before `Meta-Tools` (`projects_payload` sorts names, `src/wt/rules.py:72`). |
| `test_fit_log.py` ×3 | The fixture's `org_idea_keywords` predates [SPEC-0135](./0135-shipped-terminal-fix-outbound-reconcile-force.md) (no `SHIPPED`/`RESEARCHED`), so `pull_status` cannot move EXPORTED → SHIPPED. The tests still expect `EXPORTED` after archive and the old "could not auto-archive" warning. |
| `test_specs_dir.py::test_no_test_fixture_omits_specs_dir` | The meta-scan flags any test file whose text contains `org_ideas_file`. `test_docs_captures.py` uses it only as a docs phrase it looks for. |
| `test_docs_product_guides.py::test_guides_cover_key_commands` | `docs/product/guides/multi-project.md` shows `wt projects list --json` (2 uses repo-wide). The canonical form is `wt projects --json` (18 uses, including skills). |

## Goals / Non-goals

**Goals**
- `uv run pytest` is green on main.
- Each fix keeps the test's original intent: rename tests rename between two distinct names,
  routing tests check the derived prefix, and fit-log tests check SPEC-0135 behavior.

**Non-goals**
- Any product code change.
- Auditing the rest of the history-wide scrub for collisions the suite doesn't catch.
  Captured separately if wanted.

## Decision

Repair each test/doc in place, matching current shipped behavior and the governing specs
(SPEC-0159 rename, SPEC-0020 routing, SPEC-0139 chooser, SPEC-0135 SHIPPED, SPEC-0079
specs_dir guard, SPEC-0143+ product docs). No product code changes.

## Design

- `tests/test_outbox_migrate.py`: the rename target becomes `DemoLabs`, the source stays
  `DemoCo`. Each assertion is assigned by role: before the rename / old name → `DemoCo`;
  after the rename / new name → `DemoLabs`. Conflict fixtures carry both keys
  (`{"DemoCo": {}, "DemoLabs": {}}`).
- `tests/test_routing.py`: expect `AIWORKSTREAMS-`, the prefix derived from `AI-workstreams`.
- `tests/test_idea_project.py`: expect `["Example", "Meta-Tools"]` (sorted).
- `tests/test_fit_log.py`: fixture keywords match `DEFAULT_CONFIG` (`… EXPORTED, SHIPPED,
  DROPPED, RESEARCHED`). The archive test expects `SHIPPED` after the pull; the no-linked-idea
  test expects "could not advance linked idea to SHIPPED", the same as `tests/test_export.py`.
- `tests/test_specs_dir.py`: the marker becomes the config-key form, regex
  `org_ideas_file["']?\s*:`. It matches Python dict keys and YAML keys (the
  `test_tui_real_terminal.py` fixture) but not a bare string mention.
- `docs/product/guides/multi-project.md`: `wt projects --json`.

## Alternatives considered

- **Mark the tests xfail** — rejected. It hides real drift and leaves the DoD gate
  meaningless.
- **Change the test to accept `wt projects list --json`** — rejected. The short form is what
  skills and WORKFLOW teach, and the guide should match them.
- **Add `specs_dir` text to `test_docs_captures.py`** — rejected. That games the scan instead
  of fixing its false positive.

## Acceptance criteria

- [x] All 15 listed tests pass, with no `xfail`/`skip` added.
- [x] `test_outbox_migrate.py` renames between two distinct names and asserts both
      the old-name absence and the new-name presence.
- [x] The meta-scan still flags a fixture that has an `org_ideas_file` key and no `specs_dir`
      (proved with a temporary probe file).
- [x] No file under `src/` changed by this spec.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** run the six files, then the full suite (`uv run pytest`).
- **Manual verification:** add a probe test file with a dict-key `org_ideas_file` and no
  `specs_dir`, check that `test_no_test_fixture_omits_specs_dir` fails and names it, then
  delete the probe. Check `git diff --stat -- src/` is empty for this change.
- **Regression guard:** full suite. Compare the pass count against the 1076-pass baseline plus
  the 15 repaired tests.

### Test record (2026-10-25)

- **Automated:** the six files give 57 passed, 3 skipped (optional TUI). The full
  `uv run pytest` gives **1141 passed, 82 skipped, 0 failed**: the 1076 baseline, the 15
  repaired tests here, and SPEC-0164's 50 new tests, all on the same working tree.
  Committed on its own, without SPEC-0164: **1091 passed, 82 skipped, 0 failed** (1076 + 15).
- **Manual:** a probe `tests/test_zz_probe_specs_dir.py` with `{"org_ideas_file": …}` and no
  `specs_dir` made `test_no_test_fixture_omits_specs_dir` fail and name the probe. The probe
  was then deleted. `git diff --name-only -- src/` lists only `src/wt/org_markup.py`, which
  belongs to SPEC-0164, so nothing under `src/` was changed by this spec.
- **As shipped:** matches Design. No deviations.

## Clock Log

Internal work: clock on IDEA-011 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Test/doc-only. No migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
