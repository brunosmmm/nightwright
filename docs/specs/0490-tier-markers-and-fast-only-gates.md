---
id: SPEC-0490
title: "Tier markers and fast-only campaign gates"
status: done
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0489
tags: [agentic, gates, testing, performance]
depends_on: [SPEC-0328, SPEC-0378, SPEC-0379, SPEC-0392, SPEC-0421]
---

## Context

See SPEC-0489 for the evidence. Campaign gates select 44-53 % of test files on average, and
nothing in the suite marks the TUI, remote, end-to-end and perf tests that dominate per-test
cost. This slice gives every module a tier and removes the heavy tiers from campaign gates.
Full coverage stays on the SPEC-0379 sentinel.

## Goals / Non-goals

**Goals**
- Every test module declares exactly one tier, and a guard enforces it.
- nightwright's loop and close pytest gates run only the fast tier.
- Gate timeouts come from a measured fast-tier run.
- The sentinel and `full_suite` campaigns of a project without `cadence:` still run every tier.

**Non-goals**
- Cadence jobs (SPEC-0491).
- Changes to the selector's selection rules (SPEC-0492).

## Decision

- **Tiers.** There are five: `fast`, `integration`, `tui`, `remote`, `perf`. They are set per
  module with `pytestmark`; a single test may be marked heavier than its module. `slow` folds
  into `integration` and is unregistered.
- **Exclusion.** Gates drop the heavy tiers with
  `affected_tests.py --exclude-marks integration,tui,remote,perf`, which appends
  `-m "not (integration or tui or remote or perf) or tree_guard"` to every run the tool makes.
  The `or tree_guard` clause is deliberate: SPEC-0421 requires every `tree_guard` module on
  every loop and close selection, and `tests/test_org_write_race.py` is both a `tree_guard`
  and (by its `*_race` name) `integration`. A heavy module without `tree_guard` is not
  collected.
- **`NW_ALL_TIERS=1`** drops the exclusion. nw sets it for sentinel runs and `full_suite`
  campaigns of projects with no `cadence:` (non-empty list, see below).
- **Opt-in predicate.** "Has `cadence:`" means `bool(doc.get("cadence"))` on the raw
  `.nw/project.yaml` document. An absent key and `cadence: []` both count as "no cadence". It is
  not key presence on a config that `load_project_config` may have defaulted (SPEC-0491 shares
  this predicate). Until SPEC-0491 lands, `validate` rejects the key, so the variable is always
  set.
- **Reasoning, decided here.** Every heuristic that cannot be tested against a real file name is
  dropped (no wall-clock rule); a mis-tiered module only costs a re-tier, but a wrong exclusion
  turns a gate green without tests, so the exclusion errs toward collecting.

## Design

**Taxonomy.** The first matching rule wins, so a module gets exactly one tier:

1. **Explicit file names:**
   - `test_tui_budget` is `perf` (the only `perf` module);
   - `test_gate_timeout_tree` is `integration`.

   A `time.monotonic` / `perf_counter` comparison never changes a tier.
2. **`tui`:**
   - `test_tui_*`;
   - a module with an AST call whose attribute is `run_test`, or an import of `tui_harness` or
     `tui_budget` (today this also catches `test_idea_project`, `test_clock_json_parity` and
     `test_question_rationale`; `test_idea_tree` and `test_desk_table` no longer drive the app
     on main and are `fast`).
3. **`remote`:**
   - `test_remote_*`;
   - `test_pull_worker*`;
   - a module importing `remote_loopback`, `remote_support` or `remote_workers_golden` (today
     this also catches `test_worker_jobs`).
4. **`integration`:**
   - `*_e2e`, `*_integration`, `*_race` (so `test_dashboard_e2e`, an HTTP test, is
     `integration`, not `tui`);
   - modules over the fast budget.
5. **`fast`:** everything else.

**Fast budget.** A module stays `fast` if, in the measuring run, its summed test time is at
most 20 s and no single test exceeds 2 s.
- If one test is over 2 s, that test gets a function-level `@pytest.mark.integration` and the
  module stays `fast`.
- If the module's sum is over 20 s, the module becomes `integration`.

