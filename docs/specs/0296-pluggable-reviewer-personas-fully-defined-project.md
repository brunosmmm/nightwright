---
id: SPEC-0296
title: "Pluggable reviewer personas, defined in the project repo: declarations and registry"
status: superseded
superseded_by: SPEC-0394
owner: user
created: 2026-10-30
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, personas, project-config]
source_idea: IDEA-090
depends_on: [SPEC-0170, SPEC-0201, SPEC-0203]
---

> **Superseded by [SPEC-0394](./0394-declared-persona-run-settings-routing-and-requests.md) (2026-11-02).** This spec was implemented only on campaign branch `wt/IDEA-090-20261030-236a` and never merged to `main`; the "done" boxes below record that branch, not `main`. The persona registry (SPEC-0370/0371) has since shipped most of this scope in another shape, and SPEC-0394 reworks the remainder on top of it. Body kept unchanged as the historical record.

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-090`; user request 2026-10-25: reviewer personas should be pluggable and
defined on the project side).

Today the set of implementation reviewers is closed. `personas.REVIEWER_PERSONAS` is the tuple
`conformance_reviewer, test_reviewer, adversarial_reviewer`; each has a template in
`src/wt/agentic/personas/` and a schema constant in `personas.py`. A profile picks among them
with `reviewers:` ([SPEC-0201](./0201-reviewer-fan-out.md)) and `profiles._check_reviewers`
rejects anything else. A crypto repo that wants a security reviewer, a UI repo that wants an
accessibility reviewer, or a library that wants an API-compatibility reviewer must patch wt.

The full idea (declare, validate, run, route, display) is too large for one campaign: it
touches the project-config loader, the persona registry, profile resolution, the REVIEWING
step, routing and the cost cap, divergence, the chips and `wt.queue.v1`, and a CLI, and it
changes a merge-safety gate. This spec is the **first of four slices** and delivers the
foundation only: a project can *declare* reviewers, wt *validates* the declaration and the
templates strictly, and the persona layer can *build a run request* for a declared reviewer.
Nothing in the queue consumes declarations yet, so this slice is inert for every existing
campaign. The remaining slices are listed under Non-goals with the decisions already taken for
them, so they can be specced without re-opening this one.

Existing pieces this slice builds on:

- **Project config.** `.wt/project.yaml` is committed with the project and loaded by the strict
  hand-rolled validator in `src/wt/agentic/project.py` (`validate(doc)`,
  `load_project_config(path)`, `project_config(cfg, project)`, SPEC-0170). `project_config`
  resolves the repo through the project registry, i.e. the **main checkout**, never a campaign
  worktree.
- **Personas.** `personas.Persona` (frozen dataclass: `name, template_file, schema, read_only,
  invariant`), `PERSONAS`, `get_persona`, `render` (regex `{{placeholder}}`), `build_request`
  (builds the argv inputs from profile + project config + campaign state).
- **Findings shape.** The built-in reviewers all return `verdict pass|fix`, `score 0..100`,
  `findings[{severity must_fix|major|minor|nit, title, detail?, files?, ...}]`, `fix_brief`;
  `queue.triage_reviews` (SPEC-0203) reads exactly that and nothing persona-specific.

## Goals / Non-goals

**Goals**
- G1. A project declares its own implementation-phase reviewers in its repo: an entry in a new
  `reviewers:` list of `.wt/project.yaml` plus a prompt template under `.wt/reviewers/`.
- G2. wt validates the declaration in the project-config loader, strictly, with every problem
  reported at once in one `ValueError`, like SPEC-0170. Filesystem checks (the template) and
  structural checks are collected into the same error.
- G3. A declared reviewer becomes the persona `<name>_reviewer`, read-only, with the shared
  findings schema (optionally narrowing `category` to the reviewer's own vocabulary), usable
  through `get_persona`, `is_reviewer` and `build_request`.
- G4. Declarations are read only from the project's main checkout, so a campaign cannot rewrite
  the review that judges it.
- G5. `wt projects reviewers` shows what a project declares and whether it is valid.
- G6. Strictly inert: with no `reviewers:` declared, nothing that exists today changes.

**Non-goals** (each is a later slice of this idea; none is delivered here)
- **Slice 2: profile resolution.** Precedence with the profile layer (`personas.<id>` overrides
  from any layer including `.wt/profiles`), `enabled: false`, rejecting writable overrides,
  errors for undeclared `*_reviewer` profile keys, `_check_reviewers` accepting declared
  reviewers, `wt projects reviewers --profile`, provenance in `wt profiles show`. Until then
  profile files naming a non-built-in persona keep failing validation as today.
- **Slice 3: review set, routing, cost cap.** `last_review.set` computed on REVIEWING entry,
  the `when.paths` / `when.labels` filter, required vs advisory routing, `review_set` in
  `plan_step` / `decide_reviews` / `_pending_reviewers` / `_prepare_reviews` / `_cap_blocks`,
  adoption (`ADOPT_REGISTRY` classification of declared reviewers), `queue.py` and
  `divergence.py` membership via `is_reviewer`, the grep guard against `REVIEWER_PERSONAS`
  membership tests outside `personas.py`, the `config_invalid` park for a bad declaration and
  the e2e test. Decisions already taken for that spec: (a) the review pass **waits for every run
  in the set**, advisory ones included, so a late advisory verdict cannot exist across a state
  change; a restart adopts open advisory runs exactly like required ones (SPEC-0225); (b) the
  set is computed by a new helper that takes the resolved profile, the project config (read from
  the main checkout), the repo root, base, branch and the worktree spec's tags, and
  `_begin_review` calls it; (c) if `gitops.changed_files` fails, a `when.paths` reviewer is
  **selected** (fail open to running, never silently skipped) and the failure is noted in the
  `review.set` ledger note.
- **Slice 4: display and analysis.** Chip labels and `(adv)`, `reviews[].required` in
  `wt.queue.v1`, `reviewer_source` on `run.start`, divergence counting of required declared
  reviewers, capability-page worked example of a running reviewer.
- Spec-phase reviewers. `spec_reviewer` returns `approve | revise | split` with sizing and tone
  and the in-campaign spec review has no fan-out. `phase: spec` is rejected with a message
  saying so; a follow-up idea is filed when this ships.
- Custom output schemas. Every declared reviewer returns the shared findings shape; `categories`
  is the only per-reviewer narrowing.
- Writable reviewers; user-level (outside the repo) reviewer declarations; reviewers shipped as
  wt package data; changing the built-in reviewers, the triage formula or the divergence rules.

## Decision

Reviewers are declared in a new top-level `reviewers:` list of `.wt/project.yaml`, each entry
pointing at a markdown template under `.wt/reviewers/`. `load_project_config` validates the
entries (structure and templates, together, in one `ValueError`) and reads each template once.
A new `personas.reviewer_registry(project_cfg)` turns the loaded declarations into
`{<name>_reviewer: Persona}` (read-only, shared findings schema, template text carried in the
persona); `get_persona`, `is_reviewer` and `build_request` accept an optional project config and
consult that registry after the built-ins. Declarations come from the registry-resolved main
checkout only. Without declarations every code path behaves as before.

## Design

### Declaration

New top-level key in `.wt/project.yaml` (the loader stays strict, so an older wt rejects the key
as unknown rather than ignoring it):

```yaml
reviewers:
  - name: security                    # persona id: security_reviewer
    phase: implementation             # implementation (default) | spec (rejected, see Non-goals)
    template: .wt/reviewers/security.md
    required: true                    # true (default): blocks like a built-in; false: advisory
    when:                             # omitted or {} ⇒ always
      paths: ["src/crypto/**", "**/*.rs"]
      labels: [security, crypto]
    categories: [key_handling, timing, randomness, other]   # optional enum for finding.category
    model: opus                       # optional defaults
    effort: high
    max_budget_usd: 2.0
