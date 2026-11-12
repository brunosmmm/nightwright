---
id: SPEC-0561
title: Lint outbound specs before export (nw spec lint, tools/spec_lint.py --outbox)
status: proposed
owner: user
created: 2026-11-12
kind: feature
source_idea: IDEA-1135
tags: [specs, outbox, lint]
---

## Context

Outbound specs (`<data_dir>/outbox/<project>/<PROJ>-NNNN-slug.md`, SPEC-0015 / SPEC-0021) are
the portable specs nightwright file-drops into other repos. Internal specs have a linter
(`tools/spec_lint.py`: schema, required sections, real Test plan, placeholders, references,
duplicate ids). Outbound specs have nothing equivalent:

- `tools/spec_lint.py` only globs `docs/specs/[0-9]*.md` (`load_specs`, `SPECS`), by design in
  SPEC-0015 ("the internal `tools/spec_lint.py` is left untouched").
- `nw spec` has no `lint` subcommand (`src/nightwright/cli.py`, `spec_group`), although the
  `tools/spec_lint.py` docstring says it is "also exposed as `nw spec lint`". `nw spec verify`
  refuses outbound ids (`verify_spec`, `src/nightwright/verify.py`).
- The only outbound validation is inside `export_spec` (`src/nightwright/export.py`): the
  export scheme's `validate` (for nw-native: id, title, Context, Decision, Acceptance criteria,
  Test plan non-empty) plus a product-fit outcome warning. It runs at export time, so a
  malformed spec (for example JEROME-0022: no `kind`, `status: shipped`, a frozen
  `target_spec_path`) is only found when it is already being dropped into the target repo, and
  placeholders, an N/A Test plan on a feature, a missing Definition of done, duplicate ids,
  filename/id mismatches and unchecked boxes on a done spec are never found at all.

The result: an author or the spec-writer agent cannot answer "is this outbound spec ready to
export?" without running the export.

## Goals / Non-goals

**Goals**
- One validator for outbound specs that can run before `nw spec export`, on one outbound id,
  on one project directory, or on the whole outbox.
- Reach it both ways the idea names: a `nw spec lint` subcommand and a `--outbox` mode of
  `tools/spec_lint.py`, backed by the same code so the two cannot disagree.
- Never pass a spec that `nw spec export` would reject for validation reasons: the export
  scheme's own `validate` is part of the lint.
- Machine-readable output (`--json`) so skills and agents can gate on it.

**Non-goals**
- Changing `nw spec export` (it keeps its own validation and `--force` semantics; no new gate).
- Linting internal specs from `nw` (`tools/spec_lint.py` without `--outbox` is unchanged and
  remains the internal linter and ledger generator).
- A JSON Schema for outbound frontmatter, or rejecting unknown frontmatter keys: outbound specs
  legitimately carry extra keys (`target_project`, `target_repo`, `target_spec_path`, `outcome`,
  `scope_paths`, and scheme-specific ones).
- Editing outbound spec files or the outbox `INDEX.md` / `PROVENANCE.md`; lint is read-only.

## Decision

Add `src/nightwright/outbox_lint.py`, a stdlib + PyYAML module (no `jsonschema`, so the
installed `nw` can import it) built on `nightwright.specmeta`, that lints outbound spec files and
returns structured problems with severity `error` or `warning`. Expose it as
`nw spec lint [OUTBOUND_ID ...] [--project P] [--json] [--strict]` and as
`python3 tools/spec_lint.py --outbox PATH`. Errors fail the command (exit 1); warnings do not
unless `--strict`. Export behavior is untouched.

## Design

### Module `src/nightwright/outbox_lint.py`

- `lint_file(path, *, cfg=None, siblings=None) -> list[Problem]` and
  `lint_outbox(root, *, cfg=None, project=None, ids=None) -> LintReport`.
  `Problem = (file, severity, code, message)`; `LintReport` holds one entry per spec file
  (`id`, `path`, `errors`, `warnings`) and an `ok` property (no errors).
- `root` may be the outbox root (`<root>/<project>/<PROJ>-NNNN-*.md`), one project directory,
  or one spec file. Only files whose name matches `^[A-Za-z0-9]+-\d{4}-.+\.md$` are linted, so
  `INDEX.md`, `PROVENANCE.md` and `*.contract.md` are skipped. `cfg` is the nw config; when
  given, the project's configured export scheme is resolved (`export.resolve_scheme`) and its
  `validate` run; without `cfg` (the `tools/` mode) the default nw-native `validate` runs.
- The module reuses `export._find_outbound_spec` for id lookup and `specs.outbox_dir(cfg)` for
  the default root rather than re-implementing them.

### Rules

