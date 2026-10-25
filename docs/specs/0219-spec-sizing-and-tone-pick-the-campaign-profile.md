---
id: SPEC-0219
title: "Spec sizing and tone pick the campaign profile"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, specs, queue, profiles]
source_idea: IDEA-064
depends_on: [SPEC-0188, SPEC-0171]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), Breakdown
line "Spec reviewer with sizing and tone to profile selection" (promoted from `IDEA-064`).

[SPEC-0188](./0188-spec-review-before-admission-wt-spec-review-and-a.md) added `wt spec review`
and the `wt queue add` gate on an approving review; it listed "sizing → profile selection" as a
later-wave non-goal. Today every campaign runs `agentic.default_profile` (`balanced`) unless the
human passes `--profile`: a one-line rendering fix gets the same models, budgets and loop limits
as a change to the queue state machine. The spec reviewer already reads the whole spec and
judges whether it fits one campaign, so it is the natural place to also judge **how big** the
work is and **how much a mistake would cost**, and let that pick the profile.

Nobody can see today which profile a campaign runs with or why, and there is no record of how
a spec was sized, so mis-sizing (an `xs` that burned five loops) cannot be studied afterwards.
`IDEA-092` (a flat dataset export for after-the-fact analysis of mis-sizing) is the consumer of
the record this spec writes; it is not designed here.

## Goals / Non-goals

**Goals**
- G1. The `spec_reviewer` verdict gains `size` (`xs|s|m|l|xl`), `tone`
  (`cost-effective|high-end|critical`) and `rationale` (one or two sentences); the review
  record stores them. `xl` means "too big for one campaign": the verdict must be `split`.
- G2. A size×tone → profile matrix with built-in defaults, overridable in config.
- G3. `wt queue add SPEC` picks the profile from the approving review unless `--profile` is
  given; the choice, its source (`review|flag|default`) and a human-readable reason are
  recorded on the campaign state and on the `open` ledger event.
- G4. Size, tone, profile and its source are visible in `wt queue list` (table and
  `--json`), `wt queue watch`, `wt campaign show` and `wt spec review`.
- G5. The record is stable enough for `IDEA-092` to read size, tone, profile and source from
  `state.json` / `ledger.jsonl` without re-running anything.

**Non-goals**
- The `IDEA-092` export itself (columns, file format, joining with outcomes).
- Re-picking the profile mid-campaign, including at [SPEC-0210](./0210-in-campaign-spec-review-and-revision-loop-auto-accept.md)'s
  in-campaign acceptance of an idea-queued spec. Such campaigns keep their add-time profile
  (source `flag` or `default`); the pure `select_profile` defined here is reusable by a
  follow-up.
