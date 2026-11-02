---
id: SPEC-0375
title: "nw queue restart: graceful runner restart with park/resume and a stale-code indicator"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, runner, queue]
depends_on: [SPEC-0171, SPEC-0194, SPEC-0229, SPEC-0291, SPEC-0341]
source_idea: IDEA-780
---

## Context

Restarting the runner today is `nw queue stop --yes` followed by `nw queue run` (or
`--detach`), typed by hand. `stop` (SPEC-0291) SIGTERMs the lease holder: persona runs are
detached and adopted by the next runner (SPEC-0225/0229, lossless), local gate steps are
killed and their result discarded (the campaign stays `GATING`, the next runner re-gates),
remote gate jobs keep running and are adopted when the worktree HEAD still matches
(SPEC-0341), and an `open`/`close` inside the close guard is waited for with `--wait-guard`.
The human has to remember the command line the runner was started with, has no way to let a
running gate finish first, and nothing says which campaigns were in flight across the
restart. The runner re-executes itself on code changes (SPEC-0194), but a runner started
with `--no-reexec`, or one wedged in a long drain, keeps running old code with no visible
sign in `nw queue watch`.

## Goals / Non-goals

**Goals**
- `nw queue restart` stops the runner and starts a new one with the same command line, cwd
  and environment, recording the in-flight campaigns across the restart.
- `--drain MINUTES` lets running gate steps finish before the stop.
- In-process campaigns (`OPENING`, `GATING`, `CLOSING`) are parked with reason `restart` and
  resumed to the interrupted step, in admission (priority) order, before the new runner can
  admit backlog into their slots.
- Crash-safe and idempotent: a plan file records the phase; `--continue` finishes it.
- Dry run by default (`--yes` applies), like every other queue write.
- `nw queue watch` shows one line when the runner's code is older than the checkout's.

**Non-goals**
- Restarting a runner on another host (refused, like `nw queue stop`).
- Parking agent-state campaigns: their persona runs are already adopted losslessly by the
  next runner, and parking them would make the adopter mark the run superseded and drop its
  result (see Alternatives).
- Cancelling remote gate jobs: they are left running and adopted on re-gate (SPEC-0341).

## Decision

A new module `nightwright.agentic.restart` plans and applies the restart as ordered,
idempotent phases recorded in `<agentic_dir>/restart.json`: `drain` → `stop` → `park` →
`resume` → `start` → `done`. The stop reuses `queue.stop_runner(wait_guard=True)`; the start
reuses `queue.detach_runner` (the new runner is always detached). Parks use the new reason
`restart` (owner `human`), carry `park.restart_id`, and are resumed by the restart itself,
before the new runner starts. The watch indicator compares the runner's `code_sha` (now in
the lease file, and in the journal `start` record) with the checkout HEAD, memoised so a
refresh costs at most a few `stat` calls.

## Design

**CLI** (`cli_queue.py`): `nw queue restart [--drain MINUTES] [--only CAMPAIGN]... [--timeout S]
[--continue] [--by ADDR] [--yes] [--json]`. Without `--yes` it prints the plan (schema
`nw.queue.restart.v1`) and writes nothing. `--json` emits the plan / result (plus `written`
and `error`; exit 1 on an error). `--continue` takes the recorded plan as is and refuses
`--drain` / `--only`. `nw queue runner-log` renders the journal `restart` record as
`⟲ restart <id>  runner <old> → <new>; parked N, resumed N`.

**Plan** (`restart.plan_restart(cfg, *, drain_min, only)`), read-only:
- the lease: held, pid, host; `queue.stop_refusal` applies (another host, a one-shot tick).
  The close guard is not a refusal: the stop waits for it (`wait_guard=True`);