Errors:
1. `frontmatter`: a `---` block exists and parses to a mapping.
2. `required-keys`: `id`, `title`, `status`, `owner`, `created` (ISO date), `target_project`.
3. `id-format`: `id` matches `^[A-Z0-9]+-\d{4}$`; its number equals the filename number; its
   prefix equals the project code derived from the directory name (`specs._proj_code`, e.g.
   `comfort-assistant` -> `COMFORTASSISTANT`); `target_project` equals the directory name.
4. `status`: one of `draft`, `proposed`, `accepted`, `in-progress`, `done`, `superseded`,
   `rejected`, or `shipped` (present on real outbox specs pulled back from target repos).
   `superseded` requires `superseded_by`. `kind`, when present, is one of `feature`, `epic`,
   `policy`, `spike` (absent means `feature`).
5. `sections`: Context, Decision, Acceptance criteria, Test plan and Definition of done exist
   and are non-empty (`specmeta.sections`).
6. `test-plan`: for `feature` and `epic` the Test plan is not an "N/A" plan; `policy` and
   `spike` may be N/A.
7. `placeholder`: for `accepted`, `in-progress`, `done` and `shipped` specs,
   `specmeta.first_placeholder` finds none (fenced and inline code ignored, same rule as the
   internal linter).
8. `unchecked`: a `done` or `shipped` spec has no unchecked `- [ ]` under Acceptance criteria
   or Definition of done.
9. `duplicate`: no two files in the outbox share an id or a `PROJ-NNNN` prefix within a project.
10. `target-spec-path`: when `target_spec_path` is set, its basename equals the spec's filename.
11. `scheme`: every problem returned by the resolved scheme's `validate(canonical)`
    (`export_schemes.canonical.parse_canonical`) is reported, so lint is never more permissive
    than export. A scheme that cannot be resolved is a `scheme` error naming the project.

Warnings:
12. `outcome`: no product-fit outcome signal (`canonical.has_outcome_signal`), mirroring the
    export-time warning.
13. `reference`: an id in `depends_on` / `supersedes` / `superseded_by` that has the outbound
    shape `PROJ-NNNN` but names no spec in the same project directory (SPEC-NNNN ids are
    internal and not resolved).
14. `design`: no Design section (epics: no `Architecture / cross-cutting design`).

### CLI

- `nw spec lint [OUTBOUND_ID ...] [--project P] [--json] [--strict]` in `spec_group`
  (`src/nightwright/cli.py`). No ids lints the whole outbox (optionally narrowed by
  `--project`); ids are resolved with `export._find_outbound_spec`, so an unknown or ambiguous
  id is a usage error (exit 2). An internal `SPEC-NNNN` id is a usage error that points to
  `python3 tools/spec_lint.py`. Human output is one `<file>: <severity> [<code>] <message>`
  line per problem and a final summary; `--json` prints
  `{"ok": bool, "files": [{"id", "path", "errors": [...], "warnings": [...]}]}`. Exit 0 when
  there are no errors (and no warnings under `--strict`), 1 otherwise.
- `tools/spec_lint.py --outbox PATH` imports `nightwright.outbox_lint` lazily (like
  `--check-source-ideas`, run via `uv run`), lints `PATH` (outbox root, project dir or file)
  without a cfg, and exits with the same codes. The default `tools/spec_lint.py` run, the
  ledger generation and `load_specs` are untouched. The module docstring is corrected to say
  `nw spec lint` covers outbound specs only.

### Documentation

- `docs/specs/SCHEMA.md` gains a short "Outbound specs" section stating the outbound frontmatter
  keys, the `shipped` status, and that `nw spec lint` is the validator.
- The `nw-export` skill text tells the agent to run `nw spec lint <OUTBOUND_ID>` before
  `nw spec export`; the `nw spec` group docstring is updated to mention `lint`.

## Alternatives considered

- **Extend `tools/spec_lint.py` to scan the outbox directly** — it requires `jsonschema` (dev
  only) and the data-dir path/config, and cannot run under the installed `nw`; the shared
  package module serves both entry points instead.
- **Make `nw spec export` run the full lint and refuse on errors** — changes the export
  contract (`--force`, existing exported specs) and is a separate decision; lint stays
  advisory and explicit, and its `scheme` rule already guarantees it covers export's hard
  failures.
- **A JSON Schema for outbound frontmatter** — outbound frontmatter is open-ended per scheme
  and target; a closed schema would false-positive on legitimate keys.

## Acceptance criteria

- [ ] `nightwright.outbox_lint` exists, imports without `jsonschema`, and `lint_outbox` accepts
      an outbox root, a project directory, or a single spec file.
- [ ] A well-formed outbound feature spec yields no errors and no warnings; a spec shaped like
      JEROME-0022 (no `kind`, `status: shipped`, frozen `target_spec_path`) yields no errors.
- [ ] Each error rule (1 to 11) is reported with its code for a spec that violates exactly that
      rule, and not for a spec that satisfies it; each warning rule (12 to 14) likewise, without
      failing the exit code.
