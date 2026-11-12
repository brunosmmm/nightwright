---
id: SPEC-0557
title: Effort ceiling — no xhigh/max persona run on any backend
status: done
owner: user
created: 2026-11-11
parent: SPEC-0552
tags: [agentic, cost, profiles]
source_idea: IDEA-1297
---

## Context

Child of [SPEC-0552](./0552-leaner-agentic-campaign-loop.md). Campaign export, 2026-11-11
(successful Claude runs):

| role | model / effort | runs | mean $ | median out tok | median cache read |
|---|---|---|---|---|---|
| reviewer | opus high | 1,739 | 0.66 | 4.7k | 174k |
| reviewer | sonnet xhigh | 147 | 0.81 | 24.8k | 760k |
| reviewer | sonnet medium | 316 | 0.10 | 1.2k | 29k |
| implementer | opus high | 205 | 1.50 | 9.8k | 825k |
| implementer | sonnet high | 513 | 0.49 | 6.2k | 476k |

Sonnet at `xhigh` costs more per run than Opus at `high`: its per-token price is half, but it
writes ~5x the output and reads ~4x the context. All 146 Sonnet-xhigh reviewer runs since
2026-11-10 came from the `cursor` profile, whose reviewers are `sonnet`/`xhigh` (mapped to
`grok-4.7-xhigh` on Cursor) and ran literally as Sonnet xhigh whenever they went through
Claude. The user's config also pins `cursor_model: grok-4.7-xhigh` on reviewers and
`effort: xhigh` on `spec_reviewer`.

The human decided (2026-11-11): no `xhigh` on any backend, Cursor included.

## Goals / Non-goals

**Goals**
- No persona run on any backend (claude, cursor, pi) is sent above effort `high`, whatever
  the profile, model set, tier map, overlay or explicit `cursor_model` id says.
- Each lowered run says so in its run record.

