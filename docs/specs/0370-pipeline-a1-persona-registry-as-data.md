---
id: SPEC-0370
title: "Pipeline A1: persona registry as data (persona_defs/*.yaml, layered loader, nw personas list|show)"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0348
source_idea: IDEA-715
tags: [agentic, personas, registry, project-config]
---

## Context

Child of the epic [SPEC-0348](./0348-declarative-per-project-agentic-pipeline.md)
(Stage A, first slice). Personas are code today. `src/nightwright/agentic/personas.py` binds
each persona name to a template, an output schema, `read_only` and an invariant in a
hand-written `PERSONAS` dict of nine `Persona(...)` entries (`personas.py:272`). Two more
hand-written tables sit next to it: `REVIEWER_PERSONAS`, a three-name tuple (`personas.py:298`),
and `ADOPT_REGISTRY`, the adoption classification (SPEC-0225, `personas.py:303`). Consumers
read them as plain values: `profiles.py` (known persona names, reviewer checks),
`divergence.py` (`COUNTED_PERSONAS`), `cli_campaign.py` (persona listing) and `queue.py`
(`PS.REVIEWER_PERSONAS`, `PS.ADOPT_REGISTRY`, `PS.PERSONAS`). The templates are already data
(`agentic/personas/*.md`) and profiles are already layered YAML
([SPEC-0237](./0237-workflow-profiles-as-yaml-files-builtin-user-project.md)), but there is no
persona definition file, no way to see the registry (`nw profiles` shows profiles only), and
nothing a later slice (custom personas, A2) can validate or extend.

This slice moves the *builtin* personas into YAML and builds the layered loader and strict
validator that A2 and A3 build on. It changes no behavior: the derived tables must equal the
hand-written ones exactly, and the queue is untouched (so it does not collide with B2 on the
`queue.py` hotspot).

## Goals / Non-goals

**Goals**
- G1. The nine builtin personas are declared in `agentic/persona_defs/*.yaml`, one file each,
  using the existing templates in `agentic/personas/`.
- G2. A registry loader reads three layers (builtin, user `<config_dir>/personas/`, project
  `<main checkout>/.nw/personas/`) and a strict validator reports every problem at once.
- G3. `PERSONAS`, `REVIEWER_PERSONAS` and `ADOPT_REGISTRY` are derived from the registry, not
  hand-written; their values, key order and tuple order equal the old tables.
- G4. `nw personas list|show` makes the registry visible, with the file each definition came
  from.
- G5. An equality test pins the derived tables to a frozen copy of the old ones.

**Non-goals**
- Running, or profile-listing, a user/project persona. The module-level tables are built from
  the *builtin* layer only in this slice; making declared personas usable by profiles
  (`reviewers:`, `personas:`), `nw personas validate` and the role-to-placeholder-subset check
  are A2 ([SPEC-0348](./0348-declarative-per-project-agentic-pipeline.md)
  breakdown). Queue use is A3.
- Any change to `queue.py`, `profiles.py`, `divergence.py` or `cli_campaign.py` call sites.
- Moving or rewording the existing templates, schemas or invariants. They stay Python/Markdown
  constants; YAML only *names* them.
- New output schemas or roles for user/project files.

## Decision

Add a persona registry module that loads `nw.persona.v1` YAML definitions from three layers and
validates them strictly. Convert the nine hard-coded personas into builtin definition files and
have `personas.py` build `PERSONAS`, `REVIEWER_PERSONAS` and `ADOPT_REGISTRY` from the builtin
layer through two small derivation functions. The module-level names stay (as values computed
at import) so no call site changes. Add `nw personas list|show`. Prove equivalence with a test
against a frozen literal copy of the old tables.

## Design

### Definition file

```yaml
# agentic/persona_defs/conformance_reviewer.yaml   (builtin)
schema: nw.persona.v1
name: conformance_reviewer        # must equal the file stem; ^[a-z][a-z0-9_]*$
role: reviewer                    # implementer | reviewer | triager | system (builtin only)
template: conformance_reviewer.md # a bare file name, no path separators
description: "does the diff do what the spec says"
order: 30                         # builtin only: position in PERSONAS / REVIEWER_PERSONAS
output_schema: conformance_reviewer   # builtin only: key into personas.SCHEMAS
invariant: default                # builtin only, optional: key into personas.INVARIANTS
```

