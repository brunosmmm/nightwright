---
id: SPEC-0366
title: "herdr run tabs: one tab per campaign named after the work, supersedes the shared nw runs tab"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-662
parent: SPEC-0360
supersedes: [SPEC-0364]
depends_on: [SPEC-0362, SPEC-0363, SPEC-0365]
tags: [agentic, observability, herdr]
---

## Context

Promoted from idea `IDEA-662`. Child of epic
[SPEC-0360](./0360-look-inside-running-agents-herdr-status-reporting.md). It supersedes
[SPEC-0364](./0364-herdr-run-panes-one-pane-per-active-campaign-in.md) (run panes). The
watcher that runs them is started by the conductor's follower
([SPEC-0365](./0365-herdr-watcher-auto-start-the-conductor-follower.md)).

What the human saw in herdr 0.9.5 with SPEC-0364 running live (2026-11-01):

1. **The name says nothing.** herdr's default sidebar rows for an agent pane are
   `[state_icon, machine, workspace, tab]` / `[agent]`. Every run pane lives in the shared
   `nw runs` tab, so every row reads `nightwright · nw runs`. The pane label
   (`nw-run:<campaign>`) is never shown.
2. **The typed command shows.** The top of each pane is the long typed line
   `env NW_CONFIG_DIR=… NW_DATA_DIR=… …/python -m nightwright campaign tail CID --run RUN
   --follow`. It also lands in the human's fish history, once per run.
3. **Flicker.** The agent entries for run panes appear briefly and vanish. `herdr agent list`
   sampled every 3 s showed `w7:p8 nw-run working` for 3 samples, then no `nw-run` agent at
   all while both panes still existed. A pane whose run had ended never had an entry. herdr
   binds a reported agent to the pane's foreground process and drops it when that process
   exits. `nw campaign tail --follow` exits at the end of each run, and a report made while
   the shell settles is dropped again.
4. **Panes outstay the work.** A pane lingered until its campaign left `ACTIVE_STATES`, plus
   300 s. A campaign that spends many minutes in GATING or CLOSING kept a tab showing a
   finished run (live: SPEC-0354 CLOSING and SPEC-0349 GATING both still had panes).

herdr 0.9.5 facts this design relies on, checked in an isolated named session (see As
shipped): `tab create … --no-focus` returns the tab and its root pane; closing a tab's last
pane closes the tab; `pane get` returns `tab_id` and `label`; `pane run` types the text and
Enter into the herdr server's shell (fish for this human) and **strips a leading space**, so
fish saves the line to its history; herdr has no option to start a pane with a command, so
typing is the only way in; `clear`-style escape sequences wipe the typed line and the
scrollback.

## Goals / Non-goals

**Goals**
- One herdr tab per campaign with a running persona run, labelled after the work
  (`SPEC-0349 nw doctor`), so the sidebar row says what the pane is about.
- The pane's foreground process lives as long as the pane, so herdr never drops the agent.
- A tab exists while a run is running, then lingers briefly (default 60 s) and closes.
- No visible typed command; one shell-history line per tab, not per run.
- Panes made by SPEC-0364's code are migrated safely; adoption across watcher restarts keeps
  working.

**Non-goals**
- Anything outside run panes (display agents of the conductor/steward panes, notifications,
  workspace tokens, the auto-start supervisor): unchanged.
- Run tokens (still need herdr config, SPEC-0363).

## Decision

1. **Opt-in, as before**: `agentic.herdr.run_panes: true` or `nw herdr watch --run-panes`.
   Off: no run-pane code runs.
