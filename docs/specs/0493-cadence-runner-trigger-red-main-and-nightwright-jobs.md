---
id: SPEC-0493
title: "Cadence runner trigger, red-main source and nightwright's jobs"
status: in-progress
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0489
tags: [agentic, gates, sentinel, cadence, redmain]
depends_on: [SPEC-0491, SPEC-0490, SPEC-0359, SPEC-0379, SPEC-0381, SPEC-0420]
---

## Context

SPEC-0491 gives a project a validated `cadence:` list, per-job state, the merge counter and a
pure `due` function. This slice makes it run: the runner starts due jobs on worker slots by
reusing the sentinel's lock, worktree, placement and detached dispatch; a red job goes through
the SPEC-0420 red-main episode; and nightwright declares its five jobs. It is the second half
of the review-driven split of the original SPEC-0491. See SPEC-0489 for the evidence.

## Goals / Non-goals

**Goals**
- The runner starts due jobs, one background run per project on the single `pool.sentinel`
  slot, campaigns first, including for projects with no `gate_reuse.json`.
- A red job opens or holds the single red-main episode, and a merged fix is confirmed by that
  same job.
- `nw gate cadence run JOB`.
- nightwright's `.nw/project.yaml` declares the five jobs.

**Non-goals**
- Bisecting a red cadence run; a dashboard tab; config/state (SPEC-0491).

## Decision

- The runner reuses the sentinel's run lock, worktree, placement and detached dispatch.
- A project with a non-empty `cadence:` skips the SPEC-0379 clock trigger. SPEC-0359's
  reuse-suspect trigger stays, and it runs the fast tier only because `NW_ALL_TIERS` is not set
  for cadence projects.
- The red-main episode gains a `source` (`sentinel` or `cadence:<job>`). A cadence source never
  bisects. **A cadence episode is confirmed by its own job, not by the fast sentinel** (decided:
  the fast sentinel cannot prove a heavy tier green).
- Coverage reading: "every heavy tier at least every 24 h" means at most 24 h after a merge
  made it due. A green job at an unchanged sha is not re-run (nothing new to test), which is
  consistent with the epic's coverage invariant.

## Design

**Trigger.** In `queue._sentinel_step`, the early return when `SN.projects()` is empty is
removed, so a registered project with `cadence:` and no `gate_reuse.json` still starts. A tick
starts at most one cadence run, and it does not scan while a future is in flight on
`pool.sentinel`. The scan runs only after the sentinel loop has started nothing, the tick is not
stopping, and no campaign is waiting for a gate slot. Walk `rules.registered_projects(cfg)` in
order. A project whose config or base sha fails to load is journalled `cadence_failed` once per
reason and skipped, as is a project that is `SN.running` or whose cadence list is empty. For each
remaining project, **before `due`**, the scan calls `cadence.ensure` (SPEC-0491 seeds each job
row; `seeded_at` is set once and not refreshed) and `cadence.reconcile` (a crash-left `inflight`
is consumed with `last_ok` null, so the job waits `RETRY_S` instead of restarting at once); a
never-run job then becomes due from `every_hours` after that `seeded_at`, even on a runner that
has seen no merge. On the first project that has a due job, a forced job wins (ties: earlier in
the `cadence:` list); otherwise start the due job with the oldest `last_run_at or seeded_at`
(ties: earlier in the list). That future occupies `pool.sentinel`; harvest journals
`cadence_done` or `cadence_failed` (never `sentinel_done`) and runs before `_red_main_step`. A
project with a non-empty `cadence:` skips `SN.time_due`.

`_red_main_step` visits every project `SN.projects(cfg)` returns and every registered project
whose loaded config has a non-empty `cadence:` list (a project whose config fails to load is
skipped). A red cadence run on a project with no `gate_reuse.json` still gets moot-abandon,
admit and `step()`, so the SPEC-0420 episode is not half-wired.

**Run.** `cadence.run(cfg, project, job)`:
- Holds `sentinel.run_lock` for the whole run; if it is held, raises `sentinel.Busy` before
  `_prepare_worktree`. No per-job flock (the run lock already serializes sentinel and cadence
  runs of the project).