Keys by layer (an unknown key, or a key not allowed in the layer, is a validation error):

| key | builtin | user / project |
|---|---|---|
| `schema` (`nw.persona.v1`), `name`, `role`, `template`, `description` | required | required |
| `category` (list of strings) | optional | optional |
| `order` (unique int), `output_schema` | required | refused |
| `invariant` (default `default`) | optional | refused |
| `read_only` (bool) | `role: system` only, required there | refused |
| `adopt: {step, personas}` | `role: system` only, optional | refused |

`role` fixes the contract for `implementer` (writable), `reviewer` (read-only) and `triager`
(read-only): the validator checks a builtin's `read_only` outcome matches its role. `system` is
a builtin-only role for the four personas the epic lists as builtin-only (`spec_writer`,
`spec_reviewer`, `review_triager`, `conflict_resolver`); they set `read_only`, and their
invariant, explicitly. In user/project files `role` is limited to the first three, the schema
and `read_only` come from the role, and a name equal to any builtin name is refused. For A1 the
role-to-schema mapping for user/project files is not needed (those definitions are validated
and listed only); A2 defines it.

### Builtin definitions (the data that replaces the tables)

| name | role | template | output_schema | read_only | invariant | order | adopt |
|---|---|---|---|---|---|---|---|
| implementer | implementer | implementer.md | implementer | false | default | 10 | derived: `("implement", ())` |
| gate_triager | triager | gate_triager.md | gate_triager | true | default | 20 | derived: `("triage", ())` |
| conformance_reviewer | reviewer | conformance_reviewer.md | conformance_reviewer | true | default | 30 | derived: `("review", None)` |
| test_reviewer | reviewer | test_reviewer.md | test_reviewer | true | default | 40 | derived: `("review", None)` |
| adversarial_reviewer | reviewer | adversarial_reviewer.md | adversarial_reviewer | true | default | 50 | derived: `("review", None)` |
| spec_reviewer | system | spec_reviewer.md | spec_reviewer | true | spec_review | 60 | none |
| spec_writer | system | spec_writer.md | spec_writer | false | spec_writer | 70 | none |
| review_triager | system | review_triager.md | review_triager | true | default | 80 | `{step: review, personas: [review_triager]}` |
| conflict_resolver | system | conflict_resolver.md | conflict_resolver | false | resolver | 90 | none |

`order` values reproduce the old `PERSONAS` insertion order; reviewers keep
conformance, test, adversarial. Builtin templates resolve in `agentic/personas/` (no file moves,
so history and open campaigns are untouched). This deliberately refines the epic's "resolved
next to the YAML file": that rule applies to the user and project layers, where the template
sits beside its definition. The YAML refers to code objects by key only: `personas.py` exposes
`SCHEMAS` (name -> the existing `*_SCHEMA` dicts, same objects) and `INVARIANTS`
(`default` -> `INVARIANT`, `spec_review` -> `SPEC_REVIEW_INVARIANT`, `spec_writer` ->
`SPEC_WRITER_INVARIANT`, `resolver` -> `RESOLVER_INVARIANT`).

### Modules

- **`agentic/persona_registry.py` (new).** `PersonaDef` (frozen dataclass: `name, role,
  template, description, category, order, output_schema, invariant, read_only, adopt, scope,
  origin, path`), `load_registry(cfg, *, project_root=None) -> Registry`,
  `validate_persona_doc(doc, *, origin, layer, stem) -> list[str]`, and the two derivations
  `reviewer_personas(defs) -> tuple[str, ...]` (`role: reviewer`, by `order`) and
  `adopt_registry(defs) -> dict`: first, in `order`, every non-reviewer definition that has an
  entry (`implementer` role -> `("implement", ())`, `triager` -> `("triage", ())`, explicit
  `adopt` -> `(step, tuple(personas))`); then, in `order`, every `role: reviewer` definition ->
  `("review", None)`. This reproduces the old dict's key order (implementer, gate_triager,
  review_triager, conformance_reviewer, test_reviewer, adversarial_reviewer). It mirrors `profiles.py`'s `_read_dir`/`load_layers` shape: `*.yaml` only, a `.yml`
  or a bad stem is a broken definition, an unreadable directory raises `ValueError`, every file
  is re-read and re-validated per call. It must not import `personas.py` (that module imports
  it); the set of valid `output_schema`/`invariant` keys is passed in by the caller.
