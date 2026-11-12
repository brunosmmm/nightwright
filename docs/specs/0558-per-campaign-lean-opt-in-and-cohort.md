---
id: SPEC-0558
title: Per-campaign lean opt-in (`nw queue add --lean`) and an A/B cohort tag
status: accepted
owner: user
created: 2026-11-12
parent: SPEC-0552
depends_on: [SPEC-0553, SPEC-0554, SPEC-0555, SPEC-0556]
tags: [agentic, cost, queue]
source_idea: IDEA-1297
---

## Context

Child of [SPEC-0552](./0552-leaner-agentic-campaign-loop.md). The four lean flags
(`tiers`, `packet`, `collator`, `sessions`; SPEC-0553..0556) are read only from the config:
`agentic.lean` with the per-project overlay `agentic.project_lean.<project>`, resolved by
`tiers.effective_lean(cfg, project)`. `store.create_campaign` pins the result into
`state.lean` when the campaign is created, and `queue.plan_add` / `plan_merge` /
`plan_add_idea` read the same function for the size/tone cap. Turning the flags on for a
project switches every campaign of that project at once, so lean and normal campaigns
cannot run side by side and be compared.

**Human approval (2026-11-12).** The human asked for a separate lean cohort ("do the
separate lean"): the flags must be choosable per campaign, not only per project, so lean
campaigns run alongside normal ones as an A/B test. This spec records that decision and is
accepted on it.

## Goals / Non-goals

**Goals**
- `nw queue add … --lean` runs that one campaign with all four flags on;
  `--lean-flags a,b` with a subset. The choice overrides the config for that campaign only.
- A cohort tag on the campaign, exported, so the SPEC-0552 measurement can group runs.
- A campaign queued without these options behaves exactly as today.

**Non-goals**
- Changing what any flag does, the tier map, or the profiles.
- Editing the user's `config.yaml` or profile files (the explicit-model finding below is
  reported, not fixed).
- A lean opt-in on `nw queue merge` (a merge campaign runs only the conflict resolver and
  gates; it keeps following the config).

## Decision

Two `nw queue add` options select the flags for one campaign: `--lean` (all four) and
`--lean-flags a,b` (exactly those). The selection is stored on the queued campaign as
`state.lean_request` and **replaces** the config/project flags for that campaign when
`store.create_campaign` pins `state.lean` through the existing `effective_lean` path; the
dependency rule (`collator` needs `packet`, `sessions` needs `packet` and `collator`) still
switches unmet flags off with a reason. `--cohort NAME` stores `state.cohort` (default `lean`
with an opt-in, none otherwise), recorded on the `open` event and exported as the campaign
column `cohort` (plus `lean_request`).

## Design

- **`tiers.py`.** `lean_request(lean=False, lean_flags=None) -> list | None` parses the CLI
  form: `--lean` gives `LEAN_FLAGS`; `--lean-flags` a comma list, deduplicated and put in
  `LEAN_FLAGS` order; both at once, an empty list or an unknown name raise `ValueError`.
  `campaign_cohort(cohort, request)` validates a tag (1..40 of `[A-Za-z0-9._-]`, starting
  alphanumeric) and defaults it to `lean` when `request` is set. `effective_lean(cfg,
  project=None, *, request=None)`: with `request`, exactly the named flags are on (config and
  project overlay ignored), then the same dependency pass; `request=None` is the old code
  path, unchanged.
- **Replace, not overlay (decided).** A cohort is defined by the flags it asked for, so a
  later change to `agentic.lean` / `project_lean` cannot leak into the lean arm of the A/B or
  change what `--lean-flags packet` means. A campaign that wants the config keeps not
  passing the option.
- **Queue.** `plan_add`, `plan_add_idea` and `plan_add_batch` take `lean_request` and
  `cohort`; `_campaign_lean_pin` returns `{lean_request, cohort}` (empty without options,
  like `_campaign_runner_pin`). The plan's `lean` / `lean_off` and the size/tone cap
  (`select_profile(lean=True)`, `cap_flag`) use `effective_lean(..., request=)`, so a lean
  campaign is capped at `agentic.default_profile` like a config-lean one. `apply_add` /
  `apply_add_idea` copy the two keys onto the state (`_pin_lean_request`); the batch params
  carry them to every item's re-plan.
- **Pin.** `store.create_campaign` passes `state.lean_request` to `effective_lean` and writes
  `state.lean` when a flag is on, a dependency was switched off, or an opt-in was given (the
  old rule otherwise). The `open` event gains `lean_request` and `cohort` only when present.
  Every later step already reads `state.lean`, never the live config.
