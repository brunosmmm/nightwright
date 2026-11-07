---
id: SPEC-0455
title: A model set chooses the runner and the concrete model
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [agentic, runner]
depends_on: [SPEC-0436, SPEC-0446]
---

## Context

`balanced`, `frugal` and `thorough` are workflow shapes: slots, caps, reviewers, budgets, and which persona is the strong alias (`opus`, `sonnet`, `haiku`) at which effort. They are not a choice of vendor. The same thorough profile can run entirely on Claude Code, or on Cursor, or split across both.

On a Cursor host the builtin alias map sends `opus` to `gpt-5.6-sol`. That model is over this account's usage limit. Pinning Composer and Grok inside a fourth workflow profile named `cursor` mixed the two choices: a campaign's stored profile name would have had to change, and a Claude-only install could not keep `balanced` while picking Claude models.

## Goals / Non-goals

**Goals**

- A model set is a separate selectable binding of runner and concrete model.
- Builtin sets: `claude` (Claude Code for every persona), `grok` (Composer implements, Grok 4.7 does the rest), `mixed` (Claude writes, Grok reviews and triages).
- The workflow profile still supplies the effort. A base id that has an effort parameter gets that effort appended. A model with no effort parameter stays the bare id.
- One campaign can use both runners. Each persona's request names its own.
- Unset `agentic.model_set` keeps today's host runner and alias map.

**Non-goals**

- No fourth workflow profile. `balanced`, `frugal` and `thorough` stay the only builtins.
- No change to the builtin alias map.
- No command that rewrites a campaign's stored profile name.
- No new config-delta layer inside `config.yaml` for model sets. Extra sets are files.

## Decision

`agentic.model_set` names a set. Builtin files live in `src/nightwright/agentic/model_set_defs/`. A file under `~/.config/nightwright/model_sets/` replaces a builtin of the same name, and a project's `.nw/model_sets/` replaces that. The set's `model` is a base catalog id. At spawn, the persona's profile effort is appended when that id has an effort parameter.

## Design

A set file is `schema: nw.model_set.v1` with `runner` (`claude` or `cursor`), an optional default `model`, and optional per-persona `runner` and `model`. Personas must be names in `personas.PERSONAS`. A Cursor persona needs a model. Claude aliases (`opus`, `sonnet`, `haiku`, `claude-*`) are rejected as model ids: those stay on the workflow profile.

`resolve_model_set` returns `None` when `agentic.model_set` is unset. An unknown name or an invalid file raises `ValueError`.

`bind(set, persona, alias, effort)` returns `{runner, model, model_alias}`:

- Claude keeps `alias` as `model` and sets `model_alias` to `None`.
- Cursor uses the persona's model or the set default. `cursor_id` appends the profile effort when the catalog entry has `effort_param` (`grok-4.7` plus `high` is `grok-4.7-high`; `max` becomes `xhigh` because that is the nearest value Grok accepts). `composer-2.5` has no effort parameter, so the effort is dropped and the id stays bare. An id that is not itself a catalog key passes through unchanged.
- `model_alias` on Cursor is the profile alias, so cost and the run record still know the persona was `opus` or `sonnet`.

`build_request` and `run_spec_review` call `bind` when a set resolves. A profile `cursor_model` does not override an active set. With no set, `mapped_model` behaves as it does today.

`RunRequest.runner` carries the set's backend (`None` means the host runner). `request.json` records it and `rebuild_request` restores it. The queue's `_local_runner` asks `runner_for(state, kind=req.runner)`. A test double that rejects the `kind` keyword is called with the state only. A remote persona job header uses `req.runner` when set. `cursor_needed` is true when the selected set's default runner or any persona runner is `cursor`, including an unreadable set.

Builtin `grok`: default runner `cursor`, default model `grok-4.7`; `implementer` and `conflict_resolver` are `composer-2.5`. Builtin `mixed`: default runner `claude`; the three reviewers, `spec_reviewer`, `review_triager` and `gate_triager` are Cursor on `grok-4.7` plus the profile effort. Builtin `claude`: runner `claude` for every persona.

## Alternatives considered

- **A fourth workflow profile named `cursor`.** It cannot describe a Claude-only install of `balanced`, and a campaign stores the profile name, so switching sets would have meant rewriting every open campaign.
- **Baking `grok-4.7-high` into the set.** That freezes every workflow profile at one effort. Frugal's medium and thorough's high would become the same run.

## Acceptance criteria

- [x] `balanced`, `frugal` and `thorough` remain the only builtin workflow profiles.
- [x] `agentic.model_set` selects `claude`, `grok` or `mixed`. An unknown name raises `ValueError` listing the known sets. Unset leaves the host runner and the alias map in place.
- [x] Under `grok`, `implementer` and `conflict_resolver` spawn `composer-2.5` with no effort parameter. Every other persona spawns `grok-4.7` with the workflow profile's effort appended (`high` → `grok-4.7-high`, `medium` → `grok-4.7-medium`, `max` → `grok-4.7-xhigh`).
- [x] Under `mixed`, writers stay on Claude Code with the profile alias. The three reviewers, `spec_reviewer`, `review_triager` and `gate_triager` spawn Cursor on `grok-4.7` plus the profile effort.
- [x] Under `claude`, every persona stays on Claude Code and the profile alias. The host `agentic.runner` does not override the set.
- [x] A persona request records its runner, and the queue spawns that runner. A profile `cursor_model` does not override an active set.
- [x] The three set files ship in the wheel.

## Test plan

- **Automated tests:** `tests/test_model_sets.py` asserts the three builtins, effort suffix, bare Composer id, mixed runners, the Claude set, unknown names, `build_request` recording `runner`, and `_local_runner` passing that runner through. `tests/test_packaging.py` asserts the three YAML files are in the wheel. `tests/test_agentic_profiles.py` still lists only `balanced`, `frugal` and `thorough`.
- **Manual verification:** with `agentic.model_set: grok`, resolving `balanced` and binding `implementer` yields `composer-2.5`, and binding `conformance_reviewer` at effort `high` yields `grok-4.7-high`.
- **Regression guard:** a config with no `model_set` still maps `opus` through the alias map. The profile golden snapshot is unchanged.

## Rollout / migration

Land the code before removing any `cursor_model` overlay. Then set `agentic.model_set: grok` on this install and drop the temporary `cursor` workflow profile, so a resume keeps using Composer and Grok instead of `gpt-5.6-sol`. Open campaigns keep their stored profile names.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