- **`agentic/persona_defs/*.yaml` (new, nine files).** Package data like `profile_defs/`
  (hatch already ships the package tree).
- **`agentic/personas.py`.** Gains `SCHEMAS`/`INVARIANTS`, a `_build_persona(defn)` and a
  builtin-layer load; `PERSONAS`, `REVIEWER_PERSONAS` and `ADOPT_REGISTRY` are assigned from
  `reviewer_personas(...)`/`adopt_registry(...)` and the builder. The `Persona` dataclass,
  `get_persona`, `PLACEHOLDERS` and every call site stay as they are. A broken builtin file
  raises `ValueError` at import (a packaging bug, guarded by a test).
- **`agentic/cli_personas.py` (new), registered in `agentic/cli.py`** like `cli_profiles`.

### Validation (all problems at once, `origin: key: problem`, SPEC-0170 style)

Per file: parses as a YAML mapping; `schema` is `nw.persona.v1`; `name` matches the pattern
and equals the file stem; `role` in the layer's allowed set; `template` is a bare file name
that exists (builtin: in `agentic/personas/`; user/project: next to the YAML) and is UTF-8
text; every `{{name}}` in the template is in `PLACEHOLDERS`; `category` is a list of
non-empty strings; the layer's key table above is respected; `description` is a non-empty
string. Per registry: `order` unique among builtins; a user/project `name` equal to a builtin
name is refused (`shadows builtin persona`); a user and a project definition of the same
non-builtin name are allowed and the project wins (higher layer replaces, no merge, so a
definition is always complete in one file); builtins' derived `read_only` agrees with `role`.
The role-specific placeholder subset is A2.

### Project layer trust

The project layer is read from `project_root`, the project's registered main checkout, exactly
as `nw profiles` does, never from a campaign worktree or the cwd's worktree. A caller that has
no project passes none and gets builtin + user only.

### CLI

- `nw personas list [--project P] [--json]`: one row per definition across layers: name, role,
  scope (`builtin|user|project`), whether it is a reviewer, template, short origin, and for a
  broken definition its first problem. Exit 0 (listing is not a check). `--json` emits
  `nw.personas.v1`.
- `nw personas show NAME [--project P] [--json]`: all resolved fields with the file that set
  them, the resolved template path, `output_schema` key, `read_only`, invariant key, `adopt`
  classification, every validation problem if broken. Unknown name: non-zero exit listing
  known names. `--json` emits `nw.persona.show.v1`.
- `docs/WORKFLOW.md` gains the `nw personas list|show` line next to `nw profiles`.

## Alternatives considered

- **Keep the tables and add YAML only for custom personas** — leaves two sources of truth and
  fails the epic's "no longer hand-written tables" criterion.
- **Make `REVIEWER_PERSONAS`/`ADOPT_REGISTRY` call-time functions now** — would force edits
  across `queue.py`, `profiles.py` and `divergence.py`; deferred to A3 (after B2). Import-time
  values from the same derivation functions keep call sites untouched and give A3 the functions.
- **Move templates next to the YAML in `persona_defs/`** — a pure file-move churn that buys
  nothing in this slice and risks diverging from open campaigns' recorded prompts.
- **Let YAML embed schemas and invariants** — larger blast radius, and the epic keeps schemas
  code-owned; YAML names them by key.

## Acceptance criteria

- [x] `agentic/persona_defs/` holds exactly nine `*.yaml` files with the names in the table
      above, each valid `nw.persona.v1`, referencing existing templates in `agentic/personas/`.
