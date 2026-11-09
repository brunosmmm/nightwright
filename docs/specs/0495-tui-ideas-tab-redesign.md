---
id: SPEC-0495
title: "TUI ideas tab: project rail + Open / In flight / Built buckets with real spec status"
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
kind: feature
milestone: "M5: interactive desk"
tags: [tui, ideas, specs, projects]
depends_on: [SPEC-0100, SPEC-0102, SPEC-0103]
supersedes: [SPEC-0110, SPEC-0115]
source_idea: IDEA-1067
---

## Context

The `nw tui` ideas tab (SPEC-0100, reshaped by SPEC-0110's column planner, filter bar, sort
keys and wide mode, and SPEC-0114/0115's tree mode) is one flat list of ideas with a detail
pane. In a human interview on 2026-11-09 (IDEA-1067) it was judged to fail at the three things
the human needs from it:

- telling **open** ideas apart from **completed** ones (the default hides every closed idea, and
  `a` dumps ~1070 rows of every state into one list);
- showing **the specs ideas produced and their real status**: the list shows the idea's own
  state, never the linked spec's ledger status or an outbound spec's exported/target status;
- **per-project separation**: four projects plus project-less ideas share one list, and project
  is only a column or a filter you have to type.

Real corpus at the time: 1067 ideas, Meta-Tools 657, jerome 294, comfort-assistant 57, pwrctl 6,
53 without a project; states IDEA, INCUBATE, RESEARCHED, SPECCED, PROMOTED, EXPORTED, SHIPPED,
DROPPED. Bare `SPEC-NNNN` links exist only on Meta-Tools / project-less ideas; jerome and
comfort-assistant link outbound `JEROME-NNNN` / `COMF…` portables. 8 SPECCED ideas link a spec
file that is not on this branch (specs still in campaign worktrees).

## Goals / Non-goals

**Goals**
- Replace the ideas tab with a board: project rail · bucketed main pane · detail pane, as agreed
  in the interview (IDEA-1067 Summary, reproduced in *Decision*).
- Every idea row past OPEN names its spec and that spec's **real** status (frontmatter — the
  ledger is generated from it — or the outbox portable, with "exported" when PROVENANCE says so).
- Keep every existing mutation key acting on the selected idea.
- Stay fast on the real ~1100-idea corpus.

**Non-goals**
- Writing spec status or reconciling idea state (that stays `nw spec reconcile`); the board is
  read-only display plus the existing idea mutations.
- Resolving a foreign project's own `SPEC-NNNN` from its repo (none exist in the real data; shown
  as `foreign`).
- Changing the hub, queue, events or mailbox tabs, or the CLI `nw ideas --tree` (SPEC-0114).

## Decision

The ideas tab becomes **rail | board | detail**:

1. **Rail** (left, always visible): `All`, each project (case-insensitive alphabetical, stable
   while counts change), `(no project)` last; each shows `open·in-flight`.
2. **All** selected: a project **scoreboard** (PROJECT, OPEN, IN FLIGHT, BUILT, LAST 7D movement
   such as `+9 ideas · 3 built`, plus a total row) above a cross-project **recent-activity feed**
   (newest first: captured / became SPEC-X / built), derived from existing timestamps.
3. **A project** selected: buckets OPEN, IN FLIGHT, BUILT (folded by default); DROPPED hidden,
   `d` toggles it in as a fourth bucket.
4. **Detail** (right): the existing summary / questions / log view, plus a spec block that always
   names the linked spec and its status (or says `no spec yet`), the bucket, the spec title,
   outbound export state and an epic roll-up.
5. `/` searches every project (the SPEC-0086 searcher), lists hits with project and bucket; Enter
   on a hit jumps to its project and row, unfolding BUILT or revealing DROPPED when needed.
6. Epic ideas show a roll-up `k/n built`.
7. SPEC-0110's table (column planner + auto-drop, `f` filter bar, `o`/`O` sort, `z` wide mode)
   and SPEC-0115's `g` tree mode, plus `a` (show non-active), are retired: the rail and buckets
   replace them. Mutation keys `l s ? c t x m X i` are unchanged.

### Bucketing rules (first match wins)

