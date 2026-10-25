# Agentic loop

A deterministic queue moves **accepted specs** through implementation, gates, triage and
merge without a human at the keyboard. Judgment steps (implement, triage a gate failure)
run as fresh headless Claude personas with a budget and a timeout; every transition, run,
gate result, finding and dollar lands in a per-campaign ledger. Humans see an item only
when the loop has **parked** it. Governing epic: SPEC-0169; the sections below are filled
in by its child specs as they land.

## Project configuration

`<repo>/.wt/project.yaml` is committed with the project and declares what the loop needs
to know about it (SPEC-0170):

- `gates`: ordered shell commands (`name`, `cmd`, `cwd`, `timeout_s`, `required`) run
  inside the campaign worktree; `required` gates block, optional ones only report.
- `merge`: `policy` (`merge` | `pr` | `draft-pr`), `base`, `remote`, `push_after_merge`,
  `delete_branch`, `post_cmds`, `pr.labels` / `reviewers` / `title_prefix`.
- `worktrees`: `root` (default `<repo>/../.wt-worktrees/<project>`), `setup` commands,
  `cleanup` (`on_merge` | `on_close` | `never`).
- `agents`: `context_files` read first by every persona, `permission_mode`,
  `allowed_tools`, `commit_trailer`.

Unknown keys fail; every problem is reported by key path. Gate commands may use the
placeholders `{spec_id}`, `{campaign}`, `{worktree}`, `{base}`.

wt finds the file through the project registry (`wt projects --json`): an outbound
project's `repo_path`, or this wt checkout for `Meta-Tools`. Never from the current
directory alone.

```bash
wt projects config Meta-Tools          # resolved document, defaults filled
wt projects config --json              # wt.project.config.v1 (project from cwd)
wt projects init acme                  # print a starter; --yes writes it, --force overwrites
```

## Campaigns and the ledger

A campaign is one spec's trip through the loop: `state.json` plus an append-only
`ledger.jsonl` under the wt data dir (SPEC-0171). `wt campaign list|show|log|note`.

## Profiles

Models, effort, budgets, loop limits and slots live under `agentic:` in `config.yaml`;
builtin profiles `balanced`, `frugal`, `thorough` (SPEC-0171).

## Gates

Project commands (tests, lint) run in the campaign worktree with a timeout; `required`
gates block, optional ones only report (SPEC-0173). `wt queue gates CAMPAIGN` runs them on
demand.

## Personas

`claude -p` with a structured verdict, a dollar budget and a wall-clock timeout; a
scripted fake runner for tests (SPEC-0174).

## Closing

Rebase, re-run every gate on the rebased tree, audit the spec's Definition of Done, then
fast-forward the base branch or open a PR (SPEC-0175).

## Queue

`wt queue add|list|watch|tick|run|park|resume|abandon` and the state machine
(SPEC-0176).

## Conductor

`/wt-conduct` starts the loop, watches it and unparks items (SPEC-0177).
