---
id: SPEC-0553
title: Role tiers (marshal/judge/deep), per-backend tier mapping and the hand-only size/tone cap
status: done
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

### Design as built (where it differs from the above)

- **Pin point.** The flags are pinned when the campaign is created (`store.create_campaign`,
  which also writes the `open` ledger event, with `lean` and any `lean_off` reasons), not in
  `_do_open_unwrapped`: that is where the `open` event lives, and it covers every creation
  path. `state.lean` is written only when a flag is on (or a dependency was switched off),
  so a flag-off campaign's state and events are unchanged; an absent `state.lean` reads as
  all off and stays so.
- **Cap in two places.** `plan_add` applies the cap (review / `--profile`) with the
  effective flags, so `nw queue add` shows the capped profile and records `sizing.flag` /
  `sizing.capped_from`. The OPENING step's apply (`queue._lean_open`) then reads the hand
  values from the `spec_at_open` snapshot (the spec at the base sha) and re-runs
  `select_profile(..., hand=, lean=True)`; a changed pick re-resolves the profile (name,
  sources, sha, `cost.cap_usd`) and logs `lean_sizing`. `select_profile` keeps its old
  signature and output unless `lean=True`.
- **Hand rules.** A key the human did not write is the default (`m` / `high-end`), never
  the review's estimate (so a tone-only `high-end` with an agent `l` stays `balanced`).
  `raised` is true only when the result ranks above the default (a hand
  `cost-effective`/`xs` lowers and is not "raised"). With a flag and a hand value the flag
  is capped at `max(default, hand cell)`.
- **Other creation paths.** `plan_merge` and `plan_add_idea` (`nw queue add IDEA-…`) cap a
  `--profile` at the default through `profiles.cap_flag` when the project's effective
  `tiers` is on (no hand values on those paths).
- **Untrusted snapshot.** When `_adopt_spec_open_snapshot` rebuilds `spec_at_open` from the
  worktree (missing or untrusted snapshot on a resumed worktree), it marks
  `spec.snapshot_adopted`; `_lean_open` then reads hand size/tone as absent, journals
  `lean_hand_untrusted` and sets `hand_untrusted` on the `lean_sizing` event.
- **Override rule.** "Explicit `model:`" means a `personas.<name>.model` set by a layer
  above the builtin profiles (user file, `agentic.profiles` delta, project file — read from
  `profile_provenance`), a model set's per-persona `model:`, a `cursor_model:` on a Cursor
  spawn, or a declared persona's own `model:`. A model set's set-level `model:` is not an
  override (tiers replace it). The deep close pass ignores overrides (deep is deep).
- **Deep close pass at the review decision.** Implemented where the reviews pass
  (`_route_reviews`, decision `close`) rather than in `_do_prepare_close`: the adversarial
  verdict of the loop is dropped and the campaign stays REVIEWING (`deep_close_pass`), so
  the next review step runs that reviewer alone at tier deep and its verdict routes like any
  review (pass → CLOSING, fix → IMPLEMENTING as a review fix, +1 loop). `state.deep_close`
  (`pending` → `done`) makes it once per campaign; `deep_close` events record both. Skipped
  when the profile's reviewers do not include `adversarial_reviewer`.
- **In-campaign keys.** `cap_ignored` is checked when an implement or write_spec step
  finishes (worktree spec vs the hand values at open), once per new value. The spec writer
  note is appended to its prompt only with `lean.tiers` pinned (prompts are unchanged when
  off).
- New ledger event types: `lean_sizing`, `cap_ignored`, `deep_close`.

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

- [x] Every builtin persona definition declares a `tier`; a definition with a missing or
      unknown tier fails `nw personas validate` naming the file.
- [x] With `lean.tiers` on, each builtin persona run on claude records the model and tier of
      `tier_map.yaml` (e.g. `conformance_reviewer` → `claude-sonnet-5-5`, judge), and an
      explicit profile/model-set `model:` still overrides it.
- [x] `select_profile` never returns a profile stronger than `agentic.default_profile` from
      a review's size/tone or from `--profile`, unless the spec on base at open carries a
      hand-written `size`/`tone`; a key introduced inside the campaign is ignored and an
      event says so.
- [x] `spec_lint` accepts `size`/`tone` frontmatter with the enumerated values and rejects
      others.
- [x] A hand-raised campaign runs exactly one fresh deep-tier `adversarial_reviewer` at
      close; a default campaign runs none.
- [x] `state.lean` is pinned at open; flipping the config mid-campaign changes nothing for
      that campaign; with every flag off, model selection is byte-identical to today.
- [x] The runs export has `tier`; the campaigns export has `lean` and `raised`.

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

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; measurement recorded in the spec
      (post-merge, with the flag on: see As shipped).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

## As shipped

Shipped 2026-11-11 behind `agentic.lean.tiers` (default **off**; `agentic.lean` =
`{tiers, packet, collator, sessions}` all `false` in `config.DEFAULT_CONFIG`, per-project
overlay `agentic.project_lean`). With every flag off, `build_request` returns the same
`RunRequest` as before (asserted field by field), `run.json` / `request.json` / `run.start`
carry no `tier`, `state.json` gains no key and `select_profile` returns the old dict.

