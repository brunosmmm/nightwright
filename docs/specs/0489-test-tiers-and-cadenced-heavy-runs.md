---
id: SPEC-0489
title: "Test tiers: campaign gates run the fast tier only; heavy tiers run on a per-project cadence"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: epic
tags: [agentic, gates, testing, performance, sentinel, cadence]
depends_on: [SPEC-0328, SPEC-0359, SPEC-0378, SPEC-0379, SPEC-0381, SPEC-0420, SPEC-0421]
---

## Context

The human has asked many times for less testing per campaign. SPEC-0328, SPEC-0357 and
SPEC-0378 built an affected-test selector (`tools/affected_tests.py`) for the loop and close
gates. SPEC-0379 moved the full suite to a 6-hourly sentinel on main. In practice the gates
still run most of the suite on every campaign.

**Evidence (2026-11-09, read-only).**

- **Real campaign gates.** 56 campaigns created 2026-11-03..09 made 250 `pytest-affected` /
  `pytest` gate runs (first `affected_tests:` line of each log under
  `<agentic>/campaigns/*/gates/`). Of those runs:
  - 39 (16 %) ran the full suite;
  - **none** selected under 30 % of test files;
  - 102 selected 30-50 %;
  - 109 selected 50 % or more.

  The mean partial selection was **53 %** of test files. Every recent spec campaign's reason
  starts with `reference scan for docs/LEDGER.md; reference scan for docs/specs/NNNN-….md;
  tree readers of …`.
- **Why runs went full in that window.**
  - `NW_AFFECTED_FULL is set`: 22 runs (SPEC-0420 red-main FIX and MERGE campaigns).
  - A fixture file that no test references: 14 runs.
  - `conftest.py changed`: 3 runs.
