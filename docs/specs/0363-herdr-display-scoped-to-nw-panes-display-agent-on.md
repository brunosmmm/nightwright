---
id: SPEC-0363
title: "herdr display scoped to nw panes: --display-agent on conductor/steward panes, per-role workspace tokens"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-653
parent: SPEC-0360
supersedes: [SPEC-0361]
tags: [agentic, observability, herdr]
---

## Context

Promoted from idea `IDEA-653`. Supersedes the **display design** of
[SPEC-0361](./0361-herdr-reporter-nw-herdr-watch-pushes-queue-tokens.md) (`nw herdr watch`);
everything else that spec shipped stays as it is. Child of epic
[SPEC-0360](./0360-look-inside-running-agents-herdr-status-reporting.md).

SPEC-0361 pushed `$nw_*` tokens and told the human (in `docs/herdr.md`) to add
`[ui.sidebar.agents.rows_by_agent] claude = [...$nw_role...]` and `[ui.sidebar.spaces] rows =
[...$nw_queue...]` to `~/.config/herdr/config.toml`. The human rejected that after first use:

- `rows_by_agent.claude` changes the layout of **every** Claude pane, including the ones that
  have nothing to do with nightwright; `[ui.sidebar.spaces] rows` changes every workspace.
- The watcher pushed the **whole** queue's numbers to every workspace that hosts a steward (and
  to its own workspace): the comfort-assistant workspace showed nightwright's queue and the
  queue's `$224.69` spend.

Verified on herdr 0.9.5 (2026-11-01, on this spec author's own pane `w7:p7` only):

- `herdr pane report-metadata <pane> --source nw --seq N --ttl-ms T --display-agent TEXT`
  exits 0; `herdr agent get <pane>` then shows `"display_agent": TEXT`, and the human confirmed
  the sidebar shows it on that pane only, with no config change. `--clear-display-agent` removes
  it (read back: `display_agent` absent).
- herdr's default agent rows are `[["state_icon", "machine", "workspace", "tab"], ["agent"]]`
  (comments of its embedded default config). The `agent` cell is where `display_agent` shows.
- `--state-label working=TEXT` and `--title TEXT` are accepted too (read back as
  `state_labels: {working: TEXT}` and `title: TEXT`). Neither shows in herdr's default rows
  (`state_text` and the pane title are not in `[["state_icon", …], ["agent"]]`), so they would
  show nothing without config, and `--title` would compete with Claude's own terminal title
  (the session name).
- `workspace report-metadata` has no `--display-agent` (`unknown option: --display-agent`,
  exit 2): a workspace row can only show values through `$name` tokens, which need config.
- `herdr config check` validates the config and honors `XDG_CONFIG_HOME`.

## Goals / Non-goals

**Goals**
- nw's conductor and steward panes show what they are doing in herdr's sidebar **with no herdr
  config**, and no other pane changes.
- Workspace tokens go only to workspaces that host the conductor or a steward, and a
  workspace only ever gets numbers that belong to it: queue totals to the conductor's
  workspace, one project's summary to that project's steward workspace.
- `docs/herdr.md` no longer suggests `rows_by_agent`. The workspace row snippet is optional,
  says plainly what it affects, and is validated with `herdr config check`.

**Non-goals**
- Changing anything else from SPEC-0361. These stay as shipped: the client (timeout, breaker,
  stderr in the breaker line, argv with the id before options), `HERDR_*` stripping,
  notifications and their cursor, pane recording by the followers, the CLI gating, the opt-in
  contract test.
- Run panes (epic child 3), stream capture (child 2).

## Decision

1. **Panes**: one `pane report-metadata <pane> --source nw --seq N --ttl-ms T
   --display-agent TEXT` per push for the conductor's pane and for each steward's pane. Panes
   get no tokens any more. A clean exit (Ctrl-C, SIGTERM, `stop()`) sends
   `--clear-display-agent` to each pane; after `kill -9` the TTL (3 × interval) expires it.
   `--state-label` and `--title` are not used (not visible in herdr's default rows; see
   Context).
2. **Workspaces**: tokens `nw_queue`, `nw_attn`, `nw_cost` (names unchanged), sent only to:
   - the **conductor's workspace**: queue totals, `nw_attn` (escalations / undecided parks) and
     today's queue spend, as before;
   - each **steward's workspace that is not the conductor's**: that project's summary only
     (`nw_queue` = `"<P>: A active · Q queued · K parked"`, `nw_cost` = the project's spend
     today, `nw_attn` cleared because the attention counts are queue-wide).
   No other workspace gets tokens. The watcher's own workspace is a target only when it hosts
   one of those roles.
3. **Docs**: an optional `[ui.sidebar.spaces] rows` snippet, with a plain warning that it
   changes every workspace's layout and that only nw workspaces get values. No `rows_by_agent`.

## Design

The change is confined to `src/nightwright/agentic/herdr.py`, `herdr_watch.py`, `cli_herdr.py`
(help text), `tests/test_herdr.py` and `docs/herdr.md`.

### Client — `herdr.metadata_args` / `clear_args`

`metadata_args` gains `display_agent=None` (a string adds `--display-agent TEXT`). `clear_args`
gains `clear_display_agent=False` (adds `--clear-display-agent`). The positional id stays third,
as herdr 0.9.5's parser requires.

### Display-agent strings — `herdr_watch.conductor_display` / `steward_display`

Parts are joined with `" · "` and prefixed with `claude`, the label herdr shows for a Claude
pane, so the pane stays recognisable (both roles are Claude Code sessions):

- **conductor**: `claude · conductor`, then the conductor session's priced cost today (as
  SPEC-0361's pane `nw_cost`; omitted when unknown), then `N need you` (open escalations, when
  > 0), then `N undecided` (undecided conductor-owned parks, when > 0).
  Example: `claude · conductor · $5.24 · 2 need you`.
