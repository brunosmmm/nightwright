---
id: SPEC-0405
title: "Rich queue TUI: prioritized live queue, campaign detail popup, closed-campaign history and a cross-campaign findings and decisions browser over one shared history layer"
status: accepted
owner: user
created: 2026-11-03
source_idea: IDEA-882
kind: epic
tags: [tui, agentic, observability]
---

## Context

Promoted from idea `IDEA-882`. The human asked on 2026-11-03 (near-verbatim):

> "Our TUI is very bad and basic, especially for sections like queue. I want a much richer
> experience, and particularly the ability to double click an item and get a popup that
> thoroughly explains the timeline of the developments, cost, time, what phase is dominating
> the time, and not only that. I want a navigable history of all findings and decisions by all
> agents that record findings and decisions. And I need a better interface that shows the
> same for PAST, already closed items, not just what is live in the queue. What is live in the
> queue should have a clear ordering of priority and active states."

**What exists today** (all `done`):

- **The app.** `nw tui` is one Textual app (`src/nightwright/tui/app.py`, `IdeaDesk`) with
  these tabs:
  - 1 ideas
  - 2 hub
  - 3 queue ([SPEC-0241](./0241-tui-queue-tab-over-the-queue-dashboard.md))
  - 4 mailbox ([SPEC-0249](./0249-tui-mailbox-tab.md)), with the escalation inbox of
    [SPEC-0250](./0250-tui-escalation-inbox.md)
  - 5 events ([SPEC-0251](./0251-tui-events-tab.md))
- **The queue tab.** It is one `DataTable` over `queue.dashboard_payload`.
  - Columns: a `section` column (needs you / conductor / active / waiting / backlog /
    delivered), then campaign, spec, title and a free-text meta column.
  - It polls a `stat()` signature every 2 s and reloads every 15 s.
  - Its actions (park, resume, prioritize, abandon) go through the CLI's `plan_*`/`apply_*`.
  - Active rows come in `queue_payload` order, with no stated priority order.
  - The meta column mixes "why" text with numbers.
  - There is no time-in-state, no cost-vs-cap bar and no loop count.
- **The campaign screen.** Enter on a row pushes `CampaignScreen`
  ([SPEC-0246](./0246-tui-campaign-detail-screen.md)).
  - It has: a header (state, loops, runs, cost); a ledger timeline filterable by group
    (transitions/runs/gates/findings/notes); a detail pane per entry (run verdict, gate log
    tail); a review panel.
  - It lacks: per-phase time or cost; the dominant phase; the run stream; conductor
    decisions, mail and escalations; links between findings, runs and resolutions;
    double-click.
  - It opens only from the queue tab, so campaigns closed more than 24 h ago cannot be
    reached.
- **`nw queue watch`.** Per-reviewer review chips
  ([SPEC-0277](./0277-wt-queue-watch-shows-review-status-per-reviewer.md)) and journey tracks
  ([SPEC-0326](./0326-queue-watch-redesign-fixed-sections-adaptive-cards.md), `journey.py`)
  feed it. The TUI uses only part of them.
- **`nw campaign export`.** [SPEC-0236](./0236-wt-campaign-export-dataset-for-mis-sizing-analysis.md),
  `campaign_export.py`, already derives `time_in_state_s`, cost by persona and finding rows
  into flat tables for offline analysis. It is the closest thing to a history model, but it
  is a batch export, not a per-campaign or interactive read.
- **SPEC-0400.** Epic [SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md)
  (accepted, children IDEA-874..877) plans a web dashboard. Its child 3 (IDEA-876) has a
  per-campaign drilldown (`GET /api/campaigns/<cid>`: ledger tail, runs, conductor decisions,
  run tail).

**TUI tests under `uv run`.** These tests used to fail to import under a plain `uv run`,
because Textual was the opt-in `tui` extra (SPEC-0099). A plain `uv run` / `uv sync` drops
extras, so `textual` was missing there. Commit `f2cd9d8c` (2026-11-02, SPEC-0368) made
`textual>=8,<9` a core dependency and left `tui` as an empty alias.

Verified on 2026-11-03:
- A fresh worktree venv created by `uv run` imports textual 8.2.8.
- `uv run pytest tests/test_tui_queue_tab.py` passes: 38 tests in 78 s.

What remains is a silent-skip hazard. `tests/conftest.py` still `collect_ignore`s five TUI
files when Textual is absent, and the TUI test files `importorskip("textual")` /
`skipif(not textual_available())`. Any lean environment would therefore report green while
running no TUI test. Child 3 removes these guards and pins their absence.

Related prior work (`nw ideas --query`):

| Idea | What it covers |
|---|---|
| IDEA-084 | TUI campaign detail, promoted to SPEC-0246 |
| IDEA-037 | watch tab |
| IDEA-085 | mailbox tab |
| IDEA-086 | events tab |
| IDEA-087 | escalation inbox |
| IDEA-873, IDEA-874..877 | SPEC-0400 |

None of them covers phase timing, cross-campaign findings or closed-campaign history. This
epic extends them and does not duplicate them.

