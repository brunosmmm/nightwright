---
id: SPEC-0230
title: "Single spec review round: admit a revised spec without re-review"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, specs, queue]
source_idea: IDEA-101
depends_on: [SPEC-0210]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), from
`IDEA-101` (spec review churn).

The plan (`plans/mini-agentic-wt.org`, spec phase 1b–1d and persona 1) gives the spec
reviewer **one** pass. It approves, sends the spec for revision with suggestions, or splits
it. A revision applies the suggestions and the spec is accepted. It is not reviewed again.
The specs that implemented this drifted into a loop:

- `spec_review.check_admissible` (SPEC-0188, SPEC-0210), the `wt queue add` gate, admits only
  on an `approve` review keyed to the spec's **current** sha. Any edit made to apply a
  `revise` therefore requires another review.
- [SPEC-0214](./0214-spec-review-loop-inside-the-campaign-with-in-loop.md) (accepted, not
  built) defines `spec_review_max: 3` and the cycle SPEC_WRITING → SPEC_REVIEW, repeated.

Observed cost on 2026-10-25: SPEC-0209 took 3 review rounds, SPEC-0225/0229 took 5 and
SPEC-0228 took 4. Each round found a new narrow major after the previous ones were fixed, and
each round cost 10–15 minutes of edit, merge and review. The human's ruling: "there should
be only at most 1 review round in the spec phase".

## Goals / Non-goals

**Goals**
- `wt queue add` admits a spec whose latest review is `revise` once the spec text has changed
  since that review. The admission is recorded, and the implementer gets the review findings
  so it can check that they were applied.
- A second review is never required. `wt spec review` on an already-reviewed spec refuses
  unless `--force` is given, and warns when it runs.
- The in-campaign spec phase that SPEC-0214 defines runs one review, as stated in
  **Overrides of SPEC-0214** below.

**Non-goals**
- Automatic splitting (IDEA-065). `split` stays a refusal at admission.
- Changing the reviewer's rubric, schema or severities.
- An over-review admission of an *unrevised* `revise` (a reason instead of an edit). The
  revision is the decision; IDEA-101's convergence-rule note is resolved by this spec.

## Decision

`check_admissible` judges the **latest** stored review of the spec id, across all shas,
instead of only the review for the current sha. It admits:
- an `approve` for the current sha (as today); or
- a `revise` for an **earlier** sha (the revision happened).

A new pure `revision_brief(review)` turns the revise review's findings into a
`pending_brief`. `apply_add` seeds the campaign with it and appends an
`admitted_after_revision` ledger event. `wt spec review` gains `--force`, which is required
when the store already holds a review for the spec id. SPEC-0214's round loop is overridden
by an amendment (this spec), not by `supersedes`. The reasoning is under
**Supersession mechanism** below.

## Design

### Review lookup (`spec_review.py`)

- `list_reviews(cfg, spec_id) -> list[dict]` returns every readable payload in
  `<data>/agentic/reviews/<spec_id>/*.json` (via `_read_json_dict`; corrupt files are
  skipped), sorted by `reviewed_at`, oldest first. Temp files (`*.tmp`) are ignored.
- `latest_review(cfg, spec_id) -> dict | None` is the last element, or None.

### Admission (`check_admissible(cfg, spec_path, spec_id) -> dict`)

The signature is unchanged. It returns the governing review payload, plus one non-persisted
key, `admission`, set to `"approved"` or `"after_revision"`. With `sha` the current file sha
and `latest = latest_review(...)`:

1. A stored `approve` for `sha` ⇒ `approved`. If there is none, a legacy sidecar `approve`
   with a matching sha ⇒ `approved`. Both are unchanged from SPEC-0210.
2. Otherwise, by `latest`:
   - None ⇒ ValueError `"<SPEC> has no spec review; run wt spec review <SPEC>"`.
   - `split` (any sha) ⇒ ValueError `"<SPEC>: the spec review recommends a split
     (<sha12>); split the spec and queue the parts"`.
   - `revise` with `spec_sha256 == sha` ⇒ ValueError `"<SPEC>: the review asks for
     revisions that are not applied yet (the text is unchanged since <sha12>); edit the
     spec, then queue it (no second review needed)"`.
   - `revise` with `spec_sha256 != sha` ⇒ admitted, `admission: "after_revision"`.
   - `approve` for another sha (the spec was edited after its approval) ⇒ ValueError
     `"<SPEC> was edited after its approving review (<sha12>); run wt spec review <SPEC>
     --force"`.

Why these choices:
- The **latest** review governs, not "exactly one revise". Specs reviewed several times
  before this spec (SPEC-0225, 0228, 0229) have several `revise` reviews. Requiring exactly
  one would refuse precisely the specs whose churn caused this change. The rule only asks
  whether the last verdict was acted on.
- An edit after an **approve** is not a requested revision, so nothing vouches for it, and it
  stays a refusal. A review run then is the first review of new text, not a second round of
  the same one.
