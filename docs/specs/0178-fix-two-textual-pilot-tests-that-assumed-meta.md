---
id: SPEC-0178
title: "Fix two Textual pilot tests that assumed Meta-Tools is the first chooser entry"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M0: process"
kind: feature
tags: [tests, hygiene, tui]
source_idea: IDEA-042
depends_on: [SPEC-0165]
---

## Context

Promoted from idea `IDEA-042`.

[SPEC-0165](./0165-repair-test-drift-from-demo-name-scrub-and-spec.md) repaired
`test_known_project_choices_comes_from_the_registry` after the demo-name scrub renamed a
mapping bucket to `Example`, which now sorts before `Meta-Tools` in `projects_payload`
(`rules.py:72`) and therefore in the desk's project chooser. Two **Textual pilot tests** in
`tests/test_idea_project.py` carry the same assumption — one `j` from `(none)` lands on
`Meta-Tools` — but only run when the `tui` extra is installed. Main's own venv had no
`textual`, so the earlier "green" baselines skipped them; the SPEC-0172 builder's worktree
(synced with `--extra tui`) surfaced them, and main's venv now has textual too, so main
shows 2 failures. SPEC-0177's dogfood gate syncs `--extra tui`, so every campaign would
fail on them.

## Goals / Non-goals

**Goals**
- Both pilot tests pass with `textual` installed; main is fully green again.
- Keep the tests' intent: first real chooser entry sets the project; `(none)` clears it.

**Non-goals**
- Changing chooser order or any desk behavior.

## Decision

Expect `Example` (the first real entry in sorted order) where the tests expected
`Meta-Tools`, and fix the comments. No product code changes.

## Design

`tests/test_idea_project.py`: in `test_capture_flow_offers_a_project_and_can_decline` and
`test_metadata_chooser_sets_and_clears_project`, the assertion after the single `j` becomes
`== "Example"`; the `(none) → Meta-Tools` comment becomes `(none) → Example (first real
project, sorted)`. The later "walk back up to (none)" loop is unaffected.

## Alternatives considered

- **Pin a mapping order in the fixture** — rejected. The registry is sorted by design
  (SPEC-0139); the tests should follow it.
- **Skip the pilot tests** — rejected. They are the only end-to-end check of the chooser.

## Acceptance criteria

- [x] Both tests pass with `textual` installed; no `xfail`/`skip` added.
- [x] No file under `src/` changed by this spec.
- [x] Full `uv run pytest` green (0 failed) on main's venv with the `tui` extra;
      `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** the two tests themselves, then the full suite.
- **Manual verification:** none needed.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest -q tests/test_idea_project.py`: 20 passed (both pilot tests included; main's
  venv now has textual 8.2.8).
- Full `uv run pytest` on main: 1339 passed, 1 skipped, **0 failed**.
- `git diff --name-only -- src/` empty for this change.

## Clock Log

Internal work: clock on IDEA-042 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Test-only.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
