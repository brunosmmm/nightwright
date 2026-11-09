---
id: SPEC-0490
title: "Tier markers and fast-only campaign gates"
status: in-progress
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
     `tui_budget` (today this also catches `test_idea_tree`, `test_idea_project`,
     `test_desk_table`, `test_clock_json_parity`, `test_question_rationale`).
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
`--durations-min`, so every test enters the module sum), retried until it is placed and exits 0
(GATEJOB-1058 was `unavailable`). A log is passed to `--classify` / `--apply` only when that
pytest exited 0; an unavailable, failed or timed-out job is retried and never classified. After
the marker pass, run two more with `--timeout-s 7200`:
- the full fast tier at `-n 2`, the loop's parallelism;
- the full fast tier at `-n 4`, the close's parallelism.

Each pytest gate's `timeout_s` is `ceil(1.5 * wall seconds)` of its matching run. It stays at
today's 2400 if that run did not finish.

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
- `timeout_s` comes from the measurement.
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
- [ ] The guard fails on each of these: no tier, two tiers, an unknown tier, or a leftover
  `slow` attribute node.
- [ ] `--apply DURATIONS_LOG` behaves as designed on these shapes:
  - a single `skipif` `pytestmark`;
  - a single `tree_guard` `pytestmark`;
  - a list `pytestmark`;
  - a `pytestmark` that is not the first statement;
  - a function-level `slow`.

  It keeps the non-tier marks, removes every `slow`, a second run changes nothing, a missing
  log exits 2 without writing, and the temp tree collects under `--strict-markers`.
- [ ] On the real durations log, `--classify` reports:
  - `perf` only for `test_tui_budget.py` (`test_herdr.py`, `test_agentic_gates.py` and
    `test_pull_workers_security.py` are not `perf`);
  - `tui` for every `test_tui_*.py` except `test_tui_budget.py`, and for `test_idea_tree.py`,
    `test_idea_project.py`, `test_desk_table.py`, `test_clock_json_parity.py` and
    `test_question_rationale.py`;
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
- [ ] `--guards --exclude-marks integration,tui,remote,perf --run pytest --collect-only` still
  collects `tests/test_org_write_race.py` and does not collect a fixture module whose only tier
  mark is `integration`.
- [ ] The header built for a `full_suite` state without cadence contains `NW_ALL_TIERS=1`, a
  normal campaign header does not, and `validate_header` accepts the key. `gate_env` for that
  campaign has it. A local sentinel run and a `sentinel-` detached header have it. With a
  non-empty `cadence` none of the four has it. `worker.ENV_ALLOWLIST` includes it.
- [ ] A targeted retry under `NW_ALL_TIERS=1` whose failing id is heavy is recorded as
  unresolved, and the gate passes only on the full rerun.
- [x] `nw gate run --timeout-s` exists (default 1200 for other callers). The measuring command in
  *Design* includes `--timeout-s 7200`, and a non-zero or timed-out pytest log is not passed to
  `--classify` or `--apply`.
- [ ] nightwright's two pytest gates exclude the heavy tiers. The measured durations of both
  fast-tier runs, and the `timeout_s` (`ceil(1.5 * wall)`) derived from them, are quoted in
  *As shipped*. Neither timeout is lower than its measured run.
- [ ] A `nw gate run` of a typical branch (one spec, the ledger and one src module) runs no
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

- `uv run pytest -q tests/test_test_tiers.py tests/test_guard_registry.py::test_every_marked_module_is_a_detected_scanner tests/test_guard_registry.py::test_every_tree_scanner_is_marked_or_exempt` (10 passed).
- Full pytest-affected gate and full suite left to gating.

### Test record (2026-11-10)

- `python3 tools/test_tiers.py check` (exit 0 on the real tree).
- `uv run pytest -q tests/test_test_tiers.py tests/test_affected_tests.py tests/test_agentic_dogfood_config.py tests/test_agentic_gates.py::test_gate_env_full_suite_sets_all_tiers_without_cadence tests/test_agentic_gates.py::test_gate_run_resolve_ad_hoc_timeout tests/test_remote_gates.py::test_header_full_suite_includes_all_tiers` (133 passed).
- `python3 tools/spec_lint.py` (pre–write-ledger).
- Box measuring runs and full suite left to gating.

### As shipped (2026-11-10)

- Tier marks applied to all `tests/test_*.py` via `tools/test_tiers.py --apply` (taxonomy +
  synthetic durations stub; box run with `--durations=0` still pending).
- `pytest-affected` / `pytest` gate `timeout_s` remain **2400** until the fast-tier measuring
  runs on the worker box land.

## Rollout / migration

- The marker pass lands in the same campaign.
- The close gate of that campaign is a full fast-tier run, once, because `pyproject.toml`
  changes.
- Heavy tiers keep running in the 6-hourly sentinel through `NW_ALL_TIERS`. This is also the
  safety net for SPEC-0492 until the cadence jobs of SPEC-0491 and SPEC-0493 land.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