| # | Condition | Bucket |
|---|-----------|--------|
| 1 | idea state `DROPPED` | DROPPED |
| 2 | idea state `SHIPPED` | BUILT |
| 3 | idea links a spec whose governing status is `done` | BUILT |
| 4 | … governing status `rejected` | DROPPED |
| 5 | … any other governing status (draft, proposed, accepted, in-progress, superseded with no live successor) or `missing` / `foreign` | IN FLIGHT |
| 6 | no spec link, state `SPECCED` / `PROMOTED` / `EXPORTED` | IN FLIGHT, labelled `no spec link` |
| 7 | anything else (IDEA, INCUBATE, RESEARCHED, custom states) | OPEN |

- **Governing status**: the linked spec's frontmatter status, after following `superseded_by`
  (up to 8 hops, so a cycle terminates) to the live successor; the row shows `SPEC-A→SPEC-B
  status`. A superseded spec whose successor shipped is therefore BUILT, not stuck.
- **Draft spec under SPECCED**: IN FLIGHT. A spec exists, however early; the status column says
  `draft`, which is the information the human asked for.
- **Missing spec file** (PROMOTED/SPECCED pointing at a spec not on this branch): IN FLIGHT with
  status `missing` in red — an anomaly to surface, never silently counted as built.
- **Foreign**: a bare `SPEC-NNNN` on a non-Meta-Tools idea resolves against this repo only when
  this repo's spec claims the idea via `source_idea` (SPEC-0327's rule); otherwise `foreign`.
  Project-less ideas count as internal for resolution (as `spec_scope.idea_project` does) but get
  their own `(no project)` rail entry.
- **RESEARCHED** without a spec is OPEN, as the human listed it with IDEA/INCUBATE.
- **Epic ideas** (linked spec `kind: epic`) are bucketed by the epic's own status like any idea;
  the roll-up counts child specs (`parent:` = the epic) built/total, excluding superseded and
  rejected children. With no child specs (outbound epics) it falls back to child ideas (`:EPIC:`
  = the epic id), excluding DROPPED ones, built = in BUILT.

### Ordering

- OPEN: ideas with open questions first, then priority A, B, C, none, then most recently updated.
- IN FLIGHT: spec status in-progress, accepted, proposed, draft, then the rest (missing, foreign,
  no link); most recently updated within each.
- BUILT: newest built first; built time = the done spec's `updated` (else `created`), else the
  idea's CLOSED date, else its `:UPDATED:`.
- DROPPED: most recently updated.

### Activity feed and movement

Events per idea: *captured* at `:CREATED:` (or *updated* at `:UPDATED:` when an idea was never
stamped with CREATED, so it is still visible and not mislabelled), *became SPEC-X* at the linked
spec's `created`, *built* at the built time. Spec dates are date-only; when the idea's
`:UPDATED:` falls on that same day its clock time is borrowed, so same-day events order by when
they happened instead of all sorting at midnight. The feed keeps the newest 60. LAST 7D counts
captures and builds whose day is within the last 7 days.

## Design

- `src/nightwright/tui/ideas_board.py` (new, no Textual import): `SpecInfo`, `load_spec_index`
  (internal `docs/specs` glob + `_outbound_specs`, PROVENANCE read once, same sources as
  `hub_payload`), `SpecLink` / `resolve_link`, `classify`, `BoardItem`, `RailEntry`, `ScoreRow`,
  `Activity`, `Board` (`buckets(key, show_dropped=)`, `entry(key)`, `by_id`), pure `build_board`,
  and `load_board(cfg, filters=)`.
- `load_board` reads all ideas once with `collect_idea_tasks(all_done=True, **filters)` (CLI
  `--state/--kind/--tag/--workstream/--priority` still narrow; `--project` selects the rail
  entry instead), dedupes ids like `load_rows`, and computes open-question counts with the new
  `explore.open_question_counts`, which reads each org file once and reuses the SPEC-0054 parser
  through a `lines=` argument added to `read_idea_enrichment` / `read_idea_questions` /
  `open_question_count`. The per-idea file re-read was ~2.3 s of a 4.3 s load on the real corpus.