This needs an epic, not one spec. The work spans:
- a shared, UI-independent data layer with a stable schema, also consumed by SPEC-0400;
- a few missing writer-side records;
- test infrastructure that runs the real app against real data;
- four distinct views.

## Data inventory

Surveyed read-only on 2026-11-03 in `~/.local/share/nightwright/agentic/`: 217 campaign dirs
(191 MERGED, 14 ABANDONED, 12 live), 1.1 GiB in total. The code that writes each source was
read too. Schemas carry either the `wt.` prefix (before
[SPEC-0330](./0330-rename-to-nightwright.md)) or the `nw.` prefix, and readers must accept
both.

Unless a source says otherwise, it is kept forever: no code deletes it.

**Per campaign** (`campaigns/<cid>/`):

- **`state.json`** (`nw.campaign.v1`)
  - Records: current and previous state, priority, profile, sizing, counters,
    `cost.{total_usd,cap_usd,by_persona}`, `last_gate`, `last_verdict`, `last_review`,
    `review_carry`, `park`, `close`, `admission`, timestamps.
  - Gap: the counters are per-loop streaks that reset, so totals must come from the ledger
    (`campaign_export` already notes this).
- **`ledger.jsonl`** (`nw.ledger.event.v1`): 39 event types, 24 100 lines, 31 MiB in total.
  - `transition`: from, to, reason, ts.
  - `run.start` / `run.end`: persona, run_id, duration, cost, charged, usage, exit.
  - `cost`.
  - `gate`: gate_seq, started_at, duration_s, per-gate results.
  - `finding` (5 379): persona, severity, gate, title, detail, files, ac, category.
  - `followup`: finding key and the id of the filed idea.
  - `park` / `resume`: by, note, loops_reset.
  - `priority`.
  - `note` (2 376, many `agent:conductor`).
  - `human_decision`: escalation, choice, waive, attest.
  - `spec_review`.
  - `close`: mode, merged_sha, pr_url.
  - `gate_waiting` (waited_s), `flaky_recorded`, `loop_divergence`, `worker_failed`,
    `rebase_resolved`, and more.
  - **Gap:** `finding` has no `run_id` and no `key`. The producing run must be inferred (the
    last `run.end` of that persona before the finding), and the follow-up key recomputed
    with `followups.finding_key`.
  - **Gap:** no event marks a review or gate triager's waive or retry decision. It lives only
    in that run's `run.json` verdict.
- **`runs/<run>/run.json`** (`nw.run.v1`)
  - Records: persona, model, effort, budget, timeout, started_at, duration_s,
    exit/timeout/interrupt, cost (plus whether it is estimated), full usage, session id, and
    the `verdict` (status, summary, changed_files, tests_run, must_fix, blockers). Triager
    verdicts add `waive[{id,reason}]` and a note.
  - Gap: 14 of 2 712 runs predate `run.json` and have only `result.json`.
  - Gap: triager `waive` ids are `persona#index` into the reviewed round, not finding keys.
- **`runs/<run>/stream.jsonl`** ([SPEC-0362](./0362-stream-json-capture-of-persona-runs-and-nw.md))
  - Records: the persona's full stream-json transcript (tool calls, text).
  - Present in 797 of 2 712 runs (every run since SPEC-0362), 110 MiB in total.
  - Gap: older runs have no stream; the `result` text in `result.json` is the fallback.
- **Other run files:** `runs/<run>/{launch,exit,proc,child,request}.json`, `stderr.log`,
  `wrapper.log`, `prompt.md`, `result.json`.
  - Records: launch argv and env, exit/kill/signal and ended_at, process ids, the request,
    stderr.
  - No gap for this epic.
- **`gates/NNN-<gate>[.rK].{log,json}`**
  - Records: exit, duration and log for every gate run and retry.
  - No gap.
- **`spec_at_open.md`**
  - Records: the spec as admitted. Present in 169 of 217 campaigns.
  - No gap.

**Cross-campaign logs and records:**

- **`conductor/decisions.jsonl`** (`nw.conductor.decision.v1`, read by `nw queue decisions`;
  append-only)
  - Records 440 conductor and queue decisions: rule; kind (resume, wait, dispatch,
    ask_steward, escalate, apply_answer, abandon, merge, …); action; rationale; campaign;
    event_id.
  - Gap: 61 of 440 have no `campaign`, although many name one in the text.
- **`escalations/ESC-NNNN.json`**
  ([SPEC-0232](./0232-escalation-records-wt-queue-escalate-and-notify.md),
  [SPEC-0235](./0235-escalation-answers-decide-waive-attest-conductor-applies.md))
  - Records: question, options, recommendation, facts, park, status, answer,
    answered_by/at, withdrawn.
  - No gap: the ledger's `human_decision` cites the ESC id.
- **`mailbox/<inbox>.jsonl`** (`nw.message.v1`; append-only)
  - Records: conductor ↔ steward ↔ human threads, with kind (question, answer, request,
    info), campaign and re.
  - Gap: 315 of about 1 000 messages have no `campaign`.
