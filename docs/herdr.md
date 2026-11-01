# nightwright in herdr

[herdr](https://herdr.dev) is a terminal workspace manager for AI coding agents. nightwright can
show the agentic queue in herdr's sidebar and raise herdr notifications. It needs **no manual
step**: when the conductor runs in a herdr pane, its event follower starts `nw herdr watch` in
the background and stops it again ([SPEC-0365](./specs/0365-herdr-watcher-auto-start-the-conductor-follower.md);
[SPEC-0361](./specs/0361-herdr-reporter-nw-herdr-watch-pushes-queue-tokens.md), display
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

## It starts by itself

1. Start the conductor and the stewards in herdr panes, as usual (`nw queue conductor-launch`,
   `nw queue steward-launch P`, or `/nw-conduct` and `/nw-steward` in a Claude
   session). Their followers (`nw queue events --follow --for conductor`,
   `nw mailbox follow --to steward:P`) record which herdr pane they run in, in
   `<data_dir>/agentic/conductor/herdr_panes.json`. They do not call herdr.
2. The conductor's follower, when it runs inside herdr (`HERDR_ENV=1`) with `herdr` on
   `PATH`, starts `nw herdr watch` as a background process. Nothing else to do.

How it behaves:

- **One watcher.** If a watcher already runs (one you started by hand, or another follower's),
  the follower starts none, never stops yours, and starts one within 5 s after yours exits.
- **Output** goes only to `<data_dir>/agentic/conductor/herdr_watch.log` (rotated to
  `herdr_watch.log.1` past 5 MiB), never to the conductor's event stream.
- **Stops with the follower.** When the follower exits (Ctrl-C, re-arm, `code_changed`) it
  sends the watcher SIGTERM, which clears the display agents and closes the run tabs. If the
  follower is killed outright, the watcher notices within 2 s and exits the same way.
- **Restarts.** A watcher that crashes is restarted after 5 s, then 10, 20, … up to 300 s
  (back to 5 s after a run of 5 minutes).
- **Upgrades.** When nightwright's code changes the follower exits with `code_changed` and the
  conductor re-arms it (SPEC-0231); the new follower starts a new watcher on the new code.
- **Run tabs** follow `agentic.herdr.run_panes`, as for a watcher you start yourself.
- **Status:** `nw herdr status` (`--json`: `nw.herdr.watch.v1`) and `nw queue conduct-status`
  show whether a watcher runs, its pid, `auto` or `manual`, and the log path.

Turn it off in nightwright's config (`~/.config/nightwright/config.yaml`):

```yaml
agentic:
  herdr:
    auto_watch: false      # the follower never starts a watcher
    run_panes: true        # optional: run tabs (SPEC-0366)
    run_pane_linger_s: 60  # optional: seconds a run tab stays after its run
```

### By hand

You can still run the watcher yourself in a herdr pane (with `auto_watch: false`, or to try
options); the follower leaves it alone:

```bash
nw herdr watch                 # push every 10 s until Ctrl-C; Ctrl-C clears what it set
nw herdr watch --once          # one push; the values expire after 30 s
nw herdr watch --interval 5    # faster pushes (TTL 15 s)
nw herdr watch --conductor-pane PANE_ID --workspace WS_ID   # explicit targets
```

Outside herdr (`HERDR_ENV` is not `1`) or without `herdr` on `PATH`, the command prints one line
and exits 0. Only one watcher runs at a time; a second one exits with the holder's pid. A
watcher you start yourself does not restart when nightwright's code changes.

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

## Run tabs (opt-in)

`agentic.herdr.run_panes: true` in nightwright's config (or `nw herdr watch --run-panes` by hand) also
opens **one herdr tab per campaign that has a running persona run**, named after the work and
showing the campaign's live transcript
([SPEC-0366](./specs/0366-herdr-run-tabs-one-tab-per-campaign-named-after.md), which replaced
SPEC-0364's shared `nw runs` tab). Without the flag nothing below happens.

```bash
nw herdr watch --run-panes                        # live; Ctrl-C closes the run tabs
nw herdr watch --run-panes --run-pane-linger 30   # a tab closes 30 s after its run (default 60)
```

- **Where**: one tab per campaign in the conductor's workspace (the watcher's own workspace
  when no conductor is placed), created with `--no-focus`. nw never splits, renames or closes
  a pane or tab it did not create.
- **Name**: `<SPEC-ID> <short title>`, e.g. `SPEC-0349 nw doctor`: the spec's title without
  markdown, up to its first `:` or `—`, at most 24 characters. Idea campaigns use the idea's
  heading; a merge request just its label. herdr's sidebar row reads
  `nightwright · SPEC-0349 nw doctor`.
- **Display agent** (the row below): `nw run · adversarial_reviewer · loop 2 · $0.89/$2.50`
  while a run runs (the run's cost so far, its real cost once finished, omitted when the model
  has no configured price; then its `--max-budget-usd`), then `nw run · gating`,
  `nw run · parked: cost_cap`, `nw run · merged`, ….
- **What the pane runs**: `nw campaign tail CAMPAIGN --follow-campaign`, typed once. It clears
  the screen (so the typed command does not stay), shows the running run, prints
  `■ run … ended · waiting for the next run · campaign …` when it ends, and shows the next run
  by itself. It runs until the tab closes, so herdr keeps the pane's agent entry the whole time.
- **When it closes**: the tab exists while a run of the campaign runs. When none runs, it
  stays 60 s (`agentic.herdr.run_pane_linger_s`, or `--run-pane-linger`), then closes, whatever
  the campaign's state; a parked campaign shows `blocked` for that minute. A run that starts
  within the minute continues in the same tab; a later one opens a new tab.
- **State** (`herdr pane report-agent --source nw-run --agent nw-run`): `working` while a run
  runs, `blocked` when the campaign is parked, `idle` otherwise (herdr shows `idle` after
  `working` as done until you look).
- **At most 6 run tabs.** Another campaign first evicts the tab whose run ended first; if none
  is lingering it waits, and nw's newest run tab gets ` (+N)` appended.
  `nw campaign tail CAMPAIGN --follow-campaign` shows any campaign in a terminal of your own.
- **Ownership**: each run pane is renamed `nw-run:<campaign>`, and the pane and tab ids are kept
  in `<data_dir>/agentic/conductor/herdr_run_panes.json` per herdr socket. A restarted watcher
  (and each `--once`) adopts a recorded pane only when herdr shows that label in the recorded
  tab; anything else is forgotten, never touched. If herdr restored the pane with a fresh
  shell, the viewer is typed again. Panes left by the older shared `nw runs` tab are checked
  the same way, closed, and reopened as tabs of their own.
- **Exit**: Ctrl-C / SIGTERM closes every run tab. `--once` leaves them; the next run of the
  watcher picks them up. After `kill -9` they stay until the next watcher run handles them.
- **Closing one yourself** is fine: nw forgets it and opens no new tab for that campaign until
  its current run has ended.
- The pane's shell is herdr's (with herdr's environment), so the typed command names
  nightwright's config and data directories and the watcher's Python explicitly:
  `env NW_CONFIG_DIR=… NW_DATA_DIR=… …/python -m nightwright campaign tail CAMPAIGN
  --follow-campaign`. herdr strips a leading space, so your shell's history gets this line once
  per tab.

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
nw herdr status                # is the auto-started watcher running? pid, log path
tail -f ~/.local/share/nightwright/agentic/conductor/herdr_watch.log
# by hand, in a herdr pane (with auto_watch: false, or the follower's watcher stopped)
nw herdr watch --once          # values show for ~30 s, then expire
nw herdr watch --run-panes     # plus a tab per campaign with a running run
# outside herdr
env -u HERDR_ENV nw herdr watch   # one line, exit 0, no herdr call
```
