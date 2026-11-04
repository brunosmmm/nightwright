---
id: SPEC-0394
title: "Declared personas: run settings, reviewer routing metadata and run requests (rework of SPEC-0296 on the persona registry)"
status: accepted
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0348
tags: [agentic, review, personas, project-config]
source_idea: IDEA-090
depends_on: [SPEC-0370, SPEC-0371]
supersedes: [SPEC-0296]
---

## Context

`IDEA-090` asked for reviewer personas that a project defines in its own repo (a crypto repo
wants a security reviewer, a UI repo an accessibility reviewer) without patching nightwright.
[SPEC-0296](./0296-pluggable-reviewer-personas-fully-defined-project.md) was its first slice
(declare, validate, build a run request; inert in the queue). It was implemented on campaign
branch `wt/IDEA-090-20261030-236a` (pre-rename `src/wt/` paths, a `reviewers:` list in
`.wt/project.yaml`, `wt projects reviewers`) and never merged. Since then `main` gained the
persona registry of epic [SPEC-0348](./0348-declarative-per-project-agentic-pipeline.md):

- [SPEC-0370](./0370-pipeline-a1-persona-registry-as-data.md) (A1): builtin personas are
  `nw.persona.v1` YAML in `agentic/persona_defs/`, loaded by `persona_registry.py`
  (`load_registry(cfg, project_root=, known=)`, `validate_persona_doc`); `PERSONAS`,
  `REVIEWER_PERSONAS`, `ADOPT_REGISTRY` are derived from it; `nw personas list|show`.
- [SPEC-0371](./0371-pipeline-a2-role-limited-custom-personas.md) (A2a): user
  (`<config dir>/personas/`) and project (`<main checkout>/.nw/personas/`) definitions with
  `role: implementer | reviewer | triager`, role-fixed schema and `read_only`
  (`reviewer_schema(categories)`, category required per finding), `ROLE_PLACEHOLDERS`, builtin
  shadowing refused, every problem reported at once, main-checkout-only reads,
  `persona_defs.py` (`scan`, `validate_persona_defs`, `load_registry`, `reviewer_names`),
  `get_persona(name, project_root=None)`, `nw personas validate`.

The branch therefore cannot be rebased: its `project.py` / `personas.py` / `cli_projects.py`
changes collide with A1/A2a (and with later `build_request` edits: SPEC-0345 per-worker cost and
backoff, SPEC-0253 private TMPDIR, SPEC-0389 claude resolution in the job exec path), and most of
what it built now exists in a different shape. The human approved reworking it on top of the
registry. This spec is that rework: it keeps 0296's intent and re-expresses only the part of its
acceptance criteria that `main` does not already deliver.

### SPEC-0296 criteria, mapped onto `main`

| SPEC-0296 criterion | Status on `main` | This spec |
|---|---|---|
| Project declares reviewers in its repo | done differently: `.nw/personas/<name>.yaml`, `role: reviewer` (0371) | none |
| Strict validation, all problems in one report | done (0370/0371 validators, `nw personas validate`) | extends to the new keys |
| Template confinement, unknown placeholder, builtin collision | done (0371) | none |
| Read-only persona with shared findings schema and `categories` enum | done (`reviewer_schema`, 0371) | none |
| `get_persona` finds a declared persona with a project, not without | done (`get_persona(name, project_root)`) | none |
| Read only from the main checkout | done for definitions (0371) | applies it to run requests |
| Per-reviewer `model` / `effort` / `max_budget_usd` defaults | **missing** | AC1, AC5 |
| `required` and `when.paths` / `when.labels` stored and validated (inert) | **missing** | AC2 |
| `is_reviewer(id, project)` | **missing** | AC4 |
| `build_request` for a declared reviewer (read-only, review context, layered settings) | **missing** (`build_request` calls `get_persona(persona)` without a root) | AC5, AC6 |
| Generated Output section so the author writes only the review lens | **missing** | AC7 |
| A CLI listing declarations with template sha256 and settings | partly (`nw personas list|show`) | AC8 |
| Inert: nothing in the queue/profiles changes | holds today | AC9 keeps it |
| Capability page documents it, says it does not run yet | **missing** | AC10 |

