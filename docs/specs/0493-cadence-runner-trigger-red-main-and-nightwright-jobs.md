---
id: SPEC-0493
title: "Cadence runner trigger, red-main source and nightwright's jobs"
status: accepted
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

**Trigger.** In `queue._sentinel_step`, the cadence scan runs only when the sentinel loop did
not start a run. The early return when `SN.projects()` is empty is removed, so a registered
project with `cadence:` and no `gate_reuse.json` still starts. The scan covers
`rules.registered_projects(cfg)`. A project whose config or base sha fails to load is journalled
`cadence_failed` once per reason and skipped. For each project with a non-empty cadence list it
computes `cadence.due` for every job and starts the due job with the oldest
`last_run_at or seeded_at` (ties: earlier in the `cadence:` list). A project whose
`sentinel.run_lock` is held (`SN.running`) is skipped. A run starts only when no campaign is
waiting for a gate slot. The future occupies `pool.sentinel`; harvest journals `cadence_done` or
`cadence_failed` (never `sentinel_done`) and runs before `_red_main_step`. A project with a
non-empty `cadence:` skips `SN.time_due`.

**Run.** `cadence.run(cfg, project, job)`:
- Holds `sentinel.run_lock` for the whole run; if it is held, raises `sentinel.Busy` before
  `_prepare_worktree`. No per-job flock (the run lock already serializes sentinel and cadence
  runs of the project).
- Calls `cadence.begin` (SPEC-0491) and prepares the base worktree with
  `sentinel._prepare_worktree`. It does not call `SN._note_run` and does not write
  `<project>.last.json`.
- Calls `sentinel._placement` with a config whose gates list is `[P.cadence_gate(job)]`, so
  `gates_for(..., "close")` sees that one `when: close` gate. It then sets
  `state["id"] = "sentinel-<E._safe(project)>-<job>"` and calls `header_refusal` again on that id
  (`_placement` sanitizes differently, and `run_detached` uses `state["id"]` as the campaign);
  a refusal takes the local path.
- Holds one gate slot for the whole run, `gate_slots.acquire(cfg, state["id"], pools, repool)`,
  as `sentinel._run_locked` does, so campaigns stay first for the whole run.
- A worker pool dispatches `remote_gates.run_detached` with that one-gate config. When
  `_placement` returns no pools, the single gate runs locally through
  `gates.run_gate_with_retries`. A `remote_gates.RemoteFailure` records a failed run
  (`cadence.finish` with `ok` null) and never runs locally.
- Logs go under `<agentic>/sentinel/<project>.logs/cadence-<job>/`. An interrupt skips
  `finish` and the red-main calls.
- **Events.** Each run appends a `cadence` event
  `{project, job, ok, sha, reason, failing[:50], duration_s}` and journals `cadence_done`,
  `cadence_failed` or `cadence_deferred`.

**Red** (`redmain`):
- `cadence.run` calls `redmain.on_red` / `on_green` with `source="cadence:<job>"`, the tested
  sha and that job's failing ids. The existing sentinel passes `source="sentinel"`; an older
  episode with no `source` reads as `sentinel`.
- A same-source `on_red` updates the open episode as today. A different-source `on_red` does not
  change the source and does not open a second episode: it stores `pending_source` and
  `pending_failing` and journals `cadence_deferred`. When the episode's own source goes green,
  `on_green`, in that same write, opens the next episode from `pending_source` when set, so a
  sentinel red deferred during a cadence episode is kept while the SPEC-0379 clock is skipped.
  A different-source `on_green` returns without writing.
- For an episode whose source starts with `cadence:`, `redmain.record_merge` sets that job's
  `forced` flag (SPEC-0491 state) and does not set `due_reason`. The confirmation run is that
  cadence job; a matching `on_green` closes the episode. (`holding()` already returns None once
  the base moves past `last_red_sha`, so merges resume while the confirmation is pending.)
- `open_fixforward` sets `cadence_job` to the suffix of a `cadence:` source and leaves it unset
  for source `sentinel`. The brief's first line is
  `The base branch of <project> is RED (cadence job <job>).` followed by that job's failing
  ids; it does not say the full suite failed. `holding()` names the episode source. `full_suite`
  stays true.
- `gates.run_all` appends `P.cadence_gate(job)` onto the list it just built with `gates_for`,
  after the `full_suite` replacement and before both the remote dispatch and the local path,
  only when `tier == close` and `cadence_job` names a job in the loaded config. With a
  non-empty cadence list `NW_ALL_TIERS` is unset (SPEC-0490), so the other close gates stay on
  the fast tier and the appended job is the heavy command. Sentinel and ordinary close lists
  never gain it.
- `step()` for a `cadence:` source returns only `open_fixforward` or `escalate`, never
  `localize` or `open_revert`.

**CLI** (`agentic/cli_gate.py`): `nw gate cadence run JOB [--project P]` sets `forced` (SPEC-0491
`due` returns `forced` immediately, skipping the green same-sha rule and `RETRY_S`). It prints
that the run is queued when the queue lease is held, and that no runner is up when it is not.
`nw gate cadence status` (SPEC-0491) now shows the `running` column from `SN.running`.

**nightwright's jobs** (`.nw/project.yaml`; defaults tuned later from cadence events). Each of
the five jobs sets `timeout_s: 2400`, `retries: 1` and `results: {format: pytest}` (so a red
run has pytest failing ids for the brief). `on_paths` entries are separate SPEC-0350 globs
(braces would be literal):

