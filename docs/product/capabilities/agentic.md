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

Gates are the project's own proof commands from `.wt/project.yaml` — tests, lint, anything
with an exit code (SPEC-0173). The loop runs them in the campaign worktree, **in order and
all of them every time**, so triage and humans see every failure at once rather than the
first one. Each gate has a `timeout_s`; a timeout kills the gate's whole process group.
`required: true` gates block the campaign; optional gates are recorded but never block.

Every gate writes one log under the campaign directory, `gates/NNN-<name>.log`, starting
with a header line that names the command and its working directory; the last 60 lines
travel with the result into the ledger (`gate` event) and into `wt campaign show`. Gate
commands see `WT_CAMPAIGN`, `WT_SPEC_ID`, `WT_WORKTREE`, `WT_BASE`, `WT_DATA_DIR` and
`WT_CONFIG_DIR`, and may use `{spec_id}`, `{campaign}`, `{worktree}`, `{base}` placeholders.

`wt queue gates CAMPAIGN` runs the gates on a campaign's worktree right now — for example
on a parked item — and logs a `gate` event. It never changes the campaign's state or its
`last_gate`, so a debug run cannot steer the queue. `--json` emits `wt.campaign.gate.v1`.

## Personas

Each judgment step is one fresh `claude -p` run (SPEC-0174): a persona markdown template
(`implementer`, `gate_triager`; reviewers come later) is filled with the campaign's context
— the spec text as it is on the branch, the worktree path, the last gate logs, the brief
from the previous loop — and ends with a fixed invariant block (work only in the worktree,
do not commit, finish with the JSON verdict). The runner passes a `--json-schema` so the
verdict is structured, a `--max-budget-usd`, a wall-clock timeout, the profile's model and
effort, and an explicit permission mode (`bypassPermissions` scoped to the worktree for the
implementer; `--restricted` for read-only personas). Cost, usage and session id come back in
the result and go to the ledger. Identical inputs give a byte-identical prompt; `wt campaign
prompt CAMPAIGN PERSONA` prints it without running anything.

For tests and dry runs, `agentic.runner: fake` (or `WT_AGENTIC_RUNNER=fake`) replays a JSON
script of scripted edits and canned verdicts instead of calling `claude`.

## Closing

Rebase, re-run every gate on the rebased tree, audit the spec's Definition of Done, then
fast-forward the base branch or open a PR (SPEC-0175).

## Queue

`wt queue add|list|watch|tick|run|park|resume|abandon` and the state machine
(SPEC-0176).

## Conductor

`/wt-conduct` starts the loop, watches it and unparks items (SPEC-0177).
