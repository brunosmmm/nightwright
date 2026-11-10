---
id: SPEC-0500
title: "Gate jobs: first-come-first-served tokens, full box when no campaign, per-project requester, longer default wait"
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, workers, cli]
depends_on: [SPEC-0381]
---

## Context

Standalone `nw gate run` jobs ([SPEC-0381](./0381-gate-run-standalone-remote-gating.md),
`src/nightwright/agentic/gate_jobs.py`) take a requester token, then a share token, then a
worker gate slot. Waiting jobs poll `_take_token` every second (`POLL_S`). A freed token goes
to whichever waiter polls next, not to the oldest one. SPEC-0381's Open questions call this
"flock luck".

Job records show the effect:

- 14 to 20 jobs from other projects were waiting at once. All were submitted by user `bruno`
  with `--wait-s 3600` or `7200`.
- Callers with a short wait starved and ended `unavailable` after 300 s (the default,
  `DEFAULT_WAIT_S`) or 600 s. These included the agents' verification gates for SPEC-0491 and
  SPEC-0492.
- Every requester resolves to `bruno`, so `max_per_requester` (config 5, code default 2) limits
  the whole user, not each project.
- The share cap defaults to `max(1, placeable worker slots // 2)`, which is 5 on the 10-slot
  box. The 5 slots kept for campaigns sit idle when no campaign is active.
- The `unavailable` message reads "no a gate-job token" and does not say why the job waited.

## Goals / Non-goals

**Goals**

- Waiting standalone jobs take tokens in arrival order. A dead waiter never blocks the line.
- Standalone jobs may use every placeable worker slot while no campaign or sentinel holds or
  waits for a gate slot. Campaigns keep priority.
- The requester cap applies per project by default.
- A configurable default wait (`agentic.gate_jobs.wait_s`, code default 1800 s).
- An `unavailable` message that says how many jobs were ahead and what the cap was.

**Non-goals**

- Preempting a running job. A job already running keeps its slot when a campaign arrives.
- Ordering among standalone jobs at the worker-slot stage. That stage stays `gate_slots.acquire`
  with the campaign-first `skip`. Share tokens never exceed the placeable slots, so it is not
  where the starvation came from.
- Changing the user's config file. Its explicit `max_slots` and `max_per_requester` still apply.
- Changing campaign or sentinel slot handling.

## Decision

1. **A line per token pool.** While a job waits for a token pool, it keeps a marker in
   `gatejobs/line/<job-id>.json`. Under the existing `gatejobs/.lock`, a job may take a token
   only when no older live waiter is in the line for the same pool. Older means a smaller
   `(created_epoch, id)`. Live means the job record says `waiting` and its pid is alive. Markers
   of dead or ended jobs are ignored and deleted.
2. **A dynamic share cap.** With no explicit `agentic.gate_jobs.max_slots`, the cap is every
   placeable worker slot while no campaign or sentinel holds or waits for a gate slot. Otherwise
   it is `max(1, slots // 2)`. The cap is re-read on every poll. It counts the share tokens held,
   so a lower cap stops new jobs until the number in flight drops below it.
3. **Per-project requester.** The default requester is `<user>@<project>` when the job has a
   project, else `<user>` as before. `--by` and `NW_MAILBOX_ADDR` still win.
4. **A configurable wait.** `agentic.gate_jobs.wait_s` (code default 1800 s) is the default of
   `nw gate run --wait-s`. An explicit `--wait-s` still wins. The `unavailable` message names the
   line position and the cap.

## Design

### The line (`gate_jobs.py`)

- `_line_dir(cfg) = gatejobs/line/`. A marker is `<id>.json`:
  `{id, pool, created_epoch}`. `pool` is the token directory name: `req-<hash>` for the
  requester pool, `share` for the share pool.
