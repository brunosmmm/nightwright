---
id: SPEC-0115
title: "Desk tree mode: a real collapsible tree, not indented rows"
status: superseded
superseded_by: SPEC-0495
owner: user
created: 2026-07-31
updated: 2026-11-09
kind: feature
depends_on: [SPEC-0114]
milestone: "M5: interactive desk"
source_idea: IDEA-153
tags: [tui, tree, bug]
---

## Context

`IDEA-153`, reported from real use: the desk's tree mode "does not work at all".

**Confirmed in a real terminal** (pty + `pyte`, 120×32, not a headless pilot): `a` then `g`
produces a **reordered flat list** — no group headers, no visible nesting, no expand/collapse.

**This is an assistant defect against an approved design, not a misunderstanding.** The mock
approved on IDEA-141 showed `▾ Meta-Tools` group headers with disclosure triangles and indented
children. During SPEC-0114 implementation the first attempt emitted `task=None` header rows,
broke `paint`, and was replaced with "leaves only, indentation instead of headers", justified as
*the project column already labels the group*. That contradicted the approved mock and **was
never flagged**; expand/collapse was never built at all. SPEC-0114's acceptance criterion
("the desk can toggle tree mode; indentation is charged against the heading budget") was met
literally while missing the point.

**SPEC-0114 stays `done`.** Its CLI tree, the grouping model and `build_idea_tree` are correct
and shipped; this spec revises only its **desk** decision.

## Goals / Non-goals

**Goals**
- Tree mode shows **levels that are visibly nested and can be expanded and collapsed**.
- Group (project) and epic nodes are collapsible; ideas are leaves.
- Selecting a node drives the detail pane; mutations still act on the selected idea.
- Toggling back restores the flat table unchanged.

**Non-goals**
- Metadata **columns** inside tree mode — a `Tree` node is one label (see Decision).
- Changing `build_idea_tree`, the CLI `--tree`, or any JSON.
- Collapse-state persistence across sessions.
- Tree in the hub tab.

## Decision

**Use Textual's `Tree` widget** for tree mode, swapping it for the `DataTable` in the same pane,
rather than faking nesting in a flat table. `Tree` provides expand/collapse, disclosure markers,
depth guides and node events natively — the previous attempt failed precisely because a
`DataTable` has no concept of a parent.

**Accepted cost:** a `Tree` node is a single label, so the metadata columns cannot be columns in
tree mode. State, priority and open-question count are folded into the node label instead; the
flat table remains the view for column scanning. That is the honest trade for real nesting, and
it is stated here rather than discovered later.

## Design

- A `Tree` (`#tree`) lives beside `#list` in the list pane; exactly one is displayed.
  `action_toggle_tree` swaps visibility and focus.
- Populated from `build_idea_tree` (unchanged): project nodes → epic nodes → idea leaves.
  Project nodes carry their descendant count; epic nodes carry theirs.
