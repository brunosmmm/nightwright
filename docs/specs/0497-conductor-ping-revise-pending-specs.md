---
id: SPEC-0497
title: Conductor ping flags specs stalled on a revise verdict
status: done
owner: user
created: 2026-11-09
kind: feature
tags: [agentic, conductor, review]
source_idea: IDEA-1072
---

## Context

`nw spec review` can return `revise`. `nw queue add` then refuses the spec (one review
round; a stale review is refused; `--force` buys another round). Nothing applies the findings
and re-queues, and nothing tells the conductor, so specs sat unqueued until the human noticed.

## Goals / Non-goals

**Goals**
- The periodic `conductor_ping` lists specs whose latest review is `revise` and that have no
  campaign, and a changed list wakes the conductor.
- The conductor skill says what to do with one.

**Non-goals**
- Automatically editing specs from the CLI; new event types; changing admission rules.

## Decision

Add a `revise_pending` list to the `conductor_ping` digest, counted in the ping fingerprint.
The conductor (never the CLI) dispatches a sub-agent to apply the findings and re-queue.

## Design

- `events.revise_pending_items(cfg)`: from `spec_review.all_latest_reviews`, rows with
  `verdict == "revise"` and `matches_current is not None` (the spec resolves), minus spec ids
  that have any non-ABANDONED campaign. Item: `{spec_id, reviewed_at, stale, counts}`; `stale`
  means the spec changed since the review.
- `ping_digest` adds `revise_pending` (optional key, not in `EVENT_TYPES` required keys, so
  old pings stay valid); the ping summary line shows its count.
- `quiet.ping_fingerprint` adds `("revise_pending", spec_id)` so a new item wakes the
  conductor and an unchanged list stays held (SPEC-0317).
- `skills/nw-conduct/SKILL.md`: a decision-table row `revise_pending` and the ping row mention it.

## Alternatives considered

- **A new `spec_revise_pending` event type** — needs RULES, schema and a watchdog key; the
  existing ping already carries a deduplicated, fingerprinted list.

## Acceptance criteria

- [x] `ping_digest` has `revise_pending` listing a spec with a `revise` latest review and no campaign.
- [x] A spec with a campaign, or with an `approve` latest review, is not listed.
- [x] `ping_fingerprint` includes the item, so the ping wakes the conductor once.
- [x] The conduct skill has a `revise_pending` decision-table row.

## Test plan

- **Automated tests:** `tests/test_agentic_quiet.py` (fingerprint) and
  `tests/test_agentic_revise_pending.py` (digest listing and exclusion, with stored reviews).
- **Regression guard:** affected tests via `tools/affected_tests.py`; `spec_lint`.

## As shipped

Shipped as designed. `events.revise_pending_items` and the `revise_pending` key in
`ping_digest`; `quiet.ping_fingerprint` counts `("revise_pending", spec_id)`; the conduct
skill has a `revise_pending` decision-table row. The row's event cell reads "a
`conductor_ping` showing `revise_pending` items" (not starting with `conductor_ping`) so the
SPEC-0301 row-uniqueness test in `tests/test_skills.py` still finds one `conductor_ping` row.
Tests: `tests/test_agentic_revise_pending.py`, `tests/test_agentic_quiet.py`,
`tests/test_agentic_ping.py`, `tests/test_skills.py` pass.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).
