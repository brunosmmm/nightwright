---
id: SPEC-0494
title: "TUI ideas board: link an idea to its queue campaigns and open the campaign popup"
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
kind: feature
milestone: "M5: interactive desk"
tags: [tui, ideas, queue, agentic]
depends_on: [SPEC-0495, SPEC-0407]
source_idea: IDEA-1067
---

## Context

SPEC-0495 turned the `nw tui` ideas tab into a rail · Open / In flight / Built board with each
idea's real spec status. Most Meta-Tools ideas are now built through the agentic queue, and the
queue keeps every campaign's whole history (state, ledger, runs, gates, findings, mail) — shown
by the campaign detail popup (SPEC-0407, `CampaignDetail`). But nothing on the board says which
campaign built an idea: to see how IDEA-1041 was implemented the human has to switch to the
queue tab, show terminal campaigns, and hunt for `IDEA-1041-20261109-c0c6` by eye.

Real data at the time: 272 campaigns (224 MERGED, 34 ABANDONED, the rest live), all in
`<data_dir>/agentic/campaigns/<id>/state.json` — the queue never archives a campaign elsewhere,
it only garbage-collects worktrees and branches. Campaign ids are `<prefix>-<YYYYMMDD>-<hex>`;
idea campaigns carry `idea.id` (zero-padding varies: the org has `IDEA-001` and `IDEA-587`),
spec campaigns carry `spec_id` (or `spec.id`), and bare spec ids are per project (SPEC-0327:
comfort-assistant has its own `SPEC-0003`).

## Goals / Non-goals

**Goals**
- From any board item (every bucket; BUILT is the main case), see the queue campaigns that
  worked it and open one in the existing `CampaignDetail` popup with one key.
- Closing the popup returns to the board on the same idea.
- No cost per keypress or row: one campaign index per board load.

**Non-goals**
- Changing `CampaignDetail` itself, the queue tab, or the queue store.
- Campaigns of an epic's child specs on the epic idea (each child idea shows its own).
- Linking merge / fix-forward / red-main campaigns, which name no idea or spec.

## Decision