- **CLI.** `nw queue add` gains `--lean`, `--lean-flags A,B`, `--cohort NAME`; they apply to
  every id of the command (single, batch, `--epic`, `--from-file`) and are validated before
  any plan is built.
- **Export.** `CAMPAIGN_COLUMNS` appends `("cohort", "str")` and `("lean_request", "json")`
  (`nw.campaign.export.v1`: appended, no column retyped).
- **Docs.** `docs/product/capabilities/agentic.md` (new "Per-campaign lean opt-in" section)
  and `docs/product/reference/index.md` (`nw queue add` options).

### Model and effort of a lean Claude campaign under the user's config

With `tiers` on, `personas.build_request` takes a persona's model and effort from its tier
(`tier_map.yaml`: claude marshal `claude-haiku-5-5`/medium, judge `claude-sonnet-5-5`/high,
deep `claude-opus-5-5`/high) unless `tiers.explicit_model` finds a model named above the
builtin layer; the SPEC-0557 ceiling then clamps the effort to `high`.

The user's `~/.config/nightwright/config.yaml` (read 2026-11-12, not edited) sets
`runner: claude`, `model_set: claude`, `default_profile: cursor`,
`spec_review_profile: cursor`, every `profile_matrix` cell `cursor`, and `agentic.profiles`
overrides for `balanced` / `thorough` (only `cursor_model` keys, plus `spec_reviewer:
{model: sonnet, effort: xhigh}` on `balanced`). The `cursor` profile is the user file
`~/.config/nightwright/profiles/cursor.yaml` (`extends: balanced`), which names
`model: sonnet` for nine personas. `cursor_model` keys apply only on a Cursor spawn, so they
play no part here (and the `claude` model set binds every persona to the claude runner).

Computed with the real resolution code (`load_config()`, the Meta-Tools project config,
`select_profile(..., lean=True)`, `resolve_profile`, then `personas.build_request` for every
builtin persona on a state with `lean_request` = all four, `state.lean` all on and
`runner: claude`; data dir redirected to a scratch directory). The profile picked is
`cursor`.

| persona | tier | explicit model? | model sent | effort sent | clamped from |
|---|---|---|---|---|---|
| implementer | judge | yes (cursor.yaml) | `sonnet` | high | - |
| conflict_resolver | judge | yes | `sonnet` | high | - |
| spec_writer | judge | yes | `sonnet` | high | - |
| spec_reviewer | judge | yes | `sonnet` | high | xhigh |
| conformance_reviewer | judge | yes | `sonnet` | high | xhigh |
| test_reviewer | judge | yes | `sonnet` | high | xhigh |
| adversarial_reviewer | judge | yes | `sonnet` | high | xhigh |
| divergence_triager | judge | no | `claude-sonnet-5-5` | high | - |
| gate_triager | marshal | yes | `sonnet` | medium | - |
| review_triager | marshal | yes | `sonnet` | medium | - |
| review_marshal | marshal | no | `claude-haiku-5-5` | medium | - |
| findings_collator | marshal | no | `claude-haiku-5-5` | medium | - |

No persona runs Opus and none is sent an effort above `high`. The only Opus run a lean
campaign can make is the SPEC-0553 deep close pass (`adversarial_reviewer` at tier deep,
`claude-opus-5-5`/high), and only for a campaign raised by a hand-written `size:` / `tone:`
in the spec frontmatter; a default campaign never makes it.

**What defeats the tiers.** The `cursor` profile file names `model: sonnet` for nine of the
twelve personas, and `explicit_model` counts any `personas.<name>.model` above the builtin
layer as an override (SPEC-0553 Override rule). Under the user's config, `tiers` therefore
changes the model of only three personas (divergence_triager, review_marshal,
findings_collator); the two marshal triagers (`gate_triager`, `review_triager`) stay on
Sonnet instead of Haiku, and the judge personas keep the `sonnet` alias rather than the
pinned `claude-sonnet-5-5` (the same model family today; the alias follows the CLI's
default). `balanced`'s `spec_reviewer: {model: sonnet}` override is shadowed by the cursor
file here, and would defeat that persona's tier on its own under `balanced`. The admission
review (`nw spec review`, before any campaign exists) is not a campaign run and is not
affected by any lean flag. Getting the full tier effect on Claude would need the `model:`
keys removed from `cursor.yaml` (leaving only the `cursor_model` keys); that is the human's
file and is left as is.

## Alternatives considered

- **Overlay the request on the config** (`--lean-flags packet` adds packet to whatever the
  config has on): the lean arm would then depend on the live config at queue time, which is
  what the A/B must hold fixed. Rejected.
- **A new `agentic.lean_cohorts` config block with campaigns assigned by rule:** more
  machinery for no gain over an explicit flag at `nw queue add`. Rejected.
