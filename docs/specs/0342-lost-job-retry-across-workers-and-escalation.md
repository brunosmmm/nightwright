---
id: SPEC-0342
title: "Lost-job retry across workers and escalation"
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, reliability, conductor]
depends_on: [SPEC-0341]
---

## Context

[SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md) detects a lost job or
worker, appends `worker_job_lost` and runs the gate locally. With more than one worker
registered, a better answer is to try another worker first. A worker that keeps losing jobs
should also reach the human.

This is the third part of the split of
[SPEC-0332](./0332-worker-owned-async-jobs.md).

## Goals / Non-goals

**Goals**
- A lost job is retried with a new attempt on another eligible worker, and runs locally once
  the attempts are used up.
- Several workers are used at once.
- A worker that loses jobs repeatedly raises one escalation to the human through the
  conductor.

**Non-goals**
- Load-aware scheduling. "First free slot in registry order" is enough at this scale
  (affinity comes with SPEC-0345).
- Persona jobs (SPEC-0344).
- Bootstrap and drain (SPEC-0343).

## Decision

On give-up (SPEC-0341), the step writes a new record with `attempt + 1`, adds the lost worker
to `excluded_workers`, and acquires a slot from the remaining pools (local last). After
`agentic.workers.max_remote_attempts` (default 2) remote attempts, the job runs locally.
Losses are counted per worker in a durable file; three `worker_job_lost` for one worker
within 3600 s raise one campaign-less `escalation` with options `off` and `keep`, and the
queue tick carries out the answer.

## Design

**Config.** `agentic.workers.max_remote_attempts` is read by `remote_jobs.settings(cfg)`
(which already reads `agentic.workers`). Absent, it is 2. It must be an integer of at least 1
(`bool` excluded); any other value raises `ValueError` naming the key when the gate step reads
it, and the gate step reports that as a config error instead of silently using the default.
With 1, a lost job runs locally with no remote retry.

**Retry.**
1. Giving up appends `worker_job_lost {worker, job_id, attempt, reason}` (SPEC-0341), sends
   a best-effort `cancel`, deletes the attempt's `gates/<seq>-*` files and records the loss
   (see *Loss window*). It then retries with `attempt + 1`, where `excluded_workers` is the
   lost record's `excluded_workers` plus the lost worker.
2. The retry releases the current worker's slot and calls
   `gate_slots.acquire(cfg, cid, pools=[p for p in workers.gate_pools(cfg) if p[0] not in
   excluded_workers])` (the remote-eligibility checks of `remote_gates.pools_for` still apply
   first; an ineligible run is local, as today). That list still ends with the local pool
   `(None, agentic.gates.slots)`, and `acquire` takes the first free slot in order, so a retry
   whose eligible workers are all full runs locally at once. A local run is not a remote
   attempt, but it ends the job's attempts: no remote retry follows it.
3. When `attempt` would exceed `max_remote_attempts`, or no remote pool is left after the
   exclusion, the retry runs locally without calling the remote pools.
   `worker_job_retried {job_id, from_worker, to_worker, attempt}` (a new event in
   `EVENT_TYPES`) is appended for every new attempt, including the local fallback: `job_id` is
   the new attempt's job id (`<cid>-gate-<seq>-a<attempt>`), `from_worker` the lost worker,
   `attempt` the new attempt's number, and `to_worker` the worker that took the slot, or
   `null` when the new attempt runs locally.
4. The `gates/` files of an abandoned attempt are deleted before the next attempt writes, as
   in SPEC-0338's fallback.