2. **One tab per campaign** in the conductor's workspace (same resolution as SPEC-0364:
   `--workspace`, the conductor placement's workspace, the `--conductor-pane` prefix, else the
   watcher's own `HERDR_WORKSPACE_ID`; none → off with one stderr line). `tab create
   --no-focus`; the run pane is the tab's root pane. nw never splits a pane any more.
3. **Tab label** `<campaign label> <short title>`, e.g. `SPEC-0349 nw doctor`. The short title
   comes from the campaign's `spec_at_open.md` frontmatter `title` (internal and outbound
   specs), else the idea heading of an idea campaign; markdown is stripped (backticks, `*`,
   `_` emphasis, links → their text), the first clause before `:` / `—` / `–` is kept, cut
   to 24 characters with `…`. No title (merge requests) → the campaign label alone. If
   another run tab of nw's already carries the same label, the campaign id's last segment is
   appended (`SPEC-0349 nw doctor 72ea`), then a counter.
4. **Display agent** (sidebar row 2): the tab now carries the spec, so the label is dropped:
   `nw run · adversarial_reviewer · loop 2 · $0.89/$2.50` while a run runs, `nw run ·
   gating` / `nw run · parked: cost_cap` / `nw run · merged` after it ends. `nw run` stays as
   the marker that tells a run pane from a Claude pane. Cut to 64 characters as before.
5. **A viewer that follows the campaign**: the pane runs `nw campaign tail CID
   --follow-campaign` once. It clears the screen (so the typed line is gone), tails the
   campaign's newest running run, and when that run ends prints a footer `■ run <run> ended ·
   waiting for the next run · campaign <state>`, prints a line when the campaign's state
   changes, and starts tailing the next run itself (a rule and the run's header line). It
   exits only on a signal (the watcher closing the pane). Nothing is typed into a pane after
   the first command, except to restart the viewer in a pane herdr restored with a fresh
   shell (SPEC-0364 adoption rule).
6. **Lifetime**: a campaign gets a tab while it has a running persona run (any queue state;
   the run is what the pane shows). When no run of it is running, the tab lingers
   `run_pane_linger_s` (default **60 s**; `agentic.herdr.run_pane_linger_s`, CLI
   `--run-pane-linger SECONDS` overrides) and closes. A run that starts within the linger
   continues in the same tab (the viewer switches by itself); one that starts later opens a
   new tab. **Parked campaigns get the same linger** (decided: a parked campaign can sit for
   hours; the conductor and its sidebar row already show the park, and the tab would show a
   finished transcript). During the linger a parked campaign's pane is `blocked`, so the park
   is visible for that minute.
7. **Agent state** (`pane report-agent --source nw-run --agent nw-run`): `working` while a run
   runs, `blocked` when the campaign is `PARKED`, `idle` otherwise. Still re-reported every
   push: the viewer keeps the foreground process alive, but a report made in the first second
   (shell settling, viewer starting) can be dropped, and a repeat is not a transition.
8. **At most 6 run tabs.** A seventh campaign first evicts the earliest-ended lingering tab;
   if none lingers it waits and nw's most recently created run tab gets ` (+N)` appended to
   its label (N campaigns waiting), removed when the overflow clears.
9. **Ownership and adoption**: each pane is still renamed `nw-run:<campaign>`. The state file
   (`<data_dir>/agentic/conductor/herdr_run_panes.json`, schema `nw.herdr.run_panes.v2`)
   records per pane its `tab_id` and tab label. A restarted watcher adopts a recorded pane only
   when `pane get` shows that label in that pane's recorded tab. Anything else is forgotten,
   never touched. A clean exit closes every owned pane (and so its tab); `--once` leaves them.
10. **Migration from SPEC-0364** (schema v1: one session-level `tab`, panes without a tab):
    each recorded pane is verified exactly like adoption (`nw-run:<campaign>` label in the
    recorded shared tab). A verified old-scheme pane is closed (it is nw's; closing the shared
    tab's last pane closes the `nw runs` tab) and forgotten without dismissing its campaign,
    so the same push opens that campaign's own tab if its run is still running. An old pane
    that fails verification is forgotten untouched; one that cannot be checked (herdr down)
    stays recorded for a later check. Nothing outside `herdr_run_panes.json` is considered.
11. **Human-closed pane**: forgotten; its campaign gets no new tab until no run of it is
    running (then a later run opens one again).

## Design

### Viewer (`agentic/tail.py`, `nw campaign tail --follow-campaign`)

`follow_campaign(cfg, cid, *, out, poll_s=1.0, sleep, stop=None, clear=True)`:

- writes `ESC[H ESC[2J ESC[3J` first when `clear` (the terminal, not the shell, wipes the
  typed line; shell-agnostic, no `clear` binary needed);
- at start, when no run is running, renders the newest run once (not following) so a
  restored pane shows the last transcript;
- loop: the newest running run not yet shown (`running_runs`: run dirs newest first, at most
  8, `run_status` in `running` / `orphan_group`) → a dim rule (after the first), then
  `tail(…, follow=True)`, then the footer with the campaign state; otherwise, when the
  campaign state changed since the last line, `· campaign <state>`; sleep `poll_s`.
- `stop()` (tests) ends the loop; Ctrl-C / SIGTERM / SIGHUP end the process (exit 130).

`running_runs` moves from `herdr_runpanes` to `tail` (the viewer and the watcher share it).
CLI: `--follow-campaign` on `nw campaign tail` (ignores `--run`/`--follow`, honours `--raw`).

### Run tabs (`agentic/herdr_runpanes.py`)

- `short_title(text)`, `title_of(cfg, cid, item)`, `tab_label(label, title)` (pure, tested).
- `tail_command(cfg, cid)`: `env NW_CONFIG_DIR=… NW_DATA_DIR=… <python> -m nightwright
  campaign tail <cid> --follow-campaign`, `shlex.quote`d (fish and POSIX shells read single
  quotes alike).
- Pane record: `{pane_id, tab_id, tab_label, shown_label, run, state, ended_at, created_at}`.
- `reconcile(items, workspace)`: verify pending panes; running runs for every queue item and
  every campaign with a pane; per pane: running → `ended_at = None`, `run` = newest running;
  not running → `ended_at` set once, close after the linger; then create tabs for running
  campaigns without one (queue order, not dismissed, max 6, evicting the earliest-ended
  lingering pane); report state + display every push; apply the overflow suffix; save.
- `load()` reads v1 or v2; v1 panes get the session `tab` as their `tab_id` and `legacy`.

### CLI / config

`nw herdr watch --run-pane-linger` defaults to `agentic.herdr.run_pane_linger_s`, else 60.
Help texts name tabs instead of the `nw runs` tab.

## Alternatives considered

- **`clear; ` typed before the tail**: works in fish/bash/zsh, but depends on a `clear` binary
  and still types once per run. The viewer clearing the screen itself needs neither.
- **A leading space to keep the line out of the shell history**: herdr strips it (checked).
- **`exec` the viewer**: if it crashed the pane would vanish and nw would dismiss the campaign;
  a shell left behind is re-armed by the fresh-shell adoption rule instead.
- **A dedicated `nw herdr run-pane CID` entrypoint**: the viewer is useful outside herdr
  (`nw campaign tail CID --follow-campaign` in any terminal), and `cli_herdr` is the module
  that calls herdr; the viewer must not.
- **Keeping a parked campaign's tab until it resumes**: hours of a stale transcript; see D6.
- **Adopting old-scheme panes in place**: the shared tab is labelled `nw runs` and may hold
  several panes; herdr 0.9.5 has no command to move a pane to another tab, and renaming the
  shared tab would name it after one campaign. Close and recreate is simple and only touches
  panes nw recorded and herdr confirms by label.
- **Overflow shown in a separate tab**: one more tab for a rare case; a suffix on nw's newest
  tab is enough.

## Acceptance criteria

- [ ] Each campaign with a running run gets its own tab, `tab create --workspace <conductor
      ws> --label "<SPEC-ID> <short title>" --no-focus`; no `pane split`; two campaigns with
      the same label get distinct tab labels.
- [ ] `short_title` strips markdown and keeps the first clause, ≤ 24 chars with `…`; no title
      → the campaign label alone.
- [ ] Display agent drops the spec id: `nw run · <persona> · loop N · $c/$b` /
      `nw run · <status>`.
- [ ] The pane gets one typed command, `… campaign tail <cid> --follow-campaign`; a later run
      of the campaign is shown by the same viewer, nothing more is typed; `pane read` shows
      no typed command line.
- [ ] `--follow-campaign` follows run → footer → next run and exits only on a signal.
- [ ] Real herdr: `herdr agent list` sampled across a run end and a new run start keeps the
      `nw-run` agent entry for the pane the whole time.
- [ ] A tab closes `run_pane_linger_s` (default 60, config key, CLI flag) after its campaign's
      last run ended, whatever the campaign state (parked included); a run within the linger
      keeps the tab; a later run opens a new tab. Closing the pane removes the tab.
- [ ] At most 6 run tabs; overflow evicts the earliest-ended lingering tab, else waits with
      ` (+N)` on nw's newest run tab.
- [ ] Adoption after a watcher restart: per-pane tab check; mismatched label/tab or another
      socket never touched; a fresh shell gets the viewer typed again.
- [ ] Migration: a v1 state file's verified panes are closed (the `nw runs` tab disappears)
      and their still-running campaigns get their own tabs; unverified ones are not touched.
- [ ] The fake herdr models the commands used, from real 0.9.5 replies; `docs/herdr.md`
      describes run tabs.
- [ ] Isolated real-herdr session (SHELL=fish), torn down afterwards; the human's session only
      read after landing.

## Test plan

- **Automated** (`tests/test_herdr.py`, fake herdr; `tests/test_agentic_stream.py` for the
  viewer):
  - `short_title` / `tab_label` / disambiguation; `run_display` without the label.
  - one running run → its tab and label, rename, one `pane run` with `--follow-campaign`,
    working, display; a second campaign → a second `tab create`, no split.
  - run ends → idle/blocked, closed after the linger (injected clock, 60 s default); a new run
    within the linger → same pane, nothing typed; after the linger → a new tab.
  - parked → blocked during the linger, then closed.
  - overflow: 7 campaigns → 6 tabs, ` (+1)` on the newest; a lingering one evicted first.
  - adoption (`--once` twice), mismatched label/tab/socket untouched, fresh shell retyped,
    human-closed pane dismissed until its run ends.
  - migration: a v1 state file with a pane made by the old layout (shared `nw runs` tab) →
    closed, tab gone, the campaign gets its own tab; a v1 pane whose label differs untouched.
  - CLI: `--run-pane-linger` from config; `--follow-campaign` renders two runs with the
    footer between them (injected `stop`), starts with the clear sequence.
  - the typed command actually runs (`/bin/sh -c`) and prints the fixture transcript.
- **Contract test** (`NW_HERDR_CONTRACT_SOCKET`, isolated session) updated for tabs.
- **Manual, isolated named session** with `SHELL=/usr/bin/fish`, a fake data dir, 2+
  campaigns with real stream fixtures: `tab list` labels, `pane read` without the command
  line, `agent list` sampled across run end/start, linger close removing the tab, adoption
  after a watcher restart, migration from a layout created by SPEC-0364's code; teardown with
  `server stop --session` + `session delete`.
- **Regression**: `tools/affected_tests.py` plus the related set by hand.

## As shipped

(filled in at done)

## Rollout / migration

Opt-in as before. On upgrade the old watcher is stopped by its follower (SIGTERM closes its
panes and the `nw runs` tab); if one survives (`kill -9`, `--once`), the new watcher's
migration closes it and opens per-campaign tabs.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; contract test passes on the real herdr.
- [ ] No regressions (the full suite runs at the queue's close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.

## Open questions

None.
