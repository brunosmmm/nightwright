---
id: SPEC-0298
title: "Agentic MVP: a cheap, durable, quiet conductor on a second project"
status: accepted
owner: user
created: 2026-10-30
updated: 2026-10-30
milestone: "M10: agentic MVP"
kind: epic
parent: SPEC-0169
tags: [agentic, conductor, cost, mvp]
---

## Context

The human fixed the MVP on 2026-10-30:

> Agentic wt MVP: a human starts a conductor (a long-lived Claude Code session running
> `/wt-conduct`) on a project, queues ideas, and queue + conductor deliver merged work; the
> human is only pulled in for genuine scope decisions.

The conductor is a **feature**, not a gap. This epic does not design it away. It makes it
cheap, durable, quiet and auditable, and proves it on a second project.

Measured on 2026-10-30, over the [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
queue as it runs today:

- **Throughput.** 93 campaigns merged. 57 of 93 merged with zero parks. Park reasons:
  `rebase_conflict` 34, `cost_cap` 16, `loop_exhausted` 15, `base_moving` 9,
  `config_invalid` 5.
- **Conductor cost.** One conductor session over 5 days: 2,386 turns and 1.09B cache-read
  tokens, so about 457k tokens of context were re-read per turn. That is about $263 at list
  price, against $645 for all dispatched agents (persona runs). 83% of the conductor's cost
  is cache reads. The conductor's share per merged campaign is about $2.83.
- **Why so many turns.** Most turns acknowledged informational events: `campaign_silent` and
  `state_stuck` false alarms while a gate or close was still running, `runner_draining`,
  `conductor_ping` with nothing new, and `merged` echoes after `code_changed`.
- **Not measured today.** wt has no record of which session is the conductor, which events
  woke it, or what it decided. Those numbers above were computed by hand from the transcript.
  The conductor's decisions are free-text `wt campaign note`s, and a restarted or compacted
  conductor re-orients by re-reading its own history.

So the conductor is the most expensive single participant per merge, most of that cost is
paid to re-read a huge context, and most of the wakeups did not need a decision.

## Goals / Non-goals

**Goals** (the MVP)
1. **Cheap and measured.** wt attributes the conductor session's tokens and dollars to the
   queue, per day and per merged campaign.
2. **Durable.** The conductor survives restarts and compaction through a defined state
   handoff, not by re-reading history.
3. **Quiet.** The conductor is woken only for events that need a decision. Informational
   events reach it as a periodic digest.
4. **Auditable.** Its judgment rules are written down (the `/wt-conduct` decision table) and
   each decision is a structured record that names the rule it applied.
5. **Second project.** The same loop works on comfort-assistant: an outbound project whose
   merge policy is a pull request.

**Non-goals: frozen until the MVP exit criteria are met.** No new specs, campaigns or
amendments in these areas. Already-queued work in them may finish, but nothing new is
admitted:
- Gate ladder v2 follow-ons (the cross-campaign flaky-test record, SPEC-0293, and later
  rungs).
- Automatic spec splitting follow-ons (queueing split children on merge, SPEC-0294, and
  later refinements).
- Pluggable or additional reviewer personas.
- Exploratory campaigns (`--kind explore`).
- A shared backlog backend (sheet or other non-org stores as the queue's source).
- Follow-up triage rounds (re-triaging filed follow-up ideas automatically).
- Any change that replaces the conductor with deterministic code wholesale. Individual
  routine decisions may move into the queue (see Breakdown), but the conductor stays.

## Decision

Keep the conductor as a long-lived Claude Code session and make four changes around it, each
in its own child spec:

1. **Measure it.** A session registry and a transcript cost reader (`wt queue
   conductor-cost`) put the conductor's own spend next to persona spend in
   `wt campaign export`.
2. **Quiet the stream.** The `--for conductor` follower holds back a declared set of
   informational events and prints a compact digest instead. Every event that needs a
   decision still goes out at once. Every line the follower delivers is counted, so
   wakeups per merge is a number.
3. **Write decisions down and hand state off.** A structured decision log, and a
   `wt queue conductor-brief` that gives a fresh or compacted conductor what it needs in a
   few thousand tokens. A skill rule says to compact at about 150k tokens of context and
   re-orient from the brief.
4. **Prove it on comfort-assistant** with PR hand-off, after the first three ship.

The exit criteria below are measured from the data the first three children produce. The
MVP is done when they hold over a real window of campaigns, not when the code merges.

## Architecture / cross-cutting design

- **One conductor data directory:** `<data_dir>/agentic/conductor/` (`store.agentic_dir(cfg)
  / "conductor"`). Each child owns one append-only JSONL file there:
  `sessions.jsonl` (SPEC-0299), `deliveries.jsonl` (SPEC-0300) and `decisions.jsonl`
  (SPEC-0301). Each file is written with a flock and a line-atomic append, like
  `store.append_event`. None of them is ever rewritten.
- **`events.jsonl` stays complete.** Quieting is a filter in the follower
  (`events.follow`), never a change to what `events.sync` appends. History, the TUI events
  tab and `wt queue events` still see every event.
- **New CLI lives in new modules.** Each child attaches its commands from its own module
  (the `cli_conduct.attach` pattern in `src/wt/agentic/cli.py`), so parallel branches only
  share one-line registrations.
- **Schemas:** `wt.conductor.cost.v1`, `wt.conductor.wakeups.v1`, `wt.conductor.decision.v1`
  and `wt.conductor.brief.v1`. Exports stay `wt.campaign.export.v1`: new columns are only
  appended, as that contract allows.
- **Config** sits under `agentic.conductor.*` in the user config, with code-held defaults.
  A bad value falls back to its default, as `events.settings` does.

## Breakdown / sub-specs

- [ ] SPEC-0299 — Conductor cost measurement: session registry, `wt queue conductor-cost`, conductor share in `wt campaign export` (project: Meta-Tools)
- [ ] SPEC-0300 — Quiet conductor: the follower suppresses informational events, prints a digest, and counts wakeups (project: Meta-Tools)
- [ ] SPEC-0301 — Conductor state handoff and decision log: `wt queue decision`, `wt queue conductor-brief`, compaction rule (project: Meta-Tools) (depends_on SPEC-0299)
- [ ] Routine parks decided by the queue (project: Meta-Tools) — `base_moving` (9 parks) resumes itself with backoff, and a `cost_cap` park right after a transient run failure (no new work done) is resumed by the queue, not the conductor. Each auto-decision is written to the SPEC-0301 decision log as `by: queue`.
- [ ] Fewer `rebase_conflict` parks (project: Meta-Tools) — classify the 34 conflict parks by file (generated ledger, shared CLI registration modules, specs), then fix the dominant cause at the source: regeneration on close, registration split or a merge driver.
- [ ] comfort-assistant on the loop with PR hand-off (project: Meta-Tools) — `.wt/project.yaml` in comfort-assistant (gates, `merge: pr`), steward armed, one real idea taken from queue to merged PR with the conductor deciding every park.
- [ ] MVP scorecard: `wt queue mvp-report` (project: Meta-Tools) — computes each exit criterion below over a window from the three logs plus the export, and flags parks with no decision record. (depends_on SPEC-0299, SPEC-0300, SPEC-0301)

Sequencing: SPEC-0299 and SPEC-0300 can run in parallel. SPEC-0301 follows SPEC-0299,
because the brief reports the conductor's current context size from the cost reader. The
routine-parks and conflict children can start once SPEC-0301 is accepted, since they log
through it. The comfort-assistant child comes last, so it runs on the quiet, measured
conductor. The scorecard can start as soon as its three dependencies merge.

## MVP exit criteria

Each criterion is measured over a **window of at least 20 consecutive merged (or merged-PR)
campaigns** that start after SPEC-0299, SPEC-0300 and SPEC-0301 are all merged. The
baselines are the 2026-10-30 figures.

| # | Criterion | Target | Baseline |
|---|---|---|---|
| E1 | Conductor cost per merged campaign (`wt queue conductor-cost`: conductor USD in window ÷ merges in window) | **≤ $1.00** | ≈ $2.83 ($263 / 93) |
| E2 | Median conductor context per turn (input + cache read + cache write tokens of each conductor API turn) | **≤ 150k tokens** | ≈ 457k |
| E3 | Conductor wakeups per merged campaign (lines delivered by the `--for conductor` follower ÷ merges) | **≤ 5** | not recorded (≈ 25.7 turns per merge) |
| E4 | Parks with a structured decision record (`decisions.jsonl`, by the conductor or by the queue) before the campaign leaves PARKED | **100%** | 0% (free-text notes only) |
| E5 | A comfort-assistant idea queued, implemented, and merged through a PR, with the human pulled in only for scope decisions | **≥ 1** | 0 |
| E6 | Conductor restarts or compactions in the window that re-oriented from `wt queue conductor-brief` with no `park_undecided` raised in the following 15 min | **≥ 1, and all of them** | n/a |

The $1.00 target allows a 65% cut. Bringing context from about 457k to 150k cuts each
turn's cache-read cost by about two thirds. Cutting turns per merge from about 26 to 5
wakeups, each costing a few turns, cuts the turn count by roughly half or more. Either cut
alone gets close to the target, and both together clear it with margin.

## Acceptance criteria

- [ ] SPEC-0299, SPEC-0300 and SPEC-0301 are `done`, and the remaining Breakdown bullets
      are specs that are `done` or `superseded`.
- [ ] E1–E6 hold over one qualifying window, as reported by `wt queue mvp-report` (or, before
      it ships, by `wt queue conductor-cost`, `wt queue wakeups` and the decision log). The
      report is pasted into this spec's *As shipped* section.
- [ ] The `/wt-conduct` skill's decision table names a rule id for every row, and each
      decision record in the window carries one of those ids.
- [ ] No frozen area received new work during the epic. Check: `wt queue list
      --include-terminal --json` lists no campaign admitted after this spec's acceptance
      whose spec is tagged with a frozen area.

## Test plan

- **Integration (offline, `tests/test_agentic_mvp_integration.py`, added with the scorecard
  child):** fake runner and temp git repos, with a scripted conductor transcript fixture and
  scripted follower deliveries. A queue of 3 campaigns runs to merge, with one park and one
  decision. Assert that `conductor-cost` attributes the fixture's tokens to the 3 merges,
  that `wakeups` counts only decision-needing deliveries plus digests, that the brief lists
  the park with its decision, and that the scorecard's E1–E4 numbers equal hand-computed
  values.
- **Manual verification:** run the real conductor through a qualifying window, then
  `wt queue mvp-report --since <window start>`, and record the table in *As shipped*. For
  E5, link the comfort-assistant PR. For E6, quote the decision records written right after
  the restart.
- **Regression guard:** full `uv run pytest`. `wt queue events` (history) still returns every
  event type.

## Rollout / sequencing

1. SPEC-0299 and SPEC-0300 in parallel, then SPEC-0301. Each merges through the queue.
2. Configure model prices (`agentic.conductor.prices`). Backfill the current conductor
   session with `wt queue conductor-session add <id>` to get a measured baseline.
3. Restart the conductor on the new skill. The window starts at the first campaign admitted
   after that.
4. The routine-parks and conflict children, then comfort-assistant onboarding, then the
   scorecard.
5. When the exit criteria hold, mark this epic `done` and lift the freeze.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; `uv run pytest` green.
- [ ] Exit criteria E1–E6 met over one window; the epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

- None. The targets are set from the 2026-10-30 evidence and may be tuned from the first
  window's data by a superseding note in *As shipped*. They are not a reason to escalate.
