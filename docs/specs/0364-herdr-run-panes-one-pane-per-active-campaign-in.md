---
id: SPEC-0364
title: "herdr run panes: one pane per active campaign in an nw runs tab, running nw campaign tail, agent state + display agent"
status: superseded
superseded_by: SPEC-0366
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-655
parent: SPEC-0360
depends_on: [SPEC-0362, SPEC-0363]
tags: [agentic, observability, herdr]
---

## Context

Promoted from idea `IDEA-655`. Child 3 of epic
[SPEC-0360](./0360-look-inside-running-agents-herdr-status-reporting.md): "optionally, one herdr
pane per active run running the tail viewer, with herdr's agent state following the campaign
state". It builds on the two shipped siblings:

- [SPEC-0363](./0363-herdr-display-scoped-to-nw-panes-display-agent-on.md) (superseding
  SPEC-0361): `nw herdr watch`, the one herdr client `agentic/herdr.py` (2 s timeout, breaker,
  id before options), display agents instead of tokens because tokens need herdr config.
- [SPEC-0362](./0362-stream-json-capture-of-persona-runs-and-nw.md): `runs/<run>/stream.jsonl`,
  `nw campaign tail CID [--run RUN] [--follow]` (exits when the run ends), the `Renderer` tally.

Two lessons from those siblings bind this spec: every herdr argv must be checked against the
real herdr 0.9.5 (the fake once modelled the wrong parser), and nw may touch only what it
created (the human rejected a design that changed every Claude pane and every workspace).

herdr 0.9.5 surface used here, checked in an isolated named session (`herdr --session
nw-test-… server`, see As shipped):

- `tab create --workspace WS --label L --cwd P --no-focus` → `.result.tab.tab_id`,
  `.result.root_pane.pane_id`; the tab is not focused.
- `pane split <pane> --direction right|down --cwd P --no-focus` → `.result.pane.pane_id`.
- `pane layout --pane <pane>` → `.result.layout.panes[] {pane_id, rect {x,y,width,height}}`.
- `pane rename <pane> <label>`; `pane get <pane>` → `.result.pane {pane_id, tab_id, label}`,
  exit 1 with `pane_not_found` once it is gone.
- `pane run <pane> <command>` types the command and Enter into the pane's shell; a fresh pane
  buffers it until its shell starts. The shell is the **herdr server's** `$SHELL` with the
  server's environment (the human's is fish), not the caller's.
- `pane process-info --pane <pane>` → `.result.process_info {shell_pid,
  foreground_process_group_id}`.
- `pane report-agent <pane> --source S --agent L --state idle|working|blocked --seq N`; a
  lower `--seq` is accepted (exit 0) but ignored. After `working`, `idle` reads back as `done`
  (herdr's not-yet-seen completion).
- `pane report-metadata <pane> --source S --seq N --ttl-ms T --display-agent TEXT`.
- herdr binds a reported agent to the pane's foreground process and releases it (`agent get`
  → `agent_not_found`) when that process exits; a report made while the shell is still
  settling can be released again within a second. Re-reporting the same state does not bump
  `state_change_seq` (no transition).
- `pane close <pane>`: closing a tab's last pane closes the tab.
- A named session restarted from its snapshot keeps pane ids and pane labels, with fresh
  shells and no agent state.

## Goals / Non-goals

**Goals**
- Opt-in, one herdr pane per active campaign that has a persona run, in one dedicated
  `nw runs` tab, showing that run's live `nw campaign tail`, with herdr's agent state and a
  display agent saying what the run is.
- nw touches only the tab and panes it created, records their ids, and closes only those.
- Without the opt-in, `nw herdr watch` behaves exactly as SPEC-0363.

**Non-goals**
- Run tokens (`$nw_spec`, `$nw_loop`, `$nw_cost` of epic invariant 7): tokens need herdr
  config, which SPEC-0363 dropped; the display agent carries the same information.