- `_wait_token(cfg, rec, d, cap, wait_ev, poll_s)` loops until the job has a token or
  the deadline or stop event fires. `cap` is a callable that returns the current cap. On each poll,
  under `_lock(cfg)`, it:
  1. writes or updates its own marker for pool `d.name`
  2. computes `ahead`: the markers for the same pool whose record is `waiting` with a live pid
     (`GS._pid_alive`) and whose `(created_epoch, id)` sorts before this job's. Markers whose
     record is missing, final, not `waiting` or has a dead pid are deleted on the way.
  3. tries `_take_token(d, cap())` only when `ahead` is 0.

  It returns `(fd, ahead, cap)`. `execute` removes the marker (under the lock) once the job
  holds both tokens, and again in its `finally`, so the line advances when a job takes its last
  token, ends or is cancelled.
- Order is `created_epoch` from the job record, with the id as a tie-break. The wait in each
  line starts when the job is created, so a job that waited longer for its requester token is
  also ahead in the share line.
- A job takes at most one token at a time. Requester and share lines are separate: a job held by
  its own requester cap does not block other requesters in the share line.
- The check-and-take is atomic under `gatejobs/.lock`. Processes running an older nw (no
  markers) can still win a token by flock luck until they are upgraded. This is accepted for the
  rollout window.
- `line(cfg, pool)` returns the live ids in order, for tests and diagnostics.

### Token cap that counts holders

`_take_token(d, n)` now counts the held tokens among every `slot-*.lock` in `d` (a non-blocking
flock probe, as `gate_slots._probe_held` does). It takes a free one only while fewer than `n` are
held. The first free `slot-k` for any `k` is taken, and new files are created up to `n`. So when
the cap falls from 10 to 5, holders at `k >= 5` still count, and no new job starts until fewer
than 5 are in flight.

### The share cap (`max_slots`)

```
explicit agentic.gate_jobs.max_slots  -> that value
else total = sum(slots of placeable workers)
     campaign_active(cfg) -> max(1, total // 2)
     otherwise            -> max(1, total)
```

`campaign_active(cfg)` is true when either:

- `campaign_waiting(cfg)` is true: a live waiter marker whose owner is not `GATEJOB-`
- a gate slot is held by an owner that is not `GATEJOB-`, on the local pool or any `w-*` pool
  dir (`gate_slots._probe_held`)

The sentinel counts as a campaign here.

The rule is evaluated only by the job at the head of the share line, once per poll. Campaigns
keep priority in two ways:

- the slot-stage `skip` predicate is unchanged, so a waiting campaign takes the next free worker
  slot before any job
- the cap drops back to half as soon as a campaign holds or waits, so jobs cannot refill the
  slots it needs

The cost: a campaign that arrives while jobs fill the box waits until one job finishes. This
was chosen over keeping half the box idle all the time. Callers whose gates must never wait
behind a job can set an explicit `max_slots`.

### Requester (`requester(by=None, project=None)`)

`by` or `$NW_MAILBOX_ADDR`, else `($USER or "cli")`, plus `@<project>` when `project` is set.
`nw gate run` passes the resolved target's project. The record field `requester` stays a string,
and the token directory is still `req-<sha256(requester)[:12]>`. So `nw gate list`/`status` and
the holder metadata are unchanged in shape.

### Wait (`agentic.gate_jobs.wait_s`)

- `DEFAULTS["wait_s"] = 1800.0` and `DEFAULT_WAIT_S = 1800.0`. A positive finite number in config
  overrides the default; a bad value (bool, non-number, `<= 0`, non-finite) falls back to it.
- `nw gate run --wait-s` defaults to unset. Unset means `settings(cfg)["wait_s"]`. The detached
  child still receives the resolved value in its `--wait-s`.

### `unavailable` messages

- A token wait that times out reads, for example:
  `box unavailable: no share token within 1800s: 3 jobs ahead in line (cap 5 standalone jobs box-wide)`.
