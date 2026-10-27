---
id: SPEC-0264
title: "Review pipeline integrity: exact attestations and safe escalation answers"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-337
parent: SPEC-0169
tags: [agentic, review, escalations, review-follow-up]
---

## Context

This spec comes from a triage of the accumulated `wtfug:` review follow-ups: minor findings
that reviewers raised on merged specs SPEC-0202..0259. We re-checked each finding kept here
against `main` on 2026-10-27, and it is still true. Each one lets the review→close pipeline
do one of three things: pass work it should not, lose a human decision, or corrupt the ideas
file.

Kept findings:

- **IDEA-337** (SPEC-0235)
  - `wtfu:aed579a7ccfa`: *Persisted attestations and waived findings' `ac` clear future ACs
    through `_covers` substring matching.* An attested text of 8 or more characters marks
    met every later unmet AC that contains it, or that it contains. A must-pass AC can be
    waved through silently.
  - `wtfu:9f684f108598`: *Synthetic waivers feed the title+files key set, so they can drop an
    unrelated real finding.*
  - Two findings from the unlisted part of campaign SPEC-0235-20261025-b741:
    - *The record is closed before the ledger and state writes; a later failure loses the
      overrides and blocks a retry.*
    - *A failed escalation_answered append is swallowed, so the CLI reports success but the
      conductor never wakes.*
The regenerate-scope, divergence-reset and follow-up-group findings (IDEA-352, IDEA-344,
IDEA-325) moved to SPEC-0266, which splits them out so each spec stays one campaign.

The other findings in IDEA-337 were dropped. Most were already fixed (`_announce` and
`remind_due` errors are contained; resume withdraws only after the transition). The rest were
test-only, cosmetic, or a design limit we accept.

## Goals / Non-goals

**Goals**
- A human attestation, or a waiver of a real finding, clears only the AC or finding it was
  resolved against at answer time, including the ACs the plan showed the human as
  implicitly attested.
- Answering an escalation either records everything (record, ledger, overrides) or leaves
  the record open so the answer can be retried. A failed wake-up event is reported, not
  hidden.

**Non-goals**
- Changing how reviewer-authored finding ACs are matched (`_covers` stays for
  reviewer↔AC matching inside one review).
- Migrating legacy attestations already stored with partial text. They stop matching after
  this change. The conductor re-answers if an old campaign needs it; we record this under
  As shipped.
- Making exact matching tolerant of a later reviewer re-quoting the same AC with different
  wording. This is an accepted cost: `ac_assessment[].ac` is LLM-quoted on every review, so
  a re-worded AC can re-raise an attested one, and the conductor re-attests. Do not
  reintroduce fuzzy matching to "fix" it. `norm_finding_title` only lower-cases and
  collapses whitespace (it does not strip backticks or punctuation), and we keep it that
  way.

## Decision

Make two local fixes:
- Store the resolved full AC text at answer time, for attestations and for the ACs a
  waived real finding implicitly clears, and match them by exact normalized equality.
- Do the answer's writes before closing the record, all under one escalations lock, and
  surface a failed `escalation_answered` append.

## Design

1. **Exact attestations** (`src/wt/agentic/escalations.py` `_plan_override`;
   `src/wt/agentic/queue.py` `triage_reviews`).
   - `_plan_override` stores the AC text it resolved, not the text the human typed.
     - For a free-text `--attest`, use the single entry of `_unmet_reach(...)`, or the exact
       match when there are several.
     - For a `#ac<j>` id, use the finding's `ac` (unchanged).
     - The `reach` map stays keyed by what the human typed, for display.
   - A waived real finding's stored `ac` is the reviewer's raw, often partial, quote. The
     plan already computes `implicit` (the unmet ACs the waiver reaches via
     `_unmet_reach`, escalations.py ~379-382) and shows them to the human as attested.
     Persist those full texts on the waive dict as `resolved_ac: [<full AC text>, ...]`,
     and have `triage_reviews` clear an AC when it exactly equals a waive's `resolved_ac`
     entry (or an `attest` entry). The raw `ac` is no longer used for clearing. Without
     this, exact matching would silently stop a waiver from clearing the AC the human was
     told it resolved.
   - In `triage_reviews` (queue.py ~369, ~392), `attest_all` is matched with
     `_exact(a, entry_ac)`, which is `norm_finding_title(a) == norm_finding_title(entry_ac)
     != ""`, instead of `_covers`.
   - `by_key` (title+files) is built only from non-synthetic waivers. A waiver is synthetic
     when its `id` matches `r"#(fix|ac\d+)$"`. Synthetic waivers still match through
     `by_id`.