5. `job_id` carries the attempt (`...-a<attempt>`), one record per attempt, same `seq`. The
   lost record is set to `lost` before the next attempt's record is written, so the
   `(campaign, seq)` lookup (`remote_jobs.live_gate`, SPEC-0341) returns the single non-final
   record with the highest attempt. On the retry path the lost record stays `lost` (it is set
   to `local` only when the job falls back locally, as SPEC-0341 does today). **Crash between
   records:** if the coordinator dies after the lost record is set to `lost` but before the
   next attempt's record is written, the next tick's gate step finds no live record. Before
   starting attempt 1 it reads the campaign's newest gate record (highest seq, then attempt;
   `tier: loop`): when that record is `lost` and its `commit` equals the commit under test,
   the step continues the chain instead of starting over: same `seq`, `attempt` = its attempt
   + 1, `excluded_workers` = its `excluded_workers` plus its `worker`, and steps 2-3 apply
   (including the `worker_job_retried` event). A `lost` record never has a successor attempt,
   so it is exactly the crash window; any other newest record (or a different commit) starts
   a fresh attempt 1.
6. Adoption (SPEC-0341) of a record whose attempt is above 1 respects its
   `excluded_workers`; `remote_jobs.create` takes `excluded_workers` as a new keyword
   (default `[]`) so each attempt's record carries its exclusions.

**Several workers.** The slot pools of SPEC-0337 already cover N workers. The only change is
that `excluded_workers` removes a worker from one job's pool list.

**Loss window.** Losses are recorded in `<agentic_dir>/worker-losses.json`
(`{"schema": 1, "workers": {W: {"losses": [iso, ...], "escalated_at": iso | null,
"escalation": "ESC-id" | null}}}`), written atomically (temp file + rename) under the lock
file `worker-losses.json.lock`, in the same give-up step that appends `worker_job_lost`. The
file is shared by all campaigns, so losses from different campaigns count together, and it
survives a coordinator restart (SPEC-0231). Recording a loss for W, under the lock:
1. append the loss time, then drop entries older than 3600 s before now (a sliding window);
2. if three or more remain, and `escalated_at` is null or more than 3600 s old, and no open
   escalation for W exists (`escalations.list_all(cfg, status="open")` holding a record with
   `kind: worker_lost` and fact `worker=W`), raise the escalation (below) and set
   `escalated_at` to now and `escalation` to its id.
So at most one escalation per worker per 3600 s, counted from the moment it is raised; while
it is open, no further escalation is raised for W however many losses follow. Once it is
answered (either option) and 3600 s have passed since `escalated_at`, three losses inside a
sliding hour raise a new one. A `keep` answer does not reset the losses. A failure to raise
(for example a mailbox error) leaves `escalated_at` null, appends a `note` to the losing
campaign, and the next loss tries again; it never fails the gate step.

