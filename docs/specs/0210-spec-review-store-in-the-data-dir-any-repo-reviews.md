---
id: SPEC-0210
title: "Spec review store in the data dir, any-repo reviews and a standalone review journal"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, specs, review]
source_idea: IDEA-077
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md).
Promoted from bug `IDEA-077` (spec review sidecars belong in the data dir), and it also fixes
`IDEA-073` (`wt spec review` assumes the Meta-Tools repo). The in-campaign review/revision
loop that builds on it is SPEC-0214.

`spec_review.review_spec` (`src/wt/agentic/spec_review.py`) today:
- it resolves the spec through the configured specs dir;
- it takes the reviewer's root from `P.project_config(cfg, "Meta-Tools")`;
- it runs the reviewer;
- it writes the verdict to `docs/specs/reviews/<SPEC>.json`.

Those files dirtied `main`, so the conductor untracked and gitignored them in `2f82375`. They
are machine-local, and can't exist for a spec that only lives on a branch. `check_admissible`,
the SPEC-0188 queue-add gate, reads that sidecar. Nothing records a review while it runs.

## Goals / Non-goals

**Goals**
- Reviews live in a data-dir store keyed by spec id and spec text sha. Every reader and writer
  uses it.
- `wt spec review` can review any spec file (any repository), with that repository as root.
- Every standalone review run is journaled, start and end, so it can be shown while it runs
  (SPEC-0211).
- Approvals recorded as legacy sidecars keep counting.

**Non-goals**
- The in-campaign loop (SPEC-0214).
- Displaying reviews (SPEC-0211).
- A structured `suggested_fix` (IDEA-076).

## Decision

Restructure `spec_review.py` into small pieces:
- `spec_root`;
- `review_context` (persona context, reused by SPEC-0214);
- `review_payload` (shape + `_check_verdict`);
- the store (`store_path` / `save_review` / `load_review`);
- `journal_start` / `journal_end`;
- `run_spec_review`, a new standalone entry point.
`review_spec` becomes a thin wrapper kept for callers. `check_admissible` reads the store.

## Design

- **`spec_root(cfg, spec_path)`:**
  - **Outbound specs** (the file is under `specs.outbox_dir(cfg)`, ids `<PROJ>-NNNN`) keep
    today's root: the Meta-Tools checkout (`P.project_config(cfg, "Meta-Tools")["_root"]`).
    Its `SCHEMA.md` is the one their review has always used.
  - **Every other spec:** `git -C <spec dir> rev-parse --show-toplevel`, or outside a git
    repository `spec_path.parent.parent.parent` (`<root>/docs/specs/<file>`).
- **`review_context(spec_path, spec_id, *, idea_text)`** returns the persona context dict:
  `spec_id`, `spec_rel_path` (relative to `spec_root`), `spec_text`, `schema_text` (from
  `<root>/docs/specs/SCHEMA.md`, or None) and `idea_text`. It is the same set of keys
  `review_spec` builds today.
- **`review_payload(spec_id, sha, *, model, cost_usd, verdict, root)`** returns the
  `wt.spec.review.v1` dict: `schema, spec_id, verdict, spec_sha256, reviewed_at, model,
  cost_usd, findings, suggested_splits` plus a new `repo_root` field (for traceability, since
  specs from different repositories can share an id). It calls `_check_verdict`, which raises
  ValueError on an approve with a must_fix finding.
- **Store:**
  - `store_path(cfg, spec_id, sha)` is
    `Path(cfg["data_dir"]) / "agentic" / "reviews" / spec_id / f"{sha}.json"`.
  - `save_review(cfg, payload)` writes atomically (temp file + `os.replace`).
  - `load_review(cfg, spec_id, sha) -> dict | None` returns None for a missing, unreadable,
    non-JSON or non-dict file, so a corrupt entry refuses admission instead of crashing
    `wt queue add`.
  - A review of the same spec id and sha from another repository shares the store path; the
    last write wins, which is harmless because the text is identical, and `repo_root`
    records which one it was.
- **`run_spec_review(cfg, spec_path, spec_id, *, profile=None, idea_text=None) -> dict`** is the
  standalone run. In order:
  1. compute the sha and root;
  2. `journal_start`;
  3. build the request as `review_spec` does today (`cwd=root`, `add_dirs=(root,)`, the run
     dir under `<agentic>/spec-reviews/<run_id>`);
  4. run it;
  5. on success, `review_payload` → `save_review`, then `journal_end(verdict=…)`;
  6. on any failure (timeout, runner error, non-conforming verdict, `_check_verdict`), call
     `journal_end(error=…)` and re-raise.
  `idea_text` defaults to the spec's `source_idea`, rendered as today.
  `review_spec(cfg, spec_id, *, profile=None)` = `run_spec_review(cfg,
  SP.resolve_spec_path(cfg, spec_id), spec_id, profile=profile)`.
