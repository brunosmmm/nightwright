---
id: SPEC-0489
title: "Test tiers: campaign gates run the fast tier only; heavy tiers run on a per-project cadence"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, testing, performance, sentinel, cadence]
depends_on: [SPEC-0328, SPEC-0359, SPEC-0378, SPEC-0379, SPEC-0381, SPEC-0420, SPEC-0421]
---

## Context

The human has asked many times for less testing per campaign. SPEC-0328 / SPEC-0357 / SPEC-0378
built an affected-test selector (`tools/affected_tests.py`) for the loop and close gates, and
SPEC-0379 moved the full suite to a 6-hourly sentinel on main. In practice the gates still run
most of the suite on every campaign.

**Evidence (2026-11-09, read-only).**

- Real campaign gates, 56 campaigns created 2026-11-03..09, 250 `pytest-affected` / `pytest`
  gate runs (first `affected_tests:` line of each log under `<agentic>/campaigns/*/gates/`):
  39 runs (16 %) were the full suite, **0** selected under 30 % of test files, 102 selected
  30-50 %, 109 selected 50 % or more. The mean partial selection was **53 %** of test files.
  Every recent spec campaign's reason starts with `reference scan for docs/LEDGER.md; reference
  scan for docs/specs/NNNN-….md; tree readers of …`.
- Full-suite reasons in that window: `NW_AFFECTED_FULL is set` 22 (SPEC-0420 red-main FIX and
  MERGE campaigns), a fixture file no test references 14, `conftest.py changed` 3.
- Standalone close-tier gate jobs on the box: GATEJOB-0997 (`conftest.py changed`, full) ran
  8060 tests in 589 s; GATEJOB-1037 (SPEC-0487, 10 changed files) selected 261/326 files, 6940
  tests, 1316 s of gate time. A box gate is 10-25 minutes; a campaign pays it at least twice
  (loop and close), more with fix rounds.
- Per-file widening, measured with `--changed` on today's tree (326 test files):

  | Changed file | Test files selected | Why |
  |---|---|---|
  | `docs/specs/0487-….md` (any new spec) | 119 | no test names the new basename, so `_referencing` falls back to every module with a `specs` directory reference (67 modules), then their nearest tests |
  | `docs/LEDGER.md` | 27 | 17 referencing modules |
  | `src/nightwright/agentic/queue.py` | 144 | 98 test files import `queue` directly (nearest tests = 139) |
  | `src/nightwright/agentic/lease.py` | 129 | reaches `queue`'s importers |
  | `src/nightwright/store.py` | 295 | 118 direct importers of `agentic.store`, plus the removed-module rule |
  | `src/nightwright/agentic/sentinel.py` | 11 | a leaf: the selector works as designed |
  | `tests/conftest.py` | full | root conftest |

  The tree-guard and tree-reader rules (SPEC-0421) add only 6-10 files. The selector is
  correct; the import graph is dense. A typical campaign touches a spec, the ledger and one or
  two hub-adjacent modules (`queue`, `store`, `events`, `cli`), and the union is half the suite.
- No test is categorised. 5942 test functions (about 8000 items) in 326 files; markers exist
  only for `slow` (2 files, SPEC-0392) and `tree_guard` (13 files, SPEC-0421). Heavy groups by
  file name: 26 `test_tui_*` files (392 tests), 5 `remote*` files (245 tests), 9 `*e2e*` /
  `*integration*` files (78 tests), 2 race files, 1 TUI budget file. Box durations per group are
  in *Measurements* below.

Selection precision has a ceiling here: making every campaign gate exact needs coverage data
(rejected in SPEC-0328 for good reasons). The lever the human asked for is different: most
tests should not be candidates for a campaign gate at all, and the heavy groups should run on a
schedule the project chooses.

## Goals / Non-goals

**Goals**
- Every test belongs to exactly one tier, declared in the test module; a guard fails when one
  does not.
- Campaign loop and close gates run the **fast** tier only, intersected with the affected
  selection, under a hard time budget.
- A project declares, in `.nw/project.yaml`, cadenced jobs for the heavy tiers: after every N
  merges, every H hours, when a merge touches given paths, or after every merge.
- nw keeps the counters and timestamps per project and job on disk, and the runner starts due
  jobs itself, in the background, on worker-box gate slots, never blocking a campaign.
- A red cadenced job opens the existing SPEC-0420 red-main episode and fix campaign.
- `nw gate cadence status` shows each job's triggers, counters, last run and whether it is due.
- The affected selector stops widening on spec/ledger edits and on hot modules.
- A project without the new config behaves exactly as today.

**Non-goals**
- Coverage-based or predictive selection (SPEC-0328 phase 3 stays open).
- Bisecting a red cadenced run.
- Tiering other projects' tests: they choose their own gate commands and cadence commands; nw
  only runs them.
- A dashboard tab. The status command and events are enough for the first cut.

## Decision

1. **Five tiers, module-level markers.** `fast`, `integration`, `tui`, `remote`, `perf`. Each
   test module sets `pytestmark = pytest.mark.<tier>` (or a list holding exactly one tier plus
   non-tier markers such as `tree_guard`). A single test may carry a heavier tier marker than
   its module; a heavier marker anywhere excludes the test from the fast run. The existing
   `slow` marker is folded into `integration` and unregistered.
2. **Fast means "not heavy".** Gates exclude the heavy tiers with
   `-m "not (integration or tui or remote or perf)"`, added by a new
   `tools/affected_tests.py --exclude-marks integration,tui,remote,perf` flag. The selector
   appends the `-m` to every run it makes (the selection, a full fallback, the retry), maps
   pytest's exit 5 (nothing collected after the filter) to 0, and drops the exclusion only when
   `NW_ALL_TIERS` is truthy.
3. **Campaign gates (nightwright).** `pytest-affected` (loop) and `pytest` (close) pass
   `--exclude-marks`. Their `timeout_s` drops from 2400 to 900 (loop) and 1200 (close): the
   fast tier's full fallback fits in that on the box (*Measurements*). The time budget is the
   gate timeout; a timeout is a red gate like any other.
4. **Cadence config.** A new top-level `cadence:` list in `.nw/project.yaml`; each entry is a
   named job with its own `cmd` and any of `every_n_merges`, `every_hours`, `on_paths`,
   `always`. nw does not know about pytest tiers: a job is a command. nightwright's jobs run
   `uv run pytest -m <tier>`, plus a `fast-full` job running the whole fast tier, which is what
   catches the affected selector's misses.
5. **State and trigger.** `<agentic>/cadence/<project>.json` holds per-job counters and
   timestamps (flock + atomic write). The merge path increments them; the runner's sentinel step
   starts the most overdue due job, one sentinel-or-cadence run at a time, only when no campaign
   waits for a gate slot, on a worker slot via `remote_gates.run_detached`. This is the SPEC-0379
   sentinel path with a different gate list, not a new runner.
6. **Red.** A red cadenced run calls `redmain.on_red` like the sentinel (episode, fix campaign,
   merge hold per SPEC-0420). The episode records its `source` (`sentinel` or `cadence:<job>`):
   only a green run of the same source closes it. The fix campaign gets the red job as an extra
   close gate, so the fix is proven on the failing tier.
7. **The sentinel with cadence.** A project with a non-empty `cadence:` skips the SPEC-0379
   clock trigger (its cadence jobs replace it) and its sentinel runs keep the tier exclusion.
   A project without `cadence:` gets `NW_ALL_TIERS=1` on sentinel runs and `full_suite`
   campaigns, so its forced-full runs stay full: today's coverage.
8. **Tighter selection.** A new spec file and the ledger select only the tests that name them
   exactly (no directory-reference fallback); a hot src module (more than 30 nearest test files)
   narrows to the tests that mention a top-level name its diff changed. The `fast-full` cadence
   job is the safety net for what this misses.

## Design

### Slices (ship in this order)

1. **Tier markers + fast-only campaign gates** (highest value: removes TUI, remote,
   integration and perf from every campaign gate at once).
2. **Cadence: config, state, runner trigger, status CLI, red handling.**
3. **Tighter affected selection** (spec/ledger needles, hot-module symbol narrowing).

Each slice is one campaign and leaves `main` consistent. Until slice 2 lands, the heavy tiers
run only in the SPEC-0379 6-hourly sentinel, which slice 1 keeps full through `NW_ALL_TIERS`:
coverage is never lower than today's sentinel coverage at any point.

### Slice 1: tier markers

**Taxonomy** (what goes where; the classifier below applies it, a human reviews the diff):

| Tier | Members | Rule |
|---|---|---|
| `tui` | `test_tui_*`, `test_dashboard_e2e`, any module that drives Textual (`run_test`, `Pilot`, `tui_harness`, `tui_budget`) | Textual apps are slow to boot and the TUI rarely changes with agentic work |
| `remote` | `test_remote_*`, `test_pull_worker*`, `test_worker*` that start a loopback worker or ssh transport (`remote_loopback`, `remote_support`, `remote_workers_golden`) | real transport, sockets, subprocess workers |
| `perf` | `test_tui_budget`, any module asserting a wall-clock budget (`perf`, `budget` in the module, `time.monotonic` deltas compared to a constant) | timing-sensitive, noisy under load, meaningful only on a quiet box |
| `integration` | `*_e2e`, `*_integration`, `*_race`, `test_gate_timeout_tree`, `test_affected_tests` (nested pytest; was `slow`), any module whose box time exceeds the fast budget | end-to-end flows and real process trees |
| `fast` | everything else | in-process units, tmp git repos are fine |

**Fast budget.** A module is `fast` only if, in the measuring run, its summed test time is at
most 20 s and no test exceeds 2 s; otherwise it is `integration` (or a named heavy tier if the
rule above matches). Single slow tests in an otherwise fast module get a function-level
`@pytest.mark.integration` instead of moving the module.

**`tools/test_tiers.py`** (stdlib only, like `affected_tests.py`):
- `--classify DURATIONS_LOG` prints the proposed tier per module from the name rules and a
  pytest `--durations=0` log (the box measuring run, produced with `nw gate run --cmd`).
- `--apply` writes or rewrites the module's `pytestmark` line (merging with an existing list),
  idempotently; it is a one-off for the mechanical pass and stays for new files.
- `--check` exits 1 listing modules with no tier, more than one module tier, or an unknown tier
  name. Helper modules (not `test_*.py`) are exempt.

**Guard.** `tests/test_test_tiers.py` (`tree_guard`, so `--guards` always selects it) runs the
`--check` logic over the tree. `pyproject.toml` registers the five tier markers (and drops
`slow`); `--strict-markers` is added to `addopts` so a typo fails collection.

**`tools/affected_tests.py`:**
- `--exclude-marks A,B,...` makes every `--run` invocation append `-m "not (A or B ...)"`.
  Applies to the selection, the bare full fallback and the targeted retry the gate runs.
  (`retry_cmd` is the gate's own command: nightwright's `retry_cmd` adds the same `-m`.)
- `NW_ALL_TIERS` truthy: the flag is ignored (logged as `affected_tests: all tiers`).
- Exit 5 from the `--run` command (no tests collected) becomes 0 with
  `affected_tests: no fast tests in the selection`.
- The JSON report gains `exclude_marks` and `all_tiers`.

**`gates.py` / `worker.py`:** `gate_env` adds `NW_ALL_TIERS=1` when the state is a sentinel run
or a `full_suite` campaign **and** the project has no `cadence:` section; the worker's
`ENV_ALLOWLIST` gains `NW_ALL_TIERS`. The detached sentinel dispatch passes the same env.

**`.nw/project.yaml` (nightwright):** both pytest gates and their `retry_cmd` gain the
exclusion; `timeout_s` 900 (loop) and 1200 (close). `docs-tests` is unchanged.

### Slice 2: cadence

**Schema** (`agentic/project.py`, top-level key `cadence`, default `[]`):

```yaml
cadence:
  - name: fast-full          # unique, [a-z0-9-]+
    cmd: uv run pytest -q -n 4 -m "not (integration or tui or remote or perf)"
    every_n_merges: 5        # int >= 1
    every_hours: 12          # number > 0
    on_paths: []             # globs, matched like SPEC-0350 `paths`
    always: false            # true: due after any merge
    timeout_s: 2400          # gate defaults otherwise (retries, retry_cmd, results, remote)
    results: {format: pytest}
