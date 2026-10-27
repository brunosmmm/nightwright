---
id: SPEC-0278
title: "wt spec review --profile, a spec-review default profile, and the profile recorded"
status: accepted
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
- The stored review and the journal `start` record carry `profile` and `model`, and
  `--show` / `wt spec reviews` display the profile.

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
- **`spec_review.run_spec_review`**: `profile=None` now means `review_profile_name(cfg)`. A
  caller that passes a name keeps it, so `review_spec` callers are unchanged. After
  `resolve_profile`, `prof["name"]` is passed on:
  - to `review_payload(..., profile=prof["name"])`, which adds a top-level `"profile"` key
    to `wt.spec.review.v1` (additive; readers use `.get`);
  - to `journal_start(..., profile=..., model=req.model)`, which adds `profile` and `model`
    to the `start` record.
- **`cli.spec_review_cmd`**:
  - gains `@click.option("--profile", default=None, shell_complete=<profile names>,
    help="reviewer profile (default: agentic.spec_review_profile, else
    agentic.default_profile)")`;
  - `--profile` with `--show` is a usage error;
  - the `reviewing_line` output appends `· profile NAME (model)`;
  - the call becomes `run_spec_review(cfg, spec_path, spec_id, origin=origin,
    profile=review_profile_name(cfg, profile))`.
- **Display**: `cli._print_review`, `cli._show_review` and the `wt spec reviews` table
  (`cli.py` `spec_group.command("reviews")`) show `profile` next to `model`. A review stored
  before this change shows `-`.
- The `--force` rule for re-reviews (SPEC-0230) is unchanged: `--profile` does not by itself
  allow a second round.

## Acceptance criteria

- [ ] `wt spec review SPEC-X --profile thorough` runs the reviewer with `thorough`'s
      `spec_reviewer` model, effort and budget.
- [ ] With no flag, `agentic.spec_review_profile` is used when set, else
      `agentic.default_profile`.
- [ ] An unknown `--profile` exits non-zero before any run starts or any journal record is
      written, naming the known profiles.
- [ ] The stored review JSON and the journal `start` record carry `profile` and `model`;
      `--show` and `wt spec reviews` display the profile (`-` for older reviews).
- [ ] `--profile` combined with `--show` is a usage error.

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
- `test_review_payload_and_journal_record_profile`: the stored JSON has `profile`, and the
  journal `start` has `profile` and `model`.
- `test_show_displays_profile_and_legacy_dash`, and
  `test_cli_review_profile_with_show_usage_error`.
- **Sabotage check:** have the CLI drop the flag (call without `profile=`);
  `test_cli_review_profile_flag_sets_model` must fail. Swap the precedence order in
  `review_profile_name`; `test_review_profile_name_precedence` must fail.
- Regression guard: full `uv run pytest`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
