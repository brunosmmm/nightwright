---
id: SPEC-0490
title: "Tier markers and fast-only campaign gates"
status: accepted
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
  `-m "not (…)"` to every run the tool makes.
- **`NW_ALL_TIERS=1`** drops the exclusion. nw sets it for sentinel runs and `full_suite`
  campaigns of projects with no `cadence:` key.

## Design

**Taxonomy.** The first matching rule wins, so a module gets exactly one tier:

1. **Explicit file names:**
   - `test_tui_budget` is `perf`;
   - `test_dashboard_e2e` is `tui`;
   - `test_gate_timeout_tree` is `integration`.
2. **`perf`:** a module that asserts a wall-clock budget (a `time.monotonic` / `perf_counter`
   delta compared to a constant).
3. **`tui`:**
   - `test_tui_*`;
   - modules that drive Textual (`run_test`, `Pilot`, `tui_harness`, `tui_budget`).
4. **`remote`:**
   - `test_remote_*`;
   - `test_pull_worker*`;
   - modules that import `remote_loopback`, `remote_support` or `remote_workers_golden`.
5. **`integration`:**
   - `*_e2e`, `*_integration`, `*_race`;
   - modules over the fast budget.
6. **`fast`:** everything else.

**Fast budget.** A module stays `fast` if, in the measuring run, its summed test time is at
most 20 s and no single test exceeds 2 s.
- If one test is over 2 s, that test gets a function-level `@pytest.mark.integration` and the
  module stays `fast`.
- If the module's sum is over 20 s, the module becomes `integration`.

