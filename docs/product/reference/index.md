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
| `dashboard` | Agentic dashboard: `snapshot [--json]` prints the read-only `nw.dashboard.v1` document and writes nothing. `install` writes `~/.config/systemd/user/nw-dashboard.service` and enables it. `status [--json]` (`nw.dashboard.status.v1`). `uninstall` removes the unit | [Agentic loop](../capabilities/agentic.md) |
| `doctor` | Read-only, offline install health check: tools, config, skills, completion, agentic queue, projects; `--fix-hints`, `--json` (`nw.doctor.v1`), `--project NAME`; exit 1 on any fail | [Getting started](../guides/getting-started.md) |
| `digest` | Hours joined to org tasks by JIRA key | [Report](../capabilities/report.md) |
| `done` | Mark a task done | [Tasks](../capabilities/tasks-agenda.md) |
| `export` | Save date/topic/hours/facets dataset | [Report](../capabilities/report.md) |
| `hub` | Read-only triage JSON (ideas + specs + tasks) | [Hub](../capabilities/hub.md) |
| `idea` | Capture or enrich ideas (subcommands below) | [Ideas](../capabilities/ideas.md) |
| `ideas` | List / filter / query / tree ideas | [Ideas](../capabilities/ideas.md) |
| `ingest-meetings` | Filter a raw calendar JSON dump | [Meetings](../capabilities/meetings.md) |
| `mailbox` | Conductor ↔ steward messages (agentic loop) | [Agentic loop](../capabilities/agentic.md) |
| `escalations` | Questions escalated to the human: `list [--all] [--json]` / `show` / `answer ID (--choose\|--text\|--waive\|--attest) [--yes]` / `withdraw` (agentic loop) | [Agentic loop](../capabilities/agentic.md) |
| `gate` | Standalone remote gate jobs on worker boxes, no campaign: `run [--project P] [--worktree PATH\|--branch B] [--base REF] [--tier loop\|close\|all] [--cmd CMD...] [--by WHO] [--wait\|--detach] [--wait-s S] [--json]` (exit 0 green / 1 red / 2 cannot start / 3 box unavailable / 4 transport / 130 cancelled), `status ID [--json]` (`nw.gatejob.v1`), `logs ID [GATE]`, `cancel ID`, `list` (no job id); config `agentic.gate_jobs.{enabled,max_per_requester,max_slots,retention_days,wait_s}` (waiters served first come first served; `max_slots` defaults to the whole box while no campaign holds or waits, half otherwise; requester defaults to `$USER@<project>`; `wait_s` 1800) | [Agentic loop](../capabilities/agentic.md) |
| `herdr` | herdr sidebar tokens + notifications for the queue: `watch [--interval S] [--conductor-pane ID] [--workspace ID] [--once] [--run-panes] [--run-pane-linger S]`, `status [--json]`; the conductor's follower starts `watch` by itself inside herdr (`agentic.herdr.auto_watch`, log `<data_dir>/agentic/conductor/herdr_watch.log`); a no-op outside herdr; `--run-panes` opens a tab per campaign with a running run, named after the work (see `docs/herdr.md` in the repo) | [Agentic loop](../capabilities/agentic.md) |
| `map` | Assign facets to a base topic | [Report](../capabilities/report.md) |
| `migrate-from-wt` | One-time move of a pre-rename `wt` install to nightwright's config/data dirs (dry run unless `--yes`) | [Bootstrap](../guides/bootstrap.md) |
| `next` | Next-step hints for open ideas | [Ideas](../capabilities/ideas.md) |
| `override` | Time-scoped reclassification | [Report](../capabilities/report.md) |
| `personas` | Agentic persona registry: `list` / `show NAME` / `validate [--project NAME] [--json]` over the builtin, user (config dir `personas/`) and project (main-checkout `.nw/personas/`) layers, with each definition's origin file, and for a declared one its run settings, `required` / `when` and template sha256; `validate` exits 1 on any problem | [Agentic loop](../capabilities/agentic.md) |
| `profiles` | Agentic workflow profiles: list / show (with sources) / check | [Agentic loop](../capabilities/agentic.md) |
| `projects` | List / register outbox + research targets | [Projects](../capabilities/projects.md) |
| `queue` | Agentic work queue: add / list / watch / run / park / resume, conductor tooling (subcommands below) | [Agentic loop](../capabilities/agentic.md) |
| `refresh-meetings` | Pull Outlook meetings into meetings.jsonl | [Meetings](../capabilities/meetings.md) |
| `release-archive` | Pack git HEAD + ideas slice tarball | [Release archive](../capabilities/release-archive.md) |
| `report` | Per-topic hours | [Report](../capabilities/report.md), [Track time](../guides/track-time.md) |
| `review` | Segment timeline / unclassified / orphans | [Report](../capabilities/report.md) |
| `serve` | Coordinator side of pull workers: `serve --jobs [--bind ADDR:PORT] [--tls-cert F --tls-key F [--client-ca F]]` serves the pull-worker job API (lease, bundle, frames, result bundle, heartbeat) over HTTP(S); default bind `127.0.0.1:8790`; without TLS only loopback or Tailscale binds are allowed; `--client-ca` requires a client certificate whose CN is the worker's name (see "Pull workers" in `docs/WORKFLOW.md` in the repo); `serve --dashboard [--bind ADDR:PORT]` serves the dashboard (`GET /api/snapshot`, strong `ETag` = `rev`, `304` on `If-None-Match`; live stream, decisions inbox, campaign drilldown) with Host/Origin checks, CSRF on writes, an audit log and a runtime record `server.json`. The default bind `auto[:PORT]` serves `127.0.0.1:8791` at once with no login and also this host's own tailnet address on the same port behind the `tailscale whois` identity gate whenever Tailscale has one, re-checked every 15 s and bound or closed live (a logged-out Tailscale is never an exit); `server.json` lists both listeners. A loopback `--bind` serves loopback only; wildcard, LAN and foreign tailnet addresses and any TLS flag are refused with exit 2. `--bind tailscale[:PORT]` (tailnet only) resolves to this host's own tailnet address (`tailscale ip`); a host with no tailnet address (`tailscale` missing, logged out or stopped), including an explicit address in the tailnet ranges, exits **3** (after `--tailnet-wait SECONDS`) with a message naming `tailscale up`. Exactly one of `--jobs` and `--dashboard` is required | [Agentic loop](../capabilities/agentic.md) |
| `sheet` | Sheet-sync plan/record (JSON-only) | [Sheet](../capabilities/sheet.md) |
| `skills` | Install/list agent workflow skills | [Skills](../capabilities/skills.md) |
| `snapshot` | Archive past days to history.jsonl | [Track time](../guides/track-time.md) |
| `spec` | Spec scaffold / generate / export / verify | [Idea → ship](../guides/idea-to-ship.md), [Outbound](../guides/outbound.md) |
| `state` | Set a task's TODO state | [Tasks](../capabilities/tasks-agenda.md) |
| `tasks` | List org tasks | [Tasks](../capabilities/tasks-agenda.md) |
| `topics` | Discovered base topics + facets | [Report](../capabilities/report.md) |
| `tui` | Interactive desk (Textual is a core dependency; the `tui` extra is an empty alias): tabs ideas / hub / queue / mailbox / events (`--tab`, keys `1`..`5`), campaign screen (`--campaign`) | [Desk](../capabilities/tui.md) |
| `worker` | Remote worker, run on the worker host: `gate-job` / `have` / `gc` / `info` / `ssh-command`, async `submit` / `attach` / `poll` / `cancel` / `jobs`, `update`, `claude-check` / `cursor-check` / `pi-check`; `serve --enrol BLOB \| --config F` runs a pull worker that dials the coordinator's `nw serve --jobs` | [Agentic loop](../capabilities/agentic.md) |
| `workers` | Coordinator's worker registry (`<agentic_dir>/workers.json`), distinct from `nw worker` which runs on the worker host: `bootstrap NAME --ssh DEST [--install-key]` installs and registers a worker / `add NAME --ssh DEST [--slots N] [--nw CMD] [--no-check]` / `add NAME --pull [--kinds...] [--url URL]` prints a one-time enrolment blob for a pull worker / `token NAME --rotate` replaces its credential / `list [--json]` / `check [NAME] [--json]` / `set NAME [--slots N] [--kinds K] [--persona-slots N]` (`--slots` 1..16 changes gate slots in place: raising applies on the next tick, lowering never kills a held slot, which drains naturally) / `on NAME` / `off NAME` / `drain NAME` (no new gates; disabled once idle) / `clear-cooldown NAME` (cooldown only) / `rm NAME` / `update NAME [--force] [--dry-run]` upgrades a worker to this checkout's build over its restricted key; `update NAME --when-idle` drains it; the queue reconciles live records against `nw worker jobs` and a non-renewing `poll` and marks jobs those agree are gone as `lost`, then updates once idle and resumes (`agentic.workers.update_drain_timeout_h` default 6, `agentic.workers.auto_update` default false). A job-level error from a live worker (ENOSPC, a bug in a gate run) no longer cools the worker down: the job is retried on another slot; a worker below `agentic.workers.min_free_gb` (default 5) refuses new jobs. Disk guard: a job whose process is gone has its `tree`/`tmp`/`out` removed by the worker after `agentic.workers.reap_lost_after_min` (default 10; caches and `repos/` are never touched); a running job whose host falls below `agentic.workers.abort_free_gb` (default 2, 0 = off) kills the largest job with a `job_error` frame; `nw worker jobs [--json]` shows per-job `size_bytes`; `update` fails before uploading unless the worker has 3x the wheel free | [Agentic loop](../capabilities/agentic.md) |