- Launching the conductor/stewards in herdr, OSC 7501 (epic child 4).
- Any change to the queue, the runner or `runproc`.

## Decision

1. **Opt-in**: `nw herdr watch --run-panes` or `agentic.herdr.run_panes: true` in config.
   Off: no run-pane code runs and no extra herdr call is made.
2. **One pane per active campaign**, not per run. A campaign in `ACTIVE_STATES` with a running
   persona run gets a pane; reviewer groups run several personas at once, so per-run panes
   would triple the pane count. The pane tails the campaign's newest running run; when that
   run ends and another of its runs is (or later becomes) running, the new tail is typed into
   the same pane. The pane stays while the campaign is active, including between runs
   (gating, closing).
3. **Placement**: one tab labelled `nw runs` in the conductor's workspace (resolved as in
   SPEC-0363: `--workspace`, the conductor placement's workspace, the `--conductor-pane`
   prefix), else the watcher's own `HERDR_WORKSPACE_ID`; none → run panes stay off with one
   stderr line. The tab and every split use `--no-focus`. nw never splits a pane it did not
   create: the first pane is the new tab's root pane; later panes split nw's own largest pane
   in that tab. When nw owns no pane any more, herdr has closed the tab with its last pane, and
   the next pane gets a new tab.
4. **Layout**: split the largest of nw's panes (by `pane layout` area, cells) to the `right`
   when its width ≥ 2 × its height (cells are about twice as tall as wide), else `down`. That
   yields columns on a wide tab and a grid as panes are added.
5. **At most 6 panes.** When a campaign needs a pane and 6 exist, the lingering pane that ended
   first is closed early. If all 6 are live, the campaign waits (queue order) and the tab is
   renamed `nw runs (+N)`; it is renamed back when the overflow clears.
6. **Linger 300 s**: when the campaign leaves `ACTIVE_STATES` (parked, waiting, queued,
   merged, abandoned, handed off), its pane stays 300 s with the final transcript, then
   closes. `--run-pane-linger SECONDS` changes it. A campaign that becomes active again within
   the linger keeps its pane.
7. **Agent state** (`pane report-agent <pane> --source nw-run --agent nw-run --state S`, sent
   every push, because herdr drops it when the tail exits with its run; see Context): `working` while a persona run runs; `blocked` when the campaign is
   `PARKED` (escalations park the campaign); `idle` otherwise (between runs, queued, waiting,
   merged, abandoned, handed off).
8. **Display agent** on each run pane, every push (`pane report-metadata <pane> --source
   nw-run --ttl-ms 3×interval --display-agent TEXT`), see Design.
9. **Ownership mark**: each pane is renamed `nw-run:<campaign>`; the ids live in
   `<data_dir>/agentic/conductor/herdr_run_panes.json` with the herdr socket path. A watcher
   that starts (including every `--once`) adopts a recorded pane only when the socket matches
   and `pane get` shows the same tab and label; anything else is forgotten, never touched.
10. **Exit**: a clean exit (Ctrl-C, SIGTERM, `stop()`) closes every pane nw owns. `--once`
    leaves them and saves the state; the next invocation adopts them, so a series of `--once`
    calls creates, updates and closes panes.
11. A pane the human closed is forgotten, and its campaign gets no new pane until it leaves
    `ACTIVE_STATES`.

## Design

### Client (`agentic/herdr.py`)

`Client.call_json(args) -> dict | None`: same gating, timeout, breaker and stderr capture as
`call`, but returns the parsed stdout's `result` object (None on any failure). `call` is
unchanged. New pure argv builders, id first: `tab_create_args(ws, label, cwd)`,
`pane_split_args(pane, direction, cwd)`, `pane_layout_args(pane)`, `pane_get_args(pane)`,
`pane_rename_args(pane, label)`, `pane_run_args(pane, command)`,
`pane_process_info_args(pane)`, `pane_close_args(pane)`, `tab_rename_args(tab, label)`,
`report_agent_args(pane, state, seq, source="nw-run", agent="nw-run")`. `metadata_args` gains
`source=` (default `nw`).

