---
id: SPEC-0531
title: Spec-review precheck scopes already-accepted to the current spec
status: done
owner: user
created: 2026-11-10
updated: 2026-11-10
kind: feature
tags: [agentic, queue]
depends_on: [SPEC-0214]
source_idea: IDEA-1140
---

## Context

`_spec_review_precheck` in `src/nightwright/agentic/queue.py` runs on the main thread
before a `spec_reviewer` run ([SPEC-0214](./0214-spec-review-loop-inside-the-campaign-with-in-loop.md)).
After the worktree is clean of tracked changes, it returns `already_accepted` when the
campaign's `kind` is not `idea`, or when `store.read_ledger` finds any `spec_accepted`
event on that campaign. `_apply_spec_review` then parks `spec_review_exhausted` with
`resume_to` IMPLEMENTING and detail `the spec is already accepted; there is nothing to
review`. No reviewer runs, no second `spec_accepted` is appended, and `spec_at_open.md`
/ `sha256_at_open` stay as they are. That guard keeps a resume `--to SPEC_REVIEW` from
resetting the close-guard baseline after this spec was accepted, including when the file's
`status` was later edited to `done` or `in-progress`.

The ledger read is not filtered by spec id. A `spec_accepted` event carries `spec_id` at
the top level (`schemas.EVENT_KEYS`; `store.append_event` merges the payload onto the
record). The campaign's current spec can be a different id: the writer records
`state["spec"]["id"]` and `state["spec_id"]` for the spec it just wrote, and
`adopt_renamed_spec` updates both when close follows a rename
([SPEC-0470](./0470-close-follows-a-renamed-spec.md)). IDEA-1140 hit the unfiltered
check: a `spec_accepted` for SPEC-0449 made the review of SPEC-0473 park
`spec_review_exhausted` as already accepted, so SPEC-0473 was never reviewed.

## Goals / Non-goals

**Goals**

- The ledger half of the already-accepted short-circuit counts a `spec_accepted` event
  only when its `spec_id` is the spec this campaign is reviewing.
- An idea campaign whose current spec has no matching `spec_accepted`, and whose
  committed status is not `accepted`, still runs the spec reviewer.
- A `spec_accepted` for the current spec id still parks the resume `--to SPEC_REVIEW`
  path and leaves the close-guard baseline alone, including when the ledger also holds a
  `spec_accepted` for some other spec id.

**Non-goals**

- The `kind != "idea"` short-circuit. A plain spec campaign resumed to SPEC_REVIEW still
  returns `already_accepted` with no ledger filter.
- `_last_review_event`, `_prior_revision`, the one-review-per-campaign revise/split path,
  and the HEAD `status: accepted` branch (finish a committed accept, or park when this
  campaign has no `spec_review` event). A current spec whose committed status is
  `accepted` still does not start a new review.
- Rewriting or deleting historical `spec_accepted` events. Auto-resuming campaigns
  already parked for this bug.

## Decision

In `_spec_review_precheck`, the ledger half of the already-accepted short-circuit matches
only `spec_accepted` events whose `spec_id` equals the campaign's current spec id. A
`spec_accepted` for any other id is ignored by that check. The `kind != "idea"`
disjunct is unchanged and still returns `already_accepted` on its own. When the current
spec has no matching `spec_accepted`, the precheck continues through the existing
HEAD-status, prior-revision, and approve-resume branches, and runs the reviewer when
those branches do not apply.

## Design

The current spec id is the non-empty string `state["spec"]["id"]` when that field is
set, otherwise the non-empty string `state["spec_id"]`. Both are set to the same id when
the writer commits a spec and when `adopt_renamed_spec` follows a rename. An empty or
missing current id matches nothing: the ledger half does not return `already_accepted`
just because the ledger contains some `spec_accepted`.

Replace the truthiness test on `store.read_ledger(ctx.cfg, state["id"], types=["spec_accepted"])`
with an any-match on those records: the event's top-level `spec_id` equals the current
spec id. Equality is exact string match. An event with no `spec_id` does not match.
Order does not matter: an older event for the current id still matches when a later
event names a different id, and a later event for the current id still matches when an
older event names a different id.

`_apply_spec_review` is unchanged. `already_accepted` still parks
`spec_review_exhausted`, `resume_to` IMPLEMENTING, detail `the spec is already accepted;
there is nothing to review`, with no reviewer run, no extra `spec_accepted`, and no
rewrite of `spec_at_open.md` or `sha256_at_open`.

The branches below the short-circuit stay as they are. In particular, HEAD `status:
accepted` still finishes a committed accept when this campaign has a `spec_review` event,
and still returns `already_accepted` when it does not. A prior revise or split still
accepts a changed spec or parks an unchanged one. An approve whose sha still matches
still resumes the accept.

