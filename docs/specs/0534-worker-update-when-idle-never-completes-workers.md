---
id: SPEC-0534
title: "Reconcile ghost remote jobs so a --when-idle worker update finishes"
status: proposed
owner: user
created: 2026-11-10
kind: feature
source_idea: IDEA-1162
tags: [agentic, remote, workers]
---

## Context

`nw workers update NAME --when-idle` ([SPEC-0386](./0386-queue-scheduled-worker-update.md))
drains the worker and updates it on the first queue tick where `workers._drained` is true.
`_drained` (`src/nightwright/agentic/workers.py`) is true only when no non-final
remote-job record names that worker and no gate slot of its pool is held. It reads the
coordinator's `remote-jobs/` records (`remote_jobs.live`) and never asks the worker.

A record left `attached` after the worker has already lost the job therefore counts as
busy for the whole drain. The queue waits `agentic.workers.update_drain_timeout_h`
(default 6 hours), then `workers.end_update` returns the worker to service with
`update_last.state: timeout` and does not install anything. The same predicate blocks
`nw workers drain` (`finish_drains`). The observed case is
`IDEA-1015-20261108-faf2-gate-015-a1`, `attached` since 2026-11-09 20:42, while `box`
reported no jobs. The human has to `nw workers update NAME --force` on every such update.

`nw workers list` only prints a job count, and only while the worker is draining, so the
stuck record's id is invisible there.

The worker already answers this question. `nw worker jobs --json` is one JSON list of
`{job_id, state, ...}` (`worker_job.jobs`). `nw worker poll JOB --from N` (without
`--renew`) ends in a `status` line whose `state` is `unknown` when the job dir is gone
(`worker_job.poll`, exit 1). `remote_gates.poll` is the wrong probe: it always passes
`--renew`, which would extend a lease the coordinator is only classifying.
`remote_gates._call` parses stdout as JSON objects per line, so it cannot see the
indented JSON list that `jobs --json` prints.

## Goals / Non-goals

**Goals**

- A draining worker's live remote-job record that the worker confirms is gone is marked
  `lost` on that tick, with a runner-journal line, so `_drained` can become true and the
  scheduled update runs. The drain does not sit until `update_drain_timeout_h` and then
  return the worker un-updated.
- When the worker cannot be asked, a live record older than the drain timeout plus a
  grace is marked `lost` the same way, and only while that worker is draining.
- A record the worker reports as still running stays live and still holds the drain.
- `nw workers list` shows those stale live records, including while the worker is not
  draining.
- `nw workers drain` uses the same reconcile, so a ghost record does not block disable.

**Non-goals**

- This spec leaves `--force` exclusive with `--when-idle`. The update path still refuses
  to install over a job lock the worker holds.
- Reconcile does not call `remote_gates._give_up`. It does not cancel the job, delete
  `gates/<seq>-*` files, append `worker_job_lost`, record a loss window, set a cooldown,
  or raise an escalation.
- Pull workers are not probed over SSH (`remote_gates._call` already refuses them). Only
  the age rule applies to a draining pull worker.
- Held gate slots stay a reason `_drained` is false. Reconcile does not release a slot.
- No new queue event and no change to `EVENT_TYPES` / quiet classes. The journal line is
  the record of the transition.
- `agentic.workers.update_drain_timeout_h` still returns a draining update to service
  when a job the worker still reports as running outlasts it.

## Decision

On every queue tick, before `finish_drains` and `due_updates` decide idleness, the
coordinator reconciles live remote-job records of workers that are draining.

A record is marked `lost` with `lost_reason: "reconciled"` only when the worker's
`jobs --json` and a non-renewing `poll` agree the job is gone, or (no answer from the
worker) the record's age exceeds `update_drain_timeout_h` plus
`update_drain_grace_h`. A job the worker reports as `accepted`, `running`, `done`, or
`error` is never marked lost. `done` and `error` do not occupy the worker for
`_drained`, because the worker's own update path treats only a held `job.lock` as live.
The mark is `remote_jobs.update` plus one `queue.journal` line
`worker_job_reconciled`. The existing lost-record retry on a later gate step
([SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md)) is unchanged.