### Run panes (`agentic/herdr_runpanes.py`, used only by `herdr_watch`)

`RunPanes(cfg, client, workspace, *, linger_s=300, max_panes=6, ttl_ms, env, clock)`:

- `load()` reads the state file, drops it when `socket` differs from `HERDR_SOCKET_PATH`, and
  verifies each pane with `pane get` (exists, `tab_id` = recorded tab, `label` =
  `nw-run:<cid>`). For a verified pane whose foreground process group is its shell
  (`pane process-info`), the tail is typed again (herdr restored the pane with a fresh
  shell). Every check is per pane; a failed check forgets that pane only.
- `reconcile(items)` with the snapshot's queue items, once per push:
  1. For each item in `ACTIVE_STATES`, its running runs: run dirs (newest first, at most 8
     looked at) whose `runproc.run_status` is `running`.
  2. Panes of campaigns no longer active get `ended_at` (first time) and their final state;
     after `linger_s` they close. Active again → `ended_at` cleared.
  3. Active panes: if the shown run is no longer running and a newer running run exists, type
     its tail.
  4. Campaigns with a running run and no pane (not dismissed) get one, in queue order, while
     fewer than `max_panes` exist (evicting the earliest-ended lingering pane first).
     Creation: `tab create` (no owned pane) or `pane split` of the largest owned pane, then
     `pane rename`, `pane run` the tail, state and display.
  5. Each pane: `report-agent` and `report-metadata --display-agent`, every push. A failed call is followed by `pane get`; `pane_not_found` forgets the pane and
     dismisses its campaign (until inactive).
  6. Tab label `nw runs` / `nw runs (+N)` renamed on change.
  7. Save the state file (atomic replace).
- `close_all()` closes every owned pane and clears the state file.

**The typed command**: `env NW_CONFIG_DIR=<cfg config_dir> NW_DATA_DIR=<cfg data_dir>
<sys.executable> -m nightwright campaign tail <cid> --run <run> --follow`, each word quoted
with `shlex.quote` (single quotes, which fish and POSIX shells both read). The pane's shell
has the herdr server's environment, so the data/config dirs are passed explicitly, and the
watcher's own interpreter runs the viewer. The pane's cwd is the campaign worktree when it
exists, else the home directory.

**Display agent** (`run_display`), parts joined with ` · `, cut to 64 like SPEC-0363:
- run running: `nw run · <label> · <persona> · loop <n> · $<cost>/$<budget>`; cost is the
  result's real cost once in, else the priced stream estimate (`tail.Renderer`, fed
  incrementally from a per-run byte offset), omitted when unpriced; `/$budget` from
  `request.json` `max_budget_usd`. Example: `nw run · SPEC-0354 · implementer · loop 3 ·
  $1.20/$10`.
- active between runs: `nw run · <label> · gating` (the state, lower case).
- ended: `nw run · <label> · parked: <reason>` / `merged` / `queued` / `waiting` /
  `abandoned` / `handed off`.

### Watcher and CLI

`Watcher(run_panes=False, run_pane_linger=300)`. With run panes on, `push()` also calls
`RunPanes.reconcile(snapshot items)`; `run()` loads the state first; a clean exit calls
`close_all()`; `--once` does not. `nw herdr watch` gains `--run-panes` and
`--run-pane-linger SECONDS` (default 300); config `agentic.herdr.run_panes: true` turns them
on by default.

## Alternatives considered

