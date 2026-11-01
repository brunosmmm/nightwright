---
id: SPEC-0357
title: "Narrow affected_tests.py fallbacks and write the NW_GATE_REPORT selection report"
status: accepted
owner: user
created: 2026-11-01
kind: feature
source_idea: IDEA-649
tags: [agentic, gates, tests]
depends_on: [SPEC-0289, SPEC-0328, SPEC-0350]
supersedes: ["SPEC-0355"]
---

## Context

[SPEC-0328](./0328-tiered-gates-affected-test-selection.md) split gates into a loop tier
(`pytest-affected`: `tools/affected_tests.py` picks the tests a branch affects) and a close tier
(the full `pytest` suite once, on the rebased tree). [SPEC-0350](./0350-per-gate-path-sensitivity.md)
skips close gates whose `paths` the branch does not touch. Only docs-only merges got faster
(24 s instead of about 600 s). Code campaigns still spend most of their wall time in gates.

This spec is part 1 of 3 of the split of SPEC-0355 (which it supersedes together with the other two
parts). It covers the loop tier only: the selection tool itself. The 2026-11-01 ledgers and gate
logs show where the loop tier loses time:

- **SPEC-0338**: 8001 s in gates. 7 or more `pytest-affected` rounds fell back to the **full** suite
  (11-24 min each). `affected_tests.py` logged `full suite: .nw/project.yaml changed (static
  selection unsafe)` and `test data changed: tests/data/remote_gates_golden.json`.
- **SPEC-0354**: 1028 s on a fallback `no test reaches src/nightwright/agentic/cli_workers.py`.
  This was a new CLI-wiring module. It is a hub (`agentic.cli_*`), so the walk never goes past it.
- Slot waits of 317-671 s add to all of this. Shorter gate runs make these waits shorter too.

Most of these fallbacks are not real unknowns. A config file, a test data file, a tool script or a
package data file has readers that a string-constant scan finds, and a brand-new module usually has
tests named after it. The tool also gives later work nothing to reason about: it prints a
selection but exposes neither a machine-readable form nor the set of files the selected tests
depend on. Part 3 of the split (close-tier reuse) needs both.

## Goals / Non-goals

**Goals**
- G1. `affected_tests.py` falls back to the full suite only for changes it cannot map:
  `pyproject.toml`, `uv.lock`, any `conftest.py`, `tests/waits.py`, a removed module, an unreadable
  diff, or a selection above `MAX_FRACTION`. Config, test data, tools, package data and
  unreached src modules map to targeted sets.
- G1a. `affected_tests.py` writes a JSON report of its selection to the path in `NW_GATE_REPORT`,
  and has a JSON CLI mode that prints the same selection together with its forward static closure.
  Later specs consume both. This spec adds no consumer.
- G1b. The mapping is verified against the recorded 2026-11-01 fallbacks on the real repository
  tree (replay test), and can be rolled back with one switch.

**Non-goals**
- Coverage-based or predictive selection (SPEC-0328 phase 3).
- Batched close trains or bisecting (SPEC-0328 phase 2).
- Changing the close tier: it still runs the full `pytest` suite, and `merge.docs_only_paths` and
  SPEC-0350 `paths` are unchanged.
- Mapping a *removed* module's old importers (still a full fallback).
- Part 2 of the split of SPEC-0355: failed-first loop rounds (gate key `failed_first`, the prelude
  in `run_gate_with_retries`, dogfood `failed_first: true`).
- Part 3 of the split of SPEC-0355: close-tier reuse of a green loop gate with a full-suite
  sentinel (`head`/`tree`/`report` on gate payloads, `last_green_loop`, `reuse_from`, the
  `close`/`sentinel` project sections, rules R1-R7, `gate.reused` / `gate.reuse_declined` events,
  `gate_reuse.json`, `nw queue sentinel`, dogfood `reuse_from`). In particular, `run_gate` does not
  set `NW_GATE_REPORT` and gate results do not carry the report in this spec; part 3 wires that up.

## Decision

