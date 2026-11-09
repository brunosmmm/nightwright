---
id: SPEC-0491
title: "Per-project cadence jobs, runner trigger and red-main source"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0489
tags: [agentic, gates, sentinel, cadence, redmain]
depends_on: [SPEC-0490, SPEC-0359, SPEC-0379, SPEC-0381, SPEC-0420]
---

## Context

After SPEC-0490, campaign gates skip the heavy tiers, and only the 6-hourly SPEC-0379 sentinel
runs them, as a full suite. The human wants each project to choose when its heavy tests run:
- after every N merges;
- every H hours;
- when a merge touches given paths;
- after every merge.

nw keeps the state and the runner starts the runs in the background. See SPEC-0489 for the
evidence.

## Goals / Non-goals

**Goals**
- A `cadence:` list in `.nw/project.yaml`, validated.
- Per-project state on disk that survives restarts and counts every ff-merge.
- The runner starts due jobs on worker slots, one background run per project, and campaigns go
  first.
- A red job opens or holds the single red-main episode, and its fix campaign runs the job.
- `nw gate cadence status` and `nw gate cadence run`.

**Non-goals**
- Bisecting a red cadence run.
- A dashboard tab.

## Decision

- A cadence job is a named command with triggers. nw knows nothing about pytest tiers.
- The runner reuses the sentinel's lock, worktree, placement and detached dispatch.
- A project with `cadence:` skips the SPEC-0379 clock trigger. SPEC-0359's reuse-suspect
  trigger stays, and it now runs the fast tier only, because `NW_ALL_TIERS` is not set.
- The red-main episode gains a `source`. A cadence source never bisects.

## Design

**Schema.** `agentic/project.py` adds a top-level `cadence` key, defaulting to `[]`:

```yaml
cadence:
  - name: fast-full          # unique, [a-z0-9-]+
    cmd: uv run pytest -q -n 4 -m "not (integration or tui or remote or perf)"
    every_n_merges: 5        # int >= 1
    every_hours: 12          # number > 0
    on_paths: []             # globs, matched like SPEC-0350 `paths`
    always: false            # true: due after any merge
    timeout_s: 2400
    retries: 1               # optional, gate meaning (SPEC-0289)
    retry_cmd: …             # optional
    results: {format: pytest}
```

A job needs at least one trigger. Unknown keys and bad values are rejected by key path.
`P.cadence_gate(job)` returns a gate dict with only gate keys: `name`, `cmd`, `timeout_s`,
`retries`, `retry_cmd`, `results`, `required: true` and `when: close`. It has no `paths`.

**nightwright's jobs.** The defaults below are tuned later from cadence events:

| Job | Command (`uv run pytest -q`) | Triggers |
|---|---|---|
| `fast-full` | `-n 4 -m "not (integration or tui or remote or perf)"` | Every 5 merges; every 12 h. |
| `integration` | `-n 4 -m integration` | Every 5 merges; every 24 h; `on_paths`: `src/nightwright/agentic/{queue,close,gates,runner,sentinel,redmain}.py`. |
| `tui` | `-n 4 -m tui` | Every 10 merges; every 24 h; `on_paths`: `src/nightwright/tui/**`, `src/nightwright/agentic/dashboard/**`. |
| `remote` | `-n 2 -m remote` | Every 24 h; `on_paths`: `src/nightwright/agentic/{remote_*,worker*,pull_*,gate_jobs}.py`. |
| `perf` | `-n 0 -m perf` | Every 24 h. |

Why these numbers:
- The current rate is about 10-20 merges a day. Every 5 merges gives 2-4 `fast-full` runs a
  day, which is close to the sentinel's 4 a day but costs only the fast tier.
- The heavy tiers run at least daily, as the SPEC-0489 coverage invariant requires.
- A selector miss stays undetected for at most 5 merges.

**State.** A new module, `agentic/cadence.py`, keeps `<agentic>/cadence/<project>.json`,
written under `<project>.json.lock` with `_atomic_write`. The schema is `nw.cadence.v1`. Per
job it holds:
- `merges_since`;
- `paths_hit` (capped at 20);
- `seeded_at`;
- `last_run_at`, `last_run_sha` and `last_ok`;
- `last_green_at` and `last_green_sha`;
- `last_error`;
- `forced`.

An unreadable file is renamed to `<project>.json.bad-<ts>`, journalled, and reseeded.