- **steward P** (P's items in `ACTIVE_STATES`):
  - none: `claude · steward:P · idle`;
  - one: `claude · steward:P · <label> loop <n> · $spend/$cap`, where `n` is the loops counter
    before the `/` and `/$cap` is omitted when the item has no cap;
  - several: `claude · steward:P · N active · $spend/$cap` (sums).
  Example: `claude · steward:Meta-Tools · SPEC-0353 loop 16 · $40.95/$115`.
- **money**: `$40.95`. Whole dollars drop the cents (`$115`, `$0`).
- **truncation**: the project name and the label are cut to 24 characters each and the whole
  string to 64, each with a trailing `…`. herdr's sidebar clips further to its own width.

### Targets — `Watcher.targets(snap)`

- Live placements, as in SPEC-0361: readers `conductor` and `steward:<P>` whose follower pid
  is still alive.
- Conductor pane: `--conductor-pane` if given, else the live `conductor` placement's pane.
- Conductor workspace, first match wins:
  1. `--workspace`;
  2. the `workspace_id` of the live placement whose pane is the conductor pane;
  3. with `--conductor-pane` and no placement for that pane, the pane id's `<ws>:` prefix.
     herdr 0.9.5 pane ids are workspace-scoped (pane `w7:p2` lives in workspace `w7`;
     verified in `herdr api snapshot`);
  4. none.
- Steward workspaces: the `workspace_id` of every live `steward:P` placement that differs from
  the conductor workspace. Several stewards in one such workspace give one call whose summary
  covers exactly those projects (`"A, B: …"`, summed).
- The watcher's own `HERDR_PANE_ID` / `HERDR_WORKSPACE_ID` are never added for their own sake.

### Snapshot

`snapshot()` adds `by_project = {P: {active, queued, parked, spend_today}}`, computed with the
same rules as the totals (`queue_payload` items for counts, campaigns updated today for spend).

### Clear on exit

`pushed` remembers, per `(kind, target)`, the token names and whether a display agent was set.
`clear()` sends `--clear-token` for the token names and `--clear-display-agent` for panes.

## Alternatives considered

- **Keep `rows_by_agent.claude`**: rejected by the human, because it re-lays out every Claude
  pane.
- **`--agent nw-conductor` plus `rows_by_agent."nw-conductor"`**: this would scope a custom row
  to nw panes, but it overrides the agent identity that herdr's own Claude hook reports (the
  state icon and attention routing key on it), and it still needs config. Not pursued.
- **`--state-label` / `--title`**: herdr accepts both, but neither is visible in the default
  rows, and the title would also replace Claude's own session title. Not used.
