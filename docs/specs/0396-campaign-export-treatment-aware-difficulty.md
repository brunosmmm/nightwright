---
id: SPEC-0396
title: "Campaign export: treatment-aware difficulty metrics (per-reviewer-run findings, first-round blockers, active time net of close)"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
kind: feature
tags: [agentic, campaign, export, analytics]
source_idea: IDEA-422
depends_on: [SPEC-0236, SPEC-0303]
---

## Context

`nw campaign export` ([SPEC-0236](./0236-wt-campaign-export-dataset-for-mis-sizing-analysis.md),
`src/nightwright/agentic/campaign_export.py`) writes the `campaigns`, `runs` and `findings`
tables under schema `nw.campaign.export.v1`. Its difficulty-related columns are raw counts and raw
wall time, and both are confounded by the **process treatment** the campaign was run under, not
only by how hard the spec was:

- `must_fix_count` and `finding_count` (`campaign_export.py`, `campaign_row`) count every
  `finding` event of every persona. A `thorough` profile runs more reviewer personas, and more
  rounds, so it reports more findings for the same spec. Comparing a `frugal` campaign with a
  `thorough` one by these columns measures the profile, not the work.
- `must_fix_count` and `finding_count` also mix in findings that say nothing about spec
  difficulty: the `close` persona's rebase and cleanup findings (SPEC-0303), implementer
  blockers and gate findings.
- `wall_s` includes every `PARKED` interval. Rebase-conflict parks alone were seen on 34
  campaigns; they measure queue and human latency, not the task.
- `active_s` already leaves out `PARKED`, `WAITING` and `QUEUED` (it sums `time_in_state_s` over
  `queue.ACTIVE_STATES`), but it still includes the whole `CLOSING` state: the rebase, the
  conflict resolver runs (SPEC-0303), post-merge commands and the merge itself. That time is
  also close-mechanics, not task effort, and it varies with how busy `main` was.

The analysis the export exists for (is a campaign hard, was it sized right) needs metrics that
are normalized for the treatment. Everything required is already on the ledger: `run.end`
events carry `persona` and `verdict_ok`, `finding` events carry `persona` and `severity`, and
`transition` events carry `reason`. This is a small, non-MVP analytics improvement.

## Goals / Non-goals

**Goals**
- G1. Per-reviewer-run normalization: the campaign row exposes the number of completed reviewer
  runs and the reviewers' findings and blocking findings per such run.
- G2. First-review-round difficulty: the number of `must_fix` and of `major` findings the
  reviewers raised in the first review round, before any review-driven fix loop. It does not
  depend on how many fix rounds the profile allows; it still scales with the number of reviewer
  personas in the first round, so compare it within the same reviewer set or alongside
  `findings_per_reviewer_run`.
- G3. Active time net of close mechanics: a column with the time spent in task-delivery states,
  excluding `PARKED` (as `active_s` already does) and the `CLOSING` state (rebase, resolver,
  merge).
- G4. All new columns are appended to the `campaigns` table under `nw.campaign.export.v1`, with
  documented derivations, and no existing column, signal or threshold changes meaning.

**Non-goals**
- Changing or removing any existing column (`must_fix_count`, `finding_count`, `wall_s`,
  `active_s`, ...) or the `signals` rules, including `long_for_size`, which keeps reading
  `active_s`. Re-tuning signals on the new metrics is follow-up work once the data exists.
- A composite "difficulty score" or any analysis, chart or notebook. The dataset is the product
  (as in SPEC-0236).
- Counting reviewers declared by users or projects (SPEC-0371). Only the builtin reviewer
  personas (`personas.REVIEWER_PERSONAS`) are recognized; a declared reviewer's runs and
  findings are not in the new columns. The manifest note says so.
- New `runs` or `findings` table columns, a schema bump to `v2`, or a new CLI flag.
- Normalizing cost (`cost_usd`) or token counts per reviewer run.

## Decision

Append nine columns to `CAMPAIGN_COLUMNS` and derive them in `campaign_row` from the ledger events
already in hand: a reviewer-run count and reviewer-only finding counts with their per-run ratios,
the first-review-round `must_fix` and `major` counts, and the active time net of `CLOSING`.
Reviewer-ness is decided by persona name against the builtin `REVIEWER_PERSONAS`. The "first
review round" ends at the first `transition` to `IMPLEMENTING` after the campaign first enters
`REVIEWING`, whatever its reason (`review_fix`, or the resume after a `loop_exhausted` park). Ratios
are rounded to 3 places and are null when the denominator is zero. The existing columns stay as
they are, so every earlier consumer of the v1 export keeps working, and the manifest gains one
note describing the new group.

## Design

### New `campaigns` columns (appended after `resolver_overcap_usd`, in this order)

