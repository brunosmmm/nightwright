---
id: SPEC-0361
title: "herdr reporter: nw herdr watch pushes queue tokens and notifications"
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-653
parent: SPEC-0360
tags: [agentic, observability, herdr]
---

## Context

Promoted from idea `IDEA-653` (herdr reporter: nw herdr watch pushes queue tokens and
notifications to herdr panes). First child of epic
[SPEC-0360](./0360-look-inside-running-agents-herdr-status-reporting.md), which governs the
design: read its Context (herdr 0.9.5 is push-based; integrations call `herdr pane|workspace
report-metadata` and `herdr notification show`) and its cross-cutting invariants 1-8 and 10.

What exists today (checked 2026-11-01):

- Queue state: `queue.queue_payload` (`nw.queue.v1`, per item `state`, `loops` "n/max",
  `cost_usd`, `cap_usd`, `project`, `label`) and `events.ping_digest` (counts incl.
  `escalations_open`, `undecided_parks`). The event stream (`events.py`) is read with named
  cursors (`cursor_get`/`cursor_set`) under a per-reader lock (`reader_lock`); `sync(cfg)`
  projects new ledger entries exactly once.
- The session registry `conductor_cost.record_session` (`<agentic>/conductor/sessions.jsonl`)
  is called by `events.follow` (the conductor's `nw queue events --follow --for conductor`) and
  by `launch.register`; it de-duplicates on `(reader, session_id)`. `mailbox.follow` (the
  steward's `nw mailbox follow --to steward:P`) does not call it.
- `runproc.child_env` builds every persona child's env (detached wrapper and the in-process
  `runner._child_env`); it passes `HERDR_*` straight through today.

## Goals / Non-goals

**Goals**
- `nw herdr watch`: an opt-in, separate process that shows queue state as herdr sidebar tokens
  (workspace + conductor/steward panes) and raises herdr notifications for escalations,
  non-transient failed runs, conductor-owned parks and merges.
- herdr can never fail or slow the queue, the runner or any other `nw` command.
- Persona runs started inside a herdr pane cannot touch that pane's herdr state.
- `docs/herdr.md` with the suggested `~/.config/herdr/config.toml` snippet.

**Non-goals**
- Run panes (child 3), stream capture (child 2), OSC 7501 (child 4).
- Writing the human's herdr or Claude config; launching the conductor/stewards in herdr.
- Reading herdr state (`events.subscribe`, `api snapshot`).

## Decision

Ship three pieces, as the epic's Decision 1 says: a best-effort herdr client
(`agentic/herdr.py`), a watcher (`agentic/herdr_watch.py`, CLI `nw herdr watch` in
`agentic/cli_herdr.py`) that is the only caller of that client, and two small hooks in existing
code: `runproc.child_env` drops `HERDR_*`, and the conductor/steward followers record the herdr
pane they run in.

## Design

### Client — `src/nightwright/agentic/herdr.py`

- `unavailable(env=None) -> str | None`: the one-line reason herdr is off (`HERDR_ENV` is not
  `1`; `herdr` not on `PATH`), `None` when usable. Checked before every spawn.
- `Client(timeout_s=2.0, fail_max=5, cooldown_s=60.0, clock=time.monotonic, err=sys.stderr)`,
  `Client.call(args) -> bool`: `subprocess.run(["herdr", *args], timeout=…, stdin/stdout/stderr
  = DEVNULL)`; any exception, timeout or non-zero exit → `False`, never raised. Circuit
  breaker: after `fail_max` consecutive failures it stops spawning for `cooldown_s` and writes
  one line to stderr (`nw herdr: 5 consecutive herdr failures; pausing herdr calls for 60s`);
  the first call after the cool-down is a probe (one more failure re-opens it). Module-level
  `call(args)` delegates to a default client (the epic's "one function").
- `Client.next_seq()`: `max(last + 1, epoch milliseconds)`, strictly increasing within a
  process and across watcher restarts (a restarted watcher must not send a lower `--seq` than
  the values it replaces; a per-process counter from 1 would).
- Pure argv builders (unit-tested): `metadata_args(kind, target, tokens, *, seq, ttl_ms,
  source="nw")` → `[kind, "report-metadata", "--source", "nw", "--seq", N, "--ttl-ms", T,
  "--token", "nw_x=v"…, "--clear-token", "nw_y"…, TARGET]` (an empty value becomes
  `--clear-token`); `clear_args(kind, target, names, *, seq, source="nw")`;
  `notify_args(title, body, sound)` → `["notification", "show", TITLE, "--body", BODY,
  "--sound", none|done|request]`.

### Pane registry — `conductor_cost.record_herdr_pane(cfg, reader, env)`

When `env["HERDR_ENV"] == "1"` and `HERDR_PANE_ID` is set, writes
`<agentic>/conductor/herdr_panes.json` = `{reader: {pane_id, workspace_id, tab_id, session_id,
pid, start_ticks, ts}}` (atomic, under the registry lock; newest wins per reader). Called next
to `record_session` in `events.follow` (reader as given, e.g. `conductor`) and at the start of
`mailbox.follow` (reader = the address, e.g. `steward:Meta-Tools`). Never raises into the
follower. Only the follower's own env is read; no herdr call.

**Deviation from epic invariant 6** (it says "store `herdr` in the session record"): the
session registry de-duplicates on `(reader, session_id)` and `conductor-launch` pre-registers
the session with an empty env (`launch.register`), so the follower's later record, the one
that sees `HERDR_*`, is dropped as a duplicate; and the steward's follower is the mailbox
follower, which records no session. A sibling placement file keyed by reader, rewritten by
every follower start, has neither problem and leaves `sessions.jsonl` (cost attribution,
SPEC-0299) untouched. A placement is *live* only while its follower `pid` runs with the same
`/proc` start time; the watcher ignores dead ones, so a closed pane is never targeted (a dead
target would also feed the breaker).

### Watcher — `src/nightwright/agentic/herdr_watch.py`

`Watcher(cfg, client, *, interval_s=10, conductor_pane=None, workspace=None, env=os.environ)`:

- **Targets** (all explicit ids on the argv): workspaces = `--workspace` if given, else the
  watcher's own `HERDR_WORKSPACE_ID` plus the `workspace_id` of every live placement; panes =
  the live placement of reader `conductor` (replaced by `--conductor-pane`) and of each
  `steward:<P>`. Other readers are ignored. The watcher's own `HERDR_PANE_ID` is never a target.
- **Tokens** (`--source nw`, `--ttl-ms` = 3 × interval in ms, `--seq` from `next_seq`):
  - workspace: `nw_queue` = `"A active · Q queued · P parked"`; `nw_attn` = the non-zero
    parts of `"E esc · U undecided"` (cleared when both are 0); `nw_cost` = today's queue
    spend `"$12.40"` (sum of `cost.total_usd` of campaigns updated today, the rule
    `dashboard_payload` uses).
  - conductor pane: `nw_role=conductor`, `nw_attn` as above, `nw_cost` = the conductor
    session's priced cost today (`conductor_cost.cost_report(since=today, until=today,
    session_ids=[placement session])`, refreshed at most every 60 s; cleared when there is no
    session id, no price or any error).
  - steward pane for project P (P's items in `ACTIVE_STATES`): none → `nw_spec=idle`, loop and
    cost cleared; one → `nw_spec` = its label, `nw_loop` = `"<state lowercase> <loops>"`,
    `nw_cost` = `"$1.20/$5.00"`; several → `nw_spec="N active"`, `nw_loop` cleared, `nw_cost`
    = summed spend / summed caps. `nw_role=steward:P` always.
- **Notifications** from the event stream with its own cursor `herdr` (under
  `events.reader_lock(cfg, "herdr")`, one watcher at a time; a brand-new cursor starts at the
  current end, like `events.follow`). Each poll: `events.sync(cfg)` (no ping: pings are the
  conductor follower's job), then for every event after the cursor: `escalation` →
  `--sound request`; `run_failed` with `transient: false` → `request`; `parked` with
  `queue.park_owner(reason) == "conductor"` → `request`; `merged` → `done`; everything else
  silent. The cursor advances past every event whether or not herdr accepted the call (best
  effort: no retry storm, no duplicate).
- **Loop**: poll events every `min(2, interval)` s, push tokens every `interval` s. A clean
  exit (SIGINT/SIGTERM, or `stop()` in tests) sends one `--clear-token` call per target for
  the tokens it pushed. `--once`: one event poll + one push, then exit *without* clearing
  (the TTL expires them).
- **No code watch**: unlike Monitor followers, the watcher does not exit on a nightwright code
  change (it is a human-run pane process; exiting would blank the sidebar). After an upgrade,
  restart it; documented in `docs/herdr.md`.

### CLI — `nw herdr watch [--interval S] [--conductor-pane ID] [--workspace ID] [--once]`

`agentic/cli_herdr.py`, registered from `agentic/cli.register`. When `herdr.unavailable()`
returns a reason, it prints one line (`nw herdr: <reason>; nothing to do`) and exits 0 without
spawning anything. The client and watcher modules are imported lazily inside the command, so
importing the CLI (every `nw` command) never imports `herdr.py`. A second watcher fails with
the reader-lock message (exit 1).

### Persona env — `runproc.child_env`

Drops every key starting with `HERDR_` (after the SPEC-0319 launcher keys; `extra` still wins).

### Docs — `docs/herdr.md`

What it does, how to start it in a herdr pane, the token table, the notification policy, the
suggested `[ui.sidebar.agents] rows_by_agent` / `[ui.sidebar.spaces] rows` snippet for
`~/.config/herdr/config.toml` (which nightwright never writes), and the restart-after-upgrade
note.

## Alternatives considered

- **Report from the queue loop** — rejected by the epic (invariant 2): the loop must not depend
  on herdr in any way.
- **Per-process `--seq` counter from 1** — a restarted watcher would send lower seqs than the
  live values; epoch-ms seeding avoids depending on herdr's seq semantics.
- **herdr placement inside `sessions.jsonl`** — see the deviation note under Design.
- **Retrying failed notifications** — dropped: a flapping herdr would replay alerts; the
  sidebar tokens already carry the state.

## Acceptance criteria

- [x] `nw herdr watch` pushes workspace tokens (`nw_queue`, `nw_attn`, `nw_cost`) and
      conductor/steward pane tokens to explicit ids from the placement registry (or
      `--conductor-pane` / `--workspace`), every call with `--source nw`, `--ttl-ms` = 3 ×
      interval and a strictly increasing `--seq` (also across a restart).
- [x] An escalation, a non-transient `run_failed`, a conductor-owned `parked` and a `merged`
      event each produce exactly one `notification show` with the sound of invariant 8; other
      events (transient failures, human-owned parks, the rest) produce none; a second run does
      not repeat them; a new cursor does not replay history.
- [x] A clean exit clears every token it pushed; `--once` leaves them to expire.
- [x] Without `HERDR_ENV=1`, or without `herdr` on `PATH`, `nw herdr watch` exits 0 with one
      line and no `herdr` process is spawned (the client refuses too).
- [x] The client times a call out at 2 s (default) and never raises; after 5 consecutive
      failures it stops spawning for 60 s and logs one stderr line; a hanging or failing herdr
      only slows the watcher.
- [x] Importing `nightwright.cli`, the queue, the runner, `runproc` and the event stream does
      not import `nightwright.agentic.herdr` or `herdr_watch`.
- [x] Persona children get no `HERDR_*` variable (detached wrapper and in-process runner).
- [x] The conductor (`nw queue events --follow`) and steward (`nw mailbox follow`) followers
      record their herdr pane/workspace when `HERDR_ENV=1`; dead placements are ignored.
- [x] `docs/herdr.md` documents the integration and the suggested config snippet; nothing
      writes the human's herdr config.

The manual check in a real herdr session (Test plan) is recorded as pending the human; it is
not an acceptance criterion of this spec because agents may not drive the human's herdr.

## Test plan

- **Automated tests** (`tests/test_herdr.py`, all against a fake `herdr` on `PATH`: a Python
  script appending its argv as one JSON line to a log, modes `ok` / `hang` / `fail` via an env
  var; an autouse fixture removes every ambient `HERDR_*` and restricts `PATH` so the real
  herdr is never reached):
  - client: gated off (no `HERDR_ENV`, no binary) → `False` and the log file never created;
    ok → `True` + argv logged; fail → `False`; hang with a short timeout → `False` within the
    bound; default timeout is 2.0; breaker opens after 5 failures (6th call spawns nothing, one
    stderr line) and probes again after the injected-clock cool-down; `next_seq` strictly
    increasing and ≥ epoch ms.
  - argv builders: `--source nw`, `--ttl-ms`, `--seq`, `--token` / `--clear-token`, target last.
  - watcher push: campaigns built with the `test_agentic_events` fixtures (an IMPLEMENTING
    Meta-Tools campaign, a queued one, a parked one) + live placements for `conductor` and
    `steward:Meta-Tools` (this test's pid) + a dead placement → workspace and pane
    `report-metadata` calls with the expected tokens, explicit ids, ttl = 3 × interval, seqs
    increasing across pushes and across a new `Client`; the dead placement's pane is never
    targeted; `--conductor-pane` overrides.
  - notifications, driven through the production projection (ledger `run.end` non-transient
    and transient, `park` cost_cap and main_dirty, `transition` → MERGED, plus an appended
    `escalation` event) and `events.sync`: exactly one `notification show` for each of the four
    notifiable events with the right `--sound`, none for the others; a second watcher run
    sends nothing new; a fresh cursor created after the history sends nothing.
  - clean exit clears the pushed tokens; `--once` does not.
  - hanging herdr: a push returns within the bound and the breaker line is logged.
  - CLI: `nw herdr watch --once` without `HERDR_ENV` / without the binary → exit 0, one line,
    log absent; with the fake → logged calls.
  - import isolation: a subprocess imports `nightwright.cli`, `agentic.queue`, `runner`,
    `runproc`, `events` and asserts neither herdr module is in `sys.modules`.
  - persona env: `runproc.child_env` drops `HERDR_*`; the detached wrapper (a fake child that
    dumps its env, as in `test_agentic_launch.py`) started with `HERDR_*` set sees none.
  - placement: `record_herdr_pane` with and without `HERDR_ENV`; `events.follow` and
    `mailbox.follow` (bounded by `max_events`/`stop`) record the reader's pane under a herdr
    env.
- **Regression guard:** `test_agentic_launch.py` env tests (adjusted to compare against the
  env minus `HERDR_*`), `test_agentic_events.py`, `test_agentic_conductor_cost.py` and the
  mailbox tests, via `python3 tools/affected_tests.py --run uv run pytest -q`.
- **Manual verification (pending the human; agents may not drive the real herdr session):** in
  a herdr 0.9.x pane, paste the `docs/herdr.md` snippet into `~/.config/herdr/config.toml`,
  start the conductor and a steward in herdr panes (their followers record the panes), then in
  a third pane run `nw herdr watch --once` (one push; tokens visible for ~30 s) and
  `nw herdr watch` (live). Expect the workspace row to show `$nw_queue`, the conductor and
  steward panes their tokens, a "done" sound on a merge; Ctrl-C clears the tokens, `kill -9`
  lets them expire within 30 s. Outside herdr, `nw herdr watch` prints one line and exits 0.

## As shipped (2026-11-01)

- **Files:** `src/nightwright/agentic/herdr.py` (client: `unavailable`, `Client.call` /
  `next_seq` / breaker, module `call`, argv builders), `src/nightwright/agentic/herdr_watch.py`
  (`Watcher`: `snapshot`, token builders, `targets`, `push`, `poll_events`, `clear`, `run`;
  `notification_for`), `src/nightwright/agentic/cli_herdr.py` (`nw herdr watch`, registered in
  `agentic/cli.register`), `conductor_cost.record_herdr_pane` / `herdr_panes` /
  `placement_live`, the hooks in `events.follow` and `mailbox.follow`, the `HERDR_*` filter in
  `runproc.child_env`, `docs/herdr.md`, a `herdr` row in `docs/product/reference/index.md`
  (the reference test requires every top-level command).
- **Design as written**, with these judgment calls recorded here:
  - placement lives in `herdr_panes.json`, not in `sessions.jsonl` (the deviation from epic
    invariant 6 explained under Design);
  - the workspace targets are the watcher's own `HERDR_WORKSPACE_ID` plus the placements'
    workspaces (or only `--workspace`); the watcher's own *pane* is never a target;
  - the watcher does not exit on a code change (documented: restart after an upgrade);
  - a failure inside one poll or push (e.g. an unreadable campaign) prints one stderr line and
    the loop carries on;
  - the `docs/herdr.md` snippet covers `[ui.sidebar.agents.rows_by_agent] claude` and
    `[ui.sidebar.spaces] rows` (syntax taken from the comments of herdr 0.9.5's embedded
    default config). The epic's optional `tab_bar_right` command entry is left out: its
    exact TOML shape could not be confirmed without running herdr, and a wrong snippet is
    worse than none;
  - human-owned parks (`main_dirty`, `main_not_on_base`, `human_decision`) are silent, as
    invariant 8 says; `human_decision` parks already come with an `escalation` event.
- **Tests executed:** `tests/test_herdr.py`, 26 tests, all passing (client gating / ok / fail /
  hang / breaker open + probe + re-open / seq; argv builders; placement record + liveness +
  both followers; token push to explicit ids with ttl and seq across a restart; overrides;
  steward idle/many; notifications through the real ledger → `events.sync` projection with
  exactly one per notifiable event and none on restart or for history; clean-exit clear vs
  `--once`; reader lock; hanging herdr bounded + breaker line; the threaded loop; CLI no-op
  and fake run; import isolation in a subprocess; `child_env` and the detached `runproc`
  wrapper with `HERDR_*` set). `tests/test_agentic_launch.py::test_runner_child_env_drops_launch_keys`
  now compares against the ambient env minus `HERDR_*` (it failed when the suite itself ran
  inside a herdr pane). Regression set: 460 + 42 + 49 tests (events, ping, mailbox,
  conductor cost/brief, launch, runner, runproc, mvp report, completion, CLI permission /
  status tests, docs tests) pass. `tools/affected_tests.py` fell back to the full suite
  ("no test reaches agentic/cli.py"), so the affected set was run by hand instead; the queue
  close gate runs the full suite.
- **Fix after first real use (2026-11-01): every `report-metadata` call failed.** The human ran
  `nw herdr watch --conductor-pane w7:p2` and got only the breaker line. Root cause: herdr
  0.9.5's parser needs the positional pane/workspace id directly after the subcommand
  (`pane report-metadata ID --source nw …`). With options first it exits 2 with
  `unknown option: nw`, even though `--help` lists the id last. The argv builders put it last,
  and the fake `herdr` accepted any argv, so all 26 tests passed while every real call failed.
  The missing check was a single real herdr call. Fix: `metadata_args`/`clear_args` emit the id
  third. The fake now rejects options before the id, as herdr 0.9.5 does. `Client` keeps herdr's
  stderr as `last_error`, and the breaker line now names the failing command and its error.
  `test_real_herdr_accepts_our_argv` is an opt-in contract test (`NW_HERDR_CONTRACT=1`, inside
  a herdr pane) that writes and clears a throwaway token on the real herdr.
- **Manual verification (2026-11-01, real herdr 0.9.5, user's session):** the contract test
  passed, and `nw herdr watch --once --conductor-pane w7:p2` exited 0. `herdr api snapshot`
  then showed `nw_role`/`nw_cost` on the conductor pane, `nw_role`/`nw_spec`/`nw_cost` on both
  steward panes, and `nw_queue`/`nw_cost` on both workspaces. Still the human's to check: the
  sidebar rendering with the `docs/herdr.md` config snippet, and the notification sounds.

## Rollout / migration

Opt-in: nothing calls herdr until the human runs `nw herdr watch` inside herdr. The
`child_env` change applies to every new persona run (in-flight detached runs keep the env they
started with). Followers write `herdr_panes.json` on their next (re)start.

## Definition of done

- [x] Acceptance criteria all met; manual herdr check recorded as pending the human.
- [x] Test plan executed; affected tests pass.
- [x] No regressions (the queue close gate runs the full suite).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.

## Open questions

None.
