---
id: SPEC-0453
title: Queue watch shows the campaign title inside the card
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [cli, queue]
depends_on: [SPEC-0326]
---

## Context

`nw queue watch` draws each active campaign as a rounded card. [SPEC-0326](./0326-queue-watch-redesign-fixed-sections-adaptive-cards.md) put the campaign id, the spec title, the state, the loop and the project on the card's border. A campaign id such as `IDEA-1023-20261107-0799` uses most of that border, so the title is cut to a few characters (`writing… Run…`). The border is then an id and a state, and the words that say what the campaign is are gone.

## Goals / Non-goals

**Goals**
- The human title (the idea heading, or the spec title once one exists) is a row inside the full card, cut only by the card's own width.
- The border keeps the campaign id, state, age, loop and project.
- The compact card gives the title its own line, then the same identity line, then the track and the why line.
- The one-line row fallback shows the title in the flexible column, ahead of the why text.

**Non-goals**
- Changing `nw queue list`, the bottom tables, or the dashboard JSON.
- Rewriting SPEC-0326. The rest of that layout stays.

## Decision

The border is identity and status. The title is the first row of the card body. A spec id, when one exists, leads that row because it is short. The `writing…` prefix is not repeated there. The spec-review row already says when a spec is being written.

## Design

`_card_title` in `src/nightwright/agentic/cli_queue.py` no longer appends the spec cell. `_human_title` is the body text: `spec_title`, with `spec_id` in front when present.

`_active_panel` adds a `title` row before `track`. `_card_compact` is four lines: title, identity, track, why. `_cards_rows` puts the title in the flexible column and appends the why text after it.

`_idea_spec_cell` and `_spec_cell` existed only to build the old border fragment. They go away with it.

## Alternatives considered

- **Widen the border and keep the title there.** The campaign id is long on every idea campaign. The leftover width is still a few characters on a normal terminal.
- **Drop the campaign id.** The id is how a campaign is addressed. It stays on the border.

## Acceptance criteria

- [x] A full watch card's border contains the campaign id, state, loop and project, and does not contain the title text.
- [x] The same card's body contains the idea heading while no spec exists, and the spec title once one does, not cut to 60 characters before layout.
- [x] A compact card's first line is that title. The identity line is the next line.
- [x] The row fallback's flexible column starts with the title.

## Test plan

- **Automated tests:** `tests/test_agentic_dashboard_spec_phase.py` asserts the heading is inside the panel and `writing…` is not the border fragment. `tests/test_agentic_dashboard_journey.py` asserts the compact grid is four lines per card and still loses to the full grid, and that a tight budget still picks the row style.
- **Manual verification:** `nw queue watch --once` on a live queue shows the idea heading inside the SPEC_WRITING card.
- **Regression guard:** campaign id, `kind split`, track and spec-review row stay on the card.

## Rollout / migration

None. The next `nw queue watch` process reads the new layout. The runner does not need a restart.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the affected tests pass. The full suite is the sentinel's.
- [x] No regressions in the affected tests.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

None.