- [x] `list(PERSONAS)` equals the old key order, and for every persona `name`,
      `template_file`, `read_only` and `invariant` equal the old values, `schema` is the
      identical object as the old module-level `*_SCHEMA`, and `.template` text is unchanged.
- [x] `REVIEWER_PERSONAS == ("conformance_reviewer", "test_reviewer", "adversarial_reviewer")`
      and `ADOPT_REGISTRY` equals the old dict (same keys, values and key order); in
      particular `list(ADOPT_REGISTRY)` is implementer, gate_triager, review_triager,
      conformance_reviewer, test_reviewer, adversarial_reviewer (non-reviewers first, then
      reviewers, each by `order`).
- [x] `personas.py` contains no hand-written `Persona(...)` table, `REVIEWER_PERSONAS` tuple
      literal or `ADOPT_REGISTRY` dict literal; they are produced by the registry derivations.
- [x] The loader reads builtin, user (`<config_dir>/personas`) and project
      (`<project_root>/.nw/personas`) layers; the project layer is read only from the
      `project_root` argument.
- [x] The validator rejects, with all problems in one error list: wrong `schema`, name/stem
      mismatch, bad name pattern, unknown key, `output_schema`/`order`/`invariant`/`read_only`/
      `adopt` in a user/project file, role `system` outside builtin, missing or path-like
      `template`, template with an unknown `{{placeholder}}`, non-list `category`, a
      user/project name equal to a builtin, duplicate builtin `order`, `.yml` files.
- [x] A valid user/project definition appears in `nw personas list` but never in `PERSONAS`,
      `REVIEWER_PERSONAS`, `ADOPT_REGISTRY` or a profile's accepted persona names.
- [x] `nw personas list` and `nw personas show NAME` (text and `--json`) print the registry
      with per-definition scope and origin file; `show` of a broken definition lists its
      problems; `show` of an unknown name exits non-zero.
