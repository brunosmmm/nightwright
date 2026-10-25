# Command reference

Live CLI is authoritative: `wt --help` and `wt <command> --help`. This page is a
**curated index** of top-level commands (synced by test to `wt --help`). It does not
duplicate every flag.

Bare `wt` (no subcommand) prints the today + this-week dashboard. Add `--split` for the
Claude-generating vs your-turn diagnostic.

## Index

| Command | Purpose | See also |
|---------|---------|----------|
| *(bare)* `wt` | Dashboard: today + this week | [Report](../capabilities/report.md) |
| `add` | Capture a new org task | [Tasks](../capabilities/tasks-agenda.md) |
| `agenda` | Tasks by SCHEDULED/DEADLINE (+ overdue) | [Tasks](../capabilities/tasks-agenda.md) |
| `campaign` | Campaign state, ledger and notes (agentic loop) | [Agentic loop](../capabilities/agentic.md) |
| `completion` | Generate / install shell completion | [Completion](../capabilities/completion.md) |
| `digest` | Hours joined to org tasks by JIRA key | [Report](../capabilities/report.md) |
| `done` | Mark a task done | [Tasks](../capabilities/tasks-agenda.md) |
| `export` | Save date/topic/hours/facets dataset | [Report](../capabilities/report.md) |
| `hub` | Read-only triage JSON (ideas + specs + tasks) | [Hub](../capabilities/hub.md) |
| `idea` | Capture or enrich ideas (subcommands below) | [Ideas](../capabilities/ideas.md) |
| `ideas` | List / filter / query / tree ideas | [Ideas](../capabilities/ideas.md) |
| `ingest-meetings` | Filter a raw calendar JSON dump | [Meetings](../capabilities/meetings.md) |
| `map` | Assign facets to a base topic | [Report](../capabilities/report.md) |
| `next` | Next-step hints for open ideas | [Ideas](../capabilities/ideas.md) |
| `override` | Time-scoped reclassification | [Report](../capabilities/report.md) |
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

## Nested groups (high level)

### `wt idea …`

Capture (`wt idea "…"`), `show`, `summary`, `log`, `questions`, `mark-explored`,
`clock-in` / `clock-out`, `state`, `project`, `tags`, `kind`, `priority`, `workstream`,
`retitle`, `close`, `ext`, plus maintenance helpers (`normalize-logs`, …). Full list:
`wt idea --help`.

### `wt spec …`

`new`, `generate`, `verify`, `export`, `pull-status`, `pull-clock`, `sweep`, `reconcile`,
`schemes`, `seed-children`, `fit-log`, `postmortem`. Full list: `wt spec --help`.

### `wt projects …` / `wt sheet …` / `wt skills …` / `wt completion …` / `wt queue …` / `wt campaign …`

Subcommands via `wt <group> --help`. `wt projects` adds `config` / `init` for the per-project
`.wt/project.yaml` ([Agentic loop](../capabilities/agentic.md)).

## Date scopes

Shared by `report`, `review`, `agenda`, `digest`, `refresh-meetings`:

```bash
wt report                 # today
wt report --week          # this week
wt report 2026-06-01      # that day
wt report -w 2026-06-01   # week containing that date
wt report --last 7
```