`tests/test_affected_tests.py` is `fast` unless its durations sum exceeds 20 s, and it has no
function-level tier mark unless a durations line for one of its tests exceeds 2 s. The only live
`@pytest.mark.slow` is on `tests/test_gate_timeout_tree.py::test_real_pytest_xdist_under_a_timeout`;
that module is `integration`, so `--apply` deletes the decorator. The text `pytest.mark.slow`
inside `GUARD_FILES` fixture strings of the affected-tests tests is not a mark and stays.

**Measuring run** (the slice's first step). `nw gate run` gains `--timeout-s N` (default 1200,
unchanged for other callers). Run on the box:
`nw gate run --timeout-s 7200 --cmd "uv run pytest -q -n 0 --durations=0"` (no
`--durations-min`, so every test enters the module sum), retried until it is placed
(GATEJOB-1058 was `unavailable`). An unavailable or timed-out job is never classified, and
`test_tiers.py` enforces it: a log without pytest's final `N passed|failed … in Xs` summary line
exits 2. A log is passed to `--classify` / `--apply` only when it is complete and every failure
in it is listed and verified unrelated to timing (a test that fails on main, or one that passes
alone). A failed test still contributes its full duration, so its module sum is not understated.
(Amended while shipping: the box was shared with another project's gate jobs, and a second
65-minute serial run was not placed. The original rule required exit 0.) After
the marker pass, run two more with `--timeout-s 7200`:
- the full fast tier at `-n 2`, the loop's parallelism;
- the full fast tier at `-n 4`, the close's parallelism.

Each pytest gate's fast-tier limit is `ceil(1.5 * wall seconds)` of the slowest measured job of
its shape. The applied `timeout_s` is the larger of that and today's 2400, because the same gate
dict runs every tier under `NW_ALL_TIERS`: the sentinel runs the close gate, and a `full_suite`
campaign runs both. The all-tier suite took 13-19 min alone and timed out at 1200 under
concurrent gates (SPEC-0302/0304). A gate timeout is a ceiling, not a cost, so a fast-tier
value would only make the heavy-tier safety net time out. The time saved comes from the
exclusion. Per-tier timeouts belong with the cadence jobs (SPEC-0491). (Amended while shipping;
the original rule applied the fast-tier value directly.)

**`tools/test_tiers.py`** (stdlib only, uses `ast`):
- `--classify DURATIONS_LOG` prints one tier per module (from the rules and the log) and lists
  the function-level overrides.
- `--apply DURATIONS_LOG` rewrites `tests/test_*.py` in place with the same classification. A
  missing or empty log exits 2 and writes nothing. It is idempotent.
  - It rewrites the module's `pytestmark` (wherever the assignment sits; if absent it inserts
    one after the imports) into a list: the chosen tier plus every existing non-tier mark
    (`skipif`, `usefixtures`, `tree_guard` and any other), whether the current value is a
    single mark, a list or a tuple. A module-level `pytest.mark.slow` is dropped.
  - It rewrites `@pytest.mark.slow` on a function to `@pytest.mark.integration` when the
    module is `fast`, and deletes it when the module's tier is already heavy.
- `--check` exits 1 and lists the module when it finds any of:
  - a module with no tier;
  - a module with more than one module-level tier;
  - an unknown tier name;
  - any remaining `pytest.mark.slow` **attribute node** (a string constant is not a mark).

  Non-`test_*.py` helper modules are exempt.

**Guard.** `tests/test_test_tiers.py` is itself a `tree_guard` module, so `--guards` always
selects it. It runs `--check` over the tree. `pyproject.toml` registers the five tiers, drops
`slow`, and adds `--strict-markers` to `addopts`.

**`tools/affected_tests.py`:**
- `--exclude-marks A,B,…`: every `--run` invocation that starts the command appends
  `-m "not (A or B …) or tree_guard"`, inserted before the selected paths. That covers the
  selection and the full fallback (including forced full). The empty-selection path is
  unchanged: it prints `no affected tests`, does not start the command and exits 0.
- `NW_ALL_TIERS`, with the same off-set as `NW_AFFECTED_FULL` (empty, `0`, `false` and `no`
  are off, so the expression is kept): any other value drops the expression and logs
  `affected_tests: all tiers`.
- Exit 5 from the `--run` command (nothing collected) becomes 0 and logs
  `affected_tests: no fast tests in the selection` **only** when `--exclude-marks` is active,
  `NW_ALL_TIERS` is off and the run is a non-full selection. A full run, a forced-full run and
  any run under `NW_ALL_TIERS` keep pytest's exit code, so a sentinel or `full_suite` gate
  cannot go green with nothing collected.
