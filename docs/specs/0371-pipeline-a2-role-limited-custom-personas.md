---
id: SPEC-0371
title: "Pipeline A2a: role-limited custom personas (user/project definitions, role schemas, validator, nw personas validate)"
status: proposed
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0348
tags: [agentic, personas, project-config, validation]
source_idea: IDEA-716
---

## Context

Child of epic [SPEC-0348](./0348-declarative-per-project-agentic-pipeline.md) (Stage A, child
"Pipeline A2"), promoted from `IDEA-716`. The idea was too large for one campaign (three
subsystems, 15 acceptance criteria), so it is split in two. **This spec is the first half, A2a:
the declaration layer.** The second half, A2b (profiles reference declared personas), is listed
under Non-goals and gets its own spec that `depends_on` this one. Together they absorb slices 1-2
of `IDEA-090`; slice 3 (the queue running custom personas) is Pipeline A3.

Today a project that wants a security or accessibility reviewer must patch nightwright:
`src/nightwright/agentic/personas.py` holds `PERSONAS`, `REVIEWER_PERSONAS`, `ADOPT_REGISTRY`
and `PLACEHOLDERS` as hand-written tables.

Pipeline A1 (the persona registry as data: builtin personas in `persona_defs/*.yaml`, a layered
loader, a strict validator, `nw personas list|show`) is the base this spec extends. A1 has no
spec id yet, so this spec cannot name it in `depends_on`; the sequencing is recorded in the
epic's breakdown and in Rollout below. This spec assumes only what the epic states A1 delivers:
a registry that loads `nw.persona.v1` YAML from the builtin layer and exposes
`PERSONAS` / `REVIEWER_PERSONAS` / `ADOPT_REGISTRY` as derived views, plus `nw personas list|show`.

The reviewers' output schemas differ today (`CONFORMANCE_REVIEWER_SCHEMA`,
`TEST_REVIEWER_SCHEMA`, `ADVERSARIAL_REVIEWER_SCHEMA`); all share `verdict`, `score`,
`findings[]` (`severity`, `title`, optional `detail`/`files`) and `fix_brief`, which is what
`triage_reviews` (SPEC-0203) consumes. `ADVERSARIAL_REVIEWER_SCHEMA` is the one that also carries
a per-finding `category` drawn from `ADVERSARIAL_CATEGORIES`, so it is the model for the custom
reviewer schema.

## Goals / Non-goals

**Goals**
- A user (config dir) or a project (`.nw/personas/` in its main checkout) declares personas with
  `role: implementer | reviewer | triager` in `nw.persona.v1` YAML; the role, not the file, fixes
  the output schema and `read_only`.
- Validation is strict and reports every problem at once: schema id, name, role, template
  existence, placeholder subset, `category`, and builtin-name shadowing.
- `nw personas validate` checks every definition visible from a project and exits non-zero on any
  problem.