- **`events.jsonl`** (`nw.event.v1`, read by `nw queue events`; never trimmed)
  - Records 4 518 alert-stream events (parked, merged, run_failed, escalation,
    conductor_ping, gate_job, …) with `source.ledger_seq`.
  - Gap: for campaign facts it only repeats the ledger. It adds value only for alerts with
    no campaign (runner_dead, sentinel, worker_*).
- **`runner.jsonl`** (read by `nw queue runner-log`)
  - Records: runner start, stop, draining, tick steps, admission_blocked.
  - Gap: it is the only record of "runner down", and it is not per campaign.
- **Spec reviews:** `reviews/<SPEC>/<sha>.json` (`nw.spec.review.v1`),
  `spec-reviews/<run>/` and `spec-reviews.jsonl`
  - Records: spec-review verdicts (approve, revise, split), rationale, findings, cost.
  - Gap: spec-only reviews with no campaign exist for outbound specs.
- **`flaky-tests.json`** and **`sentinel/<project>.last.json`** (plus `.logs/`)
  - Records: cross-campaign flaky test ids (campaigns, sources, first and last seen), and
    base-branch sentinel results.
  - Gap: neither links to a campaign beyond the flaky record's `campaigns[]`.
- **Follow-up ideas** (`followups.py`, [SPEC-0207](./0207-automatic-review-followups.md),
  [SPEC-0239](./0239-grouped-review-follow-ups-one-idea-per-campaign.md); the ideas live in
  the org files)
  - Records: one grouped idea per campaign (`nwfug:<cid>`) with one `nwfu:<key12>` checklist
    line per advisory or waived finding. The ledger `followup` event links each key to the
    idea id.
  - No gap.
- **Conductor cost** (`conductor_cost.py`, [SPEC-0299](./0299-conductor-cost-measurement.md);
  derived, not stored)
  - Records: conductor session tokens and dollars, which `campaign_export` attributes per
    campaign.
  - No gap.
- **`gatejobs/`, `remote-jobs/` and worker caches**
  - Records: remote gate job dirs.
  - **Pruned:** gate jobs after 7 days (`gate_jobs.retention_days`), remote job refs per
    campaign, and worker caches by `worker gc`.
  - The history must not depend on them. Each campaign keeps its own copies under `gates/`.

**Retention verdict.** Closed campaigns keep everything this epic needs:
- `nw queue gc` ([SPEC-0275](./0275-worktree-and-branch-garbage-collection-for.md)) removes
  only worktrees and merged branches.
- `store.clear_campaign_tmp` removes only `<cid>/tmp`.
- Nothing deletes `state.json`, the ledger, `runs/` or `gates/`.
- The only pruned sources (gate jobs, remote job refs, worker caches) are not inputs of the
  history.

**Derivable today, with no new record:**
- **Per-phase wall time:** from `transition` timestamps (as
  `campaign_export._time_in_state` does).
- **Per-phase cost:** attribute each `run.end` cost to the state the campaign was in at its
  `run.start`.
- **Split of each phase visit:**
  - agent-run time, from run durations;
  - gate time, from the gate's `duration_s`;
  - gate wait, from `gate_waiting.waited_s` plus the gap to the gate's `started_at`;
  - the remainder. The part of it when no runner held the lease comes from `runner.jsonl`
    stop → start intervals.

**Missing, added by child 1:**
- `finding.run_id`
- `finding.key`
- a `triage` ledger event that records each triager waive or retry decision with the finding
  keys it resolves
- a best-effort `campaign` stamp on decisions and mail that name exactly one campaign

## Goals / Non-goals

**Goals**
- **G1. Live queue ordering.**
  - The live queue shows one stated ordering: state groups in a fixed order, then priority
    and admission order inside each group.
  - Every row shows why it is waiting.
  - The order agrees with the runner, and rows never jump under the cursor.
- **G2. Detail popup.** Double-click or Enter on any campaign, live or closed, opens a detail
  popup with:
  - the timeline of everything that happened;
  - per-phase wall time and cost, with the dominant phase called out and drawn as bars;
  - a per-run drilldown that includes the stream tail;
  - every finding and decision on the campaign, cross-linked to the run that produced it,
    the decision that resolved it and its follow-up idea.
- **G3. Closed campaigns.** Closed campaigns (merged, handed off, abandoned) can be browsed,
  searched and filtered by project, spec, state, date and cost. They open the same popup.
- **G4. Findings and decisions browser.** One cross-campaign browser lists all findings and
  decisions by all agents. It filters by agent, kind, project, severity, date and status, and
  has full-text search.
- **G5. Shared history layer.** One UI-independent layer (`agentic/history.py`,
  `nw.campaign.history.v1`, `nw campaign history`) is shared with SPEC-0400's drilldown.
- **G6. Performance.** The views stay responsive at hundreds of closed campaigns, and the
  live TUI does not hammer the data dir.
- **G7. Real tests.** The TUI tests run in every `uv run pytest` and drive the real app
  against real captured campaign data.

