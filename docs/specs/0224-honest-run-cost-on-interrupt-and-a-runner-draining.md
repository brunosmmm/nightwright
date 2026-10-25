---
id: SPEC-0224
title: "Honest run cost on interrupt, and a runner_draining signal to the conductor"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, queue, cost, watchdog, conductor]
source_idea: IDEA-100
depends_on: [SPEC-0194, SPEC-0204, SPEC-0213]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-100` ("Runner re-exec drain blocks admission for up to a full persona
run after every code merge"). This is the first of two specs for that bug. The durable fix,
persona runs that survive the runner's restart and are adopted by the new process, is
[SPEC-0225](./0225-detached-persona-runs-adopted-across-runner-restarts.md). This spec ships the
two smaller parts that the incident also exposed. They help on their own, before SPEC-0225
lands.

The incident, 2026-10-25 17:48. SPEC-0203 merged and touched `src/wt/agentic`. The runner
saw `code_changed` and entered the [SPEC-0213](./0213-nonblocking-queue-ticks.md) drain
(`queue._drain`), which joins every in-flight step before the
[SPEC-0194](./0194-runner-follows-merged-code-re-exec-between-ticks.md) exec. SPEC-0219's
implementer had been running for 8 minutes of its 30-minute timeout. SPEC-0206 and SPEC-0207
had been ready since the merge, with 2 slots free. They were not admitted, and no tick was
journaled. The human noticed first, not the conductor. The conductor then sent SIGTERM to the
runner. That interrupted the implementer, and the run was charged its full $4 budget, then
restarted. Admission followed 2 s later.

Two defects here are independent of the drain design:

1. **Cost on interrupt is silent and pessimistic.** `ClaudeRunner.run` uses
   `subprocess.run(... capture_output=True)`. A killed `claude -p --output-format json`
   prints no result object, so `parse_result` gives `cost_usd: None`.
   `queue._record_run` then charges `budget` (`charged = result.cost_usd if … else budget`).
   It emits the "run reported no cost; charged its budget" finding only when the run was
   **not** interrupted (`if result.cost_usd is None and not interrupted`). So the interrupted
   case, the one that happens on every stop, charges the maximum with no trace. Nothing in
   `run.end`, `run.json` or the displays tells the human that the number is a guess.
2. **No signal reaches the conductor during a drain.** The `ready_not_admitted` watchdog
   (SPEC-0212) reads the readiness the tick stores (`_record_backlog`). The tick that merged
   SPEC-0203 broke out of the loop for `code_changed` right after it ran, and `_drain` does
   not refresh readiness. So SPEC-0206 and SPEC-0207 were never stored as ready, and the
   watchdog stayed quiet. Nothing else describes a drain from outside the runner process.

## Goals / Non-goals

**Goals**
- A persona run's cost is the real one whenever the run's output carries it, including
  interrupted, timed-out and failed runs.
- When the real cost is unknown, the charge is marked **estimated** everywhere it is
  recorded or shown: ledger, run record, campaign cost, tick row. It is never charged
  silently.
- A drain longer than 60 s raises a `runner_draining` event on the conductor's stream, once
  per drain, saying why it is draining, what it waits on and how many items are ready.
- `ready_not_admitted` also works during a drain.

**Non-goals**
- Not waiting for persona runs during a drain. That is
  [SPEC-0225](./0225-detached-persona-runs-adopted-across-runner-restarts.md).
- Estimating cost from token usage and a price table (see Alternatives).
- Changing cost-cap semantics. Caps still count estimated charges at their upper bound.
- Changing the SIGTERM forwarding of SPEC-0213.

## Decision

1. **Cost.** `RunResult.cost_usd` stays the cost parsed from the run's stdout
   (`total_cost_usd`), whatever the exit status. `_record_run` charges it when it is present.
   When it is absent, `_record_run` charges `budget`, the upper bound, which keeps caps
   safe. It also marks the charge as estimated: `run.end.cost_estimated: true`, `cost` event
   `estimated: true`, a new campaign `cost.estimated_usd` running total, and always an
   `info` finding that names the reason (`interrupted`, `timed out`, `no cost reported`). The
   interrupted case no longer skips the finding.
2. **Drain signal.** At drain start the runner writes `draining: {reason, since, waiting_on}`
   into its lease info. The heartbeat thread (SPEC-0204) keeps that info on disk, and the
   field is removed when the drain ends, at release and on exec. The out-of-process watchdog
   (`events.watchdog_checks`) emits `runner_draining` once the drain is older than
   `drain_grace_s` (default 60). The drain loop also refreshes stored backlog readiness every
   `poll_interval_s`, so `ready_not_admitted` sees items that became ready.

## Design

### Cost (`runner.py`, `queue.py`, `runs`)

- `ClaudeRunner.run` does not change how it spawns or how it parses. `parse_result` already
  reads `total_cost_usd` from whatever stdout exists: a full JSON result, or one printed
  before a signal. This spec pins that the value is used even when `timed_out`, a negative
  exit code or `is_error` is set. A test covers a timeout whose partial stdout carries a
  result object.
- `_record_run(state, persona, run_id, result, budget, *, interrupted)`:
  - `estimated = result.cost_usd is None`; `charged = budget if estimated else
    result.cost_usd` (the same number as today);
  - `cost["estimated_usd"] += charged` when estimated (new key, created at 0.0, rounded to
    4 places like `total_usd`);
  - `run.end` gains `cost_estimated: bool`, and the `cost` event gains `estimated: bool`.
    Both are additive;
  - the finding is emitted whenever `estimated`, interrupted or not. Title: `run cost
    unknown; charged its budget as an estimate`. Detail: `$<budget> charged as an upper
    bound (<reason>)`. The reason is `interrupted` when `interrupted`, else `timed out` when
    `result.timed_out`, else `no cost reported`.
  - Choice: charge the budget rather than 0. A cap that undercounts can let a campaign
    overspend. An overcount only parks early, and a park says why. The `estimated` marks make
    the overcount visible, and the conductor's `cost_cap` row (raise the cap once) already
    handles an early park.
- `write_run_record` (`wt.run.v1`, SPEC-0192) adds `cost_estimated` (true when `cost_usd` is
  None). It is additive, and `schemas.RUN_SCHEMA` keeps its version.
- Report rows: `_apply` sets `row["cost_estimated"]`. `render.tick_row` prints
  `~$4.00 est.` (dim) instead of `$4.00` when it is set. `tick_row_text` does the same in
  plain text.
- Campaign displays: `queue_payload` / `dashboard_payload` items carry
  `cost.estimated_usd` (0.0 when absent). `wt campaign show` and `wt queue watch` show
  `(~$X est.)` after the total when it is above 0. Legacy states without the key read as 0.0.

### Drain signal (`queue.py`, `lease.py`, `events.py`)

- `_drain(cfg, pool, lease, *, reason)` gains `reason` (`code_changed` | `signal` | `once` |
  `max_ticks` | `idle` | `crash`, the same vocabulary as the `stop` record). On entry it sets
  `lease.info["draining"] = {"reason", "since": now_iso(), "waiting_on": [...]}` and calls
  `lease.heartbeat()` to persist it. `waiting_on` lists the in-flight flights as `{campaign,
  kind, run_ids}`, with kind `implement` / `triage` / `review` / `gate` / `close`. It is
  rewritten whenever a flight is harvested, so it shrinks as the drain progresses. On exit
  `draining` is popped and persisted.
- **The readiness refresh during the drain.** Every `poll_interval_s` of drain time, the
  main thread runs `backlog_readiness` + `_record_backlog` for every QUEUED item. It is the
  same code step 1 of `_tick` runs, without admitting. The main thread is otherwise idle
  waiting on futures. Readiness that a merge just flipped (as SPEC-0203 did for 0206/0207)
  is therefore stored with its real `since`.
- `Lease.handover` and `lease.acquire` (adopted path) drop `draining` from the info they
  carry over, so the new process never shows a drain that has ended.
  `lease.lease_status` exposes `draining` (or None) when the lease is held.
- `events.DEFAULTS["drain_grace_s"] = 60`. It can be overridden like the other watchdog
  settings.
- `watchdog_checks`: when `lease.held` and `lease.draining` is set and its age exceeds
  `drain_grace_s`, it appends `("runner_draining:<pid>:<since>", "runner_draining", None,
  data)` with `data = {reason, draining_s, waiting_on, ready, slots}`. `ready` is the number
  of QUEUED items whose live `queue.backlog_readiness` is ready. Computed live, so the count
  does not depend on the refresh above. The key includes `since`, so the event fires
  exactly once per drain, and the condition clears when the drain ends (the `_watch`
  bookkeeping).
- `EVENT_TYPES["runner_draining"] = ("reason", "draining_s", "waiting_on", "ready")`.
  `summary_line` needs no special case.
- `ready_not_admitted_items` rows gain `runner_draining: bool` from the lease. The conductor
  can then see that the item waits on a drain and not on a dead admission path.
- **Conductor skill.** `skills/wt-conduct/SKILL.md` gains a decision-table row for
  `runner_draining`: read `waiting_on`. If it lists persona runs and `ready` > 0, note it on
  the waiting items (`wt campaign note`), do not SIGTERM the runner (an interrupt now costs
  that run, honestly marked estimated), and re-check at the next event. If it lists only a
  gate or close, wait. If the drain lasts longer than the largest `timeout_s` among
  `waiting_on` + 5 min, treat it as `lease_stale`. No report to the human. The
  `ready_not_admitted` row gains: "`runner_draining: true` ⇒ handled by the
  `runner_draining` row".

## Alternatives considered

- **Estimate cost from token usage and a price table.** `--output-format json` has no usage
  either when the run is killed. Switching to `stream-json` gives per-message usage, but
  converting it to dollars needs a maintained per-model price table, and a wrong table is
  worse than an honest upper bound. Rejected.
- **Pro-rate the budget by elapsed time / timeout.** Arbitrary, since spend is not linear in
  wall time, and a cap could undercount. Rejected.
- **Charge 0 when unknown.** Caps would undercount. Rejected.
- **Send SIGINT first and wait for claude to print a result.** It is not documented that
  `claude -p` prints a cost on SIGINT. It would change SPEC-0213's forwarding for an unknown
  gain, and SPEC-0225 removes most interrupts anyway. Rejected for now. If a run does print a
  result on a signal, Decision 1 already uses it.
- **Have the runner append `runner_draining` to the event stream itself.** The stream is
  written only by `events.sync` (projector + watchdog), outside the loop, so that a hung loop
  is still observed. The lease field keeps that design. Rejected.

## Acceptance criteria

- [ ] A persona run that ends without a cost in its stdout (interrupted, timed out, or a
      normal exit with no `total_cost_usd`) is charged its budget. Its `run.end` has
      `cost_usd: null`, `cost_estimated: true`, `charged_usd` = budget. Its `cost` event has
      `estimated: true`. The campaign's `cost.estimated_usd` grows by the budget. An `info`
      finding naming the reason is recorded, including when the run was interrupted.
- [ ] A run whose stdout carries `total_cost_usd` is charged exactly that, even when timed out,
      interrupted or `is_error`, with `cost_estimated: false` and no estimate finding.
- [ ] `run.json` records `cost_estimated`. The tick row (`wt queue run` and `runner-log`)
      shows an estimated charge as `~$X est.`. `wt.queue.v1` / dashboard items carry
      `cost.estimated_usd`, and `wt campaign show` / `wt queue watch` show it when above 0.
      A legacy state without the key reads as 0.0.
- [ ] While `_drain` runs, the lease info carries `draining` with `reason`, `since` and a
      `waiting_on` list that shrinks as flights finish. It is gone after the drain, after
      release, and in the process that adopts the lease after an exec.
- [ ] `watchdog_checks` emits `runner_draining` (`reason`, `draining_s`, `waiting_on`,
      `ready`, `slots`) once a drain is older than `drain_grace_s` (default 60). It emits
      nothing before that or when no drain is recorded, and exactly one event per drain
      across repeated syncs.
- [ ] An item that becomes ready during a drain (its dependency merged by the tick that
      detected the code change) is stored ready by the drain's refresh, and
      `ready_not_admitted` fires for it after `ready_grace_s` with `runner_draining: true`.
- [ ] `skills/wt-conduct/SKILL.md` has the `runner_draining` row, and the
      `ready_not_admitted` row points to it.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_cost_estimate.py`, offline:
    - `test_interrupted_run_without_cost_is_estimated`: `_record_run` with
      `RunResult(cost_usd=None)` and `interrupted=True`. Asserts the `run.end` / `cost` keys,
      `cost.estimated_usd`, and the finding with reason `interrupted`.
    - `test_timeout_and_no_cost_reasons`: the same for `timed_out` and for a normal run
      with no cost.
    - `test_partial_stdout_cost_is_used_on_timeout`: monkeypatched `subprocess.run` raises
      `TimeoutExpired` whose `output` is a full result JSON with `total_cost_usd: 1.23`.
      The charge is 1.23, `cost_estimated` is false, and there is no finding.
    - `test_run_json_and_tick_row_mark_estimate`: `run.json` has `cost_estimated`, and
      `tick_row_text` contains `~$` and `est.`.
    - `test_queue_payload_estimated_usd_and_legacy`: payload carries the key, and a state
      without it gives 0.0.
    - The FakeRunner e2e loop with an entry `cost_usd: null` reaches the campaign cost
      display with the estimate.
  - `tests/test_agentic_drain_signal.py`:
    - `test_drain_writes_and_clears_lease_draining`: a FakeRunner `sleep_s` implementer is in
      flight. `run()` with a patched code change and patched `_EXEC`. While the drain waits
      (observed from a test thread), `read_lease()["draining"]` has reason `code_changed` and
      `waiting_on` names the campaign. After `_EXEC` is called, the lease file has no
      `draining`.
    - `test_watchdog_runner_draining_after_grace`: a lease file with `draining.since` 30 s
      ago gives no event, 90 s ago gives one `runner_draining` with `ready` counting a ready
      QUEUED item, and a second `sync` gives no duplicate. Clearing `draining` clears the
      condition.
    - `test_ready_not_admitted_fires_during_drain`: B depends on A. The tick merges A (fake
      close), then the code change is detected. During the drain (a held implementer of C),
      B's stored backlog flips to ready with `since` during the drain. With
      `ready_grace_s: 0`, `ready_not_admitted_items` lists B with `runner_draining: true`.
    - `test_handover_drops_draining`: `Lease.handover()` + adopt in a fresh `acquire` gives
      info without `draining`.
- **Existing suites unchanged:** `tests/test_agentic_runner.py`,
  `tests/test_agentic_reexec.py`, `tests/test_agentic_heartbeat.py`,
  `tests/test_agentic_events.py`, `tests/test_agentic_ping.py`,
  `tests/test_agentic_nonblocking.py`, `tests/test_agentic_e2e.py`.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** after the merge, on the next code-changing merge while a persona
  run is in flight, `wt queue events --tail 5` shows `runner_draining` about 60 s into the
  drain. If a run is interrupted, `wt campaign show <id>` shows the `est.` mark.

## Rollout / migration

No data migration. The new keys are additive, and legacy states and ledgers read as "not
estimated" / 0.0. The live runner re-execs into this code after the merge (SPEC-0194). That
drain is the last one without a `runner_draining` signal.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