- `model._row_for` takes an optional precomputed `open_questions`. Dead desk helpers
  (`flatten_tree`, `status_line`, `parse_filter_expr`, `format_filter_expr`, `FILTER_KEYS`,
  `SORT_FIELDS`) are removed; `load_rows` stays as the search loader.
- `app.py`: tab-ideas is `OptionList#rail` | `Vertical#board-pane` (`Static#scoreboard`, shown
  only on All; `Tree#board`; `Static#status`) | `VerticalScroll#detail-pane`. Tree nodes carry the
  idea id as `data` on leaves and `("bucket", name)` / `("section", …)` on headers, so
  `selected_id` (and every mutation) reads the cursor leaf. Fold state per bucket survives
  repaints. Switching projects is a repaint only (the board holds every project). Load runs off
  the event loop (`_reload_async`, SPEC-0129/0134 guard unchanged); search re-runs only the
  searcher (`refresh_search`), debounced (SPEC-0105). After a reload the cursor stays on the same
  idea, following it into another project (a capture there), or stays near its line when the idea
  left the view.
- Keys: `[` / `]` previous/next project, `{` / `}` previous/next bucket header (a 250-idea OPEN
  bucket otherwise pushes IN FLIGHT off screen), space/enter fold a bucket, `d` dropped, `/`
  search, Esc clears. Rail width 26, detail 36%.
- `nw tui`: `--sort/--desc` accepted but ignored (help says so); `--all` opens with DROPPED shown;
  `--project` opens that project's board.

## Alternatives considered

- **Keep the DataTable with bucket header rows** — every mutation path would special-case rows
  with no idea, the trap SPEC-0114 documented; a Tree has real fold semantics for BUILT.
- **Bucket by idea state alone** — exactly what failed: PROMOTED means "spec done" for some ideas
  and "spec written" for others, and EXPORTED says nothing about the target repo's progress.
- **Load per project on rail change** — `load_tasks` is the cost (~2 s) whatever the project, so
  one load and pure repaints is both simpler and faster.
- **Order the rail by activity** — rows would jump while counts change under the cursor.

## Acceptance criteria

- [x] The ideas tab shows a rail with All, each project, and `(no project)`, each with
      `open·in-flight` counts.
- [x] All shows the scoreboard (OPEN, IN FLIGHT, BUILT, last-7d movement, total) above a
      newest-first cross-project activity feed of captured / became-spec / built events.
- [x] A project shows OPEN, IN FLIGHT, BUILT in that order with the orderings above; BUILT is
      folded by default; DROPPED is hidden and `d` toggles it in as a fourth bucket.
- [x] IN FLIGHT and BUILT rows show the spec id and its real status (internal frontmatter or
      outbound portable, `· exported` from PROVENANCE, supersede chains followed, `missing` /
      `foreign` / `no spec link` surfaced).
- [x] Bucketing follows the rules table, including SPECCED with a draft spec, a missing spec file,
      superseded specs and epic ideas.
- [x] Epic ideas show a `k/n built` roll-up.
- [x] The detail pane always names the linked spec and its status (or `no spec yet`).
- [x] `/` searches every project; Enter on a hit jumps to its project and row, unfolding or
      revealing its bucket.
- [x] The table/tree modes and their keys (`a f o O z g`) are gone; `l s ? c t x m X i` act on
      the selected idea; hub, queue, events and mailbox tabs are unchanged.
- [x] The real corpus (1067 ideas) loads in under ~3 s off the event loop.

## Test plan

Deliberately minimal (the human's choice; the board is read-only display over existing
mutations).

- **Automated tests** — `tests/test_tui_ideas_board.py`:
  - `test_classify` (parametrized over the rules table), supersede chain + cycle, missing /
    foreign / claimed links, outbound exported label;
  - `build_board`: OPEN ordering, BUILT newest-first + DROPPED on demand, in-flight status labels
    and ordering, rail counts and order, scoreboard movement + total, activity order (incl. the
    no-CREATED fallback and same-day clock borrowing), epic roll-up from child specs and the
    child-idea fallback;
  - `load_spec_index` over a tmp specs dir + outbox + PROVENANCE; `load_board` over a real org
    file, including that the batched question count equals `open_question_count`;
  - one Textual `run_test` smoke: All renders rail + scoreboard/feed; a project renders the three
    buckets with BUILT folded and the spec status + roll-up; `l` logs onto the selected idea and
    not another; `jump_to` reaches a DROPPED idea in another project.