1. **Narrowed mapping in `tools/affected_tests.py`.** It uses a reference scan (string constants
   and import names) next to the import graph. A changed non-Python file under `tests/`,
   `src/nightwright/`, `.nw/` or `tools/`, or a changed `tools/*.py`, selects the test modules that
   reference it, plus the nearest tests of src modules that reference it. A src module that no test
   reaches selects the test files named after it (the name rule). The configured smoke set
   (`--include`) is always added. A change that maps to nothing still falls back to the full suite.
   The full-suite triggers shrink to `pyproject.toml`, `uv.lock`, any `conftest.py`,
   `tests/waits.py` and removed modules.
2. **Rollback.** `--strict` or env `NW_AFFECTED_STRICT=1` restores the SPEC-0328 triggers exactly.
3. **Report.** When env `NW_GATE_REPORT` is set, the tool writes the selection report JSON there
   (best effort).
4. **JSON mode.** `--json` prints the report on stdout instead of the human lines, and `--closure`
   adds the forward static closure of the selection. `--changed PATH` (repeatable) replaces the
   `git diff` against the base with an explicit path list. Together they let a later caller ask
   "what would be selected for these paths, and what do those tests read" on any checkout.

## Design

### 1. Narrowed fallbacks (`tools/affected_tests.py`, stdlib only)

**Full triggers** (`FULL_TRIGGERS`, `tools/affected_tests.py:42`) become `pyproject.toml`,
`uv.lock`, `tests/waits.py` and any `conftest.py`. The triggers for removed modules, an unknown base
and `MAX_FRACTION` stay. `tools/` and `.nw/` are no longer prefix triggers. `--strict` (or env
`NW_AFFECTED_STRICT=1`) restores the SPEC-0328 triggers exactly, including the `tools/` and `.nw/`
prefixes. The flag and the env are equivalent; either one turns strict mode on.

**Reference index.** `_scan` already parses every module under `src/nightwright` and `tests`. It
also collects, per module, (a) every `str` `ast.Constant` and (b) every imported top-level name.
For a changed path `rel`, the *needles* are:
- the basename (for example `remote_gates_golden.json`, `project.yaml`, `affected_tests.py`);
- for `tools/X.py`, also the stem `X` (tests load tools with `importlib`/`sys.path` or
  `subprocess [... "tools/X.py"]`, and an import of `X` counts);
- the name of the parent directory, when the file sits below the top level of `tests/`,
  `src/nightwright/` or `.nw/` (for example `data` for `tests/data/*.json`, `personas` for
  `src/nightwright/agentic/personas/*.md`, `.nw` for `.nw/project.yaml`). This catches
  `Path(__file__).parent / "data"` and glob-style readers.

A module *references* `rel` when one of its string constants contains a needle (for a directory
needle, the constant is equal to the needle or ends with `/<needle>` or `<needle>/`), or when the
needle is the stem of a module it imports. A referencing **test** module is selected, and so are the
test modules importing it (the existing free hops through helpers). A referencing **src** module
selects its `nearest_tests` (this covers package data read by src code). Matching on basenames
errs toward selecting more, and `MAX_FRACTION` caps the result.

**Rules by path class** (applied per changed file, results unioned):

| Changed path | Selection | If empty |
|---|---|---|
| `pyproject.toml`, `uv.lock`, `conftest.py`, `tests/waits.py` | full suite | n/a |
| `.nw/**` | reference scan (finds `test_agentic_dogfood_config`, `test_agentic_gate_paths`, …) | full |
| `tests/**` non-`.py` (test data) | reference scan | full |
| `src/nightwright/**` non-`.py` (package data) | reference scan, then nearest tests | full |
| `tools/*.py` and other `tools/**` | reference scan | full |
| `src/nightwright/**.py` | `nearest_tests` (unchanged), then the **name rule** if empty | full |
| `tests/**.py` | unchanged (itself and importers) | n/a |
| anything else (docs, specs, …) | nothing (unchanged) | n/a |

**Name rule** (an unreached src module, for example a new `cli_workers.py`). Take the module's leaf
name, and also the leaf with the prefixes `cli_` / `_` stripped (`cli_workers` gives `cli_workers`
and `workers`). Select the test files under `tests/` whose stem contains either token as a whole
`_`-delimited run (`test_agentic_workers.py`, `test_agentic_workers_bootstrap.py`, but not
`test_coworkers.py`). Also select the test modules whose string constants contain the
module's dotted name, or a CLI command name equal to the token with `_` replaced by `-`. An empty
result is still the full fallback. The reason string says which rule fired:
`affected: N/M test files (name rule for src/.../cli_workers.py; reference scan for .nw/project.yaml)`.

