---
id: SPEC-0438
title: "conductor-launch / steward-launch --runner cursor|claude"
status: done
owner: user
created: 2026-11-05
updated: 2026-11-05
kind: feature
tags: [agentic, launch, cursor]
depends_on: [SPEC-0437, SPEC-0319]
source_idea: IDEA-1007
---

## Context

[SPEC-0437](./0437-conductor-and-steward-launch-via-cursor-agent.md) can start conductor
and steward on `cursor-agent`, but the only opt-in for that session is
`NW_AGENTIC_RUNNER=cursor` or host/project config. Personas already have
`nw queue add --runner` / `nw queue backend`. The human asked for the same flag on
launch instead of an env var.

## Goals / Non-goals

**Goals**
- `nw queue conductor-launch --runner cursor|claude` and
  `nw queue steward-launch PROJECT --runner cursor|claude`.
- Flag is one-shot for this launch; it does not write `agentic.runner`.
- `--resume` without `--runner` follows the last launch record's `runner` when set.

**Non-goals**
- Changing persona `--runner` / `nw queue backend`.
- A host-config writer. `fake` on the flag.

## Decision

Same shape as `nw queue add --runner`. The flag wins over `resolve_runner` (including
`NW_AGENTIC_RUNNER`). Omit = today's resolution. `--resume` with no flag uses
`last.runner` so a Cursor session can be resumed without the env var. `--resume
--runner X` is refused when the last launch's runner is not `X`.

## Design

`build_plan(..., runner=None)`:

1. If `runner` is `claude` or `cursor`, that is the kind.
2. Else if `resume` and the newest launch record has `runner` in `{claude, cursor}`,
   that is the kind.
3. Else `resolve_runner` as SPEC-0437 (conductor = host; steward = project).

Unknown values error. Click `Choice` matches add: `claude`, `cursor`. Stored on the
session row as today. Dry-run prints `runner:`.

## Alternatives considered

- **Env only** — what shipped in SPEC-0437. Lost: every other backend pin is a flag.
- **Flag writes host config** — too much for a launch; config already reloads.

## Acceptance criteria

- [x] `conductor-launch --runner cursor --dry-run` argv starts with `cursor-agent`
      even when host default is `claude` and `NW_AGENTIC_RUNNER` is unset.
- [x] `conductor-launch --runner claude --dry-run` stays Claude even if
      `NW_AGENTIC_RUNNER=cursor`.
- [x] Omitting `--runner` is unchanged (resolve_runner / env / host / project).
- [x] `--resume` without `--runner` after a Cursor launch record resumes Cursor
      without env or flag.
- [x] `--resume --runner cursor` after a Claude launch record is refused.
- [x] Shipping does not write `agentic.runner: cursor`.

## Test plan

- **Automated tests:** `tests/test_cursor_launch.py` — flag vs env vs default;
  resume from stored runner; mismatch refused.
- **Manual verification:** `nw queue conductor-launch --runner cursor --dry-run`
  shows `runner: cursor` and `grok-4.7-high`.
  Executed 2026-11-05.
- **Regression guard:** existing launch tests; `python3 tools/spec_lint.py`.

## Clock Log

IDEA-1007 (`nw idea clock-in` / `clock-out`).

## Rollout / migration

No migration. Opt in per launch.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; tests pass; manual noted.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.

## Open questions

None.
