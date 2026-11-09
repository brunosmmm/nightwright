---
id: SPEC-0110
title: "Desk idea table: shared column planner, in-app filters, sorting, full-width mode"
status: superseded
superseded_by: SPEC-0495
owner: user
created: 2026-07-30
updated: 2026-11-09
kind: feature
depends_on: [SPEC-0100, SPEC-0105, SPEC-0109]
milestone: "M5: interactive desk"
source_idea: IDEA-140
tags: [tui, ideas, table, filters]
---

## Context

From `IDEA-140`, reported from real use: the desk list does not show an idea's project, and
filters and sorting cannot be changed once the desk is open.

**This revises two decisions I made, and says so.** Neither spec is superseded — SPEC-0100's
desk design otherwise still ships, and marking it superseded would make the ledger read as
though the whole read-only desk had been thrown out.

1. [SPEC-0100](./0100-read-only-ideas-list-and-detail-desk.md) deliberately **dropped `project`**
   from the list ("scanning a column of bare ids is useless; both already appear in the detail
   pane"). Defensible then. [SPEC-0109](./0109-found-another-shortcoming-when-capturing-ideas.md)
   has since made project something you *set from the desk*, so hiding it is now wrong.
2. SPEC-0100 kept filters as `wt tui` flags because "in-app filter widgets would be the chrome
   SPEC-0098 forbids" — an appeal to
   [SPEC-0098](./0098-light-ideas-tui-beside-classic-cli.md)'s *"minimal chrome; no dashboard
   widget sprawl"* invariant. In practice that forces a quit-and-relaunch cycle to re-filter a
   triage desk. **The human explicitly overrode this.** Restraint is re-read as "no sprawl",
   not "no controls": one reusable bottom strip and a handful of hotkeys, no dashboard.

**Root cause of the column problem is not the missing column.** `report.py` already has a
responsive planner the desk ignores:

- `_IDEA_META_COLS` — per column `(header, min_width, cap)`.
- `_meta_widths` — each column sized to its widest cell, bounded by `cap`.
- `_idea_table_widths` — squeezes **capped** columns (kind/project/next) toward `min_width` to
  protect a headline floor, reclaiming width instead of letting Rich crop mid-cell.

The desk replaced all of it with a hardcoded `_META_COST = 34`. So "add a project column" is
really "adopt the planner the CLI already has".

**Current parity gap, both directions.** CLI columns: `score/id/state/pri/kind/project/updated/next`.
Desk columns: `id/st/p/k/q/upd/idea`. The desk has `q` (open-question count, SPEC-0103) which
the CLI lacks; the CLI has `project` which the desk lacks.

## Goals / Non-goals

**Goals**
- The desk list shows an idea's project, via the shared planner rather than a new magic number.
- Filters (state/kind/tag/project/workstream/priority) can be changed **in-app**.
- Sort field and direction can be changed in-app, with hotkeys.
- A **full-width list mode**, toggled by a hotkey, so the table is not permanently fighting for
  half the terminal.
- The status line shows the **active filter set, sort and mode** — never invisible state.
- The **classic `wt ideas` table gains the `q` column too**, completing column parity.

**Non-goals**
- **Saved views / filter presets** — explicitly declined; they need naming, config and
  persistence for a triage surface that is already fast to re-filter.
- New filter *semantics*: everything maps to existing `collect_idea_tasks` / `search_idea_tasks`
  arguments. The desk invents no matching.
- Column configuration by the user.
- Filtering the hub tab.

## Decision

**Share one column planner.** `_idea_table_widths` currently reads the module-global
`console.width`; refactor it to take an explicit `width` (defaulting to `console.width`, so
every CLI caller is unchanged) and have the desk pass its **pane** width. One planner, two
surfaces, and the desk's headline floor becomes a real constraint instead of `_META_COST`.

**Filters and sort are in-app**, on the bottom strip that SPEC-0105 already established for
search — not a modal, which would cover the very list it is filtering.

## Design

**Columns.** Desk adopts `_IDEA_META_COLS` + the shared planner, gaining `project`. `q`
(SPEC-0103) is added to the column table with its own `min_width`/`cap` so the planner can
squeeze it like any other capped column.

**`q` in the classic CLI** (resolved 2026-07-30, after acceptance — see Open questions). The
count is already in `wt ideas --json` via `idea_row`; only the Rich table omits it, so this is a
column-plan change rather than a data change. It follows the precedent `score` already sets:
**the column appears only when it has something to say** — `score` is search-mode only, and `q`
is omitted when no visible idea has open questions, rather than printing a column of zeros into
a table that is already fighting for width. Blank cell for zero when the column is shown.

**Layout modes.** `z` toggles `split` (list + detail) and `wide` (list only). The planner
re-runs on toggle and on resize, so extra width becomes columns rather than padding.

**Auto-drop — added during implementation** (see *Decided during implementation*). Squeezing
alone was not enough: at a 60-column pane the full seven-column plan left the heading **5**
characters, worse than before the overhaul. `plan_desk_columns` therefore extends the shared
planner with *drop after squeeze* — shedding columns in a declared order until the heading
regains its floor of 12.

- `DROP_ORDER = kind, pri, state, project, q`. `kind` is a glyph the detail pane repeats;
  `pri` is usually blank; `state` is partly carried by colour. `project` and `q` are late
  because they are the two things you cannot recover without opening the idea.
- `id` and `updated` never drop: `id` is how you refer to a row, `updated` drives the default
  sort.
- Consequence worth stating: **surplus width goes to columns, not the heading, until all seven
  fit.** The heading sits at its floor through the middle of the range and only grows once
  nothing is being dropped. Measured on a 60-column pane: all-seven → 5, minus `kind` → 11,
  minus `kind`+`pri` → 12.

**Filters.** `f` opens the filter bar (bottom strip, same widget pattern as `/`). It accepts
space-separated `key=value` tokens over the existing filter vocabulary; an empty bar clears all.
Unknown keys report in the bar rather than filtering to nothing silently.

- *Tradeoff, stated:* typing `project=Example` is less discoverable than a chooser. It is chosen
  anyway because it composes several filters in one action without a wizard, and because the
  status line always shows the resulting set, which teaches the vocabulary in use. If typing
  proves annoying, an `m`-style field→value chooser is the fallback — a later spec, not a knob.

**Sorting.** `o` cycles the sort field (freshness → priority), `O` toggles direction. Both map
straight to `collect_idea_tasks(sort=…, desc=…)`, i.e. `--sort` / `--desc` parity.

**Precedence (proposed by me — the human had no answer; overturnable).**

1. Filters **AND** together and apply in **both** modes (already true today).
2. An active **query widens the corpus to open+closed**, and therefore overrides the non-active
   toggle — exactly mirroring `wt ideas --query`, which SPEC-0086 documents as "implies full
   corpus". No new rule is invented for the desk.
3. Because that one interaction is genuinely surprising, the status line **must say so** when it
   is in effect, rather than leaving the user to infer why a closed idea appeared.

**Status line.** Always shows: count · visibility (or the search override) · active filters ·
sort field/direction. Invisible filter state is how a user ends up staring at a list wondering
where a row went.

## Alternatives considered

- **A desk-specific column planner** — rejected: it is what produced `_META_COST = 34` and the
  parity gap in the first place.
- **A modal filter screen** — rejected: it covers the list being filtered, the same objection
  that ruled out a modal search in SPEC-0105.
- **Per-field cycling hotkeys for every filter** — rejected: six filters would mean six more
  bindings, which *is* the sprawl the restraint invariant is actually about.
- **Marking SPEC-0100 `superseded`** — rejected: most of it still ships; the timeline stays more
  faithful with a documented partial revision.
- **Saved views** — declined by the human.

## Acceptance criteria

- [x] The desk list shows a `project` column, sized by the shared planner.
- [x] `wt ideas` shows a `q` column when any listed idea has open questions, and omits it
      entirely when none do.
- [x] `_idea_table_widths` is shared by CLI and desk; CLI output is unchanged.
- [x] Filters can be set and cleared in-app and match `wt ideas` results for the same filters.
- [x] Sort field and direction can be changed in-app, matching `--sort` / `--desc`.
- [x] `z` toggles full-width and split, and columns re-plan for the new width.
- [x] The status line shows active filters, sort, mode, and flags the query/visibility override.
- [x] No saved views; no new filter semantics.

## Test plan

**Executed 2026-07-30.** `tests/test_desk_table.py` — 18 tests.

- **Filter expression:** round-trip parse/format; malformed, unknown-key and empty-value tokens
  are each *reported* rather than dropped; empty clears; a guard that every key in
  `FILTER_KEYS` is genuinely a `collect_idea_tasks` argument, so the UI cannot invent a filter.
- **Shared planner:** `_idea_table_widths` honours an explicit `width` and still defaults to
  `console.width` for CLI callers; `q` is registered in `_IDEA_META_COLS` and is *capped*, i.e.
  shrinkable.
- **Auto-drop:** a wide pane keeps all seven columns; a narrow one sheds them, `kind` first, and
  the heading keeps its floor afterwards; `id` and `updated` survive every width from 20 to 100.
- **Parity with the CLI — the load-bearing tests:** in-app filters produce *the same ids* as the
  equivalent `wt ideas --project/--kind/--priority --json`, and the sort hotkeys produce the same
  order as `--sort` / `--desc`.
- **CLI `q` column:** shown while an idea has open questions, gone once the last one is resolved.
- **Status line:** filters, sort, direction and mode all appear; the query case announces
  `all states (search)`, the visibility override.
- **Pilot:** the filter bar applies and clears; `o`/`O` cycle and reverse (asserted against the
  literal reversed order); `z` hides the detail pane and regains every column; the `project`
  column is on screen, clipped in split and full in wide.
- **Manual verification:** live org at 120×16 — `f` then `project=Meta-Tools` narrowed 41 → 13
  rows with the status line reading
  `13 shown · active only · project=Meta-Tools · sort freshness`, and the split pane auto-dropped
  to `id state q project updated idea`.
- **Regression guard:** `uv run pytest` — 949 passed. SPEC-0075 CLI width tests green, so the
  planner refactor is a genuine no-op for `wt ideas`.

One earlier test changed premise, deliberately:
`test_list_pane_carries_headings_and_grows_them_with_width` compared 80 vs 140 columns. Under
auto-drop the heading is pinned at its floor through that range, so it now compares 80 vs 240
and additionally asserts the floor is never breached — the invariant that actually matters.

## Decided during implementation

Rendering the first cut revealed that squeezing alone leaves a 5-character heading at a
60-column pane. Presented three options with measured numbers; **auto-drop by priority** was
chosen over fixed per-mode column sets and over widening the split pane.

## Rollout / sequencing

1. Refactor `_idea_table_widths` to take an explicit width (no behaviour change; CLI green).
2. Desk adopts the planner and gains `project`.
3. Full-width mode (`z`), since filters/sort are easier to judge with room.
4. Filter bar, then sort hotkeys, then the status line surfacing all of it.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)

**Resolved after acceptance** — recorded here rather than edited away, so the timeline stays
faithful:

- *Should the classic CLI gain the `q` column for true parity?* **Yes** (2026-07-30). It was
  accepted as a non-goal pending the human's answer; once answered it moved into scope, since
  SPEC-0110 already owns the shared column planner and splitting it out would fragment one
  change across two specs.
