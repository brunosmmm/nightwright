---
id: SPEC-0421
title: "Always-run guard tests at close and loop gates"
status: accepted
owner: user
created: 2026-11-04
source_idea: IDEA-927
parent: SPEC-0419
milestone: "M9: agentic workflow"
tags: [agentic, gates, tests, affected-tests]
---

## Context

Child 2 of [SPEC-0419](./0419-self-healing-queue.md). Since
[SPEC-0378](./0378-affected-only-close-gate.md), both nightwright `pytest` gates in
`.nw/project.yaml` (`pytest-affected` for the loop, `pytest` for close) run `python3
tools/affected_tests.py --include tests/test_agentic_dogfood_config.py --include
tests/test_agentic_project.py --run uv run pytest …`. The selection maps changed files to tests
through the static import graph and a reference scan (`select`, `tools/affected_tests.py:498-593`).

Some tests do not import what they check. They scan the tree instead:

- `tests/test_test_waits.py` globs `tests/test_*.py` for private wait helpers and bare
  `.join(N)` (SPEC-0269);
- `tests/test_no_textual_skips.py` walks `tests/**/*.py` for skip constructs (SPEC-0409);
- `tests/test_rich_output_guard.py` walks `src/nightwright` for raw prints (SPEC-0185).

A branch that adds a test file selects that file and its importers, never these guards.
`_referencing()` (`:422-438`) adds directory readers only in `wide` (closure) mode, and returns
early for a parent in `TOP_DIRS`, so a top-level `tests/` file has no directory readers at all.

The close gate runs on the branch rebased onto main. A guard that landed on main after the branch
was cut is in that tree, but it is not selected. The branch merges green, and the next full-suite
sentinel finds main red. This happened twice in the audited window:

- SPEC-0410's `importorskip` against SPEC-0409's guard (fixed by hand in `cd75e6d6`);
- dashboard tests using `.join(N)` against the SPEC-0269 guard (fixed in `91d4811e`).

## Goals / Non-goals

**Goals**
- Every non-full selection of both pytest gates includes every registered **tree guard**, read
  from the tree being tested. A guard that landed on main is then part of every later close.
- The registry is explicit and self-maintaining. A tree-scanning test module that is not
  registered fails a test.
- A new or changed test or src file also selects the test modules that read its directory tree,
  even when they are not registered guards.

**Non-goals**
- Changing full-suite triggers, the sentinel, or `--strict` mode (SPEC-0328 rules stay exact
  there).
- Making the guards faster. Their combined runtime is measured and recorded (AC6).

## Decision

1. The registry is a pytest marker, `tree_guard`, set at module level (`pytestmark =
   pytest.mark.tree_guard`, or in a `pytestmark` list) and registered in `pyproject.toml`.
   `tools/affected_tests.py` (stdlib-only) finds the marked modules statically with the AST. A
   marker beats a list in `.nw/project.yaml` because the guard and its registration land in the
   same file and the same commit. Any branch rebased onto that commit picks it up, with no second
   file to keep in sync.
2. `tools/affected_tests.py --guards` adds every marked module to a non-full selection, after
   `--include`. With `--guards`, an otherwise empty selection still runs the guards, instead of
   printing "no affected tests". Both pytest gates in `.nw/project.yaml` pass `--guards`.
3. **Tree readers.** A new AST rule, `_tree_roots(info)`, records which repo roots a module scans:
   `tests`, `src/nightwright` or the repo root. A changed or added file under one of those roots
   selects every test module that scans that root or an ancestor of it. This is directory-reader
   selection for top-level files, which `_referencing` skips today.
4. `tests/test_guard_registry.py` (itself a `tree_guard`) asserts that registered and detected
   tree-scanning modules agree, apart from an explicit, reasoned exemption dict.

## Design

### `tools/affected_tests.py`

- **`_Info` gains `tree_roots`.** This is the set of roots the module scans, computed from its AST
  nodes in the existing single walk:
  1. Collect module-level names bound to a path expression derived from `Path(__file__)`:
     `Path(__file__).parent`, `.resolve().parent`, `.parents[N]`, `/ "x"` joins, and names
     assigned from other such names (`ROOT = TESTS.parent`). Each name resolves to a repo-relative
     directory: `tests` for a test module's `Path(__file__).parent`, `.` for `parents[1]`, and so
     on. The resolution uses the module's own path.
  2. A scan is a call to `.glob(`, `.rglob(` or `.iterdir(` on such a name or expression, or
     `os.walk(` / `os.scandir(` / `os.listdir(` on one, or `ast.parse` over files from one of
     those.
  3. `tree_roots` is the set of resolved directories under scan, normalized to one of
     `TREE_ROOTS = ("tests", "src/nightwright", "src", ".")`.
