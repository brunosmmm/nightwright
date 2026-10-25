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

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted from `IDEA-050` (filed under the missing-CLI rule). Diagnosing a park needed a Python filter over `wt campaign log --json` for `run.end` fields, and `cat runs/NNN-*/result.json`. Since SPEC-0192, every persona run has `runs/<run_id>/run.json` (`wt.run.v1`), beside `prompt.md`, the raw `result.json` and `stderr.log`. Older runs have only the raw files and the ledger's `run.start` / `run.end`.

## Goals / Non-goals

**Goals**
- `wt campaign runs CAMPAIGN [--json]`: one row per persona run (run_id, persona, model/effort, started, duration, cost, exit, is_error, timed_out, verdict status).
- `wt campaign run CAMPAIGN RUN_ID [--json]`: the full record (verdict status and summary, must_fix / blockers, error tail, permission denials) and the paths of prompt, raw result and stderr.

**Non-goals**
- Re-running or editing runs.
- A dashboard panel (SPEC-0185 already shows the current run).

## Decision

Read `run.json` when present, else synthesize the same shape from the ledger's `run.start` + `run.end` (marked `source: ledger`), in a pure `campaign_runs(cfg, cid)` function; the CLI renders it.

## Design

- In `wt/agentic/store.py` (or a new `runs.py`): `campaign_runs(cfg, cid) -> list[dict]` (ordered by run_id) and `campaign_run(cfg, cid, run_id) -> dict` (raises ValueError when unknown). Each record has the `wt.run.v1` keys, plus `source` (`run.json` | `ledger`), `verdict_status`, and `paths` {dir, prompt, raw, stderr} (absolute, existing only). `campaign_run` adds `stderr_tail` (last 40 lines), `result_tail` (from `run.end`), and `permission_denials` (from the raw result when present).
- CLI in `cli_campaign.py`: `runs` is a Rich table (`R.section_table`; is_error/timed_out red; verdict status coloured) or `{schema: wt.campaign.runs.v1, campaign, runs}`; `run` is a key/value view with the verdict summary and paths, or `{schema: wt.campaign.run.v1, …}`. Shell completion for CAMPAIGN uses `complete_campaign_id`.

## Alternatives considered

- **Only support `run.json`** — campaigns from before SPEC-0192 (SPEC-0179/0180/0187…) would show nothing.

## Acceptance criteria

- [ ] `wt campaign runs` on a campaign run end-to-end with the FakeRunner lists every run with the fields above; `--json` emits `wt.campaign.runs.v1`.
- [ ] For a campaign whose runs lack `run.json`, the rows come from the ledger (`source: ledger`) with the same fields.
- [ ] `wt campaign run CAMPAIGN RUN_ID` shows the verdict and the paths; an unknown run id is a clean error.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_campaign_runs.py`: e2e via the `test_agentic_e2e` harness (implementer + conformance reviewer) → `runs` and `run` human + `--json`; delete the `run.json` files → same rows, `source: ledger`; unknown run id → exit != 0 with the id in the message.
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
