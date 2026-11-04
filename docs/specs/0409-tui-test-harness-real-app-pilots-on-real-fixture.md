---
id: SPEC-0409
title: "Remove Textual/pyte test skips, add the AST guard and a TUI test runtime budget"
status: done
owner: user
created: 2026-11-03
updated: 2026-11-03
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0405
source_idea: IDEA-885
tags: [tui, testing, agentic]
---

## Context

Part of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md), goal G7:
"the TUI tests run in every `uv run pytest`". The idea behind this spec (IDEA-885) also
covered a real-app harness, text snapshots and a scale-clone helper. Those need the epic's
sanitized real-data fixture, which does not exist yet. A reviewer split the idea in two. This
spec is the first half: it needs no fixture and can land now. The second half is listed under
*Non-goals* and is filed as its own spec.

**What exists today**

- **Textual is core.** Since [SPEC-0368](./0368-remote-gating-red-on-current-trees-human-visible.md)
  `textual>=8,<9` is a core dependency (`pyproject.toml`) and `tui = []` is an empty alias. A
  plain `uv run` imports it. pyte is in the default `dev` dependency group that `uv run`
  installs.
- **The skip guards survived.** Written for the old opt-in extra
  ([SPEC-0099](./0099-optional-textual-packaging-and-wt-tui-entry.md)), they still exist:
  - `tests/conftest.py` `collect_ignore`s five files (`test_tui_queue_tab.py`,
    `test_tui_campaign_screen.py`, `test_tui_mailbox_tab.py`, `test_tui_events_tab.py`,
    `test_tui_escalations_tab.py`) when `find_spec("textual")` is falsy.
  - `pytest.importorskip("textual")` sits at the top of `test_tui_queue_tab.py`,
    `test_tui_campaign_screen.py`, `test_tui_mailbox_tab.py` and `test_tui_events_tab.py`.
  - A `needs_textual = pytest.mark.skipif(not T.textual_available(), ...)` marker is repeated
    in about 19 test files (`test_tui_desk.py`, `test_tui_hub.py`, `test_tui_search.py`,
    `test_idea_tree.py`, `test_desk_table.py`, `test_clock_json_parity.py`, ...);
    `test_tui_resilience.py` also gates imports behind `if T.textual_available():`.
  - `test_tui_real_terminal.py` additionally has `pytest.importorskip("pyte")`.
  - Any environment that lacks Textual or pyte therefore reports green while running no TUI
    test. This is the silent-skip hazard the epic names.
  - `textual_available()` itself (`src/nightwright/tui/__init__.py`) stays: `nw tui` uses it
    for its friendly fallback, and `test_tui_entry.py` monkeypatches it to test that fallback.
    That is not a skip.
- **No runtime budget exists.** `tests/test_tui_queue_tab.py` alone takes about 78 s for 38
  tests (measured 2026-11-03). More TUI tests will grow that without anyone noticing.
- **Waits are bounded elsewhere.** `tests/waits.py` (SPEC-0269) provides the `NW_TEST_WAIT_S`
  convention that the budget's scaling variable follows.

## Goals / Non-goals

**Goals**
- **G1.** No TUI test can be silently skipped or un-collected for a missing Textual or pyte,
  and a guard test fails if any such skip comes back.
- **G2.** A runtime budget for the TUI test files, enforced by the suite itself, that catches
  a runaway test or file without flaking on slow hosts.

**Non-goals**
- The real-app harness `tests/tui_harness.py` (cfg over a copied fixture data dir, `IdeaDesk`
  via `run_test`, double-click through `Pilot.double_click`, visible-text extraction), the
  plain-text pyte screen snapshots at 160x48 with `NW_UPDATE_SNAPSHOTS=1`, and the
  scale-clone helper `tests/scale_clone.py`. Together with the refactors of
  `test_tui_real_terminal.py` and `test_tui_queue_tab.py` onto them, these form a separate
  follow-on spec under the same epic. That spec depends on the epic's fixture and on this
  spec, and carries a collision-safe deterministic id rewrite for the clone helper. Nothing
  here constrains its design.
- The History, Findings, Queue and popup views and their tests (other epic children).
- A new snapshot dependency such as `pytest-textual-snapshot`.
- Changing `textual_available()` or `nw tui`'s runtime fallback.
- Rewriting existing TUI tests beyond deleting their skip guards.

## Decision

1. **Delete every Textual and pyte skip in the test suite.**
   - Textual and pyte are unconditional test requirements. A missing one must surface as an
     import error or a failing test, never a skip.
   - Platform skips unrelated to packaging stay. `test_tui_real_terminal.py` keeps its
     `hasattr(os, "forkpty")` skip.