| column | type | meaning |
|---|---|---|
| `reviewer_runs` | int | count of `run.end` events whose `persona` is in `personas.REVIEWER_PERSONAS` with `verdict_ok is True` and not `interrupted` |
| `reviewer_findings` | int | count of `finding` events whose `persona` is a reviewer persona and whose `severity` is not `info` |
| `reviewer_blocking` | int | the subset of those with `severity` in `must_fix`, `major` |
| `findings_per_reviewer_run` | float | `reviewer_findings / reviewer_runs`, 3 places, null when `reviewer_runs` is 0 |
| `blocking_per_reviewer_run` | float | `reviewer_blocking / reviewer_runs`, 3 places, null when `reviewer_runs` is 0 |
| `first_review_must_fix` | int | reviewer `must_fix` findings in the first review round (below) |
| `first_review_major` | int | reviewer `major` findings in the first review round |
| `close_s` | int | `time_in_state_s["CLOSING"]`, 0 when the campaign never closed |
| `task_active_s` | int | `active_s - close_s`: seconds in the active states other than `CLOSING` |

Only the "blocking" definition `{must_fix, major}` is used, matching `divergence.BLOCKING`
(`divergence.py`), so the export and the fix-loop convergence check agree on what blocks.

### Derivations (pure, over the `events` list `campaign_row` already receives)

- **Reviewer personas:** `frozenset(personas.REVIEWER_PERSONAS)` (builtin: the adversarial,
  conformance and test reviewers). Imported lazily or at module top, whichever avoids a cycle
  (`campaign_export` already imports `queue` lazily for `ACTIVE_STATES`).
- **`reviewer_runs`:** counted from `run.end`, not `run.start`, so a reviewer run that was
  interrupted, timed out or returned a malformed verdict (`verdict_ok` false or missing) is not
  in the denominator: it could not have raised findings, and counting it would deflate the
  ratio for exactly the campaigns that ran under pressure.
- **First review round:** walk `events` in ledger order. Reviewer findings count toward the first
  round until the first `transition` to `IMPLEMENTING` that comes after the campaign's first
  entry into `REVIEWING`, whatever its reason (`review_fix`, or the resume after a
  `loop_exhausted` park). Transitions to `IMPLEMENTING` before the first `REVIEWING` entry (the
  initial implement, `triage_fix` gate loops) do not end the round, and neither does a
  park/resume back into `REVIEWING` (`review_escalated`, `cost_cap`). Findings after the ending
  transition belong to later rounds and are excluded. Rationale: `decide_review`
  (`queue.py:707-713`) runs a fix loop as `review_fix` only while `loops <= loop_max`; past that
  it parks with `loop_exhausted` and `resume_to="IMPLEMENTING"`, so keying on the reason alone
  would count the re-review of a low-loop (frugal) profile as first round, which is the
  treatment bias this spec removes. A campaign that never returns to `IMPLEMENTING` after
  `REVIEWING` (the first round was clean enough, or the campaign ended earlier) counts all its
  reviewer findings. A campaign with no reviewer findings, or
  one that never reached review, has both columns 0 (not null), because the counts are
  well-defined and zero.
- **`close_s` / `task_active_s`:** read the already computed `time_in_state_s` map. `CLOSING`
  covers the rebase, the `conflict_resolver` runs and the merge (SPEC-0303), so subtracting it
  removes the close-time variability the idea calls "rebase" time. `PARKED` is already excluded
  from `active_s`, so a rebase-conflict park contributes to neither column. `task_active_s`
  is clamped at 0.
- The new keys are produced in `campaign_row`'s row dict, so `_project` coerces them to their
  declared types and `signals` is unchanged (it reads the coerced row but none of the new keys).
- Severity values arrive from LLM verdicts, so a non-string or unknown severity matches
  nothing and is simply not counted in `first_review_*` / `reviewer_blocking`; it still counts
  in `reviewer_findings` unless it is `info` (the same rule `finding_count` uses).

### Manifest note

Append one entry to `NOTES` explaining: the per-reviewer-run and first-review-round columns are
for comparing campaigns across profiles; `first_review_*` does not depend on how many fix rounds
the profile allows but still scales with the number of reviewer personas in the first round, so
compare it within the same reviewer set or alongside `findings_per_reviewer_run`; only builtin reviewer personas are counted; a reviewer
run counts only with a valid verdict; `task_active_s` excludes `PARKED` and `CLOSING`, whereas
`active_s` excludes only `PARKED`. The manifest `tables.campaigns.columns` listing already
derives from `CAMPAIGN_COLUMNS`, so it picks the new columns up with no further change.

### Compatibility

`nw.campaign.export.v1` allows appending columns, so `EXPORT_SCHEMA` stays. JSONL/CSV/Parquet
rendering iterate `CAMPAIGN_COLUMNS`, so all three formats carry the new columns. Older
campaigns whose ledgers predate `verdict_ok` on `run.end` get `reviewer_runs = 0` and null
ratios; their counts of findings are still exact.

