---
id: SPEC-0264
title: "Review pipeline integrity: exact attestations, safe answers, scoped regenerate, locked follow-ups"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-337
parent: SPEC-0169
tags: [agentic, review, close, escalations, review-follow-up]
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
- **IDEA-352** `wtfu:71ee806477f9` (SPEC-0255): *Regenerate commit commits the whole index,
  not exactly the declared paths.* A regenerate command that stages files itself gets
  undeclared content into the commit. The stray-file check cannot see that content, because
  it is already committed.
- **IDEA-344** `wtfu:a9038f6f3bb1` (SPEC-0247): *Resuming a loop_exhausted park with
  --reset-loops keeps the old fix rounds for divergence.* The conductor's reset is undone in
  practice: the next fix round can trip `diverging` on rounds from before the reset.
- **IDEA-325** (SPEC-0239)
  - `wtfu:29b92671b469`: *Unlocked read-modify-write of the group Summary.* A human edit
    made between the read and the write is lost.
  - `wtfu:2031670b701b` and `wtfu:ba2432d61e35`: *The filer adopts its own group even when
    that group is closed or archived.*

The other findings in these ideas were dropped. Most were already fixed:
- `git add` moved inside the regenerate `try`
- the docs-only allowlist check now runs before the regenerate commands
- `_announce` and `remind_due` errors are contained
- resume withdraws only after the transition
- range-diff uses `-s`

The rest were test-only, cosmetic, or a design limit we accept (for example, the revert
guard detects only exact single-commit reverts).

## Goals / Non-goals

**Goals**
- A human attestation or waiver clears only the AC or finding it was resolved against at
  answer time.
- Answering an escalation either records everything (record, ledger, overrides) or leaves
  the record open so the answer can be retried. A failed wake-up event is reported, not
  hidden.
- The regenerate commit holds only declared paths.
- `--reset-loops` gives divergence detection a fresh start.
- The follow-up filer never loses a concurrent edit to its group, and never writes into a
  closed group.

**Non-goals**
- Changing how reviewer-authored finding ACs are matched (`_covers` stays for
  reviewer↔AC matching inside one review).
- Migrating legacy attestations already stored with partial text. They stop matching after
  this change. The conductor re-answers if an old campaign needs it; we record this under
  As shipped.
- Strengthening the revert guard or the absorbed-commit heuristics (SPEC-0252 follow-ups).

## Decision

Make five local fixes:
- Store the resolved full AC text at answer time, and match attestations by exact
  normalized equality.
- Do the answer's writes before closing the record, all under one escalations lock, and
  surface a failed `escalation_answered` append.
- Refuse a regenerate step that staged undeclared paths.
- Cut divergence rounds at a `resume` that reset loops.
- Do the follow-up group read-modify-write under `org_write_lock`, and match only open
  groups.

## Design

1. **Exact attestations** (`src/wt/agentic/escalations.py` `_plan_override`;
   `src/wt/agentic/queue.py` `triage_reviews`).
   - `_plan_override` stores the AC text it resolved, not the text the human typed.
     - For a free-text `--attest`, use the single entry of `_unmet_reach(...)`, or the exact
       match when there are several.
     - For a `#ac<j>` id, use the finding's `ac` (unchanged).
     - The `reach` map stays keyed by what the human typed, for display.
   - In `triage_reviews`, `attest_all` is matched with `_exact(a, entry_ac)`, which is
     `norm_finding_title(a) == norm_finding_title(entry_ac) != ""`, instead of `_covers`.
     This covers both attested texts and waived findings' `ac`.
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
   - After the locks, the `escalation_answered` append is no longer wrapped in `suppress`.
     On `ValueError`/`OSError`, `apply_answer` returns `rec` with
     `event_error: "<error>"`. `cli_escalations.escalations_answer` then prints a warning
     to stderr ("escalation_answered event not written: …; the conductor was not woken")
     and exits 0. The `--json` payload carries `event_error`.

3. **Regenerate commit scope** (`src/wt/agentic/close.py` `_regenerate_after_rebase`).
   - After `git add -- <declared>`, read
     `staged = git diff --cached --name-only -z`.
   - If `set(staged) - set(declared)` is non-empty, call `put_back()` and return
     `"regenerate commands staged undeclared files: " + ", ".join(sorted(extra)[:10])`.
     That return value is the `regenerate_failed` park.
   - Otherwise commit as today.

4. **Divergence reset** (`src/wt/agentic/divergence.py` `rounds`).
   - In the cut loop, a `resume` record with `loops_reset` truthy also sets `cut = i + 1`,
     whatever park preceded it.
   - Update the docstring and the SPEC-0247 note in the docstring.

5. **Locked, open-only follow-up groups** (`src/wt/agentic/followups.py`).
   - `find_marker(cfg, marker, *, open_only=False)`. With `open_only`, it skips nodes whose
     TODO keyword is in `dones`, and it scans the ideas file only, not the archive.
   - `_file` calls it with `open_only=True`. A closed own group then counts as absent: a new
     group is created. Recovery of already-filed lines still works through the `followup`
     ledger events (step 1).
   - `_file` wraps the span from `find_marker` (step 2) through `set_summary` /
     `retitle_idea` (step 5) in `with OW.org_write_lock(cfg):`. That lock is re-entrant,
     so the inner `OW.add_idea` / `EX.set_summary` calls still work. `own_text` is read
     inside the lock.
   - `group_legacy` keeps the current `find_marker` default.

