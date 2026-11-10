---
id: SPEC-0534
title: "Reconcile a draining worker's confirmed-gone remote job"
status: done
owner: user
created: 2026-11-10
updated: 2026-11-10
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

The worker already answers this question. `nw worker jobs --json` is one JSON list of
`{job_id, state, ...}` (`worker_job.jobs`), indented by `report._dump_json`.
`nw worker poll JOB --from N` (without `--renew`) ends in a `status` line whose `state`
is `unknown` when the job dir is gone (`worker_job.poll`, exit 1). `remote_gates.poll`
is the wrong probe: it always passes `--renew`, which would extend a lease the
coordinator is only classifying. `remote_gates._call` parses stdout as JSON objects per
line, so it cannot see that indented jobs list. `remote_gates._lines` is not the jobs
parser.

Marking the record `lost` is the transition [SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md)
retries on a later gate step. A wrong mark can replace a finished record and start
another attempt of a job the worker still holds. This spec marks `lost` only when jobs
and a non-renewing poll agree the job is gone and a locked re-read still finds the
record in `remote_jobs.LIVE`.

## Goals / Non-goals

**Goals**

- On the `finish_drains` or `due_updates` call that selected a draining SSH worker, every
  such worker with an eligible live record is probed. A live record that jobs and poll
  agree is gone, and that the locked re-read still finds in `remote_jobs.LIVE`, is marked
  `lost` once, with one `worker_job_reconciled` line (`via: poll`), so `_drained` can
  become true on that tick.
- `due_updates` then starts `workers_update.perform` with force false. `finish_drains`
  disables an idle plain drain on that same tick. The drain does not sit until
  `update_drain_timeout_h` and then return the worker un-updated.
- A job the worker reports as `accepted` or `running` stays live and still holds the
  drain. A job it reports as `done` or `error` stays live and does not occupy `_drained`,
  including `finish_drains`' locked re-check.
- `--when-idle` help and the `workers` row of `docs/product/reference/index.md` describe
  this probe and the `lost` mark.

**Non-goals**

- Aging an unanswered live job is a separate spec. It depends on this spec's no-answer
  result. It adds `agentic.workers.update_drain_grace_h` (default 1; a positive finite
  number is kept; a bool, a non-number, a non-positive number, and a non-finite number
  fall back to 1). A live record is marked `lost` once, `via: age` and
  `reported: no-answer`, only when its worker is draining, its campaign is not
  `remote_gates.attached`, and this call's probe for that record was no answer (or the
  worker is pull and was not probed). The limit is
  `update_drain_timeout_h + update_drain_grace_h`, using `last_seen_at`, else
  `submitted_at`. An unparseable stamp does not age out. A worker that is not draining,
  a worker that call did not probe, and a job reported `accepted`, `running`, `done`, or
  `error` stay live. A job still reported running past `update_drain_timeout_h` still
  takes the existing timeout return-to-service path (`worker_update_timeout`, not
  updated). That spec is the only one that may mark an unaccepted submit (no
  `accepted_at`, cursor 0), and only while its worker is draining. Past
  `update_drain_timeout_h + update_drain_grace_h`, a poll that did not run is `via: age`
  and `reported: no-answer`, and that record gets one journal line. This spec writes no
  `via: age` line and does not add `update_drain_grace_h`.
- Showing stale live remote jobs in `nw workers list` is a separate spec. It is a local
  read only: no SSH and no mutation. `workers.stale_jobs` lists each live record whose
  `last_seen_at` (else `submitted_at`) is older than `remote_jobs.settings`
  `worker_ttl_s`, and each live record whose stamp does not parse (`age_s` null).
  `nw workers list --json` adds `stale_jobs`, a list of `{job_id, state, age_s}` sorted
  by `job_id`, on every `nw.workers.list.v1` row, including disabled workers. Existing
  keys stay. The plain table is unchanged and is followed by one line per stale entry,
  `stale <worker> <job_id> <state> age {h}h{m:02d}m`, or `age unknown`, and by no such
  line when nothing is stale. That spec mentions those lines on the `workers` row of
  `docs/product/reference/index.md`. This spec does not add `stale_jobs` and does not
  change the plain table.
- This spec leaves `--force` exclusive with `--when-idle`. The update path still refuses
  to install over a job lock the worker holds.
- Reconcile does not call `remote_gates._give_up`. It does not cancel the job, delete
  `gates/<seq>-*` files, append `worker_job_lost`, record a loss window, set a cooldown,
  or raise an escalation.