**Non-goals**
- New mutation paths. v1 reuses only the existing actions (Decision 6).
- Editing or deleting history, and any change to retention policy.
- The web dashboard itself (SPEC-0400). This epic provides its data layer.
- Cross-host aggregation. Remote workers' data stays as it is today.
- Charts beyond terminal bars and sparklines. No plotting dependency is added.

## Decision

1. **One shared history layer first, then the views.**
   - `src/nightwright/agentic/history.py` holds pure functions over the data dir. It never
     writes the data dir and never imports `nightwright.tui` or Textual.
   - It serves three schemas:
     - `nw.campaign.history.v1`: one campaign.
     - `nw.history.index.v1`: one summary row per campaign.
     - `nw.history.record.v1`: one finding or decision.
   - It comes with CLIs (`nw campaign history`, `nw campaign records`) and a disposable cache
     under `cfg["cache_dir"]`.
   - `campaign_export` and the TUI both call it, and SPEC-0400's drilldown should too (see
     *Cross-dependency with SPEC-0400*). The TUI's `agentic_model` keeps only the view
     shaping.
2. **Missing records are added where they are written, not guessed forever.**
   - Child 1 adds the provenance fields.
   - The history layer reads those fields when present and infers them otherwise.
   - Each inferred link is marked `linked_by: "inferred"` (or `"text"`). Old campaigns still
     render, and the UI shows how confident each link is.
3. **Live queue: grouped sections in a fixed order, sorted on stable keys only.**
   - **Group order:**
     1. Needs you
     2. Conductor handling
     3. Closing
     4. Reviewing
     5. Gating (GATING, TRIAGING)
     6. Building (OPENING, SPEC_WRITING, SPEC_REVIEW, IMPLEMENTING)
     7. Waiting
     8. Queued
     9. Delivered (24 h), collapsed by default
   - Active work is listed furthest along first, because what is closest to landing is what
     the human acts on.
   - **Order inside a group:**
     - Active groups: `(priority, created_at, campaign id)`.
     - Queued: the runner's own admission order, i.e. `nw.queue.backlog.v1` `position`, the
       function behind `nw queue backlog`.
     - Parked: by parked-at.
   - Sort keys never include ages, costs or other values that change while a row is
     visible. A row moves only when its state group changes, and the cursor follows the
     campaign id.
4. **The detail popup is a `ModalScreen`** at about 96 % × 94 % of the current tab. It reads
   as the popup the human asked for and still has room for a timeline.
   - It opens on Enter and on double-click (`Click.chain == 2`) from the queue, history and
     findings views.
   - It has five tabs: Overview, Timeline, Runs, Findings & decisions, Mail.
   - Esc or q closes it and returns the cursor to the same row.
   - It replaces `CampaignScreen`. Child 5 supersedes SPEC-0246 and carries its timeline
     filter and review panel forward.
5. **History and findings are new desk tabs**: 6 History and 7 Findings. They read the index
   and record caches and open the same popup.
6. **v1 actions: only the existing ones.**
   - The queue view and the popup keep SPEC-0241's park, resume, prioritize and abandon,
     through `agentic_model.queue_action` (`plan_*`, then a confirm, then `apply_*`).
   - The popup offers "answer escalation" only by pushing SPEC-0250's existing answer flow
     (`escalations.plan_answer` / `apply_answer`, the SPEC-0235 path).
   - History and Findings are strictly read-only.
   - The UI in this epic adds no new write path anywhere.
7. **The tests use the real app and real data.**
   - A sanitized fixture captured from real closed campaigns backs the data-layer and UI
     tests. A capture tool makes it reproducible.
   - Textual `run_test` / Pilot drives the real `IdeaDesk`.
   - Plain-text screen snapshots go through the existing pyte path
     ([SPEC-0115](./0115-desk-tree-mode-is-not-a-tree-needs-real-expand.md)).
   - A perf test runs over 500 cloned campaigns, and the human runs a manual checklist.
   - The Textual collect-skip guards are removed, so a missing Textual fails loudly instead
     of skipping silently.

## Architecture / cross-cutting design

### Phases

`history.PHASES` defines, once, a fixed mapping from campaign state to phase:

| Phase | States |
|---|---|
| `queue` | QUEUED, OPENING |
| `spec` | SPEC_WRITING, SPEC_REVIEW |
| `implement` | IMPLEMENTING |
| `gate` | GATING, TRIAGING |
| `review` | REVIEWING |
| `close` | CLOSING |
| `parked` | PARKED |
| `waiting` | WAITING |

A phase *visit* is a maximal interval between transitions. Loop numbers come from the
transitions back into IMPLEMENTING.

**Per-phase fields:**

| Field | Meaning |
|---|---|
| `wall_s` | sum of the phase's visits |
| `run_s` | durations of runs whose `run.start` falls inside a visit |
| `gate_s` | gate `duration_s` |
| `gate_wait_s` | `gate_waiting.waited_s`, plus the gap from entering GATING to the gate's `started_at` |
| `runner_down_s` | overlap with `runner.jsonl` stop → start gaps |
| `other_s` | the remainder, never negative (overlaps are clipped) |
| `cost_usd` | costs of runs started in the phase |
| `runs` | the number of runs started in the phase |

