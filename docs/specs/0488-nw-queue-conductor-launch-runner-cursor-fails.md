---
id: SPEC-0488
title: Cursor launch passes a flat model id
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
kind: feature
tags: [agentic, launch, cursor]
depends_on: [SPEC-0460]
source_idea: IDEA-1041
---

## Context

`nw queue conductor-launch --runner cursor` builds a `cursor-agent` argv in
`build_plan` (`src/nightwright/agentic/launch.py`). The `--model` value comes from
`launch_model_arg` (`src/nightwright/agentic/cursor_map.py`). [SPEC-0460](./0460-cursor-spawns-force-fast-off.md)
formats the SDK selection as a bracket string, so the default sonnet/high mapping
`grok-4.7-high` is passed as `grok-4.7[reasoning_effort=high,fast=false]`.

This `cursor-agent` rejects that string with `Cannot use this model`. The ids it
lists are flat: `grok-4.7-high`, and the fast variant is that id plus a `-fast`
suffix (`grok-4.7-high-fast`). The same argv is used for `steward-launch` when the
runner is cursor. Persona SDK runs go through `sdk_selection` and `Agent.create`,
which accept `{id, params}`; they are not the failing path.

## Goals / Non-goals

**Goals**

- A cursor conductor or steward launch passes a flat `--model` id this agent accepts.
- The default launch id is `grok-4.7-high`.
- `fast=false` stays off the id (no `-fast` suffix). `fast=true` is the `-fast` suffix.
- Persona SDK selections keep `reasoning_effort` / `reasoning` / `effort` and `fast=false`.

**Non-goals**

- Querying `cursor-agent` for its model list at launch.
- A config key that turns fast on, or any change to context, thinking, or cyber.
- Changing `cursor_map.yaml` alias targets or the dashed id stored on a persona request.
- Changing Claude launch argv.

## Decision

`launch_model_arg` emits the flat cursor-agent id `model[-effort][-fast]`. The effort
word is the selection's effort-parameter value. `-fast` is appended only when that
selection's `fast` value is `true`. Production selections still send `fast=false`
([SPEC-0460](./0460-cursor-spawns-force-fast-off.md)), so Grok and Composer launches
carry no `-fast` suffix and no brackets. The map's flat id is the launch argument;
launch does not shell out to validate it.

## Design

`launch_model_arg` still starts from `sdk_selection(model_id, catalog)`. It then
formats that selection:

1. Start with the selection `id`.
2. If a parameter named `reasoning_effort`, `reasoning`, or `effort` is present,
   append `-` and that parameter's value (`high`, `xhigh`, `extra-high`, `max`, and
   the other catalog values).
3. If a parameter named `fast` is present and its value is `true`, append `-fast`.
   A value of `false`, or no `fast` parameter, appends nothing.
4. Any other parameter is left off the CLI id. Today the only parameters are effort
   and `fast`.
5. A selection with no parameters returns the selection `id` unchanged, which is the
   original string when the id matches no catalog entry.

Worked results with the checked-in catalog:

| Request id | CLI `--model` |
|---|---|
| `grok-4.7-high` | `grok-4.7-high` |
| `grok-4.6-low` | `grok-4.6-low` |
| `composer-2.5` | `composer-2.5` |
| `gpt-5.6-sol-xhigh` | `gpt-5.6-sol-xhigh` |
| `gpt-5.4-xhigh` | `gpt-5.4-extra-high` |
| `claude-sonnet-4-6-xhigh` | `claude-sonnet-4-6-max` |
| `not-a-catalog-model-high` | `not-a-catalog-model-high` |

A selection `{id: grok-4.7, params: [reasoning_effort=high, fast=true]}` formats as
`grok-4.7-high-fast`. `sdk_selection` does not produce `fast=true`; the formatter
still honors it so a later change cannot fall back to brackets.

`build_plan` is unchanged aside from the string `launch_model_arg` returns. The
default conductor and steward model `claude-sonnet-5-5` still maps through
`map_launch_model` to `grok-4.7-high`, and that string is the `--model` argument.
`sdk_selection` and `cursor_exec` keep sending `{id, params}` with `fast=false`
on Grok and Composer.

## Alternatives considered

- **Keep the bracket form.** This agent answers `Cannot use this model` for
  `grok-4.7[reasoning_effort=high,fast=false]`.