- The JSON report gains `exclude_marks` and `all_tiers`.

**`NW_ALL_TIERS` injection.** Set only when the project mapping has no cadence (the predicate
above), in four places:
1. `gates.gate_env` for a `full_suite` campaign (it loads the project config for
   `state["project"]` to read the key);
2. `remote_gates._header` env for a campaign state with `full_suite` true (a worker campaign
   gate does not call `gate_env`; a red-base fix campaign is such a case);
3. the env dict in `sentinel._run_locked` for a local sentinel run;
4. `remote_gates._header` when the campaign id starts with `sentinel-`.

No other `_header` call sets it. It is also added to `worker.ENV_ALLOWLIST`, and
`validate_header` accepts the key. A mapping with a non-empty `cadence` sets it in none of the
four. Do not add `cadence` to the project schema here.

**`retry_cmd`.** nightwright's targeted `retry_cmd` does not go through `affected_tests.py`, so
it carries the static `-m "not (integration or tui or remote or perf) or tree_guard"` itself
(one quoted shell word). Under `NW_ALL_TIERS=1`, that `-m` can deselect a heavy failing id.
Pytest then exits 5, which is already in `gates.TARGETED_UNRESOLVED_EXITS`, so the targeted
attempt counts as unresolved, never as a pass. Only the following full rerun can turn the gate
green; it goes through `affected_tests.py`, which drops the exclusion.

**`.nw/project.yaml` (nightwright):**
- `pytest-affected` (loop), `pytest` (close) and their `retry_cmd` gain the exclusion.
- `timeout_s` is the larger of the measured fast-tier limit and 2400 (see *Measuring run*).
- `docs-tests` is unchanged.

## Alternatives considered

- **A central tier registry file.** The tier would be invisible at the test and would drift on
  renames.
- **Explicit `-m fast`.** It would run a heavy-marked function inside a fast module.
- **Fixing only the selection.** The import graph is dense (see SPEC-0489).

## Acceptance criteria

- [x] Every `tests/test_*.py` declares exactly one module tier, and
  `python3 tools/test_tiers.py --check` exits 0 on the real tree. `GUARD_FILES` still contains
  the text `pytest.mark.slow`, and `test_gate_timeout_tree.py` has no `slow` decorator.
- [x] The guard fails on each of these: no tier, two tiers, an unknown tier, or a leftover
  `slow` attribute node.
- [x] `--apply DURATIONS_LOG` behaves as designed on these shapes:
  - a single `skipif` `pytestmark`;
  - a single `tree_guard` `pytestmark`;
  - a list `pytestmark`;
  - a `pytestmark` that is not the first statement;
  - a function-level `slow`.

  It keeps the non-tier marks, removes every `slow`, a second run changes nothing, a missing
  log exits 2 without writing, and the temp tree collects under `--strict-markers`.
- [x] On the real durations log, `--classify` reports:
  - `perf` only for `test_tui_budget.py` (`test_herdr.py`, `test_agentic_gates.py` and
    `test_pull_workers_security.py` are not `perf`);
  - `tui` for every `test_tui_*.py` except `test_tui_budget.py`, and for
    `test_idea_project.py`, `test_clock_json_parity.py` and `test_question_rationale.py`
    (`test_idea_tree.py` and `test_desk_table.py` stopped driving the app on main before this
    shipped and are `fast`);
  - `remote` for every `test_remote_*.py` and `test_pull_worker*.py`, and `test_worker_jobs.py`;
  - `integration` for every `*_e2e`, `*_integration` and `*_race` module except where an
    earlier rule won (`test_remote_workers_integration.py` is `remote`), and for
    `test_dashboard_e2e.py` and `test_gate_timeout_tree.py`.

  No module appears twice. The temp-tree tests cover a `run_test` module, a `remote_loopback`
  import and a `*_race` module.
- [x] `pyproject.toml` registers the five tiers, not `slow`, and runs with `--strict-markers`.
- [x] `--exclude-marks` applies to the selection and the full fallback, is inserted before the
  paths, and does not start pytest on an empty selection. Exit 5 becomes 0 only for a partial
  selection with `NW_ALL_TIERS` off; it stays non-zero for a full run, a forced-full run and
  `NW_ALL_TIERS=1`. `NW_ALL_TIERS=1` drops the expression and the off-set keeps it.