- Prepares the base worktree with `sentinel._prepare_worktree`, and calls
  `cadence.begin(cfg, project, job, sha)` (SPEC-0491) only after it has returned a sha. A
  prepare failure therefore never clears `forced`. It does not call `SN._note_run` and does not
  write `<project>.last.json`.
- Calls `sentinel._placement` with a config whose gates list is `[P.cadence_gate(job)]`, so
  `gates_for(..., "close")` sees that one `when: close` gate. It then sets
  `state["id"] = "sentinel-<E._safe(project)>-<job>"` and calls `header_refusal` again on that id
  (`_placement` sanitizes differently, and `run_detached` uses `state["id"]` as the campaign);
  a refusal takes the local path.
- Holds one gate slot for the whole run with `gate_slots.acquire` exactly as
  `sentinel._run_locked` does, passing `stop` too, so campaigns stay first for the whole run and
  a stop while waiting for a slot becomes the interrupt.
- A worker pool dispatches `remote_gates.run_detached` with that one-gate config. When
  `_placement` returns no pools, the single gate runs locally through
  `gates.run_gate_with_retries`. A `remote_gates.RemoteFailure` records a failed run
  (`cadence.finish` with `ok` null) and never runs locally.
- Logs go under the directory `sentinel._run_locked` already uses,
  `wt.parent / f"{wt.name}.logs" / f"cadence-<job>"`.
- An interrupt (a stop during the slot wait or the gate) does not call `finish`, `on_red` or
  `on_green`. It calls `cadence.abort` (SPEC-0491), which restores `forced` if `begin` had
  cleared it, so a confirmation is not dropped. Any other exception after `begin` calls
  `finish` with `ok` null and that sha, calls neither red-main hook, and returns the failure so
  harvest journals `cadence_done`. `ok` null never calls `on_red` / `on_green` (a
  `RemoteFailure` is a transport failure and must not open an episode or hold merges).
- **Events.** A finished run, including a red run, a run with `ok` null and an interrupt,
  returns a result. The harvest at the top of `_sentinel_step`, still before `_red_main_step`,
  journals `cadence_done` for that result and never `sentinel_done`. An exception that escapes
  `cadence.run` before it returns journals `cadence_failed` only. A different-source `on_red`
  also journals `cadence_deferred`; that journal does not replace `cadence_done`. A
  non-interrupted finish appends one `cadence` event with data `{project, job, ok, sha, reason,
  duration_s}` plus `failing` (at most 50 ids) when `ok` is false. Register that type in
  `events.EVENT_TYPES` and `schemas.EVENT_KEYS`; `quiet.RULES["cadence"]` is `hold`, and
  `cadence` is added to the conduct skill's Always held list (`tests/test_skills.py` keeps that
  list in step with the hold rules). Five jobs must not wake the conductor on every run;
  `red_main` already carries the episode.

**Red** (`redmain`):
- `cadence.run` calls `redmain.on_red` / `on_green` with `source="cadence:<job>"`, the tested
  sha and that job's failing ids. The existing sentinel passes `source="sentinel"`; an older
  episode with no `source` reads as `sentinel`.
- A same-source `on_red` updates the open episode as today and leaves `source` and any pending
  triple unchanged. A different-source `on_red` changes neither `source` nor `failing` and opens
  no second episode. It journals `cadence_deferred`. If `pending_source` is unset it stores the
  triple `pending_source`, `pending_failing`, `pending_sha`; if already set it leaves that triple
  unchanged (first pending wins), except that a repeat `on_red` of the same `pending_source`
  refreshes `pending_failing` and `pending_sha`. A different-source `on_green` whose source is
  the `pending_source` clears the triple and writes that; any other different-source `on_green`
  returns without writing, with no event and no escalation withdrawal. A run with `ok` null
  calls neither hook.