## Alternatives considered

- **Drop the ledger check and trust HEAD `status`.** A resume after acceptance often
  finds `status: done` or `status: in-progress`. Those campaigns would fall through and
  either re-review or re-baseline `spec_at_open.md`. That is the failure
  [SPEC-0214](./0214-spec-review-loop-inside-the-campaign-with-in-loop.md) closed.
- **Use only the latest `spec_accepted`.** A later event for a different spec id would
  hide an earlier accept of the current spec and reset the close-guard baseline.
- **Delete the stale event.** The ledger is the campaign history. The check is what
  misreads it.

## Acceptance criteria

- [x] An idea campaign in SPEC_REVIEW, current spec id S, committed spec status
      `proposed`, whose ledger `spec_accepted` events are only for a spec id other than
      S, runs the spec reviewer on the next tick: a `spec_review` event is recorded and
      the campaign is not parked `spec_review_exhausted` for "already accepted". On an
      approve verdict it reaches IMPLEMENTING and appends `spec_accepted` with
      `spec_id` S. The other spec's `spec_accepted` event is still in the ledger.
- [x] An idea campaign that already has `spec_accepted` for its current spec id, resumed
      `--to SPEC_REVIEW` after a committed body edit, parks `spec_review_exhausted` with
      `resume_to` IMPLEMENTING and detail containing `already accepted`. The reviewer
      does not run, `spec_accepted` count is unchanged, and `spec_at_open.md` and
      `sha256_at_open` are unchanged. This remains true when a `spec_accepted` for a
      different spec id is the latest such event, and when the file's status line was
      changed to `done` before the resume.
- [x] A campaign whose `kind` is not `idea`, resumed `--to SPEC_REVIEW`, parks the same
      way and runs no reviewer, with the baseline and the `spec_accepted` count
      unchanged, including when the spec status is `in-progress` and the ledger has no
      `spec_accepted` event.
- [x] An idea campaign with no `spec_accepted` event still reviews a proposed spec and,
      on approve, accepts it once (one `spec_accepted` for that spec id). A revise
      verdict still sends the campaign to the writer and accepts the revision without a
      second review.

## Test plan

- **Automated tests:** in `tests/test_agentic_spec_review_loop.py`, using the existing
  `begin` / `reviewed` / `ledger` / `resume` helpers and `store.append_event`.
  - `test_spec_accepted_for_another_spec_id_does_not_skip_review`: `begin` leaves an
    idea campaign in SPEC_REVIEW on SPEC-0002 (`status: proposed`). Append
    `spec_accepted` with `spec_id` `SPEC-0001` and `round` 1. Tick the scripted approve.
    Assert state IMPLEMENTING, one `spec_review` event, and `spec_accepted` spec ids
    `SPEC-0001` and `SPEC-0002`.
  - `test_current_spec_accepted_still_skips_review_when_a_later_event_names_another_spec`:
    approve through to IMPLEMENTING, append `spec_accepted` for `SPEC-0001` so it is the
    latest such event, then `_resume_accepted_to_review`. Assert the existing park,
    unchanged baseline, and unchanged `spec_accepted` count.
- **Manual verification:** none. The precheck is exercised by those ticks.
- **Regression guard:** the same file's
  `test_resume_to_spec_review_after_acceptance_keeps_the_close_guard_baseline`,
  `test_resume_to_spec_review_after_done_with_a_revise_history_accepts_nothing`,
  `test_resume_to_spec_review_after_done_with_an_approve_history_accepts_nothing`,
  `test_resume_to_spec_review_on_a_plain_spec_campaign_runs_nothing`,
  `test_resume_to_spec_review_on_a_plain_spec_campaign_that_is_not_accepted`,
  `test_approve_accepts_in_the_loop_and_runs_to_merged`, and
  `test_revise_then_accept_after_revision_without_a_second_review`.
  Run the new tests with that file via
  `python3 tools/affected_tests.py --run uv run pytest -q tests/test_agentic_spec_review_loop.py`.

### Test record (2026-11-10)

- `uv run pytest -q tests/test_agentic_spec_review_loop.py` — 48 passed.
- Full suite left to gating.

## Rollout / migration

Ship the precheck change with the tests. No ledger rewrite and no snapshot migration.
Campaigns already parked `spec_review_exhausted` with detail `the spec is already
accepted; there is nothing to review` stay parked until an operator resumes them to
SPEC_REVIEW. After this change, that resume reviews the current spec when no
`spec_accepted` names its id and the committed status is not `accepted`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the automated tests above pass. No manual steps.
- [x] No regressions in the spec-review resume and accept tests named above.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

- None.