**Dominant phase:**
- `dominant.wall` is the phase with the largest `wall_s`.
- `dominant.cost` is the phase with the largest `cost_usd`.
- When the wall-time winner is `queue`, `parked` or `waiting`, `dominant.working` also names
  the largest of the other phases. That way both "it sat parked" and "gates were slow" stay
  visible.
- Ties break in phase-table order.

`campaign_export.time_in_state_s` is computed by the same function, and a test pins that the
two agree on the fixture.

### Schemas (frozen at v1; fields may only be appended)

```
nw.campaign.history.v1 = {
  schema, campaign, generated_at, signature,
  header: {campaign, kind, spec_id, idea_id, title, project, profile, size, tone, state,
           outcome (merged|handed_off|abandoned|null), priority, created_at, closed_at,
           wall_s, active_s, loops, loop_max, runs, cost_usd, cap_usd, conductor_usd,
           merged_sha, pr_url, followup_idea, park},
  phases: [{phase, visits, wall_s, run_s, gate_s, gate_wait_s, runner_down_s, other_s,
            cost_usd, runs}],
  dominant: {wall, cost, working},
  timeline: [{seq, ts, kind, phase, loop, by, summary, ref}],
  runs: [{run_id, persona, phase, loop, model, effort, started_at, duration_s, cost_usd,
          budget_frac, exit_code, status, verdict_status, summary, has_stream}],
  gates: [{gate_seq, started_at, duration_s, ok, results: [{name, required, ok, exit_code,
           retries, log}]}],
  records: [nw.history.record.v1],
  gaps: [str]
}

nw.history.record.v1 = {
  id,
  ts, source (ledger|run|decisions|escalation|mailbox|spec_review|flaky|sentinel),
  kind (finding|verdict|triage|decision|escalation|answer|human_decision|mail|followup|
        spec_review|close_check|note|flaky|sentinel),
  agent,
  campaign, spec_id, project, severity, title, text,
  status (open|fixed|waived|filed|carried|answered|withdrawn|null),
  links: {run_id, resolved_by, followup_idea, escalation, thread, campaign},
  linked_by (field|inferred|text)
}

nw.history.index.v1 = one row per campaign: header fields + dominant + phase wall/cost
  + finding counts by severity + record count + signature
```

Notes on the fields:
- `timeline`: one row per ledger event. `run.start` and `run.end` merge into one `run` row,
  and decisions, mail and escalations are interleaved by time.
- `gaps`: what could not be derived, e.g. "run 001: no run.json".
- `id` (record): stable, `<source>:<locator>`, e.g. `ledger:<cid>:57`.
- `agent`: the persona, `conductor`, `steward:<P>`, `human` or `queue`.

**Finding status** is set as follows:

| Status | When |
|---|---|
| `waived` | a `triage` event or a `human_decision` waive names its key |
| `filed` | a `followup` event names its key; `followup_idea` links the idea |
| `carried` | it is in `review_carry` |
| `fixed` | the same reviewer did not raise the key again in a later round, and the campaign merged; the UI labels this "not re-raised" |
| `open` | none of the above |

### CLI

```
nw campaign history CID [--json]
nw campaign history --list [--project P] [--state merged|handed_off|abandoned|live|all]
    [--spec S] [--since D] [--until D] [--min-cost X] [--json]
nw campaign history --rebuild-cache
nw campaign records [--campaign C] [--agent A] [--kind K] [--project P] [--severity S]
    [--status S] [--since D] [--until D] [--query TEXT] [--limit N] [--json]
```

- `nw campaign history CID`: one campaign, as rich text or as `nw.campaign.history.v1`.
- `nw campaign history --list`: `nw.history.index.v1` rows.
- `nw campaign history --rebuild-cache`: drops the cache and rebuilds it.
- `nw campaign records`: `nw.history.record.v1` records.

### Cache and performance

**Location.** The cache lives in `<cache_dir>/history/v1/`. That is outside the data dir, so
SPEC-0400's invariant ("every other data-dir file byte-identical") holds, and the cache can
be deleted at any time.

**Contents:**
- `campaigns/<cid>.json`: the full history plus its signature.
- `index.jsonl`.
- `records.jsonl`, with a lowercased text field for search.
- `sources.json`: per global log, `{size, mtime_ns, offset}`.

**Invalidation:**
- A campaign is keyed by `(ledger size, ledger mtime_ns, state.json mtime_ns, runs/ mtime_ns)`.
- The append-only global logs (decisions, mailbox, events, runner.jsonl) are parsed
  incrementally from the stored offset. If a log shrinks or its inode changes, it is re-read
  in full.
- Writes are atomic (temp file, then rename).
- A corrupt or wrong-version cache is rebuilt, never trusted.

**Runtime behaviour:**
- A closed campaign is computed once.
- A live view stats only non-terminal campaigns and the global logs, and only for visible
  tabs.
- The TUI does no file I/O on the event-loop thread
  ([SPEC-0129](./0129-tui-poll-reload-off-the-event-loop-thread.md)); loads run in workers.
