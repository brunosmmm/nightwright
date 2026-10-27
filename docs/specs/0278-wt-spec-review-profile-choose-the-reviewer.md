---
id: SPEC-0278
title: "wt spec review --profile, a spec-review default profile, and the profile recorded"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-097
parent: SPEC-0169
tags: [agentic, review, specs, cli, profiles]
---

## Context

Promoted from idea `IDEA-097`, found on 2026-10-25 under the missing-CLI rule. The user asked
for SPEC-0214 and SPEC-0215 to be reviewed again with an Opus reviewer, and that had to be
done from Python.

Verified on main:
- `spec_review.run_spec_review(cfg, spec_path, spec_id, *, profile=None, ...)` already takes
  a profile name. It resolves it with `profiles.resolve_profile(cfg, profile,
  project_root=root)`, which falls back to `agentic.default_profile`, and reads the
  `spec_reviewer` persona settings (model, effort, budget) from that profile.
  `spec_review.review_spec` passes `profile` through.
- `cli.spec_review_cmd` (`wt spec review`) has no `--profile` option and calls
  `run_spec_review(cfg, spec_path, spec_id, origin=origin)`. Every spec review therefore uses
  the campaign default profile.
- `spec_review.review_payload` records `model` but not the profile, and the journal record
  from `spec_review.journal_start` records neither.

## Goals / Non-goals

**Goals**
- `wt spec review SPEC-X --profile NAME` (also with `--path`) runs the reviewer with NAME's
  `spec_reviewer` settings.
- A config key, `agentic.spec_review_profile`, sets the spec-review default independently of
  `agentic.default_profile`.
- The stored review and the journal `start` record carry `reviewer_profile` and `model`, and
  `--show` / `wt spec reviews` display the reviewer profile.

**Non-goals**
- Choosing the reviewer profile for in-campaign spec reviews (SPEC-0214). Those use the
  campaign's profile.
- Changing how `wt queue add` picks the campaign profile from the review (SPEC-0219,
  `would_pick`).

## Decision

Resolve the profile name in one helper, `spec_review.review_profile_name(cfg, flag) -> str`.
The order is `flag` > `agentic.spec_review_profile` > `agentic.default_profile` >
`"balanced"`. The CLI passes the result to `run_spec_review`. An unknown name fails before
any run starts, with `resolve_profile`'s ValueError, which lists the known profiles.

## Design

- **`src/wt/config.py`**: the `agentic` defaults gain `"spec_review_profile": None`.
- **`spec_review.review_profile_name(cfg, flag=None)`**: a pure function returning the
  resolved name.
- **Key name.** The recorded reviewer profile is `reviewer_profile`, never `profile`: the
  `--json` output of `wt spec review` already sets a top-level `profile` (the campaign profile
  `would_pick` returns, `cli.spec_review_cmd`), and `show_payload` uses top-level `profile` for
  the `would_pick` dict. Both keep their meaning.
- **One resolution.** A new `spec_review.reviewer_run_settings(cfg, spec_path, name) ->
  (prof, settings)` calls `resolve_profile(cfg, review_profile_name(cfg, name),
  project_root=spec_root(cfg, spec_path))` and `PS.persona_settings(prof, PERSONA)`. An
  unknown name raises its `ValueError` (listing the known profiles) here.
- **`spec_review.run_spec_review`** gains `resolved=None`, the `(prof, settings)` pair; when
  absent it calls `reviewer_run_settings(cfg, path, profile)`, so `profile=None` now means
  `review_profile_name(cfg)` and `review_spec` callers are unchanged. `prof["name"]` is passed
  on:
  - to `review_payload(..., reviewer_profile=prof["name"])`, which adds a top-level
    `"reviewer_profile"` key to `wt.spec.review.v1` (additive; readers use `.get`);
  - to `journal_start(..., reviewer_profile=..., model=req.model)`, which adds both keys to
    the `start` record.