- When the episode's own source goes green, `on_green` closes it (withdraws its escalation,
  appends `red_main_cleared`) and, in that same write, if the triple is set, opens the next
  episode the way `on_red` opens one: `episode + 1`, `source = pending_source`, `failing =
  pending_failing`, `first_red_sha = last_red_sha = pending_sha`, `awaiting_refresh` true, then
  clears the triple. So a sentinel red deferred during a cadence episode is kept while the
  SPEC-0379 clock is skipped. A promoted sentinel source also sets `due_reason` to `deferred
  sentinel red`, so one fast sentinel run is due even though this project skips `SN.time_due`. A
  promoted `cadence:<job>` source sets that job's `forced` flag under the cadence state lock.
  `step()` returns none while `awaiting_refresh` is true; the refreshing same-source `on_red` or
  `on_green` clears it. A fix-forward is not opened from a sha the base has already left.
- For an episode whose source starts with `cadence:`, `redmain.record_merge` sets that job's
  `forced` flag (SPEC-0491 state) and does not set `due_reason`. The confirmation run is that
  cadence job; a matching `on_green` closes the episode. (`holding()` already returns None once
  the base moves past `last_red_sha`, so merges resume while the confirmation is pending.)
- `open_fixforward` sets `cadence_job` to the suffix of a `cadence:` source and leaves it unset
  for source `sentinel`. The brief's first line is
  `The base branch of <project> is RED (cadence job <job>).` followed by that job's failing
  ids; it does not say the full suite failed. `holding()` names the episode source. `full_suite`
  stays true.
- `gates.run_all` appends `P.cadence_gate(job)` to the list just assigned from `gates_for` (the
  `project_cfg = {**project_cfg, "gates": P.gates_for(project_cfg, tier)}` assignment), on the
  `project_cfg` that the adopt path, the remote dispatch and the local path all read, only when
  `tier == close` and `campaign_state["cadence_job"]` names a job in that config. `full_suite`
  still only adds `NW_AFFECTED_FULL`. With a non-empty cadence list SPEC-0490 leaves
  `NW_ALL_TIERS` unset, so those close gates stay on the fast tier and the appended gate is the
  heavy command. Sentinel and ordinary close lists never gain it.
- `step()` for a `cadence:` source keeps today's `none` returns (a fix in flight, a merged fix
  awaiting confirmation, usage exhausted, and `awaiting_refresh`). It then returns
  `open_fixforward` while failed fix-forward attempts are under `fixforward_attempts`, and
  `escalate` once they are exhausted, even when revert is enabled and a culprit is known. It
  never returns `localize` or `open_revert`.

**CLI** (`agentic/cli_gate.py`): `nw gate cadence run JOB [--project P]` sets `forced` (SPEC-0491
`due` returns `forced` immediately, skipping the green same-sha rule and `RETRY_S`) whether or
not the lease is held. `--project` defaults the way `nw gate cadence status` does. The next tick
that starts a job of that project starts `JOB` (a forced job beats an older due job of the same
project), including inside `RETRY_S`, on a green unchanged sha, and when another job of that
project is also due; an earlier registered project that takes the single slot first can delay
it. It prints that the run is queued when the queue lease is held, and that no runner is up when
it is not.
`nw gate cadence status` (SPEC-0491) now shows the `running` column from `SN.running`.

**nightwright's jobs** (`.nw/project.yaml`; defaults tuned later from cadence events). Each of
the five jobs sets `timeout_s: 2400`, `retries: 1` and `results: {format: pytest}` (so a red
run has pytest failing ids for the brief). `on_paths` entries are separate SPEC-0350 globs
(braces would be literal), each a full repo-relative glob (a bare basename such as `close.py`
would match at any depth, outside `src/nightwright/agentic/`):

| Job | Command (`uv run pytest -q`) | Triggers |
|---|---|---|
| `fast-full` | `-n 4 -m "not (integration or tui or remote or perf)"` | Every 5 merges; every 12 h. |
| `integration` | `-n 4 -m integration` | Every 5 merges; every 24 h; `on_paths`: `src/nightwright/agentic/queue.py`, `src/nightwright/agentic/close.py`, `src/nightwright/agentic/gates.py`, `src/nightwright/agentic/runner.py`, `src/nightwright/agentic/sentinel.py`, `src/nightwright/agentic/redmain.py`. |
| `tui` | `-n 4 -m tui` | Every 10 merges; every 24 h; `on_paths`: `src/nightwright/tui/**`, `src/nightwright/agentic/dashboard/**`. |
| `remote` | `-n 2 -m remote` | Every 24 h; `on_paths`: `src/nightwright/agentic/remote_*.py`, `src/nightwright/agentic/worker*.py`, `src/nightwright/agentic/pull_*.py`, `src/nightwright/agentic/gate_jobs.py`. |
| `perf` | `-n 0 -m perf` | Every 24 h. |