**Merge hook.** It runs on every successful ff-merge in `close.py`: the `policy == merge` path,
right after `git.ff_merge`, docs-only merges included. It is **not** inside
`_record_reused_merge`, which runs only for reused closes.
- The call is `cadence.record_merge(cfg, project, merged, paths)`.
- `paths` is `git diff --name-only <target_sha> <merged>`, the whole range that landed.
- If the diff fails, every `on_paths` job counts as hit.
- A `pr` or `draft-pr` close does not ff-merge, so it does not count.
- A project without `cadence:` writes nothing.

**Seeding.** The first time the state sees a job, it sets `seeded_at = now`. This avoids a
stampede at rollout.

**Due rule.** `cadence.due(job, st, base_sha, now)` returns a reason or `None`:
1. Not due while the project's `sentinel.run_lock` is held (`SN.running`).
2. If the last run was **green** and `base_sha == last_run_sha`: not due, unless `forced`.
3. If the last run was **red or failed** at that sha: not due until `RETRY_S` has passed
   (`SN.backoff`). After that it is due even though the base has not moved.
4. If the job **has never run** (`last_run_sha` is null), `every_hours` counts from
   `seeded_at`, and the job does not wait for a merge.
5. Otherwise the first matching trigger wins:
   - `forced`;
   - `always` and `merges_since > 0`;
   - `merges_since >= every_n_merges`;
   - `paths_hit` is non-empty;
   - `now - (last_run_at or seeded_at) >= every_hours`.

**Consumption.** At run start, nw snapshots `merges_since` and `paths_hit` and clears
`forced`. At the end (green, red or failed), it subtracts the snapshot, so merges that land
during the run still count.

**Trigger.** In `queue._sentinel_step`, after the existing sentinel candidates, nw scans
`rules.registered_projects(cfg)`, not only `SN.projects()`. For each project whose loaded
config has a non-empty `cadence:`, it computes the due jobs and starts the one with the oldest
`last_run_at or seeded_at`. The step's existing rules still apply:
- one background run in flight per runner;
- a run starts only when no campaign is waiting for a gate slot;
- a project is skipped while `SN.running` is true, for the sentinel and for cadence alike.

A project with `cadence:` skips `SN.time_due`.

**Run.** `cadence.run(cfg, project, job)`:
- Holds `sentinel.run_lock` for the whole run, `run_detached` included. If the lock is already
  held, it raises `sentinel.Busy` before touching the worktree.
- Takes a per-job flock (`<agentic>/sentinel/<project>.cadence-<job>.lock`) inside the run lock.
- Prepares the base worktree with `sentinel._prepare_worktree`.
- Places the run with `sentinel._placement`: a worker pool when one is placeable, local only
  when none is.
- Dispatches with `remote_gates.run_detached` and `P.cadence_gate(job)`. The dispatch campaign
  id is `sentinel-<E._safe(project)>-<job>`, the same sanitizer `_placement` uses.
- Writes logs under `<agentic>/sentinel/<project>.logs/cadence-<job>/`.
- Treats a transport failure as a failed run with backoff. It never falls back to a local run.

**Events.** Each run appends a `cadence` event:
`{project, job, ok, sha, reason, failing[:50], duration_s}`. It journals `cadence_done`,
`cadence_failed` or `cadence_deferred`.

**Red** (`redmain`):
- A project has **one** open episode, with a `source`. The existing sentinel passes
  `source="sentinel"`, and an older episode with no `source` reads as `sentinel`.
- `on_red(..., source=s)` with no open episode opens one with source `s`.
- `on_red` with an episode already open for a different source does not change the source, does
  not merge the failing ids into the episode's list, and does not open a second episode. It
  journals `cadence_deferred`, reason `episode open for <source>`, and returns. The next due
  run of that job reports the red again once the episode has closed.
- `on_green(..., source=s)` with a different source returns without writing.
- `step()` for a source starting with `cadence:` returns only `open_fixforward` or `escalate`,
  never `localize` or `open_revert`.
- The brief says which job failed and lists that job's failing ids. It does not say that the
  full suite failed.
- The fix campaign's state gets `cadence_job: <job>`. `gates.run_all` appends
  `P.cadence_gate(job)` when `tier == close` and `campaign_state.get("cadence_job")` names a
  configured job. That happens after `gates_for`, which is unchanged, so sentinel and ordinary
  close lists never gain the job. `full_suite` stays set.

