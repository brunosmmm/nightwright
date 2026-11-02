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
| `doctor` | Read-only, offline install health check: tools, config, skills, completion, agentic queue, projects; `--fix-hints`, `--json` (`nw.doctor.v1`), `--project NAME`; exit 1 on any fail (SPEC-0349) | [Getting started](../guides/getting-started.md) |
| `digest` | Hours joined to org tasks by JIRA key | [Report](../capabilities/report.md) |
| `done` | Mark a task done | [Tasks](../capabilities/tasks-agenda.md) |
| `export` | Save date/topic/hours/facets dataset | [Report](../capabilities/report.md) |
| `hub` | Read-only triage JSON (ideas + specs + tasks) | [Hub](../capabilities/hub.md) |
| `idea` | Capture or enrich ideas (subcommands below) | [Ideas](../capabilities/ideas.md) |
| `ideas` | List / filter / query / tree ideas | [Ideas](../capabilities/ideas.md) |
| `ingest-meetings` | Filter a raw calendar JSON dump | [Meetings](../capabilities/meetings.md) |
| `mailbox` | Conductor ↔ steward messages (agentic loop) | [Agentic loop](../capabilities/agentic.md) |
| `escalations` | Questions escalated to the human: `list [--all] [--json]` / `show` / `answer ID (--choose\|--text\|--waive\|--attest) [--yes]` / `withdraw` (agentic loop) | [Agentic loop](../capabilities/agentic.md) |
| `gate` | Standalone remote gate jobs on worker boxes, no campaign: `run [--project P] [--worktree PATH\|--branch B] [--base REF] [--tier loop\|close\|all] [--cmd CMD ...] [--by WHO] [--wait\|--detach] [--wait-s S] [--json]` (exit 0 green / 1 red / 2 cannot start / 3 box unavailable / 4 transport), `status ID [--json]` (`nw.gatejob.v1`), `logs ID [GATE]`, `cancel ID`, `list`; config `agentic.gate_jobs.{enabled,max_per_requester,max_slots,retention_days}` (SPEC-0381) | [Agentic loop](../capabilities/agentic.md) |
| `herdr` | herdr sidebar tokens + notifications for the queue: `watch [--interval S] [--conductor-pane ID] [--workspace ID] [--once] [--run-panes] [--run-pane-linger S]`, `status [--json]`; the conductor's follower starts `watch` by itself inside herdr (`agentic.herdr.auto_watch`, log `<data_dir>/agentic/conductor/herdr_watch.log`); a no-op outside herdr; `--run-panes` opens a tab per campaign with a running run, named after the work (SPEC-0361/0363/0365/0366, see `docs/herdr.md` in the repo) | [Agentic loop](../capabilities/agentic.md) |
| `map` | Assign facets to a base topic | [Report](../capabilities/report.md) |
| `migrate-from-wt` | One-time move of a pre-rename `wt` install to nightwright's config/data dirs (dry run unless `--yes`, SPEC-0330) | [Bootstrap](../guides/bootstrap.md) |
| `next` | Next-step hints for open ideas | [Ideas](../capabilities/ideas.md) |
| `override` | Time-scoped reclassification | [Report](../capabilities/report.md) |
| `personas` | Agentic persona registry: `list` / `show NAME` / `validate [--project NAME] [--json]` over the builtin, user (config dir `personas/`) and project (main-checkout `.nw/personas/`) layers, with each definition's origin file; `validate` exits 1 on any problem (SPEC-0370, SPEC-0371) | [Agentic loop](../capabilities/agentic.md) |
| `profiles` | Agentic workflow profiles: list / show (with sources) / check | [Agentic loop](../capabilities/agentic.md) |
| `projects` | List / register outbox + research targets | [Projects](../capabilities/projects.md) |
| `queue` | Agentic work queue: add / list / watch / run / park / resume, conductor tooling (subcommands below) | [Agentic loop](../capabilities/agentic.md) |
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
| `tui` | Interactive desk (optional `tui` extra): tabs ideas / hub / queue / events / mailbox (`--tab`), campaign screen (`--campaign`) | [Desk](../capabilities/tui.md) |
| `worker` | Remote gate worker, run on the worker host: `gate-job` / `have` / `gc` / `info` / `ssh-command` (SPEC-0336) | [Agentic loop](../capabilities/agentic.md) |
| `workers` | Coordinator's worker registry (`<agentic_dir>/workers.json`), distinct from `nw worker` which runs on the worker host: `add NAME --ssh DEST [--slots N] [--nw CMD] [--no-check]` / `list [--json]` / `check [NAME] [--json]` / `on NAME` / `off NAME` / `clear-cooldown NAME` (cooldown only; SPEC-0387) / `rm NAME` (SPEC-0337) / `update NAME [--force] [--dry-run]` upgrades a worker to this checkout's build over its restricted key (SPEC-0385); `update NAME --when-idle` drains it and lets the queue update it once idle, then resume (SPEC-0386; `agentic.workers.update_drain_timeout_h` default 6, `agentic.workers.auto_update` default false). A job-level error from a live worker (ENOSPC, a bug in a gate run) no longer cools the worker down: the job is retried on another slot; a worker below `agentic.workers.min_free_gb` (default 5) refuses new jobs (SPEC-0387) | [Agentic loop](../capabilities/agentic.md) |

## Nested groups (high level)

### `nw idea …`

Capture (`nw idea "…"`), `show`, `summary`, `log`, `questions`, `mark-explored`,
`clock-in` / `clock-out`, `state`, `project`, `tags`, `kind`, `priority`, `workstream`,
`retitle`, `close`, `ext`, plus maintenance helpers (`normalize-logs`, …). Full list:
`nw idea --help`.

### `nw spec …`

`new`, `generate`, `verify`, `status`, `export`, `pull-status`, `pull-clock`, `sweep`, `reconcile`,
`schemes`, `seed-children`, `fit-log`, `postmortem`, plus the review and numbering helpers:
`review` (reviewer persona; `--show`, `--profile`, `--path`), `reviews` (latest stored review
of every spec), `reserve` / `reservations` / `release` (SPEC-number reservations visible
across branches and worktrees). Full list: `nw spec --help`.

### `nw projects …`

`list` (the default), `add`, `rename OLD NEW` (dry run unless `--yes`), and `config` / `init`
for the per-project `.nw/project.yaml` ([Agentic loop](../capabilities/agentic.md)).

### `nw queue …`

`add` (specs or `IDEA-NNN`), `list`, `watch`, `run`, `tick`, `stop`, `park`, `resume`,
`abandon`, `prioritize`, `backlog`, `merge`, `gc`, `gates`, `gate-slots`, `sweep-prs`, plus
the conductor tooling: `events`, `followers`, `wakeups`, `runner-log`, `decision`,
`decisions`, `escalate`, `conduct-status`, `conductor-brief`, `conductor-cost`,
`conductor-session`, `conductor-launch`, `steward-launch` and `mvp-report`. Full list:
`nw queue --help`.

### `nw sheet …` / `nw skills …` / `nw completion …` / `nw campaign …`

Subcommands via `nw <group> --help`.

## Date scopes

Shared by `report`, `review`, `agenda`, `digest`, `refresh-meetings`:

```bash
nw report                 # today
nw report --week          # this week
nw report 2026-06-01      # that day
nw report -w 2026-06-01   # week containing that date
nw report --last 7
```
