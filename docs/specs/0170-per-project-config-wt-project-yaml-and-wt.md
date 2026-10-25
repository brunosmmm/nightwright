---
id: SPEC-0170
title: "Per-project config .wt/project.yaml and wt projects config|init"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, config, projects]
source_idea: IDEA-017
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-017`).

The agentic loop needs to know, per project, how to prove a change (gate commands), how a
finished change lands (merge policy), and how to prepare a worktree. Today wt keeps
per-project state in the user's `config.yaml` under `outbox_targets[project]` (`repo_path`,
`spec_dir`, `scheme`, `sheet`, `extra_search_paths`; `rules.py:55-236`). That is the right
place for *where a project lives*, but gates and merge policy belong to the project and
its contributors, versioned with the code. The user decided: this config lives **in the
project repo** at `.wt/project.yaml`.

Project → repo root already has one sanctioned path: `rules.resolve_research_context`
(`rules.py:219-268`), which handles outbound projects (`repo_path`) and the internal
`Meta-Tools` project (`wt_checkout`, SPEC-0166). This spec resolves through it and never
from the current directory alone (SPEC-0079's lesson).

## Goals / Non-goals

**Goals**
- A strict, defaulted loader for `<repo>/.wt/project.yaml`, with every problem reported by
  key path in one `ValueError`.
- `project_config(cfg, project)` resolves the repo through the existing project registry.
- `wt projects config [NAME] [--json]` shows the resolved document (`wt.project.config.v1`);
  `wt projects init [NAME] [--yes]` writes a starter file, dry-run by default.
- Gate `cmd` placeholders `{spec_id} {campaign} {worktree} {base}` expanded by one helper.

**Non-goals**
- Running gates (SPEC-0173), profiles and budgets (SPEC-0171, user config), merge logic
  (SPEC-0175).
- Moving `outbox_targets` into the repo. Location stays in user config.
- A JSON Schema file for the project config. `jsonschema` is a dev-only dependency;
  validation is hand-rolled, like `extension_schema.normalize_extension_schema`.

## Decision

Add `src/wt/agentic/project.py` with `PROJECT_DEFAULTS`, `load_project_config(path)`,
`validate(doc)`, `project_config(cfg, project)`, `expand_cmd(cmd, **placeholders)` and
`find_project_for_cwd(cfg)`. Add `config` and `init` subcommands to the existing `projects`
group (not a new top-level `wt project`, which would sit next to `wt projects` and invite
mistakes). Unknown keys fail, matching `spec.schema.json`'s `additionalProperties: false`.

## Design

### File shape (`schema: wt.project.v1`)

```yaml
schema: wt.project.v1
project: Meta-Tools            # optional; cross-checked against the registry when present
base: main
gates:                         # ordered; all run every time (SPEC-0173)
  - name: pytest
    cmd: uv run pytest -q -x
    cwd: .                     # relative to the worktree
    timeout_s: 1200
    required: true
merge:
  policy: merge                # merge | pr | draft-pr
  base: main                   # defaults to top-level base
  remote: origin
  push_after_merge: false
  delete_branch: true
  post_cmds: []
  pr: {labels: [], reviewers: [], title_prefix: ""}
worktrees:
  root: null                   # null ⇒ <repo>/../.wt-worktrees/<project>; else absolute or repo-relative
  setup: []
  cleanup: on_merge            # on_merge | on_close | never
agents:
  context_files: []
  permission_mode: bypassPermissions   # bypassPermissions | dontAsk | acceptEdits
  allowed_tools: []
  commit_trailer: "Co-Authored-By: Claude <noreply@anthropic.com>"
