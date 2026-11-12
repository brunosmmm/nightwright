---
id: SPEC-0553
title: Role tiers (marshal/judge/deep), per-backend tier mapping and the hand-only size/tone cap
status: draft
owner: user
created: 2026-11-11
parent: SPEC-0552
tags: [agentic, cost, profiles]
source_idea: IDEA-1297
---

## Context

Phase 1 of [SPEC-0552](./0552-leaner-agentic-campaign-loop.md). Persona strength is set per
profile as a Claude alias (`model: haiku|sonnet|opus` in
`src/nightwright/agentic/profile_defs/*.yaml`), translated per backend by
`cursor_map.yaml` / `pi_map.yaml` (`runner.mapped_model`, `cursor_map.map_model`,
`pi_map.map_pi_model`) or bound by a model set (`model_sets.bind`). The profile itself is
chosen by `profiles.select_profile` from the spec reviewer's own `size`/`tone` via
`PROFILE_MATRIX` (SPEC-0219), called from `queue.py` (`nw queue add` planning, around the
`select_profile(cfg, flag=profile, review=review_doc, …)` call) and `spec_review.would_pick`.
So an agent's estimate can move a campaign to `thorough` (Opus everywhere), and `balanced`
already runs the three code reviewers on Opus-high — the largest cost line in the evidence
(Opus-high reviewers = 56% of all recorded spend).

## Goals / Non-goals

**Goals**
- Each persona declares a role tier; each backend maps tiers to concrete models.
- At default, no persona runs on the deep tier.
- Automatic sizing can lower, never raise, the profile; raising is a hand edit of the spec.
- A hand-raised campaign gets one fresh deep-tier adversarial pass at close.
- Introduce the `agentic.lean` flag block (all phases) and pin it per campaign.

**Non-goals**
- Changing what any persona does (prompts and schemas unchanged in this phase).
- Removing profiles or model sets; they remain (budgets, slots, reviewers, overrides).
- The packet, collator and sessions (SPEC-0554..0556).

## Decision

Add a `tier:` key (`marshal | judge | deep`) to every builtin persona definition and a
per-backend tier map (`tier_map.yaml`). With `agentic.lean.tiers` on, a builtin persona's
model is its tier's model on the spawn's backend, unless the profile or model set names an
explicit `model:` for it (override). Cap auto-sizing at `agentic.default_profile`; honour
`size:`/`tone:` written by hand into the spec frontmatter as the only way to go above it.

## Design

### Flags (shared by all phases)

`config.yaml`:

```yaml
agentic:
  lean: {tiers: false, packet: false, collator: false, sessions: false}
  project_lean: {Meta-Tools: {tiers: true}}   # per-project overlay, for A/B
```

At open (`queue._do_open_unwrapped`) the effective flags are written to `state.lean` and an
`open` event; every later step reads `state.lean`, never the live config, so a campaign's
behaviour does not change mid-flight. Unmet dependencies (`collator` without `packet`,
`sessions` without `packet` and `collator`) are switched off with a reason in the event.

### Tiers

- `persona_registry.py` / `persona_defs.py`: accept `tier` in builtin and declared
  definitions (enum; builtin required, declared default `judge`). Values per the epic table:
  spec_writer, spec_reviewer, implementer, conflict_resolver, the three code reviewers and
  divergence_triager = judge; gate_triager, review_triager = marshal.
- New `src/nightwright/agentic/tier_map.yaml` (schema `nw.tier_map.v1`) with an overlay
  chain like `cursor_map.yaml` (`<config_dir>/tier_map.yaml`, `agentic.tiers` in config,
  `<project>/.nw/tier_map.yaml`):

  ```yaml
  schema: nw.tier_map.v1
  claude: {marshal: {model: claude-haiku-5-5, effort: medium},
           judge:   {model: claude-sonnet-5-5, effort: high},
           deep:    {model: claude-opus-5-5,  effort: high}}
  cursor: {marshal: {model: grok-4.7, effort: low},        # to be confirmed
           judge:   {model: grok-4.7, effort: high},       # to be confirmed
           deep:    {model: gpt-5.6-sol, effort: high}}    # to be confirmed
  pi:     {marshal: {model: openai/gpt-5.6-luna},          # to be confirmed
           judge:   {model: openai/gpt-5.6-sol},           # to be confirmed
           deep:    {model: openai/gpt-6.1-terra}}         # to be confirmed
  ```

  A Cursor row is still checked against `sdk_models.yaml` (effort parameter), a Pi row must
  be `provider/id` (`model_sets._pi_model_ok`).
- New `agentic/tiers.py`: `persona_tier(persona, profile, state)` (declared tier, raised to
  deep only for the close pass below) and `tier_model(cfg, backend, tier, project_root)`.
- `personas.build_request`: when `state.lean.tiers` and the persona's settings carry no
  explicit `model` override, `(model, effort)` come from `tier_model` for the backend
  `runner.resolve_runner` / the model set picks; the run request records `tier`. Budgets,
  permission modes and timeouts still come from the profile.
- `runner.write_run_record` and `campaign_export.RUN_COLUMNS` gain `tier`; `CAMPAIGN_COLUMNS`
  gains `lean` (json) and `raised` (bool).

### Cap

- `docs/specs/spec.schema.json` (and `SCHEMA.md`): optional `size` (`xs|s|m|l`) and `tone`
  (`cost-effective|high-end|critical`) frontmatter keys, documented as "human-only: raises
  the campaign above the default".