- **Commit replay.** I ran the last 80 first-parent commits on main through the selector
  (`--guards` plus the gate's `--include`s). One went full (conftest). The partial selections
  had a median of 142 of 326 files (44 %), a p90 of 183 (56 %) and a maximum of 262.
- **Box gate times.**
  - GATEJOB-0997 (close tier, `conftest.py changed`, full): 8060 tests in 589 s.
  - GATEJOB-1037 (SPEC-0487, 10 changed files): selected 261 of 326 files and ran 6940 tests in
    1316 s.
  - GATEJOB-0995: 6121 tests in 316 s.

  The spread is box load, because campaign gates share the worker. A box gate takes 5-25
  minutes, and a campaign pays it at least twice (loop and close), more with fix rounds.
- **Per-file widening.** Measured with `--changed` on today's tree (326 test files):

  | Changed file | Test files selected | Why |
  |---|---|---|
  | `docs/specs/0487-….md` (any new spec) | 119 | No test names the new basename. `_referencing` falls back to every module with a `specs` directory reference (67 modules), then to their nearest tests. |
  | `docs/LEDGER.md` | 27 | 17 modules reference it. |
  | `src/nightwright/agentic/queue.py` | 144 | 98 test files import `queue` directly (139 nearest tests). |
  | `src/nightwright/agentic/lease.py` | 129 | Reaches `queue`'s importers. |
  | `src/nightwright/store.py` | 295 | 118 test files import `agentic.store` directly; the removed-module rule adds more. |
  | `src/nightwright/agentic/sentinel.py` | 11 | A leaf module: the selector works as designed. |
  | `tests/conftest.py` | full | The root conftest. |

  The tree-guard and tree-reader rules (SPEC-0421) add only 6-10 files. The selector is
  correct; the import graph is dense.
- **No test has a category.** The suite has 5942 test functions (about 8000 collected items) in
  326 files. Only two markers exist:
  - `slow` (2 files, SPEC-0392);
  - `tree_guard` (13 files, SPEC-0421).

  By file name, the heavy groups are:
  - 26 `test_tui_*` files (392 tests);
  - 5 `remote*` files (245 tests);
  - 9 `*e2e*` / `*integration*` files (78 tests);
  - 2 race files;
  - 1 TUI budget file.
- **Per-test durations.** None were ever recorded, because no gate uses `--durations`. I tried
  a measuring run (GATEJOB-1058,
  `uv run pytest -q -n 4 --durations=0 --durations-min=0.3`). It ended `unavailable`: no worker
  slot came free within 300 s while campaign gates held the box. SPEC-0490 makes that run its
  first step.

Exact selection would need coverage data, which SPEC-0328 rejected. The lever the human asked
for is different: most tests should not be candidates for a campaign gate at all, and the
heavy groups should run on a schedule that each project chooses.

## Goals / Non-goals

**Goals**
- Every test belongs to exactly one tier, declared in its module; a guard fails when one does
  not.
- Campaign loop and close gates run only the **fast** tier, intersected with the affected
  selection, under a measured time budget.
- A project declares cadenced jobs in `.nw/project.yaml`. A job runs after every N merges,
  every H hours, when a merge touches given paths, or after every merge. nw keeps the state and
  the runner starts due jobs in the background on worker gate slots.
- A red cadenced job uses the existing SPEC-0420 red-main episode and fix-campaign flow.
- `nw gate cadence status` shows the state of every job.
- The selector stops widening on spec and ledger edits, on hot modules and on fixture data.
- A project without the new config behaves exactly as today.

**Non-goals**
- Coverage-based or predictive selection.
- Bisecting a red cadenced run.
- Tiering other projects' tests.
- A dashboard tab.

## Decision

The work is four child specs, each one campaign. Two chains can run in parallel:
SPEC-0490 -> SPEC-0492, and SPEC-0491 -> SPEC-0493 (which also needs SPEC-0490 landed).

1. **Tiers** (SPEC-0490).
   - There are five module-level tiers: `fast`, `integration`, `tui`, `remote`, `perf`.
     `slow` folds into `integration`.
   - A guard enforces exactly one tier per module.
   - The selector adds `--exclude-marks` and the gates exclude the heavy tiers.
   - Gate timeouts come from a measured fast-tier run.
   - Until SPEC-0493 lands, a project without `cadence:` keeps full coverage. Its sentinel and
     `full_suite` runs drop the exclusion through `NW_ALL_TIERS=1`.
2. **Cadence config and state** (SPEC-0491).
   - A `cadence:` list goes in `.nw/project.yaml`; opt-in is a non-empty list.
   - State is kept per project in `<agentic>/cadence/<project>.json`.
   - The hook runs on every ff-merge. `due`, consumption and `nw gate cadence status`.
3. **Cadence runner, red-main source and nightwright's jobs** (SPEC-0493).
   - The runner reuses the sentinel's run lock, placement and detached dispatch.
   - Red jobs go through the red-main episode, which now carries a source and is confirmed by
     its own job.
   - `nw gate cadence run`; the five nightwright jobs.
4. **Tighter selection** (SPEC-0492).
   - Spec and ledger files select only the tests that name them exactly.
   - A hot module narrows to the tests that name a changed definition.
   - Fixture data selects the readers of its fixture root.

## Architecture / cross-cutting design

- **Tier names** are pytest markers registered in `pyproject.toml` with `--strict-markers`.
  "Fast" means "not heavy": every fast-tier command is
  `-m "not (integration or tui or remote or perf)"`. As a result, a function-level heavy marker
  inside a fast module also excludes that test.
- **`NW_ALL_TIERS`** (truthy, with the same off-set as `NW_AFFECTED_FULL`) makes
  `affected_tests.py` ignore `--exclude-marks`. nw sets it only for projects **without**
  `cadence:`, on:
  - sentinel runs, both local and detached;
  - `full_suite` campaigns.

  It is on the worker env allowlist.
- **Coverage invariant.** Every tier runs on main at least as often as the SPEC-0379 sentinel
  ran it before this epic:
  - before SPEC-0493, through the sentinel with `NW_ALL_TIERS`;
  - after it, through cadence jobs.

  For each heavy tier, the project's cadence includes a job whose `every_hours` is at most 24.
- **One background run per project.** Sentinel and cadence runs share `sentinel.run_lock`
  and the sentinel base worktree, and only one runs at a time.
- **One red-main episode per project**, with a `source` (`sentinel` or `cadence:<job>`).

## Breakdown / sub-specs

- [ ] SPEC-0490: Tier markers and fast-only campaign gates.
- [ ] SPEC-0491: Cadence config, merge accounting and due state. Depends on SPEC-0490 (the
  shared opt-in predicate).
- [ ] SPEC-0492: Tighter affected selection for specs, hot modules and fixtures. Depends on
  SPEC-0490 only: until the cadence jobs land, the sentinel with `NW_ALL_TIERS` is the stronger
  net.
- [ ] SPEC-0493: Cadence runner trigger, red-main source and nightwright's jobs. Depends on
  SPEC-0491.

Sequencing: two parallel chains, SPEC-0490 -> SPEC-0492 and SPEC-0491 -> SPEC-0493. SPEC-0490
brings the largest gain on its own and goes first.

## Acceptance criteria

- [ ] On nightwright, a typical campaign's box gates (one spec, the ledger and one src module)
  run no `integration` / `tui` / `remote` / `perf` test. Their wall time is recorded next to
  GATEJOB-1037 (6940 tests, 1316 s).
- [ ] Every heavy tier runs on main on a cadence of at most 24 hours. A red tier opens a
  red-main episode whose fix campaign runs that tier.
- [ ] A 200-commit replay (checkout + `--base <commit>^`) after SPEC-0492 shows a lower median selection than today's
  44 %, recorded in SPEC-0492.
- [ ] Projects without `cadence:` resolve byte-identical gate dicts and env (comfort-assistant,
  jerome).

## Test plan

- **Unit tests:** in each child.
- **Integration:**
  - after SPEC-0490, one `nw gate run` of a typical branch on the box;
  - after SPEC-0493, `nw gate cadence status` on the live runner and one forced job dispatched
    to the real worker (repo memory: validate against the real binary);
  - after SPEC-0492, the commit replay.
- **Manual verification:** recorded in the children's *As shipped* sections.

## Rollout / sequencing

1. SPEC-0490 lands with the mechanical marker pass, which edits one `pytestmark` line per file.
   Its own close gate is one full fast-tier run, because `pyproject.toml` changes.
2. SPEC-0491 lands the config, state and counter; nothing runs yet.
3. SPEC-0493 lands with nightwright's `cadence:` jobs; `nw queue restart` picks them up.
   SPEC-0492 lands in parallel after SPEC-0490.
4. Other projects opt in by adding `cadence:` and excluding their own heavy tests in their gate
   commands.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

- None. Thresholds (fast budget, cadence N/H, hot threshold) are set in the children and tuned
  from cadence events.
