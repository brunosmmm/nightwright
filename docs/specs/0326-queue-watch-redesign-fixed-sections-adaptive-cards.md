---
id: SPEC-0326
title: "queue watch redesign: fixed sections, adaptive cards, journey track, why line, readable events"
status: done
owner: user
created: 2026-10-31
updated: 2026-10-31
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, dashboard, rich, ux]
depends_on: [SPEC-0185, SPEC-0309]
---

## Context

`wt queue watch` ([SPEC-0185](./0185-rich-human-output-everywhere-and-a-queue-watch.md)) is
the live Rich dashboard over `queue.dashboard_payload` (`wt.queue.dashboard.v1`). On
2026-10-31 the human and the steward watched it with five campaigns in flight and saw five
problems:

1. Five active cards at ~20 lines each pushed *Needs you*, *Conductor handling*, *Backlog* and
   *Recently delivered* off the screen (the Live screen crops the bottom).
2. The phase stepper is linear: a campaign back in IMPLEMENTING after a failed review looks
   like its first pass, so fix loops are invisible.
3. `now: ⚙ running gates` was shown while the campaign was actually blocked on the host-wide
   gate-slot pool ([SPEC-0309](./0309-faster-gates-parallel-pytest-affected-tests-only.md)): a `gate_waiting` ledger
   entry and a waiter marker, with two other campaigns holding both slots.
4. The `events` rows were raw ledger lines: ISO timestamps, `sha=`, `delta_usd=`, Python dict
   reprs.
5. The header showed `slots ━━━ 5/3` (busy > total).

**Finding for 5.** The runner journal shows `config_reloaded … agentic.profiles.balanced.slots`
at 10:00:08: the human lowered `balanced.slots` from 6 to 3 while six campaigns were active
(the 09:36 `admission_blocked` record says `slots 6, occupied 6`; the 10:20 one says `slots 3,
occupied 6`). Campaigns that already hold a slot are never evicted, so `busy` (campaigns in
ACTIVE_STATES) stays above `total` until they drain, and no admission happens until it is ≤ 3.
The number was true; the dashboard just did not say why. The gate-slot pool (2 slots) was a
separate limit the header did not show at all.

## Goals / Non-goals

**Goals**
- The header and the bottom sections always fit; the active region takes the rest.
- A campaign's journey (loops, failed gates, review scores, triage verdicts) is visible at a
  glance, with one sentence saying why it is where it is.
- Waiting (gate slot, close train, campaign slot, dependency) is never shown as running.
- Events read as short human sentences.
- The slots line is truthful and explains over-capacity; the gate-slot pool is visible.

**Non-goals**
- Changing the TUI (`src/wt/tui/`): it keeps reading the same payload; fields are only added.
- Changing `wt campaign log` (the raw rows stay there).
- Changing admission or slot accounting.

## Decision

Rebuild the frame as *header · Active · fixed-budget bottom sections*. The active region picks
the densest of three card styles that fits every campaign in the height left over. The
payload gains per-campaign `track`, `why`, `waiting_for`, `gate_hold`, `recent_lines` and
`state_age_s`, plus top-level `slots.over` and `gate_slots`. The derivations are pure functions
in a new module `src/wt/agentic/journey.py`, so they are unit-tested without a queue.

## Design

**Payload (additive, `--json` too)** — `queue.dashboard_payload` (plus a `_gate_occupancy`
helper that wraps `gate_slots.occupancy` and returns None when unreadable):
- `slots.over = max(0, busy - total)`; `gate_slots = {total, held: [cid], waiting: [cid]}`.
- per active item: `track` (list of `{state, reason, at, ok, outcome}`), `track_elided`,
  `waiting_for`, `why`, `gate_hold` (`{slot, since, held_s}` when it holds a gate slot),
  `recent_lines` (`[{ts, type, text}]`, last 3 non-`cost` events) and `state_age_s`.
  `recent_events`, `phases`, `phase_index` are kept for the TUI.
- waiting rows: `waiting_for` (`campaign_slot`) and `why`; backlog rows: `waiting_for`
  (`dependency` when blocked) and `why`; parked rows (needs you / conductor): `why`.