- A popup on a terminal campaign never polls.
- A popup on a live campaign polls that campaign's signature every 2 s.
- The run tail follows `stream.jsonl` by offset every 1 s, and only while its pane is
  visible.

**Budgets.** These are measured on a 500-campaign clone of the fixture. Tests assert them with
×3 slack.

| Operation | Budget |
|---|---|
| Cold index build (shows a progress line) | ≤ 10 s |
| Warm index load | ≤ 300 ms |
| Popup open | ≤ 500 ms |
| Records filter or full-text query over 50 000 records | ≤ 200 ms |
| Live-queue repaint | ≤ 50 ms |

### Cross-dependency with SPEC-0400

- **What SPEC-0400 should use.** SPEC-0400's child 3 (IDEA-876, promoted as SPEC-0402 while this epic was written; `GET /api/campaigns/<cid>`)
  should return `history.campaign_history(cfg, cid)` (`nw.campaign.history.v1`) instead of
  assembling its own ledger tail, runs and decisions.
- **Shared run tail.** Its run tail uses the same `tail.read_lines` and `tail.Renderer` as
  this epic's run pane.
- **How the dependency is recorded.** This epic does not edit SPEC-0400. A log note on
  IDEA-876 records the dependency.
- **If IDEA-876 goes first.** It builds on `history.py` once child 2 lands, or adopts it in a
  follow-up.
- **Dependency direction.** `history.py` stays importable without Textual and without the
  dashboard package. Both UIs depend on it, and it depends on neither.

### Invariants every child respects

1. History, index and records are pure reads of the data dir. The only writes are:
   - the cache under `cache_dir`;
   - in child 1, the queue's own ledger, decision and mail writers.
2. No new mutation path. UI actions call only `agentic_model.queue_action` and the SPEC-0250
   escalation flow.
3. Untrusted text is never rendered as Rich markup
   ([SPEC-0265](./0265-operator-surfaces-survive-bad-data-and-never.md)). This covers
   findings, mail, summaries and stream output.
4. Bad or partial files produce a `gaps` entry, never a crash.
5. Both `wt.` and `nw.` schema prefixes are accepted everywhere.
6. Every view can be driven entirely from the keyboard. The mouse (double-click, clicking a
   tab, scrolling) is a shortcut, never the only way.

## Breakdown / sub-specs

- [ ] Ledger provenance: finding run_id and key, triage events, campaign stamps on decisions and mail (project: Meta-Tools) — `finding` ledger events gain `run_id` (the run whose verdict or gate produced it) and `key` (`followups.finding_key`); a new `triage` ledger event per applied review/gate triager verdict (`persona`, `run_id`, `waive[{key, reason}]` resolved from `persona#index`, retry/fix decisions); `nw queue decision` and `nw mailbox send` stamp `campaign` when absent and the text names exactly one existing campaign id (never guess between two); `nw.ledger.event.v1` docs updated; old events untouched; tests over the real writers
- [ ] Shared campaign history data layer, nw.campaign.history.v1 schema, CLI and real-data fixture (project: Meta-Tools) — `agentic/history.py` (pure; no Textual import): phases, visits, per-phase wall/run/gate/gate-wait/runner-down/other time and cost, dominant phase, merged timeline, runs, gates, `nw.history.record.v1` records from ledger/run.json/decisions/escalations/mailbox/spec reviews/flaky/sentinel with status and links (field, then inferred, then text), `gaps`; `nw.history.index.v1`; disposable cache under `cache_dir/history/v1` with signatures and incremental global-log offsets; `nw campaign history CID|--list|--rebuild-cache` and `nw campaign records` with filters and `--json`; schemas in `schemas.py`; `campaign_export` time-in-state reuses it; `tools/capture_history_fixture.py` plus a committed sanitized fixture of real closed campaigns under `tests/fixtures/agentic_history/`; perf budgets on a 500-campaign clone (depends_on child 1)
- [ ] TUI test harness: real-app pilots on real fixture data, no silent textual skips, text snapshots (project: Meta-Tools) — remove the `collect_ignore` / `importorskip("textual")` / `needs_textual` guards (textual is core since SPEC-0368) plus a guard test that they never return; `tests/tui_harness.py` (cfg over a copied fixture data dir, start `IdeaDesk` with `run_test`, `pilot_until` waits, double-click via `Pilot.double_click`, visible-text extraction); plain-text screen snapshots through the SPEC-0115 pyte path at 160x48 regenerated only with `NW_UPDATE_SNAPSHOTS=1`; a scale-clone helper reused by perf tests; a runtime budget for the TUI test files (depends_on child 2)
- [ ] Live queue view: state-grouped sections with priority, admission order and why-waiting (project: Meta-Tools) — supersedes SPEC-0241's table layout (its actions carried forward verbatim): fixed group order needs you, conductor, closing, reviewing, gating, building, waiting, queued (backlog `position`), delivered (collapsed); header rows with counts that the cursor skips; columns pri / campaign and spec / title / state plus activity (running persona and elapsed, gate name, waiting on) / loop n of max / cost vs cap bar / time in state / why; stable sort keys only and the cursor follows the campaign id; elapsed times tick from cached timestamps without reloading; Enter and double-click open the detail (the SPEC-0246 screen until child 5 lands); SPEC-0277 per-reviewer chips kept; test that the TUI order equals `nw queue backlog --json` and the `dashboard_payload` grouping on the fixture (depends_on child 3)
- [ ] Campaign detail popup: timeline, phase time and cost with dominant phase, runs with stream tail, findings and decisions (project: Meta-Tools) — supersedes SPEC-0246: a `CampaignDetail` ModalScreen (about 96% x 94%) over `nw.campaign.history.v1`, opened by Enter or double-click from queue, history and findings, Esc returns to the same row; tabs Overview (header, phase table with wall/cost/share and the dominant phase highlighted, unicode bar breakdown, cost-per-run sparkline, gaps), Timeline (grouped by loop, the SPEC-0246 group filter kept), Runs (table plus run pane: verdict, must_fix/blockers, changed files, tests, stderr tail, `tail.Renderer` stream tail of the last 200 lines with live follow only while visible, `result` text for runs before SPEC-0362), Findings & decisions (filter agent/kind/severity/status; `r` jumps to the run, `d` to the resolving decision, `i` shows the follow-up idea), Mail (campaign threads); only the existing park/resume/prioritize/abandon and the SPEC-0250 escalation answer flow; live campaigns poll their signature, closed ones never (depends_on child 2 and child 3)
- [ ] Closed-campaign history browser tab (project: Meta-Tools) — desk tab 6 `History` over `nw.history.index.v1`: closed campaigns newest-closed first (`a` toggles live ones in), columns closed / outcome / project / spec and title / profile / size / loops / runs / cost / wall / active / dominant phase / must_fix and major / follow-ups; filter bar `project: spec: state: since: until: cost>N dominant: text` (events-tab parser style) and sort cycling; cold index build in a worker with progress, warm load from cache; Enter or double-click opens the child-5 popup; read-only (depends_on child 2 and child 5)
- [ ] Cross-campaign findings and decisions browser tab (project: Meta-Tools) — desk tab 7 `Findings` over `nw.history.record.v1`: every finding, verdict, triage, conductor decision, escalation and answer, human decision, steward/conductor/human mail, follow-up, spec review, flaky and sentinel record, newest first; filters `agent: kind: project: severity: status: campaign: since: until:` plus full-text search over title and text; record pane with links; `c` opens the campaign popup focused on that record, `r` its run, `d` its resolving decision; campaign-less records listed too; read-only; 50 000-record query budget (depends_on child 2 and child 5)