- [x] No behavior change: no edits to `queue.py`, `profiles.py`, `divergence.py` or
      `cli_campaign.py`, and the existing agentic persona, profile and queue tests pass
      unmodified.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_persona_registry.py`:
    - *equality against the old tables*: a literal frozen copy (names, order, template file,
      schema-constant name, `read_only`, invariant constant name, reviewer tuple, adoption
      dict) is asserted against `PERSONAS`, `REVIEWER_PERSONAS`, `ADOPT_REGISTRY`, including
      schema/invariant object identity and `.template` text equal to the file read directly;
    - *no hand-written tables*: an `ast` walk of `personas.py` finds no tuple/dict literal
      assigned to the three names and `Persona(` is only called inside the builder;
    - *validator negatives*: one parametrized case per rejected condition listed in the
      acceptance criteria, each asserting the expected message, plus one multi-error file
      asserting all problems come back together;
    - *layers*: tmp `config_dir` user file and tmp `project_root/.nw/personas` file are loaded
      with correct scope/origin; project overrides user for the same custom name; a file under
      a different (worktree-like) directory is ignored; custom definitions do not enter the
      derived tables; a user file named like a builtin is refused;
    - *builtin integrity*: all nine builtin files validate, every template exists and uses
      only `PLACEHOLDERS`, builtin `order` values are unique.
  - `tests/test_agentic_personas_cli.py` (click `CliRunner`): `list` and `list --json` include
    the nine builtins with scope `builtin`, and a project-layer custom persona with scope
    `project`; `show conformance_reviewer` text and JSON fields; `show` of a broken file prints
    its problems; `show nope` exits non-zero and lists known names.
- **Manual verification:** `uv run nw personas list` shows nine builtin rows;
  `uv run nw personas show conformance_reviewer` shows the origin `persona_defs/…yaml`; create
  `.nw/personas/security_reviewer.yaml` (role reviewer, any existing template copied beside it)
  in a registered project's main checkout and confirm `list --project` shows it as `project`
  while `uv run nw profiles check` output is unchanged; break the file (unknown key) and
  confirm `show` reports the problem.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_agentic_personas.py`, `tests/test_agentic_profiles.py`,
  `tests/test_agentic_profiles_cli.py` and the `tests/test_agentic_*` queue files, unmodified;
  `git diff --stat` on the implementing branch shows no change to `queue.py`, `profiles.py`,
  `divergence.py`, `cli_campaign.py`; `python3 tools/spec_lint.py` exits 0. The full suite runs
  at the queue's close gate.

### Test record (2026-11-02)

- `uv run pytest -q tests/test_agentic_persona_registry.py tests/test_agentic_personas_cli.py`:
  34 passed (equality against the frozen old tables incl. key order and object identity, `ast`
  no-hand-written-tables check, validator negatives and a multi-error file, layers, builtin
  integrity, CLI list/show text and JSON, broken and unknown definitions).
- `tests/test_agentic_personas.py`, `tests/test_agentic_profiles.py`,
  `tests/test_agentic_profiles_cli.py` (unmodified): 205 passed.
- `python3 tools/affected_tests.py --run uv run pytest -q` was started but hit the background
  time limit (`personas.py` is imported by most agentic tests, so the affected set is close to the
  suite) and was stopped without a result; the broader check is left to the queue's close gate.
- Manual: `uv run nw personas list` shows nine builtin rows; `nw personas show review_triager`
  shows origin `builtin:persona_defs/review_triager.yaml`; `uv run nw profiles check` is
  unchanged (balanced, frugal, thorough ok). The project-layer custom persona and broken-file
  cases are covered by the CLI tests against a temp project root.
- `git diff --stat` shows no change to `queue.py`, `profiles.py`, `divergence.py`,
  `cli_campaign.py`. The full suite was left to the queue's close gate.

### As shipped

- `validate_persona_doc` and `load_registry` take a `known` mapping (`output_schemas`,
  `invariants`, `placeholders`) from `personas.KNOWN`; the validator also takes `directory`
  (where a user/project template is looked up). `load_builtin(known)` returns the builtin defs
  and raises `ValueError` on any problem.
- Builtin origin strings are `builtin:persona_defs/<name>.yaml`; user/project origins are the
  file path. Builtin descriptions are short phrases written for the table (no template text is
  reused). `nw personas show` of a broken definition exits 0 (it displays); `list` marks a
  lower-layer definition shadowed by a higher one as `ok (overridden)`.

- Review loop 1 hardening: `nw personas list|show` render non-string `role`/`template` values
  of a broken file via `repr` (no table or `--json` crash), escape user-controlled text before
  Rich markup (`cli_personas.py`, and the shared `_header` in `cli_profiles.py`), and a refused
  `Entry` (shadows a builtin, or a `.yml` conflicting with its `.yaml` sibling) never wins
  `Registry.resolved()`, so `show implementer` stays the builtin. Unhashable builtin
  `output_schema`/`invariant`/`adopt.step` values are reported, not raised. New tests: profile
  acceptance of custom personas (`validate_profile_doc`, `_check_reviewers`, `_persona_names`)
  plus an `ast` check that `personas.py` only uses `load_builtin`; refused-entry resolution;
  CLI cases for `template: 123`, `role: [x]`, a YAML date, a user `implementer.yaml` and
  markup in a description. Ran `uv run pytest -q` on `tests/test_agentic_persona_registry.py`,
  `test_agentic_personas_cli.py`, `test_agentic_profiles_cli.py`, `test_agentic_profiles.py`,
  `test_agentic_personas.py`: 249 passed; `spec_lint` ok. Full suite left to gating.

## Rollout / migration

No data migration. Order: (1) add `persona_registry.py` and the validator with tests;
(2) add the nine YAML files and the `SCHEMAS`/`INVARIANTS` maps; (3) switch `personas.py` to
build the tables from the registry and land the equality test in the same change; (4) add
`cli_personas.py`, register it, document it in `docs/WORKFLOW.md`. Open campaigns are
unaffected: prompts are built from the same template text and schema objects.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests and the new test files pass; manual steps performed
      and noted here.
- [x] No regressions (full suite green at the queue's close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._ Decisions taken while drafting: `system` role and `order`/`adopt` keys are
builtin-only so the four builtin-only personas and the old table order are expressible as data;
builtin templates stay in `agentic/personas/`; module-level table names remain values (not
functions) until A3 touches the queue.