- Pull workers are not probed over SSH (`remote_gates._call` already refuses them).
  This spec does not mark their records.
- Held gate slots stay a reason `_drained` is false. Reconcile does not release a slot.
- No new queue event and no change to `EVENT_TYPES` / quiet classes. The journal line is
  the record of the transition.
- `agentic.workers.update_drain_timeout_h` still returns a draining update to service
  when a job the worker still reports as running outlasts it.

## Decision

On every queue tick, before `finish_drains` and `due_updates` decide idleness, the
coordinator reconciles eligible live remote-job records of the draining SSH workers
that call already selected. Every such worker is probed, serially, with its own 20
second budget. The command is `remote_gates._argv`: `nw worker jobs --json`, then a
non-renewing `poll` (exit 0 or 1, `--from 1073741824`) for ids the list does not confirm
as `accepted`, `running`, `done`, or `error`.

A record is marked `lost` with `lost_reason: reconciled` only when jobs and that poll
agree the job is gone and, under the job lock, the re-read is still in
`remote_jobs.LIVE` (`submitted`, `attached`, `detached`). The mark is one write in that
lock hold plus one `queue.journal` line `worker_job_reconciled` with `via: poll`. A
record jobs and poll agree is gone is marked once, via poll and reported `absent` (or
the jobs state), even when its age also exceeds the limit. There is no second line.

A job reported `accepted` or `running` is never marked lost. `done` and `error` stay
live and do not occupy the worker for `_drained`, including the locked re-check,
because the worker's own update path treats only a held `job.lock` as live. A record
with no `accepted_at` and cursor 0 is not confirmed gone. A bad jobs body, a poll that
did not run, and any other no-answer leave the record live. SPEC-0342's retry of a
record that is already `lost` is unchanged.

## Design

### Who is probed

`workers.reconcile_draining(cfg, names)` runs at the start of `finish_drains` and of
`due_updates`, after the existing `_check_job_records` pre-check, for the draining
names those functions already selected. `finish_drains` selects draining workers with
no `update_pending`. `due_updates` selects enabled workers with `update_pending`. Those
sets are disjoint. An unreadable registry or `remote-jobs/` file still aborts that
pass with `OSError` / `ValueError` and disables nothing. A failed SSH probe does not
raise and does not count as an empty job list.

A live record is eligible when its `worker` is that name, its `state` is in
`remote_jobs.LIVE`, `remote_gates.attached` is false for its `campaign`, and it is not
the unaccepted shape below. `remote_gates.attached` is the in-process attach-loop set.
An SSH persona follow never registers there, so a persona job stays eligible.

Skip a pull worker (`mode == "pull"`). Skip a worker with no eligible live record. Do
not build an SSH argv for either.

Every draining SSH worker this call selected that has an eligible live record is
probed, serially, one SSH worker at a time, in the order of `names`. Each SSH worker
has its own `RECONCILE_BUDGET_S = 20` seconds, measured from the start of that worker's
probe. The jobs timeout is at most 15s and at most the time left in that worker's
budget. A no-answer on a worker this call probed leaves that worker's records live. A
pull worker is not probed. A worker with no eligible live record is not probed. A
worker this call did not probe is left unchanged.

### What "gone" means

For one SSH worker, inside its own budget:

1. `remote_gates._argv(worker, "jobs", "--json")`, timeout at most 15 s and at most the
   time left. Parse the whole stdout as one JSON value. The answer counts only when the
   process exits 0 and the value is a list. Each object with a string `job_id` and a
   string `state` is a report. A timeout, a non-zero exit, or any other body is **no
   answer** for that worker. It is never "zero jobs".
2. A reported state of `accepted` or `running` keeps the coordinator record. No poll.
   The record still occupies the worker.
3. A reported state of `done` or `error` keeps the record (adoption and `mark_done`
   stay with the campaign) and drops that `job_id` from the busy set `_drained`
   consults on this call, including the locked re-check. No poll.
