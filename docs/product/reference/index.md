# Command reference

Live CLI is authoritative: `nw --help` and `nw <command> --help`. This page is a
**curated index** of top-level commands (synced by test to `nw --help`). It does not
duplicate every flag.

Bare `nw` (no subcommand) prints the today + this-week dashboard. Add `--split` for the
Claude-generating vs your-turn diagnostic.

## Index

| Command | Purpose | See also |
|---------|---------|----------|
| *(bare)* `nw` | Dashboard: today + this week | [Report](../capabilities/report.md) |
| `add` | Capture a new org task | [Tasks](../capabilities/tasks-agenda.md) |
| `agenda` | Tasks by SCHEDULED/DEADLINE (+ overdue) | [Tasks](../capabilities/tasks-agenda.md) |
| `backups` | Org-file backup retention: `prune` (dry run unless `--yes`) | [Tasks](../capabilities/tasks-agenda.md), [Ideas](../capabilities/ideas.md) |
| `campaign` | Campaign state, ledger and notes (agentic loop) | [Agentic loop](../capabilities/agentic.md) |
| `completion` | Generate / install shell completion | [Completion](../capabilities/completion.md) |
| `digest` | Hours joined to org tasks by JIRA key | [Report](../capabilities/report.md) |
| `done` | Mark a task done | [Tasks](../capabilities/tasks-agenda.md) |
| `export` | Save date/topic/hours/facets dataset | [Report](../capabilities/report.md) |
| `hub` | Read-only triage JSON (ideas + specs + tasks) | [Hub](../capabilities/hub.md) |
| `idea` | Capture or enrich ideas (subcommands below) | [Ideas](../capabilities/ideas.md) |
| `ideas` | List / filter / query / tree ideas | [Ideas](../capabilities/ideas.md) |
| `ingest-meetings` | Filter a raw calendar JSON dump | [Meetings](../capabilities/meetings.md) |
| `mailbox` | Conductor ↔ steward messages (agentic loop) | [Agentic loop](../capabilities/agentic.md) |
| `escalations` | Questions escalated to the human: list, show, withdraw (agentic loop) | [Agentic loop](../capabilities/agentic.md) |
| `map` | Assign facets to a base topic | [Report](../capabilities/report.md) |
| `migrate-from-wt` | One-time move of a pre-rename `wt` install to nightwright's config/data dirs (dry run unless `--yes`, SPEC-0330) | [Bootstrap](../guides/bootstrap.md) |
| `next` | Next-step hints for open ideas | [Ideas](../capabilities/ideas.md) |
| `override` | Time-scoped reclassification | [Report](../capabilities/report.md) |
| `profiles` | Agentic workflow profiles: list / show (with sources) / check | [Agentic loop](../capabilities/agentic.md) |
| `projects` | List / register outbox + research targets | [Projects](../capabilities/projects.md) |
| `queue` | Agentic work queue: add / list / run / park / resume | [Agentic loop](../capabilities/agentic.md) |
| `refresh-meetings` | Pull Outlook meetings into meetings.jsonl | [Meetings](../capabilities/meetings.md) |
| `release-archive` | Pack git HEAD + ideas slice tarball | [Release archive](../capabilities/release-archive.md) |
| `report` | Per-topic hours | [Report](../capabilities/report.md), [Track time](../guides/track-time.md) |
| `review` | Segment timeline / unclassified / orphans | [Report](../capabilities/report.md) |
| `sheet` | Sheet-sync plan/record (JSON-only) | [Sheet](../capabilities/sheet.md) |
| `skills` | Install/list agent workflow skills | [Skills](../capabilities/skills.md) |
| `snapshot` | Archive past days to history.jsonl | [Track time](../guides/track-time.md) |
| `spec` | Spec scaffold / generate / export / verify | [Idea → ship](../guides/idea-to-ship.md), [Outbound](../guides/outbound.md) |
| `state` | Set a task's TODO state | [Tasks](../capabilities/tasks-agenda.md) |
| `tasks` | List org tasks | [Tasks](../capabilities/tasks-agenda.md) |
| `topics` | Discovered base topics + facets | [Report](../capabilities/report.md) |
| `tui` | Interactive ideas desk (optional `tui` extra) | [Desk](../capabilities/tui.md) |
| `worker` | Remote gate worker, run on the worker host: `gate-job` / `have` / `gc` / `info` / `ssh-command` (SPEC-0336) | [Agentic loop](../capabilities/agentic.md) |

## Nested groups (high level)

### `nw idea …`

Capture (`nw idea "…"`), `show`, `summary`, `log`, `questions`, `mark-explored`,
`clock-in` / `clock-out`, `state`, `project`, `tags`, `kind`, `priority`, `workstream`,
`retitle`, `close`, `ext`, plus maintenance helpers (`normalize-logs`, …). Full list:
`nw idea --help`.

### `nw spec …`

`new`, `generate`, `verify`, `status`, `export`, `pull-status`, `pull-clock`, `sweep`, `reconcile`,
`schemes`, `seed-children`, `fit-log`, `postmortem`. Full list: `nw spec --help`.

### `nw projects …` / `nw sheet …` / `nw skills …` / `nw completion …` / `nw queue …` / `nw campaign …`

Subcommands via `nw <group> --help`. `nw projects` adds `config` / `init` for the per-project
`.nw/project.yaml` ([Agentic loop](../capabilities/agentic.md)).

## Date scopes

Shared by `report`, `review`, `agenda`, `digest`, `refresh-meetings`:

```bash
nw report                 # today
nw report --week          # this week
nw report 2026-06-01      # that day
nw report -w 2026-06-01   # week containing that date
nw report --last 7
```