**Sequencing and dependencies:**
1. **Child 1 (writer provenance) goes first.** It is small, and landing it first means child 2's
   `linked_by: field` path is exercised by real writer output.
2. **Child 2 (history layer and fixture)** is the contract everything else reads. SPEC-0400's
   IDEA-876 should wait for it too.
3. **Child 3 (test harness)** needs child 2's fixture.
4. **Child 4 (live queue)** needs only child 3. It can run in parallel with child 5.
5. **Child 5 (detail popup)** needs children 2 and 3.
6. **Children 6 and 7 (history and findings tabs)** both need the popup (child 5). They can
   run in parallel with each other.

**Recommended profiles:**

| Child | Profile | Why |
|---|---|---|
| 1 | balanced | |
| 2 | thorough | cross-UI contract; the schema is frozen at v1 |
| 3 | balanced | |
| 4 | balanced | |
| 5 | thorough | largest UI surface |
| 6 | balanced | |
| 7 | balanced | |

## Acceptance criteria

- [ ] **AC1.** `nw campaign history <cid> --json` validates against `nw.campaign.history.v1`,
  with no exception, for every campaign in the fixture and in the live data dir (217 at
  survey time). The per-phase `wall_s` values sum to the campaign's wall time within 1 s, and
  its time-in-state equals `nw campaign export`'s.
- [ ] **AC2.** `finding` events of new campaigns carry `run_id` and `key`, and triager
  decisions produce `triage` events. Old campaigns still render, with their links marked
  `inferred`.
- [ ] **AC3.** The queue tab:
  - shows the fixed group order;
  - lists queued rows in exactly the `nw queue backlog --json` `position` order;
  - shows priority, state and activity, time in state, cost vs cap and why-waiting on every
    row except delivered ones;
  - moves no row and not the cursor on a reload where group membership has not changed.
- [ ] **AC4.** Enter and double-click on a row in Queue, History and Findings open the same
  detail popup, and Esc returns to the same row.
- [ ] **AC5.** The popup's Overview shows per-phase wall time and cost, a bar breakdown, and
  the dominant phase highlighted (by wall time and by cost, plus the working phase when it
  applies). Runs shows the stream tail for SPEC-0362 runs and the result text for older ones.
- [ ] **AC6.** From a finding, in the popup or in the Findings tab, one key jumps to the run
  that produced it, and another to the decision that resolved it when there is one.
  Follow-up ideas are shown by id, with their text.
- [ ] **AC7.** The History tab lists every closed campaign, filters by project, spec, state,
  date range and cost, and opens the popup.
