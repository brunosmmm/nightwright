---
id: SPEC-0461
title: A campaign cap counts only a cost the run reported
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [agentic, cost]
depends_on: [SPEC-0224, SPEC-0442]
---

## Context

[SPEC-0224](./0224-honest-run-cost-on-interrupt-and-a-runner-draining.md) charges a run's budget against the campaign cap when the run reports no dollar cost, and marks that charge as an estimate. [SPEC-0442](./0442-cursor-sdk-persona-runs.md) leaves `total_cost_usd` null when the Cursor SDK answers `feature_unavailable`. Every Cursor persona run hits that path, so each one is booked at its budget ceiling.

A balanced campaign cap is $15. One spec pass plus one implement and one review round of those ceilings is $16.50. The campaign parks `cost_cap`, the cost-cap policy sees a review that still says fix, and the park becomes a human escalation. IDEA-1024's recorded $16.50 is exactly the sum of the persona budgets, and `estimated_usd` is the same number. The cap is being applied to a figure the system invented.

## Goals / Non-goals

**Goals**

- A run with no reported cost does not increase the spend the cap checks.
- The budget is still recorded as `estimated_usd`, and the run is still marked estimated.
- A run that reports a cost is charged that cost, including on a timeout.
- A campaign whose recorded total is entirely an estimate is not blocked by that total.

**Non-goals**

- No token price table, and no guess at what a Cursor run actually cost.
- No change to the check that holds the next run's budget against known spend.
- No automatic resume of a campaign that is already parked.

## Decision

The cap sees known spend only. An unknown cost adds nothing to `total_usd`. It still adds the budget to `estimated_usd`, and the finding says the amount was not counted. Older states folded the estimate into `total_usd`; the cap subtracts `estimated_usd` from that total until a later run separates them.

## Design

`_record_run` charges `result.cost_usd` when it is present and `0` otherwise. `charged_usd` and the cost event's `delta_usd` are that same number. The finding title is `run cost unknown; not counted against the cap`.

`_known_spend` is what `_cap_blocks` compares with the cap. When `cost.estimate_separated` is absent, known spend is `total_usd - estimated_usd`. The first `_record_run` after this change sets the flag and subtracts the folded estimate from `total_usd` once.

## Alternatives considered

- **Keep charging the budget.** That is the current behavior, and it parks every Cursor campaign after one pass.
- **Drop the cap until the SDK returns a price.** The cap still applies to a cost a run actually reported, and to the next run's budget held against that known spend.

## Acceptance criteria

- [x] A run with `cost_usd` null leaves `total_usd` unchanged and adds its budget to `estimated_usd`.
- [x] A run that reports `1.23` adds `1.23` to `total_usd`.
- [x] A campaign with `total_usd` `16.50`, `estimated_usd` `16.50`, and cap `15` is not parked for cost before an implementer run.
- [x] A campaign with known spend `6.01` and cap `10` is still parked before an implementer run.

## Test plan

- **Automated tests:** `tests/test_agentic_cost_estimate.py` covers the null and reported charges. `tests/test_agentic_queue.py` covers the cap check, including a folded estimate that must not park. `tests/test_agentic_reviewers.py` covers a reviewer timeout. Run those files with `uv run pytest`. Executed 2026-11-07: 288 passed. The three `queue watch` width cases fail with `COLUMNS` unset; that failure is already on main.
- **Manual verification:** none. Already parked campaigns stay parked; the next cap check on a live one uses known spend.
- **Regression guard:** a reported cost, including one read from a timed-out stdout, is still charged exactly.

## Rollout / migration

Land on main. The queue re-execs. The next cap check subtracts `estimated_usd` from a total that still contains it. No campaign state is rewritten until that campaign records another run.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