## Alternatives considered

- **Normalize inside `must_fix_count` / `finding_count`.** Silently changing the meaning of a v1
  column breaks earlier exports; v1 only permits appending. Rejected.
- **Divide by `run.start` reviewer count.** Simpler, but failed or interrupted reviewer runs
  inflate the denominator. Rejected in favour of valid-verdict `run.end` events.
- **Define the first round by the `last_review` state or `review_score` history.** Those are
  overwritten by later rounds; the transitions on the ledger are the durable record.
- **Subtract only `rebase_conflict` park time from `wall_s`.** `active_s` already excludes all
  parks, and rebase work that is not parked (resolver runs, regenerated files) happens in
  `CLOSING`; excluding `CLOSING` covers both without special-casing park reasons.
- **Add `severity`-weighted difficulty score.** An opinionated composite belongs to the analysis
  layer; the export stays a flat, honest dataset.

## Acceptance criteria

- [x] `CAMPAIGN_COLUMNS` ends with the nine new columns in the order and types of the Design
      table, and the first 48 earlier columns (through `resolver_overcap_usd`) are unchanged in
      name, type and order. `EXPORT_SCHEMA` is still `nw.campaign.export.v1`.
- [x] `reviewer_runs` counts only `run.end` events of builtin reviewer personas with
      `verdict_ok` true and not interrupted; runs of other personas, interrupted runs and
      malformed-verdict reviewer runs are not counted.
- [x] `reviewer_findings` and `reviewer_blocking` count only reviewer-persona `finding` events
      (non-`info`, and `must_fix`/`major` respectively); findings from `close`, `implementer`,
      gate and triager personas are excluded.
- [x] `findings_per_reviewer_run` and `blocking_per_reviewer_run` equal the counts divided by
      `reviewer_runs` rounded to 3 places, and are null when `reviewer_runs` is 0.
- [x] `first_review_must_fix` and `first_review_major` count reviewer findings that precede the
      first `IMPLEMENTING` transition after the first `REVIEWING` entry, whatever its reason
      (`review_fix`, or the resume after a `loop_exhausted` park); findings after it are
      excluded; with no such transition all reviewer findings count; `IMPLEMENTING` transitions
      before the first `REVIEWING` entry (initial implement, `triage_fix`) and park/resume back
      into `REVIEWING` do not end the round.
- [x] `close_s` equals `time_in_state_s["CLOSING"]` (0 when absent) and `task_active_s ==
      active_s - close_s`, never negative; a campaign parked for `rebase_conflict` has the park
      interval in neither.
- [x] Campaign B equals campaign A plus one more review round (an `IMPLEMENTING`/`review_fix`
      transition followed by two more valid reviewer runs with findings): both have equal
      `first_review_must_fix` / `first_review_major`, and B has a larger `reviewer_runs` and
      `reviewer_findings` (the fix-round-independence property).
- [x] The existing `must_fix_count`, `finding_count`, `wall_s`, `active_s`, `signals` and all
      other pre-existing columns have identical values for every fixture campaign before and
      after the change.
- [x] The new columns appear, with the declared types, in the JSONL, CSV and Parquet outputs and
      in `manifest.json` `tables.campaigns.columns`, and `manifest.json` `notes` contains the new
      note.

## Test plan

- **Automated tests** (`tests/test_agentic_campaign_export.py`, using the existing `make`,
  `tr`, `run_start`, `row_of` helpers; ledger fixtures written with explicit timestamps):
  - `test_column_types_pinned` and the column-order tests are extended with the nine new
    `(name, type)` pairs; a new assertion pins that the first 48 columns are unchanged.
  - `test_reviewer_run_counts`: a ledger with reviewer `run.end` events (valid, interrupted,
    `verdict_ok: false`) plus an implementer `run.end`; asserts `reviewer_runs` counts only the
    valid reviewer runs.
  - `test_reviewer_findings_exclude_other_personas`: `finding` events from reviewer, `close`,
    `implementer` and `gate_triager` personas, including an `info` reviewer finding; asserts
    `reviewer_findings`, `reviewer_blocking` and that `finding_count`/`must_fix_count` keep their
    old values.
  - `test_per_run_ratios_and_null_denominator`: ratios to 3 places; a campaign with zero reviewer
    runs gets null ratios and 0 counts.
  - `test_first_review_round_boundary`: reviewer findings before and after an
    `IMPLEMENTING`/`review_fix` transition that follows `REVIEWING`; asserts only the earlier
    ones count; a variant with a prior `triage_fix` transition (before the first `REVIEWING`
    entry) asserts the round is not ended by it; a variant with a park/resume back into
    `REVIEWING` (`review_escalated`) asserts the round is not ended; a `loop_exhausted` variant:
    `REVIEWING` -> `PARKED` (`loop_exhausted`) -> `IMPLEMENTING` (resume reason) -> `REVIEWING`
    with later findings, asserting the later findings are excluded; a variant with no return to
    `IMPLEMENTING` counts all.
  - `test_treatment_independence`: campaign A has one round of reviewer runs and findings;
    campaign B is A plus an `IMPLEMENTING`/`review_fix` transition and two more valid reviewer
    runs with findings. Asserts A and B have equal `first_review_must_fix` /
    `first_review_major`, and B has a larger `reviewer_runs` and `reviewer_findings`. The
    per-run ratios are not asserted equal.
  - `test_task_active_excludes_closing_and_parked`: transitions through `IMPLEMENTING`,
    `CLOSING`, `PARKED` (`rebase_conflict`), `CLOSING`; asserts `close_s`, `task_active_s ==
    active_s - close_s`, and that the park interval is in neither; a never-closed campaign has
    `close_s == 0`.
  - `test_unknown_severity_not_counted_as_blocking`: a non-string and an unknown severity are not
    in `reviewer_blocking` / `first_review_*`.
  - Format tests: the existing JSONL, CSV and Parquet round-trip tests (Parquet skipped without
    `pyarrow`) assert the new columns and types; the manifest test asserts the new note.
  - `test_existing_columns_unchanged`: for the existing fixture campaigns, every pre-existing
    column value equals the value pinned before this change (reuse the existing row assertions).
