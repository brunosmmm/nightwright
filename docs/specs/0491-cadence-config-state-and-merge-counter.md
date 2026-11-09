---
id: SPEC-0491
title: "Cadence config, merge accounting and due state"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0489
tags: [agentic, gates, cadence]
depends_on: [SPEC-0490, SPEC-0350, SPEC-0359, SPEC-0379]
---

## Context

After SPEC-0490, campaign gates skip the heavy tiers. The human wants each project to choose
when its heavy tests run: after every N merges, every H hours, when a merge touches given
paths, or after every merge. This slice is the data half: the `cadence:` config, the per-project
state, the merge counter and a read-only status command. SPEC-0493 (the runner trigger, the
red-main source and nightwright's five jobs) builds on it. The review of the original combined
spec asked for this split (size xl: schema, state machine, close hook, runner slot, red-main
and CLI). See SPEC-0489 for the evidence.

## Goals / Non-goals

**Goals**
- A `cadence:` list in `.nw/project.yaml`, validated.
- Per-project state on disk that survives restarts and counts every ff-merge, including merges
  that land before any runner tick.
- A pure `due` function and the begin/finish accounting a runner will call.
- `nw gate cadence status`.

**Non-goals**
- Starting any run, the runner, the red-main source, `nw gate cadence run`, and nightwright's
  jobs (all SPEC-0493).
- Bisecting a red cadence run; a dashboard tab.

## Decision

- A cadence job is a named command with triggers. nw knows nothing about pytest tiers.
- **Opt-in is a non-empty `cadence` list.** `load_project_config` may fill a missing key with
  `[]`, but an absent key and `cadence: []` write no state file, do not skip `SN.time_due`, and
  do not suppress `NW_ALL_TIERS`. SPEC-0490 uses the same predicate on the raw document,
  `bool(doc.get("cadence"))`, not key presence on the defaulted config. (Decided: key presence
  on a defaulted config would opt every project in.)
- `due` has its own backoff. It reads the job's own `last_ok` / `last_run_sha` / `last_run_at`
  and `SN.RETRY_S`; it does not call `SN.backoff`, which reads the project-wide
  `<project>.last.json`, and nothing here writes that file.

## Design

**Schema.** `agentic/project.py` adds a top-level `cadence` key:

```yaml
cadence:
  - name: fast-full          # unique, [a-z0-9-]+, not equal to a gate name
    cmd: uv run pytest -q -n 4 -m "not (integration or tui or remote or perf)"
    every_n_merges: 5        # int >= 1 (bool rejected)
    every_hours: 12          # number > 0 (bool rejected)
    on_paths: []             # globs, matched like SPEC-0350 `paths`
    always: false            # true: due after any merge
    timeout_s: 2400          # default 1200
    retries: 1               # optional, gate meaning (SPEC-0289), default 0
    retry_cmd: …             # optional
    results: {format: pytest}
```

A trigger is `every_n_merges`, `every_hours`, `always: true`, or a non-empty `on_paths`.
`always: false` and `on_paths: []` are not triggers; a job needs at least one. Unknown keys and
bad values are rejected by key path. `P.cadence_gate(job)` returns a gate dict with only gate
keys: `name`, `cmd`, `timeout_s`, `retries`, `retry_cmd`, `results`, `required: true` and
`when: close`, omitting optional keys the job did not set. It has no `paths`.

**State.** A new module, `agentic/cadence.py`, keeps
`<agentic>/cadence/<E._safe(project)>.json` with schema `nw.cadence.v1` and shape
`{schema, jobs: {<name>: {...}}}`, written under `<project>.json.lock` with `_atomic_write`. Per
job it holds:
- `merges_since`;
- `paths_hit`: only the paths that matched that job's `on_paths`, newest 20. The marker `"*"`
  means "unknown, treat as hit";
- `seeded_at`;
- `last_run_at`, `last_run_sha` and `last_ok` (`true`, `false` or `null` for failed);
- `last_green_at` and `last_green_sha`;
- `last_error`;
- `forced`;
- `inflight`: `{merges, paths}` while a run is in progress.

An unreadable file is renamed to `<project>.json.bad-<ts>`, journalled, and reseeded.

**Seeding.** `record_merge` and the runner tick both ensure a missing job row, setting
`seeded_at = now` and zeroing `merges_since`, `paths_hit`, `last_run_sha`, `last_ok` and
`forced`. `seeded_at` is not refreshed once set. `record_merge` ensures the row before it
counts, so a merge that lands before the first tick is counted (this avoids both a rollout
stampede and under-counting). An absent or empty `cadence` returns without creating the file.

**Merge hook.** It runs on every successful ff-merge in `close.py`: the `policy == merge` path,
right after `git.ff_merge`, docs-only merges included. It is **not** inside
`_record_reused_merge`, which runs only for reused closes.
- The call is `cadence.record_merge(cfg, project, merged, paths)`, with `paths` from
  `git.changed_files(<target_sha>, <merged>, renames=False)`, the whole range that landed.
- A diff failure (`GitError`) sets `paths_hit = ["*"]` for every job with a non-empty
  `on_paths`; `"*"` counts as a hit.
- A `pr` or `draft-pr` close does not ff-merge, so it does not count.

**Due rule.** `cadence.due(job, st, base_sha, now)` returns a reason or `None`. Reasons are
`forced`, `always`, `merges`, `paths`, `hours` and `retry`. It knows nothing about locks or
slots (the runner skips a busy project, SPEC-0493).
1. `forced` set: return `forced` immediately; it skips both rules below.
2. Last run **green** (`last_ok` true) and `base_sha == last_run_sha`: `None`.
3. Last run **red** (`last_ok` false) or **failed** (`last_ok` null) at that sha: `None` until
   `SN.RETRY_S` has passed since `last_run_at`, then `retry`, even when no trigger matches and
   the base has not moved.
4. A job that **has never run** (`last_run_sha` null) counts `every_hours` from `seeded_at` and
   does not wait for a merge.
5. Otherwise the first matching trigger wins, in this order: `always` and `merges_since > 0`;
   `merges_since >= every_n_merges`; `paths_hit` non-empty; `now - (last_run_at or seeded_at) >=
   every_hours`.

**Consumption.** `cadence.begin(cfg, project, job)`, under `<project>.json.lock`, persists
`inflight: {merges: merges_since, paths: list(paths_hit)}` and clears `forced`. It does not zero
the counters. `cadence.finish(cfg, project, job, ok, sha, error)` runs at every finish except an
interrupt (green, red, failed, transport failure or exception): under the same lock it sets
`merges_since = max(0, merges_since - inflight.merges)`, keeps only the `paths_hit` entries
that were not in `inflight.paths` (still newest 20), writes `last_*` fields (`last_ok` null for
failed), and deletes `inflight`. An interrupt writes nothing and leaves counters unconsumed. An
`inflight` left by a crash is applied the same way on the next `begin` / tick with `last_ok`
set to `null`, so `RETRY_S` applies. Merges and paths that land during the run stay counted.

**CLI** (`agentic/cli_gate.py`): `nw gate cadence status [--project P] [--json]`: one row per
job, with its triggers, `merges_since` out of N, the paths hit, the last run (age, sha, ok), the
due reason or `not due (next: …)`, and `running` (always `no` in this slice: no run exists
until SPEC-0493). The JSON schema is `nw.cadence.status.v1`: `{schema, project, jobs: [{name,
triggers, merges_since, every_n_merges, paths_hit, last_run_at, last_run_sha, last_ok, due,
next, running}]}`; the table has one line per job with exactly these fields in this order.

## Alternatives considered

- **Cron schedules.** They would run even when main has not moved.
- **A new scheduler.** Reusing the sentinel path is SPEC-0493.
- **Global (nw config) cadence.** Each project's suite is different.
- **Running a heavy job at close when `on_paths` matches.** That puts heavy tests back on the
  campaign's critical path.

## Acceptance criteria

- [ ] `cadence:` is validated: name (unique, `[a-z0-9-]+`, not a gate name), cmd, at least one
  trigger from the four defined, value types (bools rejected for the numeric triggers), unknown
  keys by key path, and `timeout_s` / `retries` defaults of 1200 / 0. `always: false` and
  `on_paths: []` alone fail the trigger rule.
- [ ] An absent key and `cadence: []` write no state file and leave `SN.time_due` and the
  SPEC-0490 `bool(doc.get("cadence"))` predicate reading "no cadence".
- [ ] Ff-merging a two-commit branch without reusing the loop gate increases `merges_since` by 1
  and records, in `paths_hit`, only a matching path that changed in the first commit. A `pr`
  close does not count. A `GitError` sets `paths_hit` to `["*"]` for every job with `on_paths`.
- [ ] A merge that happens before any runner tick creates the job row (with `seeded_at`) and is
  counted. `seeded_at` is not refreshed.
- [ ] The state survives a restart. An unreadable file is moved aside and reseeded.
- [ ] `due` covers each of these cases:
  - each of the six reason strings;
  - a green job at the same sha is not due unless forced;
  - a red or failed job at the same sha is not due at `RETRY_S - 1 s` and returns `retry` once
    `RETRY_S` has passed, with no trigger matching;
  - `forced` returns `forced` for a green same-sha job and inside `RETRY_S`;
  - a never-run job becomes due from `every_hours` after `seeded_at`.
- [ ] Consumption: `begin` persists `inflight` and clears `forced` without zeroing counters; a
  merge and a matching path that arrive during the run stay counted after `finish`; an
  interrupt leaves everything unconsumed; a crash-left `inflight` is applied with `last_ok`
  null.
- [ ] `nw gate cadence status` (table and `--json`, schema `nw.cadence.status.v1`) prints the
  fields listed in *Design* for every job.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_cadence.py` (new): schema validation, the opt-in predicate,
    `record_merge`, seeding (including pre-tick), every `due` case, `begin` / `finish`
    consumption, unreadable state and the status output.
  - `tests/test_agentic_close.py`: the merge hook on a non-reused two-commit ff-merge, on a
    `pr` close, and with a faked `GitError`.
  - The `nw gate` CLI test module: `cadence status` (table and JSON).
- **Manual verification:** a project with a `cadence:` list merges a branch and
  `nw gate cadence status` shows the counters moving. Record it in *As shipped*.
- **Regression guard:** the SPEC-0379 tests pass unchanged. Run the affected tests plus the
  files above.

## Rollout / migration

- Nothing runs in this slice; projects without `cadence:` are unaffected (no file written).
- The state seeds on the first merge or tick.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
