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
- A pure `due` function and the ensure/begin/finish/abort/reconcile accounting a runner will
  call.
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
    retry_cmd: uv run pytest -q --lf {failing}   # optional
    results: {format: pytest}
```

The allowed job keys are exactly `name`, `cmd`, `every_n_merges`, `every_hours`, `on_paths`,
`always`, `timeout_s`, `retries`, `retry_cmd`, `results`. `name` full-matches `[a-z0-9-]+`, is
unique in the list and is not equal to any gate name. `cmd` is a required non-empty string.
`retries`, `retry_cmd` and `results` are validated by the existing `_check_gate_retry`
(`retries` an integer 0-3, bool rejected; `retry_cmd` contains `{failing}` and requires
`results: {format: pytest}`).

A trigger is `every_n_merges`, `every_hours`, `always: true`, or a non-empty `on_paths`.
`always: false` and `on_paths: []` are not triggers; a job needs at least one. Unknown keys and
bad values are rejected by key path. `P.cadence_gate(job)` returns `name`, `cmd`, `timeout_s`
(the job's value or 1200), `retries` (the job's value or 0), `required: true` and `when: close`,
plus `retry_cmd` and `results` only when the job set them. It has no `paths` and no `cwd`.

**State.** A new module, `agentic/cadence.py`, keeps
`<agentic>/cadence/<E._safe(project)>.json` with schema `nw.cadence.v1` and shape
`{schema, jobs: {<name>: {...}}}`, written under `<project>.json.lock` with `_atomic_write`. The
document also holds `last_merge_sha` (the merged sha last counted). Every time stamp is a
timezone-aware ISO-8601 string from `store.now_iso`, parsed with `fromisoformat` before any
subtraction. Every function that stamps takes `now=None` (epoch seconds, defaulting to the clock,
the same unit as `SN._now`) so tests can freeze it.

The initial row of a job is exactly:

```
{merges_since: 0, paths_hit: [], seeded_at: <ISO now>, last_run_at: null, last_run_sha: null,
 last_ok: null, last_green_at: null, last_green_sha: null, last_error: null, forced: false}
```

with no `inflight` key. `seeded_at` is never changed once set. `paths_hit` holds only paths that
matched that job's `on_paths`; the marker `"*"` means "unknown, treat as hit". `last_ok` is
`true` (green), `false` (red) or `null` (failed, or never run when `last_run_sha` is also null).
`inflight`, present only while a run is in progress, is `{merges, paths, sha, forced}`.

**Unreadable state.** A missing file is a fresh `{schema: nw.cadence.v1, jobs: {}}` and is not an
error. A file that is not valid JSON, is not an object, has a schema other than `nw.cadence.v1`,
or whose `jobs` is not an object is renamed to `<E._safe(project)>.json.bad-<ts>` (`ts` from
`store.now_iso` with colons replaced) and then treated as fresh. That path calls
`queue.journal(cfg, "cadence_unreadable", project=project)` and does not create job rows itself;
the caller runs `ensure`. An `OSError` while reading is not a bad file: it propagates (to the
close hook's finding, below, or journalled the same way from `ensure`, `reconcile` and status)
and the file is left in place.

**Seeding.** `cadence.ensure(cfg, project, now=None)` creates each missing job row and never
resets a row that exists. `record_merge` calls it before counting, so a merge that lands before
the first tick is counted (this avoids both a rollout stampede and under-counting); SPEC-0493's
tick calls it too. This slice does not add the tick. An absent or empty `cadence` returns without
creating the file.

**Merge hook.** The hook is the first thing after a successful `git.ff_merge` in `finish_close`'s
`policy == merge` path, docs-only merges included, and it is not inside `_record_reused_merge`
(which runs only for reused closes).
- It reads paths with `git.changed_files(root, target_sha, merged, renames=False)` on the main
  checkout (the range of everything that landed). On `GitError` from that diff it passes
  `paths=None`.
- `cadence.record_merge(cfg, project, merged, paths, now=None)` increments `merges_since` by 1
  for every job. `paths=None` appends `"*"` to the `paths_hit` of each job with a non-empty
  `on_paths`; otherwise it appends every matching path (see *Consumption*). A call whose `merged`
  equals the stored `last_merge_sha` is a no-op for every job (a resumed close re-running
  `ff_merge` on an already fast-forwarded main must not count twice).
- The hook catches `GitError`, `ValueError` and `OSError` from the diff and from `record_merge`,
  appends a finding `{persona: close, severity: minor, gate: cadence, title: "cadence merge not
  recorded", detail: <error>}`, and does not raise, so `finish_close` still returns MERGED (main
  has already fast-forwarded, and `_apply_close_result` would otherwise park a landed close),
  as `_record_reused_merge` does.
