# Agentic loop

A deterministic queue moves **accepted specs** through implementation, gates, triage and
merge without a human at the keyboard. Judgment steps (implement, triage a gate failure)
run as fresh headless Claude personas with a budget and a timeout; every transition, run,
gate result, finding and dollar lands in a per-campaign ledger. Humans see an item only
when the loop has **parked** it. Governing epic: SPEC-0169; the sections below are filled
in by its child specs as they land.

## Project configuration

`<repo>/.wt/project.yaml` declares the project's gates, merge policy and worktree setup
(SPEC-0170). `wt projects config NAME` shows the resolved file; `wt projects init NAME`
writes a starter (dry-run by default).

## Campaigns and the ledger

A campaign is one spec's trip through the loop: `state.json` plus an append-only
`ledger.jsonl` under the wt data dir (SPEC-0171). `wt campaign list|show|log|note`.

## Profiles

Models, effort, budgets, loop limits and slots live under `agentic:` in `config.yaml`;
builtin profiles `balanced`, `frugal`, `thorough` (SPEC-0171).

## Gates

Project commands (tests, lint) run in the campaign worktree with a timeout; `required`
gates block, optional ones only report (SPEC-0173). `wt queue gates CAMPAIGN` runs them on
demand.

## Personas

`claude -p` with a structured verdict, a dollar budget and a wall-clock timeout; a
scripted fake runner for tests (SPEC-0174).

## Closing

Rebase, re-run every gate on the rebased tree, audit the spec's Definition of Done, then
fast-forward the base branch or open a PR (SPEC-0175).

## Queue

`wt queue add|list|watch|tick|run|park|resume|abandon` and the state machine
(SPEC-0176).

## Conductor

`/wt-conduct` starts the loop, watches it and unparks items (SPEC-0177).