4. Any other eligible live record (id absent from the list, or a state other than
   `accepted` / `running` / `done` / `error`) is confirmed with
   `remote_gates._argv(worker, "poll", job_id, "--from", 1073741824)` and no `--renew`
   (`1073741824` is `1 << 30`, the same `--from` as `remote_gates._poll_status`), using
   whatever budget remains. When less than 2 s remains, do not poll; that record is no
   answer this tick. A poll answer counts when the process exits 0 or 1 and stdout
   contains a JSON object with `type` `status` and a string `state` (use the last such
   line). Exit 1 with state `unknown` agrees the job is gone. A timeout, an exit other
   than 0 or 1, or no status line is no answer. The jobs rule that any non-zero exit is
   no answer does not apply to poll. Poll state `unknown` or `lost` agrees the job is
   gone. Poll state `accepted`, `running`, `done`, or `error` wins over `jobs`: keep
   the record, and treat `done` / `error` as not busy. Any other poll result is no
   answer.

A live record with no `accepted_at` and cursor 0 is not confirmed gone. `poll` state
`unknown` on that shape is no answer (the same shape `remote_gates._gone` calls
`unaccepted`: `not accepted_at` and `int(cursor or 0) == 0`; a `cursor` that `int`
cannot parse is not this shape and does not abort the pass): the submit may still be
uploading. It is marked lost only by the age rule, and only while its worker is
draining. This spec does not implement that age rule, so the record stays `submitted`
and gains no journal line. It is not polled; the budget stays on eligible records. A
record with `accepted_at`, or with cursor greater than 0, including the attached ghost,
still uses jobs-and-poll agreement. SSH persona jobs are probed; they are not covered
by `remote_gates.attached`.

A bad jobs body is no answer and, in this spec, leaves the record live, at any age.
The same is true when the poll did not run, the poll result was neither agreement nor
`accepted` / `running` / `done` / `error`, or this call did not probe that worker.

### The write

Confirmed gone, and only then: under the job lock (`remote_jobs._lock`), re-read the
record. If `state` is outside `remote_jobs.LIVE` (`submitted`, `attached`, `detached`),
do not call `remote_jobs.update` and do not journal. Only a record that is still live
is set to `state: lost` and `lost_reason: reconciled`, in that same lock hold. Do not
rely on `remote_jobs.update` to keep `done`, `lost`, `local`, or `cancelled`: a final
state may replace another final state, and a cancelled record still accepts
`lost_reason`. The write does not call `remote_jobs.update`. That helper takes the lock
itself, and a second `flock` on `remote-jobs/.lock` from this process deadlocks; its
guard is also the one this paragraph refuses to trust. The two fields are merged and
written with the same atomic write `remote_jobs` already uses, still inside the hold.
A `read` that returns None is left alone. If the write raises, no journal line is
written for that record and the caller's existing `OSError` path skips disable and the
update pass.

After a successful write, one `queue.journal(cfg, "worker_job_reconciled", worker=name,
job_id=job_id, prior_state=<state just replaced>, reported=<jobs state or "absent">,
via="poll")`. `reported` is `absent` when the successful jobs list has no report for
that id, and the jobs `state` string otherwise. One record produces one line. A later
call sees `lost`, which is outside `LIVE`, and writes nothing.

`reconcile_draining` returns the set of `job_id`s this call's probe reported `done` or
`error` (from jobs, or from a poll that won). `_drained` treats a live record in that
set as not occupying. `finish_drains` and `due_updates` pass the same set into every
`_drained` call they make, including the re-check inside `finish_drains`' registry
lock. Other callers of `_drained` pass an empty set and keep today's meaning. The set
is not stored on disk.

### Idleness

`_drained` stays "no slot held, and no live record that still occupies the worker".
After `reconcile_draining`, a live record occupies the worker unless its id is in the
returned set. A record this call marked `lost` is final, so `remote_jobs.live` no
longer returns it. `due_updates` therefore returns the worker in `idle` on the tick the
ghost is cleared, and `_run_worker_updates` starts `workers_update.perform` (never
`--force`). The timeout branch (`worker_update_timeout`, `worker_update_failed` with
`reason: timeout`) runs only when something still occupies the worker past
`update_drain_timeout_h`.

`finish_drains` uses the same occupancy, then disables an idle draining worker that
has no `update_pending`, as it does today.

### Docs

The `--when-idle` help on `cli_workers.workers_update` and the `workers` row of
`docs/product/reference/index.md` state that, before a draining worker is treated as
idle, its eligible live records are reconciled against `nw worker jobs --json` and a
non-renewing `poll`, and that a job those two agree is gone is marked `lost`. They do
not describe an age mark or stale-job lines.

## Alternatives considered

- **Lower `update_drain_timeout_h`.** The timeout path returns the worker to service
  without installing. A shorter wait still skips the update, and a real long job loses
  its drain window sooner.