- A `pr` or `draft-pr` close does not ff-merge, so it does not count.

**Due rule.** `cadence.due(job, st, base_sha, now)` returns a reason or `None`. `now` is epoch
seconds (as `SN._now`); `seeded_at` and `last_run_at` are parsed from ISO before subtracting.
Shas are full `rev-parse` hex, compared for equality. A trigger the job did not configure cannot
match, and a missing `every_hours` is not zero. Reasons are `forced`, `always`, `merges`, `paths`,
`hours` and `retry`. It knows nothing about locks or slots (the runner skips a busy project,
SPEC-0493). The rules apply in this order:
1. `forced` is true: return `forced`, without applying the same-sha rules or the trigger list.
2. `last_ok` is true and `base_sha == last_run_sha`: return `None`. A green job stays idle while
   that sha is unchanged, so `every_hours` is not a cron.
3. `last_run_sha` is set, `base_sha == last_run_sha` and `last_ok` is false or null: return
   `None` while `now - parsed(last_run_at) < SN.RETRY_S`, and `retry` once it is `>= SN.RETRY_S`,
   even when no trigger matches. This return happens before the trigger list, including when a
   trigger also matches. A never-run row has `last_run_sha` null and is not this case.
4. Otherwise the first matching trigger wins, for a never-run job and for a job that has run:
   `always` is true and `merges_since > 0` -> `always`; `every_n_merges` is set and
   `merges_since >= every_n_merges` -> `merges`; `paths_hit` is non-empty (an entry `"*"` counts)
   -> `paths`; `every_hours` is set and `now - parsed(last_run_at or seeded_at) >= every_hours *
   3600` -> `hours` (epoch seconds against hours times 3600). A never-run job therefore counts
   merges from the moment it was seeded and `every_hours` from `seeded_at`.

**Consumption.** All functions take the state lock.
- `paths_hit` is append-only, newest last, capped at 20 by dropping the oldest. `record_merge`
  appends every matching path, including one already in the list.
- `cadence.begin(cfg, project, job, sha, now=None)` calls `reconcile` first, then stores
  `inflight: {merges: merges_since, paths: list(paths_hit), sha: sha, forced: <the flag it is
  about to clear>}` and sets `forced` false. It does not zero counters. `inflight.paths` is a
  copy of the list at that moment: a prefix of what the list becomes if later hits are appended.
- `cadence.finish(cfg, project, job, ok, sha, error, now=None)` runs at every finish except an
  interrupt (green, red, failed, transport failure or exception). It sets `merges_since =
  max(0, merges_since - inflight.merges)`, drops the `inflight.paths` prefix of `paths_hit` and
  keeps the suffix (capped at 20), so a path touched again during the run stays, and deletes
  `inflight`. It sets `last_run_at` to now (ISO), `last_run_sha` to `sha` and `last_ok` to `ok`
  (true green, false red, null failed). When `ok` is true it also sets `last_green_at` and
  `last_green_sha` to those values and `last_error` to null; otherwise `last_green_*` are left
  unchanged and `last_error` is `error`. Nothing in this slice reads `last_green_*`; they record
  the last green run.