**Modules.** `agentic/tiers.py` (flags, tier map loader + validation, `tier_model`,
`persona_tier`, `explicit_model`), `agentic/tier_map.yaml` (`nw.tier_map.v1`), `tier:` in
the ten `persona_defs/*.yaml` (`persona_registry.TIERS`, `check_tier`; `Persona.tier`),
`profiles.select_profile(..., hand=, lean=)` + `profile_rank` / `hand_sizing`,
`personas.build_request` (tier binding, spec-writer note), `RunRequest.tier` (run record,
request doc, remote job), queue: `plan_add` cap, `_lean_open`, `_lean_cap_check`,
`_start_deep_close` / `_note_deep_close`, `store.create_campaign` pin, export columns
(`runs.tier`, `campaigns.lean`, `campaigns.raised`), `spec.schema.json` / `SCHEMA.md`
`size` / `tone`, product docs (Agentic loop → Role tiers and the size/tone cap).

**Tier map and the real calls (one per row, 2026-11-11).**

| Backend | marshal | judge | deep | Real call |
|---|---|---|---|---|
| claude | `claude-haiku-5-5` medium | `claude-sonnet-5-5` high | `claude-opus-5-5` high | `claude -p --model <id> --effort <e> 'reply ok'`: all three replied `ok`, `modelUsage` named the same id. **Confirmed.** |
| pi | `openai/gpt-5.6-luna` medium | `openai/gpt-5.6-sol` high | `openai/gpt-6.1-terra` high | `node pi_exec.mjs <run_dir>` with `pi_provider/pi_id/pi_thinking` as `build_request` writes them: all three returned a schema-valid `{"ok":"ok"}` result. **Confirmed.** |
| cursor | `grok-4.7` low (`grok-4.7-low`) | `grok-4.7` high | `gpt-5.6-sol` high | SDK path (`cursor_exec`): `CURSOR_API_KEY` could not be resolved in this shell (`pass show cursor/sdk`: gpg "No pinentry"). `cursor-agent -p --model <launch id>`: the ids were accepted but no reply — grok rows "Weekly usage limit reached", `gpt-5.6-sol-high` "GPT-5.6 Sol is not included in your weekly usage". **To be confirmed** (rows kept, marked so in `tier_map.yaml`). |

No id had to be replaced. Cursor rows validate against `sdk_models.yaml` (the effort must be
one the model takes); Pi rows must be `provider/id`.

**Tests (all run locally, passing).** New `tests/test_agentic_tiers.py` (38: tier
validation builtin/declared/CLI, map rows, overlays, bad rows, Cursor effort parameter,
flags, flag-off identity, claude/cursor/pi binding, override precedence incl. model sets,
deep pass, run record/request/job carry `tier`); new `tests/test_agentic_lean_queue.py`
(FakeRunner: pinned at open and unchanged by a later config flip both ways, review sizing
capped, `--profile` capped, hand-raised `tone: critical` → `thorough`, `raised`, exactly one
`claude-opus-5-5` deep adversarial run before close; deep `fix` → IMPLEMENTING with one loop
and still one deep run; default campaign → no deep run and no `opus`; `cap_ignored` once for
an in-campaign `size`/`tone`; hand ignored with the flag off; `nw queue add IDEA --profile thorough`
capped; a worktree re-snapshot gives no hand values); extended
`tests/test_agentic_profiles.py` (cap over every size × tone × default, flag, hand,
`profile_rank` via `extends`, the agent's estimate never fills a missing hand key,
`cap_flag`); the flag-off identity test covers every persona × claude / cursor / pi, the
`grok` and `gpt` model sets and the frugal / thorough profiles, `tests/test_specs.py` (`size`/`tone` accepted / rejected),
`tests/test_agentic_campaign_export.py` (columns). The box gate (`--tier loop`) ran on the
final commit (job id in the campaign report).

**Pending (post-merge, flag on).** The measurement — Meta-Tools with only `lean.tiers`,
≥10 campaigns, median `cost_usd` and summed tokens per campaign vs the SPEC-0552 baseline,
and the regression-set replay (SPEC-0529 oracle, SPEC-0476 hang) with judge-tier reviewers
— needs the flag on in real campaigns and is recorded here after merge. Two things on this
host affect it: (1) the human's `cursor` profile (`~/.config/nightwright/profiles/
cursor.yaml`, the `default_profile`) names `model:` for every persona and `agentic.profiles`
names `cursor_model:`, which are explicit overrides, so tiers change nothing on those
campaigns until those keys are dropped; (2) `agentic.profile_matrix` maps every cell to
`cursor`, so a hand `size`/`tone` cannot rank above the default there and no campaign would
be "raised". `nw spec review`'s `would_pick` still shows the uncapped pick (display only;
`nw queue add` shows the capped one).

## Open questions

- ~~Confirm the Cursor and Pi tier models (see the epic).~~ Pi confirmed 2026-11-11 (As
  shipped); Cursor still to be confirmed with one reply per row once the account's weekly
  limit resets and `CURSOR_API_KEY` resolves.
- ~~Should the conductor be allowed to raise via a relayed human OK?~~ Decided by the human
  2026-11-11: **no**. Only a size/tone the human wrote into the spec counts.