## Alternatives considered

- **Keep `_covers` but require length ≥ 20 for attestations.** This narrows the problem but
  does not remove it. An attestation was resolved to a specific AC at answer time, and only
  that AC should clear.
- **Revalidate the whole override plan at apply time** (IDEA-337 `5f1d388e7510`). This is
  more work than the risk justifies, because `apply_answer` already re-checks `open` and
  the park under the locks.
- **Commit with a pathspec (`git commit -- <declared>`).** This silently leaves the
  undeclared files staged and needs the stray check to catch them. An explicit refusal is
  clearer.

## Acceptance criteria

- [ ] An attestation answered for AC "uv run pytest passes" does not mark met a later unmet
      AC "uv run pytest passes with the new regression tests". A synthetic `p#fix` waiver
      does not drop a real finding with the same title and no files.
- [ ] If the state write in `apply_answer` raises, the record stays `open` and no override is
      stored. Retrying the answer after the fault clears succeeds.
- [ ] If the `escalation_answered` append fails, `apply_answer` returns `event_error`, and
      `wt escalations answer` prints the warning to stderr and exits 0.
- [ ] A regenerate command that runs `git add` on an undeclared file parks
      `regenerate_failed` with that path in the detail, and the branch gets no regenerate
      commit.
- [ ] After `wt queue resume <cid> --reset-loops`, `divergence.rounds` returns only rounds
      recorded after that resume.
- [ ] A Summary edit made by another writer while `_file` holds the group is not lost. The
      edit waits on `org_write_lock`, and `_file` re-reads inside the lock. A campaign whose
      `wtfug:<cid>` group is closed gets a new group instead of lines appended to the
      closed one.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_review_triage.py::test_attestation_matches_exact_ac_only`: build
    `triage_reviews` inputs with `attested=["uv run pytest passes"]` and an unmet
    `ac_assessment` entry for the longer AC. Assert the AC stays unmet. Add a second case
    with an equal AC, which must be met.
  - `tests/test_agentic_review_triage.py::test_synthetic_waiver_does_not_key_real_findings`:
    `human_waived=[{"id": "p#fix", "title": "Fix the thing", "files": []}]` plus a real
    finding titled "Fix the thing" with `files: []`. The real finding survives.
  - `tests/test_agentic_escalations.py::test_plan_override_stores_resolved_ac_text`: a
    partial `--attest` text resolves to one unmet AC, and `plan["attest"]` holds that AC's
    full text.
  - `tests/test_agentic_escalations.py::test_answer_state_write_failure_keeps_record_open`:
    monkeypatch `store.write_state` to raise `OSError` once. `apply_answer` raises, and
    `read(...)["status"] == "open"` with no `human_overrides`. A second `apply_answer`
    succeeds.
  - `tests/test_agentic_escalations_cli.py::test_answer_reports_failed_wake_event`:
    monkeypatch `E.append` to raise for `escalation_answered`. The exit code is 0, stderr
    contains `conductor was not woken`, and `--json` has `event_error`.
  - `tests/test_agentic_close_regenerate.py::test_regenerate_that_stages_undeclared_file_parks`:
    the regenerate cmd is `python3 tools/spec_lint.py --write-ledger && touch x && git add x`.
    Close parks `regenerate_failed` with `x` in the detail, and `git log` shows no
    regenerate commit.
  - `tests/test_agentic_divergence.py::test_reset_loops_resume_cuts_rounds`: the ledger
    holds 3 fix rounds, a `park` (`loop_exhausted`), a `resume` (`loops_reset: true`) and 1
    new round. `rounds()` returns 1 round. The same ledger with `loops_reset: false` returns
    4.
  - `tests/test_agentic_review_triage.py::test_filer_rereads_group_under_lock`:
    monkeypatch `EX.read_idea_enrichment` so that the first call (outside any lock, in the
    old code) is followed by a concurrent `EX.set_summary` adding a line from another
    thread. That thread must block on `org_write_lock`. Assert the final Summary holds both
    the human line and the new follow-up lines.
  - `tests/test_agentic_review_triage.py::test_filer_ignores_closed_own_group`: create the
    `wtfug:<cid>` group, then close it (state DROPPED). `file_followups` creates a new group
    id and leaves the closed group's Summary unchanged.
- **Sabotage checks.** Each one must fail its test; record the result in the spec:
  - restore `_covers` for `attest_all`
  - build `by_key` from all waivers
  - move the record write back before the ledger writes
  - re-add `suppress` around the append without `event_error`
  - remove the staged-scope check
  - remove the `loops_reset` cut
  - drop the `org_write_lock` wrapper
  - pass `open_only=False` in `_file`
- **Regression guard:** run the full `uv run pytest`. In particular,
  `test_agentic_escalations*.py`, `test_agentic_review_triage.py`,
  `test_agentic_close_regenerate.py`, `test_agentic_divergence.py` and
  `test_ideas_group_followups.py` must stay green. Existing SPEC-0235 tests that relied on
  substring attestations are updated to pass the resolved text, and each such edit is noted
  under As shipped.
- **Manual verification:** none. Every path is exercised in-process.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log