## Nested groups (high level)

### `nw idea …`

Capture (`nw idea "…"`), `show`, `summary`, `log`, `questions`, `mark-explored`,
`clock-in` / `clock-out`, `state`, `project`, `tags`, `kind`, `priority`, `workstream`,
`retitle`, `close`, `ext`, `epic`, `spec-link`, `backfill-stamps`, plus maintenance helpers
(`normalize-logs`, …). Full list:
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

`add` (specs or `IDEA-NNN`; `--runner claude|cursor|pi` pins the campaign), `list` (`status`
is an alias), `watch`, `run`, `tick`, `stop`, `restart`, `park` (needs `--reason`),
`resume`, `abandon`, `prioritize`, `backlog`, `after`, `unqueued`, `graph`, `flaky`,
`cost-policy`, `backend` (per-campaign pin, or `--host` / `--all`; the choice is
`claude|cursor|pi|default`), `merge`, `gc`, `gates`, `gate-slots`, `sweep-prs`,
`conductor-guard`, `main-dirty-plan` (read-only plan to clear a dirty base checkout), `main-dirty-apply`
(human or steward run: commits a trivial dirty base checkout and saves non-trivial dirt to a `wip/main-dirty-*` branch in a worktree before restoring main; a `hold` then switches to the base; the conductor is denied it),
`sentinel` (`run`, `clear`, `status`, `resume-fix`), plus the conductor
tooling: `events`, `followers`, `wakeups`, `runner-log`, `decision` (needs a rationale
argument), `decisions`, `escalate`, `conduct-status`, `conductor-brief`, `conductor-cost`,
`conductor-session`, `conductor-launch` / `steward-launch` (`--runner claude|cursor`) and
`mvp-report`. Full list: `nw queue --help`.

### `nw dashboard …`

`snapshot` (text summary) and `snapshot --json` (the `nw.dashboard.v1` document) build the
dashboard view in-process and write nothing. `install` and `uninstall` manage
the user systemd unit. `status [--json]` reports whether that unit is running. Full list:
`nw dashboard --help`.

### `nw sheet …` / `nw skills …` / `nw completion …` / `nw campaign …`

Subcommands via `nw <group> --help`.

## Date scopes

Shared by `report`, `agenda`, and `digest`. `review` has `--week` / `-w` and a date, and
rejects `--last`. `refresh-meetings` has `--week` and `--days` (default 7), and rejects
`--last`:

```bash
nw report                 # today
nw report --week          # this week
nw report 2026-06-01      # that day
nw report -w 2026-06-01   # week containing that date
nw report --last 7
```
