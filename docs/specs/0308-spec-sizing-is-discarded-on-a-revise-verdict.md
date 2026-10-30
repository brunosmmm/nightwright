---
id: SPEC-0308
title: "A revise verdict keeps its sizing: select_profile honors size and tone for revise, not only approve"
status: proposed
owner: user
created: 2026-10-30
source_idea: IDEA-415
kind: feature
parent: SPEC-0298
tags: [agentic, profiles, sizing, cost, bug]
---

## Context

[SPEC-0219](./0219-spec-sizing-and-tone-pick-the-campaign-profile.md) made the spec review
size the work (`size` xs..xl) and give it a `tone`; `select_profile` maps them through the
profile matrix to a campaign profile. [SPEC-0230](./0230-single-spec-review-round.md) made the
review a single round: a `revise` verdict is applied by the spec writer without a second
review, and the campaign is admitted (`admission: after_revision`, or `revise_pending` for an
internal spec whose text is not yet amended, SPEC-0279).

`select_profile` (`src/wt/agentic/profiles.py:391`) computes

```python
sized = (size in SIZES and tone in TONES) and review.get("verdict", "approve") == "approve"
```

so a `revise` review's size and tone are thrown away. A campaign admitted after a revision
opens with `profile_source=default`, `profile_reason="review predates sizing (no size/tone)"`
and `size=None`, although the review did size the spec. That reason is also false: the review
does not predate sizing.

Evidence, 2026-10-30:

- 45 of 77 stored spec reviews are `revise` and carry a size.
- 33 implement campaigns opened since 2026-10-26 ran as `default` / `balanced` (for example
  the `open` event of SPEC-0301-20261030-c726).
- Only 19 of 125 campaigns carry a size in `wt campaign export`.

Two consequences, both against the MVP ([SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md),
cost): the profile matrix never runs for most specs, so cheap specs get `balanced` and
critical ones get no `thorough`; and the sizing prediction is missing from the export
([SPEC-0236](./0236-wt-campaign-export-dataset-for-mis-sizing-analysis.md)), which starves the
predicted-versus-actual analysis it exists for.

## Goals / Non-goals

**Goals**
- A `revise` review with a valid size and tone selects the profile through the matrix, exactly
  as an `approve` does.
- Size, tone, rationale, `profile_source` and `profile_reason` reach the campaign's `open`
  event, `state.sizing` and `wt campaign export` for those campaigns.
- Every `reason` string stays true for the case it describes.
- A `split` verdict is still never sized into a profile, and `xl` is still "split".

**Non-goals**
- No change to the matrix, the tones or the sizes, and no change to admission rules
  (SPEC-0230, SPEC-0279, SPEC-0290).
- No backfill of past campaigns: their `open` events and state stay as they are.
- No change to the order of precedence: `--profile` flag first, then the review, then
  `agentic.default_profile`.
- No change to what a `split` campaign does with its review (`plan_add` already passes
  `review_doc=None` for `split_pending`).

## Decision

`select_profile` uses the review's size and tone when the verdict is `approve` or `revise`
(a review with no `verdict` key still counts as `approve`, for legacy sidecars). Any other
verdict (`split`) is not sized. A profile chosen from a `revise` review says so in its
`reason`, so a reader of the `open` event can tell the source review did not approve outright.
`would_pick` follows the same rule, so `wt spec review --show` reports the profile the queue
will actually choose.

## Design

**`select_profile`** (`src/wt/agentic/profiles.py`)

- Replace the approve-only test with a module constant
  `SIZING_VERDICTS = ("approve", "revise")` and
  `sized = size in SIZES and tone in TONES and verdict in SIZING_VERDICTS`, where
  `verdict = review.get("verdict", "approve")`.
- Return shape is unchanged: `{profile, source, reason, size, tone}`. `size` and `tone` are
  `None` when not sized, as today; with `--profile`, they are still recorded when the review
  is sized, now including `revise`.
- Reasons:
  - sized, `approve`: `size {size}, tone {tone} → {name}` (unchanged);
  - sized, `revise`: `size {size}, tone {tone} → {name} (revise review, applied without
    re-review)`;
  - sized `xl`: `size xl has no profile (split recommended)` (unchanged);
  - no review: `no approving review (--force)` (unchanged);
  - review without a valid size/tone, `approve` or `revise`:
    `review predates sizing (no size/tone)` (unchanged; now true because it is the only way
    to reach it for these verdicts);
  - any other verdict: `review verdict {verdict} does not size a campaign`.
- `xl` with `revise` cannot be stored (`spec_review._check_verdict` rejects it), but if one
  appears (hand-edited store) it falls into the `xl` branch: default profile, `size` recorded.