1. **Matching** (`tui/ideas_queue.py`, no Textual): a board item is linked to every campaign,
   live or terminal, that names
   - its **idea**: the campaign's `idea.id`, else `idea_id`, else an `IDEA-N` prefix of the
     campaign id itself; compared by number (`IDEA-0587` ≡ `IDEA-587`), no project check (idea
     ids are global); or
   - its **spec**: the campaign's `spec_id` / `spec.id` equals the idea's `:SPEC:` or the live
     successor it resolves to (SPEC-0495's supersede chain), keyed by `(project, spec id)` with
     a missing project on either side meaning the internal project.
   Matches are de-duplicated by campaign id and ordered newest first by `created_at`.
2. **Detail pane**: a `Queue` section under the spec block lists the matches as
   `<campaign id>  <state>  <day>` (day = close day — last state write — for MERGED /
   HANDED_OFF / ABANDONED, else the open day), at most 6 then `+k more`; its rule carries
   `w opens`. With no match it says `not built through the queue`.
3. **Key `w`** ("where was it worked"; footer label `queue run`) on the selected idea: one
   campaign opens `CampaignDetail` directly; several push the existing `ChoiceScreen` with the
   same newest-first labels; none shows a short notify and opens nothing. `w` is an idea action
   (disabled off the ideas tab and while a campaign popup is up, like `l` / `s`).
4. **Return**: dismissing the popup refocuses the board; the board is not repainted, so the
   cursor stays on the same idea.

## Design

- `src/nightwright/tui/ideas_queue.py` (new): `idea_key(text)`, `CampaignRef` (id, state, kind,
  spec_id, idea_id, project, created_at, updated_at; `terminal`, `day`, `status`, `label`),
  `CampaignIndex` (`by_idea`, `by_spec[(project, spec)]`, `from_states(states)`,
  `for_item(board_item)`), and `load_campaign_index(cfg)` over
  `agentic.store.list_campaigns(cfg, include_terminal=True)` — the store's own loader, with its
  SPEC-0372 mtime cache — reading an absent or unreadable store as empty.
- `ideas_board.Board` gains `campaigns: CampaignIndex` and `campaigns_for(idea_id)`;
  `load_board` attaches the index after `build_board`, so it is built once per board load inside
  the existing off-loop reload worker (`_reload_async`). `build_board` stays pure.
- `app.py`: `Binding("w", "queue_campaign", "queue run")`, `queue_campaign` added to
  `_IDEA_ACTIONS`; `action_queue_campaign` ignores the key while any other screen is stacked;
  `open_campaign_detail(cid, origin=self)` — the dismiss callback refocuses `#board` when the
  desk itself opened it (the queue-tab cursor restore is unchanged); `_queue_lines(refs)` +
  `_render_detail(..., queue=)` render the section.
- **Archived/closed campaigns**: `CampaignDetail` already renders terminal campaigns read-only
  (its `load_detail` marks them `terminal`; the events and mailbox tabs already open merged
  ones), so no rendering change was needed there.
- **Resilience fix in `CampaignDetail`** (found on the box gate of SPEC-0495's head: GATEJOB-1087
  failed once in `test_tui_campaign_detail.py::test_decisions_append_triggers_poll_reload` with
  `WorkerFailed: NoMatches('#campaign-timeline')`, and the sibling
  `test_mailbox_append_triggers_poll_reload` flaked earlier): a dismiss tears the screen's
  children down before `is_mounted` drops, so a reload / entry-detail worker resuming in that
  window queried a widget that was gone. `_worker_alive()` — the gate every worker tail already
  passes before painting — now also requires `#campaign-timeline` to be in the DOM, and the
  entry-detail tail catches `NoMatches` and skips the paint. This matters more now that the
  ideas board opens and closes the popup too.
- **Why `w`, not Enter**: on the board's `Tree`, Enter on a leaf and a mouse click both post
  `NodeSelected`, so binding the popup to it would open a modal on every click, and Enter on a
  search hit already means "jump to it". `w` was unbound on the desk (the mailbox tab's own `w`
  is a widget binding there and wins on that tab).

## Alternatives considered

- **Enter on an idea row** — see above: clicks would open popups and search Enter is taken.
- **Scan campaigns per keypress / per detail repaint** — 272 `state.json` reads on every cursor
  move; the per-load index costs ~0.1 s cold (~0.04 s with the store cache) once, off the loop.
- **Match only by spec id** — idea campaigns abandoned before writing a spec, and ideas whose
  `:SPEC:` names an outbound portable while the campaign built the target repo's own spec
  (IDEA-651 → COMFORTASSISTANT-0015 vs campaign spec SPEC-0003), would be missed.

## Acceptance criteria

- [x] Matching links a campaign by its idea id (suffixed campaign id, nested `idea.id`, any
      zero-padding) or by the idea's spec / live successor within the same project; matches
      are de-duplicated and newest first; no match yields an empty list.
- [x] The detail pane of every bucket's idea shows a `Queue` section listing matching campaigns
      (id, state, day) or `not built through the queue`.
- [x] `w` opens `CampaignDetail` for the one matching campaign, offers a newest-first chooser
      for several, and only notifies for none.
- [x] Closing the popup returns to the ideas board with the same idea selected and the board
      focused.
- [x] Terminal (MERGED / ABANDONED) campaigns open and render in the popup.
- [x] The campaign index is built once per board load (in the reload worker), not per key or
      row.
- [x] `CampaignDetail` workers never paint into a torn-down or uncomposed screen
      (`test_tui_campaign_detail.py` stable over 5 runs with `-n 8`).

## Test plan

- **Automated tests** — `tests/test_tui_ideas_queue.py`:
  - `idea_key` normalisation (suffix, padding, non-idea ids);
  - idea match including a campaign with no `idea` block (id prefix only);
  - spec-only match: project-scoped (another project's same bare id excluded), supersede
    successor followed, nested `spec.id`, project-less idea/campaign = internal;
  - several (deduped, newest first, open vs close day in the label) and none;
  - `load_board` attaches the index from a real `store.create_campaign` store, and an absent
    store reads as empty;
  - one Textual `run_test`: the Queue section renders, `w` opens `CampaignDetail` for the
    selected idea, Escape returns to the same idea with the board focused, several campaigns
    open the chooser newest first and the pick opens, none opens nothing.
- **Manual verification**: headless pilot on the real data (`queue_shot.py` in the session
  scratchpad): IDEA-1041's detail pane, its popup (overview + timeline), the return, a chooser
  for an idea with two campaigns, and an idea with none.
- **Regression guard**: `python3 tools/affected_tests.py --run uv run pytest -q -n 8` plus the
  touched TUI test files.

## As shipped / verification

- Shipped as designed above.
- Tests: `tests/test_tui_ideas_queue.py` 6 passed. Affected set
  (`tools/affected_tests.py --run uv run pytest -q -n 8`, 246 files): 6609 passed, 6 skipped,
  1 failed — `test_tui_campaign_detail.py::test_a_failed_paint_is_retried_by_the_next_poll`,
  raised by the shared `settle()` test helper (`tests/test_tui_queue_tab.py`): Textual's
  `wait_for_complete` re-raises `WorkerCancelled` when an `exclusive=True` entry-detail worker
  is superseded by a newer one, which is by design. The test passed 24/24 re-runs under load;
  `settle()` now treats a cancelled worker as settled and waits again. After both fixes
  `test_tui_campaign_detail.py` passed 5/5 runs with `-n 8` (23 tests each), and
  `test_tui_queue_tab.py` + both ideas-board files passed (75).
- Real-data headless run (Textual pilot, 200×55, 2026-11-09): board load 2.55 s off the loop
  (index ~0.1 s cold of that). 178 board items have campaigns: 149 BUILT, 17 IN FLIGHT, 9 OPEN,
  3 DROPPED. IDEA-1041 (BUILT, SPEC-0488 done) shows `Queue ── w opens` /
  `IDEA-1041-20261109-c0c6  merged  2026-11-09`; `w` opened that campaign's popup (overview:
  MERGED, spec SPEC-0488, runs 7; Timeline tab: the full ledger through close); Escape returned
  to IDEA-1041 in Meta-Tools with the board focused. IDEA-1023 offered a chooser
  `IDEA-1023-20261108-a2fb merged` above `IDEA-1023-20261107-0799 abandoned`. IDEA-1067 shows
  `not built through the queue` and `w` opened nothing. Captures (text + SVG) in the session
  scratchpad: `queue-detail-IDEA-1041`, `queue-popup-IDEA-1041`,
  `queue-popup-IDEA-1041-timeline`, `queue-back-IDEA-1041`, `queue-chooser`,
  `queue-detail-none-IDEA-1067`.

## Clock Log

N/A — internal work; wall time is clocked on IDEA-1067.

## Rollout / migration

None: read-only over the existing campaign store.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