## Goals / Non-goals

**Goals**
- G1. A declared (user or project) persona definition can carry its own run defaults:
  `model`, `effort`, `max_budget_usd`.
- G2. A declared reviewer-role definition can carry review-set routing metadata, `required` and
  `when: {paths, labels}`, validated strictly and stored, consumed later by Pipeline A3.
- G3. `personas.build_request` can build a correct run request for a declared persona: resolved
  from the project's main checkout, read-only for read-only roles, with the review context for a
  reviewer, settings layered from nightwright defaults, the profile, the definition and an
  explicit profile entry.
- G4. A declared reviewer's prompt states the output contract (findings shape and its category
  vocabulary) without the author writing it.
- G5. `nw personas list|show` show the new fields and the template's sha256.
- G6. Strictly inert for the queue: no campaign can reach a declared persona yet.

**Non-goals**
- Profiles referencing declared personas (`reviewers:` / `personas:` accepting declared names,
  `_check_reviewers`, `nw profiles check`): Pipeline A2b, unchanged in scope by this spec except
  for the settings-layering constraint recorded under Rollout.
- Running declared reviewers in the queue (review set, `when` filtering, required vs advisory
  routing, cost cap, adoption, divergence, chips, `config_invalid` park): Pipeline A3. The three
  decisions SPEC-0296 recorded for that slice carry over to A3 unchanged: (a) the review pass
  waits for every run in the set, advisory included, and a restart adopts open advisory runs like
  required ones; (b) the set is computed by one helper from the resolved profile, the project
  config read from the main checkout, repo root, base, branch and the spec's tags; (c) if
  `gitops.changed_files` fails, a `when.paths` reviewer is selected (fail open) and the failure
  is noted in the `review.set` ledger note.
- Spec-phase reviewers, new roles, custom output schemas, writable reviewers, overriding
  builtins (as in 0296 and 0371).
- The `.nw/project.yaml` `reviewers:` list, `project.validate(doc, root=)`, template front matter,
  the `<name>_reviewer` naming rule and `nw projects reviewers` from SPEC-0296: replaced by the
  registry's definition files, its naming rules and `nw personas` (see Alternatives).
- Merging the two declared-persona validators (`persona_registry.validate_persona_doc` for
  user/project layers and `persona_defs._read_file`) into one: a separate cleanup; this spec only
  makes them agree on the new keys through one shared helper.

## Decision

Extend `nw.persona.v1` for the user and project layers with optional run-default keys
(`model`, `effort`, `max_budget_usd`, any custom role) and reviewer-only routing keys
(`required`, `when`), checked by one helper that both registry validators call. Declared
personas carry these values on `Persona.declared`. `build_request` resolves the persona against
the project's main checkout (`project_cfg["_root"]`), layers settings
`nw defaults < profile role-default entry < definition < profile personas.<name> entry`, forces
read-only roles to `read_only: true` / `dontAsk`, and gives every reviewer-role persona the review
context through a new `is_reviewer(name, project_root)`. `build_prompt` appends a generated Output
section to a declared reviewer's template. `queue.py` and `profiles.py` are not edited, so the
queue still cannot name a declared persona.

## Design

### Definition keys (user/project layers only)

```yaml
# <project main checkout>/.nw/personas/security_reviewer.yaml
schema: nw.persona.v1
name: security_reviewer
role: reviewer
template: security_reviewer.md
description: "crypto misuse, secrets, injection"
category: [key_handling, timing, randomness]
model: opus                 # new: opus|sonnet|haiku or a claude-* model id (profiles.MODEL_*)
effort: high                # new: one of profiles.EFFORTS
max_budget_usd: 2.0         # new: a number > 0 (not a bool)
required: true              # new, reviewer role only: bool, default true (A3: blocking vs advisory)
when:                       # new, reviewer role only: omitted or {} means always
  paths: ["src/crypto/**", "**/*.rs"]   # non-empty list of non-empty glob strings
  labels: [security, crypto]            # non-empty list of non-empty strings
```