- A `split` stays a refusal even after edits: the expected outcome of a split is new specs,
  and queueing the original would bypass that.
- Legacy sidecars are only consulted for `approve`, as today. Store-only revise lookups keep
  the rule simple; sidecars predate revise tracking.

### Brief and recording

- `revision_brief(review) -> dict` (pure, `spec_review.py`) returns:
  `{"source": "spec_review", "fix_brief": "This spec was admitted after one revision of
  review <sha12>, with no second review. Before implementing, check that the spec text
  addresses each finding below. Implement the spec as written; where a finding is not
  addressed, apply its suggestion in a way consistent with the spec.", "must_fix": [{"title":
  "[<severity>] <title>" + (" (<section>)" if section), "detail": <detail or None>} for
  every finding], "human_notes": []}`. Every severity is included, nits too, because the
  implementer is checking that each suggestion was applied, not deciding which ones matter.
  `personas.render_pending_brief` renders it unchanged (`MUST FIX: [major] … — detail`).
- `queue.plan_add`: `review` becomes `"approved" | "after_revision" | "forced"`. The plan
  gains `revision: {review_sha, current_sha, findings}` for `after_revision` (else None),
  where `findings` is the review's list as stored. `--force` skips `check_admissible`, as
  today. The dry-run text (`R.plan`) prints a `revision` line with both sha12s and the
  findings count. `--json` (`wt.queue.add.v1`) carries `revision` in full.
- `queue.apply_add`: when `plan["revision"]` is set, `state.pending_brief =
  revision_brief(<that review>)` before `store.create_campaign`, then
  `store.append_event(cfg, cid, "admitted_after_revision", plan["revision"], by=by)`. The
  existing implement path clears `pending_brief` after the first implementer run
  (`queue.py` ≈992), so exactly the first run sees it.
- `schemas.EVENT_KEYS["admitted_after_revision"] = ("review_sha", "current_sha",
  "findings")`.
- SPEC-0219 (accepted, not built) calls `select_profile` with the review that
  `check_admissible` returns. A `revise` review is not an approving review, so it falls to
  `default_profile` under 0219's own precedence. This spec does not change that.

### `wt spec review` (`cli.py` `spec_review_cmd`)

- New flag `--force`.
- Before running, for both the SPEC-ID and `--path` forms: if `latest_review(cfg, spec_id)`
  is not None and `--force` is absent, raise ClickException `"<SPEC> was already reviewed
  (<verdict>, <sha12>); a revised spec is queued without a second review. Pass --force for
  an extra round"`.
- With `--force` and a prior review, print `warning: extra review round for <SPEC> (previous:
  <verdict>, <sha12>)` to stderr, then run as today.
- The help text replaces "admits a spec only when its review for the current text is
  `approve`" with the rule above.

### Overrides of SPEC-0214 (in-campaign spec phase)

The in-campaign spec phase is SPEC_WRITING → SPEC_REVIEW (once) → [`revise`/`split` ⇒
SPEC_WRITING revise mode applies the findings ⇒ accept, no second review] → IMPLEMENTING.
SPEC-0214's implementer must apply these overrides to its Design:
- **Goals**: `(SPEC_WRITING → SPEC_REVIEW)*` becomes the single pass above.
- **State and reasons**: `spec_review_max` is not added to profiles. The round count is fixed
  at 1, and `counters.spec_rounds` is not needed.
- **Worker, new step 0′** (after step 0): if this campaign's ledger already has a conforming
  `spec_review` event with verdict `revise` or `split`, no reviewer runs. If the current spec
  sha differs from that event's `spec_sha256`, the step returns `accept_after_revision`, and
  `_apply` runs the accept steps 0–6. Accept step 3's commit message ends `(after revision)`.
  Accept step 5 sets `pending_brief = revision_brief(<the stored review>)` instead of None.
  If the sha is unchanged, park `spec_review_exhausted`, detail "the revision did not change
  the spec", resume_to SPEC_WRITING. This also covers resumes after an accept failure: they
  go to accept, never to a second review.
- **Routing**: `revise` or `split` always go to SPEC_WRITING revise mode. The "Otherwise
  (exhausted)" branch is removed. In-campaign `split` keeps SPEC-0214's narrowing rule: the
  writer narrows the spec in place, and the narrowing is the revision.
- **Resume**: `--reset-loops` has no spec-round effect.
- **Acceptance criteria**: "Revise path" becomes writer → revise → writer → accept, with one
  `spec_review` event. "Exhaustion" is replaced by the unchanged-sha park above.

### Supersession mechanism

`supersedes` (SCHEMA.md) names "decisions this replaces" and pairs with the old spec's
`status: superseded` + `superseded_by`. `tools/spec_lint.py` has no partial form. Superseding
SPEC-0214 would retire its whole design (states, accept step, failure parks), most of which
stands. This spec is therefore an **amendment**. It has no `supersedes` field, and the
overrides above are binding on SPEC-0214's implementation and on its `/wt-verify`. SPEC-0214
is accepted but not built, so no shipped behaviour changes.

