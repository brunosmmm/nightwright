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

A campaign is one spec's trip through the loop. Each lives under
`<data_dir>/agentic/campaigns/<id>/` as an atomically written `state.json` (schema
`wt.campaign.v1`: state, counters, cost, last gate, park reason) plus an append-only
`ledger.jsonl` with one event per line (`open`, `transition`, `run.start`/`run.end`,
`commit`, `gate`, `finding`, `cost`, `note`, `park`, `resume`, `close`), each carrying a
monotonic `seq`. Writers hold the campaign's `flock`, so the loop and a human command can
touch the same campaign safely. Campaign ids are `<SPEC-ID>-<YYYYMMDD>-<hex>`.

```bash
wt campaign list                 # open campaigns (--all for terminal ones)
wt campaign show CAMPAIGN        # state + last events (--json → wt.campaign.v1)
wt campaign log CAMPAIGN --tail 20   # the ledger (--json → wt.campaign.log.v1)
wt campaign note CAMPAIGN "text" # append a note; inside a persona run $WT_CAMPAIGN is set
```

A single `wt queue run` holds the runner lease (`queue.lock` + `queue.lease.json` with a
heartbeat); a second runner is refused and names the holder. A heartbeat older than
`agentic.lease_ttl_s` is reported as `stale`, never taken over (SPEC-0171).

## Profiles

Models, effort, budgets, loop limits and slots live under `agentic:` in `config.yaml`.
Builtin profiles `balanced` (default: Sonnet implementer and triager, $10 cap, 3 loops,
2 slots), `frugal` and `thorough` ship in code; the user block holds only deltas, deep-merged
over the builtin of the same name (SPEC-0171):

```yaml
agentic:
  default_profile: balanced
  lease_ttl_s: 120
  profiles:
    balanced:
      personas:
        implementer: {max_budget_usd: 6.0}
```

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