- For the requester pool it reads:
  `... no requester token within 1800s: 1 job ahead in line (cap 2 per requester bruno@Meta-Tools)`.
- A slot wait that times out reads:
  `box unavailable: no worker gate slot within Ns (campaign gates go first)`.
- The old wording "no a gate-job token" is gone.

## Alternatives considered

- **Scan every job record for the line instead of markers.** Retention keeps up to 7 days of
  records. Reading all of them every second from every waiter costs far more than reading a
  marker dir that holds only the current waiters. The record is still the source of liveness.
- **A ticket file (a single FIFO file).** It would have to be repaired when a waiter dies.
  Markers whose record or pid is dead simply drop out.
- **Full box whenever the queue has no running campaign** (instead of no slot holder or waiter).
  A running campaign spends most of its time outside gates, so the box would rarely open up and
  the idle half would stay idle.
- **Preempt standalone jobs when a campaign arrives.** SPEC-0381 rules out preemption. A
  cancelled job would lose its run.
- **Per-project requester through config only.** Every agent would have to pass `--by`. The
  default should already be fair.

## Acceptance criteria

- [x] With several jobs waiting for the same token pool, tokens go out in `(created_epoch, id)`
      order whatever the order in which the waiters started polling (a deterministic test with
      three waiters started in reverse order).
- [x] A line marker whose job record has a dead pid, or is final, does not block a younger job,
      and the marker is deleted.
- [x] A job blocked by its requester cap does not block other requesters' jobs in the share line.
- [x] With no `max_slots` configured and no campaign holding or waiting, `max_slots(cfg)` equals
      the placeable worker slots. With a live campaign waiter marker, or a campaign holding a
      slot, it equals `max(1, slots // 2)`. An explicit `max_slots` overrides both.
- [x] `_take_token` counts held tokens: with cap `n` and `n` tokens held at any indices, no new
      token is handed out.
- [x] `requester(None, "P")` is `<USER>@P`. `--by` and `NW_MAILBOX_ADDR` win over it. Without a
      project it is the old value. `nw gate run` records the per-project requester.
- [x] `agentic.gate_jobs.wait_s` sets the default wait. Bad values fall back to 1800. `--wait-s`
      overrides it, and the detached child gets the resolved value.
- [x] A token wait that times out says how many jobs were ahead and the cap. No message contains
      "no a ".
- [x] The existing `tests/test_gate_jobs.py` behaviour (caps, campaign priority, cancel,
      idempotence) still passes.

## Test plan