- **Whole-queue numbers on every steward workspace**: this is what SPEC-0361 did, and it leaks
  one project's numbers into another project's workspace.

## Acceptance criteria

- [ ] Each push sends exactly one `pane report-metadata <pane> … --display-agent TEXT` to the
      conductor pane and to each live steward pane, with the strings of Design and no
      `--token`. No other pane gets a call (not the watcher's own, not dead placements, not
      other readers).
- [ ] Workspace calls go only to the conductor's workspace (queue totals) and to steward
      workspaces that differ from it (that project's summary only, `nw_attn` cleared). The
      watcher's own workspace is not a target unless it hosts one of those roles, and no
      workspace gets another project's numbers.
- [ ] A clean exit sends `--clear-display-agent` to every pane it set and `--clear-token` for
      every workspace token it pushed. `--once` leaves them to the TTL.
- [ ] The fake `herdr` models the real parser (id before options; `--display-agent` /
      `--clear-display-agent` accepted on panes and rejected on workspaces).
- [ ] The opt-in contract test (`NW_HERDR_CONTRACT=1`) sets a display agent and a token on
      the caller's own pane / workspace on the real herdr, reads them back with `herdr agent
      get` / `herdr api snapshot`, clears them and reads back their absence. It passes on herdr
      0.9.5.
- [ ] `docs/herdr.md` documents the display-agent strings and the workspace rules, has no
      `rows_by_agent`, and its optional `[ui.sidebar.spaces]` snippet passes `herdr config
      check` in an isolated `XDG_CONFIG_HOME` (the human's config is never touched).
- [ ] SPEC-0361's other behavior (client, breaker, notifications, gating, `HERDR_*`
      stripping, placement recording, import isolation) is unchanged, and its tests still pass.

## Test plan

- **Automated tests** (`tests/test_herdr.py`, with a fake `herdr` on `PATH` that rejects
  options before the id and, like herdr, rejects `--display-agent` on `workspace
  report-metadata`):
  - argv builders: `--display-agent`, `--clear-display-agent`, id third.
  - display-agent strings: conductor with and without cost and attention; steward idle, one
    (label + loop + cost/cap; cap missing) and several; money formatting; truncation to 64
    with a long project and label.
  - push: placements for the conductor (ws-a), `steward:Meta-Tools` (ws-a, the conductor's),
    `steward:Other` (ws-b), a dead one and a `mailbox:human` one; the watcher's own env says
    ws-self. Expect pane calls with only `--display-agent` and the expected strings; workspace
    calls to ws-a (totals) and ws-b (an `Other:` summary with Other's numbers only, `nw_attn`
    cleared); no call to ws-self, the dead placement or other readers; seqs strictly
    increasing across a restart.
  - overrides: `--conductor-pane w9:p3` without a placement gives pane `w9:p3` and workspace
    `w9`; `--workspace ws-X` sends the totals to ws-X.
  - no conductor placement and no override: no totals workspace call at all.
  - a clean exit clears display agents and tokens; `--once` does not.
  - the SPEC-0361 tests for client, breaker, notifications, CLI, isolation and env stripping
    stay unchanged.
- **Contract test against the real herdr** (`NW_HERDR_CONTRACT=1`, run inside a herdr pane).
  It writes only to `$HERDR_PANE_ID` / `$HERDR_WORKSPACE_ID`, with TTL ≤ 60 s, and clears what
  it wrote: set a display agent and a token, read them back with `herdr agent get` and
  `herdr api snapshot`, clear them, read back their absence.
- **Config snippet**: write the `docs/herdr.md` snippet to a temporary
  `XDG_CONFIG_HOME/herdr/config.toml` and run `herdr config check` (expect no diagnostics);
  repeat with an unknown built-in to confirm the check reports it.
- **Manual**: the agent does not run `nw herdr watch` against the live queue's panes (that
  would write to other panes). The human sees the strings on their next `nw herdr watch`.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_herdr.py`.

## Rollout / migration

The human restarts `nw herdr watch` after the upgrade. Pane tokens pushed by an older watcher
expire within their TTL. If the human pasted SPEC-0361's `rows_by_agent.claude` snippet, they
should remove it (`docs/herdr.md` says so); nightwright never edits their config.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; contract test passes on the real herdr.
- [ ] No regressions (the full suite runs at the queue's close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.

## Open questions

None.