- **Make the cursor profile's `model: sonnet` not count as explicit under tiers:** changes
  SPEC-0553's override rule for every user and profile. Out of scope; recorded above.

## Acceptance criteria

- [x] `nw queue add SPEC-NNNN --lean --yes` pins `state.lean` = all four flags on,
      `state.lean_request` = `[tiers, packet, collator, sessions]` and `state.cohort` = `lean`,
      with the config's flags all off; the `open` event carries `lean`, `lean_request`,
      `cohort`.
- [x] `--lean-flags a,b` pins exactly those flags; an unmet dependency is switched off with
      the `effective_lean` reason on the `open` event (`lean_off`).
- [x] The opt-in replaces the config/project flags for that campaign only (config `tiers`
      on + `--lean-flags packet` → only `packet`), and the size/tone cap applies to a lean
      campaign (`--profile thorough` capped at the default).
- [x] `--cohort NAME` is stored and exported as the campaign column `cohort` (and
      `lean_request` as a json column); without an opt-in it only tags.
- [x] Bad input is refused before anything is written: `--lean` with `--lean-flags`, an
      empty or unknown flag, a malformed cohort.
- [x] A campaign queued without `--lean` / `--lean-flags` / `--cohort` has no new state or
      `open`-event keys and runs the same models as before, with the config flags off and on;
      lean and plain campaigns coexist in one queue.
- [x] The opt-in works for spec campaigns, idea campaigns and batches.
- [x] A lean campaign on Claude under the user's profile shape resolves the table above: no
      Opus, no effort above `high`; personas with an explicit profile model keep it.

## Test plan

- **Automated:** new `tests/test_agentic_lean_optin.py`: parsing and cohort validation;
  `effective_lean(request=)` replace + dependency rule + `None` identity; through
  `nw queue add` (FakeRunner queue): `--lean` pin and `open` event and export row,
  `--lean-flags tiers` driven to MERGED with tier models while the config is off, dependency
  reason, override of a config flag, profile cap, cohort-only tag, CLI refusals writing
  nothing, idea campaign, batch; the no-option regression test with config flags off and on
  (no new keys, today's models); a lean and a plain campaign side by side; the per-persona
  table for the user's `cursor` profile shape (copied from `test_agentic_effort_ceiling`)
  with `model_set: claude`, and a builtin-profile contrast where every persona takes its tier.
  Updated column pins in `tests/test_agentic_campaign_export.py`.
- **Manual:** the table above computed from the real `~/.config/nightwright/config.yaml`
  with `personas.build_request` (read-only; scratch data dir).
- **Regression guard:** the touched test files (`test_agentic_tiers`, `test_agentic_lean_queue`,
  `test_agentic_campaign_export`, `test_agentic_queue_cli`, `test_agentic_queue`,
  `test_agentic_idea_campaign`, `test_agentic_effort_ceiling`, `test_agentic_collator`,
  `test_agentic_sessions`, `test_agentic_split_queue`, docs tests) locally, then one
  `nw gate run --tier close` on the box.

## Clock Log

## Rollout / migration

Additive. Queued and running campaigns have no `lean_request` and keep their pinned flags;
nothing is migrated. To start the A/B: queue lean campaigns with `--lean --runner claude`
next to normal ones and compare by `cohort` in `nw campaign export`.

## Definition of done

Per AGENTS.md.

## As shipped

- **Code.** `src/nightwright/agentic/tiers.py` (`lean_request`, `campaign_cohort`,
  `effective_lean(..., request=)`), `store.py` (`create_campaign` pins from
  `state.lean_request`; `open` event keys), `queue.py` (`_campaign_lean_pin`,
  `_pin_lean_request`; `plan_add`, `plan_add_idea`, `plan_add_batch`, `apply_add_batch`),
  `cli_queue.py` (`--lean`, `--lean-flags`, `--cohort`), `campaign_export.py` (`cohort`,
  `lean_request` columns). `plan_merge` is unchanged (non-goal).
- **CLI form.** `nw queue add SPEC-NNNN --lean --runner claude --yes`;
  `nw queue add SPEC-NNNN --lean-flags tiers,packet --cohort tiers-packet --yes`.
- **Tests.** `tests/test_agentic_lean_optin.py` (20) pass; the touched files above pass
  locally (641 passed, 3 skipped, after the two export column-position pins were updated).
- **Real-config table.** Computed as in Design (scratch script around
  `personas.build_request`); identical to the table the test pins for the copied profile
  shape.
- **Gate.** See below.
- **Clock.** No `nw idea clock-in`: that writes the org files, which this lane may not touch.

## Open questions

None.
