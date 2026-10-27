---
id: SPEC-0277
title: "Per-reviewer review chips in queue watch and the TUI queue tab"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-078
parent: SPEC-0169
tags: [agentic, queue, watch, tui, review]
---

## Context

Promoted from idea `IDEA-078` (user, 2026-10-25: "review work status must show up in wt
queue watch"). Two specs already took parts of that idea:
[SPEC-0211](./0211-spec-phases-and-review-rounds-in-queue-watch-and.md) covers the spec
phases and the spec-review round inside campaigns, and
[SPEC-0217](./0217-standalone-spec-reviews-in-watch-spec-phase-events.md) covers standalone
`wt spec review` runs (the **Spec reviews** section). Both name the remainder as deferred,
with `IDEA-078` left open for it: **per-reviewer status for implementation review
(REVIEWING)**. This spec covers that remainder.

What exists today (verified on main):
- With SPEC-0201 fan-out, REVIEWING runs several reviewer personas in parallel, as listed by
  `profiles.reviewers_of(profile)`. Their stored verdicts live in
  `state.last_review.verdicts[persona] = {verdict: pass|fix, score, findings, run, loop}`,
  written by `queue._store_verdicts`. `queue._valid_verdicts` keeps those of the current loop.
- `queue.dashboard_payload` gives each active campaign one `activity` from `queue._activity`.
  That function tracks only the **last** open `run.start` in the ledger, so a fan-out of
  three shows one reviewer. It also gives one `last_verdict` from `_verdict_summary`, which
  the next persona overwrites.
- `cli_queue._active_panel` renders `now` and `verdict` rows. The TUI queue tab
  (`tui/agentic_model._active_meta`) shows `STATE · persona elapsed`.

## Goals / Non-goals

**Goals**
- Each active campaign in REVIEWING, or with current-loop reviewer verdicts, carries
  `reviews`: one entry per configured reviewer persona for the current loop.
- `wt queue watch` shows these as a `reviews` row of chips, and the TUI queue tab row shows
  them in compact form.
- The dashboard header counts reviewer runs in flight.

**Non-goals**
- Spec-review status (SPEC-0211, SPEC-0217).
- Showing past loops' reviews (`wt campaign show` and the campaign detail screen,
  SPEC-0246, keep the history).
- Changing the review triage score or routing (SPEC-0203).

## Decision

Add a pure helper, `queue._review_chips(state, ledger, reviewers, now) -> list[dict]`, called
from `dashboard_payload` for active rows. The data comes from the ledger's `run.start` and
`run.end` events and from `last_review.verdicts`. No new stored state.

## Design

### Payload (`queue.dashboard_payload`, `wt.queue.dashboard.v1`, additive)

- `reviewers` is `reviewers_of(resolve_profile(cfg, st["profile"], project_root=...))`. It is
  resolved once per (profile, project) per payload call and cached in a local dict. If
  resolution raises `ValueError` or `OSError` (`profiles.load_layers` reports parse and
  validation errors as `ValueError`; `OSError` covers an unreadable file), the fallback
  list is the personas in `personas.REVIEWER_PERSONAS` that appear in
  `last_review.verdicts` or in a `run.start` of the current window, in that order. The
  triagers (`review_triager`, `gate_triager`) and the implementer are not in
  `REVIEWER_PERSONAS`, so they never become chips. In the fallback path the current-loop
  filter is applied directly (`entry["loop"] == counters.loops`), since
  `_valid_verdicts` needs a profile. The dashboard never fails on a broken profile
  (SPEC-0265).
- `_review_chips` produces one entry per persona, in `reviewers` order:
  `{persona, run_id, status, verdict, score, counts, elapsed_s}`. **Window:** rules 1 and 3
  apply only while the campaign state is REVIEWING; the window starts at the ts of the last
  `transition` event into REVIEWING. Outside REVIEWING only rule 2 can match. After a fix
  route (`counters.loops` incremented, state IMPLEMENTING) the stored verdicts belong to an
  earlier loop, so every chip is `not_run` and `reviews` is `[]`. The `status` is the first
  match of:
  1. `running`: an unmatched `run.start` for that persona inside the window. `elapsed_s`
     is measured from the `run.start` ts.
  2. `pass` or `fix`: a valid current-loop entry in `_valid_verdicts`. `counts` holds the
     number of findings by severity, `{must_fix, major, minor, nit}`, and `score` is the
     verdict's score.
  3. `failed`: a matched `run.end` for the persona inside the window with no valid
     current-loop verdict (a run error or a non-conforming verdict).
  4. `not_run`: none of the above.