**CLI** (`agentic/cli_gate.py`):
- `nw gate cadence status [--project P] [--json]`: one row per job, with:
  - its triggers;
  - `merges_since` out of N;
  - the paths hit;
  - the last run (age, sha, ok);
  - the due reason, or `not due (next: …)`;
  - whether it is running.

  The JSON schema is `nw.cadence.status.v1`.
- `nw gate cadence run JOB [--project P]`: sets `forced`. It prints that the run is queued for
  the next runner tick, or that no runner is up.

## Alternatives considered

- **Cron schedules.** They would run even when main has not moved.
- **A new scheduler.** The sentinel path already offers one run in flight, campaigns first and
  worker placement.
- **Global (nw config) cadence.** Each project's suite is different.
- **Running a heavy job at close when `on_paths` matches.** That puts heavy tests back on the
  campaign's critical path.
- **One episode per source.** A merge hold could then lift while another tier is still red.

## Acceptance criteria

- [ ] `cadence:` is validated: name, cmd, at least one trigger, value types, and unknown keys by
  key path. With no `cadence:`, nothing changes: no state file, the SPEC-0379 clock as today,
  and `NW_ALL_TIERS` as in SPEC-0490.
- [ ] Ff-merging a two-commit branch without reusing the loop gate increases `merges_since` by
  1 and records a file that changed only in the first commit. A `pr` close does not count. A
  diff failure marks every `on_paths` job hit.
- [ ] The state survives a restart. An unreadable file is moved aside and reseeded.
- [ ] `due` covers each of these cases:
  - each trigger returns its reason;
  - a green job at the same sha is not due unless forced;
  - a red job at the same sha is not due at `RETRY_S - 1 s` and is due once `RETRY_S` has
    passed;
  - a never-run job becomes due from `every_hours` after `seeded_at`.
- [ ] Merges during a run stay counted (snapshot consumption).
- [ ] Calling `cadence.run` while `run_lock` is held raises `Busy` without calling
  `_prepare_worktree`. A running cadence job makes `SN.running` true.
- [ ] The runner starts at most one background run, only when no campaign is waiting for a
  slot, and on a worker pool when one is placeable. A transport failure never runs locally.
- [ ] A project name containing a space yields a campaign id that `validate_header` accepts. A
  registered project with `cadence:` and no sentinel state file still gets its due job started.
- [ ] With a `cadence:tui` episode open:
  - `on_red` for `cadence:remote` leaves the source and failing ids unchanged;
  - `on_green` for `sentinel` leaves the episode open;
  - `step()` never returns `localize`.
- [ ] A fix campaign with `cadence_job: tui` runs the tui command at close even when the branch
  touches none of the job's `on_paths`. A sentinel close list for the same project does not
  include it.
- [ ] A project with `cadence:` skips the SPEC-0379 clock.
- [ ] `nw gate cadence status` (table and `--json`) and `nw gate cadence run JOB` work.
- [ ] nightwright's `.nw/project.yaml` declares the five jobs.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_cadence.py` (new): schema validation, `record_merge`, seeding, every
    `due` case, consumption, unreadable state, the `run` lock and placement with a faked
    `run_detached`, the dispatch id, and events.
  - `tests/test_agentic_close.py`: the merge hook on a non-reused two-commit ff-merge and on a
    `pr` close.
  - `tests/test_agentic_sentinel.py`: the runner step (oldest due job, one in flight, campaigns
    first, the `registered_projects` scan), the clock skipped for a cadence project, and `Busy`.
  - `tests/test_agentic_redmain.py`: source on open, cross-source `on_red` and `on_green`, a
    legacy episode, and `step()` never localizing for a cadence source.
  - `tests/test_agentic_gates.py`: `run_all` appends the job gate only for `cadence_job`.
  - The `nw gate` CLI test module: `cadence status` (table and JSON) and `cadence run`.
  - `tests/test_agentic_dogfood_config.py`: the five jobs validate.
- **Manual verification:**
  - after merge and `nw queue restart`, `nw gate cadence status` shows the five jobs seeded;
  - `nw gate cadence run perf` dispatches to the real worker within one tick, and its event and
    log dir exist.

  Record both in *As shipped*.
- **Regression guard:** the SPEC-0379 and SPEC-0420 tests pass unchanged. Run the affected
  tests plus the files above.

## Rollout / migration

- nightwright's `cadence:` lands in the same campaign. `nw queue restart` picks it up.
- The state seeds on the first tick. No job runs before its `every_hours` from seeding unless
  merges or paths make it due.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