**Events and escalation.** `worker_job_retried` joins `EVENT_TYPES` (`worker_job_lost` is
SPEC-0341's). Both are informational for the conductor. The escalation is raised by the gate
step through
`escalations.plan_escalate(cfg, None, question="Worker <W> lost 3 jobs in the last hour.
Disable it?", options=["off", "keep"], recommend=1, facts=["worker=<W>", "<job_id>: <reason>",
...the last three losses], frm="conductor", kind="worker_lost")` followed by
`escalations.apply_escalate(cfg, plan)`. `queue` is not a mailbox address
(`mailbox.validate_address` knows `conductor`, `human` and `steward:<P>`), so `by` is
`conductor`, as for every other escalation. `plan_escalate`/`apply_escalate` gain an optional
`kind` (default `None`, stored on the record) so the record says what it is about. It is
campaign-less (`campaign=None`): `apply_escalate` writes the record and announces it (event,
mail to `human`, notification) but parks no campaign, and the retry or local run continues.

**Carrying out the answer.** `apply_answer` only records the answer. A new handler,
`workers.apply_answered_escalations(cfg)`, runs once per queue tick (`queue._tick`, beside the
other per-tick housekeeping). It scans `escalations.list_all(cfg, status="answered")` for
records with `kind: worker_lost` and no `applied_at`, takes W from the `worker=` fact, and
applies the chosen option: option 1 `off` sets W's `enabled: false` through `workers.update`
(the same path as `nw workers off W`); option 2 `keep` changes nothing and leaves the registry
file untouched (no write). If W is no longer registered (`nw workers rm`), or the answer is a
free-text one, the handler changes nothing and appends a global `note` event saying so. In
every case it then sets `applied_at` (and `applied: "off" | "keep" | "skipped"`) on the
record through the escalations lock, so the handler is idempotent: a record that already has
`applied_at` is skipped, and a crash between the registry write and `applied_at` only repeats
an idempotent `enabled: false`.

## Alternatives considered

- **Always fall back to local on the first loss.** Simple, but it ignores an idle second
  worker.
- **Unbounded retries.** A job that kills every worker would loop. Two remote attempts then
  local bounds the waste.
- **Wait for an eligible remote worker before running locally.** Keeps the remote attempt,
  but a busy fleet would stall a gate a free local slot could run now; the local pool stays
  last in the list instead.
- **Count losses in memory.** A coordinator restart would reset the count; the file is cheap.

## Acceptance criteria

- [x] Killing the worker's job process yields `lost`, then `worker_job_lost`, then a retry
      with `attempt + 1` on a second worker (when it has a free slot). After
      `max_remote_attempts`, the job runs locally.
- [x] The retry's `acquire` gets the pool list `gate_pools(cfg)` minus `excluded_workers`,
      ending with the local pool; with the eligible worker's slots all held, the retry runs
      locally at once and no further remote attempt follows.
- [x] `worker_job_retried` is appended for every new attempt with `job_id` the new attempt's
      id, `from_worker`, `attempt`, and `to_worker` the worker used, or `null` for the local
      fallback; the abandoned attempt's `gates/` files are deleted before the next attempt
      writes.
- [x] `agentic.workers.max_remote_attempts` defaults to 2. Set to 1, a lost job runs locally
      with no remote retry. A value below 1 (or not an integer) is a config error.
- [x] With two workers registered, concurrent campaigns use both.
- [x] A retry never picks a worker in `excluded_workers`; with all eligible workers
      excluded, the run is local.
- [x] A coordinator crash after attempt 1's record is set to `lost` but before attempt 2's
      record is written is recovered on the next tick: attempt 2 starts on the same `seq` with
      `excluded_workers` taken from the lost record.
- [x] Losses are stored in `<agentic_dir>/worker-losses.json` and survive a coordinator
      restart; three `worker_job_lost` for one worker within a sliding 3600 s (from any
      campaigns) raise one escalation with options `off` and `keep`, `kind: worker_lost`,
      `campaign: null`, `by: conductor`; no campaign is parked; a fourth loss while it is open,
      or within 3600 s of `escalated_at`, raises none.
- [x] Answering the worker escalation with `off` sets the worker's `enabled: false` in the
      registry on the next queue tick, and answering with `keep` leaves the registry
      byte-identical; either way the record gets `applied_at` and is not applied twice; a
      worker removed before the answer is applied leaves the registry unchanged and appends
      a `note`.
- [x] With no workers registered, behaviour is unchanged.

## Test plan

- **Automated tests** (`tests/test_worker_job_retry.py`, offline). They use SPEC-0338's
  loopback transport with two loopback workers (two temp data dirs):
  - **lost:** the job's pid is killed, which gives `worker_job_lost`, a retry on worker 2 and
    `worker_job_retried` with the attempt-2 job id and `to_worker` = worker 2; with both
    excluded, the run is local and `worker_job_retried` has `to_worker: null`;
  - **busy:** worker 2's slots are all held when worker 1's job is lost: the retry runs
    locally at once (`to_worker: null`) and no attempt 3 is made;
  - **pools:** the `pools` passed to `gate_slots.acquire` (spied) equal `gate_pools` minus
    the excluded workers, with the local pool last;
  - **max_remote_attempts:** the default (2) is honoured and the abandoned attempt's files
    are gone; with 1, a lost job runs locally with no remote retry; with 0, -1, `"2"` or
    `true`, the gate step reports a config error;
  - two campaigns against two workers place one job on each;
  - adoption of an attempt-2 record keeps `excluded_workers`;
  - **crash between records:** attempt 1's record is set `lost` and the step is killed before
    attempt 2 is written; the next tick finds no live record and starts attempt 2 on the same
    seq with `excluded_workers` taken from the lost record (and, with a different commit
    under test, starts attempt 1 instead);
  - **loss window:** three `worker_job_lost` for one worker in an hour, from two different
    campaigns, give one escalation (`kind: worker_lost`, `campaign: None`, `by: conductor`,
    no campaign parked); a fourth while it is open gives none; losses older than 3600 s are
    pruned; after the escalation is answered and `escalated_at` is more than 3600 s old (time
    injected), three new losses raise a second one;
  - **restart:** two losses are recorded, the coordinator state is dropped (new process /
    fresh module state reading the same data dir), and a third loss raises the escalation:
    the loss count persists;
  - **off:** answering `off` and running one queue tick sets the worker `enabled: false`; a
    second tick does not rewrite the registry (`applied_at` set);
  - **keep:** answering `keep` and running a tick leaves the registry file byte-identical;
  - **removed:** `nw workers rm W` before the tick: no registry change, a `note` is appended,
    and the record is marked applied (`skipped`).
- **Manual verification:**
  1. With two workers registered, reboot the first mid-gate. Expect `worker_job_lost`, then
     `worker_job_retried` to the second worker, and a completed gate.
- **Regression guard:** affected tests while iterating; SPEC-0341's and the escalations
  tests run unchanged (the new `kind` keyword defaults to `None`).

### Test record (2026-11-01)

- `uv run pytest -q tests/test_worker_job_retry.py`: 24 passed. It covers every automated case
  of the plan on two loopback workers (the real forced command `nw worker ssh-command`, each
  against its own data dir): lost (default limit and limit 3), busy, pools (spied `acquire`
  plus `retry_pools` directly), `max_remote_attempts` (settings unit test; 1 runs locally with
  no remote retry; 0, -1, `"2"` and `true` are a gate-step config error; a bad value with no
  workers changes nothing), two campaigns on two workers, adoption of a detached attempt-2
  record, crash between records (same tree continues on seq 1 as attempt 2 on b2; another
  commit starts attempt 1 on seq 2), the loss window (two campaigns, open blocks a fourth,
  pruning, a second escalation after the answer and an hour, a failed raise is a note and the
  next loss retries), restart (the third loss recorded by a fresh `python -c` process raises
  the escalation), and `off` / `keep` / removed through `queue.tick`.
- Regression guard: `tests/test_remote_job_records.py`, `test_remote_gates.py`,
  `test_gate_slots.py`, `test_workers_registry.py` (220 passed, `-n 4`), and
  `test_agentic_escalations*.py`, `test_agentic_events*.py`, `test_agentic_quiet.py`,
  `test_agentic_gates.py`, `test_gate_tiers.py`, `test_cli_gate_slots.py`,
  `test_agentic_conduct*.py`, `test_agentic_conductor_brief.py`, `test_tui_escalations_model.py`,
  `test_tui_events_model.py`, `test_workers_bootstrap.py`, `test_agentic_queue_visibility.py`
  (487 passed). SPEC-0341's and the escalation tests run unchanged. In one serial `-x` run,
  `test_remote_job_records.py::test_submit_failure_after_accept_still_has_a_record` failed once
  (it checks for the cancel call right after the record turns `local`, and `_failed` writes
  `local` before it cancels). That path is not changed here, and the test passed in the
  re-run. The full suite is left to gating.
- Manual step 1 (two real workers, reboot the first mid-gate) was run on the loopback instead:
  the job process is killed, as the `lost` test does. This host has one registered worker
  (`box`), and it does not list the `async` feature (see SPEC-0341's test record), so the step
  on real machines needs a second, upgraded worker. That is left to the human and to SPEC-0346's
  integration suite.
- `python3 tools/spec_lint.py` passes after `--write-ledger`.

### Test record (2026-11-01, gate fix)

- The `pytest-affected` gate failed at collection on a worker without the optional `tui`
  extra: `test_tui_campaign_screen.py` imports `test_tui_queue_tab`, which imports
  `nightwright.tui.queue_tab` (textual) before the `needs_textual` pytestmark can apply. This
  was not caused by SPEC-0342's code. `pytest.importorskip("textual")` now runs right after
  `import pytest` in every test module that imports a textual-dependent module at module
  level: `test_tui_queue_tab.py`, `test_tui_campaign_screen.py`, `test_tui_escalations_tab.py`,
  `test_tui_events_tab.py` and `test_tui_mailbox_tab.py`. The `needs_textual` marks stay.
- In a throwaway venv without textual (`UV_PROJECT_ENVIRONMENT=<tmp> uv run --no-extra tui`),
  those five modules ran as "5 skipped" and `pytest --co` collected all 5470 tests with no
  error. With textual installed, those five modules plus `tests/test_worker_job_retry.py` gave
  158 passed (`-n 4`). The full suite is left to gating.
- Second gate fix: the gate worker has no global git identity, so test repos failed `git commit`
  with exit 128. `tests/conftest.py` gains an autouse `_git_identity` fixture that sets
  `GIT_AUTHOR_*`/`GIT_COMMITTER_*` ("nightwright test" / "nightwright@test"). The identity env
  overrode the repo's `user.*` config, which changed the shas in `test_remote_gates.py`'s golden
  record, so that module's `_git` helper now drops those four variables. The run without `-x`
  also showed a real SPEC-0342 miss: `note` and `worker_job_retried` are `hold` in
  `quiet.RULES` but were missing from `skills/nw-conduct/SKILL.md`'s "Always held" list
  (`test_skills.py::test_conduct_skill_quiet_rules_match_code`). Both are now listed. Under the
  worker's conditions (`HOME`/`XDG_CONFIG_HOME` temp dirs, `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_CONFIG_NOSYSTEM=1`), drain_signal + adopt + worker_job_retry gave 67 passed (`-n 2`). The
  affected selection is the full suite once conftest.py changes. At `-n 8` it gave 5591 passed,
  7 skipped and 6 failed: the 4 above, now fixed (remote_gates + skills: 172 passed under the
  same conditions), and 2 timing tests (`test_heartbeat_spans_many_ticks`,
  `test_acquire_waits_for_whichever_pool_frees_first`) that pass when rerun alone.
- Third gate fix: `test_tui_queue_model.py`'s two `queue_tab` tests
  (`test_summary_text_for_each_action`, `test_plans_carry_the_keys_the_summary_reads`) now
  `pytest.importorskip("textual")` in-function; the rest of that module runs without textual.
  An audit of every test importing `nightwright.tui.*`/textual found no other unguarded import.
  The full suite in a venv without textual, under the worker's git conditions, at `-n 8`: 5377
  passed, 96 skipped, no textual import error, 2 failed under load that pass alone (3 reruns):
  `test_reexec_drains_inflight_close` and `test_submit_failure_after_accept_still_has_a_record`.
  The latter asserted the cancel call with no wait after the record turned `local`; it now
  waits with `until` (SPEC-0269). `test_remote_job_records.py` + `test_tui_queue_model.py` +
  `test_test_waits.py`: 80 passed.

### Test record (2026-11-01, review fixes)

- Three review findings were fixed in the code (see *As shipped*): a retry's transport-failure
  fallback now sets the lost record `local`; `worker_job_retried` for a remote attempt is written
  only once the job is about to be sent; an adoption whose worker was removed records no loss.
- `tests/test_worker_job_retry.py` gains 8 tests: a free-text answer is `skipped` with one
  `note` and no registry write; a stop while the retry waits for a slot (b2 and both local
  slots held) leaves the record `lost` with no event, and the next step continues as attempt 2
  on b2; a stop after b2's slot is taken but before submit appends no event (the continuation
  appends the only one); `events.sync` projects `worker_job_retried` with
  `job_id`/`from_worker`/`to_worker`/`attempt`; `retry_pools` with a removed or disabled
  `--worker` is local; a retry whose `prepare` fails sets the lost record `local` and appends
  `to_worker: null`, and with a crash during that local run the next step starts fresh with no
  attempt 2; adopting three records of a removed worker records no loss and raises no escalation.
  32 passed (`-n 4`). With the `src/` change reverted, the four tests of the logic fixes fail
  and the others pass.
- Regression guard: `test_remote_job_records.py`, `test_remote_gates.py`, `test_gate_slots.py`,
  `test_workers_registry.py`, `test_agentic_gates.py`, `test_gate_tiers.py`,
  `test_agentic_events.py`: 355 passed (`-n 4`). The full suite is left to gating.

### As shipped (2026-11-01)

- **Where the retry lives.** `remote_gates._give_up` no longer sets the record `local`. It
  leaves it `lost`, records the loss (`workers.record_loss`, through `_record_loss`, which turns
  any failure into a campaign `note`) and returns it as `Outcome.lost`. `gates.run_all` hands
  it to the new `gates._retry`, which loops attempt by attempt. A local run sets the lost
  record `local` first and appends `worker_job_retried` with `to_worker: null`. A remote
  attempt appends the event (`remote_gates.retried`) and calls `remote_gates.run(...,
  attempt=, excluded=)`. The pool list comes from `remote_gates.retry_pools`: `pools_for`
  without the excluded workers, or None when no remote pool is left. A plain transport failure
  of a retry falls back locally as SPEC-0338 does, with no further retry. A stop while a retry
  waits for a slot leaves the record `lost`, so the next gate step continues the chain through
  the crash path.
- **Config check.** `remote_jobs.settings` raises for a bad `max_remote_attempts`. `run_all`
  reads it before dispatch whenever a remote pool, an adoption or a lost chain is involved, so
  the error reaches `_do_gate`'s `res.error` (`config error: agentic.workers.max_remote_attempts
  ...`) before any job is submitted. The gate step then parks with its usual detail. The park
  does not use `config_error`, because the `config_invalid` auto-resume only re-checks the
  project config and the profile, and would resume and park again on every tick. With no
  worker registered, the key is never read.
- **"The commit under test" on the crash path** is checked as `remote_gates.adoptable` checks
  adoption: the record's `head` and `tree` must equal the worktree's. Comparing the raw
  `commit` would not work, because a dirty tree's `git stash create` commit is new on every
  call. The newest record comes from the new `remote_jobs.newest_gate` (loop tier). The chain
  is also not continued when `_gate_committed` says the state already holds that seq's gate.
- **Loss file.** Each worker's entry also keeps `recent`, its last three
  `{at, job_id, reason}`, which feed the escalation's facts (`losses` stays a list of ISO
  times, as designed). The question uses the actual count in the window, so it reads
  "lost 3 jobs" at the threshold.
- **Queue-wide note.** `note` (`{text}`) and `worker_job_retried` were added to
  `events.EVENT_TYPES`. Both are `hold` in the quiet table. `worker_job_retried` is also in
  `schemas.EVENT_KEYS` and projected by `events.sync`, as `worker_job_lost` is.
  `queue._apply_worker_escalations` calls the handler in `_tick` next to
  `_settle_ended_remote_jobs`. An unreadable registry leaves the record unapplied until the
  next tick.
- **Review fixes.** `remote_gates.run` takes `on_attempt`, which is called once the job is
  about to be sent (after the attempt's record is written for an async worker, after the last
  stop check for a `gate-job` one). `_retry` appends `worker_job_retried` there, so a stop
  before the send leaves no event and the next step's continuation writes the only one. A
  transport failure of a retry sets the lost record `local` before the local fallback (and
  appends `to_worker: null` when nothing was sent), so a crash during that run does not continue
  the chain. `_give_up` takes `count`; `adopt` passes `count=False` when the record's worker is
  no longer registered, so no loss (and no escalation) is recorded against a removed worker.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. Nothing to migrate. With one worker registered, the retry has no second pool and the job
   runs locally, as after SPEC-0341.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0332:
- A job gets 2 remote attempts before it runs locally.
- The escalation threshold is three losses for one worker in 3600 s.
