---
id: SPEC-0195
title: "wt spec new drops org drawers from the scaffolded Context"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M4: cli ergonomics"
kind: feature
tags: [specs, promotion]
source_idea: IDEA-053
---

## Context

Promoted from bug `IDEA-053`. `wt spec new --from-idea` prefills the spec's Context with the idea's org body (`specs._idea_context`, `src/wt/specs.py`). It copies every line from the headline to the next headline and runs it through `org_markup.from_org_body`, which converts emphasis and src/example blocks but leaves org drawers alone. Every captured idea has a `:PROPERTIES:` drawer (`:ID:`, `:CREATED:`, `:UPDATED:`, `:PROJECT:`, …), and a clocked one has a `:LOGBOOK:` drawer with `CLOCK:` lines. Both landed verbatim in SPEC-0185 and SPEC-0190.

## Goals / Non-goals

**Goals**
- The scaffolded Context contains no org drawer (`:PROPERTIES:` … `:END:`, `:LOGBOOK:` … `:END:`, or any `:NAME:` … `:END:` drawer) and no planning lines (`SCHEDULED:`, `DEADLINE:`, `CLOSED:`).
- Everything else is unchanged: the `** Summary` / `** Log` subheadings stay (SPEC-0030 keeps them on purpose), and so do emphasis conversion and src fences.

**Non-goals**
- Rewriting the org subheadings as Markdown headings.
- Changing what the idea itself contains.

## Decision

Strip drawers and planning lines from the idea body in `_idea_context`, before `from_org_body`.

## Design

- New helper `org_markup.strip_drawers(text) -> str`: removes every block from a line matching `^\s*:[A-Z_]+:\s*$` (opening drawer name, e.g. `:PROPERTIES:`, `:LOGBOOK:`) through the next line matching `^\s*:END:\s*$`, plus lines matching `^\s*(SCHEDULED|DEADLINE|CLOSED):`. It leaves property-looking lines outside drawers alone, and lines inside `#+begin_src`/`#+begin_example` blocks are never touched. An unterminated drawer is left in place (don't eat the rest of the body).
- `specs._idea_context` calls `strip_drawers` on the joined body before `from_org_body`.

## Alternatives considered

- **Strip in `from_org_body`** — that function is also used for Summary/Log enrichment text, where drawers never appear; keeping the change at the one call site that reads raw subtrees is narrower.

## Acceptance criteria

- [x] `wt spec new --from-idea` on an idea with a `:PROPERTIES:` drawer and a `:LOGBOOK:` CLOCK entry produces a Context without `:PROPERTIES:`, `:LOGBOOK:`, `:END:`, `:ID:` or `CLOCK:` lines.
- [x] Summary text, `** Summary` / `** Log` headings and src fences are preserved; drawer-looking text inside a src block is preserved; an unterminated drawer is not removed.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_org_markup.py` (or the existing markup test module) — `strip_drawers` on a body with PROPERTIES + LOGBOOK drawers, a CLOSED line, a src block containing `:END:`, an unterminated drawer. `tests/test_explore.py` (promote tests) — a promoted idea that was clocked in/out has none of the drawer lines in its Context and keeps its Summary.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest -q tests/test_org_markup.py tests/test_explore.py` — new `strip_drawers` unit tests (PROPERTIES + LOGBOOK + CLOSED/SCHEDULED removal, drawer text inside a src block kept, unterminated drawer kept) and `test_promote_context_drops_drawers` (clocked-in/out idea keeps Summary, loses all drawer lines) pass.
- Full `uv run pytest`: 1661 passed, 1 skipped. `python3 tools/spec_lint.py`: exit 0 (ledger regenerated).

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
