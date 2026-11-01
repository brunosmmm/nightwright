---
id: SPEC-0360
title: "Look inside running agents: herdr status reporting, stream-json run capture and a tail viewer"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-427
kind: epic
tags: [agentic, observability, herdr, conductor]
---

## Context

Promoted from idea `IDEA-427`. The human asked (2026-10-30) to see what every
dispatched agent is doing at any moment and to look into its internals, ideally inside
[herdr](https://herdr.dev) (a terminal workspace manager for AI coding agents) next to the
long-lived conductor and stewards.

What exists today, checked against the code on 2026-11-01:

- **Persona runs are opaque while they run.** `runner.build_argv`
  (`src/nightwright/agentic/runner.py:153`) runs `claude -p --output-format json
  --no-session-persistence …`. The detached wrapper (`runproc.py:265`, SPEC-0225) pipes stdout
  into `runs/<run>/result.json`, which holds one final JSON object written at the very end. No
  transcript exists (`--no-session-persistence` is deliberate: session files would pollute
  nightwright's own transcript-based time tracking). `parse_result` already has a defensive
  branch for a stream-json *array* but not for JSONL. `nw campaign run` shows the verdict and
  a stderr tail only.
- **Queue state is rich but only text/JSON.** `nw queue conduct-status --json`
  (`nw.conduct.status.v1`), `nw queue list`, `nw queue watch` and the event stream
  (`events.py`, `nw.event.v1`: `parked`, `merged`, `run_failed`, `escalation`, `cost_warning`,
  …) already say everything a status display needs. A follower reads them with a named cursor
  (`nw queue events --follow --for NAME`).
- **The conductor and stewards are Claude Code sessions** started by
  `nw queue conductor-launch` / `steward-launch` (SPEC-0319). Their followers register in the
  session registry (`conductor_cost.record_session`, SPEC-0299). SPEC-0319 explicitly left
  "`--in herdr` placement" to this idea.

**Correction to the idea's original framing.** The idea assumed herdr detects agent state only
by screen/title heuristics, which made OSC 7501 (Mitchell Hashimoto's program-status escape
sequence) look like the way in. Verified on the installed herdr 0.9.5 (2026-11-01): herdr is
**push-based**. It knows nothing about nightwright; it injects `HERDR_ENV=1`,
`HERDR_SOCKET_PATH`, `HERDR_PANE_ID`, `HERDR_TAB_ID`, `HERDR_WORKSPACE_ID` into every pane, and
integrations push state through its CLI/socket. herdr's own Claude Code integration is just
`~/.claude/hooks/herdr-agent-state.sh` calling `herdr pane report-agent`. The relevant surface:

| Facility | CLI (0.9.5) | Shows up as |
|---|---|---|
| Pane display metadata | `herdr pane report-metadata --source ID [--token NAME=VALUE]… [--clear-token NAME] [--title] [--display-agent] [--state-label S=TEXT] [--seq N] [--ttl-ms N] PANE_ID` | `$name` tokens in `[ui.sidebar.agents] rows` / `rows_by_agent` |
| Workspace display metadata | `herdr workspace report-metadata --source ID [--token …] [--clear-token …] [--seq N] [--ttl-ms N] WS_ID` | `$name` tokens in `[ui.sidebar.spaces] rows` |
| Agent lifecycle | `herdr pane report-agent --source ID --agent LABEL --state idle\|working\|blocked\|unknown [--message] PANE_ID`, `release-agent` | sidebar state icon, attention queue, sounds |
| Notifications | `herdr notification show TITLE [--body] [--sound none\|done\|request]` | toast + sound |
| Panes/tabs | `herdr tab create --workspace WS --label L --no-focus`, `herdr pane run`, `herdr pane close` | a pane per thing |
| Status bar | `[ui] tab_bar_right` entries of type `command` | a server-run shell command's output |
| Events | socket `events.subscribe` / `events.wait`, `herdr api snapshot` | (for consumers; not needed here) |

So token metadata + `report-agent` is the clean path for the herdr case, and OSC 7501 is at
best a later, terminal-generic extra: herdr does not implement it today.

**Pitfall found while verifying.** A headless `claude -p` persona run spawned (via the queue
loop) from inside a herdr pane inherits that pane's `HERDR_*` variables. If the human has
herdr's Claude hook installed, every persona run's hooks would `report-agent` against the
*conductor's* pane (or whichever pane started the loop) and clobber its state. Any reporter
must target explicit pane ids with its own `--source`, and persona children must not see
`HERDR_*` at all.

This needs several specs, not one: a herdr reporter (useful today, no runner change), a change
to how every persona run is captured (touches the runner contract and in-flight campaigns),
a viewer on top of that capture, and a pane-per-run layer that needs both.

## Goals / Non-goals

**Goals**
- At a glance inside herdr: what the queue is doing (active / queued / parked, open
  escalations) and, per conductor/steward pane, which spec, which loop, what cost vs budget.
- Get a herdr notification with sound when the human is needed (escalation, a non-transient
  failed run) or something finished (merge).
- Capture every persona run's full event stream to disk while it runs, with the verdict, cost
  and budget contract unchanged.
- Read any run, live or after the fact, as a readable transcript: `nw campaign tail`. Works
  without herdr.
- Optionally, one herdr pane per active run running that viewer, with herdr's agent state
  reflecting the campaign state.
- Never fail or slow a nightwright or queue operation because of herdr.

**Non-goals**
- Editing the human's herdr config (`~/.config/herdr/config.toml`) or Claude settings. We ship
  a suggested snippet; the human pastes it.
- Hosting/starting the conductor or stewards inside herdr automatically (`conductor-launch
  --in herdr`). The human starts them in herdr panes; we detect that. A launcher can come
  later as its own idea if wanted.
- Persisting Claude Code sessions for persona runs (`--no-session-persistence` stays).
- Consuming herdr's `events.subscribe` API (nightwright pushes; it does not need to read
  herdr).
- Per-run difficulty features from the stream (IDEA-423) — the stream enables them; using them
  is IDEA-423's job.
- OSC 7501 emission now (child 4 is deferred).

## Decision

Build four children in dependency order:

1. **herdr reporter** — a separate, opt-in follower process `nw herdr watch` that reads queue
   state (the same data as `conduct-status` / `queue list`) and the event stream with its own
   cursor (`--for herdr`), and pushes display tokens and notifications to herdr. It is the
   only process that calls `herdr`; the queue loop, the runner and every other `nw` command
   never do. Plus a one-line hardening in `runproc.child_env`: persona children get no
   `HERDR_*` variables.
2. **stream-json capture + `nw campaign tail`** — persona runs switch to
   `--output-format stream-json --verbose`; the wrapper writes the stream live to
   `runs/<run>/stream.jsonl` and, when the child exits, writes the final `result` event to
   `result.json`, so everything downstream of `result.json` is unchanged. A tail viewer renders
   it, live (`--follow`) or post hoc.
3. **herdr run panes** — the reporter, when opted in, opens one herdr tab per active run in an
   `nw` workspace, runs `nw campaign tail --follow` in it, reports the campaign state with
   `pane report-agent`, and closes it when the run ends.
4. **OSC 7501 program status** — *deferred*: emit queue > campaign > run status records from
   the viewer / `queue watch` for terminals that support it. Parked until herdr (or the
   human's terminal) implements it.

## Architecture / cross-cutting design

Invariants every child must respect:

1. **herdr is optional and inert by default.** A herdr call is made only when all hold:
   `HERDR_ENV=1` in the calling process, `herdr` is on `PATH`, and the human started a
   herdr feature explicitly (`nw herdr watch`; run panes additionally need
   `--run-panes` or `agentic.herdr.run_panes: true`). Otherwise every herdr code path is a
   no-op that does not even spawn a process. Child 2 does not depend on herdr at all.
2. **One herdr client, best-effort.** All calls go through one module,
   `src/nightwright/agentic/herdr.py`, with one function `call(args) -> bool`: argv list (no
   shell), `timeout=2s`, stdout/stderr captured and discarded, any exception or non-zero exit
   → `False`, never raised. A circuit breaker: after 5 consecutive failures the client stops
   calling for 60 s and logs one line to stderr. Nothing in the queue loop, the runner or
   `runproc` imports this module. That is the structural guarantee that herdr can neither
   fail nor slow the queue.
3. **Explicit targets, own source.** Every `report-metadata` / `report-agent` names an explicit
   pane or workspace id and uses `--source nw` (run panes: `--source nw-run`). Nothing reads
   `HERDR_PANE_ID` of the current process as a target except to *record* where a conductor or
   steward lives (below).
4. **Expiry instead of cleanup.** Every metadata push sets `--ttl-ms` = 3 × the push interval
   (default interval 10 s → 30 s TTL), and the watcher re-pushes every interval with an
   increasing `--seq`. If the watcher dies, stale values vanish on their own. A clean exit
   additionally clears its tokens.
5. **Persona children never see `HERDR_*`.** `runproc.child_env` drops every `HERDR_*` key
   (alongside the SPEC-0319 launcher keys), so herdr's Claude hook inside a headless run is
   inert and cannot overwrite the conductor's pane.
6. **Where the conductor and stewards live.** `conductor_cost.record_session` already runs in
   every follower (`nw queue events --follow --for conductor`, `nw mailbox follow --to
   steward:P`), and those followers are children of the Claude session in the pane. When
   `HERDR_ENV=1` it additionally stores `herdr: {pane_id, workspace_id}` from the env in the
   record. The watcher targets the pane of the newest record per reader that has one;
   `--conductor-pane ID` overrides. No herdr call is made to discover panes.
7. **Token names** (stable; the docs snippet uses them), all prefixed `nw_` to avoid clashes
   with other integrations:
   - workspace: `$nw_queue` (`3 active · 2 queued · 1 parked`), `$nw_attn` (open escalations +
     undecided parks, empty when 0), `$nw_cost` (today's queue spend).
   - conductor pane: `$nw_role` (`conductor`), `$nw_attn`, `$nw_cost` (conductor session cost,
     SPEC-0299 `conductor-cost`).
   - steward pane: `$nw_role` (`steward:P`), `$nw_spec` (the project's active campaign spec, or
     `N active`), `$nw_loop` (state + loop/round), `$nw_cost` (`$1.20/$5.00`, campaign spend vs
     cap).
   - run pane (child 3): `$nw_spec`, `$nw_loop` (persona + state), `$nw_cost` (run cost so far
     vs the run's `--max-budget-usd`).
8. **Notifications** come from events, once per event id (the watcher's cursor): `escalation`
   → `--sound request`; `run_failed` with `transient: false` → `--sound request`; `merged` →
   `--sound done`; `parked` with a conductor-owned reason → `--sound request`. Everything else
   is silent (the sidebar shows it).
9. **The run directory contract** (`nw.run.v1`, SPEC-0192) keeps `result.json` as the raw final
   result object and `run.json`'s `files` map gains `"stream": "stream.jsonl"` when a stream
   exists. Whether a run streams is fixed at launch, recorded in `launch.json` (`"stream":
   true`); adoption and `collect` follow the run's own record, never the current code's
   default. That is what makes child 2 safe for in-flight campaigns.
10. **Docs.** `docs/herdr.md`: what the integration does, how to start `nw herdr watch` in a
    herdr pane, and a suggested `config.toml` snippet (`[ui.sidebar.agents.rows_by_agent]`,
    `[ui.sidebar.spaces] rows` with the `$nw_*` tokens, an optional `tab_bar_right` command
    entry). Never written to the human's config by nightwright.

## Breakdown / sub-specs

- [x] herdr reporter ([SPEC-0361](./0361-herdr-reporter-nw-herdr-watch-pushes-queue-tokens.md), done 2026-11-01): nw herdr watch pushes queue tokens and notifications to herdr panes (project: Meta-Tools) — `agentic/herdr.py` best-effort client (2 s timeout, circuit breaker), `nw herdr watch [--interval] [--conductor-pane] [--once]` follower with `--for herdr` cursor, workspace + conductor/steward pane tokens with `--source nw --ttl-ms --seq`, notifications per invariant 8, herdr pane/workspace ids recorded in the session registry, `HERDR_*` stripped in `runproc.child_env`, `docs/herdr.md` with the suggested config snippet
- [x] Stream-json capture of persona runs and nw campaign tail viewer ([SPEC-0362](./0362-stream-json-capture-of-persona-runs-and-nw.md), done 2026-11-01) (project: Meta-Tools) — `build_argv` uses `--output-format stream-json --verbose` (keeps `--no-session-persistence`), wrapper writes `runs/<run>/stream.jsonl` live and the final result event to `result.json`, `launch.json` `stream: true`, `parse_result` accepts JSONL, `collect` falls back to the stream when `result.json` is missing; `nw campaign tail CAMPAIGN [--run RUN] [--follow] [--raw]` renders tool calls, results, assistant text, estimated cost vs budget; works without herdr; in-flight runs unaffected
- [x] herdr pane per active run running the tail viewer ([SPEC-0364](./0364-herdr-run-panes-one-pane-per-active-campaign-in.md), done 2026-11-01; one pane per active campaign in an `nw runs` tab, display agent instead of run tokens) (project: Meta-Tools) — opt-in (`nw herdr watch --run-panes` / `agentic.herdr.run_panes`), one tab per active run in an `nw` workspace running `nw campaign tail --follow`, `pane report-agent --source nw-run` state (running=working, parked/escalated=blocked, queued=idle), run tokens, pane closed after the run ends (depends_on child 2 and child 1)
- [ ] herdr watcher auto-start ([SPEC-0365](./0365-herdr-watcher-auto-start-the-conductor-follower.md)) (project: Meta-Tools) — the conductor follower supervises `nw herdr watch` in the background: on by default inside herdr (`agentic.herdr.auto_watch`), one instance (herdr flock + `herdr_watch.json` with start ticks), output only to `herdr_watch.log`, stops with the follower, exits when its parent dies, restarts with backoff, restarted on upgrade via SPEC-0231; `nw herdr status`
- [ ] OSC 7501 program-status emission from nw (project: Meta-Tools) — DEFERRED: emit queue > campaign > run status records from `nw campaign tail --follow` / `nw queue watch`; do not start until herdr or the human's terminal implements OSC 7501

Sequencing / dependencies: child 1 first. It is the thin slice the human asked for, needs no
runner change and is useful alone. Child 2 is independent of child 1 and may run in parallel
with it; it is the riskier change (every persona run). Child 3 needs both: the viewer (child 2)
to put in the pane and the herdr client + watcher (child 1) to drive panes. Child 4 is deferred
and is not part of this epic's completion: when the epic closes, child 4 is either done or
`rejected`/superseded with a pointer to a fresh idea.

## Acceptance criteria

- [ ] With `nw herdr watch` running in a herdr pane, the herdr sidebar (with the docs snippet
      applied) shows the `$nw_queue` workspace tokens and per-pane conductor/steward tokens, and
      they update within one push interval of a queue change.
- [ ] An escalation, a non-transient failed run and a merge each produce exactly one herdr
      notification with the sound per invariant 8.
- [ ] Killing the watcher makes all `nw` tokens disappear within the TTL; no herdr state set
      by nightwright outlives it.
- [ ] Without `HERDR_ENV=1` or without `herdr` on `PATH`, no `herdr` process is ever spawned by
      any `nw` command, and every herdr-facing command exits 0 with a one-line explanation.
- [ ] A hung or failing `herdr` binary never delays a queue tick or a persona run (the queue,
      runner and `runproc` do not import the herdr module; the watcher's calls time out at 2 s).
- [ ] A persona run started from inside a herdr pane does not change that pane's herdr agent
      state (no `HERDR_*` in the child env).
- [ ] Every new persona run has `runs/<run>/stream.jsonl` growing while it runs; its
      `result.json`, `run.json` verdict, `cost_usd` and budget handling are identical to a
      `--output-format json` run of the same output.
- [ ] Runs started before the upgrade (no `stream` in `launch.json`) are adopted and collected
      exactly as before.
- [ ] `nw campaign tail CAMPAIGN --follow` shows a running run's tool calls and text as they
      happen and stops when the run ends; without `--follow` it renders a finished run.
- [ ] With run panes enabled, each active run gets one herdr pane showing its tail, herdr's
      agent state for it follows the campaign state, and the pane closes after the run ends.
- [ ] `docs/herdr.md` documents the integration and the suggested config snippet;
      nightwright never writes the human's herdr config.

## Test plan

- **Integration/e2e tests:** a fake `herdr` executable on `PATH` (a script that appends its argv
  as one JSON line to a log file; variants that hang past the timeout and that exit 1) plus the
  existing `FakeRunner`/fake `claude` used by the queue tests.
  - `tests/test_herdr_integration.py`: with `HERDR_ENV=1` and the fake binary, drive a queue
    campaign through admitted → run_failed (non-transient) → escalation → merged with the fake
    runner while `nw herdr watch --once` / a short-interval watcher runs; assert the logged
    argv: workspace and pane tokens with `--source nw`, `--ttl-ms`, increasing `--seq`, explicit
    pane ids taken from the session registry, one `notification show` per event with the right
    `--sound`. Same run with the hanging variant: the queue tick duration is unchanged (within
    the test wait budget) and the watcher logs the circuit breaker. Without `HERDR_ENV` or with
    no `herdr` on `PATH`: the log file is never created.
  - Persona env: a fake `claude` that dumps its env; assert no `HERDR_*` key when the runner is
    started with `HERDR_*` set.
  - Stream capture: a fake `claude` that emits a stream-json transcript (system, assistant,
    tool_use, tool_result, result) with pauses; assert `stream.jsonl` grows before exit,
    `result.json` equals the final result event, and `run.json` matches the snapshot of the
    same output in `json` mode. An adoption test with a pre-upgrade run dir (`launch.json`
    without `stream`) collects as before.
  - Tail: `nw campaign tail` on a recorded `stream.jsonl` golden file (post hoc), and
    `--follow` on a growing file ends when `exit.json` appears (waits via `tests/waits.py`).
  - Run panes: with the fake herdr, one active run → `tab create` + `pane run … nw campaign tail
    --follow` + `report-agent --state working`; park it → `--state blocked`; finish → `pane
    close`.
- **Manual verification:** in a real herdr 0.9.x session, apply the docs snippet, start the
  conductor and one steward in herdr panes and `nw herdr watch --run-panes` in a third; queue a
  small spec on a test project; observe sidebar tokens, a run pane appearing with a live tail,
  the state icon changing on park/resume, a notification with sound on merge; kill the watcher
  and see the tokens expire. Note the result in each child spec's As shipped.

## Rollout / sequencing

Child 1 lands first and is usable alone (opt-in: nothing happens until the human starts
`nw herdr watch`). Child 2 can land in parallel. It changes every new persona run, and the
`stream` flag in `launch.json` keeps runs that are already in flight on the old path, so it is
safe to merge while campaigns run. The queue runner re-execs on code change (codewatch), so
old and new runs coexist cleanly. Child 3 lands after both, opt-in. Child 4 stays deferred.
Partial delivery: child 1 alone gives the status display and notifications; child 2 alone
gives transcripts and the viewer in any terminal.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this); child 4 may close as
      `rejected` with a pointer to a fresh idea if OSC 7501 is still unavailable.
- [ ] Integration test plan executed; `uv run pytest` green.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

- None. Decided here: reporter is a separate watcher process, not the queue loop
  (Decision 1, invariant 2); pane discovery via the session registry (invariant 6); token
  names (invariant 7); notification policy (invariant 8); in-flight safety via `launch.json`
  (invariant 9); conductor/steward hosting in herdr stays manual (Non-goals).