- **Manual verification:** `uv run nw campaign export --out /tmp/nw-export-0396 --format csv`
  (see `nw campaign export --help` for the exact flags), then `head -1
  /tmp/nw-export-0396/campaigns.csv` shows the nine new header names last, and a spot check of
  one real merged campaign compares `first_review_must_fix` / `first_review_major` with its
  `nw campaign` ledger's first reviewer findings.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_agentic_campaign_export.py` and `tests/test_rich_output_guard.py`; the unchanged
  pre-existing-column assertions above; `python3 tools/spec_lint.py` exits 0.

### Test record (2026-11-02)

- `uv run pytest -q tests/test_agentic_campaign_export.py`: 78 passed, 2 skipped (pyarrow
  Parquet tests skipped when absent). New tests: `test_reviewer_run_counts`,
  `test_reviewer_findings_exclude_other_personas`, `test_per_run_ratios_and_null_denominator`,
  `test_first_review_round_boundary` (incl. triage_fix, review_escalated, loop_exhausted and
  no-return variants), `test_treatment_independence`,
  `test_task_active_excludes_closing_and_parked`, `test_unknown_severity_not_counted_as_blocking`
  (via `campaign_row` on raw events), `test_existing_columns_unchanged`; column order/type pins,
  CSV, Parquet and manifest-note assertions extended.
- `tests/test_rich_output_guard.py` fails identically on `main` (unrelated: cli_worker /
  cli_workers / pull_worker raw writes); not touched here.
- Manual: `uv run nw campaign export --out /tmp/nw-export-0396 --format csv` (213 campaigns)
  lists the nine new headers last; for SPEC-0207-20261025-87ec the single round-1 reviewer
  `major` (findings seq 23) is `first_review_major == 1`, later rounds excluded.
- The full suite was left to gating.

### As shipped

- `test_export_conductor_columns_appended` located the conductor columns by negative index
  (`[-6:-4]`); re-anchored to `[-15:-13]` since nine columns now follow them. No behavior change.
- `BLOCKING` is a tuple in `campaign_export.py` (not a frozenset) so a non-hashable LLM severity
  cannot raise on membership.
- A reviewer `finding` counts toward `reviewer_findings`, `reviewer_blocking` and `first_review_*`
  only when the latest `run.end` of its persona was not discarded (`verdict_ok is False` or
  `interrupted`). `queue._record_run` writes a run's findings after its `run.end` even for a
  non-conforming or interrupted run, and the retry repeats them, so counting both double-counted.
  A reviewer `run.end` with `verdict_ok` missing (older ledgers) is not a run in the denominator
  but its findings still count, as the Compatibility section says. `must_fix_count` and
  `finding_count` are unchanged. Tests: `test_findings_of_discarded_reviewer_runs_not_counted`,
  `test_reviewer_run_end_without_verdict_ok_not_counted`.

## Rollout / migration

1. Land the columns, derivations, manifest note and tests together; there is no data migration
   because every export is a full snapshot derived from existing ledgers.
2. Consumers of the v1 export that select columns by name are unaffected; consumers that
   read positionally still see the first 48 columns in place.
3. Follow-up (separate spec, only once the data is collected): tune `signals` or add
   size-relative thresholds from these normalized metrics.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. Decided: only builtin reviewer personas are counted (declared reviewers are a
  non-goal, noted in the manifest), and "blocking" means `must_fix` plus `major`.
