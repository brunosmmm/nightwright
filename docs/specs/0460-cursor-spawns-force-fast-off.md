---
id: SPEC-0460
title: Cursor spawns keep the effort and force fast off
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [agentic, cursor]
depends_on: [SPEC-0446, SPEC-0437]
---

## Context

[SPEC-0446](./0446-sdk-model-effort-catalog.md) sends a persona's effort parameter and leaves `fast` unset. The account catalog's default variant for `grok-4.7` is Grok 4.7 High Fast (`reasoning_effort=high`, `fast=true`, 500k context). Composer 2.5's default is the fast variant too. A spawn that omits `fast` therefore runs the fast model. The same thing happens on the conductor and steward CLI: `--model grok-4.7-high` does not pass `fast=false`, so the CLI takes that default.

The effort is the one already chosen. Fast is not a workflow choice.

## Goals / Non-goals

**Goals**

- A Grok or Composer spawn uses the same effort it uses today, with `fast=false`.
- Every other catalog model is unchanged.

**Non-goals**

- No change to context, thinking, or cyber.
- No config key that turns fast back on.
- No change to the dashed id stored on a persona request (`grok-4.7-high` stays the request id).

## Decision

The catalog records `fast: false` only on the Grok and Composer ids. `sdk_selection` appends `fast=false` when that key is present, and it never sends `true`. The conductor and steward `--model` value is the same selection written as the CLI bracket form, `grok-4.7[reasoning_effort=high,fast=false]`.

## Design

`sdk_models.yaml` gains `fast: false` on `grok-4.7`, `grok-4.6`, `grok-4.5`, `composer-2.5`, and `composer-2`. Those are the models this queue spawns, and their default variant is the fast one. The code treats the key as presence only and always sends `false`. GPT and Claude ids keep the effort parameter alone.

`launch_model_arg` formats `sdk_selection` as `id[param=value,...]` for `cursor-agent --model`. `map_launch_model` still returns the dashed id; `build_plan` passes that through `launch_model_arg`.

## Alternatives considered

- **Leave fast unset.** That is today's behavior, and the default variant is fast.
- **A profile setting for fast.** There is nothing to choose. Fast stays off.

## Acceptance criteria

- [x] `sdk_selection("grok-4.7-high")` is `grok-4.7` with `reasoning_effort=high` and `fast=false`.
- [x] `sdk_selection("composer-2.5")` is `composer-2.5` with only `fast=false`.
- [x] `sdk_selection("claude-sonnet-5-5-high")` and `sdk_selection("gpt-5.4-xhigh")` still send only the effort parameter.
- [x] A Cursor conductor launch passes `--model grok-4.7[reasoning_effort=high,fast=false]`.
- [x] No selection this code builds contains `fast=true`.

## Test plan

- **Automated tests:** `tests/test_cursor_map.py` asserts the grok, composer, and sonnet selections. `tests/test_cursor_sdk_exec.py` asserts the persona exec options. `tests/test_cursor_launch.py` asserts the conductor argv. Run those files with `uv run pytest`. Executed 2026-11-07: 42 passed.
- **Manual verification:** none beyond the tests. The next persona spawn and the next conductor or steward launch pick this up; a process already running keeps the model it started with.
- **Regression guard:** models without a fast parameter keep the same parameter list they had.

## Rollout / migration

Land on main. The queue re-execs on the source change, and the next persona spawn sends `fast=false`. Restart the conductor and any steward to leave a session that was started with `--model grok-4.7-high`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