**`journey.track(ledger, current_state, cap=8)`** walks the `transition` events. Each
transition closes the open visit with `(ok, outcome)` and opens a visit for `to`; QUEUED and
OPENING are not visits; a re-entry into the state of the previous visit (after skipped
visits) re-opens it (consecutive duplicates merged). Outcomes: GATING → `ok` / `timeout` /
`N failed` (sum of `summary.failed`, else `pytest failed` / `N gates failed`) from the visit's
last `gate` event; REVIEWING → the score from the queue's `review score S/P outcome` note plus
the visit's must-fix / major finding counts (`43 · 2 must-fix · 1 major`); TRIAGING →
`retry` / `fix` / `escalate` / `loop limit`; SPEC_REVIEW → `accepted` / `revise`; a move to
PARKED → `parked`; a forward reason → `ok`. The last `cap` visits are kept; `elided` counts
the rest. No transitions → one visit for the current state.

**`journey.why(state, ledger, waiting, loops=)`** — one sentence: a close-train, campaign-slot
or dependency wait *is* the why (`waiting on SPEC-0302`); a gate-slot wait is not (the `now`
line shows it; the why keeps saying what the gates are for). PARKED: `parked: cost cap $15
reached`, `parked: loop limit reached (3/3)`, else `parked: <reason> — <detail>`. Otherwise from
the latest transition into the current state: `review_fix` → `back to implementing: review
scored 43 < 80 (2 must-fix, 1 major)`; `retry_gate` → `re-running gates: triage says the
failures are flaky`; `triage_fix` → `back to implementing: triage says the gate failure is
real — <first must-fix title>`; `gates_failed` → `triaging: gates failed (2 failed)`;
`implemented` → `gating the implementation` / `gating the fix` (a later pass); and so on, with a
per-state fallback.

**`journey.waiting_for(state, ledger, occupancy, busy=, total=, now=)`** — WAITING →
`{kind: campaign_slot, resume_to, busy, total, since}`; QUEUED with `blocked_by` →
`{kind: dependency, on}`; CLOSING with `closing.queued_behind` → `{kind: close_train, behind}`;
GATING/CLOSING → `{kind: gate_slot, slots, held: [cid], since, waited_s}` when the live
occupancy lists the campaign as a waiter and not as a holder. With no occupancy snapshot the
last `gate_waiting` entry after the last transition counts until a `gate` or `run.start`
follows it.

**`journey.event_sentence(ev)`** — `HH:MM` (local) + a short sentence per type: transition,
run.start / run.end, cost, commit (7-char sha, the `wt(<cid>):` prefix dropped, first line),
gate / gate_waiting, finding, note (author prefix unless `queue`), park / resume, spec_written /
spec_review / spec_accepted, close, open, human_decision, idea_reconciled. Unknown types:
`type key=value …` with values cut to 30 and the line to 90 characters.

**Renderer** (`src/wt/agentic/cli_queue.py`):
- Header: a Panel with 2-3 content lines that never fold (narrow widths stack runner / slots).
  The slot text is `campaigns ━━━ 5/3 over by 2, no admissions until ≤3  gates 2/2 +3 waiting
  today $58.66`.
- Bottom sections with fixed row budgets (`SECTION_ROWS`): Needs you 4, Conductor handling 4,
  Spec reviews 3, Waiting 3, Backlog 4, Recently delivered 5; each adds a `+N more` row and keeps
  the full count in its title. Rows never fold (ellipsis). Backlog and Recently delivered sit
  side by side (3:2) when width ≥ 150 (`SIDE_BY_SIDE`), and Recently delivered drops its project
  column below 90 columns. Section tables drop their blank edge lines. *Decided while building:*
  the brief said ≥ 120, but at 120 the two halves (≈ 70 / 48 columns) cut every title, status and
  outcome cell to `…` on live data; 150 is the narrowest width where both stay readable.
- Active region: `active_region(items, width, budget)` tries `full`, then `compact`, and
  measures each (`_lines`, a throwaway Console); if neither fits it renders `row` (a table, one
  line each, truncated with `+N more`). `budget = height − header − bottom − 1`. Columns
  (`card_columns`): 1, 2 at width ≥ 140, 3 at ≥ 210. The `Active (N)` title says `compact view` /
  `row view` when not full.
- Full card (`_active_panel`, ~9 lines with the border): title `campaign · spec · STATE 12m ·
  loop N/M · project` (the spec title is cut so the rest fits); `track`, `why`, `spec review`
  (idea campaigns in a spec phase), `now`, `cost` (`$2.22/$15 ━━━━━━ · size/tone → profile · runs · age`),
  `reviews` chips when present, `last` (2 sentences). Compact (`_card_compact`, 3 lines):
  title, track, why (prefixed `⏳ waiting for a gate slot …` when that is the wait). Row
  (`_cards_rows`): `campaign  STATE 12m  ⏳/▶/·  loop N/M  $cost  why`.