- `profiles.select_profile(cfg, *, flag, review, hand=None, …)`: `hand` is the
  `{size, tone}` read from the spec **as committed on the base branch at open** (the
  `spec_at_open` snapshot, `queue._adopt_spec_open_snapshot`). Order: profiles are ranked by
  their position in `PROFILE_MATRIX` strength (`frugal < balanced < thorough`; a
  user-defined profile ranks by `extends` ancestry, unknown → equal to default).
  - review-derived pick: `min(matrix pick, default_profile)`; the reason says "capped".
  - `--profile` flag: `min(flag, default_profile)` unless `hand` raises it.
  - `hand` present: matrix cell of the hand values, with no cap; `source: "hand"`,
    `state.raised = true`.
- A `size`/`tone` key added or changed in the spec inside the campaign (spec writer output,
  implementer commit) is ignored for sizing and logged as a `cap_ignored` event; the spec
  writer persona's instructions say not to write these keys.

### Deep close pass (raised only)

With `state.raised`, the CLOSING path (`queue._do_prepare_close` / `close.py`) runs one
extra fresh `adversarial_reviewer` run at tier deep (no session, no packet dependency)
before the close; its `fix` verdict sends the campaign back to IMPLEMENTING as a normal
review fix (counts against `loop_max`). Not raised → no extra run.

## Alternatives considered

- **Retune the profile models only (reviewers to Sonnet)** — rejected by the human
  ("restructure, not just retune"); and the evidence shows Sonnet reviewers cost the same
  per run because they re-read more.
- **Reuse the haiku/sonnet/opus aliases as the tiers** — the aliases are Claude names that
  Cursor/Pi maps reinterpret; a backend-neutral name per role is clearer, and explicit
  Claude ids (`claude-sonnet-5-5`) stop silent drift when an alias moves.
- **Trust the spec reviewer's size/tone** — rejected by the human: auto-sizing may never
  raise the tier.

## Acceptance criteria

- [ ] Every builtin persona definition declares a `tier`; a definition with a missing or
      unknown tier fails `nw personas validate` naming the file.
- [ ] With `lean.tiers` on, each builtin persona run on claude records the model and tier of
      `tier_map.yaml` (e.g. `conformance_reviewer` → `claude-sonnet-5-5`, judge), and an
      explicit profile/model-set `model:` still overrides it.
- [ ] `select_profile` never returns a profile stronger than `agentic.default_profile` from
      a review's size/tone or from `--profile`, unless the spec on base at open carries a
      hand-written `size`/`tone`; a key introduced inside the campaign is ignored and an
      event says so.
- [ ] `spec_lint` accepts `size`/`tone` frontmatter with the enumerated values and rejects
      others.
- [ ] A hand-raised campaign runs exactly one fresh deep-tier `adversarial_reviewer` at
      close; a default campaign runs none.
- [ ] `state.lean` is pinned at open; flipping the config mid-campaign changes nothing for
      that campaign; with every flag off, model selection is byte-identical to today.
- [ ] The runs export has `tier`; the campaigns export has `lean` and `raised`.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_tiers.py` (new): tier validation in persona defs; `tier_model` per
    backend incl. overlays; override precedence; Cursor effort and Pi `provider/id` checks.
  - `tests/test_agentic_profiles.py` (extend): cap matrix — every (size, tone) review pick
    capped at default; flag capped; hand values uncapped; in-campaign edits ignored with a
    `cap_ignored` event.
  - `tests/test_specs.py` / spec_lint fixture: `size`/`tone` accepted, bad values rejected.
  - FakeRunner queue test: raised campaign → one deep adversarial run at close, its `fix`
    re-enters IMPLEMENTING; default campaign → none; flags pinned at open.
  - `tests/test_agentic_campaign_export.py` (extend): `tier`, `lean`, `raised` columns.
- **Manual verification:** one real call per backend row in `tier_map.yaml` (claude, and
  cursor / pi where credentials exist) — rows not verified stay marked "to be confirmed".
  **Measurement:** with only `lean.tiers` on for Meta-Tools, run ≥10 campaigns; compare
  median `cost_usd` and summed tokens per campaign against the baseline query in
  SPEC-0552; replay the regression set (SPEC-0529 oracle, SPEC-0476 hang) with judge-tier
  reviewers and record whether each is caught.
- **Regression guard:** all flags off → existing profile / model-set tests unchanged.

## Rollout / migration

Ships with `lean.tiers: false`. Turn on for Meta-Tools via `agentic.project_lean`, measure,
then default-on. The `size`/`tone` schema keys are additive; existing specs are unaffected.
Existing campaigns keep their stored profile.

## Risks

- **Quality dip from judge-tier reviewers.** Accepted at default by the human; the
  regression set is the floor, and a hand raise restores deep strength.
- **Agents writing `size`/`tone`.** Mitigated by honouring only the base-branch spec at
  open and logging in-campaign edits.
- **Unconfirmed Cursor/Pi models.** Rows stay marked until a real call succeeds; a bad row
  fails at spawn with the backend's own error (no silent fallback to another model).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; measurement recorded in the spec.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## Open questions

- Confirm the Cursor and Pi tier models (see the epic).
- Should the conductor be allowed to raise via an explicit, logged human-OK relay instead of
  a spec edit? (Default here: no — a spec edit only.)