- One helper `persona_registry.check_declared_keys(doc, role, bad)` validates the five keys. It
  imports `MODEL_ALIASES`, `MODEL_RE`, `EFFORTS` from `profiles` lazily (inside the function) so
  `persona_registry` keeps its no-`personas`-import rule. Errors use each validator's existing
  `origin: key: problem` shape.
- `persona_registry.validate_persona_doc` adds the five keys to the allowed set for
  `user`/`project` layers and calls the helper; in a builtin definition they are refused with
  `allowed only in a user or project definition` (builtin run settings stay the profiles' job).
- `persona_defs.KEYS` gains the five keys and `_read_file` calls the same helper, so
  `nw personas validate` and `nw personas list|show` report identical problems.
- `required` or `when` on an `implementer` / `triager` role: `only allowed for role reviewer`.
  `when` accepts only `paths` and `labels`; `null`, a non-mapping, an unknown key, an empty or
  non-list value, or an empty/non-string item each fail. Both given means both must match (A3).
- `read_only`, `permission_mode`, `output_schema`, `invariant` stay refused (0371).

### Persona carries the declaration

`personas.Persona` gains `declared: Mapping | None = None` (builtins: `None`, so `PERSONAS` is
unchanged). For a declared persona `persona_defs._build` sets it to a read-only mapping:
`{"settings": {model?, effort?, max_budget_usd?}, "required": bool, "when": {paths?, labels?},
"template_sha256": hex}` with only the settings keys the file gives, `required` defaulting to
`True` and `when` to `{}` (non-reviewer roles: `required: None`, `when: None`). The sha256 is of
the template bytes read during validation.

### `is_reviewer`

`personas.is_reviewer(name, project_root=None, *, user_dir=None) -> bool`: `name in
REVIEWER_PERSONAS`, or, with a `project_root`, the registry entry for `name` has
`role == "reviewer"`. Without a root only builtins count. `REVIEWER_PERSONAS` itself is unchanged.

### `build_request`

Only the persona-resolution and settings lines of `build_request` change; the env, TMPDIR,
remote/cost fields and the rest are untouched.

- `project_root = (project_cfg or {}).get("_root")`. `project.project_config` sets `_root` to the
  registry-resolved main checkout, so the root is never a campaign worktree. `p =
  get_persona(persona, project_root)`.
- Builtin persona (`persona in PERSONAS`): `settings = persona_settings(profile, persona)`, as
  today.
- Declared persona: `settings = declared_settings(profile, p)` (new, in `personas.py`):
  1. nightwright defaults `{model: sonnet, effort: medium, max_budget_usd: 1.0}`;
  2. the profile's entry for the role-default builtin (`reviewer` -> `conformance_reviewer`,
     `implementer` -> `implementer`, `triager` -> `gate_triager`), if the profile has one;
  3. `p.declared["settings"]`;
  4. the profile's explicit `personas.<name>` entry, if present (reachable once A2b lands; a
     hand-built profile dict may carry it now).
  Later layers win key by key. For a read-only role (`p.read_only`) the result is forced to
  `read_only: True`, `permission_mode: "dontAsk"` whatever the profile says; the implementer role
  keeps the builtin implementer's writable path (project `agents.permission_mode`, allowlist).
- Review context: `review=is_reviewer(persona, project_root)` (was `persona in
  REVIEWER_PERSONAS`), so a declared reviewer gets `diff`, `test_files`, `changed_files`.
  `with_tests` stays `persona == "test_reviewer"`.
- `build_prompt(p, ctx)` receives the resolved `Persona` object (it already accepts one), so the
  declared persona is never re-looked-up without a root.