- the old runner's command: `/proc/<pid>/cmdline`, `/proc/<pid>/cwd`, `/proc/<pid>/environ`.
  The command must contain `queue run`; `--detach`/`--log`, `--once`, `--max-ticks N` and
  `--until-idle` are stripped (a restart starts the long-lived loop). When there is no
  runner or `/proc` cannot be read, the command is `python -m nightwright queue run` in the
  current cwd and environment (`source: fallback`). `NW_RUNNER_LEASE_FD` and `NW_RUNNER_LOG`
  are dropped from the environment. The environment is never written to disk (it can hold
  credentials); the plan records only its source (`proc` / `current`);
- the log: the old lease's `log` (a `--detach` runner), else `<agentic_dir>/runner.log`.
  A foreground runner therefore comes back detached, logging there;
- the targets: every campaign in an active state (filtered by `--only`), in admission order
  (`queue.admission_key`), each with `action`: `park` for `OPENING`/`GATING`/`CLOSING`
  (in-process steps the stop ends or guards), `adopt` for the persona states (left to the
  existing detach/adopt path); `resume_to` = `queue._resume_target(state)`;
- refusal: an unfinished `restart.json` (phase not `done`) refuses a new plan; use
  `--continue`.

**Apply** (`restart.apply_restart(cfg, plan, *, by, timeout_s, start_timeout_s, popen, kill,
clock, sleep, poll_s, fail_after)`; the environment travels in the in-memory plan's private
`_env` key, which `public()` drops from every write and print). The
plan file (0600, written atomically) is written first with a fresh id `R-<UTC stamp>`, then
after each phase. Each phase is skipped when already in `done_phases`:
1. **drain** (only with `--drain N` and a held lease): poll the campaign states every 1 s
   (injectable sleep/clock) until none of the `park` targets that were `GATING` when the
   drain began is still `GATING`, or N minutes passed. The old runner keeps running
   meanwhile (it may admit backlog into a freed slot; resumed campaigns then wait in
   `WAITING`, ahead of the backlog). Records `drain: {waited_s, finished, pending}`.
2. **stop**: `queue.stop_runner(cfg, timeout_s, wait_guard=True)` — the SIGTERM path of
   `nw queue stop`. Skipped (and recorded) when nothing holds the lease.
3. **park**: refuses (plan kept, `--continue` later) when a runner holds the lease again.
   For each `park` target still in its planned in-process state:
   `queue._park(cfg, st, "restart", "nw queue restart <id>", resume_to, by=actor,
   count=False, extra={"restart_id": id, "from": state})` under the campaign lock. Remote
   gate jobs are not cancelled. A target that moved (now an agent state, parked, abandoned, …)
   or carries a human's `park_requested` (that park is the human's, applied by the next tick)
   is recorded as `skipped` with a `why`.
4. **resume**: for each campaign parked by this restart (`park.reason == "restart"` and
   `park.restart_id == id`), in plan order: clear the park, then
   `queue._enter_slot_or_wait(cfg, st, resume_to, "resumed", by=actor)` with a `resume`
   event noting the restart id. Counters are untouched (unlike `apply_resume`, which resets
   the timeout re-gate budget), escalations are left alone. A campaign that is no longer
   parked by this restart is skipped — resuming twice is a no-op.
5. **start**: when the lease is already held (a crash after the spawn, or a human), record
   that holder and do not start another. Else `queue.detach_runner(cfg, argv, log, env=…,
   cwd=…, popen=…)` (new `env`/`cwd` keyword arguments), which waits until the child holds
   the lease. With `--continue` after a crash the environment comes from the current
   process (`env_source: current`).
6. **done**: phase `done`; one runner-journal record `restart` (`id`, `phase`, `parked`,
   `resumed`, `old_pid`, `new_pid`).

Resuming before the start (instead of after the new runner holds the lease) is deliberate:
with no runner holding the lease nothing can admit backlog into the freed slots, so the
resumed campaigns get their slots back deterministically.

**Park reason** `restart` joins `PARK_REASONS`; `PARK_OWNERS["restart"] = "human"` (a
restart park only survives a crashed restart, which the human who ran it finishes with
`nw queue restart --continue`; the conductor is not asked). The operate page's park table
documents it.

