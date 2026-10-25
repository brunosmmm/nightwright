---
id: SPEC-0199
title: "wt campaign runs / run: persona runs without reading run files"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, campaign, observability]
source_idea: IDEA-050
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-050` (filed under the missing-CLI rule). Diagnosing a 429 park needed a Python
filter over `wt campaign log --json` for `run.end` fields, and `cat runs/NNN-*/result.json`.

Since SPEC-0192, every persona run has `runs/<run_id>/run.json` (`wt.run.v1`), beside
`prompt.md`, the raw `result.json` and `stderr.log`. Older runs (SPEC-0179/0180/0187 and the
rest) have only the raw files and the ledger's `run.start` / `run.end`. Run ids are
`NNN-<persona>` (`queue._prepare_run`).

Revised twice on 2026-10-25 after `wt spec review` (`docs/specs/reviews/SPEC-0199.json`,
revise both times).
- **Round 1** added the transient column, defined `verdict_status`, mapped the ledger
  fallback, pinned the RUN and JSON shapes, and added the skill line.
- **Round 2** defines which runs are listed, makes the ledger the source of truth for
  `started_at`/`interrupted` (so both sources agree), aligns `transient` with
  `events.run_failed`, settles the `verdict_status` edge cases, adds `charged_usd`, and pins
  the shapes of the tails.

## Goals / Non-goals

**Goals**
- `wt campaign runs CAMPAIGN [--json]`: one row per persona run, ordered by run_id. Columns:
  run_id, persona, model/effort, started, duration, cost, exit, is_error, timed_out,
  **transient**, verdict status.
- `wt campaign run CAMPAIGN RUN [--json]`: the full record (verdict status and summary,
  must_fix, blockers, error tail, permission denials, stderr tail) and the paths of the
  run's files.
- The same output whether a run has `run.json` or only ledger events.

**Non-goals**
- Re-running or editing runs; a dashboard panel (SPEC-0185 shows the current run).

## Decision

A new `src/wt/agentic/runs.py` holds two pure functions, `campaign_runs(cfg, cid)` and
`campaign_run(cfg, cid, run)`. They list the union of run ids known to the ledger and the
`runs/` directory. The ledger (`run.start` / `run.end`) is authoritative for every field it
carries; `run.json` fills only what the ledger lacks. `cli_campaign.py` renders the records.

## Design

- **Which runs:** the union of (a) `run_id`s of the campaign's `run.start` events and
  (b) the names of directories under `runs/` (files like `.fake-cursor` are ignored), ordered
  by run_id. A run known only from its directory still gets a row.
- **Record (`list` row):** the `wt.run.v1` keys (`campaign, run_id, persona, model, effort,
  max_budget_usd, timeout_s, permission_mode, started_at, duration_s, exit_code, timed_out,
  interrupted, is_error, cost_usd, usage, session_id, verdict_ok, verdict`) plus `charged_usd`
  and `result_tail` (from `run.end`), and:
  - `source`: `ledger+run.json`, `ledger`, `run.json` (directory with `run.json` but no
    ledger events) or `files` (directory with neither);
  - `status`:
    - `running` — a `run.start` with no `run.end` and no `run.json`;
    - `orphan` — a directory with neither ledger events nor `run.json`;
    - `done` — otherwise;
  - `verdict_status`, evaluated in order:
    1. `—` when `verdict` is null;
    2. `invalid` when `verdict` is not a dict, or when `verdict_ok` is false;
    3. otherwise the first of `verdict.status`, `verdict.disposition` and `verdict.verdict`
       that is present (stringified);
    4. `?` when the dict has none of them;
  - `transient`: exactly `events.run_failed`'s rule. When `is_error` or `timed_out`,
    `events._is_transient(result_tail or ("timed out" if timed_out else "error"))`; otherwise
    false.
- **Field precedence:**
  - From `run.start`: `persona, model, effort, max_budget_usd, timeout_s, permission_mode`;
    its `ts` is `started_at`.
  - From the matching `run.end`: `exit_code, is_error, timed_out, interrupted` (the step's
    view, which knows when the loop was stopping), `duration_s, cost_usd, charged_usd, usage,
    session_id, verdict_ok, verdict, result_tail`.
  - `run.json` supplies any of these keys the ledger lacks (a run known only from its
    directory), and its own `started_at` is kept as `runner_started_at`.
  - `campaign` is the campaign id. Keys missing everywhere are null.
- **Detail (`campaign_run`)** adds:
  - `paths`: `{dir, prompt, raw, stderr, run_json}`, absolute, each included only when the
    file exists;
  - `stderr_tail`: a JSON list of the last 40 lines of `stderr.log` (`[]` when absent or
    empty);
  - `permission_denials`: `result.json`'s top-level `permission_denials` when the file
    parses as a JSON object with that key, else `[]` (missing, empty or unparseable files
    included);
  - `must_fix` and `blockers`: from the verdict (`[]` when absent).
- **RUN:** the full id (`001-implementer`), or a prefix that matches exactly one run id
  (`001`). No match → ValueError `no run '…' in <cid> (runs: …)`. More than one match →
  ValueError listing them. An unknown campaign → the store's ValueError (`no campaign …`).
- **CLI** (`cli_campaign.py`, CAMPAIGN completion = `complete_campaign_id`):
  - `runs` is a Rich table (`R.section_table`). The cost column shows `cost_usd`, or
    `charged_usd` followed by `*` when the runner reported no cost. is_error / timed_out are
    red, transient yellow,
    verdict status coloured (done / pass green, blocked / fail / escalate red, invalid and
    `—` dim). An empty campaign prints `no runs yet`.
  - `--json` → `{"schema": "wt.campaign.runs.v1", "campaign": cid, "runs": [records]}`.
  - `run` is a key/value view: verdict status + summary, must_fix / blockers lists, tails,
    paths.
  - `--json` → `{"schema": "wt.campaign.run.v1", "campaign": cid, "run": record}`.
  - `ValueError` → `ClickException`.
- **Skill:** `skills/wt-conduct/SKILL.md`'s quick-reads list adds `wt campaign runs|run` (for
  diagnosing failed runs).

## Alternatives considered

- **Only support `run.json`** — campaigns from before SPEC-0192 would show nothing.
- **Filters on `wt campaign log --type run.end`** — the log is one line per event, and a run
  is two events plus files; joining them is the point of this command.
- **A section in `wt campaign show`** — `show` is the campaign summary; a run listing grows
  with loops and wants its own `--json` shape.

## Acceptance criteria

- [x] On an e2e campaign (implementer + conformance reviewer, FakeRunner), `wt campaign runs
      --json` lists both runs ordered by run_id with every record key above,
      `source: ledger+run.json`, the right `verdict_status` and `transient: false`.
- [x] After deleting the `run.json` files, every key except `source` and `runner_started_at`
      has the same value (`source: ledger`).
- [x] Listing covers each case: a lone `run.start` gives `status: running`; a directory with
      only `prompt.md` gives `status: orphan, source: files`; a directory with `run.json` and
      no ledger events gives `source: run.json`; a ledger run whose directory is gone still
      appears (with no `paths`).
- [x] `transient`: `is_error` with `result_tail` containing `429` → true; `is_error` with
      another tail → false; `timed_out` with an empty tail → false (the `"timed out"` text has
      no transient marker), matching `events.run_failed` for the same event.
- [x] `verdict_status`: null → `—`; a string verdict → `invalid`; `verdict_ok: false` with
      `status: done` → `invalid`; `{disposition: fix}` → `fix`; `{}` → `?`.
- [x] `charged_usd` is in the records, and the human cost column shows `$4.00*` for a run with
      `cost_usd: null, charged_usd: 4.0`.
- [x] `wt campaign run C 001` (unique prefix) and `wt campaign run C 001-implementer` return
      the same record. `--json` has keys `schema, campaign, run`; `run` includes `paths`
      (existing files only), `stderr_tail` as a list (the last 40 lines of a 100-line
      stderr.log), and `permission_denials` (from a raw result.json carrying them, and `[]`
      for an empty or unparseable one), plus `must_fix` and `blockers`.
- [x] Unknown campaign, unknown run and ambiguous prefix each exit non-zero naming the problem.
- [x] The human `runs` and `run` views render (smoke: run ids and verdict status appear), and
      the conduct skill names `wt campaign runs`.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_campaign_runs.py`, using the `test_agentic_e2e`
  harness. One test per AC:
  - e2e then `runs --json`;
  - delete the `run.json` files and compare;
  - craft `run.end` events (via `schemas.with_defaults`) with `429` / other errors / timeout
    for `transient`, compared with `events._project_ledger_event` on the same event;
  - the four listing cases (lone `run.start`, a `prompt.md`-only directory, a `run.json`-only
    directory, a removed directory);
  - the `verdict_status` table;
  - a `charged_usd` run in the human view;
  - write a 100-line `stderr.log` and a `result.json` with `permission_denials`, then
    `run 001` / `run 001-implementer`;
  - error cases;
  - human smoke;
  - grep the skill.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest tests/test_agentic_campaign_runs.py` — 22 passed (e2e `runs --json`; same
  record without `run.json`; the four listing cases; `transient` against
  `events._project_ledger_event`; `verdict_status` table; `$4.00*` in the human view; `run`
  prefix/full id, 100-line stderr tail, `permission_denials`, `paths`; error cases; human smoke;
  conduct-skill grep).
- Full `uv run pytest -q` and `python3 tools/spec_lint.py` (after `--write-ledger`) pass.

### As shipped (2026-10-25)

As designed. Notes: a run directory whose `run.json` does not parse counts as having none
(`source` reflects that); `verdict_ok` only makes a verdict `invalid` when it is exactly
`false`; the detail's `permission_denials` is `[]` when the key is not a list.

## Rollout / migration

Ships through the agentic queue (balanced profile). No data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