```

### `src/wt/agentic/project.py`

- `PROJECT_DEFAULTS`: the full default document above (deep-copied on load).
- `load_project_config(path) -> dict`: `yaml.safe_load`; a non-mapping or missing `schema:
  wt.project.v1` is an error; then `validate`, then defaults merged **per section**
  (`merge`, `worktrees`, `agents`, `pr`) and per gate (`cwd`, `timeout_s`, `required`).
- `validate(doc) -> None`: collects problems, raises `ValueError("\n".join(problems))`.
  Checks: unknown top-level / section keys; `base` non-empty string; `gates` is a list
  with unique non-empty `name`, non-empty `cmd`, `timeout_s` positive int, `required`
  bool; `merge.policy` in the enum; `worktrees.cleanup` in the enum;
  `agents.permission_mode` in the enum; list fields are lists of strings.
- `project_config(cfg, project) -> dict`: `rules.resolve_research_context(cfg, project)`;
  `root` None ⇒ `ValueError` carrying the context `note`; file missing ⇒ `ValueError("no
  .wt/project.yaml under <root>; run `wt projects init <project>`")`. Returns the loaded
  document plus `_root` (absolute repo root) and `_path`.
- `worktree_root(doc, repo_root, project) -> Path`: `root` null ⇒
  `repo_root.parent / ".wt-worktrees" / project`; relative ⇒ relative to `repo_root`.
- `expand_cmd(cmd, **values) -> str`: replaces only the four known placeholders with
  `str.replace`, never `str.format` (commands contain braces).
- `find_project_for_cwd(cfg) -> str | None`: the registry project whose resolved root is
  the git toplevel of cwd (`topics.RepoResolver`), else None. `Meta-Tools` wins ties only
  when cwd is the wt checkout.

### CLI (`src/wt/agentic/cli.py`, attached to `projects_group` from `wt/cli.py`)

- `wt projects config [NAME] [--json]`: NAME defaults to `find_project_for_cwd`; human
  output is the resolved YAML (`yaml.safe_dump(sort_keys=False)`) with a header line
  naming `_path`; `--json` emits `{"schema": "wt.project.config.v1", "project", "root",
  "path", "config": <doc>}`. Errors → `click.ClickException`.
- `wt projects init [NAME] [--yes]`: prints the starter document and the target path;
  `--yes` writes it (refuses to overwrite an existing file unless `--force`). Dry-run
  default, same wording as `projects add` (`not written — re-run with --yes`).
- `NAME` gets `shell_complete=C.complete_project` (the completion guard requires it for a
  param named `project`; the argument is named `project`).

### Docs

`docs/product/capabilities/agentic.md` is created here as a stub section ("Project
configuration") so the nav entry exists from the first child; later children fill it in.
`docs/product/reference/index.md` gains the two subcommands under `wt projects`.

## Alternatives considered

- **Keep gates and merge policy in `outbox_targets`** — rejected. They are facts about the
  project's code, so they belong with it, and `Meta-Tools` has no outbox entry at all.
- **Resolve from cwd only** — rejected. SPEC-0079 removed cwd-relative resolution because
  tests silently hit the live repo; the registry path is explicit.
- **New `wt project` group** — rejected. `project` next to `projects` is a footgun; the
  subcommands fit the existing group and reuse its completer.

## Acceptance criteria

- [ ] A valid file loads with every default filled (gate `cwd`, `timeout_s`, `required`;
      `merge.*`, `worktrees.*`, `agents.*`).
- [ ] Unknown top-level or section keys, a missing `gates[].cmd`, a bad `merge.policy`, a
      non-positive `timeout_s` and a wrong `schema` each fail, and the message names the key
      path; several problems are reported in one error.
- [ ] `project_config(cfg, "Meta-Tools")` resolves through `wt_checkout`; an outbound
      project resolves through `outbox_targets[p].repo_path`; a missing file errors with
      the `wt projects init` hint; an unresolvable project errors with the registry note.
- [ ] `worktree_root` defaults to `<repo>/../.wt-worktrees/<project>` and honors absolute
      and repo-relative overrides.
- [ ] `expand_cmd` replaces the four placeholders and leaves other braces untouched.
- [ ] `wt projects config --json` emits `wt.project.config.v1`; `wt projects init` writes
      nothing without `--yes`, writes the starter with it, and refuses to overwrite.
- [ ] Completion guard, reference guard and `mkdocs build --strict` pass.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_project.py`: valid / invalid YAML fixtures
  (parametrized problems); both resolution paths (a tmp dir with `src/wt/` +
  `pyproject.toml` as `wt_checkout`; `outbox_targets` for an outbound name); missing file
  and unresolvable project errors; `worktree_root` cases; `expand_cmd`; CLI `config`
  (human + `--json`) and `init` (dry-run, `--yes`, overwrite refusal) through `CliRunner`
  with a patched `load_config`.
- **Manual verification:** from the wt checkout, `wt projects init Meta-Tools` prints the
  starter; `wt projects config Meta-Tools` fails with the init hint until SPEC-0177 commits
  the real file.
- **Regression guard:** full `uv run pytest`, including the completion, reference and
  mkdocs guards.

## Clock Log

Internal work: clock on IDEA-017 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Library + read-only command + dry-run writer. No existing behavior changes. wt's own
`.wt/project.yaml` is committed by SPEC-0177.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