- **`_guards(ix)`** returns the sorted test module paths whose AST assigns `pytestmark` at module
  level to an expression containing the attribute `tree_guard`: `pytest.mark.tree_guard`, a list
  or tuple holding it, or a combination.
- **`select()`**. For each changed path `rel` (added files included, since `_changed_since`
  already lists untracked files):
  - If `rel` is under `tests/` or `src/nightwright/`, add every test module whose `tree_roots`
    contains a root that is `rel`'s root or an ancestor of it.
  - The rule is appended to `rules` as `tree readers of <root> for <rel>`.
  - It applies in non-strict mode only, like the other SPEC-0378 rules.
- **`main()`** gets `--guards` (flag, default off, so callers keep their behaviour):
  - when the selection is not full, `sel.tests |= _guards(ix)`;
  - the JSON report gains `"guards": [...]`;
  - the stderr reason gains `+N guards`;
  - the "empty selection → exit 0 without running" branch is skipped when guards were added.

  `Index` is built once and reused; the guard scan reads the AST already parsed by `Index`.
- **`closure()`** is unchanged: it already adds directory readers in wide mode.

### Registry

- `pyproject.toml` `[tool.pytest.ini_options] markers` gains `"tree_guard: scans the tests or src
  tree; always selected by tools/affected_tests.py --guards (SPEC-0421)"`.
- Every module that `tree_roots` detects as scanning `tests` or `src/nightwright` for `*.py`
  sources gets `pytestmark = pytest.mark.tree_guard`, unless it is exempt. A module with an
  existing `pytestmark` uses a list. The implementer runs the detector over `tests/` and lists the
  final set in As shipped. At least these are marked: `test_test_waits.py`,
  `test_no_textual_skips.py`, `test_rich_output_guard.py` and `test_guard_registry.py`.
- `tests/test_guard_registry.py`:
  - `GUARD_EXEMPT: dict[str, str]` maps a module to the reason it scans without being a guard,
    for example a test that globs fixture files under `tests/fixtures/`.
  - Tests:
    - (a) every detected tree scanner of `tests` / `src/nightwright` is marked or exempt;
    - (b) every marked module is detected as a tree scanner (no stale marks);
    - (c) no module is both marked and exempt;
    - (d) the `.nw/project.yaml` `pytest-affected` and `pytest` gate commands contain `--guards`;
    - (e) the marker is registered in `pyproject.toml`.

  It imports `tools/affected_tests.py` the way `tests/test_affected_tests.py` does.

### `.nw/project.yaml`

Both `pytest-affected` (loop) and `pytest` (close) `cmd`s gain `--guards`, placed before `--run`.
The pre-SPEC-0328 fallback branch of `pytest-affected` (`if [ -f tools/affected_tests.py ]`) is
unchanged. A worktree whose `tools/affected_tests.py` predates `--guards` would fail on the
unknown flag; at the close re-gate the rebased tree always has the new tool, because the gate list
and the tool both come from main. The loop gate runs in the un-rebased worktree, so its cmd
becomes `python3 tools/affected_tests.py --help | grep -q -- --guards && G=--guards || G=; python3
tools/affected_tests.py $G …`. Old branches then keep working. Remote gates run the same cmd
string and need no change.

### Docs

`docs/WORKFLOW.md` (affected-tests section) gains one paragraph: tree guards, the marker, and how
to register a new guard.

## Alternatives considered

- **A list in `.nw/project.yaml` (`gates[].always`)**. Lost: a second file to update with every
  new guard, and the drift check would still need the AST detector.
- **Always run the full suite at close.** Lost: it reverses SPEC-0378. The full suite takes 13 to
  19 min, against a few seconds for the guards.
- **Make guards import what they scan.** Lost: there is no module to import. They check files.
- **Only the tree-reader rule, no marker.** Lost: a guard over `tests/` would then run only when
  a test file changes. The SPEC-0409 case was a branch adding a test that used `importorskip`, so
  that one would be caught, but a guard over `src` (rich output) must run when a src file changes.
  That is the same rule with a different root, and it is covered. The marker adds the
  "always, even for a docs or tools change" guarantee and a named registry, at the cost of a few
  seconds.