2. **Pin that with an AST guard test**, `tests/test_no_textual_skips.py`. It scans all of
   `tests/` and fails on any skip construct for Textual or pyte, and on any `collect_ignore`
   in a conftest.
3. **Enforce a runtime budget with a pytest plugin**, `tests/tui_budget.py`, registered from
   `conftest.py`. It fails the run when the TUI files go over per-test, per-file or total
   limits. All limits scale with one environment variable, like `NW_TEST_WAIT_S`.

## Design

### 1. Guard removal and the guard test

**Removals**

- `tests/conftest.py`: delete `collect_ignore`, the `importlib.util` import and its comment.
- Every `pytest.importorskip("textual")` and `pytest.importorskip("pyte")` call.
- Every `needs_textual` definition and use, and every `pytestmark = needs_textual`.
- Every `if T.textual_available():` import gate in a test (`test_tui_resilience.py`). The
  imports become unconditional.
- The `not T.textual_available()` half of `test_tui_real_terminal.py`'s `skipif`.
- A bare `from nightwright import tui as T` stays wherever the file still uses `T`.

**`tests/test_no_textual_skips.py`**

It parses every `tests/**/*.py` with `ast` and reports each violation as
`path:line: message`. A violation is any of:

- **a skip construct on Textual or pyte.** A call to `importorskip`, `skip`, `skipif` or
  `xfail`, or an `if`/`ifexp` test, whose subtree mentions `textual_available`, a
  `find_spec("textual")` or `find_spec("pyte")` call, or a string constant equal to
  `"textual"`, `"textual.*"` or `"pyte"`.
- **a name** `needs_textual`, `needs_pyte`, or any `collect_ignore*`, anywhere in a test
  module or conftest.

