---
id: SPEC-0454
title: A live idea after-wait does not reread the idea corpus
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [queue, watch]
depends_on: [SPEC-0423]
---

## Context

`nw queue watch` rebuilds `queue_payload` on every refresh. For each queued campaign whose
`after` points at an idea, `after_met` called `_idea_task`, and `_ideas` drops the org cache
and parses every idea file. Two waiting campaigns made one refresh parse the corpus twice,
about six seconds, so a watch next to a running queue sat on a core.

The card-title change in SPEC-0453 does not do this work. The cost showed up when watch was
restarted onto a queue that already had those waits.

## Goals / Non-goals

**Goals**
- A prerequisite idea that already has a non-abandoned campaign is resolved from that
  campaign and its spec file.
- The watch refresh no longer parses the idea corpus for that case.
- The messages for a queued prerequisite, a merged one, an abandoned one, and a missing
  idea stay the ones SPEC-0423 specified.

**Non-goals**
- Speeding up a cold load of the idea files for a prerequisite that has no campaign.
- Changing admission: a live prerequisite is still not ready.

## Decision

Look up the prerequisite's idea campaigns first. A merged campaign is met. A live campaign
with a `spec_id` is the spec check. A live campaign without one is the campaign-state wait.
The idea file is read only when no campaign can answer (nothing queued, or the newest
campaign was abandoned).

## Design

`after_met` in `src/nightwright/agentic/queue.py`, the `kind == "idea"` branch.

## Alternatives considered

- **Cache `_ideas` for one payload.** One parse per refresh instead of two. A refresh was
  still seconds, because the corpus is large.
- **Serve the stored `backlog` object and never recompute.** The runner would be the only
  writer, and a watch opened between ticks would show a stale wait.

## Acceptance criteria

- [x] Readiness of a campaign waiting on an idea that has a live campaign does not call
  `_idea_task`.
- [x] That wait is still unmet, and a merged prerequisite is still met.
- [x] An abandoned prerequisite is still reported as abandoned.

## Test plan

- **Automated tests:** `tests/test_idea_ordering.py` covers the new guard and the existing
  after-wait messages.
- **Manual verification:** one `dashboard_payload` on the live queue is well under a second
  of idea-file parsing. `nw queue watch` is no longer a core.
- **Regression guard:** the SPEC-0423 ordering tests.

## Rollout / migration

None. The next watch process picks it up. A watch that is already running keeps the old
code until it is restarted.

## As shipped

`after_met` reads the prerequisite's campaigns before the idea file. A merged campaign is
met. A live campaign is the spec check when it has a `spec_id`, otherwise the campaign-state
wait. `_idea_task` runs only when nothing is queued or the newest campaign is abandoned.

`tests/test_idea_ordering.py`: 21 passed. One live `dashboard_payload` went from about
seven seconds to under a second (0.68s on the second call). The remaining time is spec
frontmatter for the history rows, which watch already paid before this.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed.
- [x] No regressions in the ordering tests.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

None.