- **Journal** (`<agentic>/spec-reviews.jsonl`, `wt.spec.review.run.v1`; not the same thing as
  the `<agentic>/spec-reviews/` run-dir directory):
  - `journal_start(cfg, run_id, spec_id, timeout_s, run_dir)` and
    `journal_end(cfg, run_id, spec_id, *, verdict=None, counts=None, cost_usd=None,
    duration_s=None, error=None)` each append one record with a single O_APPEND write.
  - The records are `{schema, kind: start|end, run_id, spec_id, ts, …}`. `run_id` is the run
    dir name, `<spec_id>-<now_iso>-<pid>-<4 random hex>`, unique per run even within one
    timestamp. It pairs the two records.
  - `read_journal(cfg, *, since_ts=None, tail=2000)` reads only the last `tail` lines and
    filters by `ts`.
- **CLI:** `wt spec review [SPEC-ID] [--path FILE] [--json]`. Exactly one of SPEC-ID and
  `--path` is required: both, or neither, is a usage error.
  - SPEC-ID calls `review_spec`.
  - `--path` calls `run_spec_review` on the file, with `spec_id` taken from its front matter.
    A file whose front matter has no `id` is an error, naming the file.
  - `--json` prints the payload (as today, plus `repo_root`).
  - Nothing is written under `docs/specs/`.
- **`check_admissible(cfg, spec_path, spec_id)`** gains `cfg`; its one caller is
  `queue.plan_add` (≈line 1403). With `sha` = the file's current sha:
  - a stored approve for `sha` is admissible;
  - otherwise a legacy `docs/specs/reviews/<SPEC>.json` with `spec_sha256 == sha` and verdict
    approve is admissible;
  - otherwise it raises, with the existing message.
- **`load_review` users:** the old `load_review(spec_path, spec_id)` is renamed
  `load_legacy_sidecar`, used only by the fallback above.
- **Test helper:** `write_review` in `tests/test_agentic_e2e.py` becomes `write_review(cfg,
  spec_path, spec_id, verdict="approve")` and writes the store. Its call sites are updated:
  `test_agentic_e2e.py`, `test_agentic_spec_review.py`, `test_agentic_queue.py`,
  `test_agentic_queue_cli.py`, `test_agentic_waiting.py` and `test_agentic_dashboard.py`
  (12 calls today).

## Alternatives considered

- **Keep sidecars in the repo, untracked** — invisible to branches, lost on clone, and racy
  with closes.
- **Track sidecars in git** — dirtied main and parked closing campaigns (`2f82375`).
- **Key the store by path** — specs move and get renumbered; id + sha is what the gate checks.

## Acceptance criteria

- [x] `wt spec review SPEC-X` (FakeRunner reviewer) writes
      `<data_dir>/agentic/reviews/SPEC-X/<sha>.json`, including `repo_root`, and writes nothing
      under `docs/specs/`.
- [x] `wt queue add SPEC-X`:
  - accepts a stored approving review for the current text;
  - accepts a legacy sidecar whose `spec_sha256` matches;
  - refuses when the stored or legacy review is for an older text;
  - refuses a stored `revise`.
- [x] `wt spec review --path <file in a second git repo>`:
  - runs with that repo as `cwd`;
  - reads that repo's `SCHEMA.md` into the prompt;
  - records `repo_root`.
  Outside a git repo, the root falls back to `parent.parent.parent`.
- [x] Every standalone run appends a `start` and an `end` sharing `run_id`, and two runs of the
      same spec started in the same second get distinct `run_id`s. A run that fails (runner
      error, or an approve carrying must_fix) still appends an `end` with `error` and raises.
      `read_journal(tail=…)` returns only the tail, filtered by `since_ts`.
- [x] `wt spec review` with both SPEC-ID and `--path`, or neither, is a usage error. `--path`
      on a file without `id` errors. A corrupt store file makes `wt queue add` refuse with
      the usual message instead of crashing.
- [x] An outbound spec (`<PROJ>-NNNN` in the outbox) is reviewed with the Meta-Tools root and
      its SCHEMA.md, as before.
- [x] `review_context` returns the same keys `review_spec` used, and `review_payload` rejects
      an approve with must_fix.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_spec_review_store.py`, one test per AC.
  - FakeRunner `spec_reviewer` entries for approve, revise and error.
  - A second scratch git repo with its own `docs/specs/SCHEMA.md` for `--path`.
  - The legacy fallback, via a hand-written sidecar.
  - Journal tail and filtering on a pre-seeded file.
- **Regression guard:** full `uv run pytest`. The SPEC-0188 tests keep passing once
  `write_review` writes the store.

### Test record (2026-10-25)

- `uv run pytest -q` (full suite): 1865 passed, 1 skipped, including the new
  `tests/test_spec_review_store.py` (one test per AC) and the existing SPEC-0188 tests with
  `write_review(cfg, spec_path, spec_id)` writing the store.
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: exit 0.

### As shipped (2026-10-25)

As designed. `review_context` takes an extra optional `root=` keyword (defaults to the
`parent.parent.parent` fallback) so `run_spec_review` can pass the root it already computed.
`journal_end` records `verdict, counts, cost_usd, duration_s, error` on every `end` (null when
unset); `read_journal`'s `since_ts` accepts an ISO string or a datetime and skips torn lines.

## Rollout / migration

Ships through the agentic queue. Existing legacy sidecars keep working through the fallback,
and new reviews go to the store.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