Allowed, and pinned by the guard's own self-test: `monkeypatch.setattr(T,
"textual_available", ...)` (the `nw tui` fallback tests in `test_tui_entry.py`) and the
`hasattr(os, "forkpty")` skip. The guard's own synthetic violating sources are string
literals, never parsed as code by the scan, so the scan does not flag them.

A second test runs `python -m pytest --collect-only -q tests/test_tui_*.py` in a subprocess
and asserts exit code 0 and that every one of these files contributed at least one collected
test. That catches a TUI file that is silently un-collected by some other mechanism.

### 2. Runtime budget: `tests/tui_budget.py`

A pytest plugin whose hooks are module-level functions. Its core is a pure function,
`evaluate(durations, limits) -> list[Violation]`.

- **Registration.** `tests/conftest.py` registers the module in `pytest_configure` with
  `config.pluginmanager.register(...)` unless `has_plugin("tui_budget")` is already true.
  It does not use `pytest_plugins`, so it works whichever directory pytest is started from.
  A test can also load it into a subprocess with `-p tui_budget` and `tests/` on
  `PYTHONPATH`.
- **Scope.** Test items from files named `tests/test_tui_*.py`. Their duration is the sum of
  setup, call and teardown from `pytest_runtest_logreport`. Under xdist the controller
  receives every worker's reports, and the verdict is computed on the controller only (the
  plugin does nothing when `config.workerinput` exists).
- **Defaults** (summed test durations, not wall time):
  - per test: 30 s
  - per file: 180 s
  - all TUI files together: 900 s

  They are about 2x the current worst case (78 s for `test_tui_queue_tab.py`), so they leave
  room for new pilots but catch a runaway.
- **Scaling.** All three are multiplied by `NW_TUI_BUDGET_SCALE` (float, default 1). The
  individual limits can also be overridden with `NW_TUI_TEST_BUDGET_S`,
  `NW_TUI_FILE_BUDGET_S` and `NW_TUI_TOTAL_BUDGET_S`. A malformed value fails the run with a
  clear message rather than disabling the budget.
- **Partial runs.** A partial run (affected-tests, a single file) is judged only on the items
  that ran. The total is checked only against items that ran. A run with `--collect-only` or
  with no TUI item prints nothing and passes.
- **Outcome.** On a violation it prints a table of offenders (worst first) in the terminal
  summary and sets the session exit status to `TESTS_FAILED`. Otherwise it prints a single
  line with the slowest TUI file.

## Alternatives considered

- **Keep the skips but make them loud (a `warnings.warn` or a count in the summary).**
  Lost: a warning is still a green run with no TUI test. The point is that a missing
  dependency must break the run.
- **Time budget as a wall-clock timer around the whole TUI set.** Lost: unreliable under
  xdist and the host-wide gate-slot pool. Summed test durations are stable and attributable
  to a file.
- **Make the guard a lint rule or a pre-commit hook.** Lost: `uv run pytest` is the one gate
  every campaign already runs, and the repo already pins rules with tests (for example
  `tests/test_test_waits.py`).
- **Register the plugin with `pytest_plugins` in `tests/conftest.py`.** Lost: pytest rejects
  `pytest_plugins` in a non-top-level conftest in some invocation shapes; explicit
  registration in `pytest_configure` has no such restriction.

## Acceptance criteria

- [x] **AC1.** `tests/conftest.py` defines no `collect_ignore` and does not import
  `importlib.util` for it. `grep -rnE
  'importorskip\("(textual|pyte)|needs_textual|textual_available\(\)'` over `tests/`
  matches nothing except the allowed `monkeypatch.setattr(T, "textual_available", ...)`
  lines in `test_tui_entry.py` and the guard test's own synthetic sources.
- [x] **AC2.** `tests/test_no_textual_skips.py` passes on the current tree and fails with a
  `path:line` message on synthetic sources for each violation kind: `importorskip`,
  `skipif`, `skip`, an `if` gate, `needs_textual`, and `collect_ignore`. It accepts the
  `monkeypatch.setattr(T, "textual_available", ...)` and `hasattr(os, "forkpty")` forms.
- [x] **AC3.** Collecting `tests/test_tui_*.py` exits 0, collects tests from every one of
  those files, and no test of those files is reported skipped for Textual or pyte.
- [x] **AC4.** The budget plugin's `evaluate` flags per-test, per-file and total violations,
  applies `NW_TUI_BUDGET_SCALE` and the three per-limit overrides, and is inert under
  xdist workers and on runs with no TUI item.
- [x] **AC5.** A subprocess run of a tiny `test_tui_x.py` that sleeps, with a tiny
  `NW_TUI_TEST_BUDGET_S`, exits non-zero and names the offender. With default limits the
  repo's own TUI files pass, and the measured per-file times are recorded in this spec's
  *As shipped* section.
- [x] **AC6.** `python3 tools/spec_lint.py` exits 0, and the affected tests pass
  (`python3 tools/affected_tests.py --run uv run pytest -q`).

## Test plan

**Automated tests** (all run in a plain `uv run pytest`; none may skip)

- `tests/test_no_textual_skips.py`
  - `test_tree_has_no_textual_or_pyte_skips`: scans the real `tests/` tree; asserts no
    violations (AC1).
  - `test_guard_detects_each_violation_kind`: parametrised over synthetic source strings
    (`importorskip("textual")`, a `skipif(not textual_available())` decorator, a
    `pytestmark`, an `if not find_spec("textual"):` gate, `needs_textual = ...`,
    `collect_ignore = [...]` in a conftest). Asserts a violation with the right line (AC2).
  - `test_guard_allows_fallback_monkeypatch_and_platform_skip`: the allowed forms produce no
    violation (AC2).
  - `test_tui_files_collect_without_skips`: subprocess `pytest --collect-only -q
    tests/test_tui_*.py`; asserts exit 0 and at least one collected test per file (AC3).
- `tests/test_tui_budget.py`
  - Unit tests of `evaluate`: per-test, per-file and total violations, ordering worst first,
    scale and per-limit overrides, malformed env value (AC4).
  - Hook tests with a fake `config`: inert when `workerinput` exists and when no TUI item
    reported (AC4).
  - Subprocess test: a tmp dir with a sleeping `test_tui_x.py`, run with `-p tui_budget`,
    `-p no:cacheprovider` and a tiny `NW_TUI_TEST_BUDGET_S`, exits non-zero and the output
    names `test_tui_x.py` (AC5).
- Existing `test_tui_*`, `test_idea_tree`, `test_desk_table`, `test_idea_project` and
  `test_clock_json_parity` suites, now un-gated (AC1, AC3).

**Manual verification**

1. `uv run pytest tests/test_no_textual_skips.py tests/test_tui_budget.py -q` is green.
   Output shows no `s` (skipped) markers and no import errors.
2. Prove the guard bites: temporarily add `pytest.importorskip("textual")` to any TUI test
   file and confirm `test_no_textual_skips.py` fails with that file and line. Revert it.
3. Prove that nothing is skipped when Textual is unavailable: run `uv run pytest
   tests/test_tui_queue_tab.py -q` in an environment where `textual` is made unimportable
   (for example a `sitecustomize` that blocks the import, in a throwaway directory). Confirm
   the result is a collection error, not a green run with the file ignored.
4. Run the TUI files with `uv run pytest tests/test_tui_*.py -q` and record the per-file
   timings from the budget plugin's summary line in *As shipped*.

**Regression guard**

- Several previously ignored or skipped files now run in every environment, which is the
  intended change. They must stay green.
- `test_tui_real_terminal.py`'s assertions are unchanged; only its skip guards are removed.
- `python3 tools/spec_lint.py` exits 0.

### Test record (2026-11-03)

- `uv run pytest -q tests/test_no_textual_skips.py tests/test_tui_budget.py`: 52 passed, none skipped.
- `uv run pytest -n 4 -v tests/test_tui_*.py tests/test_idea_tree.py tests/test_desk_table.py
  tests/test_idea_project.py tests/test_clock_json_parity.py tests/test_question_rationale.py`:
  470 passed in 126 s, no skips; the budget line reported ok.
- Manual 2: appended `pytest.importorskip("textual")` to `tests/test_tui_desk.py`; the guard test
  failed with `tests/test_tui_desk.py:404: importorskip() on textual/pyte`. Reverted.
- Manual 3: with `textual` blocked by a throwaway `sitecustomize` on `PYTHONPATH`,
  `pytest tests/test_tui_queue_tab.py` ended `Interrupted: 1 error during collection`
  (`ImportError: textual blocked for test`), not a green run.
- `python3 tools/affected_tests.py --run uv run pytest -q -n 4` (conftest changed, so nearly
  everything was selected): 6495 passed, 5 skipped (none for Textual/pyte), 2 failed
  (`test_specs.py::test_all_specs_pass_lint`, `test_export.py::test_internal_spec_lint_unaffected_by_outbox`)
  because the run began before the ledger was regenerated; both pass on re-run after
  `python3 tools/spec_lint.py --write-ledger`. `python3 tools/spec_lint.py` exits 0.
- The full suite was left to gating.

### As shipped (2026-11-03)

- Removed every Textual/pyte skip in 19 test files plus the `collect_ignore` and `importlib.util`
  import in `tests/conftest.py`; the now-unused `pytest` and `T` imports went with them.
  `test_tui_real_terminal.py` imports `pyte` unconditionally and keeps only its `forkpty` skip.
- `tests/test_no_textual_skips.py`: one deviation from the Design wording. An `if`/`ifexp` test
  counts a bare `"textual"` string only through `find_spec(...)`; a plain string in a conditional
  is ordinary text (`test_tui_highlight.py` does `re.search("textual", ...)` on rendered
  output). Skip calls (`importorskip`, `skip`, `skipif`, `xfail`) still flag the bare string. The
  collect-only subprocess also passes `-p no:cacheprovider`.
- `tests/tui_budget.py`: the scale multiplies a per-limit override too
  (`limit = (override or default) * NW_TUI_BUDGET_SCALE`). A non-positive, non-finite or
  non-numeric value raises `pytest.UsageError` naming the variable. Violations print under the
  heading `TUI runtime budget exceeded`; per-file times print when `-v` is given. Defaults
  (30 s / 180 s / 900 s) were kept.
- Measured summed test time per file, `-n 4` on the dev box (20 TUI files, 421.6 s total;
  the five most expensive, rest under 15 s each): `test_tui_queue_tab.py` 105.4 s,
  `test_tui_mailbox_tab.py` 89.7 s, `test_tui_escalations_tab.py` 65.3 s,
  `test_tui_events_tab.py` 49.5 s, `test_tui_campaign_screen.py` 29.0 s. All under the limits.

## Rollout / migration

1. Remove the guards and add the guard test in one change, so the tree never has a window in
   which a new skip can enter.
2. Add the budget plugin and its tests, and register it from `conftest.py`. Run the TUI
   files once and record the measured timings; if any default limit is too tight on the dev
   box, retune it from that evidence.
3. No data migration. The only behaviour change is that files previously ignored for a
   missing Textual now always run.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (affected tests via `python3
  tools/affected_tests.py --run uv run pytest -q`, plus the files touched); manual steps
  performed and noted here.
- [x] No regressions at the queue's close gate.
- [x] Spec body updated to match what shipped (including the measured TUI file timings).
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
  `python3 tools/spec_lint.py` exits 0.

## Open questions

None. Decided while writing:

- **Why is pyte covered by the guard, and not just Textual?** The real-terminal test needs
  it, it is in the default `dev` group that `uv run` installs, and a skipped real-terminal
  test is the same silent hazard.
- **Why test durations and not wall time?** See *Alternatives considered*.
- **Why 30 s / 180 s / 900 s?** They are about 2x today's worst case (78 s per file).
  `NW_TUI_BUDGET_SCALE` is the tuning knob for slow hosts. The numbers are retuned from the
  evidence recorded in *As shipped* if real runs show they are too tight or too loose.
- **Why split from the harness?** The harness, snapshots and clone helper need the epic's
  fixture; this half does not, and landing it first closes the silent-skip hazard today.