`nw workers list` reads local records only and prints every live record whose last
observation is older than `worker_ttl_s`.

## Design

### What "gone" means

`workers.reconcile_draining(cfg, names)` runs at the start of `finish_drains` and
`due_updates`, after the existing `_check_job_records` pre-check, for the draining
names those functions already selected. An unreadable registry or `remote-jobs/` file
still aborts the pass with `OSError` / `ValueError` and disables nothing. A failed SSH
probe does not raise and does not count as an empty job list.

For each of those names, load the normalized worker. Skip a pull worker (age rule
only). Skip every live record whose `campaign` is in `remote_gates.attached`: that
attach loop already polls and gives up. One SSH worker per tick, budget
`RECONCILE_BUDGET_S = 20` seconds:

1. `ssh … nw worker jobs --json`, timeout at most 15 s and at most the budget.
   Parse stdout as one JSON value. The answer counts only when the process exits 0
   and the value is a list. Each object with a string `job_id` and a string `state`
   is a report. A timeout, a non-zero exit, or any other body is **no answer** for
   that worker. It is never "zero jobs".
2. A reported state of `accepted` or `running` keeps the coordinator record. No poll.
3. A reported state of `done` or `error` keeps the record (adoption and `mark_done`
   stay with the campaign) and drops it from the busy set `_drained` consults on
   this tick. No poll.
4. Any other live record (id absent from the list, or state `lost` / `unknown` /
   anything else) is confirmed with `poll JOB --from 1073741824` and no `--renew`
   (the same `--from` as `remote_gates._poll_status`), using whatever budget remains.
   When less than 2 s remains, do not poll; that record is no answer this tick.
   Poll state `unknown` or `lost` agrees the job is gone. Poll state `accepted`,
   `running`, `done`, or `error` wins over `jobs`: keep the record, and treat
   `done` / `error` as not busy. Any other poll result is no answer.

Confirmed gone: `remote_jobs.update(cfg, job_id, state="lost", lost_reason="reconciled")`.
`remote_jobs.update` already refuses to move a `cancelled` record or to replace a
final state with a non-final one; a record that became final since the scan is left
as written. Then `queue.journal(cfg, "worker_job_reconciled", worker=name,
job_id=job_id, prior_state=<previous state>, reported=<jobs state or "absent">,
via="poll")`.

### Age backstop

`workers.update_drain_grace_h(cfg)` reads `agentic.workers.update_drain_grace_h`.
A positive finite number (bool excluded) is kept; anything else is `1.0`. The age
limit is `update_drain_timeout_h(cfg) + update_drain_grace_h(cfg)` hours.

A live record of a draining worker (SSH or pull) that this tick did not confirm as
`accepted` / `running` / `done` / `error`, and whose campaign is not
`remote_gates.attached`, is marked `lost` with `lost_reason: "reconciled"` when
`now - stamp` exceeds that limit. `stamp` is `last_seen_at`, else `submitted_at`.
An unparseable stamp does not age out (the record can still be marked lost by a
confirmed-gone probe). The journal line is the same, with `via: "age"` and
`reported: "no-answer"`. This runs inside the same `reconcile_draining` call, so
`due_updates` sees the record as gone before it classifies the worker as timed out.

A worker that is not draining is never aged out and never SSH-probed by this spec.
A long job the worker still reports as running is never aged out, including when
its `last_seen_at` is old because no attach loop is refreshing it.

### Idleness

`_drained` stays "no slot held, and no live record that still occupies the worker".
After `reconcile_draining`, a live record occupies the worker unless this tick's
successful probe reported `done` or `error` for it. `due_updates` therefore returns
the worker in `idle` on the tick the ghost is cleared, and `_run_worker_updates`
starts `workers_update.perform` (never `--force`). The timeout branch
(`worker_update_timeout`, `worker_update_failed` with `reason: timeout`) runs only
when something still occupies the worker past `update_drain_timeout_h`.

`finish_drains` uses the same occupancy, then disables an idle draining worker that
has no `update_pending`, as it does today.

### `nw workers list`