### Generated Output section

`build_prompt` appends, for a persona with `p.declared is not None and p.role == "reviewer"`, a
fixed `## Output` block between the rendered template and the invariant block: return
`verdict` (`pass` | `fix`), `score` (integer 0..100), `findings` (each `severity` in
`must_fix|major|minor|nit`, `category` from the persona's enum as listed, `title`, optional
`detail`, `files`, `lines`) and `fix_brief`. The category list is read from `p.schema`, so it is
exactly what `--json-schema` enforces. Builtin prompts are byte-identical to today's.

### CLI

`nw personas list --json` and `show` (human and `--json`) add, for user/project entries:
`settings` (`model`, `effort`, `max_budget_usd`, absent keys omitted), `required` and `when` for
reviewer roles, and `template_sha256`. Builtin entries show none of these fields. No new command;
`nw personas list --project P` is the listing SPEC-0296's `wt projects reviewers` provided.

### Docs

`docs/product/capabilities/agentic.md`, section *Personas*: a *Declared personas* subsection with
the security-reviewer example above, the role table, the new keys, `nw personas
list|show|validate`, and a plain statement that declared personas are validated and listed but
the queue does not run them until Pipeline A2b/A3 ship.

## Alternatives considered

- **Keep SPEC-0296's `reviewers:` list in `.nw/project.yaml`** — a second declaration surface
  next to `.nw/personas/` with different naming and validation; the epic chose definition files.
- **Template front matter for settings (0296)** — the definition is already a YAML file, so a
  second YAML block in the template would split one declaration in two.
- **Let A2b's role-default fill write `personas.<name>` into the resolved profile and drop layer
  2/3 here** — then a profile copy of the role default would always outrank the definition's own
  `model`, silently ignoring it. Computing the layers in one function keeps the definition above
  the role default and below an explicit profile entry.
- **Rebase the 0296 branch and adapt** — the branch's loader, registry and CLI duplicate what
  A1/A2a shipped in another shape; reworking costs less than reconciling two registries.
- **Put the Output contract in `{{schema_text}}`** — `schema_text` is the spec SCHEMA.md for the
  spec personas, not an output contract, and is not filled for campaign personas.

## Acceptance criteria

- [ ] AC1. A user/project definition accepts `model`, `effort`, `max_budget_usd`; bad values (an
      unknown model, a non-`claude-*` id, a bad effort, a budget `<= 0`, a bool or a string)
      are refused naming file and key; the keys in a builtin definition are refused.
- [ ] AC2. A reviewer-role definition accepts `required` (bool) and `when` (`paths`/`labels`
      only, non-empty lists of non-empty strings); every bad shape (non-bool, `when: null`,
      non-mapping, unknown key, empty list, non-string item) and `required`/`when` on a
      non-reviewer role are refused; defaults are `required: true`, `when: {}`.
- [ ] AC3. `nw personas validate` and `nw personas list|show` report the same problems for the
      new keys (one shared helper); all problems of a multi-fault file come back in one run.
- [ ] AC4. `is_reviewer(name, project_root)` is true for a declared reviewer and false for a
      declared implementer/triager; without a root, or for a project that does not declare it,
      it is false; builtin answers are unchanged.