- Declared personas are available through registry functions (`load_registry`, `personas_for`,
  `reviewer_names`, `get_persona(name, project_root)`) and show up in `nw personas list|show`
  (A1's commands) with their origin layer.

**Non-goals**
- **Pipeline A2b: profiles reference declared personas** (separate spec, `depends_on` this one).
  Everything touching `profiles.py` and the profile layers is deferred to it: `reviewers:` and
  `personas:` accepting declared names in `validate_profile_doc` / `_check_reviewers`; the
  role-based "not a reviewer persona" error; the rule that every profile layer (builtin, user,
  project, `config.yaml` delta) is validated against the registry of the project in scope
  (builtin plus user personas when `project_root` is None, plus that project's personas
  otherwise, so a user profile naming a project-declared persona is valid only from that project
  and `nw profiles check` outside a project reports `unknown persona`); `resolve_profile` writing
  a missing `personas.<declared name>` entry as a copy of the role-default persona's settings
  (`conformance_reviewer` / `implementer` / `gate_triager`, `read_only: true` for read-only
  roles) with `profile_provenance` labelling them `role-default:<persona>`; the not-yet-run
  warning in `nw profiles check`; and making `profiles.reviewers_of(profile)` return only entries
  in `PS.REVIEWER_PERSONAS` (falling back to `["conformance_reviewer"]`) until A3, so that a
  declared reviewer in a profile never makes `queue tick` raise in `get_persona` or wait forever
  in `_pending_reviewers`. A2b must ship a real `queue.tick` test for that.
- Running custom personas in the queue: review fan-out, triage, divergence, adoption, watch chips
  and the `config_invalid` park for a broken declaration are Pipeline A3.
- New output schemas, new roles (spec writer, conflict resolver, new triage kinds), or any
  override of builtin personas. A definition cannot set its own schema or flip `read_only`.
- Pipeline definitions (Stage B). Moving builtin personas to YAML (A1).

## Decision

Extend A1's layered persona registry with two non-builtin layers, user and project, and a
role-aware validator. Roles are a closed set that maps to an existing output schema and
`read_only` value; a persona's only schema freedom is a `category` vocabulary on reviewer-role
personas. Declaration problems are a validation result (a list of messages), never an exception
at import time, so a broken project file cannot take down the CLI or other projects.

**Inertness.** This spec edits neither `profiles.py` nor `queue.py`. The module-level
`PERSONAS`, `REVIEWER_PERSONAS` and `ADOPT_REGISTRY` stay builtin-only, and profile validation
still rejects any `reviewers:` / `personas:` name that is not builtin. A declared persona is
therefore reachable only through the new registry functions and the CLI, and cannot reach the
queue (`profiles.reviewers_of` returns `profile["reviewers"]` as written, and `build_request`'s
`get_persona(persona)` knows only builtins, so the queue must never see a declared name until A2b
and A3 handle it).

## Design

### Definition file

```yaml
# <project main checkout>/.nw/personas/security_reviewer.yaml
schema: nw.persona.v1
name: security_reviewer            # must equal the file stem
role: reviewer                     # implementer | reviewer | triager
template: security_reviewer.md     # resolved next to the YAML file
description: "crypto misuse, secrets, injection"
category: [crypto, secrets, injection]   # reviewer role only; optional
```

Allowed keys: `schema`, `name`, `role`, `template`, `description`, `category`. Any other key,
including `schema_ref`, `output_schema`, `read_only`, `invariant`, is an "unknown key" error.

### Layers

Precedence for lookup is builtin, then user, then project, but no layer may redefine a name from
a lower one: a user file named like a builtin, or a project file named like a builtin or a user
persona, is an error (not an override). Locations:

- user: `<config dir>/personas/*.yaml` (the same config dir the profile loader uses for user
  profiles);
- project: `<project main checkout>/.nw/personas/*.yaml`, always read from the main checkout and
  never from a campaign worktree, so a campaign cannot rewrite the persona that judges it. The
  loader takes the project root as an explicit argument (the same convention as
  `profiles`'s `project_root`).

Templates are resolved in the directory of the YAML file. A template path with a separator or
`..`, or one that does not resolve to a regular file inside that directory, is an error.

### Roles

| role | output schema | read_only | notes |
|---|---|---|---|
| `implementer` | `IMPLEMENTER_SCHEMA` | false | writable like the builtin implementer |
| `reviewer` | the custom reviewer schema (below) | true | the role A2b makes eligible for `reviewers:` |
| `triager` | `GATE_TRIAGER_SCHEMA` | true | |

The custom reviewer schema is `ADVERSARIAL_REVIEWER_SCHEMA` built through a function
`reviewer_schema(categories)` in `personas.py`, with the finding `category` enum replaced by
`categories + ["other"]`. When `category` is omitted the enum is `ADVERSARIAL_CATEGORIES`. The
`category` values are lowercase slugs matching `^[a-z][a-z0-9_]{0,31}$`, unique, at most 24
entries; `other` is implicit and listing it is allowed but not duplicated. `category` on a
non-reviewer role is an error. The role's `invariant` is the builtin default `INVARIANT`; a
definition cannot set one.

### Names

`name` matches `^[a-z][a-z0-9_]{1,39}$` and equals the file stem. The builtin names
(`implementer`, `gate_triager`, `conformance_reviewer`, `test_reviewer`, `adversarial_reviewer`,
`spec_reviewer`, `spec_writer`, `review_triager`, `conflict_resolver`, and whatever A1 adds to the
registry) are reserved: shadowing is refused with `name 'x' is a builtin persona; pick another
name`. Reserved names are read from the registry's builtin layer, not duplicated in this module.

### Placeholder subset

A template may use only `{{name}}` placeholders in its role's subset of `PLACEHOLDERS`; an unknown
or out-of-role `{{name}}` is an error that names the placeholder, the line, and the role.
Subsets are defined once as a table `ROLE_PLACEHOLDERS` in `personas.py`, derived from the
placeholders the builtin template of the same role uses today plus the always-available campaign
context (`campaign`, `spec_id`, `spec_rel_path`, `spec_text`, `worktree`, `base`, `loop`,
`loop_max`, `context_files`, `schema_text`, `human_overrides`):

- `implementer`: the common set plus `gate_results`, `pending_brief`, `previous_verdicts`;
- `reviewer`: the common set plus `diff`, `gate_results`, `previous_verdicts`, `test_files`,
  `tests_touched`, `changed_files`, `campaign_diff`;
- `triager`: the common set plus `gate_results`, `triage_input`.

A test pins each subset to be a superset of the placeholders in the corresponding builtin
templates, so the builtins stay valid under the same rule. Placeholders used only by
`spec_writer`, `spec_reviewer` or `conflict_resolver` (`idea_text`, `template_text`,
`write_problems`, `write_rules`, `epic_text`, `rebase_target`, `conflicts`, `base_diff`,
`landed`, `regenerate`) are in no custom role's subset.

### Validator

`validate_persona_defs(project_root=None, *, user_dir=None)` returns a list of messages
`"<origin>: <path>: <problem>"`, where origin is `user:<file>` or `<project>:.nw/personas/<file>`
(the profile validator's message shape). It never raises for a bad definition; an unreadable or
non-mapping YAML document is a message. It checks, per file: YAML parses to a mapping; `schema`
is `nw.persona.v1`; keys are known; `name`, `role`, `template`, `category` as above; template
exists, is UTF-8, and passes the placeholder rule; plus cross-file: duplicate names across the
user and project layers, and builtin shadowing. Loading (`load_registry(project_root)`) returns
the builtin registry plus every **valid** custom definition; invalid ones are omitted from the
registry and reported by the validator, so one broken file does not hide the others.

### Registry views

`PERSONAS`, `REVIEWER_PERSONAS` and `ADOPT_REGISTRY` (A1) stay builtin-only module values, so
every existing import and test is unchanged. New functions take the project root:
`personas_for(project_root)`, `reviewer_names(project_root)` (reviewer-role entries, builtin
first in canonical order, then custom sorted by name) and `get_persona(name, project_root=None)`
which returns custom personas too. The `Persona` dataclass gains no field a builtin would need
changed; custom personas are `Persona` instances built with the role's schema, `read_only` and the
default invariant, with `template_file` set to the path of the template file.
The `template` property accepts an absolute `template_file` for these. Existing callers of
`get_persona(name)` without a project root keep seeing builtins only.

### CLI

- `nw personas validate [--project NAME] [--json]`: validates the builtin layer, the user layer
  and, if a project is in scope (`--project`, else the cwd's project, as `nw profiles` resolves
  it), that project's layer. Prints one line per problem, `ok` when clean; exit 1 on any
  problem, 0 otherwise. `--json` emits `{"ok": bool, "problems": [...], "personas": [...]}`.
- `nw personas list|show` (A1) gain a `layer` column / field (`builtin|user|project`) and show
  `role`, `category` and the template path for declared personas. Broken definitions appear in
  `list` marked `invalid`, mirroring `nw profiles list`.
- Tab completion (`completion.py`) for persona names includes declared ones.

### Edge cases

- Empty `.nw/personas/` or a missing one is fine. Non-`*.yaml` files are ignored.
- Two files with the same stem in different layers: the name error above.
- A symlinked definition or template that resolves outside its directory is an error.
- A persona declared in a project is not visible to other projects.

## Alternatives considered

- **Let a definition carry its own JSON schema** — rejected by the epic (G1): `triage_reviews`,
  adoption and the TUI read fixed finding fields; an arbitrary schema breaks them.
- **Allow overriding builtin personas by name** — would let a project weaken the conformance
  reviewer silently; shadowing is refused instead, and projects add reviewers next to them.
- **Raise at import time on a broken project file** — a typo in one project would break the CLI
  for all projects; a validation result keeps it local.
- **One placeholder set for all roles** — lets a reviewer template reference writer-only context
  that is empty for it; the per-role subset catches this at validation time.
- **Ship profile integration in the same spec** — rejected: it is a separate subsystem
  (`profiles.py`, per-layer registry rules, `resolve_profile` defaults, the `reviewers_of`
  filter that keeps the queue safe) and pushed the spec to 15 criteria; it is A2b.

## Acceptance criteria

- [ ] A valid `.nw/personas/security_reviewer.yaml` (role `reviewer`, template, `category`) in a
      project's main checkout is loaded by `load_registry(project_root)`; the persona is
      read-only with an output schema whose finding `category` enum is the declared values plus
      `other`, and a reviewer definition without `category` gets the `ADVERSARIAL_CATEGORIES`
      enum.
- [ ] `implementer` and `triager` definitions get `IMPLEMENTER_SCHEMA` / `GATE_TRIAGER_SCHEMA`;
      the implementer is writable, the others read-only.
- [ ] A definition naming a builtin persona (in the user or project layer), or duplicating a name
      across user and project layers, is refused with a message naming both files/layers.
- [ ] A definition with an unknown key (including `read_only`, `schema_ref`, `output_schema`,
      `invariant`), a bad `schema`, `role`, `name`, `template`, or a `category` on a
      non-reviewer role is refused with a message naming the file and key.
- [ ] A template using a `{{placeholder}}` outside the role's subset, or an unknown one, is
      refused naming the placeholder and role; all problems across all files are reported in one
      run, and a broken file is omitted from `load_registry` while a good sibling loads.
- [ ] A template path escaping its directory (separator, `..`, or a symlink out) is refused.
- [ ] Project definitions are read from the project's main checkout path passed in, and a file
      under a campaign worktree's `.nw/personas/` is never read.
- [ ] `nw personas validate` exits 1 with the problems listed when any definition is broken, 0
      and `ok` when clean, and `--json` emits the documented object.
- [ ] `nw personas list|show` show the layer, role, category and template for declared personas,
      mark an invalid definition `invalid`, and completion offers declared names.
- [ ] `PERSONAS`, `REVIEWER_PERSONAS`, `ADOPT_REGISTRY` and the resolved builtin profiles are
      unchanged; a profile listing a declared name in `reviewers:` is still rejected by
      `validate_profile_doc` exactly as before; and `profiles.py` and `queue.py` are untouched by
      the diff.

## Test plan

- **Automated tests** (new `tests/test_agentic_persona_defs.py`, uses `tmp_path` projects and a
  temp config dir; no network, no real agent runs):
  - valid definitions per role: schema, `read_only`, category enum (declared, default, with
    `other` listed);
  - one negative fixture per rule: unknown key (`read_only`, `output_schema`), bad schema id,
    bad role, name != stem, bad name, builtin shadow in user and in project layer, cross-layer
    duplicate, missing template, escaping template (`..`, symlink), non-UTF-8 template,
    out-of-role and unknown placeholder, `category` on a non-reviewer, bad category slug,
    duplicate category, too many categories; a multi-fault set asserts all problems come back in
    a single call;
  - `ROLE_PLACEHOLDERS` superset test against the builtin templates of each role;
  - only the main checkout is read: a `.nw/personas/` placed in a worktree path is ignored;
  - broken file omitted from `load_registry` while a good sibling loads;
  - `get_persona(name)` without a project root still resolves builtins only and raises for a
    declared name.
- **CLI tests** (new `tests/test_agentic_personas_cli.py`, click `CliRunner`, as
  `tests/test_agentic_profiles_cli.py`): `nw personas validate` clean / broken / `--json` /
  `--project`, exit codes, and `list`/`show` layer and `invalid` rendering; completion includes a
  declared name.
- **Inertness guard** (in `tests/test_agentic_persona_defs.py`): with a valid project persona
  declared, `PERSONAS`, `REVIEWER_PERSONAS`, `ADOPT_REGISTRY` equal their builtin values, and
  `profiles.validate_profile_doc` for `reviewers: [security_reviewer]` still raises the existing
  unknown-persona error. The existing `tests/test_agentic_personas.py` and
  `tests/test_agentic_profiles*.py` run unchanged.
- **Manual verification** (before `done`, on a real project checkout): create
  `.nw/personas/security_reviewer.yaml` plus a template, run `nw personas validate` and
  `nw personas list`/`show security_reviewer`; then break the template placeholder and confirm the
  exit code and message. Record in "As shipped".
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_agentic_personas.py` and `tests/test_agentic_profiles*.py`; assert
  `git diff --stat` shows no change to `src/nightwright/agentic/queue.py` or `profiles.py`; the
  full suite runs at the queue's close gate.

## Rollout / migration

1. Land after Pipeline A1 (registry, loader, `nw personas list|show`); this spec extends them and
   adds `depends_on` on A1's spec id when that exists.
2. Add the user/project layers, validator, `reviewer_schema` and registry functions, with the
   builtin-only module values untouched.
3. Add `nw personas validate` and the list/show/completion changes.
4. No data migration: no existing project has `.nw/personas/`, and nothing in profiles or the
   queue changes.
5. Pipeline A2b (profiles accept declared names, behind the `reviewers_of` filter) follows with
   `depends_on` this spec; Pipeline A3 then switches the queue to registry membership.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run pytest -q`) and the full suite is green at the queue's close gate; manual steps performed and noted.
- [ ] No regressions (builtin tables and profiles identical; `queue.py` and `profiles.py` untouched).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._ Decisions taken while drafting, recorded for later tuning: the custom reviewer schema is
the adversarial schema with a declared `category` enum (plus `other`) rather than a new shared
schema, because it is the one existing reviewer shape with per-finding categories; the profile
integration is split out as A2b so this spec stays inert with respect to profiles and the queue;
the dependency on A1 is prose-only until A1 has a spec id.
