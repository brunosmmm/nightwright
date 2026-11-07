---
id: SPEC-0445
title: Persona SDK runs pass a model id and an effort parameter
status: done
owner: user
created: 2026-11-07
tags: [agentic, cursor]
depends_on: [SPEC-0442]
---

## Context

SPEC-0442 sends `request.json`'s `model` straight to `Agent.create`. That string is
the cursor-agent CLI id from the Cursor map, for example `grok-4.7-high`. The SDK
rejects it. The account's model list has `grok-4.7` and `gpt-5.6-sol`, and effort
is a parameter on the model: `reasoning_effort` for `grok-4.7`, `reasoning` for
`gpt-5.6-sol`. IDEA-967's spec writer died in about two seconds on
`Cannot use this model: grok-4.7-high`.

Profiles stay `opus` / `sonnet` / `haiku` plus an effort. The map already turns
that into a CLI id. Conductor and steward launches still use that CLI id with
`cursor-agent`.

## Goals / Non-goals

**Goals**
- A persona SDK run sends a model id the SDK accepts, with the mapped effort as
  that model's parameter.
- `sonnet` / `high` sends `grok-4.7` and `reasoning_effort=high`.
- `opus` / `xhigh` sends `gpt-5.6-sol` and `reasoning=xhigh`.
- Conductor and steward launches keep the CLI id.

**Non-goals**
- Rewriting profile personas to Cursor model ids.
- Changing the parameter names on models other than `grok-4.7` and `gpt-5.6-sol`.
  An unknown id is passed through with no parameters.
- Resuming parked campaigns from this spec.

## Decision

Keep the map's string ids for `cursor-agent`. At the SDK boundary, split a
trailing `-low`, `-medium`, `-high`, `-xhigh`, or `-max` off `grok-4.7` or
`gpt-5.6-sol` and pass `{id, params}` to `Agent.create`. The effort in the suffix
is the one the map chose, which is not always the profile's effort (haiku high
is `grok-4.7-medium`).

## Design

`cursor_map.sdk_selection` does the split. `xhigh` is matched before `high`.
`cursor_exec.execute` stores that selection on the options it passes to
`Agent.create`. `request.json` still records the CLI id.

## Alternatives considered

- **Put `{id, params}` in the map and drop the CLI id** — conductor launch still
  needs `grok-4.7-high` for `cursor-agent --model`.
- **Pass the profile's effort field instead of the suffix** — haiku high would
  send `reasoning_effort=high` while the map says medium.

## Acceptance criteria

- [x] `sdk_selection("grok-4.7-high")` is `{id: grok-4.7, params: [{id: reasoning_effort, value: high}]}`.
- [x] `sdk_selection("gpt-5.6-sol-xhigh")` is `{id: gpt-5.6-sol, params: [{id: reasoning, value: xhigh}]}`.
- [x] `sdk_selection("composer-2.5")` is `{id: composer-2.5, params: []}`.
- [x] A persona exec started from `request.json` model `grok-4.7-high` calls the
      agent with the `grok-4.7` selection.
- [x] `map_launch_model` still returns `grok-4.7-high` for `claude-sonnet-5-5`.

## Test plan

- [x] `tests/test_cursor_map.py` covers the three selections and the launch id.
- [x] `tests/test_cursor_sdk_exec.py` asserts the options model is the selection.
- [x] `uv run pytest -q tests/test_cursor_map.py tests/test_cursor_sdk_exec.py tests/test_cursor_launch.py tests/test_cursor_runner.py` — 55 passed.

## As shipped

`cursor_map.sdk_selection` splits the CLI id. `cursor_exec.execute` passes that
selection to `Agent.create`. Profile YAML is unchanged. Conductor and steward
launch argv is unchanged. 55 tests passed on 2026-11-07.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions in the Cursor launch and runner tests.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