`test_affected_tests` is `fast` unless the measurements say otherwise. Only its tests that
start a nested pytest (today's `slow` tests) get the function-level `integration` mark.

**Measuring run** (the slice's first step). Run on the box with
`nw gate run --cmd "uv run pytest -q -n 0 --durations=0 --durations-min=0.05"`, retried until
it is placed (GATEJOB-1058 was `unavailable`). After the marker pass, run two more:
- the full fast tier at `-n 2`, the loop's parallelism;
- the full fast tier at `-n 4`, the close's parallelism.

Each pytest gate's `timeout_s` is that run's wall time plus 50 %. It stays at today's 2400 if
the run did not finish.

**`tools/test_tiers.py`** (stdlib only, uses `ast`):
- `--classify DURATIONS_LOG` prints one tier per module (from the rules and the log) and lists
  the function-level overrides.
- `--apply` rewrites the module's `pytestmark` into a list: the chosen tier plus every existing
  non-tier mark (`skipif`, `usefixtures`, `tree_guard` and any other), whether the current
  value is a single mark, a list or a tuple.
  - It rewrites `@pytest.mark.slow` on a function to `@pytest.mark.integration` when the
    module is `fast`.
  - It deletes that decorator when the module's tier is already heavy.
  - It is idempotent.
- `--check` exits 1 and lists the module when it finds any of:
  - a module with no tier;
  - a module with more than one module-level tier;
  - an unknown tier name;
  - any remaining `pytest.mark.slow`.

  Non-`test_*.py` helper modules are exempt.

**Guard.** `tests/test_test_tiers.py` is itself a `tree_guard` module, so `--guards` always
selects it. It runs `--check` over the tree. `pyproject.toml` registers the five tiers, drops
`slow`, and adds `--strict-markers` to `addopts`.

**`tools/affected_tests.py`:**
- `--exclude-marks A,B,…`: every `--run` invocation appends `-m "not (A or B …)"`. That covers
  the selection, the full fallback (including forced full) and the empty-selection path.
- `NW_ALL_TIERS`, with the same off-set as `NW_AFFECTED_FULL` (empty, `0`, `false` and `no`
  are off): any other value ignores the flag and logs `affected_tests: all tiers`.
- Exit 5 from the `--run` command (nothing collected) becomes 0 and logs
  `affected_tests: no fast tests in the selection`.
- The JSON report gains `exclude_marks` and `all_tiers`.

**`NW_ALL_TIERS` injection.** Set only when the project config has no `cadence` key, in three
places:
1. `gates.gate_env` for a `full_suite` campaign;
2. the env dict in `sentinel._run_locked` for a local sentinel run;
3. `remote_gates._header` env for a sentinel dispatch.

It is also added to `worker.ENV_ALLOWLIST`. (The sentinel does not call `gate_env`, so setting
it there alone would leave the sentinel filtered.)

**`retry_cmd`.** nightwright's targeted `retry_cmd` does not go through `affected_tests.py`, so
it carries the static `-m` itself. Under `NW_ALL_TIERS=1`, that `-m` can deselect a heavy
failing id. Pytest then exits 5, which is already in `gates.TARGETED_UNRESOLVED_EXITS`, so the
targeted attempt counts as unresolved, never as a pass. Only the following full rerun can turn
the gate green; it goes through `affected_tests.py`, which drops the exclusion.

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

- [ ] Every `tests/test_*.py` declares exactly one module tier, and
  `python3 tools/test_tiers.py --check` exits 0.
- [ ] The guard fails on each of these: no tier, two tiers, an unknown tier, or a leftover
  `slow`.
- [ ] `--apply` behaves as designed on these shapes:
  - a single `skipif` `pytestmark`;
  - a single `tree_guard` `pytestmark`;
  - a list `pytestmark`;
  - a function-level `slow`.

  It keeps the non-tier marks, removes every `slow`, and the temp tree collects under
  `--strict-markers`.
- [ ] `--classify` puts:
  - `test_tui_budget.py` in `perf`;
  - `test_dashboard_e2e.py` in `tui`;
  - `test_affected_tests.py` in `fast` (with only function-level `integration` marks).

  No module appears twice.
- [ ] `pyproject.toml` registers the five tiers, not `slow`, and runs with `--strict-markers`.
- [ ] `--exclude-marks` applies to the selection, the full fallback and the empty selection.
  Exit 5 becomes 0, and `NW_ALL_TIERS=1` drops the expression.
- [ ] A sentinel run of a project without `cadence:` has `NW_ALL_TIERS=1` in its local env and
  in its detached header. A project with `cadence:` has it in neither. A `full_suite` campaign
  gets it from `gate_env`, and the worker allowlists it.
- [ ] A targeted retry under `NW_ALL_TIERS=1` whose failing id is heavy is recorded as
  unresolved, and the gate passes only on the full rerun.
- [ ] nightwright's two pytest gates exclude the heavy tiers. The measured durations of both
  fast-tier runs, and the `timeout_s` derived from them, are quoted in *As shipped*. Neither
  timeout is lower than its measured run.
- [ ] A `nw gate run` of a typical branch (one spec, the ledger and one src module) runs no
  heavy-tier test. Its count and time are recorded next to GATEJOB-1037.

## Test plan

- **Automated tests:**
  - `tests/test_test_tiers.py`:
    - the guard over the real tree;
    - `--check`, `--apply` and `--classify` on a temp tree, covering the shapes in the
      acceptance criteria, idempotence, the helper-module exemption, durations-log parsing,
      the 20 s / 2 s budget and the precedence order.
  - `tests/test_affected_tests.py`: `--exclude-marks` on each path, exit 5 becomes 0,
    `NW_ALL_TIERS` (including the off-set), and the JSON fields.
  - `tests/test_agentic_gates.py`: `gate_env` for a `full_suite` campaign with and without
    `cadence`, and the targeted retry ending unresolved on exit 5.
  - `tests/test_agentic_sentinel.py` / `tests/test_remote_gates.py`: the local sentinel env, the
    detached header and the allowlist.
  - `tests/test_agentic_dogfood_config.py`: nightwright's gates carry the exclusion.
- **Manual verification:** the three measuring runs and the typical-branch `nw gate run`,
  recorded in *As shipped*.
- **Regression guard:** projects without `cadence:` and without `--exclude-marks` resolve
  identical gate dicts. Run the affected tests plus the files above, never the full suite by
  hand.

## Rollout / migration

- The marker pass lands in the same campaign.
- The close gate of that campaign is a full fast-tier run, once, because `pyproject.toml`
  changes.
- Heavy tiers keep running in the 6-hourly sentinel through `NW_ALL_TIERS`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass at the close gate; manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