### Displays (SPEC-0211, SPEC-0217)

A spec phase is now 1 review plus an optional revision. SPEC-0211's `spec_round` is 1 at
most: `SPEC_WRITING r1 · revising · last: revise`, then `r1 · last: revise` after the
accept. Its round-2 examples become unreachable but stay correct. SPEC-0217's `round` field
is always 1. Neither spec needs a change.

## Alternatives considered

- **`--over-review "reason"`** (the first draft for IDEA-101): admit an unrevised `revise` on
  a recorded decision. Rejected by the human's ruling: the revision itself is the decision,
  and an extra flag keeps the loop as the default.
- **Admit only on exactly one prior revise**: refuses the multi-round legacy specs. See
  Design.
- **Supersede SPEC-0214 with a full copy**: a large rewrite of an unbuilt spec to change one
  rule. See Supersession mechanism.

## Acceptance criteria

- [x] `check_admissible` with store fixtures:
  - an approve for the current sha, and a legacy sidecar approve, each admit `approved`;
  - a latest `revise` for an earlier sha admits `after_revision`, also when older revise
    reviews exist;
  - no review, a latest `split` (current or earlier sha), a `revise` for the current sha, and
    an `approve` for an earlier sha each raise ValueError with the message fragment in Design;
  - a corrupt store file is skipped.
- [x] `revision_brief` returns the documented shape: every finding with severity, title,
      section and detail, plus the fix_brief naming the review sha12. Its
      `render_pending_brief` output contains each finding's title and detail.
- [x] `wt queue add SPEC` on an after-revision spec: the dry run prints the `revision` line,
      and `--json` carries `review: "after_revision"` and `revision` with all findings.
      Nothing is written without `--yes`.
- [x] With `--yes`: the campaign's `pending_brief` equals `revision_brief(review)`, and the
      ledger has `admitted_after_revision {review_sha, current_sha, findings}` after `open`,
      valid under `schemas.ledger_problems`. The first implementer prompt (FakeRunner)
      contains every finding title, and the second run's prompt does not.
- [x] `wt spec review SPEC` with a prior stored review exits non-zero with the "already
      reviewed" message and runs no reviewer. With `--force` it runs and prints the
      extra-round warning. With no prior review it runs as before. The same holds for
      `--path`.
- [x] Existing approve-path tests pass unchanged. `uv run pytest` is green and
      `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_spec_review_store.py`: `list_reviews` / `latest_review` ordering, skipping
    corrupt files and `.tmp` files; every `check_admissible` case in AC 1, using
    `save_review` fixtures with explicit `reviewed_at`.
  - `tests/test_agentic_spec_review.py`:
    - `revision_brief` shape and `render_pending_brief` text;
    - `wt spec review` refusal, the `--force` warning and the no-prior run (FakeRunner), each
      for both the SPEC-ID and `--path` forms.
  - `tests/test_agentic_queue.py`:
    - `plan_add` / `apply_add` after revision: plan `revision`, state `pending_brief`, and
      the ledger event with `ledger_problems == []`;
    - one tick with FakeRunner, checking the first implementer prompt has the findings and
      the next run's prompt does not.
  - `tests/test_agentic_queue_cli.py`: the dry-run text `revision` line and the `--json`
    fields.
- **Manual verification:** on a scratch data dir, save a `revise` review for a spec, edit
  the spec, and run `uv run wt queue add <SPEC>` (dry run). It must show `after_revision`.
  Then run `uv run wt spec review <SPEC>`: it must refuse without `--force`.
- **Regression guard:** full `uv run pytest`, in particular the e2e, waiting and dashboard
  tests that use the `write_review` approve helper.

### Test record (2026-10-25)

- `uv run pytest -q`: full suite green (new tests in `test_spec_review_store.py`,
  `test_agentic_spec_review.py`, `test_agentic_queue.py`, `test_agentic_queue_cli.py`).
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: exit 0.
- Manual verification (revise review saved, spec edited, `wt queue add` dry run shows
  `after_revision`, `wt spec review` refuses without `--force`) is covered by the CLI tests
  above, which run the same commands on a scratch data dir.

### As shipped

- Existing tests that asserted the old refusal text ("no approving review …") or reviewed the
  same spec twice were updated to the new messages / `--force`; `write_review` in
  `tests/test_agentic_e2e.py` gained optional `sha`, `findings`, `reviewed_at` arguments.
- The `revision` plan key is printed in the dry run as one line
  (`review <sha12> -> spec <sha12>, N findings`); `--json` carries it in full.
- SPEC-0214's overrides are not implemented here (that spec is unbuilt); only the
  admission, brief, event and `wt spec review --force` parts shipped.

## Rollout / migration

None needed for stored data. Specs already parked behind a `revise` whose findings were
applied (edited since) become admissible without another review. When SPEC-0214 is queued,
the conductor points its campaign at this spec's **Overrides of SPEC-0214** (`wt campaign
note`), and SPEC-0214's verification checks them.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None. Decisions are recorded above._