- [ ] Any spec for which the nw-native `validate` returns problems gets a `scheme` error
      containing each of those problems, so lint never passes a spec export rejects.
- [ ] `INDEX.md`, `PROVENANCE.md` and `*.contract.md` in the outbox are not linted.
- [ ] `nw spec lint JEROME-0022` (and with other outbound ids) lints one spec; `nw spec lint`
      lints every spec in the outbox; `--project` narrows it; the exit code is 0 for no errors,
      1 for errors (or warnings with `--strict`), 2 for an unknown/ambiguous outbound id or an
      internal `SPEC-NNNN` id.
- [ ] `nw spec lint --json` prints the documented object and nothing else on stdout.
- [ ] `python3 tools/spec_lint.py --outbox PATH` produces the same problems and exit code as
      `nw spec lint` for the same files; `python3 tools/spec_lint.py` with no flags behaves
      exactly as before (existing lint tests pass unchanged).
- [ ] The lint never writes: file contents and mtimes under the outbox are identical after a run.
- [ ] `SCHEMA.md` documents outbound specs and `nw spec lint`; the `nw-export` skill names the
      pre-export lint step; `python3 tools/spec_lint.py` still exits 0 for this repo.

## Test plan

- **Automated tests:** new `tests/test_outbox_lint.py` with `pytestmark = pytest.mark.fast`
  (no TUI, remote or integration imports, so the SPEC-0490 taxonomy gives `fast`). It builds
  outbox trees under `tmp_path` and asserts:
  - `test_valid_spec_clean`, `test_jerome_shape_passes` (acceptance 2);
  - one `test_rule_<code>_*` per error rule 1 to 11 and warning rule 12 to 14, each with a
    violating and a satisfying fixture (acceptance 3), including `test_policy_may_be_na` and
    `test_placeholder_only_for_accepted_plus`;
  - `test_scheme_problems_surface` using a spec missing Decision and checking the `scheme`
    error text equals what `NwNativeScheme.validate` returns (acceptance 4);
  - `test_non_spec_files_skipped` (INDEX.md, PROVENANCE.md, `.contract.md`);
  - `test_duplicate_ids_across_files`;
  - `test_lint_is_read_only` (bytes and mtimes unchanged);
  - CLI tests through `click.testing.CliRunner` against a cfg pointing at the tmp outbox:
    `test_cli_single_id`, `test_cli_all_and_project`, `test_cli_exit_codes` (0 / 1 / 2, `--strict`,
    internal id), `test_cli_json_shape`;
  - `test_tools_outbox_mode_matches_cli` loading `tools/spec_lint.py` with `importlib` (as
    `tests/test_spec_lint_duplicates.py` does) and running `main` with `--outbox` against the
    same tree, comparing problems and exit code to the CLI;
  - `test_module_has_no_jsonschema_import` (AST check of `outbox_lint.py`).
- **Existing tests kept green:** `tests/test_spec_lint_duplicates.py`, `tests/test_export.py`,
  `tests/test_export_schemes.py`, and the repo-wide spec test that runs `tools/spec_lint.py`,
  run as affected tests with `python3 tools/affected_tests.py --run uv run pytest -q`.
- **Tier check:** `python3 tools/test_tiers.py --check` exits 0 (the new module carries its one
  module-level `pytestmark = pytest.mark.fast`).
- **Manual verification (real binary, against the real outbox):**
  1. `uv run nw spec lint JEROME-0022` exits 0 and prints no errors.
  2. `uv run nw spec lint` over the whole data-dir outbox: record the count of errors and
     warnings in the spec's "As shipped" notes. Any error that is a false positive on a
     legitimate real spec is fixed by correcting the rule, not by editing the outbound specs.
  3. `uv run python3 tools/spec_lint.py --outbox <data_dir>/outbox` reports the same problems.
  4. Copy one outbound spec to a temp dir, delete its `## Test plan` section, and confirm
     `nw spec lint` exits 1 with a `sections` and a `scheme` error, then that
     `nw spec export` on it would be rejected for the same reason.
- **Regression guard:** `python3 tools/spec_lint.py` (internal mode) exits 0 and the generated
  ledger block is unchanged; `nw spec export` tests pass without modification.

## Rollout / migration

1. Land `outbox_lint.py` with its tests, then the CLI subcommand and the `tools/` flag.
2. Update `SCHEMA.md`, the `nw spec` group docstring, the `tools/spec_lint.py` docstring and the
   `nw-export` skill.
3. Run the manual real-outbox pass and note the result in the spec; no data migration.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; `python3 tools/test_tiers.py --check` exits 0;
      manual steps performed against the real binary and noted.
- [ ] No regressions (internal lint and export tests unchanged and green).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

- (none)