2. **Answer atomicity** (`escalations.apply_answer`).
   - The lock order stays as it is: `item_lock`, then `E._locked(_lock_path(cfg))`.
   - The escalations lock is an `flock` on a fresh fd and is not reentrant, so take it once
     and inline the read and write. Do not call `_mutate`:
     1. `rec = read(...)` and check `status == "open"`.
     2. Append the `human_decision` and `note` ledger events.
     3. Write `human_overrides` to the state.
     4. `rec.update(status="answered", ...)`, then `_write(cfg, rec)`.
   - If step 2 or 3 raises, the record is still `open` and the error reaches the CLI.
   - The ledger appends are not rolled back, so a retry after a step 3 or step 4 failure
     appends a second `human_decision` and `note`, and a step 4 failure leaves overrides
     stored on a still-open record that the retry writes again. This is accepted: the
     decision events are an audit trail, overrides are rewritten idempotently, and no
     consumer counts them.
   - After the locks, the `escalation_answered` append is no longer wrapped in `suppress`.
     On `ValueError`/`OSError`, `apply_answer` returns `rec` with
     `event_error: "<error>"`, added to the returned dict only, after `_write`, so it never
     lands in the persisted record. `cli_escalations.escalations_answer` then prints a warning
     to stderr ("escalation_answered event not written: …; the conductor was not woken")
     and exits 0. The `--json` payload carries `event_error`.

## Alternatives considered

- **Keep `_covers` but require length ≥ 20 for attestations.** This narrows the problem but
  does not remove it. An attestation was resolved to a specific AC at answer time, and only
  that AC should clear.
- **Revalidate the whole override plan at apply time** (IDEA-337 `5f1d388e7510`). This is
  more work than the risk justifies, because `apply_answer` already re-checks `open` and
  the park under the locks.

## Acceptance criteria

- [x] An attestation answered for AC "uv run pytest passes" does not mark met a later unmet
      AC "uv run pytest passes with the new regression tests". A synthetic `p#fix` waiver
      does not drop a real finding with the same title and no files.
- [x] Waiving a real finding whose `ac` is a substring of an unmet AC still clears that AC
      on the next triage, through the persisted `resolved_ac` text.
- [x] If the state write in `apply_answer` raises, the record stays `open` and no override is
      stored. Retrying the answer after the fault clears succeeds.
