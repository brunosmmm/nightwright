---
id: SPEC-0482
title: An adopted spec review with no start snapshot retries instead of crashing the runner
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
tags: [agentic, queue, bugfix]
depends_on: [SPEC-0214, SPEC-0225]
---

## Context

A fresh spec review runs through `_do_spec_review`, which records `res.spec_review =
{"sha": <spec sha at start>, "modified": None}`. After a runner restart an in-flight
`spec_reviewer` run is adopted and goes through the generic `_do_persona` path, which never
sets `res.spec_review`. `_apply_spec_review` then sees `sr = {}` and `sr["sha"]` raises
`KeyError`. The normal tick's `_harvest` is unguarded, so the runner dies, and dies again on
every restart because the adopted run is re-harvested. Seen live on campaign
IDEA-1020-20261108-0a27 (run 002-spec_reviewer).

## Goals / Non-goals

**Goals**
- An adopted spec review never raises in `_apply_spec_review`.

**Non-goals**
- Trusting an adopted verdict. Making adopted reviews resumable.

## Decision

An adopted review has no start snapshot of the spec (`sha` absent). It is a failed review
attempt: failure `"adopted review has no start snapshot of the spec"`, through the existing
retry path (`spec_review_retry`, bounded by `SPEC_REVIEW_RETRY_MAX`, then
`spec_review_exhausted`). The sha is never computed after the fact: the spec may have changed
while the run was in flight, so a verdict could be recorded against the wrong revision.

## Design

In `_apply_spec_review`, the failure chain gains a branch right after `sr["modified"]`:
`elif "sha" not in sr: failure = ...`. It precedes the only two later `sr["sha"]` uses
(`SR.review_payload` and the ledger/state writes), which run only when `failure` is unset, so
none can raise. The early `sr.get(...)` branches (dirty, accepted, ...) are unaffected.

## Alternatives considered

- **Have adoption populate `res.spec_review`** — the only available sha would be computed
  after the run started, which is exactly what must not be trusted; recording the start sha
  would need persisting it with the run record, a larger change. Rejected for this bugfix.
- **Guard `_harvest` in the normal tick** — would hide other bugs and leave the campaign
  stuck re-harvesting; out of scope.

## Acceptance criteria

- [x] An adopted-shaped spec review result (no `sha`) does not raise.
- [x] It retries (`SPEC_REVIEW`, `spec_review_retries` = 1), then parks `spec_review_exhausted`
      with detail naming the missing snapshot, and records no `spec_review` ledger event.

## Test plan

- **Automated tests:** `tests/test_agentic_spec_review_loop.py::test_an_adopted_review_without_a_start_snapshot_retries`
  drives a review whose result lacks `spec_review` through the real tick.
- **Regression guard:** the existing spec-review loop and adopt tests.

## Rollout / migration

None.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

## As shipped

Shipped as designed: one `elif "sha" not in sr` branch in `_apply_spec_review` (queue.py) plus the regression test, which fails with `KeyError: 'sha'` without the fix.

## Open questions

- None.