- `now`: a wait → `⏳ waiting for a gate slot (2/2 held: IDEA-511, IDEA-509) · 30s`; holding a
  gate slot → `⚙ running gates · slot 0 · 3m12s`; a persona run → as before.
- The track is cut from the left (`+N …`) until the current visit fits the card.
- `wt queue watch` passes `height`: hidden `--height`, else `console.height` on a terminal,
  else 50 (`DEFAULT_HEIGHT`) for `--once` off a terminal. `_dashboard(p, width, hours,
  height=None)` keeps its old call shape; `height=None` means unbounded (full cards).

## Alternatives considered

- **Keep the stepper and add a loop counter** — a counter says *that* it looped, not *why*
  (which gate failed, what the review scored). The track carries both.
- **Scrollable Live region** — Rich `Live` has no scrolling; the human would lose the bottom
  sections again. A density switch keeps everything on screen.
- **Put `why`/`track` in the renderer only** — the TUI and `--json` consumers want the same
  words; computing them once in the payload keeps them consistent.

## Acceptance criteria

- [x] The payload adds `track`, `track_elided`, `why`, `waiting_for`, `gate_hold`,
      `recent_lines`, `state_age_s` per active item, `why`/`waiting_for` on waiting and backlog
      rows, `why` on parked rows, `slots.over` and `gate_slots`; no existing field is removed or
      changed (TUI tests pass unchanged).
- [x] The track shows fix loops (a second `impl` visit after `review ✗ 43 · 2 must-fix`), merges
      consecutive duplicates and caps to 8 visits with an elided count.
- [x] The why line covers a review fix loop, a gate retry after triage, a cost-cap park and a
      dependency wait with the sentences in the Design.
- [x] A campaign blocked on a gate slot shows `⏳ waiting for a gate slot (…)`, never `running
      gates`.
- [x] Events render as `HH:MM` sentences; unknown types fall back to truncated `key=value`.
- [x] The layout chooses full / compact / row by height, and the bottom sections always render
      with `+N more` truncation; a `--once --height 30` frame still contains the Backlog and
      Recently delivered headings.
- [x] The header shows over-capacity (`5/3 over by 2, no admissions until ≤3`) and the gate-slot
      pool.

## Test plan

- **Automated tests:** `tests/test_agentic_dashboard_journey.py` (offline, tmp dirs, no waits):
  track (fix loop with outcomes, gate failure / timeout / triage outcomes, duplicate merge,
  cap + elided, width cut keeps the current visit, no-transition fallback); why (review fix
  loop, gate retry after triage, triaging after a failure, cost-cap / loop-limit / generic
  parks, dependency, close train, campaign slot); waiting_for (gate slot from occupancy, holder
  not waiting, ledger fallback until the gate runs, other states None) and the `now` line;
  event sentences for every mapped type and the fallback; `recent_lines` skips cost rows;
  layout (density selection at given heights, `+N more` in the row view, column thresholds);
  bottom sections truncate with `+N more` at widths 100 and 160 and sit side by side at 160;
  `wt queue watch --once --height 30` keeps the bottom headings; payload fields on a fixture
  (track, why, state age, no false gate-slot wait, readable last line, backlog dependency, park
  why); the over-capacity header.
- **Existing tests updated where the output changed on purpose:**
  `tests/test_agentic_dashboard_spec_phase.py` (the stepper's `● spec_writing` / `✓ opening` and
  the `from IDEA-…` title suffix are replaced by the track — `spec ●`, `impl ●` — and the
  campaign id in the title) and `tests/test_agentic_dashboard.py` (the profile reads
  `- → balanced` on the cost line; the `waiting on` backlog text at width 80 is still there).
- **Manual verification:** `uv run wt queue watch --once` at the real terminal width on live
  data, and `--json`; the excerpt is in *As shipped*.
- **Regression guard:** the full suite (`uv run pytest -n 4`), including the TUI queue/campaign
  model tests that read the payload.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

Built directly on main at the human's request (2026-10-31), no queue campaign.

AS_SHIPPED_PLACEHOLDER