**Runner `code_sha`.** `queue.run` resolves the checkout (`code_root` or `code_checkout()`)
even without `--reexec`, writes `code_sha` into the lease info (`lease.LOOP_KEYS` gains
`code_sha`) and the journal `start` record.

**Stale-code indicator.** `queue.runner_code_status(cfg, lease)` →
`{runner_sha, head_sha, stale, changed, pending_reexec}` or None (no runner / no checkout /
unknown sha). The runner sha is the lease's `code_sha`, else the newest journal `start`
record of the lease pid (bounded backwards read, memoised per pid + started_at). The
checkout HEAD is `git rev-parse HEAD`, memoised on a stat fingerprint of the git `HEAD`,
the branch ref and `packed-refs`; the code diff (`codewatch._changed_paths`) is memoised per
(runner sha, head). `dashboard_payload` adds `runner_code`; `_dash_header` adds one line
`runner code stale: <sha7> → <sha7> (N code files) · nw queue restart` (or `re-exec
pending` while the lease shows a `code_changed` drain). No new polling loop: it is computed
inside the existing refresh.

## Alternatives considered

- **Park every in-flight campaign, agents included (the idea's literal wording)** — a
  parked agent-state campaign has a human event after its run started, so the next runner's
  adopter marks the run `_superseded` and keeps the facts but not the transition: the agent's
  work is recorded but not applied. The existing detach/adopt path is lossless, so agent
  states are left to it.
- **Drain with deferred park requests** (`park_requested`) — the old runner applies them
  after the step, but `finish` computes `resume_to` from the next state, and a gate that
  failed (→ `TRIAGING`) resumes to `GATING`, re-running the gate it just waited for. Polling
  for the gate step to end loses nothing.
- **Resume after the new runner holds the lease** — its first tick can admit backlog into
  the slots the parked campaigns freed, which then queue behind it in `WAITING`.
- **Restart via the SPEC-0194 re-exec (signal the runner to exec itself)** — keeps the lease
  and needs no new process, but needs a new signal protocol in the running (old) code, so it
  cannot restart a runner that predates it — the case that motivates this command.
- **`git rev-parse` on every watch refresh** — a subprocess per refresh; the stat
  fingerprint makes the steady state free.

## Acceptance criteria

- [x] `nw queue restart` without `--yes` prints the plan (old pid, command, cwd, env source,
      log, targets with `park`/`adopt` and `resume_to`, drain) and changes nothing: no
      signal, no file, no state change.
- [x] With `--yes`: the runner holding the lease gets SIGTERM via `stop_runner`, in-process
      targets are parked with reason `restart` + `restart_id`, then resumed to their
      interrupted state in admission order before the new runner starts; agent-state
      campaigns are not parked; a new detached runner starts with the old argv (stripped of
      `--detach`/`--log`/`--once`/`--max-ticks`/`--until-idle`), cwd and environment (minus
      the lease-handover and log variables) and holds the lease.
- [x] `--drain N` waits for the `GATING` targets to leave `GATING` (or N minutes) before the
      stop, and records what finished and what was still pending.
- [x] `--only C` limits parking/resuming to C (repeatable); an unknown or terminal C is an
      error.
- [x] A restart interrupted after any phase leaves `restart.json` with its phase; a new
      plan is refused while it is unfinished; `--continue` completes it without parking,
      resuming or starting twice (a held lease at `start` is recorded, not duplicated).
- [x] Another host's runner and a one-shot tick are refused, as in `nw queue stop`.
- [x] `restart` is a park reason owned by the human and listed in the operate page.
- [x] The runner writes `code_sha` to its lease and journal `start` record (with or without
      re-exec); `nw queue watch` shows `runner code stale …` when nightwright code changed
      between that sha and the checkout HEAD, nothing when it did not, and a repeat refresh
      spawns no `git` process when nothing changed.

## Test plan

- **Automated tests** (`tests/test_agentic_queue_restart.py`, data dirs under pytest's
  basetemp; runners are fake lease-holding child processes, as in `test_agentic_detach.py`):
  - dry run: plan contents, nothing signalled/written, refusals (another host, tick,
    unfinished plan file);
  - argv capture/strip from a fake runner's `/proc` (real child process) and the fallback;
    environment filtering;
  - full apply against a fake runner: SIGTERM received, GATING/OPENING/CLOSING targets
    parked with `restart` then resumed to the right state in admission order, agent-state
    campaign untouched, ledger events, journal `restart` record, the new runner started
    with argv/cwd/env/log (recording popen that spawns a fake lease holder);
  - `--only` filtering and errors;
  - `--drain`: a GATING target that leaves GATING ends the wait early; one that never does
    hits the deadline (injected clock), both recorded;
  - crash-safety: an exception injected after `park` leaves the plan at that phase;
    `--continue` resumes and starts once; a held lease at `start` is recorded, not
    duplicated; a second `--continue` on a done plan is a no-op error;
  - CLI: `nw queue restart` dry run and `--json`;
  - `PARK_OWNERS`/`park_owner("restart")`; `test_park_owner_table` updated;
  - `runner_code_status`: stale vs fresh on a temp git checkout, pending re-exec, journal
    fallback, memoisation (no `git` call on a repeat with an unchanged fingerprint); the
    watch header shows / hides the line; `run` writes `code_sha` to the lease.
- **Manual verification:** none against the live runner (forbidden for the implementing
  agent). A human verifies with the steps in *Rollout*.
- **Regression guard:** the touched / nearest test files (affected-test selection is the
  full suite here, left to the close gate), `tests/test_docs_two_pillars.py` (park table), `tests/test_agentic_queue.py`,
  `tests/test_agentic_detach.py`; the full suite at the queue's close gate.

## Rollout / migration

No data migration. The old runner (without `code_sha` in its lease) is read through the
journal fallback; a runner whose start record has no sha shows no indicator. Human check:
`nw queue restart` (dry run) → read the plan → `nw queue restart --yes` → `nw queue watch`
shows a new runner pid and the in-flight campaigns back in their states;
`nw queue runner-log` shows the old runner's `stop`, the new one's `start`, then the
`⟲ restart` record. If the command fails mid-way, `nw queue restart --continue` (dry run)
then `--continue --yes`. Docs: the operate page's park table (`restart`) and the agentic
workflow guide (§4) mention the command.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual steps listed for the human.
- [x] No regressions (affected tests; full suite at the close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

- Code: `src/nightwright/agentic/restart.py` (new), `cli_queue.py` (`queue restart`, the
  watch line `_runner_code_line`, the runner-log `restart` line), `queue.py` (`restart` park
  reason/owner, `detach_runner(env=, cwd=)`, `code_sha` in the lease from `run`,
  `runner_code_status` + memo helpers, `dashboard_payload.runner_code`), `lease.py`
  (`LOOP_KEYS` += `code_sha`), `schemas.py` (journal kind `restart`).
- Tests: `tests/test_agentic_queue_restart.py` (20 tests) — all passing; plus
  `test_park_owner_table` updated and `tests/test_docs_two_pillars.py` (park table).
  `tools/affected_tests.py` selects the full suite for this change (101/231 test files,
  `queue.py` is widely imported), which is left to the queue's close gate; run instead:
  `tests/test_agentic_queue_restart.py`, `test_docs_two_pillars.py`,
  `test_agentic_queue.py::test_park_owner_table`, `test_agentic_detach.py`,
  `test_agentic_dashboard.py`, `test_agentic_lease.py`, `test_agentic_codewatch.py` —
  139 passed.
- Not verified: a restart of a real `nw queue run` process (the implementing agent was not
  allowed to signal the live runner). The real-process parts covered by tests are `/proc`
  capture of a real child, SIGTERM + lease release of a real child, and the detached start of
  a real child with the captured argv/cwd/env. The human's first real run is the check in
  *Rollout*.
- Follow-up (not in scope): the conductor skill still says `nw queue stop --yes` then
  `nw queue run --detach` for a runner restart; switching it to `nw queue restart --yes` is a
  skill edit for when this ships.

## Open questions

- None.
