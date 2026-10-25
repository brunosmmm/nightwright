---
id: SPEC-0238
title: "`wt profiles list/show/check` with per-value provenance; sources recorded on campaigns and runs"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, profiles, cli, config]
source_idea: IDEA-098
depends_on: [SPEC-0237]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), second half
of `IDEA-098`. [SPEC-0237](./0237-workflow-profiles-as-yaml-files-builtin-user-project.md)
moves profiles into layered YAML files: builtin, `<config_dir>/profiles`, `config.yaml`
`agentic.profiles`, and `<repo>/.wt/profiles`. It exposes `profile_chain(cfg, name, *,
project_root)` (the `(origin, delta)` pairs, lowest first), `load_layers` and
`known_profiles`, and returns `sources` on every resolved profile. Today nothing shows a human
which values a profile resolves to, or which file set each one. On 2026-10-25 the steward
claimed a `gate_triager` model change was live when it was not, and nothing let anyone check.
Campaigns record only the profile name, so the files that shaped a run are lost once they
change.

## Goals / Non-goals

**Goals**
- G1. `wt profiles list` shows every profile visible from the current project, with its status
  (ok or the validation error). `wt profiles show NAME` shows every resolved value next to the
  source that set it. `wt profiles check` exits non-zero when any profile fails.
- G2. The resolved `sources` are recorded on the campaign (`state.profile_sources` and the
  `open` event) and on each run (`run.json` `profile` / `profile_sources`, `run.start`
  `profile`).

**Non-goals**
- Editing profiles from the CLI.
- Showing historical profile values. Only the sources are recorded, not the file contents.
- SPEC-0219's `agentic.profile_matrix` display.

## Decision

Add `profiles.profile_provenance` and a new `cli_profiles.py` with the `wt profiles` group
(`list`, `show`, `check`), registered in `agentic/cli.register`. `plan_add` / `apply_add` copy
the resolved `sources` into the state and the `open` event. `RunRequest` gains `profile` and
`profile_sources`, filled by `PS.build_request` and `run_spec_review` and written by
`runner.write_run_record`.

## Design

### Provenance (`profiles.py`)

`profile_provenance(cfg, name, *, project_root=None) -> dict[str, str]` walks
`profile_chain` and records, for each leaf key path, the origin of the last delta that set it.
Key paths are dot-joined, for example `personas.implementer.model` or `review.weights.major`.
Lists (`reviewers`) are leaves, because `deep_merge` replaces them whole. The keys are exactly
the leaf paths of `resolve_profile`'s result, minus `name`, `description` and `sources`.
Origins are shortened for display only. The JSON keeps the full origin:
- `builtin:balanced.yaml` → `builtin`;
- the user directory → `~/.config/wt/profiles/<f>` (home abbreviated);
- `config.yaml:agentic.profiles.<n>` → `config.yaml`;
- a project file → `<project>:.wt/profiles/<f>`.

### CLI (`cli_profiles.py`)

- The project context for all three commands: `--project P` (via `P.project_root(cfg, P)`),
  else `P.find_project_for_cwd(cfg)`, else none (no project layer). The header line names it.
- `wt profiles list [--project P] [--json]`:
  - Table columns: `name`, `from` (the scope of the top-most layer: builtin, user, config or
    project), `extends`, `slots`, `loops`, `cap`, `implementer`, `reviewers`, `status`. The
    implementer cell is `model/effort`. The status cell is `ok`, or the first error in red.
    A `*` marks `agentic.default_profile`.
  - `--json`: `{"schema": "wt.profiles.v1", "project", "default", "profiles": [{name, scope,
    sources, ok, error, slots, loop_max, campaign_cap_usd, reviewers, description}]}`.
  - Exit 0 even with broken profiles. Listing is for looking.
- `wt profiles show NAME [--project P] [--json]`:
  - A table of `key`, `value` and `source`, in the profile's key order, followed by the
    `sources` chain.
  - `--json`: `{"schema": "wt.profile.show.v1", "name", "project", "profile":
    <resolve_profile result>, "provenance": {path: origin}, "sources": [...]}`.
  - An unknown or invalid profile raises ClickException with the resolver's message (exit 1).
- `wt profiles check [--project P] [--json]`:
  - Resolves every `known_profiles` name and also resolves `agentic.default_profile`.
  - Prints `✓ <name>` or `✗ <name>: <error>` per profile.
  - Exits 1 when any fails. `--json` gives `{"schema": "wt.profiles.check.v1", "ok",
    "results": [{name, ok, error}]}`.
- Registration: `agentic/cli.register` adds `profiles_group` with
  `cli.add_command(cli_profiles.profiles_group)`. Its docstring has the three-line usage
  block in the style of `queue_group`.