- **`cli.spec_review_cmd`**:
  - gains `@click.option("--profile", default=None, shell_complete=C.complete_profile,
    help="reviewer profile (default: agentic.spec_review_profile, else
    agentic.default_profile)")`. `complete_profile` is a new completer in
    `src/wt/completion.py` returning the names from `profiles.load_layers` (builtin, user and
    the cwd project's layers), empty on any error;
  - `--profile` with `--show` is a usage error;
  - before printing `reviewing_line`, it calls `reviewer_run_settings` once (inside the
    existing `try`, so an unknown name exits 1 before anything is journaled), appends
    `· profile NAME (model)` to the line, and passes the pair on:
    `run_spec_review(cfg, spec_path, spec_id, origin=origin, resolved=(prof, settings))`.
    The line and the run therefore come from the same resolution.
- **Display**:
  - `cli._print_review` and `cli._show_review` show `reviewer_profile` next to `model`.
  - `spec_review.all_latest_reviews` rows gain `reviewer_profile` (they already carry
    `model`), so `wt.spec.reviews.v1` JSON gains the key. The `wt spec reviews` table gains
    a `reviewer` column after `verdict`, showing `<reviewer_profile>/<model>`.
  - A review stored before this change shows `-` for the profile.
- The `--force` rule for re-reviews (SPEC-0230) is unchanged: `--profile` does not by itself
  allow a second round.

## Acceptance criteria

- [x] `wt spec review SPEC-X --profile thorough` runs the reviewer with `thorough`'s
      `spec_reviewer` model, effort and budget.
- [x] With no flag, `agentic.spec_review_profile` is used when set, else
      `agentic.default_profile`.
- [x] An unknown `--profile` exits non-zero before any run starts or any journal record is
      written, naming the known profiles.
- [x] The stored review JSON and the journal `start` record carry `reviewer_profile` and
      `model`; `--show`, `wt spec reviews` (table and JSON) display it (`-` for older
      reviews); the `--json` top-level `profile` keeps meaning the queue pick.
- [x] `--profile` combined with `--show` is a usage error.

## Test plan

New tests in `tests/test_agentic_spec_review.py` and `tests/test_spec_review_show.py`, using
the existing fake runner (`select_runner` stub that captures the `RunRequest`):

- `test_review_profile_name_precedence`: flag > `spec_review_profile` > `default_profile` >
  `balanced`.
- `test_cli_review_profile_flag_sets_model`: `--profile thorough` gives a captured
  `req.model` equal to the thorough profile's `spec_reviewer.model`, and `req.profile ==
  "thorough"`.
- `test_cli_review_config_default_profile`: `agentic.spec_review_profile: frugal` without a
  flag gives frugal's model.
- `test_cli_review_unknown_profile_no_journal`: exit code ≠ 0, the message lists the
  profiles, and `spec-reviews.jsonl` is unchanged.
- `test_review_payload_and_journal_record_profile`: the stored JSON has `reviewer_profile`,
  the journal `start` has `reviewer_profile` and `model`, and `wt spec review --json` still
  has the would-pick `profile` alongside `reviewer_profile`.
- `test_reviewing_line_names_profile_and_model`, and `test_spec_reviews_reviewer_column`
  (table column and JSON key).
- `test_show_displays_profile_and_legacy_dash`, and
  `test_cli_review_profile_with_show_usage_error`.
- **Sabotage check:** have the CLI drop the flag (call without `profile=`);
  `test_cli_review_profile_flag_sets_model` must fail. Swap the precedence order in
  `review_profile_name`; `test_review_profile_name_precedence` must fail.
- Regression guard: full `uv run pytest`.

### Test record (2026-10-27)

New tests in `tests/test_agentic_spec_review.py` and `tests/test_spec_review_show.py` (all
named in the plan) pass. Sabotage checks run: the CLI calling without the flag fails
`test_cli_review_profile_flag_sets_model`; a swapped precedence in `review_profile_name` fails
`test_review_profile_name_precedence`. Full `uv run pytest -q -x` and `python3
tools/spec_lint.py` pass.

### As shipped (2026-10-27)

- As designed. `complete_profile` (`src/wt/completion.py`) lists `known_profiles(cfg,
  project_root=cwd)`.
- `test_reviews_list_filter_json_and_unresolvable` now sets the console width to 120: the new
  `reviewer` column pushes the table past 80 columns, where the `current` column was cut.
- `tests/test_spec_review_cwd.py` expects the reviewing line with the appended
  `· profile balanced (sonnet)`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
