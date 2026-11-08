---
id: SPEC-0466
title: GPT-only Pi SDK model set for all nightwright personas
status: in-progress
owner: user
created: 2026-11-08
kind: feature
tags: [agentic, pi, models]
depends_on: [SPEC-0465, SPEC-0455]
source_idea: IDEA-1031
---

## Context

The Pi SDK runner and the model-set feature were built concurrently. Their integration must allow
one selectable model set to use GPT through Pi for all personas, without accidentally falling back
to Claude or Cursor. Workflow profiles select budgets, limits and efforts; model sets choose models
and backends.

## Goals / Non-goals

Provide a selectable builtin GPT-only Pi model set and accurate per-persona request resolution.
Do not change conductor/steward backends or make GPT the default.

## Decision

Add model set `gpt` with runner `pi` and OpenAI GPT identifiers. Persona model settings override
the default as in SPEC-0455; every row of the builtin set uses Pi and an OpenAI GPT model. Existing
profile effort becomes Pi thinking. Model sets can be overridden through existing user/project
layers. Pi model-set bindings use explicit `provider/id`, rather than the legacy alias mapping.

## Design

Allow `pi` in model-set validation; require a valid provider/id for Pi entries. Bind the explicit
model and effort into the Pi request's provider, id and thinking fields, including standalone spec
review and remote dispatch. Pi readiness detection must account for selected Pi model sets. Preserve
all non-Pi behavior. Ship a builtin `gpt` model set, documented as opt-in alongside workflow
profiles; do not alter the user's configuration without an explicit request.

## Alternatives considered

- Reconfigure the alias map globally: rejected because it is not a selectable model set and may
  change unrelated Pi uses.

## Acceptance criteria

- [ ] Builtin `gpt` binds every persona to Pi and an OpenAI GPT model; no implicit fallback.
- [ ] Persona and spec-review requests preserve provider/id/thinking, including remote jobs.
- [ ] Invalid Pi model set entries fail validation, existing sets remain unchanged, and Pi readiness
      detects the selected set.
- [ ] Documentation explains how to select and override the set and provider credentials.

## Test plan

Run focused tests for model sets, Pi runner, persona request building and remote serialization;
verify a real Pi child with a working configured GPT model. Run spec lint and document results.

### Progress (2026-11-08)

The `gpt` set uses `openai/gpt-6.1-terra` for implementer, conflict resolver and spec writer;
`openai/gpt-5.6-sol` for spec, conformance and adversarial review; and `openai/gpt-5.6-luna`
for tests and triage (also the default). This deliberately diversifies reviews and assigns a
separate model to triage rather than claiming benchmarked model superiority. These are initial
choices, not a measured ranking; project/user model sets can replace them. Pi model-set validation,
per-persona Pi request fields, and selected-set provider readiness are implemented. Spec-review
and remote paths still require explicit integration verification; the set has not been installed
or enabled globally.

## Rollout / migration

After merging Pi support, select the `gpt` model set using existing model-set configuration.

## Definition of done

- [ ] Acceptance criteria true; test plan executed and results recorded.
- [ ] Spec and generated ledger match what shipped; spec lint passes.

## Open questions

None.
