---
id: SPEC-0237
title: "Workflow profiles as YAML files: builtin package data, user and project layers"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, profiles, config, yaml]
source_idea: IDEA-098
depends_on: [SPEC-0171, SPEC-0203, SPEC-0206]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-098` ("Workflow profiles defined in YAML files (builtin, user and project), not in
Python"). The human asked for this directly on 2026-10-25.

Workflow profiles ([SPEC-0171](./0171-workflow-profiles-campaign-store-ledger-locks-and.md)) are the
`BUILTIN_PROFILES` dict in `src/wt/agentic/profiles.py` (`balanced`, `frugal`, `thorough`):
slots, `loop_max`, `campaign_cap_usd`, `run_timeout_s`, `reviewers`, the `review` block
([SPEC-0203](./0203-review-triage-score-and-routing.md),
[SPEC-0206](./0206-review-triager-persona.md)) and per-persona `model / effort /
max_budget_usd / permission_mode / read_only`. The only YAML is the user's
`agentic.profiles.<name>` deltas in `config.yaml`. `resolve_profile(cfg, name)` deep-merges
those over the builtin of the same name, or over `balanced` for a new name. Persona settings
are not validated: a typo in a key or a model name is silently ignored or passed to `claude`.
A project cannot ship its own profile.

Other specs that touch profiles:
- [SPEC-0219](./0219-spec-sizing-and-tone-pick-the-campaign-profile.md) adds
  `agentic.profile_matrix` and `select_profile`, which checks cell values against
  `known_profiles(cfg)`.
- [SPEC-0226](./0226-runner-reloads-user-and-project-config-on-change.md) makes the runner
  reload `config.yaml` on change and validate it with `validate_runner_cfg`.

Both are accepted, not built. This spec fits with either landing order (see Design).

## Goals / Non-goals

**Goals**
- G1. The builtin profiles ship as YAML package data (`wt.profile.v1`), and the Python dict is
  removed. The resolved builtin values do not change.
- G2. Users add or override profiles in `<config_dir>/profiles/*.yaml`, projects in
  `<repo>/.wt/profiles/*.yaml`. Precedence: project > user > builtin. A file is either complete
  or a delta (`extends:`, or the same name as a lower layer). The `config.yaml`
  `agentic.profiles` deltas keep working.
- G3. Every profile source is validated: unknown keys, personas, models, efforts and permission
  modes, and wrong types, fail with `<file>: <key.path>: <problem>`. A broken file breaks only
  the profile it defines.
- G4. `resolve_profile` keeps its semantics and gains `project_root`. Every caller that knows the
  campaign's project passes it. The resolved profile lists its `sources`.

**Non-goals**
- `wt profiles list/show` and recording the sources on the campaign and in `run.json`: that is
  [SPEC-0238](./0238-wt-profiles-list-show-with-provenance-and-recorded-sources.md).
- Moving the persona registry (`personas.PERSONAS`) to YAML (`IDEA-090` groundwork).
- New profiles, or changes to any builtin value.
- Journaling profile-file changes in the runner. See the SPEC-0226 note below.
- `spec_review_max`, mentioned in the idea: no such setting exists today, so it is out of
  scope here.

## Decision

Add `src/wt/agentic/profile_defs/{balanced,frugal,thorough}.yaml`. `profiles.py` gets a loader
that reads four layers, lowest first:
1. builtin files;
2. `<config_dir>/profiles/*.yaml`;
3. `config.yaml` `agentic.profiles`;
4. `<project_root>/.wt/profiles/*.yaml`.

Each layer is validated by one function, `validate_profile_doc`. `profile_chain` lists the
deltas that make up a profile name, and `resolve_profile` folds them with the existing
`deep_merge`. Then the existing `_check_reviewers` / `_check_review` run, plus a completeness
check. `BUILTIN_PROFILES` is replaced by `builtin_profiles()`.

## Design

### Files and format

- Package data: `src/wt/agentic/profile_defs/<name>.yaml`, located like the persona templates
  (`Path(__file__).resolve().parent / "profile_defs"`). Hatch already ships every file under
  `src/wt`. The directory is named `profile_defs`, not `profiles`, so it cannot shadow or be
  confused with the `profiles.py` module.
- Each builtin file is complete and equals today's dict, key for key. Before the dict is
  deleted, a golden snapshot `tests/data/builtin_profiles_v1.json` is written from it.
- File format (`wt.profile.v1`):
  ```yaml
  schema: wt.profile.v1          # required in files
  extends: balanced              # optional
  description: "cheap model mix" # optional
  slots: 2
  loop_max: 3
  campaign_cap_usd: 10.0         # number > 0, or null for no cap
  run_timeout_s: 1800
  reviewers: [conformance_reviewer, test_reviewer]
  review: {pass_score: 80, weights: {must_fix: 100, major: 15, minor: 4, nit: 1}, triager: true}
  personas:
    implementer: {model: sonnet, effort: high, max_budget_usd: 4.0,
                  permission_mode: bypassPermissions, read_only: false}
  ```
- The profile name is the file stem and must match `^[a-z][a-z0-9_-]*$`. A `*.yml` file is an
  error ("rename to .yaml") rather than being silently ignored. Other files are ignored.
- User files: `Path(cfg["config_dir"]) / "profiles"`. Project files:
  `<project_root>/.wt/profiles`, where `project_root` is the project's **main checkout**
  (`pcfg["_root"]` / `state.repo.root`), never a campaign worktree. A campaign therefore
  cannot change its own profile by editing its branch.

### Validation (`validate_profile_doc(doc, *, origin, in_file) -> list[str]`)

Every message reads `"<origin>: <key.path>: <problem>"`. `origin` is the file path,
`builtin:<name>.yaml` or `config.yaml:agentic.profiles.<name>`. The rules:
- `doc` is a mapping. Top-level keys are a subset of `PROFILE_KEYS = {schema, extends,
  description, slots, loop_max, campaign_cap_usd, run_timeout_s, reviewers, review,
  personas}`.
- `schema == "wt.profile.v1"` is required when `in_file`, and allowed but not required in
  `config.yaml`.
- `extends` and `description` are strings.
- `slots`, `loop_max` and `run_timeout_s` are ints ≥ 1 (no bools). `campaign_cap_usd` is a
  number > 0 or null. `reviewers` is a list of strings.
- `review`: the existing `_check_review` rules, refactored to collect messages instead of
  raising on the first one.
- `personas` is a mapping:
  - each persona name is in `personas.PERSONAS`;
  - each persona's settings are a mapping with keys ⊆ `{model, effort, max_budget_usd,
    permission_mode, read_only}`;
  - `model` is in `MODEL_ALIASES = ("opus", "sonnet", "haiku")` or matches
    `^claude-[a-z0-9][a-z0-9.-]*(\[1m\])?$`;
  - `effort` ∈ `("low", "medium", "high", "xhigh", "max")`;
  - `max_budget_usd` is a number > 0;
  - `permission_mode` ∈ `("default", "acceptEdits", "bypassPermissions", "dontAsk", "plan")`;
  - `read_only` is a bool.
- YAML that does not parse is reported as `<origin>: <yaml error>`.

Completeness: a resolved profile must have every key of `REQUIRED_KEYS = (slots, loop_max,
campaign_cap_usd, run_timeout_s, reviewers, review, personas)` and settings for every persona
in `PROFILE_PERSONAS = tuple(personas.PERSONAS)`. A missing one raises
`profile <name> (<top origin>): missing <key.path> (add it or `extends: <profile>`)`.
`_check_reviewers` and `_check_review` then run on the merged result, as today.

### Layers and resolution (`profiles.py`)

- `load_layers(cfg, *, project_root=None) -> list[Layer]`. `Layer(scope, defs)` has `scope` in
  `builtin | user | config | project`, and `defs: {name: Def(origin, doc, errors)}`. Every file
  is read and validated on each call; nothing is cached across calls. The files are a few
  small YAML documents, and `_Ctx` already caches resolved profiles for one tick.
- `profile_chain(cfg, name, *, project_root=None) -> list[tuple[str, dict]]` lists
  `(origin, delta)` pairs, lowest first. To find `name` at layer index `i` it takes the
  highest layer ≤ `i` that defines `name`, then:
  - a `Def` with `errors` raises ValueError joining them;
  - `extends: X` with `X != name` puts the full chain of `X`, from the top layer, before this
    delta;
  - `extends: name`, or no `extends` while a lower layer defines `name`, puts the chain of
    `name` from the layers below first. That makes a same-named file a delta;
  - no `extends` and nothing below: the delta stands alone and must be complete. The one
    exception keeps today's behaviour: a `config`-scope definition with a new name sits over
    the chain of `balanced`;
  - an `extends` target that no layer defines raises `unknown profile X (extended by
    <origin>)`. A repeated `(name, layer)` raises `profile extends cycle: a → b → a`.
  - no layer defines `name` at all: ValueError `unknown profile 'x'; known: …`, the same text
    as today.
- `resolve_profile(cfg, name=None, *, project_root=None) -> dict`:
  1. `name` defaults to `agentic.default_profile` or `balanced`, as today;
  2. fold `deep_merge` over the chain's deltas, minus `schema`, `extends` and `description`;
  3. set `name`, `description` (top-most one) and `sources` (the chain's origins, lowest
     first, without duplicates);
  4. run the completeness check, `_check_reviewers` and `_check_review`.
- `known_profiles(cfg, *, project_root=None)`: the sorted union of the names defined in any
  layer, including broken ones (resolving those raises).
- `builtin_profiles() -> dict[str, dict]`: the resolved builtin layer, as deep copies.
  `BUILTIN_PROFILES` is deleted. The module docstring and the `config.py` comment on
  `agentic.profiles` describe the layers.

### Callers

- `queue._Ctx.profile(state)`: `resolve_profile(cfg, name,
  project_root=(state.get("repo") or {}).get("root"))`, cached per `(name, root)` for the tick.
  A profile file edit therefore takes effect at the next tick without a restart.
- `queue.plan_add`: `project_root=root`. `queue.queue_payload` (`loop_max`): `repo.root` of the
  row.
- `queue.slots_for(cfg, profile_name=None)`: unchanged, no project layer. Slots are a property
  of the whole queue, so a project file cannot change them.
- `spec_review.run_spec_review`: `project_root=root` (`spec_root(cfg, path)`, already computed).
- `cli_campaign` `prompt`: `project_root` from the campaign's `repo.root`.
- An invalid profile file raises ValueError with the message above, through the existing paths.
  `_tick` parks the campaign `open_failed` with that detail (`_apply`, `res.error`), and
  `plan_add` / `wt spec review` fail with it. No new park reason.
- **SPEC-0219**, if landed: `select_profile` gains `project_root=None`, forwarded to
  `known_profiles` when it checks matrix cells, and `plan_add` passes `root`. If this spec
  lands first, SPEC-0219's implementer adds that forwarding.
- **SPEC-0226**, if landed: `validate_runner_cfg` keeps calling `resolve_profile` without a
  project root. Profile files need no watch, because they are re-read every tick. Journaling
  their changes (`config_reloaded` for a profile path) is a separate follow-up, deliberately
  left out to keep this spec one campaign.

### Docs

`docs/product/capabilities/agentic.md` gets a "Profiles" subsection: the file locations, the
format, the precedence and an `extends` example. The existing doc tests
(`test_docs_list_the_test_reviewer` and the others) keep passing.

## Alternatives considered

- **Generate `BUILTIN_PROFILES` from YAML at import**: rejected. It adds file I/O at import and
  keeps a mutable module global that a test or caller can mutate. A function returning copies
  is safer, and the tests move to it.
- **Project files read from the campaign worktree**: rejected. An implementer could raise its own
  budget or cap by committing a profile file.
- **Accept any model string**: rejected. The idea asks for unknown models to fail. The alias
  list plus the `claude-*` pattern covers every model the CLI takes today, and a new alias is a
  one-line change.
- **Profiles in `.wt/project.yaml`**: rejected. Separate files keep the project schema small
  and let a profile be copied between user and project scope unchanged.

## Acceptance criteria

- [x] `src/wt/agentic/profile_defs/{balanced,frugal,thorough}.yaml` exist with
      `schema: wt.profile.v1`. `builtin_profiles()` equals `tests/data/builtin_profiles_v1.json`
      (today's dict). `BUILTIN_PROFILES` no longer exists in `src/` or `tests/`.
- [x] A user file and a project file each add a new complete profile and override a builtin by
      delta. Precedence is project > `config.yaml` delta > user file > builtin, and a key set at
      a higher layer wins. A `config.yaml`-only new name still merges over `balanced`.
- [x] `extends: X` builds on the effective `X`. `extends: <own name>` builds on the lower
      layers. A cycle raises with the chain, and an unknown `extends` target raises naming the
      file.
- [x] Each validation rule rejects with `<origin>: <key.path>`: unknown top-level key, unknown
      persona, unknown persona key, bad model, effort, permission mode or type, missing `schema`
      in a file, an incomplete profile with nothing to extend, a bad file name, and a `.yml`
      file. A broken file makes only its own profile fail to resolve, and other profiles still
      resolve.
- [x] `resolve_profile` keeps the default-name, unknown-name, reviewer and review semantics (the
      existing `tests/test_agentic_profiles.py` assertions pass, ported from `BUILTIN_PROFILES`
      to `builtin_profiles()`). It returns `sources` in layer order.
- [x] A campaign of project A uses A's `.wt/profiles/<name>.yaml` from A's main checkout. A
      campaign of another project and `slots_for` do not. An edit to the file is used by the
      next tick's `_Ctx`. A copy of the file in the campaign worktree is ignored.
- [x] An invalid profile file makes the next tick park an active campaign of that profile
      `open_failed` with the file and key path in the detail. `wt queue add` and
      `wt spec review` fail with the same message.
- [x] `uv build` produces a wheel that contains `wt/agentic/profile_defs/*.yaml`.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_profiles.py` (extended):
    - golden equality with `builtin_profiles_v1.json`;
    - layer precedence with temp `WT_CONFIG_DIR` user files, a `config.yaml` delta and a temp
      project root;
    - `extends` (other name, own name, cycle, unknown target);
    - one test per validation rule, asserting the origin and key path in the message;
    - broken-file isolation;
    - `sources` order;
    - the existing tests ported to `builtin_profiles()`.
  - `tests/test_agentic_queue.py`:
    - `_Ctx.profile` with a project file, against a campaign of another project;
    - a worktree copy of the file is ignored;
    - an edit between two `tick()` calls is picked up;
    - an invalid file parks `open_failed`;
    - `plan_add` error message.
  - `tests/test_agentic_spec_review.py`: `run_spec_review` (fake runner) uses the project
    layer's `spec_reviewer` model.
  - `tests/test_packaging.py` (new, marked slow if needed): `uv build --wheel` into a temp dir,
    then `zipfile` lists the three YAML files.
- **Manual verification:**
  1. Before merging, resolve every profile against the real config:
     `uv run python -c "from wt.config import load_config; from wt.agentic.profiles import
     resolve_profile, known_profiles; c = load_config(); print({n: resolve_profile(c,
     n)['sources'] for n in known_profiles(c)})"`.
  2. Check that it succeeds, and fix any user `agentic.profiles` key the new validation
     rejects. SPEC-0238 replaces this with `wt profiles check`.
- **Regression guard:** full `uv run pytest`. Queue, e2e, personas and spec-review tests pass
  unchanged apart from the `BUILTIN_PROFILES` → `builtin_profiles()` port.

### Test record (2026-10-25)

- `uv run pytest -q` (full suite): 2054 passed, 1 skipped.
- `tests/test_agentic_profiles.py` (extended): golden equality with
  `tests/data/builtin_profiles_v1.json`; precedence project > `config.yaml` > user > builtin;
  new complete profiles from a user and a project file; `extends` (other name, own name, cycle,
  unknown target); one case per validation rule asserting `<file>: <key.path>`; broken-file
  isolation; `sources` order; bad file name and `.yml`; the pre-existing tests ported to
  `builtin_profiles()`.
- `tests/test_agentic_queue.py`: `_Ctx.profile` with a project file against another project's
  campaign and `slots_for`; a worktree copy is ignored; an edit is seen by the next `_Ctx`;
  an invalid file parks an active campaign `open_failed` and fails an open before the worktree
  exists; `wt queue add` fails with the file message.
- `tests/test_agentic_spec_review.py`: `wt spec review` uses the project layer's `spec_reviewer`
  model, and fails on an invalid project file.
- `tests/test_packaging.py`: `uv build --wheel` lists the three `profile_defs/*.yaml` files
  (also checked by hand with `zipfile`).
- Manual: the resolve-everything one-liner against the real config succeeded:
  `balanced` (builtin + `config.yaml` delta), `frugal`, `thorough`; no user key was rejected.
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: exit 0.

### As shipped (2026-10-25)

Built to Design, with these details the Design left open:
- The builtin files also carry a `description:`; `builtin_profiles()` strips `name`,
  `description` and `sources`, so it still equals the golden snapshot. A resolved profile's
  `description` is the top-most one, or `""`.
- `validate_profile_doc` checks each document on its own, so a delta may omit `review.weights`
  keys; the merged profile must still have all four (`_check_review`).
- Resolution order after folding: required top-level keys, then `_check_reviewers` and
  `_check_review`, then the per-persona completeness check. A reviewer without settings
  therefore reports `no persona settings`, as before.
- `_do_open` resolves the profile first, so an invalid file fails the open before the worktree
  is created; `_apply` tolerates a profile that fails to resolve when the step already carries
  that error (the park path needs no profile).
- A `*.yml` file or a stem that is not a valid name is a broken definition under that stem
  (`<file>: <file>: …`), and shows in `known_profiles`.
- `tests/test_packaging.py` is not marked slow (the build takes about a second).

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

No data migration. Existing `config.yaml` deltas resolve as before. The one visible change is
that invalid keys or values in them, ignored until now, now raise. That is why the manual step
runs against the real config before merging. Campaign state keeps storing only the profile
name.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
