---
id: SPEC-0242
title: "wt spec review --show and wt spec reviews: read stored reviews without re-running"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, specs, review, cli]
source_idea: IDEA-075
depends_on: [SPEC-0210, SPEC-0230]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-075` (a missing-CLI report).

[SPEC-0210](./0210-spec-review-store-in-the-data-dir-any-repo-reviews.md) moved spec reviews to
the data-dir store (`<data>/agentic/reviews/<SPEC-ID>/<sha>.json`), and
[SPEC-0230](./0230-single-spec-review-round.md) made a spec reviewed once: `wt spec review`
refuses a spec that already has a review unless `--force` is given. So the only CLI that
prints a review is the one that runs a new, paid reviewer, and that is refused after the first
round. To relay a verdict, agents read the JSON with Python, which is what the missing-CLI rule
forbids. Nothing lists which specs have a pending `revise` or an unapplied `split`.

`spec_review.py` already has the readers: `load_review`, `list_reviews`, `latest_review`,
`check_admissible` and `would_pick`. The human renderer lives inline in `cli.spec_review_cmd`
(`src/wt/cli.py`).

## Goals / Non-goals

**Goals**
- `wt spec review SPEC-ID --show [--json]` (and `--path FILE --show`) prints the stored review
  without running anything. It shows the verdict, findings, model, cost, `reviewed_at`, the
  reviewed sha, whether that sha still matches the current file, the sizing, the profile it
  would pick, and whether `wt queue add` would admit the spec now.
- `wt spec reviews [--verdict V] [--json]` lists the latest stored review of every spec.

**Non-goals**
- Showing reviews in `wt queue watch` or the listing (SPEC-0211, SPEC-0217).
- Review history browsing beyond the latest review (the `--json` of `--show` carries the
  number of stored reviews; a history view is a follow-up if needed).
- Changing what is stored, or the admission rules.

## Decision

Add `--show` to `wt spec review` and a new `wt spec reviews` command. Both read through new
pure helpers in `spec_review.py` (`show_payload`, `all_latest_reviews`) and share one human
renderer, extracted from `spec_review_cmd` as `_print_review`.

## Design

### Helpers (`src/wt/agentic/spec_review.py`)

- `show_payload(cfg, spec_path, spec_id) -> dict | None`. It returns None when the spec has no
  stored review (`list_reviews(cfg, spec_id, spec_path)` is empty). Otherwise, with
  `current = spec_sha256(spec_path)`:
  - `review` = `load_review(cfg, spec_id, current)` when that exists, else
    `latest_review(cfg, spec_id, spec_path)`;
  - `matches_current` = `review["spec_sha256"] == current`;
  - `admission`: call `check_admissible(cfg, spec_path, spec_id)`; on success
    `{"ok": true, "kind": <its "admission">, "message": null}`, on ValueError
    `{"ok": false, "kind": null, "message": str(e)}`;
  - `profile` = `would_pick(cfg, review)` (None unless the review approves);
  - `stored` = the number of stored reviews of this spec (`len(list_reviews(...))`).
  The payload is `{"schema": "wt.spec.review.show.v1", "spec_id", "spec_path",
  "current_sha", "matches_current", "review", "admission", "profile", "stored"}`.
- `all_latest_reviews(cfg) -> list[dict]`. For every directory under
  `<data>/agentic/reviews/` (sorted by name), with `spec_id` = the directory name:
  - try `SP.resolve_spec_path(cfg, spec_id)`. When it resolves, `review =
    latest_review(cfg, spec_id, path)` and `matches_current = review["spec_sha256"] ==
    spec_sha256(path)`. When it raises ValueError (a `--path` review of a spec from another
    repository, or a deleted spec), `review = latest_review(cfg, spec_id)` and
    `matches_current = None`;
  - a spec id whose directory holds no readable review is skipped.
  Each row is `{spec_id, verdict, size, tone, counts, reviewed_at, spec_sha256,
  matches_current, model, cost_usd}`, where `counts` is `_counts(review["findings"])`.

### CLI (`src/wt/cli.py`)

- `wt spec review`:
  - gains `--show`. With `--show`, the spec is resolved exactly as today
    (`resolve_spec_path_for_cwd` for a SPEC-ID, the file for `--path`), the
    `reviewing_line` is not printed and no reviewer runs. The SPEC-0230 "already reviewed"
    guard does not apply;
  - `--show` together with `--force` is a usage error;
  - no stored review: exit 1 with `<SPEC> has no stored review; run wt spec review <SPEC>`;
  - `--json` prints `show_payload`; otherwise `_print_review(review, profile)`, then one
    status line: `reviewed <sha12> · current|edited since · admissible: approved |
    after_revision | no — <message>`.
- `_print_review(review, pick)`: the existing rendering from `spec_review_cmd` moved verbatim
  into a function: verdict line with model and cost, size/tone/rationale, the `→ profile`
  line, the findings table, the split lines. `spec_review_cmd` calls it after a run, so the
  output of a real review is unchanged.
- `wt spec reviews [--verdict approve|revise|split] [--json]`:
  - rows from `all_latest_reviews`, filtered by `--verdict` when given;
  - table columns: spec, verdict, size, tone, findings (`2 must_fix · 1 major`, the
    non-zero counts in severity order), reviewed (date and time), current (`yes`, `edited`,
    or `?` for None);
  - `--json` prints `{"schema": "wt.spec.reviews.v1", "reviews": [...rows]}`;
  - no stored reviews: an empty table with `no stored spec reviews` (exit 0).

## Alternatives considered

- **A separate `wt spec review-show` command** — `--show` keeps one place for one spec's
  review and reuses its spec resolution and `--path`.
- **Read the run journal (`spec-reviews.jsonl`)** — it holds counts, not findings; the store
  is the source of truth.
- **Only list reviews whose sha matches the current file** — stale reviews are exactly what
  an agent needs to see (a `revise` whose edit is pending); the `current` column shows it.

## Acceptance criteria

- [x] `wt spec review SPEC-X --show` with a stored `revise` prints the verdict, findings table
      and the status line (`current`, `admissible: no — …`), runs no reviewer (the FakeRunner
      script is untouched and the journal gains no record), and works when the spec already
      has a review (no `--force` needed).
- [x] After the spec file is edited, `--show` reports `edited since` and `admissible:
      after_revision`; for a stored `approve` of the current text it reports `admissible:
      approved` and the `→ profile` line.
- [x] `--show --json` emits `wt.spec.review.show.v1` with `current_sha`, `matches_current`,
      `review`, `admission {ok, kind, message}`, `profile` and `stored`; when both an older
      review and one for the current sha exist, `review` is the current-sha one.
- [x] `--show` on a spec with no stored review exits 1 with the `run wt spec review` hint;
      `--show --force` is a usage error; `--path FILE --show` shows the review of that
      file's repository only.
- [x] `wt spec reviews` lists the latest review per spec with verdict, size, tone, counts and
      `current`; `--verdict revise` keeps only revise rows; `--json` emits
      `wt.spec.reviews.v1`; a store entry whose spec id no longer resolves shows `current`
      `?` (`matches_current: null`) instead of failing; an empty store exits 0.
- [x] A real `wt spec review` run prints the same output as before the `_print_review`
      extraction (existing SPEC-0188/0219/0230 CLI tests pass unchanged).
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_spec_review_show.py`, one test per AC, using
  `write_review`-style store fixtures (approve, revise, two shas for one spec, a spec id with
  no spec file) and CliRunner. A FakeRunner whose script would fail if called proves no
  reviewer runs; `read_journal` proves nothing is journaled.