| Job | Command (`uv run pytest -q`) | Triggers |
|---|---|---|
| `fast-full` | `-n 4 -m "not (integration or tui or remote or perf)"` | Every 5 merges; every 12 h. |
| `integration` | `-n 4 -m integration` | Every 5 merges; every 24 h; `on_paths`: `src/nightwright/agentic/queue.py`, `close.py`, `gates.py`, `runner.py`, `sentinel.py`, `redmain.py` (each under `src/nightwright/agentic/`). |
| `tui` | `-n 4 -m tui` | Every 10 merges; every 24 h; `on_paths`: `src/nightwright/tui/**`, `src/nightwright/agentic/dashboard/**`. |
| `remote` | `-n 2 -m remote` | Every 24 h; `on_paths`: `src/nightwright/agentic/remote_*.py`, `worker*.py`, `pull_*.py`, `gate_jobs.py` (each under `src/nightwright/agentic/`). |
| `perf` | `-n 0 -m perf` | Every 24 h. |

Why these numbers: about 10-20 merges a day, so every 5 merges gives 2-4 `fast-full` runs a
day (close to the sentinel's 4 a day, at fast-tier cost); heavy tiers run at least daily after a
change, as the coverage invariant requires; a selector miss stays undetected for at most 5
merges.

## Alternatives considered

- **A new scheduler.** The sentinel path already offers one run in flight, campaigns first and
  worker placement.
- **Confirming a cadence fix with the fast sentinel.** It cannot close the episode.
- **One episode per source.** A merge hold could lift while another tier is still red.
- **A per-job flock.** Redundant under `run_lock`.

## Acceptance criteria

- [ ] The runner starts the due job with the oldest `last_run_at or seeded_at`, only when no
  sentinel run started and no campaign waits for a slot, never while `SN.running`, and for a
  registered cadence project with no `gate_reuse.json` (empty `SN.projects()`). A project whose
  config fails to load is journalled `cadence_failed` once per reason and skipped.
- [ ] Harvest journals `cadence_done` / `cadence_failed`, not `sentinel_done`, before
  `_red_main_step`. A project with `cadence:` skips `SN.time_due`.
- [ ] `cadence.run` while `run_lock` is held raises `Busy` without calling `_prepare_worktree`;
  a running job makes `SN.running` true; it writes no `<project>.last.json` and does not call
  `SN._note_run`.
- [ ] The run calls `_placement` with the one-gate config, then sets the campaign id to
  `sentinel-<E._safe(project)>-<job>` and calls `header_refusal` again (a project name with a
  space yields an id `validate_header` accepts; a refusal runs locally); it holds a gate slot
  for the whole run; with no placeable pool the one gate runs locally; a `RemoteFailure` is a
  failed run and never runs locally; an interrupt consumes nothing.
- [ ] A red run opens an episode with source `cadence:<job>`. With a `cadence:tui` episode open,
  `on_red` for `cadence:remote` or a sentinel red leaves the source and failing ids unchanged
  and stores `pending_source`; `on_green` for another source leaves the episode open.
- [ ] A merge during a `cadence:<job>` episode forces that job (no `due_reason`), and a
  same-source green closes the episode; a deferred sentinel red becomes the next episode in
  that same write.
- [ ] The fix brief names the job on its first line and lists that job's failing ids;
  `open_fixforward` sets `cadence_job`; `step()` returns neither `localize` nor `open_revert`.
- [ ] A fix campaign with `cadence_job: tui` runs the tui command at close even when the branch
  touches none of the job's `on_paths`, appended after the `full_suite` replacement on both the
  remote and local paths. A sentinel or ordinary close list for the same project does not
  include it.
- [ ] `nw gate cadence run JOB` sets `forced`, prints the queued / no-runner message, and the
  next tick starts the job inside `RETRY_S` and on a green unchanged sha.
- [ ] nightwright's five jobs validate with `timeout_s: 2400`, `retries: 1`,
  `results.format: pytest`; every `on_paths` entry is one SPEC-0350 glob, and `path_matches`
  matches `queue.py` for `integration` and `remote_gates.py` for `remote`.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_cadence.py`: the `run` lock, `Busy`, placement with the one-gate config,
    the id overwrite and second `header_refusal`, slot acquisition, local branch,
    `RemoteFailure`, events (with a faked `run_detached`).
  - `tests/test_agentic_sentinel.py` / the queue step tests: the runner scan (oldest due, ties,
    one in flight, campaigns first, empty `SN.projects()`, clock skipped, load failure once),
    and harvest ordering.
  - `tests/test_agentic_redmain.py`: source on open, cross-source `on_red` / `on_green`,
    `pending_source` promotion, `record_merge` forcing, a legacy episode, the brief, and
    `step()` never localizing.
  - `tests/test_agentic_gates.py`: `run_all` appends the job gate only for `cadence_job` at
    close, after the `full_suite` replacement, remote and local.
  - The `nw gate` CLI test module: `cadence run` messages.
  - `tests/test_agentic_dogfood_config.py`: the five jobs validate and the glob checks.
- **Manual verification** (validate against the real binary): after merge and `nw queue
  restart`, `nw gate cadence status` shows the five jobs seeded; `nw gate cadence run perf`
  dispatches to the real worker within one tick and its event and log dir exist. Record both
  in *As shipped*.
- **Regression guard:** the SPEC-0379 and SPEC-0420 tests pass unchanged. Run the affected
  tests plus the files above.

## Rollout / migration

- nightwright's `cadence:` lands in the same campaign. `nw queue restart` picks it up.
- The state seeds on the first merge or tick (SPEC-0491). No job runs before its `every_hours`
  from seeding unless merges or paths make it due.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