- `reviews` is set on an active row only when the state is REVIEWING or when at least one
  chip is not `not_run`; otherwise it is `[]`.
- `totals.reviews_running` is the number of `running` chips across active rows.

### Rendering

- Names: the **short name** is the persona minus `_reviewer` (`conformance`,
  `adversarial`, `test`); the **tag** is the first four characters of the short name
  (`conf`, `adve`, `test`).
- `cli_queue._active_panel` adds a `reviews` row after `verdict`, built by a new
  `_reviews_text(chips)`. Each chip is the short name followed by:
  - `running`: `▶ 3m`;
  - `pass`: `✓ 92`;
  - `fix`: `✗ 2mf/1mj`, i.e. must_fix and major counts (the `wt queue watch` help text
    states this legend);
  - `failed`: `! failed`;
  - `not_run`: `·`.
  Styles: green for pass, red for fix and failed, cyan for running, dim for not_run.
- The watch header line appends `· reviews N` when `totals.reviews_running > 0`.
- `tui/agentic_model._active_meta` appends ` · rev ✓conf ✗adve ▶test` (glyph plus tag,
  without counts; `not_run` is `·tag`) when `reviews` is non-empty.

## Acceptance criteria

- [ ] With a three-reviewer profile in REVIEWING and two runs open, `active[*].reviews` lists
      all three personas: two `running` with `elapsed_s`, one `not_run`.
- [ ] After the runs end, each chip carries `pass` or `fix` with `score` and severity
      `counts` from `last_review.verdicts`. A run that ended without a stored verdict is
      `failed`. Verdicts of an earlier loop never show: after a fix route back to
      IMPLEMENTING, `reviews` is `[]`.
- [ ] `wt queue watch` renders a `reviews` row with one chip per persona, and the TUI queue
      tab row meta includes the compact chips.
- [ ] `totals.reviews_running` counts running chips, and the header shows it when it is
      non-zero.
- [ ] A broken profile does not break the dashboard: chips fall back to the observed
      personas.

## Test plan

New tests in `tests/test_agentic_dashboard.py` and `tests/test_tui_queue_model.py`:

- `test_review_chips_running_and_not_run`: a ledger with a REVIEWING transition and two
  unmatched reviewer `run.start` events, under a profile with three reviewers.
- `test_review_chips_verdicts_and_counts`: `last_review.verdicts` with `pass` (score 92) and
  `fix` (1 must_fix, 2 major) for the current loop gives the right `status`, `score` and
  `counts`.
- `test_review_chips_ignore_previous_loop`: verdicts with `loop` < `counters.loops` give
  `not_run`.
- `test_review_chips_failed_run`: a matched `run.end` with no stored verdict gives `failed`.
- `test_review_chips_after_fix_route_empty`: loop-N verdicts stored, `counters.loops` N+1,
  state IMPLEMENTING, with the reviewers' `run.end` after the last REVIEWING transition:
  `reviews == []`.
- `test_review_chips_broken_profile_fallback`: `resolve_profile` raises `ValueError` (and,
  parametrized, `OSError`); the chips come from the observed reviewer personas, and a
  `review_triager` `run.start` in the window never becomes a chip.
- `test_reviews_running_total_and_header`, and a render test of `_active_panel`: the text
  contains `✗ 1mf/2mj` and `▶`.
- `test_tui_active_meta_review_chips`: `_active_meta` contains `rev ✓conf`.
- **Sabotage check:** make `_review_chips` ignore the `loop` filter;
  `test_review_chips_ignore_previous_loop` must fail. Make it reuse `_activity` (last open
  run only); `test_review_chips_running_and_not_run` must fail. Drop the REVIEWING-only
  window; `test_review_chips_after_fix_route_empty` must fail.
- Regression guard: full `uv run pytest`, including the existing dashboard golden tests,
  updated only for the additive keys.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