### Recording

- `queue.plan_add`: the plan gains `profile_sources` (the resolved profile's `sources`).
  `apply_add` writes `state.profile_sources`. `store.create_campaign` adds `profile_sources`
  to the `open` event as an optional extra (`schemas.EVENT_KEYS["open"]` unchanged).
- `runner.RunRequest` gains `profile: str | None = None` and `profile_sources: tuple = ()`.
  The defaults keep every existing constructor call valid. `PS.build_request` sets them from
  `profile["name"]` / `profile.get("sources")`, and so does `spec_review.run_spec_review`.
- `runner.write_run_record` adds `"profile"` and `"profile_sources"` to `run.json`. This is
  additive under `wt.run.v1`, so `schemas.RUN_KEYS` is unchanged. `queue._prepare_run` adds
  `profile` to the `run.start` payload as an extra.
- `wt campaign show`: the header's `profile <name>` becomes `profile <name> (<short sources
  joined by " + ">)` when `profile_sources` is present. Campaigns from before this spec show
  the name alone.

## Alternatives considered

- **Embed the resolved profile values in `state.json`**: rejected. That duplicates about 2 KB
  per campaign and would compete with the live profile as the authority, since the queue
  resolves profiles each tick. The sources say where to look. Full snapshots can follow from
  evidence.
- **`wt profiles list` exiting non-zero on errors**: rejected. A list command that fails is
  surprising and noisy in scripts. `check` is the explicit gate.

## Acceptance criteria

- [ ] `profile_provenance` returns, for every leaf path of the resolved profile, the origin of
      the layer that set it last. That holds for builtin-only, user-file, `config.yaml` and
      project overrides, and for a list replaced at a higher layer.
- [ ] `wt profiles list` shows every known profile with scope, key settings, the default marker
      and an ok or error status. `--json` emits `wt.profiles.v1`. It exits 0 with a broken
      profile present.
- [ ] `wt profiles show NAME` prints every resolved key with its value and short source, and
      `--json` emits `wt.profile.show.v1` with full origins. An unknown or invalid name exits 1
      with the resolver's message. `--project` and cwd detection select the project layer.
- [ ] `wt profiles check` exits 0 when every profile and the default resolve. It exits 1 and
      names the profile and key path otherwise, and `--json` emits `wt.profiles.check.v1`.
- [ ] A campaign created by `wt queue add --yes` has `state.profile_sources`, and its `open`
      event carries `profile_sources`. Ledgers without it still pass `schemas.ledger_problems`.
- [ ] `run.json` of a persona run has `profile` and `profile_sources`, and the `run.start`
      event has `profile`. The same holds for a `wt spec review` run. Existing `RunRequest`
      constructions in tests still work unchanged.
- [ ] `wt campaign show` prints the sources after the profile name, and the name alone for an
      old campaign.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_profiles.py`: provenance cases (builtin, user file, `config.yaml`,
    project, list replacement, `extends` chain).
  - `tests/test_agentic_profiles_cli.py` (new, `CliRunner`, temp `WT_CONFIG_DIR` and project
    root):
    - `list` table and `--json` with an ok profile and a broken one, plus the exit code;
    - `show` text and `--json`, and an unknown name;
    - `check` with exit 0 and 1, plus `--json`;
    - project selection by `--project` and by cwd.
  - `tests/test_agentic_queue.py`: `plan_add` / `apply_add` `profile_sources` in the state and
    the `open` event. `tests/test_agentic_logs.py`: an old `open` event still validates.
  - `tests/test_agentic_runner.py` / `tests/test_agentic_personas.py`: `build_request` sets
    `profile` / `profile_sources`, `write_run_record` writes them, and `run.start` carries
    `profile` (fake runner tick).
  - `tests/test_agentic_spec_review.py`: the spec-review `run.json` has `profile`.
  - `tests/test_agentic_campaign_cli.py`: the `wt campaign show` sources suffix, new and old
    campaigns.
- **Manual verification:**
  1. Run `uv run wt profiles check` and `uv run wt profiles show balanced` against the real
     config, and check that the source column names `config.yaml` for every key the user
     config overrides.
  2. From this repo's directory, `uv run wt profiles list` shows the project context
     `Meta-Tools`.
- **Regression guard:** full `uv run pytest`. All changes to records are additive.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None. Old campaigns and runs lack the new keys, and every reader treats them as absent. After
this lands, the `/wt-conduct` and `/wt-steward` skills can point at `wt profiles show` when
someone asks which model a persona uses. That skill wording is a doc edit, not required here.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