- `cadence.abort(cfg, project, job)` deletes `inflight` and restores `forced` from the snapshot;
  it does not change counters or `last_*`. SPEC-0493's interrupt path calls it (it does not skip
  every write). A crash does not call it.
- `cadence.reconcile(cfg, project, now=None)` treats each leftover `inflight` (a crash) as a
  failed finish: counters are consumed as in `finish`, `last_ok` is null, `last_run_sha` is
  `inflight.sha`, `last_run_at` is now, `last_error` is `"crashed"`, `inflight` is deleted and
  `forced` is left as it stands. `begin` and SPEC-0493's tick scan call `reconcile` before `due`.
  A crash while testing a new sha B therefore comes back as a failed run at B: `due` at B is
  `None` until `RETRY_S`, then `retry`.
- A diff failure appends `"*"` and does not replace the list; `due` treats any non-empty list,
  one containing `"*"` included, as a `paths` hit.

**CLI** (`agentic/cli_gate.py`): `nw gate cadence status [--project P] [--json]`. `--project`
defaults the way `nw gate run` does (the cwd's registered project) and the command exits 2 when
the project or its config cannot be loaded. Status does not call `ensure` and does not create the
state file. With no file, each configured job prints `merges_since` 0, `paths_hit` `[]`, null
last-run fields, `due` null, `running` `"no"` and `next` `"not seeded"`. With a file:
- `triggers` is the subset of `["always", "every_n_merges", "on_paths", "every_hours"]` that are
  real triggers on that job, in that order; `every_n_merges` is the integer or null;
- `due` is the reason from `cadence.due` or null; `next` is null when `due` is set;
- when `due` is null because the job is green at this sha, `next` is `"when the base moves"`;
  otherwise `next` is the earliest waiting trigger: `"on the next merge"` (`always` true and
  `merges_since` 0), `"in <N - merges_since> merges"` (`every_n_merges` set, count short), `"on a
  matching path"` (`on_paths` non-empty, `paths_hit` empty), `"at <ISO>"` (the `every_hours`
  deadline, `seeded_at` or `last_run_at` plus `every_hours`); with several waiting, the soonest,
  a merge-count or path wait counting as sooner than a future hour;
- `running` is the string `"no"` in this slice (no run exists until SPEC-0493).

The table is one line per job with the keys `name, triggers, merges_since, every_n_merges,
paths_hit, last_run_at, last_run_sha, last_ok, due, next, running` in that order; `--json` is
`{schema: nw.cadence.status.v1, project, jobs: [those objects]}`.

## Alternatives considered

- **Cron schedules.** They would run even when main has not moved.
- **A new scheduler.** Reusing the sentinel path is SPEC-0493.
- **Global (nw config) cadence.** Each project's suite is different.
- **Running a heavy job at close when `on_paths` matches.** That puts heavy tests back on the
  campaign's critical path.

## Acceptance criteria

- [ ] `cadence:` is validated: name (unique, `[a-z0-9-]+`, not a gate name), cmd, at least one
  trigger from the four defined, value types (bools rejected for the numeric triggers), unknown
  keys by key path, and `timeout_s` / `retries` defaults of 1200 / 0 (both present in
  `P.cadence_gate` output even when omitted). `always: false` and `on_paths: []` alone fail the
  trigger rule. `retry_cmd` without `{failing}` and `retries: 4` are rejected.
- [ ] An absent key and `cadence: []` write no state file and leave `SN.time_due` and the
  SPEC-0490 `bool(doc.get("cadence"))` predicate reading "no cadence".
- [ ] Ff-merging a two-commit branch without reusing the loop gate increases `merges_since` by 1
  and records, in `paths_hit`, only a matching path that changed in the first commit. A `pr`
  close does not count. A `GitError` appends `"*"` to `paths_hit` on every job with a non-empty
  `on_paths`, and `merges_since` still increases by 1.
- [ ] Merge hook robustness: a raising `record_merge` still leaves the close MERGED (with a
  `cadence merge not recorded` finding) and `merges_since` 1 where it succeeded earlier; a second
  call with the same `merged` sha leaves `merges_since` at 1; an absent or empty `cadence`
  creates no file; the `changed_files` call starts with the repo root.
- [ ] A merge that happens before any runner tick creates the job row (with `seeded_at`) and is
  counted. `ensure` does not reset an existing row; `seeded_at` is not refreshed. The initial
  row has every field of the *State* section, with no `inflight` key.
- [ ] The state survives a restart. A missing file is fresh with no journal entry. An invalid
  JSON, non-object, foreign-schema or non-object-`jobs` file is renamed to
  `<project>.json.bad-<ts>`, `cadence_unreadable` is journalled and the state is fresh; an
  `OSError` while reading leaves the file in place.
- [ ] `due` covers each of these cases:
  - each of the six reason strings;
  - a green job at the same sha is not due unless forced;
  - a red or failed job at the same sha is not due at `RETRY_S - 1 s` and returns `retry` once
    `RETRY_S` has passed, with no trigger matching, and also when a trigger matches inside
    `RETRY_S` (returns `None`);
  - `forced` returns `forced` for a green same-sha job and inside `RETRY_S`;
  - a never-run job with `merges_since >= every_n_merges` returns `merges` while `every_hours`
    is still in the future;
  - a never-run job with only `every_hours: 12` returns `None` at `seeded_at + 12*3600 - 1 s`
    and `hours` at `seeded_at + 12*3600 s`;
  - a red same-sha job whose hours have also elapsed returns `retry`.
- [ ] Consumption: `begin(…, sha)` persists `inflight` (with `sha` and the `forced` snapshot) and
  clears `forced` without zeroing counters; a merge and a matching path that arrive during the
  run stay counted after `finish`, including `paths_hit ["a.py"]` at `begin` followed by a merge
  that appends `"a.py"` again, which leaves `["a.py"]` after `finish`; `abort` after `begin`
  restores `forced` and leaves `merges_since` unchanged; a leftover `inflight` whose sha is B,
  after a green run at A, reconciles to `last_ok` null, `last_run_sha` B and `last_error`
  `"crashed"`, and `due` at B is `None` until `RETRY_S` and `retry` after it.
- [ ] A red `finish` leaves `last_green_sha` unchanged and a later green `finish` sets
  `last_green_at` / `last_green_sha`, and clears `last_error`.
- [ ] `nw gate cadence status` (table and `--json`, schema `nw.cadence.status.v1`) prints the
  fields listed in *Design* for every job, with `triggers`, `due`, `next` and `running` as
  defined there (including `"when the base moves"` for a green job); with no state file it
  prints the unseeded values (`next` `"not seeded"`) and does not create the file; it exits 2 for
  an unloadable project.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_cadence.py` (new): schema validation (including `retry_cmd` / `retries`),
    the opt-in predicate, `record_merge` (idempotence), seeding (including pre-tick and
    `ensure`), every `due` case with a frozen `now`, `begin` / `finish` / `abort` / `reconcile`
    consumption, last-green fields, unreadable state and the status output.
  - `tests/test_agentic_close.py`: the merge hook on a non-reused two-commit ff-merge, on a
    `pr` close, with a faked `GitError` from the diff, and with a raising `record_merge` (close
    still MERGED, finding appended).
  - `tests/test_gate_jobs.py`: `cadence status`, table and `--json`, including a project with
    no state file.
- **Manual verification:** a project with a `cadence:` list merges a branch and
  `nw gate cadence status` shows the counters moving. Record it in *As shipped*.
- **Regression guard:** the SPEC-0379 tests pass unchanged. Run the affected tests plus the
  files above.

## Rollout / migration

- Nothing runs in this slice; projects without `cadence:` are unaffected (no file written).
- The state seeds on the first merge (`record_merge` calls `ensure`) or on SPEC-0493's first
  tick. `nw gate cadence status` never seeds.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