- Automatic splitting of `xl` specs (the reviewer recommends; a human or SPEC-0210's writer acts).
- Tuning the matrix from evidence (that is what `IDEA-092`'s data is for), new profiles, or
  per-persona overrides keyed by size/tone.
- Changing `wt spec review`'s own profile choice (it keeps `default_profile` / `--profile`).

## Decision

Extend the `spec_reviewer` schema and template with a sizing and tone rubric; copy `size`,
`tone`, `rationale` into the review record (additive under `wt.spec.review.v1`). Add a
built-in `PROFILE_MATRIX` and a pure `select_profile(cfg, *, flag, review)` in `profiles.py`
returning `{profile, source, reason, size, tone}`. `queue.plan_add` calls it with the review
that `spec_review.check_admissible` returns; `apply_add` writes the result to
`state.sizing`, and `store.create_campaign` adds it to the `open` event. The listing,
dashboard, campaign show and spec review renderers display it.

## Design

### Verdict and review record

- `personas.SPEC_REVIEWER_SCHEMA` gains properties `size` (enum `xs, s, m, l, xl`), `tone`
  (enum `cost-effective, high-end, critical`) and `rationale` (string), all **required** for
  new reviews.
- `spec_review._check_verdict` also rejects (ValueError, nothing written) a verdict with
  `size: xl` whose `verdict` is not `split`. SPEC-0188 already has a dedicated `split` verdict
  ("needs edits before admission because it is too big") with `suggested_splits`, which is
  what "revise with a split finding" means here. Using it means no new mechanism: the queue
  gate already refuses it, and SPEC-0210 already parks it as `spec_split`.
- The persona template `personas/spec_reviewer.md` gets a **Size and tone** section:
  - **xs**: at most 3 ACs, one module, no new state or schema.
  - **s**: at most 5 ACs, one or two modules.
  - **m**: at most 8 ACs, a few modules or one new CLI surface.
  - **l**: 9–10 ACs, or a cross-cutting change (state machine, persisted schema, several CLI
    surfaces).
  - **xl**: more than about 10 ACs or several independent subsystems ⇒ verdict `split`.
    This replaces the existing "Size" check bullet, with the same threshold.
  - **critical**: a defect can corrupt persisted state or the main branch, lose data, weaken
    a safety gate (merge, lease, permissions, cost caps), or touch security.
  - **high-end**: core user-facing behaviour or subtle design, where a mediocre
    implementation means costly rework.
  - **cost-effective**: docs, rendering, isolated additive features with straightforward
    tests.
  - `rationale` names the facts that drove both choices (AC count, modules, risk).
- The review payload (sidecar today; SPEC-0210's store when it lands, which keeps the same
  payload) gains top-level `size`, `tone`, `rationale`. The schema name stays
  `wt.spec.review.v1`: the change is additive, and readers treat missing keys as `null`.

### Matrix and selection (`profiles.py`)

The built-in `PROFILE_MATRIX` maps tone → size → profile (`xl` has no entry):

| tone \ size    | xs       | s        | m        | l        |
|----------------|----------|----------|----------|----------|
| cost-effective | frugal   | frugal   | balanced | balanced |
| high-end       | balanced | balanced | balanced | thorough |
| critical       | thorough | thorough | thorough | thorough |

Why this table:
- `frugal` (cap $3, `loop_max` 2, implementer $1.5) fits only small, low-risk work. On an
  `m`/`l` it would hit cap parks, which cost more human attention than the dollars saved, so
  cost-effective `m`/`l` get `balanced`.
- `high-end` work buys the stronger reviewer of `balanced` (opus conformance review) at every
  size. Only `l` needs `thorough`'s larger cap and fourth loop.
- For `critical` work the cost of a defect dominates, so it always gets `thorough`, even at
  `xs`: an opus implementer on a small but dangerous change is cheap insurance.
- No size×tone ever picks a profile that is more lenient than the default for a riskier tone.
  The table is monotone in both axes.

Config override: `agentic.profile_matrix: {<tone>: {<size>: <profile>}}` is deep-merged over
the built-in with `deep_merge`, so users write only the cells they change.
`select_profile` raises ValueError naming the cell when a key is not a known tone or size,
when it names `xl`, or when its profile is not in `known_profiles(cfg)`.

`select_profile(cfg, *, flag=None, review=None) -> dict` is pure (no I/O beyond `cfg`).
Precedence:
1. `flag` given ⇒ `{profile: flag, source: "flag", reason: "--profile <flag>"}`. If the
   review has a size and tone, they are still recorded, so `IDEA-092` can see overrides.
2. `review` is an approving review with a valid `size` (not `xl`) and `tone` ⇒ the matrix
   cell, `source: "review"`, reason `"size m, tone high-end → balanced"`.
3. Otherwise ⇒ `agentic.default_profile`, `source: "default"`. The reason says why: `"no
   approving review (--force)"` or `"review predates sizing (no size/tone)"`. An **old
   sidecar** without size/tone therefore keeps working and queues on the default profile,
   exactly as today. That is why existing tests and approvals need no migration.

### Admission and recording

- `plan_add` keeps the review dict `check_admissible` returns (on `--force` it passes
  `review=None`; a forced add never trusts a stale or non-approving review). It calls
  `select_profile(cfg, flag=profile, review=review)`, resolves the chosen name with
  `resolve_profile` as today, and adds `size`, `tone`, `sizing_rationale`,
  `profile_source`, `profile_reason` to the plan. The dry-run output prints them.
- `apply_add` writes `state.sizing = {size, tone, rationale, source, reason,
  review_sha256}`, where `review_sha256` is the spec hash the review was keyed on, or null.
  `state.profile` remains the single authority for the profile name.
- `store.create_campaign` adds `size`, `tone`, `profile_source`, `profile_reason` to the
  `open` event payload. These keys are **not** added to `schemas.EVENT_KEYS["open"]`: they
  are optional extras, so ledgers written before this spec still validate.
- Campaigns created before this spec have no `sizing`. Every reader treats that as all-null.

### Visibility

- `queue_payload` (`wt.queue.v1`): every item gains `size`, `tone`, `profile_source`
  (null when absent). `profile` already exists. This is additive, so the schema name is
  unchanged.
- `wt queue list` table: new columns `size`, `tone`, `profile`. The profile cell is the name,
  followed by a dim `(flag)` or `(default)` when the source is not `review`. Absent values
  render `-`.
- `wt queue watch` (`dashboard_payload` items carry the same three fields plus `profile`):
  - the active panel's where-line reads `· m/high-end → balanced ·`, with the same source
    marker;
  - the backlog and waiting tables gain a `size` column showing `m/high-end`.
- `wt campaign show`:
  - the header line adds `size m  tone high-end  profile balanced (review)`;
  - a `sizing:` line follows with the reason and the rationale;
  - `--json` already emits the state, which now includes `sizing`.
- `wt spec review`:
  - the text output adds `size`, `tone`, `rationale`, and either `→ profile balanced`
    (what `wt queue add` would pick now under the current config) or `xl: split
    recommended, no profile`;
  - `--json` emits the stored payload plus a non-persisted `profile` key with the same
    would-pick value (null for non-approve). The profile is not stored in the review because
    the matrix is config and may change.

### For IDEA-092

The fields a mis-sizing export needs are in `state.json`:
- `sizing.size`, `sizing.tone`, `sizing.source`, `sizing.reason`, `sizing.rationale`;
- `profile`;
- the outcome fields that already exist: `counters.loops`, `cost.total_usd`, `state`,
  `close`.

The `open` event repeats size, tone and source, so a ledger-only reader gets them too. The
field names above are the contract; renaming one needs a superseding spec.

## Alternatives considered

- **Profiles declare the (size, tone) cells they serve** (as the idea suggested): rejected.
  Overlaps and gaps between profiles would need conflict rules, and a user profile delta
  could silently steal cells. One central matrix is total by construction and one
  config key overrides it.
- **A fourth `split` route for `xl` via `revise` plus a finding** (the idea's wording):
  folded into the existing `split` verdict, which already means exactly that and is already
  routed by the gate and by SPEC-0210.
- **Persisting the selected profile in the review**: rejected. The review is keyed by spec
  hash and must stay valid when the matrix config changes. The profile is picked at add time
  and recorded on the campaign.
- **Bumping to `wt.spec.review.v2` / `wt.queue.v2`**: rejected. All changes are additive, and
  old readers ignore the new keys.

## Acceptance criteria

- [x] The `spec_reviewer` schema requires `size`, `tone`, `rationale` with the enums above,
      and the template carries the rubric. A verdict with `size: xl` and a verdict other than
      `split` raises ValueError and writes no review.
- [x] `wt spec review` (fake runner) stores `size`, `tone`, `rationale` in the
      `wt.spec.review.v1` payload.
- [x] `profiles.PROFILE_MATRIX` equals the table above. `select_profile` applies the
      precedence flag > review > default with the documented `source` and `reason`. A config
      `agentic.profile_matrix` cell overrides one entry. An unknown tone, size or profile, or
      an `xl` cell, raises ValueError naming the cell.
- [x] `wt queue add SPEC` with an approving sized review picks the matrix profile
      (`source: review`). `--profile` wins (`source: flag`, size/tone still recorded). An old
      sidecar without size/tone, or `--force`, yields `default_profile` (`source: default`)
      with the documented reason.
- [x] The created campaign's `state.sizing` has `size, tone, rationale, source, reason,
      review_sha256`. The `open` event carries `size, tone, profile_source, profile_reason`.
      Ledgers without them still pass `schemas.ledger_problems`.
- [x] `wt queue list --json` items carry `size`, `tone`, `profile`, `profile_source`, null
      for a campaign without `sizing`. The `wt queue list` table shows `size`, `tone` and
      `profile` columns with the source marker.
- [x] `wt queue watch` shows size/tone and profile on active panels and a `size` column in
      the backlog and waiting tables.
- [x] `wt campaign show` prints size, tone, profile with source, the reason and the
      rationale. `wt spec review` prints size, tone, rationale and the would-pick profile (or
      the `xl` split notice), and its `--json` adds `profile`.
- [x] Full `uv run pytest` green. `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_personas.py`: schema enums and required keys; the template mentions
    every size and tone.
  - `tests/test_agentic_spec_review.py`, fake runner:
    - sized payload stored;
    - `xl`+`approve` and `xl`+`revise` rejected with no file written;
    - `xl`+`split` accepted;
    - CLI text and `--json` `profile` key, including the `xl` notice.
  - `tests/test_agentic_profiles.py`:
    - every cell of `PROFILE_MATRIX`;
    - flag/review/default precedence and reasons;
    - a config override of one cell;
    - ValueError cases (unknown tone, size or profile, `xl` cell).
  - `tests/test_agentic_queue.py`:
    - `plan_add`/`apply_add` with a sized approving sidecar, with `--profile`, with an old
      sidecar (the existing `write_review` helper shape), and with `--force`;
    - `state.sizing` and the `open` event keys;
    - `queue_payload` fields, including null for a pre-sizing campaign.
  - `tests/test_agentic_logs.py`: an `open` event
    without the new keys still validates.
  - `tests/test_agentic_queue_cli.py`: `wt queue list` table columns and source marker;
    `--json` fields.
  - `tests/test_agentic_dashboard.py`: active-panel line and backlog/waiting `size` column.
  - `tests/test_agentic_campaign_cli.py`: `wt campaign show` sizing lines.
- **Manual verification:**
  1. Run one real `uv run wt spec review <an accepted spec> --json` and record size, tone,
     rationale, the would-pick profile and the cost.
  2. Run `uv run wt queue add <spec> --json` (dry run, no `--write`) and check that
     `profile_source: review` and the reason appear.
  3. Run `uv run wt queue list` against a data dir with one pre-sizing campaign and check
     that the new columns render `-`.
- **Regression guard:** full `uv run pytest`. In particular the existing queue, e2e and
  dashboard tests use old-shape sidecars and must still pass unchanged (they now record
  `source: default`).

### Test record (2026-10-25)

- `uv run pytest -q -x`: 1806 passed, 1 skipped. New/extended tests cover every automated item in
  the plan (personas, spec_review, profiles matrix/precedence/override/errors, queue plan/apply
  and `queue_payload`, logs, queue CLI, dashboard, campaign show).
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: exit 0.
- Manual: `wt queue list` (80 and 140 columns) against the live data dir with pre-sizing
  campaigns: the `size`/`tone`/`profile` cells render `-` / the profile name, and
  `--json` items carry `size`/`tone`/`profile_source` as null.
- Not run: manual step 1 (a real `wt spec review`, a paid model call) and the `wt queue add`
  dry run with a sized sidecar (no accepted spec with a sized review exists yet); both paths are
  covered by the fake-runner and `plan_add` tests.

### As shipped

- At widths under 110 columns `wt queue list` has no room for separate size/tone/profile columns,
  so it adds a dim `m/high-end → balanced (flag)` line under the campaign id instead; the
  columns appear from 110 columns up.
- `profile_matrix(cfg)` in `profiles.py` validates and merges the override; `select_profile`
  calls it. `spec_review.would_pick` feeds the `wt spec review` output.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None required. Existing approvals (no size/tone) keep admitting specs on the default
profile, and existing campaigns render `-` for the new fields. Re-running `wt spec review`
on a spec produces a sized review, and its next `wt queue add` uses the matrix. Users who
want the old behaviour everywhere can set every matrix cell to their default profile, or
pass `--profile`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
