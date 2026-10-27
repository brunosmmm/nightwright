---
id: SPEC-0286
title: "Escalation overrides are idempotent, withdrawable and exact"
status: done
updated: 2026-10-27
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-363
parent: SPEC-0169
tags: [agentic, review, escalations, review-follow-up]
---

## Context

This spec comes from the second triage of the accumulated `wtfug:` review follow-ups
(campaigns of SPEC-0253 and later). Each finding kept here was re-checked against `main`
on 2026-10-27 and is still true. Both let a human override (`wt escalations answer
--waive/--attest`) clear more of a review than the human decided. That can pass an unmet
acceptance criterion to the merge.

Kept findings:

- **IDEA-363** (SPEC-0264)
  - `wtfu:aed5bdfe1db9`: *Retry after a record-write failure duplicates overrides;
    withdrawal leaves them applied.* `escalations.apply_answer` appends to
    `state["human_overrides"]["waive"/"attest"]` and writes the state before the record
    closes. SPEC-0264 Design §2 called this idempotent, but it is not. If `_write(cfg, rec)`
    fails, the record stays `open` and the overrides stay stored. A retry appends a second
    copy of each one. If the human then gives up and runs `wt escalations withdraw`
    (`escalations.withdraw`), the overrides of a withdrawn escalation still steer
    `triage_reviews`.
  - `wtfu:37aac57bb4ac`: *Waiver `resolved_ac` keeps every substring hit, not just the exact
    AC.* `_plan_override` stores `resolved_ac = _unmet_reach(f["ac"], unmet, …)`.
    `_unmet_reach` refuses only when there are several hits and none is exact. When one
    hit is exact, it still returns all of them. So waiving finding X whose `ac` is
    "pytest passes" also clears an unrelated unmet AC "pytest passes with the new
    regression tests". The `--attest` path already narrows to the exact hit.

The other IDEA-363 findings are test-only (`ValueError` branch of the wake append, a
step-4 failure test). That test is added here as part of the fix.

## Goals / Non-goals

**Goals**
- Storing an answer's overrides is idempotent per escalation id.
- A withdrawn escalation leaves no override in effect.
- A waiver clears exactly the AC its finding names when that AC is among the unmet ones.

**Non-goals**
- Removing overrides when a campaign leaves the park by resume, abandon or a terminal
  transition (`withdraw_for_campaign`, where the caller owns the state). A resume after a
  half-written answer keeps the human's overrides. That is accepted: the human chose to
  resume.
- Changing `_covers` or the attest matching rules from SPEC-0264.

## Decision

Tag-and-replace: `apply_answer` drops any stored override that carries the same
`escalation` id before it appends. A human withdraw (`withdraw(..., clear_request=True)`)
strips that escalation's overrides under the campaign lock it already holds. `_plan_override`
keeps only the exact hit when one exists, the same rule the attest path uses.

## Design

1. **Idempotent store** (`src/wt/agentic/escalations.py` `apply_answer`): before the append
   loops, filter `h["waive"]` and `h["attest"]` to entries whose `escalation != esc_id`.
   Then append as today. A retry after any failure before the record write yields exactly
   one copy of each override.
2. **Withdraw strips** (`src/wt/agentic/escalations.py` `withdraw`): in the
   `known and clear_request` branch, which already holds `item_lock` and re-reads `st`, also
   remove `human_overrides` entries tagged `escalation == esc_id`. When any were removed,
   write the state once, together with the existing `park_requested` change, and add `"; N
   override(s) removed"` to the withdrawn ledger note.
3. **Exact waiver reach** (`src/wt/agentic/escalations.py` `_unmet_reach`): return `exact`
   when it is non-empty, else `hits` (the >1-and-no-exact refusal is unchanged). The
   `--attest` caller's own exact pick stays as it is. Both paths now get the same answer
   from one helper.

## Alternatives considered

- **Make `human_overrides_of` ignore overrides whose record is not `answered`.** Rejected.
  It would turn a pure function into a file read on every triage. It would also drop
  overrides kept on purpose after a resume (see Non-goals).
- **Write the record before the state.** Rejected. SPEC-0264 fixed that order on purpose:
  a closed record with lost overrides cannot be retried.

## Acceptance criteria

- [x] `apply_answer` whose record write fails once, then is retried, leaves exactly one
      waive entry and one attest entry per override of that escalation in `state.json`.
- [x] `wt escalations withdraw ESC-x` on an open record whose overrides were already stored
      removes every `human_overrides` entry tagged `ESC-x`, and leaves entries of other
      escalations in place. The ledger note says how many were removed.
- [x] Waiving a finding whose `ac` is exactly one unmet AC, and a substring of another unmet
      AC, stores `resolved_ac` with only the exact AC. The longer AC stays unmet on the next
      `triage_reviews`.
- [x] A waiver whose `ac` is a strict partial quote of exactly one unmet AC still resolves to
      that AC (SPEC-0264 behaviour kept).

## Test plan

