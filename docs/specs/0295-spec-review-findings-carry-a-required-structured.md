---
id: SPEC-0295
title: "Review findings carry a required, structured suggested_fix"
status: proposed
owner: user
created: 2026-10-30
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-076
parent: SPEC-0169
depends_on: [SPEC-0188, SPEC-0187, SPEC-0215]
tags: [agentic, review, personas, specs]
---

## Context

Promoted from idea `IDEA-076`. The question that started it: does a review give a solution for
each finding?

Today the `spec_reviewer` verdict schema (`SPEC_REVIEWER_SCHEMA`, `src/wt/agentic/personas.py`,
SPEC-0188) has findings `{severity, section, title, detail}`; only `severity` and `title` are
required. The conformance reviewer (`CONFORMANCE_REVIEWER_SCHEMA`, SPEC-0187) has
`{severity, ac, title, detail, files}` with the same two required keys. A reviewer usually puts
a concrete fix at the end of `detail` (SPEC-0197's review: "change the Design to a
`make_console(is_tty, env)` factory…"), but nothing requires it, and minor and nit findings
often only describe the problem. The reader of a finding — a human running `wt spec review`,
the spec writer revising in the SPEC_WRITING phase (SPEC-0215, `_spec_review_brief` in
`src/wt/agentic/queue.py`), the headless revise path (`revision_brief` in
`src/wt/agentic/spec_review.py`, SPEC-0230/0279), or the implementer fixing a conformance
finding (`review_brief`) — then has to re-derive the fix the reviewer already had in mind.

All of those consumers today read a finding's free-text `detail` and nothing else, so a
structured fix has to be threaded through that one seam, not through each consumer separately.

## Goals / Non-goals

**Goals**
- Every finding a `spec_reviewer` or `conformance_reviewer` returns carries a required
  `suggested_fix`: the concrete edit that resolves it, and an optional `acceptance`: how the
  reader can tell it is resolved.
- The two personas' prompts say so, for every severity including `minor` and `nit`.
- The human output (`wt spec review`, `--show`) shows both fields.
- The spec writer's revision brief, the headless revise brief and the implementer's fix brief
  carry both fields, so the next agent applies the fix instead of re-deriving it.
- Reviews already stored without the fields keep loading and rendering.

**Non-goals**
- The `test_reviewer` and `adversarial_reviewer` findings (a follow-up, same helper).
- Making the reviewer's fix binding: the reader may apply a better fix; `suggested_fix` is a
  starting point, and the verdict logic (approve / revise / split, triage score) is unchanged.
- Any change to severity vocabulary, scoring, waiving or de-duplication keys.
- Backfilling `suggested_fix` into stored reviews.

## Decision

Add `suggested_fix` (string, **required**) and `acceptance` (string, optional) to the finding
items of the `spec_reviewer` and `conformance_reviewer` schemas, and require the reviewers by
prompt to fill `suggested_fix` with a concrete edit for every finding. Thread the two fields
through the existing single text seam instead of adding parallel plumbing: a pure helper
`finding_text(finding)` in `src/wt/agentic/personas.py` renders `detail` followed by
`Fix: <suggested_fix>` and `Done when: <acceptance>` lines (a line is omitted when its field is
absent or blank). The human renderer, `_spec_review_brief`, `revision_brief`, and
`finding_detail` (the conformance/adversarial detail used for ledger `finding` events, triage
records and the implementer brief) all build their text through it. Stored findings keep the
two fields as plain keys; nothing is rewritten.

A reply with a missing `suggested_fix` does not conform to the schema, so a run that returns one
is a failed run like any other non-conforming verdict. A present but blank `suggested_fix` in a
**spec** review is rejected by the same check that already rejects an `approve` carrying a
`must_fix`, so nothing is stored.

## Design

### Schemas (`src/wt/agentic/personas.py`)

- `SPEC_REVIEWER_SCHEMA`: finding item `required: ["severity", "title", "suggested_fix"]`;
  properties gain `"suggested_fix": {"type": "string"}` and `"acceptance": {"type": "string"}`.
- `CONFORMANCE_REVIEWER_SCHEMA`: the same two additions on its finding item
  (`required: ["severity", "title", "suggested_fix"]`).
- `TEST_REVIEWER_SCHEMA` and `ADVERSARIAL_REVIEWER_SCHEMA` are unchanged.
- `runner._conforms` already enforces `required` inside array items, so no runner change is
  needed. It has no `minLength`, so blankness is checked in `spec_review._check_verdict`
  (below).

### The text helper

```python
def finding_text(f: dict, detail=None) -> str | None:
    """`detail` (default f['detail']), then 'Fix: …' and 'Done when: …' lines for a non-blank
    suggested_fix / acceptance; None when all three are empty."""
```

Pure, no I/O, strings are stripped, non-string values are ignored (a stored finding is never
trusted to be well-typed). `queue.finding_detail(f)` keeps folding `trigger` (SPEC-0202) first
and passes its result as `detail`, then appends the fix lines, so the ledger `finding` event,
the triage record `detail`, `_bullet` and `review_brief` `must_fix[].detail` carry the fix with
no change to those call sites. Findings without the new keys render exactly as before.

### Spec reviewer

- `src/wt/agentic/personas/spec_reviewer.md`: the Verdict section's findings line becomes
  `[{"severity": …, "section": "...", "title": "...", "detail": "...", "suggested_fix": "...",
  "acceptance": "..."}, …]`, and a new "Findings" paragraph states: every finding, including
  `minor` and `nit`, has a `suggested_fix` that is the concrete edit to the spec (the sentence or
  section to add, change or delete, in the spec's own words, not "clarify this"); `acceptance`
  says how the reviser can tell it is fixed (optional, one line); `detail` explains the problem.
- `spec_review._check_verdict` raises `ValueError` when any finding's `suggested_fix` is not a
  non-blank string (message names the finding title). Nothing is stored; the journal records the
  error like the existing `approve`-with-`must_fix` case.
- `spec_review.revision_brief` and `queue._spec_review_brief` build each `must_fix` entry's
  `detail` with `finding_text(f)`, so `render_pending_brief` already shows
  `— <detail>\nFix: …\nDone when: …` to the spec writer (the writer persona is told by
  `write_rules` to address every finding; the brief now carries the fix).
- `spec_writer.md` gets one sentence: a finding's `Fix:` line is the reviewer's suggested edit —
  apply it, or an equivalent that meets the finding's `Done when:` line; do not re-derive the
  fix from the `detail` text.

### Conformance reviewer

- `src/wt/agentic/personas/conformance_reviewer.md`: the `findings` line gains
  `"suggested_fix": "...", "acceptance": "..."` and the Review section says every finding
  names the concrete code or test change that resolves it (file and function where known), and
  `acceptance` the check (a test, command or observable behavior) that shows it resolved.
- No change to scoring, `ac_assessment`, de-duplication (`finding_key` / `norm_finding_title`)
  or waiving: the fix text is part of `detail` only.

### Human output (`src/wt/cli.py`, the review renderer around line 1897)

The findings table keeps its four columns; the `detail` cell is `finding_text(f)` so the fix and
acceptance lines sit under the problem. The `--json` payload (`wt.spec.review.v1`) carries the
two keys as stored on each finding; `review_payload` copies findings verbatim, so no payload
code changes. The TUI queue/campaign views that show a spec-review finding's `detail` use the
same helper where they render findings.

### Compatibility

Stored reviews and ledgers from before this spec have findings without the keys; every consumer
reads them with `.get`, `finding_text` omits the missing lines, and `wt spec reviews` /
`--show` and `would_pick` do not validate stored findings against the new schema.

## Alternatives considered

- **Fold the fix into `detail` by prompt only.** Today's behavior; unenforced, nothing to
  render or test, and nits stay unfixed.
- **Separate `suggested_fix` / `acceptance` plumbing in each consumer.** Many call sites build
  briefs from `detail`; threading the fields through the one text helper changes less code and
  cannot leave one consumer behind.
- **Make the fix optional for `nit`.** A nit with no fix is the case that prompted the idea;
  the cost of one sentence is small.
- **Add the fields to all four reviewer personas now.** Keeps this spec to the two the idea
  names and leaves the other two to a follow-up through the same helper.

## Acceptance criteria

- [ ] `SPEC_REVIEWER_SCHEMA` and `CONFORMANCE_REVIEWER_SCHEMA` finding items list
      `suggested_fix` in `required` and define `suggested_fix` and `acceptance` as strings;
      `TEST_REVIEWER_SCHEMA` and `ADVERSARIAL_REVIEWER_SCHEMA` are unchanged.
- [ ] A `spec_reviewer` or `conformance_reviewer` verdict with a finding lacking `suggested_fix`
      is non-conforming (`runner._conforms` is false for it).
- [ ] `wt spec review` rejects a verdict whose finding has a blank or non-string
      `suggested_fix`: it raises `ValueError` naming the finding title and stores no review.
- [ ] `spec_reviewer.md` and `conformance_reviewer.md` tell the reviewer to give every finding,
      including `minor` and `nit`, a concrete `suggested_fix`, and list `suggested_fix` and
      `acceptance` in their Verdict findings line.
- [ ] `finding_text` returns `detail`, then `Fix: …`, then `Done when: …`, omitting blank or
      absent parts, and returns `None` when all are empty; a finding without the new keys yields
      exactly its `detail`.
- [ ] `wt spec review` (text) and `wt spec review --show` print each finding's fix and
      acceptance; `--json` output carries both keys on each finding.
- [ ] The spec writer's `pending_brief` for a `revise` or `split` review contains the
      `Fix:` / `Done when:` text in every `must_fix` entry's `detail`, and
      `render_pending_brief` shows it.
- [ ] The headless revision brief (`revision_brief`) carries the same text for every finding,
      nits included.
- [ ] A conformance finding with `suggested_fix` produces a ledger `finding` event and a
      `must_fix` brief entry whose `detail` includes the `Fix:` line; the triage score, finding
      ids and de-duplication keys are identical to those of the same finding without it.
- [ ] A stored spec review without the new keys still loads and renders through
      `wt spec review --show`, `wt spec reviews` and `revision_brief` without error.
- [ ] `spec_writer.md` tells the writer to apply the `Fix:` line or an equivalent meeting
      `Done when:`.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_personas.py`: schema assertions for the two personas (required key,
    property types) and unchanged test/adversarial schemas; `_conforms` rejects a finding
    without `suggested_fix` and accepts one with it; the rendered `spec_reviewer`,
    `conformance_reviewer` and `spec_writer` prompts contain the required wording;
    `finding_text` unit cases (all parts, only fix, blank strings, non-string values, none);
    `render_pending_brief` shows the fix lines; `finding_detail` with `trigger` plus a fix.
  - `tests/test_agentic_spec_review.py`: a scripted reviewer verdict with a blank
    `suggested_fix` writes nothing and errors; a valid one is stored with both keys; the text
    output and `--json` payload show them; a fixture review stored without the keys still
    renders (`--show`, `reviews`).
  - `tests/test_agentic_spec_review_loop.py`: a `revise` review with fixes yields a
    `pending_brief` whose `must_fix[].detail` contains `Fix:`; the headless `revision_brief`
    carries the fix for a nit as well.
  - `tests/test_agentic_review_triage.py`: a conformance finding with and without
    `suggested_fix` has the same triage score, ids and keys, and the first one's brief detail
    contains the fix.
  - Existing tests and the fake runner's canned spec-review and conformance verdicts
    (`tests/test_agentic_e2e.py` and others) gain a `suggested_fix` on each finding; the
    update is part of this change.
- **Manual verification:** run `wt spec review SPEC-NNNN` on a spec with known problems
  (e.g. a copy with a leftover template placeholder) and check that every finding, including a
  nit, prints a `Fix:` line; run `wt spec review SPEC-NNNN --show` on a review stored before
  this change and check it renders without error.
- **Regression guard:** `uv run pytest` (the full suite, including the docs and skills
  guards) and `python3 tools/spec_lint.py` pass.

## Rollout / migration

1. Ship the schema, prompt, helper and renderer changes together; the schema's `required`
   takes effect on the next reviewer run, so a reviewer run in flight at restart may fail to
   conform and is retried by the normal run-error path.
2. No data migration: stored reviews and ledgers are left as they are.
3. Update the canned verdicts of the fake runner and the tests listed above in the same change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decided above: scope is the spec reviewer and the conformance reviewer; the test and
adversarial reviewers follow through the same helper._