- **One pane per run** (the epic's wording): a reviewer group runs three personas in parallel,
  so two reviewing campaigns would need six panes, and panes would churn every loop.
- **A dedicated `nw` workspace**: a workspace switch is more intrusive than a tab, and the
  human asked for run panes next to the conductor.
- **Splitting the conductor's tab**: it would resize the human's panes.
- **Run tokens**: need config (SPEC-0363).
- **Closing and recreating every pane on each start**: simpler, but makes `--once` useless
  and flickers on every restart; adoption with an ownership label is safe and cheap.
- **Passing the env with `--env` on `pane split`**: lost when herdr restores a pane from its
  snapshot; the `env …` prefix in the command is not.

## Acceptance criteria

- [x] Without `--run-panes` (and no config), `nw herdr watch` makes exactly the SPEC-0363
      calls; its tests pass unchanged.
- [x] With run panes and one running run: `tab create --workspace <conductor ws> --label "nw
      runs" --no-focus`, `pane rename <root> nw-run:<cid>`, `pane run <root> "env … -m
      nightwright campaign tail <cid> --run <run> --follow"`, `report-agent --state working`,
      `report-metadata --source nw-run --display-agent "nw run · …"`. A second campaign splits
      nw's own pane with `--no-focus`; never a pane nw did not create.
- [x] State follows the campaign: running → `working`, parked → `blocked`, queued / waiting /
      merged / between runs → `idle`, re-reported every push (herdr releases it when the
      tail exits).
- [x] A campaign that leaves `ACTIVE_STATES` keeps its pane for the linger, then
      `pane close`; a new run of an active campaign is typed into the same pane.
- [x] At most 6 panes; overflow evicts the earliest-ended lingering pane, else waits with the
      tab renamed `nw runs (+N)`.
- [x] A clean exit closes every owned pane; `--once` leaves them and a later `--once` adopts
      and closes them. A recorded pane whose label or tab differs, or a state file of another
      socket, is never touched. A pane the human closed is forgotten without error spam.
- [x] The fake herdr models every command used (id first, per-command options, the JSON
      responses copied from herdr 0.9.5); the opt-in contract test covers them on the real
      herdr in an isolated named session.
- [x] Real herdr 0.9.5, isolated session: `nw herdr watch --run-panes --once` against a fake
      campaign with a real `stream.jsonl` fixture creates the tab and pane, `pane read` shows
      the tail, the state goes working → blocked/idle, and the pane closes after the linger.
      The session is torn down afterwards; the human's session is never touched.
- [x] `docs/herdr.md` documents run panes.

## Test plan

- **Automated** (`tests/test_herdr.py`, fake `herdr` on `PATH`): the fake gets a model of each
  command's parser (positional id first, the options each takes, unknown option → exit 2) and
  stateful JSON responses copied from herdr 0.9.5 (`tab create` → `result.tab` +
  `result.root_pane`, `pane split` → `result.pane`, `pane layout`, `pane get` /
  `pane_not_found`, `pane process-info`), with closing a tab's last pane closing the tab.
  - argv builders: id first for each command.
  - `call_json`: returns `result`; failure / bad JSON → None; breaker shared with `call`.
  - off by default: no run-pane calls; the SPEC-0363 tests unchanged.
  - one running run (a run dir with `proc.json` naming a live pid and the real
    `read_tool_schema.jsonl` stream): tab in the conductor workspace, rename, `pane run` with
    the tail command, working, display agent with persona, loop and cost/budget.
  - second campaign: split of nw's own pane, `--no-focus`, direction from the layout.
  - state transitions: run ends with the campaign between runs → idle; parked → blocked;
    a new run → new tail typed in the same pane; the state re-reported each push with a
    higher seq.
  - linger: closed only after `linger_s` (injected clock); reactivation keeps the pane.
  - overflow: 7 campaigns → 6 panes, tab `nw runs (+1)`; a lingering pane is evicted first.
  - adoption: `--once` twice keeps the same pane; a mismatched label, another tab or another
    socket is forgotten and not closed; a pane with only its shell in the foreground gets
    its tail typed again; a pane closed by the human is forgotten and not recreated.
  - clean exit closes all; `--once` does not; CLI `--run-panes` / config key.
  - the typed command runs: executing it (without herdr) prints the tail of the fixture run.
- **Contract test** (`NW_HERDR_CONTRACT_SOCKET=<socket of a running named test session>`):
  every argv used, against the real herdr, read back via `pane get` / `agent get` /
  `tab list`, then closed.
- **Manual (isolated named session)**: start `herdr --session nw-test-<random> server`, run
  `nw herdr watch --run-panes --once` with `HERDR_SOCKET_PATH` of that session and a fake data
  dir; read back with `pane list` / `pane read` / `agent get`; flip the run to ended and the
  campaign to parked; repeat; then past the linger; then stop that session's server and delete
  it. Record commands and output under As shipped.
- **Regression guard**: `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_herdr.py`, `tests/test_agentic_stream.py`.

## As shipped (2026-11-01)

- **Files:** `src/nightwright/agentic/herdr.py` (`Client.call_json`, a shared `_run` spawn for
  `call`/`call_json`; argv builders `tab_create_args`, `tab_rename_args`, `pane_split_args`,
  `pane_layout_args`, `pane_get_args`, `pane_process_info_args`, `pane_rename_args`,
  `pane_run_args`, `pane_close_args`, `report_agent_args`; `RUN_SOURCE = "nw-run"`),
  new `src/nightwright/agentic/herdr_runpanes.py` (`RunPanes`: `load`/`_verify` adoption,
  `reconcile`, `close_all`, `save`; `run_display`, `agent_state`, `status_text`,
  `tail_command`, `running_runs`, `pane_label`, `state_path`), `herdr_watch.py`
  (`Watcher(run_panes=, run_pane_linger=, clock=)`, `conductor_workspace()`,
  `run_pane_workspace()`; push reconciles, `run` loads first and closes on a clean exit),
  `cli_herdr.py` (`--run-panes/--no-run-panes`, `--run-pane-linger`, config
  `agentic.herdr.run_panes`), new `tests/fixtures/fake_herdr.py` (stateful model of the
  herdr 0.9.5 parser and replies, replacing the inline fake), `tests/test_herdr.py`,
  `docs/herdr.md` (Run panes section), `docs/product/reference/index.md` (herdr row).
- **Design as written**, with one change found on the real herdr: the agent state is
  re-reported every push, not only on change. herdr binds a reported agent to the pane's
  foreground process (the tail) and releases it when that exits, and a report made while the
  shell settles can be released again; repeating the same state is not a transition. So after
  a run ends, the state can be missing for up to one push interval (10 s by default).
- **Deviations from the epic** (recorded in Decision/Alternatives): one pane per campaign, not
  per run (reviewer groups run in parallel); a `nw runs` tab in the conductor's workspace
  rather than an `nw` workspace; a display agent instead of run tokens (SPEC-0363).
- **Judgment calls:** max 6 panes, overflow evicts the earliest-ended lingering pane then
  waits with `nw runs (+N)`; linger 300 s; `blocked` only for `PARKED` (escalations park the
  campaign); state kept per herdr socket; an unverifiable pane (herdr unreachable) stays
  recorded but untouched until a later `pane get` confirms or denies it; a pane the human
  closed dismisses the campaign until it is inactive again.
- **Real herdr 0.9.5, isolated session only.** `herdr --session nw-test-0caa2649 server`
  (headless, `SHELL=/usr/bin/fish` like the human's server), every call with
  `HERDR_SOCKET_PATH=~/.config/herdr/sessions/nw-test-0caa2649/herdr.sock --session
  nw-test-0caa2649` and the human's `HERDR_*` removed. Checked each argv by hand first (`tab
  create`, `pane split <id> --direction right --no-focus`, `pane layout --pane`, `pane rename`,
  `pane get` / `pane_not_found` exit 1, `pane run`, `pane process-info --pane`, `pane
  report-agent` incl. a lower `--seq` ignored, `pane report-metadata --source nw-run
  --display-agent`, `tab rename`, `pane close`, last pane closes the tab, a restarted session
  keeps pane ids and labels with fresh shells). Then the CLI end to end against a fake data
  dir (three campaigns with the real `read_tool_schema.jsonl` stream, a conductor placement
  in `w1`, the watcher's own env saying `w99`):
  - `nw herdr watch --run-panes --once --run-pane-linger 20` → `tab w1:t5 'nw runs'
    not-focused 3 panes`; panes `w1:p9 / w1:pA / w1:pB` labelled `nw-run:<cid>`, agent
    `nw-run working`, display `nw run · SPEC-0901 · implementer · loop 1 · $0.01/$5`; the
    conductor's tab still only `w1:p1`. Layout (`pane layout`): `pA` 60×40 at x=60,
    `p9`/`pB` 60×20 stacked (split right, then down). `pane read w1:pB` showed the tail:
    `→ Read …`, `← 4 lines: 1 alpha`, `■ result: success · cost $0.01 / $5.00 · 3 turns ·
    3.4s · verdict done`.
  - runs ended, campaigns set to GATING / PARKED(human_decision) / MERGED, two `--once` →
    `idle 'nw run · SPEC-0901 · gating'`, `blocked 'nw run · SPEC-0902 · parked:
    human_decision'`, `idle 'nw run · SPEC-0903 · merged'`.
  - a new run `002-conformance_reviewer` for SPEC-0901 (REVIEWING) and 20 s later `--once` →
    the parked and merged panes closed after their linger; `run 002-conformance_reviewer`
    typed into the same pane `w1:pB` (`pane wait-output` matched), `working`.
  - `nw herdr watch --run-panes --interval 2` then SIGTERM → exit 0, run pane and `nw runs`
    tab gone, conductor display agent cleared, state file `sessions: {}`.
  - session server restarted: the run pane came back as `w1:pC` with its label and a bare
    shell (`foreground_process_group_id == shell_pid`); `--once` adopted it and typed the tail
    again (matched), `working`. Then MERGED + `--run-pane-linger 0` → closed.
  - Contract tests: `NW_HERDR_CONTRACT_SOCKET=<test socket> uv run pytest tests/test_herdr.py
    -k isolated` (create/split/overflow rename `nw runs (+1)` → back to `nw runs`, layout,
    labels, `working` → `blocked`, `pane read`, adoption via `pane get` + `process-info`,
    close, tab gone, its workspace closed) passed; SPEC-0363's `NW_HERDR_CONTRACT=1` test,
    pointed at the test session's `w1:p1` (given an agent with `report-agent` first, as a
    Claude pane has), passed too.
  - Teardown: `herdr --session nw-test-0caa2649 server stop`, `herdr session delete
    nw-test-0caa2649` (`deleted session`), `~/.config/herdr/sessions/` empty, the human's
    `default` session still running and never addressed; the fake data dir removed.
- **Tests executed:** `tests/test_herdr.py` 44 passed + 2 opt-in contract tests skipped (both
  pass when pointed at an isolated session, above). `tools/affected_tests.py` falls back to
  the full suite ("no test reaches agentic/cli_herdr.py"), so the related set was run by
  hand: herdr, stream/tail, campaign/conduct/queue CLI, conductor brief/cost, events, events
  settings, launch, mailbox, completion, docs product/captures, JSON CLI, spec lint: 659
  passed, 2 skipped. The queue close gate runs the full suite.
- **Not done:** nothing ran against the human's herdr session; the human sees run panes on
  their next `nw herdr watch --run-panes`.

## Rollout / migration

Opt-in; nothing changes until the human adds `--run-panes`. Restart `nw herdr watch` after the
upgrade.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; contract test passes on the real herdr.
- [x] No regressions (the full suite runs at the queue's close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.

## Open questions

None.
