---
id: SPEC-0180
title: "Queue backlog: dependency-gated, prioritized admission and backlog view"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, backlog]
source_idea: IDEA-046
depends_on: [SPEC-0176]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-046`).

The queue (SPEC-0176) has a waiting state, `QUEUED`, and each tick admits the oldest queued
campaign into a free slot. The user asked for the queue to have an explicit **inactive
backlog** whose items claim slots automatically. Four gaps stop the existing waiting state
from being that backlog:

1. **Admission ignores `depends_on`.** A spec whose dependencies have not merged would be
   opened, implemented against a base that lacks them, and fail or produce wrong code. This
   blocks queueing a chain of specs in one go (SPEC-0181 → 0182 → 0183 → 0184).
2. **Order is strictly FIFO** by creation time; no priority, no reordering.
3. **Not visible as a backlog.** `wt queue list` mixes waiting and active items in one table.
4. **No re-validation at admission.** The spec is checked at `queue add` time only; by the
   time a slot frees it may have been edited back to draft, superseded or rejected.

## Goals / Non-goals

**Goals**
- Backlog = campaigns in `QUEUED`. Admission picks, among the backlog, the highest-priority
  item whose dependencies are satisfied, oldest first among equals.
- A dependency is satisfied when the dependency spec's `status` in the **base checkout**
  (the project repo root) is `done` or `superseded`. Unknown dependency ids block (with a
  reason), they never pass silently.
- Priority `1..5` (1 highest, default 3) set at `queue add --priority` and changed with
  `wt queue prioritize CAMPAIGN N`.
- Admission re-reads the spec in the base checkout; status not `accepted` (or
  `in-progress` for a `--force` add) ⇒ the item stays in the backlog with a `blocked_by`
  reason instead of being opened.
- `wt queue list` shows two sections: **Active** (slots) and **Backlog** (position,
  priority, waiting-on). `wt queue backlog` lists only the backlog. `wt.queue.v1` gains a
  per-item `backlog` object `{position, priority, ready, blocked_by[]}` for queued items.
- Every admission writes a transition with reason `admitted`; an item that becomes
  blocked or unblocked writes one `backlog` ledger event (no repeats per tick).

**Non-goals**
- Automatic intake of accepted specs or ideas into the backlog (later; the spec-writer
  phase).
- Cross-project ordering beyond priority + age.
- Event emission to the conductor (SPEC-0181 consumes the `backlog` / `transition` events).

## Decision

Keep `QUEUED` as the backlog state; add `priority` to campaign state; make admission a pure
function `pick_admissions(backlog, busy, slots, readiness)` in `queue.py`, where
`readiness(state) -> (ready, blocked_by)` resolves dependencies and spec status from the
project's base checkout. Add `wt queue prioritize` and `wt queue backlog`, and split the
list view.

## Design

- **State:** `priority: int` (default 3) and `backlog: {ready, blocked_by[], since}` on
  campaign state; `plan_add` / `apply_add` accept `priority` (1..5, else `ValueError`).
- **Readiness** (`queue.backlog_readiness(cfg, state) -> (bool, list[str])`):
  - resolve the spec path in `state["repo"]["root"]` (`spec.rel_path`), read frontmatter;
    missing file ⇒ `["spec file missing"]`;
  - status check as above ⇒ e.g. `["spec status is draft"]`;
  - for each `depends_on` id: resolve via the same specs dir; missing ⇒
    `["SPEC-0181 not found"]`; status not in {done, superseded} ⇒
    `["waiting on SPEC-0181 (accepted)"]`.
- **Admission** (`pick_admissions`, pure): candidates = backlog items with `ready` and no
  `park_requested`; sort by `(priority, created_at, id)`; take `slots - busy`.
- **Tick integration:** before admission, compute readiness for every backlog item; when
  `ready`/`blocked_by` differs from the stored `backlog` object, write state and append one
  `backlog{ready, blocked_by}` event. Admitted items transition `QUEUED → OPENING`
  (`admitted`) as today.
- **CLI** (`cli_queue.py`):
  - `wt queue add … --priority N` (default 3; shown in the plan);
  - `wt queue prioritize CAMPAIGN N [--yes]` (dry-run default; only `QUEUED`/`PARKED`;
    `wt.queue.prioritize.v1`; ledger `priority{from, to}` event);
  - `wt queue backlog [--json]` → `wt.queue.backlog.v1` `{items: [{campaign, spec_id,
    priority, position, ready, blocked_by, age_s}]}` ordered by admission order;
  - `wt queue list` human view renders "Active (busy/slots)" and "Backlog (n)" tables;
    `--json` keeps `wt.queue.v1` and adds the `backlog` object per queued item.
- **Docs:** "## Queue" section of `capabilities/agentic.md` gains "Backlog and admission";
  agent-surface rows for the two new schema ids.

## Alternatives considered

- **A separate backlog store** — rejected; the campaign directory already exists from
  `queue add`, and one source of truth (`state.json`) stays the rule from SPEC-0171.
- **Dependencies satisfied when the dependency campaign is MERGED** — rejected; specs may
  be completed by humans outside the queue. The spec status in the base checkout is the
  authoritative signal and works for both.
- **Admit and let the implementer discover missing dependencies** — rejected; it burns a
  run and produces code against the wrong base.

## Acceptance criteria

- [ ] `pick_admissions` orders by priority then age, skips not-ready and park-requested
      items, and never exceeds free slots (parametrized pure tests).
- [ ] A queued spec with `depends_on: [X]` is not admitted while X is `accepted` and is
      admitted on the first tick after X's status in the base checkout becomes `done`
      (e2e with the fake runner: queue X and Y together; X merges first, then Y opens).
- [ ] Unknown dependency ids and a spec edited back to `draft` keep the item in the backlog
      with the documented `blocked_by` reason; one `backlog` event per change, none per
      unchanged tick.
- [ ] `wt queue add --priority`, `wt queue prioritize` (dry-run / `--yes`, ledger event),
      and `wt queue backlog --json` behave as documented; invalid priorities error.
- [ ] `wt queue list` shows separate Active and Backlog sections; `wt.queue.v1` queued
      items carry `backlog{position, priority, ready, blocked_by}`.
- [ ] Completion, reference, agent-surface guards pass.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** pure admission tests in `tests/test_agentic_queue.py`; readiness
  tests with tmp spec files; CLI tests in `tests/test_agentic_queue_cli.py`; one e2e in
  `tests/test_agentic_e2e.py` (two dependent specs, fake runner whose implementer marks
  each spec done, the second admitted only after the first merges).
- **Manual verification:** none beyond the suite; the next real campaigns (SPEC-0181..0184
  queued together) exercise it.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Built by the agentic loop; idea bookkeeping via `wt spec reconcile --apply` after merge.

## Rollout / migration

Existing `QUEUED` campaigns without `priority` default to 3. No data migration.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