- **Make `--force` the idle-update path.** That installs while the worker holds a job
  lock, which `--when-idle` exists to avoid.
- **Call `_give_up` for the ghost.** That deletes the attempt's gate files, cancels,
  cools the worker down, and charges a loss. A record the worker has already dropped
  needs none of that. The campaign's next gate step still sees a `lost` record and
  retries under SPEC-0342 when the gate was not committed.
- **Trust `remote_jobs.update` to keep a finished record.** `lost` is final, so that
  helper replaces `done` and `local`, and on a `cancelled` record it still merges
  `lost_reason`. The re-read under the lock is what keeps those records.
- **Treat every non-zero poll exit as no answer.** The observed ghost is exit 1 with
  `state: unknown`. Copying the jobs rule would leave that record attached.
- **Mark every live id missing from `jobs --json`.** `remote_gates._run_async` and
  `remote_personas._fresh` write the live record before `_submit` returns. Until
  `accepted_at` is set and cursor is still 0, the worker has no job dir, so `jobs`
  is `[]` and poll is `unknown` while the upload can still be in flight
  (`slot_wait_s`, default 300 s, plus the bundle).
- **Probe one SSH worker per tick, or share one 20 s budget across workers.** The names
  selected for the call are the ones whose idleness is decided on this tick. A worker
  left unprobed would keep a ghost, and a shared budget would let the first worker's
  timeout skip the second.
- **Ship the age backstop and the `nw workers list` stale lines in this spec.** The age
  rule is a second transition that consumes this spec's no-answer result. The list is a
  local read with no write. They are the follow-on specs in Non-goals.

## Acceptance criteria

- [x] A draining SSH worker with one live `attached` record that has `accepted_at`,
      `nw worker jobs --json` exiting 0 with `[]`, and the poll parser given exit 1 and
      a final status line `{"type":"status","state":"unknown"}` (no `--renew`,
      `--from 1073741824`), is marked `state: lost`, `lost_reason: reconciled`, on that
      tick. The runner journal gains exactly one `worker_job_reconciled` line
      (`via: poll`, `reported: absent`), including when the record's age exceeds
      `update_drain_timeout_h`. There is no second line. `due_updates` returns that
      worker as idle, the timeout return-to-service does not run, and the scheduled
      update starts only through `workers_update.perform` with force false. A second
      reconcile of that `lost` record does not write another journal line and does not
      change `lost`.
- [x] When the successful jobs list reports that id as `lost` or `unknown` and poll
      agrees (`unknown` or `lost`, exit 0 or 1), the single line's `reported` is that
      jobs state and `via` is `poll`.
- [x] A record that is `done`, `local`, `lost`, or `cancelled` before the write is
      unchanged and produces no `worker_job_reconciled` line, including when the scan
      saw it live and it became final during the probe. A `cancelled` record does not
      gain `lost_reason`.
- [x] A draining worker's `submitted` record with no `accepted_at` and cursor 0, jobs
      `[]` and poll `unknown`, stays `submitted` and writes no journal line. The same
      answers mark an `attached` record that has `accepted_at` as `lost`.
- [x] Two draining SSH workers with old live records are both probed on the same
      `finish_drains` or `due_updates` call. Each has its own 20 s budget: the second
      is still probed when the first uses the full 20 s. The jobs timeout passed for
      each is at most 15 s and at most the time left in that worker's budget. The
      second worker's record stays live when its jobs report is `running`, and poll is
      not called for that job.
- [x] The poll parser treats exit 1 and a final status line
      `{"type":"status","state":"unknown"}` as `unknown`, and that pair plus jobs `[]`
      marks the accepted record `lost`. Exit 0 with state `lost` agrees the same way.
      Exit 2 or a timeout leaves the record live when its age is inside the limit and
      when its age is past `update_drain_timeout_h`. A poll with no status line is no
      answer and leaves the record live. The jobs rule that any non-zero exit is no
      answer does not apply to poll.
- [x] `jobs` reporting `accepted` or `running` leaves the record non-final, does not
      journal, does not call poll, and `due_updates` does not treat the worker as idle.
      `jobs` reporting `done` or `error` does not mark the record `lost`, does not call
      poll, and does not occupy `_drained` on the classification or on `finish_drains`'
      locked re-check. A held slot on that worker still makes `_drained` false.
      When `jobs` omits the id and poll returns `accepted` or `running`, the record
      stays non-final. When that poll returns `done` or `error`, the record stays
      non-final and does not occupy `_drained` on the locked re-check either.
