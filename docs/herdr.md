# nightwright in herdr

[herdr](https://herdr.dev) is a terminal workspace manager for AI coding agents. nightwright can
show the agentic queue in herdr's sidebar and raise herdr notifications. The integration is
**opt-in**: nothing calls herdr until you start `nw herdr watch` inside a herdr pane
([SPEC-0361](./specs/0361-herdr-reporter-nw-herdr-watch-pushes-queue-tokens.md), display
design [SPEC-0363](./specs/0363-herdr-display-scoped-to-nw-panes-display-agent-on.md), epic
[SPEC-0360](./specs/0360-look-inside-running-agents-herdr-status-reporting.md)).

## What it does

`nw herdr watch` is a separate process. It reads the queue state (the same data as
`nw queue conduct-status` and `nw queue list`) and the event stream with its own cursor
(`herdr`), then pushes:

- a **display agent** on the conductor's pane and on each steward's pane
  (`herdr pane report-metadata <pane> --source nw --display-agent …`). herdr shows it in place
  of the pane's agent label in its default sidebar rows, so it needs **no herdr config** and no
  other pane changes;
- **workspace tokens** (`$nw_queue`, `$nw_attn`, `$nw_cost`) to the workspaces that host the
  conductor or a steward, and nowhere else. They only show if you add the optional workspace
  row below;
- each push has a TTL of 3 push intervals (default 10 s, so 30 s), so the values disappear on
  their own if the watcher dies;
- **notifications** with `herdr notification show`, once per event.

It is the only nightwright process that ever calls `herdr`. The queue runner, the persona runs
and every other `nw` command never do. Each call times out after 2 s; after 5 failures in a
row the watcher stops calling herdr for 60 s and prints one line. Persona runs get no
`HERDR_*` variables, so herdr's Claude hook inside a headless run cannot change the state of
the pane that started the queue.

nightwright never edits `~/.config/herdr/config.toml`.

## Start it

1. Start the conductor and the stewards in herdr panes, as usual (`nw queue conductor-launch`,
   `nw queue steward-launch P`, or `/nw-conduct` and `/nw-steward` in a Claude
   session). Their followers (`nw queue events --follow --for conductor`,
   `nw mailbox follow --to steward:P`) record which herdr pane they run in, in
   `<data_dir>/agentic/conductor/herdr_panes.json`. They do not call herdr.
2. In another herdr pane, run:

   ```bash
   nw herdr watch                 # push every 10 s until Ctrl-C; Ctrl-C clears what it set
   nw herdr watch --once          # one push; the values expire after 30 s
   nw herdr watch --interval 5    # faster pushes (TTL 15 s)
   nw herdr watch --conductor-pane PANE_ID --workspace WS_ID   # explicit targets
   ```

Outside herdr (`HERDR_ENV` is not `1`) or without `herdr` on `PATH`, the command prints one line
and exits 0. Only one watcher runs at a time; a second one exits with the holder's pid.

The watcher does not restart when nightwright's code changes. Restart it after an upgrade.

## What the sidebar shows

### Panes (no config)

| Pane | Display agent | Example |
|---|---|---|
| conductor | `claude · conductor`, then its session's cost today (when priced), `N need you` (open escalations), `N undecided` (undecided conductor-owned parks) | `claude · conductor · $5.24 · 2 need you` |
| steward, one active campaign | `claude · steward:P · <spec> loop <n> · $spend/$cap` | `claude · steward:Meta-Tools · SPEC-0353 loop 16 · $40.95/$115` |
| steward, several | `claude · steward:P · N active · $spend/$cap` (sums) | `claude · steward:Meta-Tools · 2 active · $12/$40` |
| steward, none | `claude · steward:P · idle` | `claude · steward:comfort-assistant · idle` |

Whole dollars drop the cents. The project name and the spec label are cut to 24 characters,
the whole string to 64. Only the conductor's and stewards' panes are touched: other Claude
panes, the watcher's own pane and dead placements are never written. Ctrl-C (or SIGTERM)
clears the display agents; after `kill -9` they expire with the TTL.

### Workspaces

| Workspace | `$nw_queue` | `$nw_attn` | `$nw_cost` |
|---|---|---|---|
| the conductor's (or `--workspace`) | `3 active · 2 queued · 1 parked` (whole queue) | `1 esc · 2 undecided` | `$12.40` (today's queue spend) |
| a steward's, if not the conductor's | `Meta-Tools: 1 active · 0 queued · 0 parked` (that project only) | cleared | that project's spend today |

No other workspace gets tokens, including the watcher's own unless it hosts the conductor or a
steward. A workspace never gets another project's numbers. Several stewards in one workspace
share one summary of exactly their projects (`A, B: …`).

## Notifications

| Event | Sound |
|---|---|
| `escalation` (a question for you) | `request` |
| `run_failed` that is not transient (not a rate or usage limit) | `request` |
| `parked` with a conductor-owned reason | `request` |
| `merged` | `done` |