- **Automated tests** (`tests/test_agentic_escalations.py`):
  - `test_answer_retry_after_record_write_failure_does_not_duplicate_overrides`: an
    override with one waive and one attest. Monkeypatch `escalations._write` to raise
    `OSError` once. The first `apply_answer` raises, and the record is `open`. The second
    succeeds. Assert `len(waive) == len(attest) == 1` for that escalation id.
  - `test_withdraw_strips_that_escalations_overrides`: store overrides for ESC-1 (by a
    failing record write as above) and an answered ESC-0 override. Then `apply_withdraw`
    ESC-1. Only ESC-0's entries remain, and the last ledger note contains `1 override`
    (count as built).
  - `test_waiver_resolved_ac_keeps_only_the_exact_ac`: unmet ACs `["pytest passes",
    "pytest passes with the new regression tests"]`, finding `ac = "pytest passes"`.
    `plan["override"]["waive"][0]["resolved_ac"] == ["pytest passes"]`, and
    `Q.triage_reviews` with the stored overrides keeps the longer AC unmet.
  - `test_waiver_partial_ac_still_resolves_single_hit`: the existing SPEC-0264 case stays
    green (re-run, no edit).
- **Sabotage checks.** Each one must fail its test; record the result in the spec:
  - remove the pre-append filter in `apply_answer`
  - remove the strip in `withdraw`
  - return `hits` unconditionally from `_unmet_reach`
- **Regression guard:** run the full `uv run pytest`. `test_agentic_escalations*.py`,
  `test_agentic_review_triage.py` and `test_tui_escalations_*.py` must stay green.
- **Manual verification:** none. Every path is exercised in-process.

### Test record (2026-10-27)

- `uv run pytest -q tests/test_agentic_escalations.py`: 84 passed. New tests:
  `test_answer_retry_after_record_write_failure_does_not_duplicate_overrides`,
  `test_withdraw_strips_that_escalations_overrides` (the half-written ESC-0001 stored one waive
  and one attest, so its note reads `; 2 override(s) removed`),
  `test_withdraw_without_overrides_keeps_the_plain_note`,
  `test_waiver_resolved_ac_keeps_only_the_exact_ac` (the longer AC is not attested and the
  triage outcome stays `fix`).
- The SPEC-0264 partial-quote case has no test named `test_waiver_partial_ac_still_resolves_single_hit`;
  the existing cases that cover it, `test_plan_override_stores_resolved_ac_text` and
  `test_waived_partial_ac_finding_clears_full_ac_on_next_triage`, re-ran green unedited.
- Sabotage checks, each run against `tests/test_agentic_escalations.py` and then reverted:
  - pre-append filter removed in `apply_answer`: the retry test fails (2 copies).
  - strip removed in `withdraw`: the withdraw test fails (ESC-0001's entries remain).
  - `_unmet_reach` returns `hits` unconditionally: the exact-AC waiver test and
    `test_attest_text_covering_two_acs_is_refused_listing_both` fail.
- Gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py` exit 0;
  `uv run pytest -q -x` first full run stopped on the stale ledger
  (`test_export.py::test_internal_spec_lint_unaffected_by_outbox`). After the regeneration
  the full re-run gave 3511 passed, 3 skipped.

### Test record (2026-10-27, review round 2)

- Review fix: a human-waived finding no longer keeps a longer unmet AC from getting its own
  synthetic `#ac<j>` major. `test_waiver_resolved_ac_keeps_only_the_exact_ac` now also asserts
  that `blocking == [RV#ac1]` (the longer AC), that it is in `synthetic` and
  `answerable_findings`, and that `--attest RV#ac1` resolves to it.
  `test_withdraw_state_write_failure_leaves_the_record_open_to_retry` is new.
- Sabotage checks, each reverted: without the `human_gone` exclusion in the covering check, and
  without the overridden findings in the `#ac<j>` attest lookup, the exact-AC test fails.
  Closing the record before the state write fails the new withdraw test.
- `uv run pytest -q tests/test_agentic_escalations.py tests/test_agentic_review_triage.py
  tests/test_tui_escalations_*.py`: 204 passed. Gates: `python3 tools/spec_lint.py
  --write-ledger && python3 tools/spec_lint.py` exit 0; `uv run pytest -q -x` 3512 passed,
  3 skipped.

### As shipped (2026-10-27)

- As designed. The strip in `withdraw` runs on every human withdraw (`clear_request=True`),
  including when the campaign is `PARKED` (the usual case after a half-written answer). The
  `park_requested` change still applies only before the park. One `write_state` covers both.
  The count in the ledger note is the total of removed waive plus attest entries.
- An existing assertion changed as a consequence of Design §3:
  `test_attest_text_covering_two_acs_is_refused_listing_both` now expects the `reach` of an
  exact `--attest` to be only that AC. `reach` is display only (the `covers:` row of the CLI
  preview), and triage matches attests exactly, so the old two-AC reach overstated it.
- Review round 2 (two changes outside the three Design steps, both needed for the AC "the
  longer AC stays unmet" to be visible and not just counted):
  - `queue.triage_reviews`: a real major that the human waived no longer counts as covering an
    unmet AC. Otherwise the longer AC (no longer in the narrowed `resolved_ac`) blocked the
    merge as `fix` with an empty `blocking` list, and no `#ac<j>` id named it. It now gets its
    own synthetic `<persona>#ac<j>` major.
  - `escalations._plan_override`: the `--attest <persona>#ac<j>` lookup also includes the
    findings the stored overrides produce, so that synthetic id can be attested.
  - `withdraw` (hardening): the state (override strip and `park_requested`) is written before
    the record closes, after an `open` check under the same locks. A failed state write leaves
    the record `open`, so the withdraw can be retried.

## Rollout / migration

None. Duplicate entries already stored stay as they are. They are harmless for triage,
because matching is set-like.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log