**Smoke set.** Every path in `--include` is added to every non-full selection, whatever rule
produced it, so the configured smoke tests run on each narrowed round.

**Why these needles.** They are a grep of real readers: 20 test modules mention `project.yaml`
(about 9 % of 216 test files), and the golden data file is read by exactly its golden test. The
needles reach every test that names the file, and the cap stops a needle that is too broad from
costing more than SPEC-0328's fallback already did.

### 2. Report and JSON mode

**Report.** When env `NW_GATE_REPORT` is set, `affected_tests.py` writes a JSON report there:
`{full, reason, tests, include, changed, base_sha, head_sha, head_tree}`. `tests` is the sorted
selected test file list (the full list when `full` is true is not required: it may be empty with
`full: true`). `base_sha` is the resolved base commit, `head_sha` and `head_tree` are `HEAD` and
`HEAD^{tree}` of `--repo`, and `include` is the smoke set as passed. Writing is best effort: a
write error is ignored, and a missing parent directory is created once. The report is written for
`--run` and for plain selection runs, and does not change the exit code or the printed output.

**JSON mode.** `--json` prints the same object (plus `closure` when `--closure` is given) on stdout
as a single line and prints nothing else there (diagnostics go to stderr). With `--run` the JSON is
printed before the command starts. `--changed PATH` (repeatable, repo-relative) replaces
`changed_files(repo, base)` with the given list, so a caller can ask about an arbitrary change set,
for example `D = git diff --name-only G H` on a checkout of `H`, without needing a branch. The base is then
not resolved and `base_sha` is `null`.

**Forward closure.** `closure` is the sorted list of repo-relative paths the selected tests read,
by the same static reasoning the selection uses, computed as a fixed point:
- every selected test file;
- the modules it imports, transitively, within `src/nightwright`, `tests` and `tools` (the import
  graph, forward direction);
- every non-Python file that a closure module references by needle, i.e. a path `p` for which the
  reference scan of section 1 would select that module (this is the same scan, run in the reverse
  direction over all tracked non-Python files under `tests/`, `src/nightwright/`, `.nw/` and
  `tools/`).
When `full` is true the closure is `null` (the whole tree), not a list. The closure is a pure
function of the tree and the selection; it reads no git state beyond the tracked file list.

### Out of scope here

No gate, close or queue code changes. The tool's callers (`.nw/project.yaml` gate commands) keep
working unchanged, because the new flags and the env var are additive and strict mode is opt-in.

## Alternatives considered

- **Keep the prefix triggers and only whitelist `.nw/project.yaml`.** It fixes one recorded case
  and leaves test data, tools and unreached modules full. The reference scan handles all of them
  with one mechanism.
- **Coverage-based mapping for data files.** It needs an instrumented seeding run, which costs
  what we are trying to save (SPEC-0328 analysis).
- **Make the report a gate-runner concern only.** The tool already knows `full`, `reason` and the
  list; having the runner reconstruct them from stdout would parse human text. The tool writes the
  report, and the runner (part 3) only points `NW_GATE_REPORT` at a file.

## Acceptance criteria

- [ ] `affected_tests.py` selects a targeted, non-full set for a `.nw/project.yaml` change, a
      `tests/data/*.json` change, a `tools/*.py` change, a package-data change under
      `src/nightwright/`, and a src module reached by no test but matched by the name rule. Each
      reason names the rule that fired.
- [ ] `pyproject.toml`, `uv.lock`, `conftest.py`, `tests/waits.py`, a removed module, an unmapped
      data file, an unmatched unreached module, and a selection above `MAX_FRACTION` still give the
      full suite. `--strict` / `NW_AFFECTED_STRICT=1` reproduce SPEC-0328 selection exactly.
- [ ] The smoke set (`--include`) is added to every non-full selection.
- [ ] With `NW_GATE_REPORT` set, the tool writes a JSON file with the keys `full`, `reason`,
      `tests`, `include`, `changed`, `base_sha`, `head_sha`, `head_tree`; a write error does not
      change the exit code or output.
- [ ] `--json` prints one JSON object on stdout with those keys; `--closure` adds `closure` (a
      sorted path list, `null` when `full`); `--changed PATH` selects for the given paths without
      resolving a base.