**Non-goals**
- Changing which model a persona uses (SPEC-0553 owns tiers).
- Lowering reviewers below `high` (a measured question for SPEC-0552's post-merge study).
- Editing the user's `config.yaml`; the ceiling applies over it.

## Decision

One ceiling, `agentic.effort_ceiling`, default `high`, applied once when a run's model and
effort are resolved, before any backend mapping, and again on an explicit backend model id.
On by default (not behind an `agentic.lean.*` flag): it is a cost fix, not an experiment.
Only the human can raise it, by editing `agentic.effort_ceiling` in their config; there is no
spec-level or relayed raise. `null` turns it off.

## Design

- `profiles.EFFORTS` order (`low < medium < high < xhigh < max`) defines the clamp;
  `clamp_effort(effort, ceiling) -> (effort, clamped_from|None)` in a small helper module.
- Claude: the `--effort` sent to the CLI is the clamped effort.
- Cursor: clamp the effort before `cursor_map.map_model` alias lookup; an explicit
  `cursor_model` id whose effort suffix or SDK effort parameter is above the ceiling
  (e.g. `grok-4.7-xhigh`, `gpt-5.6-sol-xhigh`) is rewritten to the ceiling variant
  (`grok-4.7-high`); SDK runs send the clamped effort parameter (`sdk_models.yaml`).
- Pi: the `thinking` level is clamped the same way (`pi_map.map_pi_model`).
- Run record / ledger `run.start` gains `effort_clamped_from` when lowered (absent
  otherwise); the campaign export gets an `effort_clamped_from` column.
- Applies to every persona, including the hand-raised deep close pass of SPEC-0553.

## Alternatives considered

- Edit the profiles and the user's config to drop `xhigh`: misses overlays, model sets and
  future profiles, and touches the human's checkout. Rejected.
- Per-model ceilings (Sonnet only): Cursor `xhigh` is also out per the human. Rejected.

## Acceptance criteria

- [x] With the default config, no persona run on claude, cursor or pi is sent an effort
      above `high`, for every built-in profile and the user's `cursor` / `balanced` /
      `thorough` profiles as configured.
- [x] An explicit `cursor_model: grok-4.7-xhigh` (or any `*-xhigh` / `max` id) runs as the
      `high` variant.
- [x] A lowered run's record carries `effort_clamped_from`; the export shows it.
- [x] `agentic.effort_ceiling: null` restores today's behaviour exactly (identity test).
- [x] One real call per backend that has credentials confirms the lowered id/effort is
      accepted (claude required; cursor/pi noted if credentials are unavailable).

## Test plan

- **Automated:** new `tests/test_agentic_effort_ceiling.py`: clamp helper; every persona x
  {claude, cursor, pi} x built-in profiles x model sets → effort ≤ high; explicit
  `cursor_model` xhigh/max ids rewritten; Pi thinking clamped; `effort_clamped_from`
  recorded and exported; `null` ceiling identity against today's resolution.
- **Manual:** one real claude call at a lowered effort; cursor/pi where credentials exist.
- **Gate:** touched tests locally, then one `nw gate run --tier close` on the box.

## Clock Log

## Rollout / migration

Default on at merge. Running campaigns pick the ceiling up on their next run start; no
state migration.

## Definition of done

Per AGENTS.md.

## As shipped

- **Module** `src/nightwright/agentic/effort_ceiling.py`: `ceiling(cfg)`, `clamp_effort(effort,
  limit) -> (effort, clamped_from|None)`, `clamp_cursor_id(id, limit)` and `apply(...)`.
  `agentic.effort_ceiling` is in `DEFAULT_CONFIG` as `high`; a cfg with no key also reads as
  `high`, an explicit `null` is off (every function then returns its input unchanged). An
  unknown value raises `ValueError`.
- **Clamp points (decided).** The effort is clamped in two places, both through the one helper
  and both in the two resolvers that build a `RunRequest` (`personas.build_request`,
  `spec_review`): (1) the profile effort right after `persona_settings`, before any backend
  mapping, so the `cursor_map` / `pi_map` alias lookup, `model_sets.bind` and the tier overlay
  all see the lowered effort; (2) one final `apply` pass after the tier overlay, which clamps
  the effort again (a tier row can carry `xhigh`), rewrites a Cursor id whose trailing word is
  above the ceiling (`grok-4.7-xhigh` -> `grok-4.7-high`, `gpt-5.6-sol-xhigh` ->
  `gpt-5.6-sol-high`, `-max`, `-extra-high`, and `-xhigh-fast` keeps `-fast`; this covers
  explicit `cursor_model`, model-set ids and tier ids alike) and clamps `pi_thinking`.
  `cursor_map.map_model` and `pi_map.map_pi_model` are untouched. The Cursor SDK effort
  parameter is derived from the id suffix (`sdk_selection`), so it is lowered with the id;
  every catalog id with an xhigh/max variant also lists `high` (tested).
- **Record.** `RunRequest.effort_clamped_from` (the first lowered value, e.g. `xhigh`) is
  written to `run.json`, `request.json`, the remote job doc and the `run.start` ledger event,
  only when set, and read back by `rebuild_request` / `request_from_job`. The campaign export
  has an `effort_clamped_from` run column (from `run.start`). The spec-review run record
  carries it too.
- **Tests** `tests/test_agentic_effort_ceiling.py` (47): helper; every persona x {claude,
  cursor, pi, model sets claude/grok/gpt/mixed} x all profiles (built-in plus the copied
  user shape: `cursor` profile file and `balanced` / `thorough` overrides with
  `cursor_model: grok-4.7-xhigh`, `spec_reviewer` `sonnet`/`xhigh`) x tiers on/off; a guard
  that the unclamped baseline does contain `xhigh`; explicit ids; tier overlay; Pi map
  `xhigh`; `null` identity against `mapped_model` / `map_pi_model` with the raw effort;
  record, request round-trip, `run.start` and export through a FakeRunner queue.
- **Manual.** One real call, `claude -p --model claude-sonnet-5-5 --effort high` with a
  one-line prompt (what a clamped `sonnet`/`xhigh` reviewer is now sent): accepted, ended
  `end_turn`, cost $0.053. Cursor and Pi: **unverified**; `CURSOR_API_KEY` resolves only
  through `pass:cursor/sdk` (pinentry) and no Pi provider key is in the environment. Their
  lowered ids are covered by the catalog test (`grok-4.7-high` and `gpt-5.6-sol-high` are
  legal catalog efforts) and the Pi map already has `high` rows.
- **Gate.** The first close gate (GATEJOB-2451) failed on one test that pinned an explicit
  `effort: max` (`test_agentic_persona_run_settings.py`); its expectation is now `high`. After rebasing onto main (62937eb6) the close gate
  GATEJOB-2463 passed (spec-lint, docs-tests, pytest).

## Open questions

None.
