# nightwright in herdr

[herdr](https://herdr.dev) is a terminal workspace manager for AI coding agents. nightwright can
show the agentic queue in herdr's sidebar and raise herdr notifications. The integration is
**opt-in**: nothing calls herdr until you start `nw herdr watch` inside a herdr pane
([SPEC-0361](./specs/0361-herdr-reporter-nw-herdr-watch-pushes-queue-tokens.md), epic
[SPEC-0360](./specs/0360-look-inside-running-agents-herdr-status-reporting.md)).

## What it does

`nw herdr watch` is a separate process. It reads the queue state (the same data as
`nw queue conduct-status` and `nw queue list`) and the event stream with its own cursor
(`herdr`), then pushes:

- **sidebar tokens** to the herdr workspace and to the conductor's and stewards' panes, with
  `herdr pane|workspace report-metadata --source nw`. Each push has a TTL of 3 push intervals
  (default 10 s, so 30 s), so the values disappear on their own if the watcher dies;
- **notifications** with `herdr notification show`, once per event.

It is the only nightwright process that ever calls `herdr`. The queue runner, the persona runs
and every other `nw` command never do. Each call times out after 2 s; after 5 failures in a
row the watcher stops calling herdr for 60 s and prints one line. Persona runs get no
`HERDR_*` variables, so herdr's Claude hook inside a headless run cannot change the state of
the pane that started the queue.

nightwright never edits `~/.config/herdr/config.toml`. To see the tokens, paste the snippet
below into it yourself.

## Start it

1. Start the conductor and the stewards in herdr panes, as usual (`nw queue conductor-launch`,
   `nw queue steward-launch P`, or `/nw-conduct` and `/nw-steward` in a Claude
   session). Their followers (`nw queue events --follow --for conductor`,
   `nw mailbox follow --to steward:P`) record which herdr pane they run in, in
   `<data_dir>/agentic/conductor/herdr_panes.json`. They do not call herdr.
2. In another herdr pane, run:

   ```bash
   nw herdr watch                 # push every 10 s until Ctrl-C; Ctrl-C clears the tokens
   nw herdr watch --once          # one push; the values expire after 30 s
   nw herdr watch --interval 5    # faster pushes (TTL 15 s)
   nw herdr watch --conductor-pane PANE_ID --workspace WS_ID   # explicit targets
   ```

Outside herdr (`HERDR_ENV` is not `1`) or without `herdr` on `PATH`, the command prints one line
and exits 0. Only one watcher runs at a time; a second one exits with the holder's pid.

The watcher does not restart when nightwright's code changes. Restart it after an upgrade.

## Tokens

| Where | Token | Example |
|---|---|---|
| workspace | `$nw_queue` | `3 active · 2 queued · 1 parked` |
| workspace | `$nw_attn` | `1 esc · 2 undecided` (empty when nothing waits on you or the conductor) |
| workspace | `$nw_cost` | `$12.40` (today's queue spend) |
| conductor pane | `$nw_role` | `conductor` |
| conductor pane | `$nw_attn` | as above |
| conductor pane | `$nw_cost` | `$3.10` (the conductor session's cost today; empty without a configured price) |
| steward pane | `$nw_role` | `steward:Meta-Tools` |
| steward pane | `$nw_spec` | `SPEC-0361`, `2 active`, or `idle` |
| steward pane | `$nw_loop` | `implementing 2/5` (one active campaign only) |
| steward pane | `$nw_cost` | `$1.20/$5.00` (campaign spend vs cap) |

Workspaces that get the tokens: the watcher's own workspace and the workspaces of the conductor's
and stewards' panes (or only `--workspace`).

## Notifications

| Event | Sound |
|---|---|
| `escalation` (a question for you) | `request` |
| `run_failed` that is not transient (not a rate or usage limit) | `request` |
| `parked` with a conductor-owned reason | `request` |
| `merged` | `done` |

Every other event is silent; the sidebar shows it.

## Suggested `~/.config/herdr/config.toml` snippet

Paste this into `~/.config/herdr/config.toml` and adjust it as you like. Tokens that a pane or
workspace does not report render empty.

```toml
[ui.sidebar.agents.rows_by_agent]
# Claude panes (the conductor and the stewards): role, spec and loop, cost and attention.
claude = [
  ["state_icon", "workspace", "tab"],
  ["$nw_role", "$nw_spec", "$nw_loop"],
  ["$nw_cost", "$nw_attn"],
  ["agent"],
]

[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["$nw_queue"],
  ["$nw_attn", "$nw_cost"],
  ["branch", "git_status"],
]
```

A token can be styled inline, for example
`{ token = "$nw_attn", fg = "#f38ba8", bold = true }` (see the comments in herdr's default
config). Then reload the herdr config.

## Try it

```bash
# in a herdr pane
nw herdr watch --once          # tokens show for ~30 s, then expire
nw herdr watch                 # live; Ctrl-C clears; kill -9 lets them expire in 30 s
# outside herdr
env -u HERDR_ENV nw herdr watch   # one line, exit 0, no herdr call
```