- [x] If the `escalation_answered` append fails, `apply_answer` returns `event_error`
      (not persisted in the record), and `wt escalations answer` prints the warning to
      stderr and exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_review_triage.py::test_attestation_matches_exact_ac_only`: build
    `triage_reviews` inputs with `attested=["uv run pytest passes"]` and an unmet
    `ac_assessment` entry for the longer AC. Assert the AC stays unmet. Add a second case
    with an equal AC, which must be met.
  - `tests/test_agentic_review_triage.py::test_synthetic_waiver_does_not_key_real_findings`:
    `human_waived=[{"id": "p#fix", "title": "Fix the thing", "files": []}]` plus a real
    finding titled "Fix the thing" with `files: []`. The real finding survives.
  - `tests/test_agentic_review_triage.py::test_waiver_resolved_ac_clears_substring_ac`: a
    waive of a real finding whose `ac` is a partial quote of an unmet AC, stored with
    `resolved_ac` holding the full text, still clears that AC; without `resolved_ac` it
    does not.
  - `tests/test_agentic_escalations.py::test_plan_override_stores_resolved_ac_text`: a
    partial `--attest` text resolves to one unmet AC, and `plan["attest"]` holds that AC's
    full text. Also: a waive's plan holds `resolved_ac` with the implicitly attested full
    texts.
  - `tests/test_agentic_escalations.py::test_answer_state_write_failure_keeps_record_open`:
    monkeypatch `store.write_state` to raise `OSError` once. `apply_answer` raises, and
    `read(...)["status"] == "open"` with no `human_overrides`. A second `apply_answer`
    succeeds.
  - `tests/test_agentic_escalations_cli.py::test_answer_reports_failed_wake_event`:
    monkeypatch `E.append` to raise for `escalation_answered`. The exit code is 0, stderr
    contains `conductor was not woken`, and `--json` has `event_error`. Also assert the
    stored record has no `event_error` key.
- **Sabotage checks.** Each one must fail its test; record the result in the spec:
  - restore `_covers` for `attest_all`
  - build `by_key` from all waivers
  - drop `resolved_ac` from the waive dict
  - move the record write back before the ledger writes
  - re-add `suppress` around the append without `event_error`
- **Regression guard:** run the full `uv run pytest`. In particular,
  `test_agentic_escalations*.py` and `test_agentic_review_triage.py` must stay green.
  Existing SPEC-0235 tests that relied on substring attestations are updated to pass the
  resolved text, and each such edit is noted under As shipped. The waiver case is not
  papered over this way: `resolved_ac` covers it.
- **Manual verification:** none. Every path is exercised in-process.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

### Test record (2026-10-27)

- `uv run pytest -q` (full suite): 3246 passed, 3 skipped, 1 failed on the first run
  (`test_rich_output_guard::test_no_unlisted_raw_writes`: my first stderr warning used
  `click.echo`; switched to `R.warn`). After the fix, `tests/test_rich_output_guard.py`,
  `test_agentic_escalations*.py` and `test_agentic_review_triage.py` pass (170+ tests).
- New tests: `test_attestation_matches_exact_ac_only`, `test_synthetic_waiver_does_not_key_real_findings`,
  `test_waiver_resolved_ac_clears_substring_ac` (triage); `test_plan_override_stores_resolved_ac_text`,
  `test_answer_state_write_failure_keeps_record_open`,
  `test_apply_answer_returns_event_error_without_persisting_it`,
  `test_waived_partial_ac_finding_clears_full_ac_on_next_triage` (escalations);
  `test_answer_reports_failed_wake_event` (CLI).
- Sabotage checks, each failed its test as expected: `_covers` for `attest_all` (1 failed);
  `by_key` from all waivers (1 failed); `resolved_ac` dropped (2 failed); record written before
  the ledger writes (`test_answer_state_write_failure_keeps_record_open` failed); `suppress`
  back without `event_error` (2 failed).

### As shipped (2026-10-27)

- Implemented as designed. `triage_reviews` clears an unmet AC only by `_exact` equality with an
  `attest` text or a waive's `resolved_ac` entry; a waive's raw `ac` no longer clears anything.
  Waivers stored before this change have no `resolved_ac`, so they stop clearing ACs (legacy
  attestations are likewise not migrated, per Non-goals).
- `plan["override"]["reach"]` is keyed by the stored (resolved) AC text, not by what the human
  typed, because `cli_escalations` looks it up by each `attest` entry.
- `apply_answer` holds the escalations lock once and inlines read, ledger, state and record
  write; ledger events may repeat on a retry (accepted, see Design §2).
- The CLI warning goes through `render.warn` (stderr), because the output-guard test forbids raw
  writes.
- Existing test edits: `test_waiving_a_real_major_that_covers_an_unmet_ac_clears_the_ac` now
  passes `resolved_ac`; `test_apply_takes_item_lock_then_the_escalations_lock_nested` now checks
  the outermost `item-` (ledger/state writes nest reentrant item locks inside the escalations
  lock).
- Brief items on `put_back()` and `_open_group` (Design §3/§5 of the original review) concern
  SPEC-0266's scope, not this spec; nothing changed there.

## Open questions

- (none)

## Clock Log