- [ ] AC5. `build_request` for a declared reviewer (project config with `_root`) returns a
      request with `read_only` true, `permission_mode: dontAsk`, no allowed tools, the
      reviewer's JSON schema, and settings layered `nw defaults < profile conformance_reviewer
      entry < definition < profile personas.<name>`; `read_only: false` or `bypassPermissions`
      in the profile entry is overridden. A declared implementer builds a writable request.
- [ ] AC6. The declared reviewer's prompt context carries the review `diff` and `test_files`;
      the persona is resolved from `project_cfg["_root"]`, and a different `.nw/personas/` copy
      in the campaign worktree changes nothing in the request.
- [ ] AC7. A declared reviewer's prompt contains the generated Output section listing its
      category vocabulary; builtin prompts are byte-identical to `render(template) + invariant`
      as before.
- [ ] AC8. `nw personas show` / `list --json` show `settings`, `required`, `when` and
      `template_sha256` for a declared reviewer and omit them for builtins.
- [ ] AC9. Inert: `PERSONAS`, `REVIEWER_PERSONAS`, `ADOPT_REGISTRY` unchanged; a profile naming a
      declared persona in `reviewers:` is still refused by `resolve_profile`; the diff does not
      touch `queue.py`, `profiles.py` or `divergence.py`.
- [ ] AC10. The capability page documents declared personas with the example, the new keys and
      the "not run by the queue yet" statement.

## Test plan

- **Automated tests** (new `tests/test_agentic_persona_run_settings.py`, offline, `tmp_path`
  project with a temp git repo and worktree, temp user personas dir, no agent runs):
  - `test_settings_keys_valid_and_invalid` (AC1), `test_routing_keys_reviewer_only` (AC2): one
    case per rule, parametrized; a multi-fault file returns every problem in one
    `validate_persona_defs` call and the same messages through `persona_registry.load_registry`
    entry errors (AC3).
  - `test_declared_mapping_defaults` : `Persona.declared` contents, defaults, `template_sha256`.
  - `test_is_reviewer` (AC4).
  - `test_build_request_declared_reviewer` (AC5, AC6): layering with each layer present and
    absent, forced read-only/`dontAsk`, review context, worktree copy ignored;
    `test_build_request_declared_implementer_writable`.
  - `test_output_section` (AC7): declared reviewer prompt contains the category list; every
    builtin persona's prompt equals `render(template) + "\n\n" + render(invariant)`.
  - `test_inert` (AC9): builtin tables equal, `resolve_profile` with a declared reviewer raises.
- **CLI** (extend `tests/test_agentic_personas_declared_cli.py`): `show`/`list --json` fields for
  a declared reviewer and their absence for a builtin (AC8).
- **Run:** `uv run pytest -q -n 4 tests/test_agentic_persona_run_settings.py
  tests/test_agentic_personas_declared_cli.py tests/test_agentic_persona_defs.py
  tests/test_agentic_personas.py` (new file plus three existing). AC9's "not touched" is checked
  with `git diff --stat main` naming no `queue.py`, `profiles.py`, `divergence.py`. Anything
  wider runs at the queue's close gate (SPEC-0378), not by hand.
- **Manual verification** (before `done`): in a scratch project, add the example definition,
  run `nw personas show security_reviewer --project <P>` and `nw personas validate --project
  <P>`; set `effort: extreme` and `when: {files: x}` and confirm one run lists both problems with
  exit 1. Record output in *As shipped*. AC10 is checked by reading the page.

## Rollout / migration

1. Land this spec's code: inert, no data migration (no project has `.nw/personas/` with the new
   keys; builtins unchanged).
2. Constraint recorded for Pipeline A2b: its role-default handling must not materialize a
   `personas.<declared name>` entry in the resolved profile that would outrank the definition's
   own settings; it either leaves the entry absent (this spec's `declared_settings` supplies the
   role default) or marks it `role-default:<persona>` and `declared_settings` treats a marked
   entry as layer 2.
3. Pipeline A3 consumes `required` / `when` and `is_reviewer` with SPEC-0296's three recorded
   decisions (Non-goals).
4. The stranded campaign `IDEA-090-20261030-236a` and its branch are abandoned, not merged;
   `IDEA-090`'s `:SPEC:` link moves to SPEC-0394.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; listed tests pass; manual steps performed and noted.
- [ ] No regressions (affected tests at the queue's close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._ Decided: the five keys live in the definition file (not project.yaml, not front matter);
settings layering puts the definition above the profile's role default and below an explicit
profile entry; `nw personas` replaces `wt projects reviewers`; the two-validator duplication is
left for a separate cleanup and bridged by one shared helper.
