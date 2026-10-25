---
id: SPEC-0196
title: "set_property reuses an existing drawer that sits after body text"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M4: cli ergonomics"
kind: feature
tags: [org, writes]
source_idea: IDEA-055
---

## Context

Promoted from bug `IDEA-055`. `org_write._inject_property` (`src/wt/org_write.py`) looks for a `:PROPERTIES:` drawer only immediately after the headline and its planning lines. When an entry's drawer comes after body text (hand edits, a test fixture in `test_export.py`), which orgparse still reads, it inserts a second drawer right after the headline. The entry then has two drawers: the new property in the first, and `:ID:`/`:CREATED:` in the second. SPEC-0190's `_drawer_id` had to be made lenient to cope.

## Goals / Non-goals

**Goals**
- `_inject_property` and `_remove_property` find the entry's existing `:PROPERTIES:` drawer anywhere before the next headline and edit it in place; they create a drawer (after the headline and planning lines) only when the entry has none.

**Non-goals**
- Moving a misplaced drawer to the canonical position.
- Merging entries that already have two drawers.

## Decision

Search for the drawer between the headline and the next headline, as `_drawer_id` already does.

## Design

- New helper `_find_drawer(lines, idx) -> tuple[int, int] | None`: (start, end) indexes of the first `:PROPERTIES:` … `:END:` block between `lines[idx]` and the next headline; None if absent.
- `_inject_property`: when `_find_drawer` returns a block, replace or insert inside it; otherwise insert a new drawer at the canonical position (after the headline and planning lines), as today.
- `_remove_property` uses the same lookup.
- `_drawer_id` keeps its own scan (it reads any drawer) but may reuse `_find_drawer`.

## Alternatives considered

- **Normalize by moving the drawer** — rewrites user text unasked; out of scope.

## Acceptance criteria

- [x] `set_property` on an entry whose drawer follows body text adds the key to that drawer; the entry still has exactly one `:PROPERTIES:` line.
- [x] `clear_property` removes a key from such a drawer.
- [x] An entry without a drawer still gets one right after the headline (and after SCHEDULED/DEADLINE/CLOSED lines).
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_org_write.py` (or a new `tests/test_org_drawer.py`): fixture with body text before the drawer → `set_property` → one drawer containing both the old and new keys; `clear_property` on it; an entry with no drawer; an entry with planning lines. `tests/test_export.py::test_scaffold_outbound_from_idea_prefills_and_links_back` still passes, and its idea ends with one drawer.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest -q tests/test_org_drawer.py` — 5 passed (drawer after body text: set/replace/clear keep one `:PROPERTIES:`; no-drawer entries, with and without planning lines, get a drawer at the canonical position; `set_property`/`clear_property` end to end).
- `uv run pytest -q -x` — 1663 passed, 1 skipped (includes `test_export.py::test_scaffold_outbound_from_idea_prefills_and_links_back`).
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py` — exit 0.

## Rollout / migration

Ships through the agentic queue (balanced profile). No data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