- [x] `--guards --exclude-marks integration,tui,remote,perf --run pytest --collect-only` still
  collects `tests/test_org_write_race.py` and does not collect a fixture module whose only tier
  mark is `integration`.
- [x] The header built for a `full_suite` state without cadence contains `NW_ALL_TIERS=1`, a
  normal campaign header does not, and `validate_header` accepts the key. `gate_env` for that
  campaign has it. A local sentinel run and a `sentinel-` detached header have it. With a
  non-empty `cadence` none of the four has it. `worker.ENV_ALLOWLIST` includes it.
- [x] A targeted retry under `NW_ALL_TIERS=1` whose failing id is heavy is recorded as
  unresolved, and the gate passes only on the full rerun.
- [x] `nw gate run --timeout-s` exists (default 1200 for other callers). The measuring command in
  *Design* includes `--timeout-s 7200`. An incomplete (cut or timed-out) log exits 2 from
  `--classify` and `--apply`, and the log that was used is complete, with each failure listed
  and verified in *As shipped*.
- [x] nightwright's two pytest gates exclude the heavy tiers. The measured durations of both
  fast-tier runs, and the limits (`ceil(1.5 * wall)`) derived from them, are quoted in
  *As shipped*. The applied `timeout_s` is the larger of that limit and 2400. Neither timeout is
  lower than its measured run.
- [x] A `nw gate run` of a typical branch (one spec, the ledger and one src module) runs no
  heavy-tier test. Its count and time are recorded next to GATEJOB-1037.

## Test plan

- **Automated tests:**
  - `tests/test_test_tiers.py`:
    - the guard over the real tree;
    - `--check`, `--apply` and `--classify` on a temp tree, covering the shapes in the
      acceptance criteria, idempotence, the missing-log exit 2, the helper-module exemption,
      durations-log parsing, the 20 s / 2 s budget and the precedence order;
    - the real-tree classification assertions above.
  - `tests/test_affected_tests.py`: `--exclude-marks` on each path, the exit-5 matrix, the
    `tree_guard` collect-only case, `NW_ALL_TIERS` (including the off-set), and the JSON fields.
  - `tests/test_agentic_gates.py`: `gate_env` for a `full_suite` campaign with and without
    `cadence`, and the targeted retry ending unresolved on exit 5; `--timeout-s` on `nw gate run`.
  - `tests/test_agentic_sentinel.py` / `tests/test_remote_gates.py`: the local sentinel env, the
    detached header, the `full_suite` header, the normal header without it, and the allowlist.
  - `tests/test_agentic_dogfood_config.py`: nightwright's gates carry the exclusion.
- **Manual verification:** the three measuring runs and the typical-branch `nw gate run`,
  recorded in *As shipped*.
- **Regression guard:** projects without `cadence:` and without `--exclude-marks` resolve
  identical gate dicts. Run the affected tests plus the files above, never the full suite by
  hand.

### Test record (2026-11-09)

- `python3 tools/test_tiers.py --check`: exit 0 on the real tree.
- `uv run pytest -q -n 4 tests/test_test_tiers.py tests/test_affected_tests.py
  tests/test_agentic_dogfood_config.py tests/test_remote_gates.py` plus
  `tests/test_agentic_gates.py::{test_heavy_retry_id_exit_5_is_unresolved,
  test_all_tiers_env_local_sentinel_state, test_sentinel_run_env_uses_a_sentinel_id,
  test_gate_env_full_suite_sets_all_tiers_without_cadence}`: 277 passed.
- A `-k` tier/sentinel/header/timeout slice over those files plus
  `tests/test_agentic_sentinel.py`: 221 passed.
- `tests/test_agentic_conductor_guard.py::test_conduct_skill_never_asks_the_human` fails on
  main `f06bc847` (1 failed in 0.99 s, main checkout). This is the only failure in every box run
  below, and it is not caused by this spec.