Every other event is silent; the sidebar shows it.

## Run panes (opt-in)

`nw herdr watch --run-panes` (or `agentic.herdr.run_panes: true` in nightwright's config) also
opens **one herdr pane per active campaign that has a persona run**, showing the run's live
transcript (`nw campaign tail --follow`, [SPEC-0364](./specs/0364-herdr-run-panes-one-pane-per-active-campaign-in.md)).
Without the flag nothing below happens.

```bash
nw herdr watch --run-panes                        # live; Ctrl-C closes the run panes
nw herdr watch --run-panes --run-pane-linger 60   # finished panes close after 60 s (default 300)
```

- **Where**: one tab labelled `nw runs` in the conductor's workspace (the watcher's own
  workspace when no conductor is placed), created with `--no-focus`. nw never splits, renames
  or closes a pane it did not create; your panes and tabs are left alone. When the last run
  pane closes, herdr closes the tab; the next run opens a new one.
- **Layout**: each new pane splits nw's own largest pane in that tab, to the right when it is
  at least twice as wide as tall (in cells), else down: columns on a wide screen, then a grid.
- **One pane per campaign**, not per run: a reviewer group's parallel runs share the
  campaign's pane, which tails the newest running run; the campaign's next run is typed into
  the same pane.
- **At most 6 panes.** A seventh campaign first evicts the earliest-finished lingering pane;
  if none is lingering it waits, and the tab reads `nw runs (+N)`. `nw campaign tail
  CAMPAIGN --follow` shows any run in a terminal of your own.
- **Linger**: when the campaign stops (parked, waiting, queued, merged, abandoned, handed off),
  its pane stays for 300 s with the final transcript, then closes.
- **State** (`herdr pane report-agent --source nw-run --agent nw-run`): `working` while a run
  runs, `blocked` when the campaign is parked (an escalation parks it), `idle` otherwise
  (between runs, queued, waiting, merged). herdr shows `idle` after `working` as done until
  you look. herdr drops a reported state when the pane's foreground process exits (the tail
  exits with its run), so the watcher re-reports each push; after a run ends, the state can
  be missing for up to one push interval.
- **Display agent** (no config needed): `nw run · SPEC-0354 · implementer · loop 3 · $1.20/$10`
  while a run runs (the run's cost so far, its real cost once finished, omitted when the model
  has no configured price; then its `--max-budget-usd`), `nw run · SPEC-0354 · gating` between
  runs, `nw run · SPEC-0354 · parked: cost_cap` / `… · merged` after it stops.
- **Ownership**: each run pane is renamed `nw-run:<campaign>`, and the ids are kept in
  `<data_dir>/agentic/conductor/herdr_run_panes.json` per herdr socket. A restarted watcher (and
  each `--once`) adopts a recorded pane only when herdr shows that label in the recorded tab;
  anything else is forgotten, never touched. If herdr restored the pane with a fresh shell, the
  tail is typed again.
- **Exit**: Ctrl-C / SIGTERM closes every run pane. `--once` leaves them; the next run of the
  watcher picks them up. After `kill -9` they stay until the next watcher run closes them.
- **Closing one yourself** is fine: nw forgets it and opens no new pane for that campaign until
  it stops and starts again.
- The pane's shell is herdr's (with herdr's environment), so the typed command names
  nightwright's config and data directories and the watcher's Python explicitly:
  `env NW_CONFIG_DIR=… NW_DATA_DIR=… …/python -m nightwright campaign tail CAMPAIGN --run RUN --follow`.

## Optional: a workspace row

Workspace tokens only render if a `[ui.sidebar.spaces] rows` entry names them. **This changes
the layout of every workspace in herdr**, not only nightwright's; only the workspaces that host
the conductor or a steward get values, and in the others the `$nw_*` cells render empty. If you
want it, add this to `~/.config/herdr/config.toml` yourself:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["$nw_queue"],
  ["$nw_attn", "$nw_cost"],
  ["branch", "git_status"],
]
```

The snippet passes `herdr config check` (herdr 0.9.5). To check it without touching your
config, put it in a scratch directory: `XDG_CONFIG_HOME=/tmp/x herdr config check` with the
snippet in `/tmp/x/herdr/config.toml`. Then run `herdr server reload-config`.

Do **not** add `[ui.sidebar.agents.rows_by_agent] claude = …` for nightwright (SPEC-0361's old
suggestion): it re-lays out every Claude pane, and the panes no longer report `$nw_*` tokens.
If you pasted it earlier, remove it.

## Try it

```bash
# in a herdr pane
nw herdr watch --once          # values show for ~30 s, then expire
nw herdr watch                 # live; Ctrl-C clears; kill -9 lets them expire in 30 s
nw herdr watch --run-panes     # plus a pane per active campaign tailing its run
# outside herdr
env -u HERDR_ENV nw herdr watch   # one line, exit 0, no herdr call
```