- **Probe the agent model list at launch and pick a listed id.** The map already
  stores the flat ids the agent lists. A launch would then depend on the binary,
  auth, and a second process.
- **Return the request string unchanged whenever `fast` is false.** That leaves
  `gpt-5.4-xhigh` and `claude-sonnet-4-6-xhigh` as words the catalog does not use
  (`extra-high` and `max`). The CLI id uses the same effort value the SDK selection
  already resolved.

## Acceptance criteria

- [x] `launch_model_arg("grok-4.7-high")` is `grok-4.7-high`.
- [x] `launch_model_arg("grok-4.6-low")` is `grok-4.6-low`.
- [x] `launch_model_arg("composer-2.5")` is `composer-2.5`.
- [x] `launch_model_arg("gpt-5.6-sol-xhigh")` is `gpt-5.6-sol-xhigh`.
- [x] `launch_model_arg("gpt-5.4-xhigh")` is `gpt-5.4-extra-high`.
- [x] `launch_model_arg("claude-sonnet-4-6-xhigh")` is `claude-sonnet-4-6-max`.
- [x] `launch_model_arg("not-a-catalog-model-high")` is `not-a-catalog-model-high`.
- [x] None of those return values contain `[` or `]`, and none of the `fast=false`
      results end with `-fast`.
- [x] When `sdk_selection` is stubbed to return `{id: grok-4.7, params: [{id: reasoning_effort, value: high}, {id: fast, value: true}]}`, `launch_model_arg` returns `grok-4.7-high-fast`.
- [x] A cursor `build_plan` for the conductor, with the builtin map and the default
      model, puts `--model` `grok-4.7-high` on the argv. Dry-run output contains
      that id and does not contain `grok-4.7[reasoning_effort`.
- [x] A cursor `build_plan` for a steward uses that same `--model` value.
- [x] `sdk_selection("grok-4.7-high")` is still `{id: grok-4.7}` with
      `reasoning_effort=high` and `fast=false`. No selection this code builds
      contains `fast=true`.
- [x] A Claude conductor plan's argv still starts with `claude` and still passes
      `--effort`.

## Test plan

- **Automated tests:** Update `tests/test_cursor_map.py` so `launch_model_arg`
  assertions expect the flat ids in the table above, including the unknown-id
  pass-through and the absence of brackets and a trailing `-fast`. Add one test
  that monkeypatches `sdk_selection` to return `fast=true` and expects
  `grok-4.7-high-fast`. Update `tests/test_cursor_launch.py`
  (`test_cursor_conductor_argv`, `test_runner_flag_selects_cursor_without_env`)
  so the conductor argv and dry-run output expect `grok-4.7-high`. Assert the
  steward cursor plan's `--model` value in that file (extend
  `test_steward_follows_project_overlay` or add a sibling). Leave
  `tests/test_cursor_sdk_exec.py` asserting the SDK `{id, params}` shape,
  including `fast=false`. Run
  `uv run pytest -q tests/test_cursor_map.py tests/test_cursor_launch.py tests/test_cursor_sdk_exec.py`.
- **Manual verification:** `uv run nw queue conductor-launch --runner cursor --dry-run`
  exits 0. The printed argv contains `--model grok-4.7-high` and does not contain
  `grok-4.7[reasoning_effort`. This assumes the builtin sonnet/high mapping (no
  overlay replaces it).
- **Regression guard:** `tests/test_cursor_launch.py::test_claude_default_unchanged`
  and `test_runner_flag_claude_beats_env` still show a Claude argv with `--effort`.
  `sdk_selection` cases in `tests/test_cursor_map.py` keep today's parameter lists.

### Test record (2026-11-09)

- `uv run pytest -q tests/test_cursor_map.py tests/test_cursor_launch.py tests/test_cursor_sdk_exec.py` — 44 passed.
- `uv run nw queue conductor-launch --runner cursor --dry-run` — exit 0; argv contains `--model grok-4.7-high`, no bracket form.
- Full suite left to gating.

## Clock Log

IDEA-1041 (`nw idea clock-in` / `clock-out` during implementation).

## Rollout / migration

No data migration. The next `conductor-launch` or `steward-launch` on the cursor
runner picks up the flat id. A process already running keeps the model it started
with. A launch that failed on the bracket id is started again after this lands.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