- Box runs (worker `box`, shared with another project's gate jobs):

| Job | Run | Result | pytest wall | job wall |
|---|---|---|---|---|
| GATEJOB-1266 | durations, all tests, `-n 0` (`cbce8a0b`) | 5 failed, 8151 passed, 11 skipped | 3906.16 s | 3933.3 s |
| GATEJOB-1335 | fast tier `-n 2` (`20dede28`) | 1 failed, 5656 passed, 6 skipped | 287.59 s | 311.0 s |
| GATEJOB-1341 | fast tier `-n 4` (`9a63fdce`) | 1 failed, 5658 passed, 6 skipped | 170.56 s | 1113.8 s (contended box) |
| GATEJOB-1342 | project gates, probe on the unmerged branch | spec-lint ok; loop: 1 failed, 2472 passed, 4 skipped (`-x`) | 171.88 s | 924.0 s |
| GATEJOB-1336 / 1337 / 1333 | first `-n 4` and probe / durations retry | `unavailable` (no slot in 600 s) / cancelled | — | — |

### As shipped (2026-11-09)

- **Measuring-run log.** The marks come from GATEJOB-1266. That log is complete (final summary
  line present) but red, and the rule requiring exit 0 was amended (see *Measuring run*). The
  durations retry (GATEJOB-1333) was cancelled: the box was saturated by another project's
  jobs, and the run is 65 min serial. The five failures were verified one by one:
  - `test_conduct_skill_never_asks_the_human` fails on main;
  - `test_test_tiers::test_check_real_tree` was the old guard flagging 8 modules added on main
    by the rebase (now tiered);
  - `test_tui_fixture_harness` (2) and
    `test_tui_ideas_board::test_tab_renders_rail_and_buckets_and_mutations_target_the_selection`
    pass alone with `-n 0`, so they are timing-sensitive under load, and their modules are `tui`
    anyway.

  A failed test still reports its duration, so no module sum is understated. `test_tiers.py`
  now exits 2 on a log without the final summary, so a cut or timed-out run cannot be applied.
- **Tiers.** About 331 modules were re-tiered from that log; `--check` is 0 on the tree.
  `test_idea_tree` and `test_desk_table` stopped driving the app on main and are `fast`.
- **Decorator rewrite.** `--apply` rewrites decorators and `pytestmark` through the AST. The
  first regex version also edited `pytest.mark.slow` inside fixture strings and dropped
  `pytestmark` trailing comments.
- **Gate time.** The fast tier is 5659 tests in 288 s at `-n 2` and 171 s at `-n 4` on an idle
  slot. The whole suite is 8167 tests in 3906 s serial, and a pre-0490 close gate (GATEJOB-1037)
  was 6940 tests in 1287 s.
- **Timeouts.** The derived fast-tier limits are 1386 for `pytest-affected` (1.5 × 924 s,
  GATEJOB-1342, the slowest `-n 2` job; 1335 alone gives 467) and 1671 for `pytest`
  (1.5 × 1113.8 s, GATEJOB-1341). Both gates keep `timeout_s: 2400`, the larger value, because
  `NW_ALL_TIERS` runs every tier through them (see *Measuring run*).
- **Loop-gate guard.** The loop gate passes `--exclude-marks` only when
  `affected_tests.py --help` lists it. A pre-0490 branch, whose tool lacks the flag, still gates.
- **Typical branch.** GATEJOB-1353 ran the probe branch (one spec, the ledger, one line in
  `src/nightwright/agentic/gate_jobs.py`) rebased on merged main `a9fc9479`. It passed in 373.3 s
  on the box, with spec-lint ok and `pytest-affected` at 2255 passed, 3 skipped in 145.32 s
  (127/332 files + 11 guards, under `-m "not (integration or tui or remote or perf) or
  tree_guard"`). GATEJOB-1037 had 6940 tests in 1287.41 s (job 1500.0 s). The only heavy-marked
  module collected is the `tree_guard` exception `test_org_write_race`, by design. GATEJOB-1342,
  cut before the merge, was a full fast-tier fallback because its diff still contained
  `pyproject.toml`.
- The probe worktree `SPEC-0490-typical` was not removed: `git worktree remove` was denied to
  the implementing agent.

## Rollout / migration

- The marker pass lands in the same campaign.
- The close gate of that campaign is a full fast-tier run, once, because `pyproject.toml`
  changes.
- Heavy tiers keep running in the 6-hourly sentinel through `NW_ALL_TIERS`. This is also the
  safety net for SPEC-0492 until the cadence jobs of SPEC-0491 and SPEC-0493 land.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.