```

`required` and `when` are validated and stored in this slice but nothing acts on them until
slice 3. `PROJECT_DEFAULTS["reviewers"] = []`; each loaded entry is
`{name, phase: "implementation", template, required: True, when: {}, categories: None, model:
None, effort: None, max_budget_usd: None}` plus `persona` (`<name>_reviewer`),
`template_sha256` and `template_text` (the file text as read), added by the loader.

### Validation: where it runs

`project.validate(doc, root=None)` gains an optional `root` (the repo root).

- Structural checks (everything below except those marked *fs*) always run, whatever `root` is,
  so every existing caller and test of `validate(doc)` is unaffected.
- *fs* checks (template existence, resolution, confinement, readability, front matter,
  placeholders, empty body) run only when `root` is given.
- `load_project_config(path)` derives `root = Path(path).resolve().parent.parent` (the file is
  `<root>/.wt/project.yaml`) and calls `validate(doc, root=root)`. Both kinds of problem append to
  the one `problems` list, so the single `ValueError("invalid project config:\n  - …")` names all
  of them, each prefixed `reviewers[i].<key>`; template problems also name the file (and line
  where the problem has one).
- The loader then reads each template once for `template_text` / `template_sha256`; the read
  happens inside the same validation pass so a template cannot change between check and use.

Checks, per entry:

- `name`: required, `^[a-z][a-z0-9_]*$`, not ending in `_reviewer`, unique within the list; the
  persona id `<name>_reviewer` must not collide with a key of `personas.PERSONAS`.
- `phase`: `implementation` or absent. `spec` ⇒ `phase: spec reviewers are not supported yet`.
- `template` (*fs* for all but the first): required string, repo-relative, no `..` or absolute
  path, ends `.md`; after symlink resolution it must be a regular file **inside**
  `<root>/.wt/reviewers/` (a symlink escaping the directory, a directory, or a missing or
  unreadable file fails).
- `required`: bool. `when`: mapping accepting only `paths` and `labels`; `paths` a non-empty list
  of non-empty glob strings; `labels` a non-empty list of non-empty strings. Both given ⇒ both
  must match (slice 3).
- `categories`: non-empty list of distinct `^[a-z][a-z0-9_]*$` strings.
- `model`, `effort`, `max_budget_usd`: the same value rules as profile persona settings
  (`profiles.MODEL_ALIASES` / `MODEL_RE`, `profiles.EFFORTS`, a number > 0); imported lazily
  inside the check to avoid an import cycle. `read_only` and `permission_mode` are not accepted.
- Unknown keys fail (`reviewers[i].x: unknown key (allowed: …)`); `reviewers` itself must be a
  list.

### Template file

`.wt/reviewers/<name>.md` is a prompt template rendered by `personas.render` with the
`{{placeholder}}` syntax of the built-in templates. Optional YAML front matter (`---` fenced at
the top) may carry `description`, `model`, `effort`, `max_budget_usd`, validated like the entry
keys; any other front-matter key, or unparsable front matter, fails. The body may use only
`REVIEWER_PLACEHOLDERS` (a new constant in `personas.py`, a subset of `PLACEHOLDERS`: `campaign,
spec_id, spec_rel_path, spec_text, worktree, base, loop, loop_max, context_files, gate_results,
pending_brief, previous_verdicts, diff, test_files, changed_files, human_overrides`); an unknown
`{{x}}` fails naming the file and line. A body empty after stripping fails. When rendering a
declared persona, wt appends a generated "Output" section stating the findings schema (and the
reviewer's `categories` when declared) after the template, and the standard invariant block is
unchanged, so the author writes only the review lens.

### Persona registry

`personas.py` changes:

- `Persona` gains `template_text: str | None = None`; `template_file` becomes optional
  (`str | None`); the `template` property returns `template_text` when set, else reads the
  package file. Built-in personas are constructed as before.
- `PROJECT_REVIEWER_SCHEMA` / `project_reviewer_schema(categories=None)`: the shared findings
  schema, one function: `required: [verdict, score, findings, fix_brief]`; `verdict` enum
  `pass | fix`; `score` integer 0..100; `findings[{severity ∈ must_fix|major|minor|nit, title,
  detail?, files?[], lines?[], category?, ac?}]` with `required: [severity, title]`; `fix_brief`
  string. With `categories`, `category` becomes an enum of them and is required on each finding.
  No other output keys are read.
- `reviewer_registry(project_cfg) -> {persona_id: Persona}`: built from
  `project_cfg["reviewers"]` (`[]` or absent key ⇒ `{}`): `Persona(name=<id>, template_file=None,
  template_text=<generated: front-matter-stripped body + Output section>, schema=…,
  read_only=True)`. Only `phase: implementation` entries exist (the only phase the loader
  accepts).
- `declared_settings(project_cfg, persona_id) -> dict`: `{model, effort, max_budget_usd,
  read_only: True, permission_mode: "dontAsk"}` from, lowest first: wt defaults (`model: sonnet`,
  `effort: medium`, `max_budget_usd: 1.0`), template front matter, the `project.yaml` entry.
- `get_persona(name, project_cfg=None)`: built-ins first, then the registry; a declared id
  without a project config (or with a config that does not declare it) still raises the
  existing `ValueError`.
- `is_reviewer(name, project_cfg=None)`: `name in REVIEWER_PERSONAS` or `name in
  reviewer_registry(project_cfg)`.
- `build_request` passes `project_cfg` to `get_persona`, uses `is_reviewer` for the `review=`
  context (diff, test files), and for a declared reviewer takes its settings as
  `{**declared_settings(project_cfg, persona), **profile_entry}` where `profile_entry` is the
  profile's `personas.<id>` if present (slice 2 wires the profile layer; here a hand-built
  profile dict may carry it), with `read_only` forced to true. For built-ins the settings path
  is unchanged (`persona_settings(profile, persona)`).

`REVIEWER_PERSONAS` stays the built-in tuple, unchanged. The queue, divergence, adoption and
profile modules are not touched by this slice.

### `wt projects reviewers`

`wt projects reviewers [PROJECT] [--json]` (`wt.project.reviewers.v1`; PROJECT defaults to the
cwd's project like `wt projects config`) loads the config through `project_config` and prints,
per declaration: `name`, persona id, phase, `required`, `when`, template path, `template_sha256`,
and the declared model / effort / budget. No declarations ⇒ prints "no reviewers declared",
exit 0. An invalid `project.yaml` or template prints the loader's problem list and exits
non-zero (it reuses the `ValueError` as a `ClickException`, like `wt projects config`).
`wt projects config` includes the loaded `reviewers` (without `template_text`) in its output.

### Reading from the main checkout

`project_config(cfg, project)` already resolves the repo through the project registry, which
yields the main checkout; the template paths in the declaration are resolved against that root.
No function in this slice takes a worktree path, and the registry builder takes only an
already-loaded config, so a campaign worktree copy of `.wt/project.yaml` or
`.wt/reviewers/*.md` is never read.

### Starter file and docs

`wt projects init`'s starter `.wt/project.yaml` gets a commented `# reviewers: []` block (the
file stays valid and unchanged in behavior). `docs/product/capabilities/agentic.md` gains a
"Project reviewers" section documenting the declaration keys, the template rules and
`wt projects reviewers`, with the security-reviewer example above, and states plainly that
declared reviewers are validated and listed but only run once the review-set slice ships.

## Alternatives considered

- **Reviewers as a separate `.wt/reviewers.yaml`** — one more file to keep in sync and to
  validate; `project.yaml` already carries every other project-side agent setting.
- **Declaration only in template front matter** — makes discovery and validation depend on
  scanning a directory and gives the loader no single place to see names and order; front matter
  is kept as optional defaults only.
- **A separate template-validation pass outside the loader** — two entry points would let a bad
  template slip past one of them; extending `validate` with an optional `root` keeps one
  collected error.
- **Reading declarations from the campaign worktree** — the implementer could edit the reviewer
  that judges its own work (the SPEC-0186 concern).
- **Custom per-reviewer output schemas** — breaks triage, divergence and chips, which the shared
  findings schema protects; `categories` covers the realistic need.
- **Writing declarations as Python plugins** — executes repo code inside the runner; markdown
  plus validated YAML keeps the trust boundary of `project.yaml`.
- **Shipping all of IDEA-090 as one spec** — rejected by the spec review: 19 criteria over five
  subsystems and a change to the review gate; the slices land independently and each is testable
  alone.

## Acceptance criteria

- [x] `.wt/project.yaml` accepts a `reviewers:` list; `load_project_config` returns each entry
      with defaults filled (`phase: implementation`, `required: true`, empty `when`) plus
      `persona`, `template_sha256` and `template_text`; a file without the key loads as before
      with `reviewers: []`.
- [x] `validate(doc)` without `root` runs the structural checks only and behaves as before for
      every existing config; `load_project_config` runs structural and template checks in one
      pass and reports every problem in a single `ValueError`: unknown key, bad or duplicate
      `name`, a name ending `_reviewer`, a collision with a built-in persona, `phase: spec`, bad
      `when`, bad `categories`, bad `model`/`effort`/`max_budget_usd`, and a missing, absolute,
      `..`, symlink-escaping, non-`.md`, directory or unreadable `template`.
- [x] Template content is validated: front matter with unknown keys or invalid values, unknown
      `{{placeholder}}` (message names file and line) and an empty body each fail; the
      `project.yaml` entry wins over front-matter defaults.
- [x] `reviewer_registry` registers a declared reviewer as persona `<name>_reviewer`, read-only,
      with the shared findings schema (and the `categories` enum, required per finding, when
      declared); its template text is the body plus the generated Output section.
- [x] `get_persona(id, project_cfg)` and `is_reviewer(id, project_cfg)` find a declared reviewer;
      without a project config, or with one that does not declare it, `get_persona` still raises
      and `is_reviewer` is false; built-in lookups are unchanged.
- [x] `build_request` for a declared reviewer yields a read-only request (`dontAsk`, restricted)
      with the review context (diff, test files), settings
      `wt defaults < front matter < project.yaml entry`, a profile `personas.<id>` entry on top,
      and `read_only` true even if the profile entry says false.
- [x] Declarations and templates are read from the registry-resolved main checkout: editing
      `.wt/project.yaml` or `.wt/reviewers/x.md` in a campaign worktree changes nothing returned
      by `project_config` or the registry.
- [x] `wt projects reviewers [--json]` lists declarations (name, persona id, phase, required,
      when, template path and sha256, declared settings), prints "no reviewers declared" for
      none, emits `wt.project.reviewers.v1`, and exits non-zero with the problem list on an
      invalid config; `wt projects config` shows the loaded `reviewers`.
- [x] With no `reviewers:` declared, nothing changes: `PERSONAS`, `REVIEWER_PERSONAS`, profile
      validation (a profile naming `foo_reviewer` is still rejected), the queue, divergence,
      prompts, chips and events of existing campaigns are identical, and the starter
      `project.yaml` still validates.
- [x] The capability page documents the declaration, template rules and `wt projects reviewers`
      with a worked example and says declared reviewers do not run yet; the starter file carries
      the commented block.

## Test plan

- **Automated tests** (new `tests/test_agentic_project_reviewers.py`; extended
  `tests/test_agentic_project.py`, `tests/test_agentic_personas.py`; offline, temporary repos,
  no runner needed):
  - `test_declaration_defaults_and_absent_key`: defaults filled, `persona` / `template_sha256`
    / `template_text` present; no key ⇒ `reviewers == []`; an older-style file still loads.
  - `test_validate_without_root_is_structural_only`: `validate(doc)` with a missing template
    file passes the fs checks (skipped) but fails a structural defect.
  - `test_declaration_validation_collects_all_problems`: one project file with a dozen defects
    (the AC 2 list, structural and fs mixed); the single error names each
    `reviewers[i].<key>`.
  - `test_template_confined_to_reviewers_dir`: `..`, absolute, symlink escape, a directory, a
    non-`.md` file and an unreadable file are rejected; a symlink resolving inside the directory
    is accepted.
  - `test_template_front_matter_and_placeholders`: entry beats front matter; unknown
    front-matter key, bad value, unknown placeholder (file and line in the message) and empty
    body fail.
  - `test_persona_registry_and_schema`: persona id, read-only, schema, `categories` enum and
    required category, generated Output section, built-in id collision rejected; `get_persona`
    and `is_reviewer` with, without and with a non-declaring project config.
  - `test_build_request_for_declared_reviewer`: read-only mode, review context present,
    settings layering, profile entry on top, `read_only: false` in the profile entry ignored.
  - `test_declarations_read_from_main_checkout`: a temp repo registered as a project plus a git
    worktree whose copies of `project.yaml` and the template differ; `project_config` and the
    registry reflect the main checkout.
  - `test_projects_reviewers_cli`: human and `--json` output (`wt.project.reviewers.v1`), the
    no-declaration message, invalid-config exit code and problem list, `wt projects config`
    includes `reviewers` without `template_text`.
  - `test_builtin_behavior_unchanged_without_declarations`: registry empty, `REVIEWER_PERSONAS`
    and `PERSONAS` keys unchanged, profile validation of `foo_reviewer` still fails, the starter
    file validates and carries the commented block.
- **Manual verification:** in a scratch repo registered as a wt project, add the example
  declaration and template, run `wt projects reviewers` and `wt projects reviewers --json`,
  then introduce a bad placeholder and a `..` template path and confirm one error listing both.
  Record the commands and output in the spec's "As shipped" notes.
- **Regression guard:** the existing project, persona, profile, reviewer, triage and divergence
  tests pass unmodified; `uv run pytest` and `python3 tools/spec_lint.py` (the docs build runs
  inside pytest).

### Test record (2026-10-30)

- `uv run pytest tests/test_agentic_project_reviewers.py tests/test_agentic_personas.py
  tests/test_agentic_project.py` — the new tests of the Test plan (declaration defaults, structural-only
  `validate(doc)`, collected problems, template confinement, front matter / placeholders, registry
  and schema, `build_request`, main-checkout read, CLI, built-in behavior unchanged) pass, as do the
  pre-existing tests of those files, unmodified.
- `uv run pytest -q -x tests/test_agentic_*.py tests/test_docs_*.py tests/test_spec*.py
  tests/test_projects*.py` — 2062 passed, 2 skipped (agentic, docs build and spec/ledger drift tests).
- Manual verification: see "As shipped".
- `python3 tools/spec_lint.py` exits 0; the ledger was regenerated.
- The `wt projects reviewers --json` emitter was added to the rich-output allowlist
  (`tests/test_rich_output_guard.py`) after gating flagged it; the full `uv run pytest -q` then
  ran: 3726 passed, 3 skipped.
- Review round fixes (2026-10-30): names and categories are matched with `fullmatch` (a trailing
  `\n` no longer passes); `load_project_config` takes the repo root from the unresolved
  `.wt/project.yaml` path (a symlinked file keeps its repo; only the template is resolved);
  `build_request` forces `permission_mode: dontAsk` for a declared reviewer on top of the profile
  entry. New tests cover the duplicate / non-list / non-string categories, trailing-newline name and
  category, every `when` shape (`labels`, non-string and empty entries, non-mapping, `when: null`),
  non-mapping front matter, non-string `description`, a symlinked `project.yaml`, and
  `bypassPermissions` in a profile entry. `uv run pytest -q tests/test_agentic_project_reviewers.py
  tests/test_agentic_personas.py tests/test_agentic_project.py` — 121 passed. The full suite is left
  to gating.

- Test-review fixes (2026-11-02): added assertions that front matter is stripped from a declared
  reviewer's template and prompt (AC4), a test of the wt-defaults settings layer
  (`sonnet` / `medium` / 1.0, read-only, `dontAsk`) through `declared_settings` and `build_request`
  (AC6), sha256 / `when` in the CLI human output, and an invalid non-spec `phase` test.
  `uv run pytest -q tests/test_agentic_personas.py tests/test_agentic_project_reviewers.py
  tests/test_agentic_project.py` — 123 passed. The full suite is left to gating.

### As shipped (2026-10-30)

- `project.validate(doc, root=None)` keeps its signature for existing callers; the work is in a
  private `_validate(doc, root)` that also returns the template texts it read, so
  `load_project_config` validates and reads each template in one pass. Front matter parsing is
  `project.split_front_matter`, shared with `personas`.
- `personas.declared_settings` returns the settings with `permission_mode: dontAsk` (the
  Design's precedence list); `build_request` forces `read_only` true on top of the profile entry.
- `personas.build_request` no longer tests `REVIEWER_PERSONAS` membership for the review
  context: it uses `is_reviewer(persona, project_cfg)`, so a declared reviewer gets the diff.
- `wt projects reviewers` prints one two-line block per declaration; `wt projects config`
  drops `template_text` from the listed reviewers.
- Manual run in a scratch repo (`/tmp/scratch-rev/repo`, registered through `outbox_targets`):
  `wt projects reviewers scratch` printed `security → security_reviewer  phase=implementation
  required=true  when: paths=['src/crypto/**']` with the template path and sha256; `--json`
  emitted `wt.project.reviewers.v1`. After adding an unknown `{{bogus}}` placeholder and a second
  entry with template `.wt/reviewers/../project.yaml`, the command exited 1 with one
  `invalid project config:` error listing `reviewers[0].template: …security.md:1: unknown
  placeholder {{bogus}}` and `reviewers[1].template: must not contain '..'`.
- The follow-up ideas of Rollout step 2 are not filed from this run; they are listed in the
  verdict's `followup_ideas`.

## Rollout / migration

1. Land this slice: loader, validation, persona registry, `wt projects reviewers`, starter block
   and docs. Inert without declarations; no data migration; wt's own `.wt/project.yaml` declares
   nothing.
2. File three follow-up ideas, one per remaining slice (profile resolution; review set, routing
   and cost cap; display and analysis), each linked to `IDEA-090` and carrying the decisions
   recorded under Non-goals, plus the idea for spec-phase reviewers.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None._ Decided: the idea is delivered in four slices and only the first is this spec; the
follow-up slices' open design points (advisory waiting rule, review-set inputs, `changed_files`
failure behavior) are decided under Non-goals; spec-phase pluggable reviewers need a fan-out for
the in-campaign spec review and a findings-based variant of the `approve | revise | split`
verdict (SPEC-0214/SPEC-0230) and are a separate idea.