```

A job needs at least one trigger key; unknown keys and bad values are rejected by key path, as
for gates. `retry_cmd` / `retries` / `results` keep their gate meaning, so a flaky test is
retried before the run counts as red (SPEC-0289). A job is turned into a gate dict
(`P.cadence_gate(job)`) and run by the existing gate runner.

**nightwright's jobs** (defaults chosen from *Measurements*; tune from the cadence events):

| Job | Command (`uv run pytest -q`) | Triggers |
|---|---|---|
| `fast-full` | `-n 4 -m "not (integration or tui or remote or perf)"` | every 5 merges, every 12 h |
| `integration` | `-n 4 -m integration` | every 5 merges, every 24 h, `on_paths: [src/nightwright/agentic/queue.py, close.py, gates.py, runner.py, sentinel.py, redmain.py]` |
| `tui` | `-n 4 -m tui` | every 10 merges, every 24 h, `on_paths: [src/nightwright/tui/**, src/nightwright/agentic/dashboard/**]` |
| `remote` | `-n 2 -m remote` | every 24 h, `on_paths: [src/nightwright/agentic/remote_*.py, worker*.py, pull_*.py, gate_jobs.py]` |
| `perf` | `-n 0 -m perf` | every 24 h |

Why these: at the 2026-11 merge rate (about 10-20 merges a day), every 5 merges is 2-4 fast-full
runs a day, close to SPEC-0379's 4-a-day sentinel but on the fast tier only; the heavy tiers run
once a day, sooner when a merge touches the code they cover. The window in which an affected-
selection miss stays undetected is at most 5 merges.

**State** (`agentic/cadence.py`, new): `<agentic>/cadence/<project>.json`, written under
`<project>.json.lock` with `_atomic_write`:

```json
{"schema": "nw.cadence.v1",
 "jobs": {"fast-full": {"merges_since": 3, "paths_hit": [], "seeded_at": "…",
                        "last_run_at": "…", "last_run_sha": "…", "last_ok": true,
                        "last_green_at": "…", "last_green_sha": "…", "last_error": null,
                        "forced": false}}}
```

- **Merge hook.** `close.py`, next to `SN.record_merge`: `cadence.record_merge(cfg, project,
  merged_sha, paths)` increments `merges_since` for every job and appends the paths matching a
  job's `on_paths` to its `paths_hit` (capped at 20). `paths` is `git diff --name-only
  <merged>^1 <merged>`; a failure to diff marks every `on_paths` job hit (fail toward running).
- **Seeding.** A job seen for the first time gets `seeded_at = now` and is not due by
  `every_hours` until that long after seeding; adding a cadence does not start every job at once.
- **Unreadable state** is renamed to `<project>.json.bad-<ts>`, journalled, and replaced with a
  seeded state. Restarts lose nothing: the file is the only state.
- **Due rule** (`cadence.due(job, st, base_sha, now)` returns the reason or None). Not due when
  the job's run lock is held, or `base_sha == last_run_sha` and the job is not `forced`.
  Otherwise due, in this order, when: `forced`; `always` and `merges_since > 0`;
  `merges_since >= every_n_merges`; `paths_hit` non-empty; `now - (last_run_at or seeded_at) >=
  every_hours`. `SN.backoff`'s rule applies per job: a red or failed run at the same sha waits
  `RETRY_S`.
- **Consumption.** At run start the job's `merges_since` and `paths_hit` are snapshotted; at the
  end (green, red or failed) the snapshot is subtracted, so merges during the run still count.
  `forced` is cleared at start.

**Trigger** (`queue._sentinel_step`): after the existing sentinel candidates, for each project
with a `cadence:` section, compute due jobs; start the one with the oldest
`last_run_at or seeded_at`. The step keeps its rules: one sentinel-or-cadence run in flight per
runner, started only when no campaign waits for a gate slot. A project with `cadence:` skips
`SN.time_due` (decision 7); SPEC-0359's reuse-suspect trigger is kept.

**Run** (`cadence.run(cfg, project, job)`): reuses `sentinel._prepare_worktree` (the detached
base worktree), `sentinel._placement` (worker pools while a worker is placeable, local only
when none is or the job is remote-ineligible) and `remote_gates.run_detached` with the job's gate
dict, under a per-job flock `<agentic>/sentinel/<project>.cadence-<job>.lock`. The dispatch
campaign id is `sentinel-<project>-<job>` (accepted by the worker's header check; it also makes
`affected_tests.py` force full if a project's job calls it). Logs go to
`<agentic>/sentinel/<project>.logs/cadence-<job>/`. A transport failure is a failed run with the
SPEC-0379 backoff and no local fallback.

**Events.** Each finished run appends a `cadence` event `{project, job, ok, sha, reason,
failing[:50], duration_s}` and journals `cadence_done` / `cadence_failed` / `cadence_deferred`
like the sentinel.

**Red** (`redmain`): `on_red(..., source="cadence:<job>")` and `on_green(..., source=...)`; the
episode stores `source`; `on_green` from another source leaves an open episode alone. The
existing sentinel passes `source="sentinel"`; an episode without `source` (pre-existing) is
treated as `sentinel`. The fix campaign's state gets `cadence_job: <job>`, and `gates_for` adds
that job's gate to the close tier for that campaign only. `full_suite` stays set, so the fast
gates of that campaign run their full fast tier.

**CLI** (`agentic/cli_gate.py`):
- `nw gate cadence status [--project P] [--json]`: one row per job: triggers, `merges_since` /
  N, paths hit, last run (age, sha, ok), due reason or `not due (next: …)`, running.
  JSON schema `nw.cadence.status.v1`.
- `nw gate cadence run JOB [--project P]`: sets `forced`; the runner starts it at its next tick.
  Prints that the run is queued, or that no runner is up.

### Slice 3: tighter affected selection

In default (non-strict) mode:
- **Specs and ledger.** `docs/specs/NNNN-*.md` and `docs/LEDGER.md` select only test modules
  whose text contains the file's exact basename (the needle), never the parent-directory
  readers. spec-lint (an `always` gate) validates spec files; the tests that read the spec tree
  are tree guards selected by `--guards` anyway. `docs/specs/SCHEMA.md`, `spec.schema.json`
  and `TEMPLATE*.md` keep today's rule.
- **Hot-module narrowing.** When a changed src module's nearest tests exceed
  `--hot-threshold` (default 30 files) and the diff against the merge base is available, the
  selector extracts the top-level names (functions, classes, module constants) whose definition
  lines intersect the diff hunks, and keeps only the nearest tests whose source contains one of
  those names as a word, plus the module's name-rule tests. It keeps the whole set when no name
  is found (a module-level change, an import edit), or when a changed name is private and
  referenced only inside the module (then the callers of the public names that use it are not
  known statically). The report adds `hot: {module: [names]}`.
- The fixture-data fallback (`no test references tests/fixtures/…`, 14 of the 39 full runs) is
  narrowed: a data file under a `tests/fixtures/<dir>/` tree selects the test modules that
  reference `<dir>` (the fixture root), not the full suite.

Measured target, to be re-run when slice 3 ships: replay the last 200 first-parent commits on
main through the selector and report the distribution next to SPEC-0378's.

## Measurements

Measured 2026-11-09 (box and campaign logs):

- **Box gate times.** GATEJOB-0997 full close tier: 8060 passed, 11 skipped, pytest 589 s at
  `-n 4`. GATEJOB-1037: 261/326 files, 6940 tests, 1316 s. GATEJOB-0995: 6121 tests, 316 s.
  The spread (316-1316 s for similar counts) is box load: campaign gates share the worker.
- **Per-test durations.** None were recorded anywhere (no `--durations` in any gate). A
  measuring run (`nw gate run --cmd "uv run pytest -q -n 4 --durations=0 --durations-min=0.3"`,
  GATEJOB-1058) ended `unavailable`: no worker slot within 300 s while campaign gates held the
  box. Slice 1's first step is that measuring run (retried until placed); the classifier's
  20 s / 2 s budget is applied to its log, and the per-tier totals are recorded in *As
  shipped*. Tier sizes by name rule alone: `tui` 26 files / 392 tests, `remote` 5 / 245,
  `integration` 11+ / 80+ (e2e, integration, race, nested pytest), `perf` 1. Together they are
  about 12 % of test functions, but they hold the slow per-test costs (Textual boot per test,
  loopback workers, nested pytest).
- **Selection.** The 250 real gate runs in *Context* are the distribution (mean partial 53 %,
  none under 30 %). Slice 3 re-runs a commit replay as its measurement.

## Alternatives considered

- **A central tier registry file (`tests/tiers.toml`) instead of `pytestmark`.** Less churn, but
  the tier is invisible at the test, the registry drifts from renames, and pytest's own `-m`
  needs a conftest hook to apply it. Module markers are standard pytest and greppable.
- **Explicit `-m fast` instead of `not (heavy)`.** A test that carries both the module's `fast`
  and a function-level `integration` would still run. Excluding heavy tiers is correct for
  overrides and for unmarked helper-collected items.
- **Fix selection only (no tiers).** The import graph is dense: queue, store, events and the CLI
  hubs are imported directly by 60-120 test files each. Exact selection needs coverage data,
  rejected in SPEC-0328 (per-worktree seeding, staleness after rebase). Tiers cut the candidate
  set by about half regardless of what changes.
- **A cron-like schedule.** Rejected as in SPEC-0379: running when main did not move wastes a
  slot. The due rule requires a moved base.
- **A new background scheduler.** The runner already has a one-in-flight, campaign-first,
  worker-placed background gate path (the sentinel); cadence jobs are more of the same.
- **Global (nw config) cadence.** The human asked for per-project config, and other projects'
  test suites have nothing in common with nightwright's.
- **Running heavy tiers at close when `on_paths` matches.** That puts the heavy tiers back on the
  campaign's critical path, which is what the human asked to remove. `on_paths` makes the job due
  after the merge instead.

## Acceptance criteria

Slice 1:
- [ ] Every `tests/test_*.py` declares exactly one tier marker at module level; `python3
  tools/test_tiers.py --check` exits 0; `tests/test_test_tiers.py` fails on a module with no
  tier, two tiers or an unknown tier, and is a `tree_guard`.
- [ ] `pyproject.toml` registers `fast`, `integration`, `tui`, `remote`, `perf` (no `slow`) and
  runs with `--strict-markers`.
- [ ] `affected_tests.py --exclude-marks …` appends the `-m "not (…)"` expression to the
  selection, the full fallback and the empty-selection path; exit 5 becomes 0;
  `NW_ALL_TIERS=1` drops the expression.
- [ ] nightwright's loop and close pytest gates (and their `retry_cmd`) exclude the heavy tiers;
  `timeout_s` 900 / 1200.
- [ ] A sentinel run and a `full_suite` campaign of a project without `cadence:` see
  `NW_ALL_TIERS=1`, locally and on the worker (allowlisted).
- [ ] A box gate of a typical spec campaign (one spec, the ledger, one src module) on the new
  config runs no `tui` / `remote` / `integration` / `perf` test (checked in a `nw gate run`
  log).

Slice 2:
- [ ] `cadence:` is validated (name, cmd, at least one trigger, value types; unknown keys by key
  path); absent ⇒ `[]` and no behavior change (no state file written, SPEC-0379 clock as today).
- [ ] `record_merge` increments every job's counter and records `on_paths` hits; the state file
  survives a restart and an unreadable file is moved aside and reseeded.
- [ ] `due` returns each trigger's reason; not due on an unmoved base unless forced; seeding
  delays `every_hours`; backoff after red/failed at the same sha.
- [ ] The runner starts at most one sentinel-or-cadence run, only when no campaign waits for a
  slot, on a worker pool when one is placeable; a transport failure never runs locally.
- [ ] Counters are consumed by snapshot: merges during a run stay counted.
- [ ] A red job opens a red-main episode with `source: cadence:<job>`; a green run of another
  source does not close it; the fix campaign's close gates include the job.
- [ ] A project with `cadence:` skips the SPEC-0379 clock and does not get `NW_ALL_TIERS`.
- [ ] `nw gate cadence status` (table and `--json`) and `nw gate cadence run JOB` work.
- [ ] nightwright's `.nw/project.yaml` declares the five jobs above.

Slice 3:
- [ ] A new spec file or the ledger selects only tests naming its basename (plus guards).
- [ ] A hot module change touching only named defs selects only the nearest tests naming one of
  them; a module-level change keeps the whole set; `--strict` is unchanged.
- [ ] A fixture data file selects its fixture root's readers instead of the full suite.
- [ ] A 200-commit replay is recorded in *As shipped* with the new distribution.

## Test plan

- **Automated tests:**
  - `tests/test_test_tiers.py`: the guard over the real tree; `--check` / `--apply` /
    `--classify` on a temp tree (no tier, two tiers, unknown tier, list form with `tree_guard`,
    idempotent apply, helper module exempt, durations log parsing and the 20 s / 2 s budget).
  - `tests/test_affected_tests.py`: `--exclude-marks` on the selection, the full fallback and the
    empty selection; exit 5 → 0; `NW_ALL_TIERS`; JSON fields. Slice 3: spec/ledger needles,
    hot-module narrowing (named def, module-level change, private name), fixture root.
  - `tests/test_agentic_gates.py` / `tests/test_remote_gates.py`: `gate_env` sets
    `NW_ALL_TIERS` only for sentinel / `full_suite` without `cadence:`; worker allowlist.
  - `tests/test_agentic_cadence.py` (new): schema validation; `record_merge` (counts, path
    hits, diff failure); seeding; every `due` reason and the unmoved-base / forced / backoff
    cases; snapshot consumption; unreadable state; `run` placement with a faked `run_detached`
    (worker, local-only, transport failure never local); events.
  - `tests/test_agentic_sentinel.py`: the runner step picks the most overdue job, one run in
    flight, campaign-first; a cadence project skips `time_due`.
  - `tests/test_agentic_redmain.py`: `source` on open; cross-source green does not close; legacy
    episode reads as `sentinel`; fix campaign gets the job's gate.
  - `tests/test_gate_cli.py` (or the existing `nw gate` CLI test module): `cadence status`
    table / JSON, `cadence run` sets `forced`.
  - `tests/test_agentic_dogfood_config.py`: nightwright's gates carry `--exclude-marks` and the
    new timeouts; the five cadence jobs validate.
- **Manual verification:**
  - Slice 1: `nw gate run` of the slice's branch on the box; record the pytest gate's test count
    and duration next to GATEJOB-1037 (6940 tests, 1316 s) in *As shipped*.
  - Slice 2: after merge and `nw queue restart`, `nw gate cadence status` shows the five jobs
    seeded; `nw gate cadence run perf` starts a run on a worker slot within one tick; its
    `cadence` event and log dir exist. One real `run_detached` dispatch against the live worker
    (repo memory: validate integrations against the real binary).
  - Slice 3: the 200-commit replay.
- **Regression guard:** a project with no `cadence:` and no `--exclude-marks` in its gates
  (comfort-assistant, jerome) resolves byte-identical gate dicts and env; the SPEC-0379 and
  SPEC-0420 tests pass unchanged. Run the affected tests of each slice plus the files above; the
  full suite is not run by hand (AGENTS.md).

## Rollout / migration

1. Slice 1 lands with the mechanical marker pass in the same campaign (the diff is one
   `pytestmark` line per file, reviewable by `git diff --stat` and the classifier's printed
   table). Merging it changes `pyproject.toml` (`[tool.pytest]`), so its own close gate is a
   full fast-tier run, once.
2. Slice 2 lands with nightwright's `cadence:`; `nw queue restart` picks it up. State seeds on
   the first tick; no job runs before its `every_hours` from seeding unless merges or paths make
   it due.
3. Slice 3 lands independently.
4. Other projects opt in by adding `cadence:` and excluding their heavy tests in their own gate
   commands; nothing changes for them until they do.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).