**`would_pick`** (`src/wt/agentic/spec_review.py:213`): return `None` unless the verdict is in
`SIZING_VERDICTS`, instead of unless it is `approve`.

**Callers, no change in shape.** `plan_add` (`src/wt/agentic/queue.py:5299`) already passes
`review_doc` (the latest review plus `admission`) and copies `size`, `tone`, `source` and
`reason` into the plan; `apply_add` writes them to `state.sizing` and the `open` event. The
rationale is stored when `pick["size"]` is set, so it appears for `revise` once `size` does.
`campaign_export.py` reads `sizing`, so no export change is needed. The spec only has to make
`select_profile` return the values.

**Stale-review caution.** For `after_revision` the review is for an earlier text. Its sizing
is still the best prediction of the work, and the revision is meant to address findings, not
change scope. That is why SPEC-0230 admits it, and why it is used here.

## Alternatives considered

- **Re-run the spec review after the revision to get an `approve`** — violates the
  one-review-round rule (SPEC-0230) and costs a review per spec.
- **Let the spec writer copy size/tone into the spec or the campaign by hand** — duplicates
  the review and drifts from it.
- **Honor size/tone for every verdict, including `split`** — a `split` review sizes the
  unsplit work, and the split campaign writes parts, not code; sizing a profile from it
  would be wrong.
- **Fix only the reason text** — leaves the matrix unused and the export empty, which is the
  harm.

## Acceptance criteria

- [ ] `select_profile` with `{"verdict": "revise", "size": s, "tone": t}` returns the matrix
      profile for every valid `(tone, size)` cell, with `source == "review"` and the
      recorded `size` and `tone`.
- [ ] That reason ends with `(revise review, applied without re-review)`; the approve reason
      is byte-for-byte unchanged.
- [ ] `select_profile` with `verdict: split` and a valid size/tone returns the default
      profile, `size` and `tone` `None`, and the reason
      `review verdict split does not size a campaign`.
- [ ] A `revise` review with `size: xl` returns the default profile, `size == "xl"` and the
      reason `size xl has no profile (split recommended)`.
- [ ] A `revise` review with no size or tone returns the default profile and the reason
      `review predates sizing (no size/tone)`.
- [ ] `--profile` still wins over a `revise` review, and records its size and tone.
- [ ] A campaign queued from a spec with a stored `revise` review (admission
      `after_revision` or `revise_pending`) opens with `profile_source == "review"`, the
      review's `size` and `tone`, and a profile from the matrix, in both its `open` event
      and `state.sizing`.
- [ ] `wt campaign export` shows that campaign's `size`, `tone` and
      `profile_source == "review"`.
- [ ] `would_pick` returns the same pick for a `revise` review as for an `approve` with the
      same size and tone, and `None` for a `split`.
- [ ] A custom matrix (`agentic.profile_matrix`) is honored for `revise` as for `approve`.

## Test plan

- **Automated tests, `tests/test_agentic_profiles.py`:**
  - extend `test_profile_matrix_every_cell` (or add a sibling parametrized over the same
    cells) to assert the `revise` pick equals the `approve` pick, with the extended reason;
  - extend `test_select_profile_precedence_and_reasons` for the `split`, `revise` + `xl`,
    `revise` without sizing and flag-over-revise cases;
  - a custom-matrix case with a `revise` review.
- **Automated tests, `tests/test_agentic_queue.py`:** store a `revise` review with size `s`,
  tone `cost-effective` for a spec, change the spec text, `plan_add` + `apply_add`; assert the
  `open` event and `state.sizing` carry `size=s`, `tone=cost-effective`,
  `profile_source=review` and the `frugal` profile. Repeat for an unamended spec
  (`revise_pending`). Add a `split_pending` case asserting size stays `None`.
- **Automated tests, `tests/test_agentic_campaign_export.py`:** a campaign opened from a
  `revise` review exports `size`, `tone` and `profile_source=review`.
- **Automated tests, `tests/test_spec_review_show.py`:** `--show` reports the would-pick
  profile for a `revise` review.
- **Manual verification:** with a stored `revise` review that has a size,
  `wt queue add SPEC-NNNN --dry-run` prints the matrix profile and the `revise` reason, not
  `default`; after queueing, `wt campaign export` lists the size.
- **Regression guard:** full `uv run pytest`, in particular the existing approve-path
  assertions in `tests/test_agentic_profiles.py` and the admission tests in
  `tests/test_agentic_spec_review_loop.py`; `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Merge the change; it applies to campaigns queued afterwards. No data migration.
2. Past campaigns keep `default` / no size. Analysis over the export should treat campaigns
   opened before this merge as unsized.
3. Check the first few revise-then-proceed campaigns after merge: their `open` event shows
   `profile_source: review`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The `revise` reason suffix and the `split` reason text are fixed here; wording may be
  tuned without a new spec.
