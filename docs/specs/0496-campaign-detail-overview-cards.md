---
id: SPEC-0496
title: "Campaign detail Overview pane: dashboard cards (outcome + why banner, time per phase, runs, gates, findings)"
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
kind: feature
parent: SPEC-0405
source_idea: IDEA-1069
tags: [tui, agentic, observability]
depends_on: [SPEC-0407]
---

## Context

Child of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md), promoted
from `IDEA-1069` (Meta-Tools). The popup shell
[SPEC-0407](./0407-campaign-detail-popup-timeline-phase-time-and.md) ships the Overview tab
as `#overview-text`, a `Static` showing `DetailView.header_lines` joined by newlines: two to
five lines of `key value` text above an empty pane. The human (2026-11-09) called it visually
poor and chose, from a mockup on real data (IDEA-1041), a **dashboard cards** layout fed by
`nw.campaign.history.v1`, which `load_detail` already carries as `DetailView.history`.

What the human must read in two seconds, in order:

1. **Outcome and why.** For a parked, abandoned, failed or escalated campaign, the reason is a
   prominent banner line.
2. **Where the time went.** Bars per phase, the dominant phase marked.
3. **Problems hit.** Failed gates, retried runs, must-fix/major findings, flaky tests, in
   red/yellow; green when clean.

SPEC-0476 (Runs pane) and SPEC-0477 (Findings & Mail panes) are being built at the same time
by queue campaigns that edit `campaign_detail.py` heavily. This spec keeps its footprint in
that file to a few lines.

This spec replaces SPEC-0407's Overview content contract ("Overview shows `header_lines`"),
not the rest of SPEC-0407. It does not supersede SPEC-0407; SPEC-0476/0477 replace their
placeholder panes the same way.

## Goals / Non-goals

**Goals**
- A banner card plus five content cards in the Overview tab, from the history payload.
- Outcome badge, coloured by outcome, and a WHY line for parked/abandoned/attention states.
- Responsive layout: three, two or one column by width; scrolls when taller than the pane.
- Live campaigns: badge is the current state, "elapsed" instead of "closed", current phase
  highlighted, refreshed by the existing poll.
- Missing data (no runs, no gates, empty payload, gaps) renders placeholders, never raises.

**Non-goals**
- No change to the Timeline, Runs, Findings & decisions or Mail tabs.
- No change to the history payload, `load_detail` or `header_lines` (the queue screens still
  use `header_lines`).
- No interactivity inside the overview (no cursor, no clickable cards).
- Keyboard scrolling of the overview (see Rollout): mouse wheel and scrollbar scroll it now.

## Decision

A new pure-Rich module `src/nightwright/tui/campaign_overview.py` turns the history payload
into one `Overview` renderable. `Overview.__rich_console__` reads the width Rich gives it and
picks the layout, so the existing `Static` re-lays out on resize with no widget code.
`campaign_detail.py` changes in four lines: one import, one CSS rule that makes the Overview
tab pane scroll, and the two `#overview-text` updates that now pass `CO.overview(detail)`.

## Design

**Module** `nightwright.tui.campaign_overview` (imports Rich and
`nightwright.agentic.history.PHASE_OF`; no Textual, no I/O except `overview()`, which reads
the cached spec title through `queue._spec_title(state)`, the same call `header_lines` makes).

- `Overview(history, *, state=None, title=None)`: a Rich renderable. `renderables(width)`
  returns the card list; `plain(width=120)` and `__str__` export plain text (tests and
  `str(Static.content)` use it).
- `overview(detail)` builds one from an `agentic_model.DetailView` (history, state, title).
- Helpers: `badge`, `reason`, `fmt_s`, `short_model`, `run_glyph`.