- **Adjusted tests**: retired-behaviour tests removed from `test_desk_table.py` (planner auto-drop,
  filter bar, sort keys, wide mode, status line), `test_idea_tree.py` (desk tree mode),
  `test_sort_registry.py` (desk sort vocabulary); `test_tui_desk.py`, `test_tui_search.py`,
  `test_tui_highlight.py`, `test_tui_resilience.py`, `test_tui_hub.py`, `test_epic_view.py`
  re-pointed at the board (hits list, `#board`, `jump_to`, `load_board`);
  `test_tui_real_terminal.py` re-pointed at the board under a real pty (buckets in order, spec
  status + roll-up on the row, state hues distinct).
- **Manual verification**: run the desk headless with Textual's pilot on the real data
  (`uv run python <scratch>/shot.py`), capture the All view and the jerome view as text + SVG,
  search `rail`, Enter, Enter, and check the jump lands on the hit.
- **Regression guard**: `python3 tools/affected_tests.py --run uv run pytest -q` plus the touched
  TUI files; `test_tui_mutations.py` unchanged and green.

## As shipped / verification

- Shipped as designed above. Two details settled while building: a search's first hit is where
  the cursor lands (Enter in the box hands over the hit list; Enter on a hit jumps), and `{`/`}`
  were added because the real jerome / Meta-Tools OPEN buckets (64 / 254 ideas) push IN FLIGHT
  below the fold.
- Real-data headless run (Textual pilot, 200×55, 2026-11-09): board load 2.7 s off the loop
  (4.3 s for the old `load_rows(all_done=True)`, mostly the per-idea file re-read the batched
  question count removes). Rail `All 368·33 · comfort-assistant 36·8 · jerome 64·4 ·
  Meta-Tools 254·21 · pwrctl 6·0 · (no project) 8·0`; scoreboard total `368 / 33 / 415`,
  `+361 ideas · 177 built` in 7 days (jerome's ideas were mostly captured this week). jerome
  IN FLIGHT shows `JEROME-0019 in-progress · exported 5/6 built`, `JEROME-0020 accepted ·
  exported 0/11 built`; Meta-Tools IN FLIGHT surfaces the 8 SPECCED ideas whose spec file is not
  on this branch as `missing`. Search `rail` → 16 hits across projects; Enter, Enter landed on
  IDEA-1067 in Meta-Tools' OPEN bucket. Captures (text + SVG) were saved in the session
  scratchpad: `ideas-all`, `ideas-jerome`, `ideas-jerome-folded`, `ideas-search`, `ideas-jump`.
- The rail only selects a project on the user's own moves (it has focus): `paint_rail` sets
  `highlighted` itself, and that queued echo otherwise landed after a newer `jump_to` /
  `select_project` and dragged the view back to All (found as a flaky smoke test).
  `--project P` with no ideas keeps P on the rail with zero counts so capture still pre-selects it.
- Tests: `tests/test_tui_ideas_board.py` 31 passed (repeated runs). Affected set
  (`tools/affected_tests.py --run uv run pytest -q -n 8`, 245 files): 6599 passed, 5 failed —
  all five fixed after (two focus-id asserts `list`→`board` in the queue/events tab tests, the
  `--project` pre-select, the rail echo race, a test terminal too narrow for the status line);
  the 69 TUI/explore files re-ran green apart from two load-sensitive tests
  (`test_tui_campaign_detail::test_mailbox_append_triggers_poll_reload`,
  `test_tui_fixture_harness::test_pilot_session_does_not_write_the_copy`) that pass alone and
  in the first affected run.
- Narrow terminals: at 110 columns the board pane is ~45 columns and crops headings (the spec
  status sits before the heading so it survives); the pty test runs at 160 columns.

## Clock Log

N/A — internal work; wall time is clocked on IDEA-1067.

## Rollout / migration

No data migration: the board reads existing idea properties and spec frontmatter. SPEC-0110 and
SPEC-0115 are marked `superseded` by this spec.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