## Acceptance criteria

- [ ] AC1: `python3 tools/affected_tests.py --guards --changed docs/README.md --json` (any
      non-full selection) lists every `tree_guard` module in `tests` and in `guards`.
      Without `--guards`, the output is unchanged from today, byte for byte, for the same input.
- [ ] AC2: `--guards --run CMD` with an otherwise empty selection runs CMD with the guards.
      A full selection appends nothing, as today.
- [ ] AC3: For a changed or added `tests/test_new.py`, the selection includes every test module
      that scans `tests`, registered or not. For a changed `src/nightwright/x.py`, it includes
      `tests/test_rich_output_guard.py`. Both are in non-strict mode only.
- [ ] AC4: `tests/test_guard_registry.py` passes on main and fails when the marker is removed from
      any registered guard, or when a new module that globs `tests/test_*.py` is added unmarked.
- [ ] AC5: Both pytest gate commands in `.nw/project.yaml` pass `--guards`. The loop gate still
      runs on a worktree whose `tools/affected_tests.py` lacks the flag.
- [ ] AC6: Replay of the audited incidents. At the parent of `91d4811e` (dashboard `.join(N)` on
      main), `python3 tools/affected_tests.py --guards --changed tests/test_dashboard_auth.py --changed
      tests/test_dashboard_server.py --run uv run pytest -q` selects `tests/test_test_waits.py` and exits non-zero.
      At `91d4811e` it exits 0. The guard set's runtime under `-n 4` is recorded in As shipped.

## Test plan

- **Automated tests**
  - `tests/test_affected_tests.py` (extend), on synthetic repos under `tmp_path` as the existing
    tests do:
    - `_guards` finds the single, list and tuple forms of `pytestmark` and ignores a function-level
      mark (AC1);
    - `--guards` JSON report and stderr reason;
    - an empty selection with guards runs CMD (a fake `--run` command that records argv) (AC2);
    - `_tree_roots` detects `Path(__file__).parent.glob`, `ROOT.rglob`, `os.walk(SRC)`, and does
      not detect a glob over `tmp_path` (AC3);
    - the tree-reader rule for an added test file and for a src file (AC3);
    - strict mode unchanged.
  - `tests/test_guard_registry.py` (new): the registry tests (AC4, AC5). Its negative cases build
    a temp copy of a guard without its marker, and a new unmarked globbing module, then assert
    that the detector reports them.
- **Real verification (beyond fakes)**
  - **Fixture replay test** (`tests/test_affected_tests_replay.py`, extend). A real git repo in
    `tmp_path`:
    - main gets a guard module (marked) that fails on any `.join(5)` in `tests/`;
    - a branch cut before it adds `tests/test_late.py` with `.join(5)`;
    - rebase the branch onto main and run the real `python3 tools/affected_tests.py --guards --run
      python3 -m pytest -q` (subprocess, real pytest);
    - assert exit ≠ 0 and the guard in the output. Without `--guards`, assert exit 0: this
      demonstrates the gap.
  - **Real-repo replay** (manual, AC6). In a scratch worktree of this repo at `91d4811e^`, run the
    AC6 command and record the exit code, the selection and the guard set runtime in As shipped.
    Then at `cd75e6d6^` run the same check with `--changed tests/test_tui_queue_parity.py --changed
    tests/test_tui_queue_view.py` and record that
    `tests/test_no_textual_skips.py` is selected and fails.
  - **Live gate check** (manual). After landing, the next campaign close's gate log
    (`nw campaign gates <id>`, or the `pytest` gate log) shows `+N guards` in the
    `affected_tests:` line, and the guard modules in the pytest node ids.
- **Regression guard**: `python3 tools/affected_tests.py --run uv run pytest -q` (affected),
  `python3 tools/spec_lint.py`. The existing `test_affected_tests*.py` tests pass unchanged.

## Clock Log

CLOCK-IN: [timestamp]
CLOCK-OUT: [timestamp]

## Rollout / migration

1. Land the tool change, the markers, the registry test and the `.nw/project.yaml` change in one
   branch, so the merge's own close runs with `--guards`.
2. Worktrees cut earlier keep working (AC5). Their close runs on the rebased tree with the new tool.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## As shipped

_To be filled at close: the final guard set, the exemption dict, the replay records and the guard
runtime._

## Open questions

_None._
