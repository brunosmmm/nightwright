---
id: SPEC-0199
title: "wt campaign runs / run: persona runs without reading run files"
status: accepted
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

Revised 2026-10-25 after `wt spec review` (`docs/specs/reviews/SPEC-0199.json`, verdict
revise). This version adds the transient column, defines `verdict_status`, maps the ledger
fallback field by field, gives every promised field an AC, pins the RUN_ID and JSON shapes,
and adds the skill line.

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

Two pure functions in a new `src/wt/agentic/runs.py`, `campaign_runs(cfg, cid)` and
`campaign_run(cfg, cid, run)`, build records from `run.json` when present, else from the
ledger. `cli_campaign.py` renders them.

## Design

- **Record (`list` row):** the `wt.run.v1` keys (`campaign, run_id, persona, model, effort,
  max_budget_usd, timeout_s, permission_mode, started_at, duration_s, exit_code, timed_out,
  interrupted, is_error, cost_usd, usage, session_id, verdict_ok, verdict`) plus:
  - `source`: `run.json` | `ledger`;
  - `status`: `running` when there is a `run.start` but neither a `run.end` nor a `run.json`,
    else `done`;
  - `verdict_status`: `verdict.status` → `verdict.disposition` → `verdict.verdict` (the
    `personas.render_previous_verdicts` fallback); `invalid` when a verdict exists but
    `verdict_ok` is false; `—` when there is no verdict;
  - `transient`: `events._is_transient(result_tail or "")` when `is_error` or `timed_out`,
    else false;
  - `result_tail`: from the matching `run.end` (both sources; empty when absent).
- **Ledger fallback:** `run.start` gives `persona, model, effort, max_budget_usd, timeout_s,
  permission_mode`, and its `ts` becomes `started_at`. The matching `run.end` (same `run_id`)
  gives `exit_code, is_error, timed_out, interrupted, duration_s, cost_usd, usage,
  session_id, verdict_ok, verdict, result_tail`. `campaign` is the campaign id. Keys missing
  from both are null.
- **Detail (`campaign_run`)** adds:
  - `paths`: `{dir, prompt, raw, stderr, run_json}`, absolute, each included only when the
    file exists;
  - `stderr_tail`: the last 40 lines of `stderr.log` (empty when absent);
  - `permission_denials`: `result.json`'s top-level `permission_denials` when the file
    parses as JSON with that key, else `[]`;
  - `must_fix` and `blockers`: from the verdict (`[]` when absent).
- **RUN:** the full id (`001-implementer`), or a prefix that matches exactly one run id
  (`001`). No match → ValueError `no run '…' in <cid> (runs: …)`. More than one match →
  ValueError listing them. An unknown campaign → the store's ValueError (`no campaign …`).
- **CLI** (`cli_campaign.py`, CAMPAIGN completion = `complete_campaign_id`):
  - `runs` is a Rich table (`R.section_table`): is_error / timed_out red, transient yellow,
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

## Acceptance criteria

- [ ] On an e2e campaign (implementer + conformance reviewer, FakeRunner), `wt campaign runs
      --json` lists both runs ordered by run_id with every record key above, `source: run.json`,
      the right `verdict_status`, and `transient: false`.
- [ ] After deleting the `run.json` files, the same command yields the same values for every
      shared key, with `source: ledger`.
- [ ] A run with `is_error` and a `result_tail` containing `429` has `transient: true`; one
      with a non-transient error has `transient: false`.
- [ ] A run with `run.start` only shows `status: running`.
- [ ] `wt campaign run C 001` (unique prefix) and `wt campaign run C 001-implementer` return
      the same record. `--json` has keys `schema, campaign, run`; `run` includes `paths`
      (existing files only), `stderr_tail` (the last 40 lines of a 100-line stderr.log),
      `permission_denials` (from a raw result.json carrying them), `must_fix` and `blockers`.
- [ ] Unknown campaign, unknown run and ambiguous prefix each exit non-zero naming the problem.
- [ ] The human `runs` and `run` views render (smoke: run ids and verdict status appear), and
      the conduct skill names `wt campaign runs`.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_campaign_runs.py`, using the `test_agentic_e2e`
  harness. One test per AC:
  - e2e then `runs --json`;
  - delete the `run.json` files and compare;
  - craft `run.end` events (via `schemas.with_defaults`) with `429` / other errors for
    `transient`;
  - append a lone `run.start` for `running`;
  - write a 100-line `stderr.log` and a `result.json` with `permission_denials`, then
    `run 001` / `run 001-implementer`;
  - error cases;
  - human smoke;
  - grep the skill.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Ships through the agentic queue (balanced profile). No data migration.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