`workers.stale_jobs(cfg)` groups `remote_jobs.live` by string `worker`. A record is
stale when `now - stamp` is greater than `remote_jobs.settings(cfg)["worker_ttl_s"]`
(`last_seen_at`, else `submitted_at`). An unparseable stamp is stale, with
`age_s: null`. Each entry is `{job_id, state, age_s}` sorted by `job_id`. This read
does not SSH.

`cli_workers.workers_list` adds `stale_jobs` to every `nw.workers.list.v1` row
(a list, empty when none). Existing keys are unchanged. The plain table keeps its
columns (the enabled cell and the capacity column stay as they are). After the
table, one line per stale entry:

`stale <worker> <job_id> <state> age <NhMm>`

with `age unknown` when `age_s` is null. No lines when nothing is stale. A
disabled worker with a stale record still gets the lines and the JSON list.

### Docs

The `--when-idle` help in `cli_workers.workers_update` and the `workers` row of
`docs/product/reference/index.md` state that a draining worker's live record is
reconciled against `nw worker jobs` / `poll`, that a confirmed-gone or
aged-unanswered record is marked `lost`, and that `nw workers list` shows stale
live records.

## Alternatives considered

- **Lower `update_drain_timeout_h`.** The timeout path returns the worker to
  service without installing. A shorter wait still skips the update, and a real
  long job loses its drain window sooner.
- **Make `--force` the idle-update path.** That installs while the worker holds a
  job lock, which `--when-idle` exists to avoid.
- **Call `_give_up` for the ghost.** That deletes the attempt's gate files, cancels,
  cools the worker down, and charges a loss. A record the worker has already
  dropped needs none of that. The campaign's next gate step still sees a `lost`
  record and retries under SPEC-0342 when the gate was not committed.
- **Age out every live record past the limit, including ones the worker reports
  as running.** A gate can outlive the drain budget. The age rule is only the
  no-answer backstop, and only while draining.
- **Probe from `nw workers list`.** List stays a local read. Stale rows come from
  record age; the mutation happens on the queue tick.

## Acceptance criteria

- [ ] A draining SSH worker with one live `attached` record, `nw worker jobs --json`
      exiting 0 with `[]`, and `poll` (no `--renew`) returning `unknown`, is marked
      `state: lost`, `lost_reason: "reconciled"`, on that tick. The runner journal
      gains one `worker_job_reconciled` line (`via: poll`, `reported: absent`).
      `due_updates` returns that worker as idle, and the timeout return-to-service
      does not run.
- [ ] The same tick starts the scheduled update only through `workers_update.perform`
      with force false. A second reconcile of that final record does not write
      another journal line and does not change `lost`.
- [ ] `jobs` reporting that job as `accepted` or `running` leaves the record
      non-final, does not journal, and `due_updates` does not treat the worker as
      idle. `poll` is not called for that job.
- [ ] `jobs` exiting non-zero, timing out, or printing a JSON object or invalid
      JSON is no answer: the record stays non-final when its age is inside the
      limit. An empty list is the only successful "zero jobs" answer, and it still
      needs an agreeing `poll` before the mark.
- [ ] When `jobs` omits the id and `poll` returns `accepted` or `running`, the
      record stays non-final. When `poll` returns `done` or `error`, the record
      stays non-final and does not occupy `_drained`.
- [ ] `jobs` reporting `done` or `error` does not mark the record `lost` and does
      not occupy `_drained`. A held slot on that worker still makes `_drained`
      false and keeps the worker out of `due_updates`' idle list.
- [ ] With no answer, a draining worker's live record older than
      `update_drain_timeout_h + update_drain_grace_h` is marked `lost`
      (`via: age`). A younger one is left live. A worker that is not draining is
      left live even when the record is older than that limit.
- [ ] A live record whose campaign `remote_gates.attached` reports true is not
      probed, not aged out, and still occupies the worker.
- [ ] A draining pull worker is not given an SSH `jobs` or `poll` argv. Its live
      record is marked `lost` only by the age rule.
- [ ] Reconcile does not call `_give_up`, does not set a cooldown, does not append
      a `worker_job_lost` campaign event, and does not delete `gates/` files.