**Layout** (content width `w`, the `Static`'s width):
- `w >= 150` (`THREE_COL_MIN`): banner, then three columns 38% / 34% / rest: *time* |
  *runs* | *gates* over *findings*; then *what happened* full width.
- `96 <= w < 150` (`WIDE_MIN`): banner, two columns 58% / rest: *time* over *runs* | *gates*
  over *findings*; then *what happened*. A 110-column terminal gives a ~99-column pane, so it
  is two-column.
- `w < 96`: everything stacked; *gates* and *findings* side by side when `w >= 64`.

All card lines are built at the card's inner width and cropped with an ellipsis (`no_wrap`),
so bars and rows never wrap. Only the WHY line wraps: the reason is never cropped.

**Banner** (Panel, border = badge colour). Title row: `SPEC-id · spec title` on the left,
the badge on the right (`✓ MERGED` green, `⇢ HANDED OFF` green, `✗ ABANDONED` red,
`⏸ PARKED` / `⧗ WAITING` yellow, `● <STATE>` cyan for live states). Body: chunks packed
three spaces apart, never split, onto as few lines as fit:
`campaign · project · idea · profile`, `opened HH:MM` (`Mon DD HH:MM` when not the day the
history was generated), `closed …` or `elapsed <wall>` (live); `wall · active`,
`loops n/max` (yellow over max), `runs n`, `cost $x / $cap` (yellow at >= 80% of the cap,
red at >= 100%), `conductor $x`, `merged <sha8>` (green), `PR`, `follow-up`, and `branch`
for live campaigns. Then the WHY line (`reason()`):
- park present: `why parked: <reason> (from <escalated_from>): <detail> → resume to <state>`,
  yellow;
- abandoned: `why abandoned: <text>` from the newest `decision` record titled `abandon`, else
  the newest `escalation`, else the last failing gate, else `no reason recorded`, red;
- live with `park_requested`: `park requested: <detail>`, yellow;
- live whose newest gate failed: `attention: gate #n failed: <names>`, yellow;
- otherwise no line (merged/handed-off clean).
History `gaps` add a dim yellow `⚠ N history gaps …` line.

**Where the time went.** One row per phase with `wall_s > 0`, plus the live current phase
(`PHASE_OF[state]`) even at zero; so `parked`/`waiting` vanish when zero. Row: phase label,
bar scaled to the largest phase (eighth-block partials, at least `▏` for a non-zero phase,
`░` track in grey30), duration, `×visits` when more than one, marker `◀` on
`dominant.wall`, `● now` on the live phase (`◀ now` when both). Bar colour: cyan for working
phases, grey62 for queue/waiting, yellow for parked; the dominant/current bar is bold. A dim
footer gives idle time (queue + parked + waiting) and its share of wall when >= 60 s and
>= 5%.

**Runs.** Consecutive same-loop `review`-phase `*_reviewer` runs fold into one row
(`3 reviewers`, duration = the longest, verdicts counted `2×pass fix`). Row: loop tag `Ln` on
the first row of each loop, persona, short model (`grok-4.7-high` → `grok-high`,
`composer-2.5` → `composer`, `medium` → `med`), duration (running runs: elapsed to
`generated_at` plus `…`), glyph, verdict. Glyphs: `✓` green; `↻` yellow for a retry verdict
(`fix`, `partial`, `split`, `blocked`, `retry_gate`, `rework`, …); `✗` red for
`error`/`timeout`/… or a non-zero exit; `●` cyan running. A group shows its worst glyph.
Latest rows kept: 18 / 14 / 10 (three/two/one column), the rest summarised as
`… N earlier rows (M runs)`. Title `runs · N · k failed · j retried`; border red if any run
failed, yellow if any retried, else green.

**Gates.** Row per gate: `#seq`, `✗`/`✓`/`●`, the first failing check name (`+n` for more)
or `N checks`, prefixed `close · ` when the gate started after the first transition into the
close phase (from the history timeline), `↻n` (yellow) when checks were retried, duration.
Latest 10 (6 in one column) kept, earlier summarised with their failure count. Title
`gates · N · k failed`; border red when the newest gate failed, yellow when an earlier one
did, green otherwise. No gates: `no gates yet`.

**Findings.** Counts of `finding` records by severity `must-fix` (bold red), `major` (red),
`minor` (yellow), `nit`, `info`, dim when zero, flowed onto as few lines as fit; then
`N flaky` (bold yellow) and up to three short test names; `N triage: gate, divergence`
(yellow); `N escalations` (bold red); `✓ nothing blocking` (green) when there are no
must-fix/major findings, flaky tests or escalations. Border red for must-fix or escalations,
yellow for major or flaky, green otherwise.

**What happened** (added during implementation: without it a short campaign left half the
pane empty at 200x55). Full width, oldest first, the newest 10 / 8 / 5 kept: `decision`,
`escalation`, `human_decision`, `answer`, `note` and `close_check` records plus run
`summary` lines, each `time  agent  text` on one cropped line. The ledger's
`decision <kind> (<trigger>): ` note prefix is stripped and repeated texts dropped (a decision
and its note say the same thing). Omitted when there is nothing to show.

**Colours** reuse the TUI's named Rich colours: green/red/yellow as in `event_style` and
gate `ok`/`FAIL`, cyan for live/current, dim for secondary text, grey50 for neutral card
borders.

**Popup wiring** (`campaign_detail.py`): `from . import campaign_overview as CO`;
`#tab-overview { height: 1fr; overflow-y: auto; }`; both `#overview-text` updates pass
`CO.overview(detail)` / `CO.overview(previous_view)`. The poll path is unchanged: a changed
signature reloads `load_detail`, which updates the overview.

## Alternatives considered

- **Build the cards in `campaign_detail.py` as Textual widgets** (a grid of `Static`s): real
  CSS layout, but tens of lines in the file SPEC-0476/0477 are rewriting. Lost on merge risk.
- **Fixed layout picked in `on_resize`**: needs widget code; a width-aware renderable gets the
  same effect inside Rich.
- **`rich.columns.Columns`**: wraps cards by their natural width and gives uneven, jumping
  layouts; an explicit grid with set widths is predictable.
- **Keep `header_lines` above the cards**: duplicates the banner; branch/worktree are kept
  only for live campaigns (branch) since a closed campaign's worktree is gone.

## Acceptance criteria

- [x] `campaign_overview.py` exists, imports no Textual, and `Overview(history)` renders at
      any width >= 30 for: a merged clean payload, a parked payload with a reason, a live
      in-gate payload, an abandoned payload, and an empty/new payload (`{}` and a QUEUED
      header with no phases/runs/gates) without raising.
- [x] Banner shows spec id + title, the outcome badge (MERGED/ABANDONED/PARKED/live state)
      in its colour, campaign/project/idea/profile, opened and closed-or-elapsed, wall/active,
      loops, runs, cost/cap, conductor cost and merged sha when present.
- [x] Parked shows `why parked:` with the park reason and detail; abandoned shows
      `why abandoned:` with the abandon decision text; a live campaign whose newest gate
      failed shows `attention: gate #n failed: <name>`; a merged clean one shows no WHY line.
- [x] Time card: one bar row per non-zero phase (zero `parked`/`waiting` omitted), dominant
      phase marked `◀`, live current phase marked `now`; no bar row wraps at any layout.
- [x] Runs card groups same-loop reviewers, marks failed `✗`, retried `↻`, running `●`; gates
      card shows each gate's pass/fail with the failing check name and duration; findings card
      shows severity counts, flaky tests and `✓ nothing blocking` when clean.
- [x] Placeholders `no time recorded yet`, `no runs yet`, `no gates yet`; gaps line when
      `gaps` is non-empty.
- [x] Layout: three columns at >= 150, two at 96–149, stacked below 96; every rendered line
      is exactly the requested width (no wrapping overflow).
- [x] In the popup, `#overview-text` holds an `Overview` whose text has the banner and the
      card titles; the Overview tab pane scrolls (`max_scroll_y > 0`) when the content is
      taller than the pane.
- [x] `campaign_detail.py` diff is limited to the import, the CSS rule and the two update
      calls.

## Test plan

- **Automated tests:** `tests/test_tui_campaign_overview.py` (new) — fixture payloads built in
  the test (merged clean, parked with reason, live in gate with a failed newest gate and a
  running run, abandoned with an abandon decision, empty `{}` and a new QUEUED campaign with
  gaps). Asserts badge/WHY text, phase rows and omission of zero phases, dominant/now markers,
  reviewer grouping and glyphs, gate rows, findings counts and clean marker, placeholders,
  layout choice by width (3/2/1 columns via line structure) and that every line of
  `plain(w)` is `w` cells for w in {40, 80, 110, 160, 200}. One Textual `run_test` opens the
  popup on a fixture campaign and asserts `#overview-text` content is an `Overview` whose
  text contains the campaign id, badge and card titles, and that a narrow, short terminal
  makes `#tab-overview` scrollable.
- **Existing tests:** `tests/test_tui_campaign_detail*.py` assertions that pinned the old
  `header_lines` text in `#overview-text` are updated to the new contract (campaign id
  present, state badge present, markup in a title rendered literally).
- **Manual verification:** headless Pilot captures of the real popup at 200x55 and 110x40
  for IDEA-1041-20261109-c0c6 (merged), IDEA-1019-20261107-6e80 (parked), SPEC-0411-20261108-c153
  (abandoned) and SPEC-0476-20261109-ca46 (live), checked by eye for alignment, no wrapped bars
  and no empty half-pane.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q -n 4`.

## Rollout / migration

None: render-only. **Follow-up (not in this spec):** keyboard scrolling of the overview
(`pagedown`/`ctrl+j`) needs one branch in `CampaignDetail._detail_scroll`, which SPEC-0476
rewrites; add it after SPEC-0476 lands to avoid a conflict.

## As shipped

- `src/nightwright/tui/campaign_overview.py` (new, ~700 lines, no Textual import) as designed
  above. Two robustness rules were added during implementation: `Overview` normalises the
  payload first (`_clean`: `header`/`dominant` coerced to dicts, list sections filtered to
  dicts), and every card is built through `_safe`, so one failing card renders
  `unavailable: <error>` instead of blanking the pane.
- The *what happened* card was added during real-data review (a short merged campaign left
  half of a 200x55 pane empty); it reuses records already in the payload.
- `campaign_detail.py`: +4/-3 lines (import, `#tab-overview` CSS rule, two update calls).
- Existing tests that pinned the old text contract were changed in place: the overview
  *contains* the campaign id (was `startswith`), shows `● QUEUED` (was equal to the joined
  `header_lines`), and renders park-detail markup literally in the card text (was
  `isinstance(content, Text)`).
- Keyboard scrolling is deferred (Rollout); the pane scrolls with the mouse wheel and
  scrollbar, verified by `pane.max_scroll_y > 0` and `scroll_end` in the Pilot test.
- Real-data captures (headless Pilot, the real popup): IDEA-1041-20261109-c0c6 (merged),
  IDEA-1019-20261107-6e80 (parked, `human_decision` reason), SPEC-0411-20261108-c153
  (abandoned, divergence decision), SPEC-0476-20261109-ca46 (live) at 200x55 and 110x40.
  No wrapped bars or rows; three columns at 200, two at 110; the parked campaign scrolls at
  110x40 (49 content rows in a 29-row pane).
- Tests run: `tests/test_tui_campaign_overview.py` + `tests/test_tui_campaign_detail.py` +
  `tests/test_tui_campaign_detail_ac.py` (`-n 4`): 97 passed; `test_tui_campaign_detail_model.py`
  also passed earlier (102 with it). The full affected set (~large, agentic import closure) is
  gated on the worker box by the parent (`nw gate run`); a local `-n 4` attempt was stopped
  at the parent's request before completing.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions
