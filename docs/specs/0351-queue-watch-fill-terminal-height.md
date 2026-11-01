---
id: SPEC-0351
title: "queue watch: size the bottom sections to the terminal height"
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, dashboard, ux]
depends_on: [SPEC-0326]
---

## Context

IDEA-624. `nw queue watch` caps each bottom section at a fixed row budget
(`SECTION_ROWS`, SPEC-0326) and shows `+N more` even when the terminal has many unused rows.

## Goals / Non-goals

**Goals**
- Rows the active region leaves unused go to sections that truncate; `+N more` appears only
  when a section truly lacks room.
- Small terminals and non-tty `--once` output are unchanged.

**Non-goals**
- Changing the active-card density rules or the section order.

## Decision

`SECTION_ROWS` becomes each section's minimum. After the active region picks its density,
the spare lines (`budget - lines(active)`) are handed out round-robin by a pure function
`allocate_rows`, and the frame is re-measured so it never overflows `height`. Backlog and
Recently delivered share a lane when side by side (one line grows both).

## Design

- `allocate_rows(counts, minimums, spare, lanes)` in `agentic/cli_queue.py`.
- `_bottom_sections(..., extra)` applies the limits; `_dashboard(..., fill=True)` computes the
  spare and shrinks it until the measured frame fits.
- `queue watch --once` without a terminal and without `--height` passes `fill=False`
  (the fixed budgets, as before).

## Alternatives considered

- **Proportional split** — round-robin is simpler and fair; sections with few items are
  satisfied first by construction.

## Acceptance criteria

- [x] On a tall terminal, sections show all items (no `+N more`) when they fit.
- [x] On a short terminal, every non-empty section keeps at least its previous minimum.
- [x] The frame never exceeds `height` lines because of the extra rows.
- [x] Non-tty `--once` without `--height` renders as before.

## Test plan

- **Automated:** `tests/test_agentic_watch_height.py`: unit tests for `allocate_rows`;
  render at heights 30, 60, 120 asserting `+N more` presence/absence and line count.
- **Regression:** `tests/test_agentic_dashboard_journey.py` and affected tests.

## As shipped

Shipped as designed: `allocate_rows` plus the measured fill in `_dashboard`; `--once` without
a terminal keeps the fixed budgets.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass (textual-dependent TUI tests cannot collect in this env, pre-existing).
- [x] No regressions (full suite left to the close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).