- **Regression guard:** full `uv run pytest`, in particular `tests/test_agentic_spec_review.py`,
  `tests/test_spec_review_store.py` and the SPEC-0230 tests.

### Test record (2026-10-25)

- `uv run pytest tests/test_spec_review_show.py` — 5 tests, one per AC group (show on a stored
  revise with no reviewer run and no journal record; edited/approved status lines and profile;
  `--json` payload preferring the current-sha review; no-review hint, `--show --force`, `--path`
  scope; `wt spec reviews` list, `--verdict`, `--json`, unresolvable id, empty store): pass.
- Review round 2: added tests for skipping empty/corrupt store directories, the findings
  column order and `-`, malformed stored reviews, cwd-worktree comparison, and the empty/filtered
  messages; `tests/test_spec_review_show.py` (11 tests), `tests/test_agentic_spec_review.py` and
  `tests/test_spec_review_store.py` pass (83 passed).
- Regression: `tests/test_agentic_spec_review.py`, `tests/test_spec_review_store.py` pass
  unchanged; full `uv run pytest` and `python3 tools/spec_lint.py` pass (see the run below).

### As shipped (2026-10-25)

As designed. Details: the `reviews` table keeps every column on one line except `tone`, so it
stays readable at 80 columns; `_print_review` reads `findings`, `suggested_splits` and `model`
with `.get` so a stored review missing a field still renders. `wt.spec.review.show.v1` and
`wt.spec.reviews.v1` are listed in `docs/product/guides/agent-surface.md`.

Review fixes: when a store directory's id resolves locally but holds only another repository's
`--path` reviews, `wt spec reviews` falls back to the unfiltered latest review and shows
`current ?` (`matches_current: null`) instead of dropping the row; `_print_review` tolerates a
stored review missing `spec_id`/`verdict` or with a non-numeric `cost_usd`. The `--path FILE
--show` scope test now stores a newer SPEC-0007 review of a different existing repository and
asserts it is not shown.

Second review round: `wt spec reviews` resolves each spec with `resolve_spec_path_for_cwd`, so
inside a linked worktree it compares against that worktree's copy exactly as `--show` does
(elsewhere: the registered checkout). A review whose recorded `repo_root` no longer exists
still counts as the local spec's (a removed campaign worktree is the common case) and shows
`edited`/`yes`, not `?`. The list path tolerates malformed stored reviews (`findings` not a
list of objects, a non-string severity or size: counted as `unknown`/ignored, never a
traceback), and `_print_review` skips non-object findings and splits. An empty store prints
the (empty) table and `no stored spec reviews`; `--verdict V` matching nothing prints `no V
spec reviews`. The table has no box borders and collapsed padding so the findings column
(`1 must_fix · 1 major`) and `current` fit in 80 columns.

## Rollout / migration

Additive CLI. No stored data changes.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
