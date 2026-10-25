---
id: SPEC-0217
title: "Standalone spec reviews in queue watch, spec-phase queue events and conductor rows"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, watch, review, events]
source_idea: IDEA-078
depends_on: [SPEC-0210, SPEC-0211, SPEC-0214, SPEC-0215]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), the
second half of `IDEA-078`; SPEC-0211 is the first. It covers three things:
- `wt spec review` runs outside campaigns, journaled by SPEC-0210 in
  `<agentic>/spec-reviews.jsonl`, appear on the dashboard;
- the spec phases (SPEC-0215, SPEC-0214) emit queue events;
- the conductor's decision table covers every new park reason.

## Goals / Non-goals

**Goals**
- A **Spec reviews** dashboard section lists standalone reviews that are in flight, or that
  ended or failed recently.
- The queue events `spec_written`, `spec_reviewed` and `spec_accepted`.
- `skills/wt-conduct/SKILL.md` has a row for each spec-phase park reason.

**Non-goals**
- Round numbers for standalone reviews: one standalone run has no loop.
- Notifications (IDEA-059).

## Decision

Read the journal with `spec_review.read_journal(cfg, tail=2000)` (SPEC-0210's function, not
`queue.read_journal`, which reads `runner.jsonl`), pair records by `run_id`, and apply the
status and age rules in memory. Project the new events in `events._project_ledger_event`.

## Design

- **Standalone review rows** (`dashboard_payload` → top-level `spec_reviews`):
  - Pair `start` and `end` records by `run_id`. An `end` whose `start` is not in the tail uses
    its own `ts` as `started_at`.
  - Each row is `{run_id, spec_id, status, counts, started_at, duration_s, cost_usd}`:
    - no `end` yet and `now < started_at + timeout_s` → `running`;
    - an `end` with a verdict → that verdict (`approve` / `revise` / `split`), with the end
      record's counts, cost and duration;
    - an `end` with `error` → `failed` (`counts: null`, cost and duration from the record);
    - no `end` past `started_at + timeout_s` → `failed`.
  - Listed: running rows; ended rows whose end `ts` is within `recent_hours`; failed-orphan
    rows until `started_at + timeout_s + recent_hours`. There is no time filter on the read,
    so a long `timeout_s` never hides a running review.
  - `totals.reviews_running` counts running standalone rows. It is a new total, hidden with
    `--project` along with the rows.
  - **With `--project`** (`dashboard_payload(project=…)`), standalone rows are hidden: journal
    records carry no project. Without it, all rows are shown.
  - **Known limitation:** a killed `wt spec review` leaves a start with no end, which shows
    `running` until `started_at + timeout_s` and then `failed`.
- **Rendering:** a **Spec reviews** table after Needs attention, only when non-empty. Columns:
  spec, status (approve green, revise/split yellow, failed red, running cyan), findings
  (`2 must_fix · 1 major`, zero counts omitted, `—` when counts are null), cost (`$0.15`, or
  `—`), and when (`_ago` of the end, or `▶ <elapsed>` while running). `--json` carries the
  rows unrendered under `spec_reviews`.
- **Events (`wt.event.v1`):**
  - `EVENT_TYPES` gains `spec_written: ("spec_id",)`, `spec_reviewed: ("spec_id", "verdict",
    "round")` and `spec_accepted: ("spec_id",)`.
  - `_project_ledger_event`:
    - ledger `spec_written` → `spec_written`;
    - ledger `spec_review` → `spec_reviewed`;
    - ledger `spec_accepted` (SPEC-0214's event) → `spec_accepted`. Transitions are not
      used for this, so a `spec_accepted`-reason transition from WAITING still projects only
      the existing `admitted` event.
  - The event **data** carries `spec_id` explicitly, from the campaign state's `spec_id`
    (SPEC-0215 sets it when the spec is written); the envelope already carries it too.
    `verdict` and `round` come from the ledger event.
- **Conduct skill:** decision-table rows for the spec-phase park reasons.
  - `spec_writer_unavailable` and `spec_review_unavailable`: legacy, produced only between
    the SPEC-0209 / SPEC-0215 merges; resume once the next spec is in.
  - `spec_write_failed`: read the problems or blockers; resume with a `--note` that answers
    them. The steward helps when one is armed, and the conductor escalates otherwise.
  - `spec_review_exhausted`: read the reviews (`wt spec review --path …` / the store) and the
    detail. Resume with a note, or `--reset-loops`; if it recurs, escalate. Rework is the
    likely outcome (IDEA-089).
  The events `spec_written`, `spec_reviewed` and `spec_accepted` need no action.

## Alternatives considered

- **Filter the journal by a time window on read** — a review with a long timeout would
  vanish while still running.
- **Emit the events from queue code instead of projecting the ledger** — every other queue
  event is a ledger projection.

## Acceptance criteria

- [ ] With journal records written by `wt spec review` (FakeRunner reviewer with `sleep_s`,
      started in a thread; the test polls the dashboard until the row appears, with a
      10-second limit):
  - while it runs, the dashboard lists it as `running`, and `totals.reviews_running` counts
    it;
  - after it ends, the Spec reviews table renders its verdict, findings, cost and when, e.g.
    `revise`, `1 must_fix`, `$0.00`;
  - with `--project Meta-Tools`, the section is hidden.
- [ ] Backdated journal records:
  - a start with no end past its `timeout_s` shows `failed`, and disappears after
    `recent_hours`;
  - an end with `error` shows `failed` with counts null and the record's cost;
  - a review that ended more than `recent_hours` ago is not listed;
  - a running review whose start is 2.5 hours old (with a 3-hour `timeout_s`) is still
    listed.
- [ ] `spec_written`, `spec_reviewed` (with verdict and round) and `spec_accepted` are each
      projected exactly once from an idea campaign's ledger (`events.sync` run twice). Their
      data `spec_id` matches the campaign's spec. A `spec_accepted`-reason transition from
      WAITING projects only `admitted`.
- [ ] `skills/wt-conduct/SKILL.md` has a row for each of the four spec-phase park reasons
      listed in the Design.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_dashboard_spec_reviews.py`. One test per AC:
  - the CLI path uses a FakeRunner entry with `sleep_s`, sampling the dashboard during and
    after the run;
  - the backdated cases write journal lines directly;
  - the events use an idea-campaign ledger fixture;
  - the skill rows are checked by grepping the file.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Ships through the agentic queue after SPEC-0211. The changes are additive.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