- [ ] The replay test passes on the real repository tree: the recorded `.nw/project.yaml`,
      `tests/data/remote_gates_golden.json` and new `cli_workers.py` changes give non-full
      selections under `MAX_FRACTION` that include `test_agentic_dogfood_config.py`, the golden
      test and a `*workers*` test respectively; `pyproject.toml` and `uv.lock` stay full.
- [ ] The close tier is unchanged: `.nw/project.yaml` still defines the full `pytest` close gate,
      and no gate, close or queue module is modified by this spec.

## Test plan

- **Automated: mapping rules** (`tests/test_affected_tests.py`, existing temp-git-repo fixture,
  extended with `.nw/project.yaml`, `tests/data/golden.json`, `tools/tool_x.py`,
  `src/nightwright/pkg/data/x.md` and a `cli_widgets.py` hub module):
  - one test per row of the rules table: expected selected files, `full` false (or true for the
    full rows), and the reason names the rule;
  - the needle forms: a basename constant, `Path(...)/"data"`, a `tools` stem import, a subprocess
    path, a dotted module name and a CLI command name;
  - the empty-mapping fallbacks (an unreferenced data file, a name-rule miss) give the full suite,
    and `test_coworkers.py` is not matched by the token `workers`;
  - `--strict` and the env give the SPEC-0328 results (the SPEC-0328 tests keep passing under
    strict mode); the smoke set is always appended.
- **Automated: report and JSON** (`tests/test_affected_tests.py`): with `NW_GATE_REPORT` set the
  file has the eight keys and the right `full`/`tests`; an unwritable path is ignored; `--json`
  stdout parses as one object; `--closure` contains the imported helper and a referenced data file
  and is `null` for a full selection; `--changed` selects without a base and gives `base_sha`
  `null`.
- **Automated: replay** (`tests/test_affected_tests_replay.py`, fixture
  `tests/data/affected_fallback_replay.json`). This file holds the recorded 2026-11-01 fallbacks:
  `.nw/project.yaml` (SPEC-0338), `tests/data/remote_gates_golden.json` (SPEC-0338), and a new
  `src/nightwright/agentic/cli_workers.py` (SPEC-0354), plus controls `pyproject.toml` and
  `uv.lock`. Run against the **real repository tree**, with missing files staged as temporary
  copies, the three recorded reasons must now give a non-full selection under `MAX_FRACTION`
  that includes `test_agentic_dogfood_config.py`, the golden test and a `*workers*` test
  respectively. The controls stay full.
- **Manual verification:** `git checkout <SPEC-0338 branch tip>` in a scratch worktree, run
  `python3 tools/affected_tests.py --base <its base>` and record the selection size against the
  old full fallback in *As shipped*; the same for the SPEC-0354 tip; and
  `python3 tools/affected_tests.py --json --closure --changed .nw/project.yaml` to confirm the JSON
  mode on the real tree.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_affected_tests.py`, `test_agentic_gate*` and `test_agentic_dogfood_config`;
  `python3 tools/spec_lint.py` exits 0. The full suite runs at the queue's close gate.

## Risks

- **Basename needles too narrow** (a file opened by a computed name that contains no needle). A
  data change can then select a set that misses its reader. *Mitigation:* the parent-directory
  needle catches directory-level readers, `MAX_FRACTION` bounds over-selection, and the close tier
  still runs the full suite on every merge. `--strict` is the rollback.
- **Name rule misses** (an unreached module tested only through the CLI under another name). The
  result is empty and falls back to the full suite, or the close tier catches it. A new module with
  no tests is a test-coverage finding in any case.
- **Over-wide closure.** The reverse reference scan can include files that only share a basename.
  The closure errs toward more paths, which only makes later consumers more conservative.

## Clock Log

CLOCK-IN: n/a
CLOCK-OUT: n/a

## Rollout / migration

1. Ship the mapping, the report and the JSON mode together (default **on**; `--strict` /
   `NW_AFFECTED_STRICT=1` is the rollback). It only affects the loop tier; the close tier still
   runs the full suite.
2. `.nw/project.yaml` needs no change: the `pytest-affected` command already invokes the tool, and
   the new flags are optional.
3. Watch the loop-tier fallback rate in gate logs for a week, and record the selection sizes and
   any missed-reader case in *As shipped*.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions (queue close gate full suite).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## Open questions

None.