- [ ] `update_drain_grace_h({})` is `1.0`. A positive number is kept. A bool,
      a non-number, or a non-positive number falls back to `1.0`.
- [ ] A confirmed-running job that is still running after `update_drain_timeout_h`
      still takes the existing timeout path: back in service, not updated, journal
      `worker_update_timeout`.
- [ ] `nw workers list --json` includes `stale_jobs` on every worker: a list of
      `{job_id, state, age_s}` for live records older than `worker_ttl_s` (or with
      an unparseable stamp, `age_s: null`). A record newer than `worker_ttl_s` is
      absent. The plain listing prints `stale <worker> <job_id> <state> age …`
      after the table, and prints no such line when nothing is stale. This listing
      does not SSH.
- [ ] `finish_drains` disables a draining worker (no `update_pending`) on the tick
      a ghost record is confirmed gone, and does not disable it while the worker
      reports the job `running`.
- [ ] `--when-idle` help and the `workers` row in `docs/product/reference/index.md`
      describe the reconcile and the stale lines.

## Test plan

- **Automated tests:** new `tests/test_worker_job_reconcile.py` with exactly one
  module-level speed-tier marker, `pytestmark = pytest.mark.fast`. The module stubs
  SSH (monkeypatch the reconcile probe) and does not import `remote_loopback`,
  `remote_support`, `remote_workers_golden`, `tui_harness`, or `tui_budget`, and
  does not call `run_test`. It asserts every acceptance criterion above: confirmed
  gone marks `lost` and journals once and yields `due_updates` idle; a running
  report keeps the record and skips `poll`; a bad `jobs` body is no answer; `poll`
  overrides an omission; `done` / `error` neither marks lost nor occupies
  `_drained`; a held slot still blocks; the age rule fires only past
  timeout+grace and only while draining; `remote_gates.attached` skips the record;
  a pull worker gets no SSH argv; `_give_up`, cooldown, `worker_job_lost`, and
  `gates/` deletes are not invoked; grace-config fallback; the existing timeout
  path still fires for a job the probe reports running; `workers list` / `--json`
  stale lines; `finish_drains` disables after a confirmed-gone record and not
  while the job is running. Fixtures are a tmp registry, a tmp `remote-jobs`
  record, and a stub probe. No worker box.
- **Tier check:** `python3 tools/test_tiers.py --check` exits 0. The new module
  carries `pytestmark = pytest.mark.fast` and no second marker.
  `tests/test_workers_drain.py` and `tests/test_workers_update_schedule.py` already
  have their one `pytestmark = pytest.mark.fast`; this spec does not add another.
- **Manual verification:** with a ticking runner and a draining `box` whose
  coordinator record is `attached` while `ssh box nw worker jobs --json` is `[]`,
  `nw workers list` prints a `stale box <job_id> attached age …` line before the
  tick, and the next tick journals `worker_job_reconciled` and then
  `worker_update_started` / `worker_updated` without waiting
  `update_drain_timeout_h` and without `--force`. `nw workers check box` shows
  the new build and `updating` clear.
- **Regression guard:** `uv run pytest -q tests/test_worker_job_reconcile.py
  tests/test_workers_update_schedule.py tests/test_workers_drain.py
  tests/test_workers_registry.py` passes. Those existing modules cover the
  timeout return, `finish_drains` on a genuinely idle worker, and `list --json`
  keys other than `stale_jobs`.

## Clock Log

Implementation sessions clock IDEA-1162 with `nw idea clock-in` and
`nw idea clock-out`. No implementation session is clocked yet.

## Rollout / migration

No migration and no rewrite of existing final records. The next queue tick after
this code loads reconciles draining workers. A ghost record confirmed gone, or
older than the timeout plus grace with no answer, becomes `lost` with
`lost_reason: reconciled` and one journal line. Records already `done`, `lost`,
`cancelled`, or `local` stay as they are. A job the worker still reports as
running keeps its record and the current timeout behavior.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. The grace default (1 hour), the 20 s per-worker probe budget, and marking
`lost` without `_give_up` are the decision above.