- [x] Inside the age limit, an empty list without an agreeing poll leaves the record
      live and writes no journal line. The same is true past `update_drain_timeout_h`:
      this spec writes no `via: age` line. `jobs` exiting non-zero, timing out, or
      printing a JSON object or invalid JSON is no answer and leaves the record live
      at any age. When less than 2 s of that worker's budget remains, poll is not
      started and that record stays live.
- [x] A live record whose campaign `remote_gates.attached` reports true is not probed
      and still occupies the worker. A draining SSH worker whose only live records are
      that shape, or the unaccepted shape, is given no jobs argv and no poll argv. An
      SSH persona record (its campaign is not in `remote_gates.attached`) with
      `accepted_at` is probed, and jobs `[]` plus poll `unknown` marks it `lost`. A
      draining pull worker is given no SSH jobs or poll argv, and its live record stays
      as it is. A worker neither call selected is left unchanged.
- [x] Reconcile does not call `_give_up`, does not set a cooldown, does not append a
      `worker_job_lost` campaign event, and does not delete `gates/` files.
- [x] A job the probe reports `running` that is still running after
      `update_drain_timeout_h` still takes the existing timeout path: back in service,
      not updated, journal `worker_update_timeout`.
- [x] `finish_drains` disables a draining worker with no `update_pending` on the tick a
      ghost record is confirmed gone, and does not disable it while the worker reports
      the job `running`.
- [x] `--when-idle` help and the `workers` row in `docs/product/reference/index.md`
      state that a draining worker's eligible live records are reconciled against
      `nw worker jobs` and a non-renewing `poll`, and that agreement marks the record
      `lost`. They do not describe stale-job lines or an age mark.
- [x] An unreadable registry or `remote-jobs/` record still raises, and nothing is
      disabled. A failed SSH probe does not raise.

## Test plan

- **Automated tests:** new `tests/test_worker_job_reconcile.py` with exactly one
  module-level speed-tier marker, `pytestmark = pytest.mark.fast`. The module does not
  import `remote_loopback`, `remote_support`, `remote_workers_golden`, `tui_harness`,
  or `tui_budget`, and does not call `run_test`. It stubs SSH by feeding the reconcile
  parser a return code and stdout bytes (the subprocess call helper), not a pre-parsed
  poll state. Named tests:
  - `test_confirmed_gone_marks_lost_once` — the accepted attached ghost, jobs stdout
    `[]` with exit 0, poll stdout whose last line is
    `{"type":"status","state":"unknown"}` with exit 1, one `worker_job_reconciled`
    (`via: poll`, `reported: absent`) even when `last_seen_at` is older than
    `update_drain_timeout_h`, `due_updates` idle, `workers_update.perform` called with
    force false, no timeout journal; a second call leaves `lost` and adds no line.
  - `test_reread_leaves_final_record` — the probe stub flips the record to `done`,
    `local`, `lost`, or `cancelled` before it returns the gone answers; the record is
    unchanged, a `cancelled` record has no `lost_reason`, and there is no
    `worker_job_reconciled` line.
  - `test_unaccepted_submit_stays_submitted` — no `accepted_at`, cursor 0, jobs `[]`,
    poll `unknown`: stays `submitted`, no journal line; the same answers on an
    `attached` record with `accepted_at` mark `lost`.
  - `test_two_ssh_workers_both_probed` — one `finish_drains` or `due_updates` call
    probes both; the first probe consumes 20 s and the second still runs with its own
    budget; jobs timeout argument `<= 15` and `<=` time left; the second record stays
    live when jobs reports `running`, and that job's poll argv is absent.
  - `test_poll_exit_1_unknown_marks_lost` — the parser (return code plus stdout bytes,
    not an already-parsed `"unknown"`) maps exit 1 and that status line to `unknown`
    and the accepted record becomes `lost`; exit 0 with `"state":"lost"` does too;
    exit 2, a timeout, and stdout with no status line leave the record live both
    inside and past `update_drain_timeout_h`.
  The same module asserts the remaining criteria: `accepted` / `running` skip poll and
  block idle; `done` / `error` from jobs or from poll neither mark `lost` nor occupy
  `_drained`, including the locked re-check inside `finish_drains`; a held slot still
  blocks; an omitted id plus poll `accepted` / `running` stays non-final; a bad jobs
  body, an empty list without an agreeing poll, and a poll skipped because less than
  2 s remain leave the record live with no journal line at any age and never write
  `via: age`; `remote_gates.attached` skips the record and still occupies; a worker
  whose only live records are attached or unaccepted gets no SSH argv; an SSH persona
  record with `accepted_at` is marked `lost` on agreement; a pull worker gets no SSH
  argv and stays live; a worker the call did not select is unchanged; `_give_up`,
  cooldown, `worker_job_lost`, and `gates/` deletes are not invoked; the existing
  timeout path still fires for a job the probe reports `running`; `finish_drains`
  disables after a confirmed-gone record and not while the job is `running`; an
  unreadable registry raises and disables nothing, and a failed SSH probe does not
  raise; `--when-idle` help and the `workers` reference row describe the probe and the
  `lost` mark and do not describe stale lines or an age mark. `reported` is the jobs
  state when the list says `lost` or `unknown` and poll agrees. Fixtures are a tmp
  registry, a tmp `remote-jobs` record, and the stubbed probe. No worker box.