- [ ] **AC8.** The Findings tab lists records from every source in the inventory, filters by
  agent, kind, project, severity, status and date, and searches the full text.
- [ ] **AC9.** The budgets in *Cache and performance* hold on the 500-campaign clone. While
  the TUI idles on the Queue tab, it reads no file of a terminal campaign.
- [ ] **AC10.** No new write path:
  - History, Findings and `history.py` leave the data dir byte-identical (apart from the
    queue's own writers).
  - Actions in Queue and in the popup go only through `queue_action` and the SPEC-0250 flow.
- [ ] **AC11.** `uv run pytest` collects and runs every `tests/test_tui_*.py`, with none
  skipped for a missing Textual. The new views are tested by Pilot against the real-data
  fixture.

## Test plan

**Integration: Pilot, the real app, real data.** `tests/test_tui_history_e2e.py` starts the
real `IdeaDesk` with `run_test()`. Its cfg points at a data dir copied from
`tests/fixtures/agentic_history/`, which holds sanitized real campaigns. The fixture includes
at least:
- one merged campaign with several loops and review rounds;
- one abandoned campaign;
- one handed-off campaign;
- one campaign with an answered human escalation;
- one campaign from before SPEC-0362, so with no `stream.jsonl`;
- one campaign with flaky tests and triage waives.

Steps:
1. Open Queue. Assert the group order, and the queued order against `nw queue backlog --json`
   run on the same data dir.
2. Double-click a row (`Pilot.double_click`), then press Enter on another row. Assert that
   each time the popup opened for the right campaign.
3. Assert that Overview's dominant phase equals `dominant` from
   `nw campaign history --json`.
4. Jump from a finding to its run, then to its decision.
5. Open History, filter on `state:abandoned project:Meta-Tools`, and open the popup.
6. Open Findings, search for a phrase known to be in one real finding, and jump to its
   campaign.
7. Hash every file in the data dir before and after, and assert they are byte-identical.

**CLI and UI agree:**
- For every fixture campaign, the numbers the popup renders (phase wall time and cost,
  totals) equal `nw campaign history --json`.
- Index rows equal the per-campaign headers.
- `campaign_export`'s time-in-state equals history's.

**Text snapshots.** Pyte renders Queue, the popup's Overview and History on the fixture at
160×48. The snapshots are regenerated only with `NW_UPDATE_SNAPSHOTS=1`.

**Performance.** `tests/test_history_perf.py`:
- clones the fixture in `tmp_path` to 500 campaigns and about 50 000 records;
- asserts the budgets, with ×3 slack;
- runs the Queue tab idle for 60 s with `os.stat` and `open` patched to count calls, and
  asserts no file of a terminal campaign is opened.

**Regression:**
- The existing `test_tui_*` and `test_agentic_dashboard` suites stay green.
- The guard test fails if any TUI test brings back a Textual skip.

**Manual checklist** for the human, on the real data dir:
1. Run `nw tui` and open tab 3. Is the order obvious? Does anything jump while you watch for
   2 minutes?
2. Double-click a running campaign. Does the timeline match `nw campaign log`? Is the
   dominant phase believable? Does the stream tail move?
3. Open tab 6. Find last week's most expensive merged campaign and open it.
4. Open tab 7. Search for a reviewer phrase you remember and jump to its run.
5. Repeat steps 2 to 4 using only the keyboard.
6. Leave the TUI open for 10 minutes with the queue running. Check that `nw queue tick`
   latency and CPU do not change.

Record the outcome in this epic's *As shipped*.

## Rollout / sequencing

- Children land in the order above, and each one is useful on its own:
  - **After child 2:** `nw campaign history` and `nw campaign records` answer every question
    from the CLI.
  - **After child 4:** the queue is easy to read.
  - **After child 5:** double-click works from the queue.
  - **Children 6 and 7:** add past campaigns and the cross-campaign view.
- Until child 5 lands, Enter and double-click in the queue open the SPEC-0246 screen.
- **No data migration.**
  - The cache builds lazily on first use.
  - Old campaigns render with their links marked inferred.
- **Supersessions.** When children 4 and 5 are promoted, their specs set
  `supersedes: [SPEC-0241]` and `supersedes: [SPEC-0246]` respectively. They restate the
  behaviour they carry forward (actions, timeline filter, review panel).

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; `uv run pytest` green.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

None. These were decided while writing the spec:

| Question | Decision | Why |
|---|---|---|
| Modal or pushed screen? | modal | it feels like a popup, at near-full size |
| Which actions in v1? | the existing ones only | |
| Where does the cache live? | `cache_dir` | keeps SPEC-0400's data-dir invariant true |
| How are snapshots taken? | text snapshots through pyte, with no new `pytest-textual-snapshot` dependency | its SVG goldens change on every Textual minor bump, and pyte is already the repo's real-terminal oracle |
| What order do the sections take? | furthest along first | what is closest to landing is what the human acts on |
| How is provenance filled in? | child 1 adds fields where they are written; old data falls back to inference | |
| What does `fixed` mean? | it is labelled "not re-raised" | the data cannot prove that a finding was fixed |