- **Automated tests** (`tests/test_gate_jobs.py`, using the existing `gw` fixture, waits via
  `tests/waits.py`, never a fixed sleep):
  - `test_share_line_is_first_come_first_served`: `max_slots: 1`. The test holds the share
    token, creates jobs A, B and C, and starts their `execute` threads in reverse order. It waits
    until `line(cfg, "share") == [A, B, C]`, then releases the token. The stub's dispatch order
    is A, B, C.
  - `test_dead_or_final_waiter_never_blocks_the_line`: the test writes an older marker for a
    record with a dead pid, and one for a final record. A younger job runs at once, and both
    markers are gone.
  - `test_requester_cap_does_not_block_other_requesters`: `max_per_requester: 1`. An older job by
    alice waits behind alice's held token, and a younger job by bob still runs.
  - `test_max_slots_full_box_without_campaigns_half_with_one` (replaces
    `test_max_slots_defaults_to_half_the_worker_slots`): covers the full box with no campaign, half with
    a live campaign waiter marker, half while a campaign holds a worker slot, and the explicit
    override.
  - `test_take_token_counts_holders_at_any_index`.
  - `test_requester_defaults_to_user_at_project`: the `--by` and `NW_MAILBOX_ADDR` precedence,
    and the record's requester through `nw gate run`.
  - `test_settings_defaults_and_bad_values` (extended) covers the settings value and bad values.
  - `test_wait_s_setting_is_the_cli_default`: covers the CLI
    default, the config value and the override, through the detached child's argv.
  - `test_unavailable_message_names_line_and_cap`: the timeout message counts the jobs ahead and
    names the cap, and nothing reads "no a ".
  - The existing tests stay: `settings` defaults are updated for `wait_s`, and the token-timeout
    tests check the new wording.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` in the
  worktree, plus `tests/test_gate_jobs.py` and `tests/test_gate_slots.py`, and
  `python3 tools/spec_lint.py`.
- **Local verification temp dirs (requirement):** local verification runs for this spec either
  clean up their pytest temp dirs or use a base temp outside `/tmp` (`TMPDIR` or `--basetemp`
  on `/home`). `/tmp` is a 1M-inode tmpfs shared by every agent's pytest runs, and a full run
  leaves about 200k inodes in `/tmp/pytest-of-<user>/pytest-N`.
- **Manual verification:** none against the real box. The task rules forbid depending on the
  worker box or touching other users' jobs, and the line logic is local file and flock logic
  that the tests run for real.

## Rollout / migration

- This is additive. Old records have no line marker. An old-version process still waiting does
  not join the line and can win a token by flock luck until it ends.
- The user's config is untouched. An explicit `max_slots` or `max_per_requester` keeps its
  meaning. Because the requester now includes the project, `max_per_requester: 5` means 5 per
  project.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed or the skip explained.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

- Shipped as designed. `gate_jobs.py` gained `_line_dir`, `_line_scan`, `line`, `_leave_line`,
  `_wait_token`, `campaign_active`, a counting `_take_token`, the dynamic `max_slots`, the
  `wait_s` setting, `requester(by, project)` and `_ended_waiting(..., what, why)`.
  `cli_gate.py` resolves `--wait-s` from `settings(cfg)["wait_s"]` when unset and passes the
  target's project to `requester`.
- The sentinel's local-pool hold counts as a campaign (tested with `SENTINEL-<project>`); a
  standalone job's own slot hold does not; a dead campaign waiter marker does not.
- Test names as shipped: `test_max_slots_full_box_without_campaigns_half_with_one` replaced
  `test_max_slots_defaults_to_half_the_worker_slots`, and the wait test is
  `test_wait_s_setting_is_the_cli_default`; bad `wait_s` values are covered in the extended
  `test_settings_defaults_and_bad_values`.
- Docs: `docs/product/reference/index.md` lists `wait_s` and the new defaults;
  `skills/nw-orient/SKILL.md` notes the 1800 s default and arrival order.
- Verification (reduced scope): full affected run waived by the human on 2026-11-09; ran only
  `tests/test_gate_jobs.py` + `tests/test_gate_slots.py` under `timeout 300`, 60 passed in 5 s,
  plus `python3 tools/spec_lint.py` (exit 0). The new SPEC-0500 tests also passed 6 repeated
  runs under `-n 8`. No hanging test was found in the gate files (slowest 1.01 s).
- What happened to the affected run, for the record: the first run (160 files) ended
  `1 failed, 2088 passed, 430 errors` in 1833 s. The errors came from the host: `/tmp` (a
  1M-inode tmpfs) ran out of inodes, which gave `OSError: could not create numbered dir` and
  ENOSPC, and the queue tests saw `admission_paused: temp_pressure`. A rerun of the 98 affected
  files with `TMPDIR` on `/home` was killed by its own 1700 s `timeout` at 94%, with no single
  test identified as hanging, and one failure. A per-file split (300 s each) was stopped on the
  human's order after its first file. That file, `tests/test_agentic_conductor_guard.py::
  test_conduct_skill_never_asks_the_human`, fails on `skills/nw-conduct/SKILL.md` (the phrase
  "wait for the human"). This branch does not touch that skill or that test, so the failure
  already exists on main. The other 97 files were not re-verified one by one.
- No manual run against the worker box, per the skip in the Test plan.

## Open questions

- None.