- **Tier check:** `python3 tools/test_tiers.py --check` exits 0. The new module
  carries `pytestmark = pytest.mark.fast` and no second marker.
  `tests/test_workers_drain.py`, `tests/test_workers_update_schedule.py`, and
  `tests/test_workers_registry.py` already have their one
  `pytestmark = pytest.mark.fast`; this spec does not add another.
- **Manual verification:** with a ticking runner, `nw workers update box --when-idle`,
  and a coordinator record `attached` with `accepted_at` set whose job dir is gone
  (`ssh box nw worker jobs --json` prints `[]`, and
  `ssh box nw worker poll <job_id> --from 1073741824` exits 1 with a status line
  `"state": "unknown"`), the next tick appends `worker_job_reconciled` (`via` poll)
  and then `worker_update_started` / `worker_updated` without waiting
  `update_drain_timeout_h` and without `--force`. `nw workers check box` shows the
  new build and `updating` clear.
- **Regression guard:** `uv run pytest -q tests/test_worker_job_reconcile.py
  tests/test_workers_update_schedule.py tests/test_workers_drain.py
  tests/test_workers_registry.py` passes. Those existing modules cover the timeout
  return, `finish_drains` on a genuinely idle worker, and `list --json` keys. This
  spec adds no `stale_jobs` key.

### Test record (2026-11-10)

- `uv run pytest -q tests/test_worker_job_reconcile.py tests/test_workers_update_schedule.py tests/test_workers_drain.py tests/test_workers_registry.py` — 140 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py` — exit 0; ledger regenerated.
- Full suite left to campaign gating.
- Manual verification on live `box` not run in this loop (no ticking runner in worktree).

### Test record (implementer loop 2, 2026-11-10)

- `uv run pytest -q tests/test_worker_job_reconcile.py tests/test_workers_update_schedule.py tests/test_workers_drain.py tests/test_workers_registry.py` — 148 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to campaign gating.
- Manual verification on live `box` not run in this loop.

### As shipped (implementer loop 2)

- `_mark_reconciled_lost` journals `prior_state` from the locked re-read, not the pre-probe scan.
- `ensure_record_ssh_dir` runs before each `jobs` and `poll` SSH argv; `OSError` aborts the pass.
- `reconcile_draining` accepts optional `progress` (threaded from `queue.tick` via
  `finish_drains` / `due_updates`) and stamps it during serial probes.
- Jobs SSH timeout is taken from `left()` after `ensure_record_ssh_dir`, not at worker entry.

## Clock Log

Implementation sessions clock IDEA-1162 with `nw idea clock-in` and
`nw idea clock-out`. Implementer loop 0 clocked in/out.

## Rollout / migration

No migration and no rewrite of existing final records. The next queue tick after
this code loads probes each selected draining SSH worker that has an eligible live
record. A ghost those probes agree is gone becomes `lost` with
`lost_reason: reconciled` and one `worker_job_reconciled` line (`via: poll`). Records
already `done`, `lost`, `cancelled`, or `local` stay as they are, including when they
become final during the probe. An unaccepted submit stays `submitted`. A no-answer
leaves the record live; aging it is the follow-on spec. A job the worker still reports
as running keeps its record and the current timeout behavior. `nw workers list` is
unchanged.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Each selected SSH worker's 20 s budget, marking `lost` only on jobs-and-poll
agreement after the locked re-read, and leaving no-answer records for the age-out
spec are the decision above.
