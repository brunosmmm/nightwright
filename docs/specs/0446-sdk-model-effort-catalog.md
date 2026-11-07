---
id: SPEC-0446
title: Persona SDK effort follows each model's own parameter
status: done
owner: user
created: 2026-11-07
tags: [agentic, cursor]
depends_on: [SPEC-0445]
---

## Context

SPEC-0445 splits a CLI id into a base model and an effort parameter, and it only
knows two rows: `grok-4.7` uses `reasoning_effort`, and `gpt-5.6-sol` uses
`reasoning`. The account's model list has 48 models. The effort parameter is
not the same name, and the legal values are not the same words.

`Cursor.models.list()` on 2026-11-07 groups them like this:

- `reasoning_effort`: `grok-4.7`, `claude-sonnet-5-5`, `claude-haiku-5-5`,
  `gemini-4-argon`, `gemini-3.8-flash`, `muse-wonder-1.0`, `deepseek-v41-flash-1017`.
- `reasoning`: the GPT-5 family, `kimi-k3`, and the GLM models. `gpt-5.4`,
  `gpt-5.5`, `gpt-5.2`, and `gpt-5.3-codex` spell the top step `extra-high`,
  not `xhigh`.
- `effort`: `grok-4.6`, `grok-4.5`, the Claude Opus and Fable models,
  `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-opus-4-6`,
  `gemini-3.6-flash`, `gemini-3.7-flash`, `muse-spark-1.3`.
  `claude-sonnet-4-6` and `claude-opus-4-6` have `max` and no `xhigh`.
- No effort parameter: `composer-2.5`, `composer-2`, `default`, and several
  older Claude, Gemini, and GPT ids. They have other knobs (`fast`, `thinking`,
  `context`) or none.

Context, fast, and thinking stay on the model's default. Sending only the
effort parameter is what a persona spawn needs.

## Goals / Non-goals

**Goals**
- A persona SDK run looks the base id up in the account catalog and sends that
  model's effort parameter and a value that model accepts.
- `gpt-5.4-xhigh` sends `reasoning=extra-high`.
- `claude-sonnet-4-6-xhigh` sends `effort=max`.
- `composer-2.5-high` sends `composer-2.5` with no parameters.
- The builtin alias map and the conductor CLI id stay as they are.

**Non-goals**
- Calling `Cursor.models.list()` on every spawn.
- Setting context, fast, thinking, or cyber.
- Changing which Cursor id an alias maps to.

## Decision

Check in the 2026-11-07 catalog as `sdk_models.yaml`. `sdk_selection` matches
the longest catalog id, treats a trailing effort word as that model's
parameter, and picks the first legal spelling of that step. When the word is
not legal, it picks the nearest rank and prefers the higher step on a tie.
A catalog model with no effort parameter drops the suffix.

## Design

`load_sdk_models` reads the file. `_legal_effort` does the spelling and the
nearest-rank fallback. An id that matches no catalog entry is passed through,
so a model Cursor adds later is not rewritten into a guess.

## Alternatives considered

- **Keep the two-row table** — a persona `cursor_model` of `claude-sonnet-5-5-high`
  or `gpt-5.4-xhigh` would send a value the SDK rejects.
- **Call `models.list()` at spawn** — accurate, and it adds a network call and
  a failure mode to every persona run. The snapshot can be refreshed when the
  account catalog changes.

## Acceptance criteria

- [x] `grok-4.7-high` still sends `reasoning_effort=high`, and `gpt-5.6-sol-xhigh` still sends `reasoning=xhigh`.
- [x] `claude-sonnet-5-5-high` sends `reasoning_effort=high`.
- [x] `claude-opus-5-5-max` sends `effort=max`.
- [x] `gpt-5.4-xhigh` sends `reasoning=extra-high`.
- [x] `claude-sonnet-4-6-xhigh` sends `effort=max`.
- [x] `kimi-k3-medium` sends `reasoning=high`.
- [x] `composer-2.5-high` sends `{id: composer-2.5, params: []}`.
- [x] `gpt-5.4-mini-high` binds to `gpt-5.4-mini`, not `gpt-5.4`.
- [x] An unknown id is passed through unchanged.

## Test plan

- [x] `tests/test_cursor_map.py` covers the rows above.
- [x] `uv run pytest -q tests/test_cursor_map.py tests/test_cursor_sdk_exec.py tests/test_cursor_launch.py tests/test_cursor_runner.py` — 56 passed.

## As shipped

`sdk_models.yaml` holds the 2026-11-07 catalog. `sdk_selection` matches the longest id and sends that model's effort parameter, translating `xhigh` to `extra-high` or `max` when that is the legal spelling. Models with no effort parameter drop the suffix. Context, fast, and thinking are not sent. 56 tests passed on 2026-11-07.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions in the Cursor launch and runner tests.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