Why these numbers: about 10-20 merges a day, so every 5 merges gives 2-4 `fast-full` runs a
day (close to the sentinel's 4 a day, at fast-tier cost); heavy tiers run at least daily after a
change, as the coverage invariant requires; a path miss on `fast-full` or `integration`
stays undetected for at most 5 merges, on `tui` for at most 10, and on `remote` or `perf` until
`every_hours` (24) after the base has moved.

## Alternatives considered

- **A new scheduler.** The sentinel path already offers one run in flight, campaigns first and
  worker placement.
- **Confirming a cadence fix with the fast sentinel.** It cannot close the episode.
- **One episode per source.** A merge hold could lift while another tier is still red.
- **A per-job flock.** Redundant under `run_lock`.

## Acceptance criteria

- [x] A tick starts at most one cadence run and does not scan while a future is in flight on
  `pool.sentinel`. On the first remaining registered project with a due job, a forced job wins
  over an older due job (ties: earlier in the list), otherwise the due job with the oldest
  `last_run_at or seeded_at`; only when no sentinel run started and no campaign waits for a slot,
  never for a project that is `SN.running`, and for a registered cadence project with no
  `gate_reuse.json` (empty `SN.projects()`). A project whose config fails to load is journalled
  `cadence_failed` once per reason and skipped.
- [x] Before `due`, the scan seeds each job row (`seeded_at` set once): a cadence project that
  has never merged has seeded rows after one tick and an `every_hours`-only job becomes due after
  `seeded_at + every_hours`; a crash-left `inflight` is consumed before the due check, so that
  job waits `RETRY_S` instead of starting again at once.
- [x] A registered cadence project absent from `SN.projects()` gets an `open_fixforward` on the
  tick after a red run (`_red_main_step` visits it); a project whose config fails to load is
  skipped.
- [x] A finished run, red or `ok` null, returns a result and the harvest journals `cadence_done`
  (never `sentinel_done`) before `_red_main_step`, plus one `cadence` event; a deferral also
  journals `cadence_deferred`; an exception escaping `cadence.run` journals `cadence_failed`
  only. `cadence` is in `events.EVENT_TYPES` and `schemas.EVENT_KEYS`, `quiet.RULES["cadence"]`
  is `hold`, and the conduct skill's Always held list matches. A project with `cadence:` skips
  `SN.time_due`.
- [x] `nw gate cadence status` shows `running` from `SN.running`, the same value on every job row
  while the project lock is held.
- [x] `cadence.run` while `run_lock` is held raises `Busy` without calling `_prepare_worktree`;
  a running job makes `SN.running` true; it writes no `<project>.last.json` and does not call
  `SN._note_run`.
- [x] `cadence.begin` is called only after `_prepare_worktree` returned a sha (a prepare failure
  does not clear `forced`). The run calls `_placement` with the one-gate config, then sets the
  campaign id to `sentinel-<E._safe(project)>-<job>` and calls `header_refusal` again (a project
  name with a space yields an id `validate_header` accepts; a refusal runs locally); it holds a
  gate slot via `gate_slots.acquire` with `stop`, for the whole run; with no placeable pool the
  one gate runs locally; a `RemoteFailure` is a failed run (`ok` null) and never runs locally;
  logs go under `wt.parent/<wt.name>.logs/cadence-<job>`. A stop during the slot wait or the gate
  restores `forced` (`cadence.abort`), writes no `finish` and calls neither red-main hook; any
  other exception after `begin` finishes with `ok` null and calls neither hook.
- [x] A red run opens an episode with source `cadence:<job>`; `ok` null opens none. With a
  `cadence:tui` episode open, `on_red` for `cadence:remote` or a sentinel red leaves the source
  and failing ids unchanged, journals `cadence_deferred` and stores the `pending_source` /
  `pending_failing` / `pending_sha` triple; a second different-source `on_red` does not replace
  it (first wins; a repeat of the same pending source refreshes it); `on_green` for another
  source leaves the episode open and, unless it is the `pending_source`, writes nothing; the
  `pending_source`'s own green clears the triple.
- [x] A merge during a `cadence:<job>` episode forces that job (no `due_reason`), and a
  same-source green closes the episode; a deferred sentinel red becomes the next episode in that
  same write (episode + 1, `first_red_sha = last_red_sha = pending_sha`, `awaiting_refresh`
  true, `due_reason` `deferred sentinel red`; a promoted cadence source sets its job's `forced`);
  `step()` is none while `awaiting_refresh` is true until the refreshing same-source run.
- [x] The fix brief names the job on its first line and lists that job's failing ids;
  `open_fixforward` sets `cadence_job`; `step()` for a `cadence:` source is none while a fix is in
  flight, a merged fix awaits confirmation, usage is exhausted or `awaiting_refresh` is true, then
  returns `open_fixforward` under `fixforward_attempts` and `escalate` once exhausted (even with
  revert enabled and a culprit known), and never `localize` or `open_revert`. `holding()` names
  the episode source; an episode with no source reads as `sentinel` and its `record_merge` still
  sets `due_reason`.
- [x] A fix campaign with `cadence_job: tui` runs the tui command at close even when the branch
  touches none of the job's `on_paths`, appended to the list just assigned from `gates_for` in
  `run_all` (so the adopt, remote and local paths all see it), only when `tier == close` and
  `cadence_job` names a configured job. A sentinel or ordinary close list for the same project
  does not include it.
- [x] A reuse-suspect trigger (SPEC-0359) on a cadence project still starts a sentinel run and
  does not set `NW_ALL_TIERS`.
- [x] `nw gate cadence run JOB` sets `forced` whether or not the lease is held, `--project`
  defaults like `status`, and prints the queued / no-runner message. The next tick that starts a
  job of that project starts `JOB`, inside `RETRY_S`, on a green unchanged sha, and when another
  job of that project is also due. It does not claim `JOB` starts when an earlier registered
  project takes the slot.
- [x] nightwright's five jobs validate with `timeout_s: 2400`, `retries: 1`,
  `results.format: pytest`; every `on_paths` entry is one full repo-relative SPEC-0350 glob (no
  bare basename), and `path_matches` matches `src/nightwright/agentic/queue.py` for
  `integration` and `src/nightwright/agentic/remote_gates.py` for `remote`, and does not match a
  bare `queue.py` or `close.py`.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_cadence.py`: the `run` lock, `Busy`, `begin` after prepare, placement
    with the one-gate config, the id overwrite and second `header_refusal`, slot acquisition with
    `stop`, local branch, `RemoteFailure`, interrupt (`abort` restores `forced`), other
    exceptions, the log dir, events and result shapes (with a faked `run_detached`).
  - `tests/test_agentic_sentinel.py` / the queue step tests: the runner scan (forced beats older
    due, oldest due, ties, one in flight, campaigns first, empty `SN.projects()`, clock skipped,
    load failure once, `ensure` / `reconcile` before `due`), harvest ordering, and a fix-forward
    admitted for a cadence project absent from `SN.projects()`. A test for the reuse-suspect
    trigger on a cadence project.
  - `tests/test_redmain.py`: source on open, cross-source `on_red` / `on_green`, the pending
    triple (first wins, clear on that source's green, promotion with `pending_sha` and
    `awaiting_refresh`), `ok` null calling neither hook, `record_merge` forcing, a legacy
    episode and `holding()`, the brief, and `step()` (none returns, escalate after the budget,
    never localize or revert).
  - `tests/test_agentic_gates.py`: `run_all` appends the job gate to the `gates_for` list only
    for `cadence_job` at close, remote and local.
  - `tests/test_events.py` / the quiet-rules and `tests/test_skills.py` checks: the `cadence`
    event type, `hold`, and the conduct skill list.
  - `tests/test_gate_jobs.py`: `cadence run` messages and `status` `running`.
  - `tests/test_agentic_dogfood_config.py`: the five jobs validate and the `path_matches`
    checks with the two repo-relative paths.
- **Manual verification** (validate against the real binary): after merge and `nw queue
  restart`, `nw gate cadence status` shows the five jobs seeded; `nw gate cadence run perf`
  dispatches to the real worker within one tick and its event and log dir exist. Record both
  in *As shipped*.
- **Regression guard:** the SPEC-0359, SPEC-0379 and SPEC-0420 tests pass unchanged. Run the affected
  tests plus the files above.

## Rollout / migration

- nightwright's `cadence:` lands in the same campaign. `nw queue restart` picks it up.
- The state seeds on the first merge or tick (SPEC-0491). No job runs before its `every_hours`
  from seeding unless merges or paths make it due.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass (AFFECTED_RESULT); manual steps not performed (see As shipped).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

### As shipped (2026-11-09)

- `agentic/cadence.py`: `pick` (ensure, reconcile, then forced first / oldest due, list order
  on ties), `run` / `_run_locked` under `SN.run_lock`, `campaign_id`
  (`sentinel-<E._safe(project)>-<job>`), `force`, `_find_job`, `FAILING_EVENT_MAX = 50`; status
  `running` from `SN.running`. The `cadence` event is appended `by="queue"`.
- `agentic/queue.py`: `_sentinel_step` no longer returns early on an empty `SN.projects()`;
  a project with `cadence:` skips the clock (`SN.time_due`); after the sentinel loop starts
  nothing, `_cadence_scan` walks `registered_projects`. `pool.sentinel` holds a 3-tuple
  `(project, future, job)` for a cadence run; harvest (`_cadence_harvest`) journals
  `cadence_done` / `cadence_failed`, and the scan journals `cadence_started`. `_waiting_on`
  lists a cadence flight as kind `cadence`. `_red_main_step` adds the registered projects whose
  config loads with a non-empty `cadence:` (`_cadence_projects`).
- Decided: a registered project with no repo or no `.nw/project.yaml` is skipped silently by
  the scan (most registered projects have no cadence; journalling them every tick would be
  noise). Only a config that exists but fails to load, or a base sha that cannot be read,
  journals `cadence_failed`, once per (project, error).
- `agentic/redmain.py`: `source` on episodes (`source_of`, `cadence_job`, `source_label`), the
  pending triple and `_promote`, `awaiting_refresh`, cadence-aware `step` / `holding` / `brief`
  / `open_fixforward` / `record_merge`. A cadence episode's `last_green_sha` comes from the
  job's cadence row, so a cadence red never creates sentinel state. The deferral journal kind
  is `cadence_deferred` for both directions (a sentinel red deferred behind a cadence episode
  too). `sentinel.settle` passes `source="sentinel"`.
- `agentic/gates.py`: `cadence_job_of`; `run_all` appends the job gate at close.
- `schemas.py` / `events.py` / `quiet.py` / `skills/nw-conduct/SKILL.md`: the `cadence` event
  type (keys `project, job, ok, sha, reason, duration_s`, plus `failing` when `ok` is false),
  `hold`.
- `cli_gate.py`: `nw gate cadence run JOB [--project P]`; an unknown job exits 2.
- `.nw/project.yaml`: the five jobs as in the table.
- Tests: the run, pick, runner-scan, harvest, `_red_main_step`, CLI `run` and status `running`
  tests are in a new file, `tests/test_agentic_cadence_run.py` (it reuses the `world` git
  fixture of `test_agentic_close`), instead of `test_agentic_cadence.py` /
  `test_agentic_sentinel.py` / `test_gate_jobs.py`. Red-main source tests are in
  `tests/test_redmain.py`, the close append in `tests/test_agentic_gates.py`, the event keys
  and skill list in `tests/test_agentic_events.py`, `hold` in `tests/test_agentic_quiet.py`,
  the five jobs in `tests/test_agentic_dogfood_config.py`.
- Manual verification was **not performed**: it needs `nw queue restart` and a real worker
  dispatch, and this implementation session was told not to touch the runner or `nw queue`.
  It remains for the first runner restart after merge: `nw gate cadence status` should list
  the five jobs seeded, and `nw gate cadence run perf` should dispatch within one tick.