- Node labels are compact: `IDEA-128  SPECCED  ·3q  Light ideas TUI…`, with context ancestors
  (SPEC-0114's `matched=False`) rendered dim.
- Idea nodes carry their id in `node.data`; `selected_id` reads from whichever widget is active,
  so every existing mutation, the clock and the questions modal keep working untouched.
- `Tree.NodeHighlighted` drives the detail pane, mirroring `DataTable.RowHighlighted`.
- Projects start **expanded** and epics **collapsed**: the point of the mode is to see structure
  without 151 rows, and an epic's children are the detail you drill into.

## Alternatives considered

- **Keep indented DataTable rows** — rejected: that is the shipped behaviour being reported as
  broken, and a table cannot collapse.
- **Synthetic header rows in the DataTable** — rejected again: it forces every mutation path to
  special-case a row with no idea behind it, which is what broke the first attempt, and still
  offers no collapse.
- **A third pane** — rejected: sprawl, and the list pane is where triage happens.
- **Keep columns by using a `DataTable` per group** — rejected: many widgets, no shared cursor.

## Acceptance criteria

- [x] `g` shows a tree with project → epic → idea levels, visibly indented.
- [x] Project and epic nodes expand and collapse.
- [x] Highlighting an idea node updates the detail pane.
- [x] Mutations, clock and the questions modal act on the highlighted idea in tree mode.
- [x] `g` again restores the flat table with its columns intact.
- [x] Every idea in the flat view is reachable in the tree — none lost.
- [x] Verified in a **real terminal**, not only a headless pilot.

## Test plan

**Executed 2026-07-31.** `tests/test_idea_tree.py` (+6 tests) and a new
`tests/test_tui_real_terminal.py`.

- **Structure:** project → epic → idea asserted through the widget's own parentage; epics start
  collapsed; `expand()` increases the visible line count and `collapse()` restores it — the
  behaviour a padded `DataTable` never had.
- **Nothing lost:** every id in the flat view is reachable in the tree.
- **`j`/`k` routing regression:** they were hardcoded to the `DataTable`, so in tree mode they
  moved the *hidden* table — the detail pane followed an invisible row and the tree cursor never
  moved, which made `Enter` look like it did not expand. Now asserted to move the tree cursor.
- **Opens with a selection:** a fresh `Tree` has `cursor_line == -1`, which left the detail pane
  showing a stale idea.
- **Mutations follow the tree:** `selected_id` reads from the active widget; `x` (mark-explored)
  lands on the highlighted idea.
- **Toggling back** restores the table *with all its columns* — which caught a real bug: painting
  immediately after un-hiding the table plans columns against width 0 and auto-drops half of
  them. Paint is now deferred to after layout, the same trap as SPEC-0100's pre-layout paint.
- **Real terminal (`pyte` + pty):** spawns `wt tui` against a throwaway org and asserts on the
  **composited** screen — disclosure markers present in tree mode and absent in flat mode, the
  epic's label starting at a greater column than its project, and `Enter` revealing the child.
  This is the check that would have caught the shipped SPEC-0114 desk mode.

Two of my own test bugs surfaced while writing that last one, both worth recording:

1. Nesting was measured with `lstrip()`. Textual draws depth with guide glyphs (`├──`), not
   whitespace, so every level reported column 0. It now measures the label's column.
2. The fixture ideas had no `:UPDATED:` stamps, so freshness tied and fell back to heading — the
   epic sorted *after* the loose idea, `Enter` hit a leaf, and the test "proved" expansion was
   broken when it was not. Stamps make the order deterministic.
   The harness also typed unknown key *names* literally (sending `"space"` pressed `s,p,a,c,e`
   and opened the summary editor); it now refuses an unrecognised name.

**Colour (reported separately as "no color at all in tree view").** The first tree styled every
label `dim` or not at all, discarding SPEC-0082's state hue, SPEC-0085's kind glyph and
SPEC-0110's yellow question count. Node labels now reuse the same helpers the flat table uses, so
there is one definition of how an idea looks. Verified on the composited screen: `INCUBATE`
renders cyan `58d1eb`, `DROPPED` red `ae0d4b`, the question count orange `fd971f`, headings body
colour.

**That colour test was vacuous twice before it worked**, and the sabotage check is the only reason
it did not ship that way:

1. First version counted distinct colours across the whole screen — the *detail* pane (SPEC-0107
   lexing) supplied them, so it passed on a monochrome tree.
2. Scoping to the left pane was still not enough: the tab bar, tree guides and footer keys give
   even a monochrome tree ~10 colours, and the one state that looked distinct was distinct only
   because it sat on the **cursor row**, whose highlight recolours it.
3. It now compares two **non-cursor** rows with different states, and a state against body text.
   Confirmed failing on the monochrome build, passing on the fix.

- **Regression guard:** `uv run pytest` — **1032 passed**, including SPEC-0110/0113/0114 suites.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed **in a real terminal**.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
