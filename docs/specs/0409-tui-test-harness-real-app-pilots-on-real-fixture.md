---
id: SPEC-0409
title: "TUI test harness: real-app pilots on real fixture data, no silent textual skips, text snapshots"
status: proposed
owner: user
created: 2026-11-03
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0405
source_idea: IDEA-885
tags: [tui, testing, agentic]
---

## Context

Child 3 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md) (goal G7:
"the TUI tests run in every `uv run pytest` and drive the real app against real captured
campaign data"). The epic's child 2 (shared history layer) ships the sanitized real-data
fixture `tests/fixtures/agentic_history/`; this spec builds the test infrastructure that
children 4 to 7 then reuse. It has no `depends_on` key because the linter only accepts ids
of existing specs; when child 2 is promoted, `depends_on: [<child 2>]` is added (see
*Rollout*).

**What exists today**

- **Textual is core.** Since [SPEC-0368](./0368-remote-gating-red-on-current-trees-human-visible.md)
  `textual>=8,<9` is a core dependency (`pyproject.toml`) and `tui = []` is an empty alias. A
  plain `uv run` imports it.
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
- **Each TUI test file builds its own scaffolding.** `test_tui_queue_tab.py` has its own
  `_desk` (an `IdeaDesk` with a `notify` spy), `settle`, `open_queue` and so on, over
  hand-built campaigns from `test_agentic_dashboard._campaign`. No test runs the desk over
  captured real data.
- **Pilot waits exist.** `tests/waits.py` (SPEC-0269) provides `pilot_until` and the
  `NW_TEST_WAIT_S` bound.
- **Real-terminal path exists.** `tests/test_tui_real_terminal.py` (SPEC-0115) spawns
  `nw tui` under a pty and feeds the bytes to `pyte` (the `_render` function), because
  Textual repaints incrementally. It is private to that file and fixed at 110x30.
- **A snapshot convention exists** in `tests/test_agentic_queue_visibility.py`: golden `.txt`
  files, rewritten only when `NW_UPDATE_SNAPSHOTS` is set.
- **No runtime budget exists.** `tests/test_tui_queue_tab.py` alone takes about 78 s for 38
  tests (measured 2026-11-03). Adding end-to-end pilots will grow that without anyone
  noticing.
- **No scale helper exists.** The epic's performance test (`tests/test_history_perf.py`,
  500 campaigns and about 50 000 records) needs a way to inflate the fixture.

## Goals / Non-goals

**Goals**
- **G1.** No TUI test can be silently skipped or un-collected for a missing Textual or pyte,
  and a guard test fails if any such skip comes back.
- **G2.** One shared harness, `tests/tui_harness.py`, to run the real `IdeaDesk` over a
  throwaway copy of the real fixture data dir. It gives the following to children 4 to 7
  and to the epic's e2e test:
  - cfg construction
  - app start through `run_test`
  - `pilot_until`-based waits
  - double-click through `Pilot.double_click`
  - visible-text extraction
- **G3.** Plain-text screen snapshots of the real app through the SPEC-0115 pyte path at
  160x48, regenerated only with `NW_UPDATE_SNAPSHOTS=1`.
- **G4.** A deterministic scale-clone helper that inflates a data dir to N campaigns, reused
  by the perf tests.
- **G5.** A runtime budget for the TUI test files, enforced by the suite itself.

**Non-goals**
- The History, Findings, Queue and popup views and their tests. Children 4 to 7 write those
  against this harness.
- The fixture itself and its capture tool (child 2).
- A new snapshot dependency such as `pytest-textual-snapshot`. The epic decided on pyte text
  snapshots because SVG goldens churn on every Textual minor bump.
- Rewriting every existing TUI test onto the harness. Only the two helpers that
  `test_tui_queue_tab.py` duplicates move (see *Design*).
- Changing `textual_available()` or `nw tui`'s runtime fallback.

## Decision

1. **Delete every Textual and pyte skip in the test suite.**
   - Textual and pyte are unconditional test requirements (Textual is core, pyte is in the
     default `dev` dependency group that `uv run` installs). A missing one must surface as an
     import error or a failing test, never a skip.
   - Platform skips unrelated to packaging stay. `test_tui_real_terminal.py` keeps its
     `hasattr(os, "forkpty")` skip.
2. **Pin that with an AST guard test**, `tests/test_no_textual_skips.py`. It scans all of
   `tests/` and fails on any skip construct for Textual or pyte, and on any `collect_ignore`
   in a conftest.
3. **One harness module, `tests/tui_harness.py`, owns the common TUI test machinery**,
   including the pyte renderer lifted out of `test_tui_real_terminal.py`. A second module,
   `tests/scale_clone.py`, holds the scale-clone helper, so non-TUI perf tests can import it
   without pulling in Textual.
4. **Snapshots are text, taken through a real pty and pyte at 160x48.** They are compared
   to committed `.txt` goldens. They are rewritten only under `NW_UPDATE_SNAPSHOTS=1`. A
   missing golden fails, it never auto-creates.
5. **The runtime budget is enforced by a pytest plugin**, `tests/tui_budget.py`, loaded from
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
`hasattr(os, "forkpty")` skip.

A second test runs `python -m pytest --collect-only -q tests/test_tui_*.py` in a subprocess
and asserts exit code 0 and that all of these files contributed at least one collected test.
That catches a TUI file that is silently un-collected by some other mechanism.

### 2. `tests/tui_harness.py`

Imports Textual and pyte unconditionally. All paths below resolve under pytest's `tmp_path`.

```
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "agentic_history"   # child 2's data-dir-shaped fixture
SIZE = (160, 48)                                                       # the epic's snapshot size

make_cfg(tmp_path, *, fixture=FIXTURE_ROOT, extra=None) -> cfg
async def running_desk(cfg, *, size=SIZE, **desk_kw)   # async context manager -> (app, pilot)
async def settle(app, pilot, n=3)
pilot_until                                            # re-export of waits.pilot_until
async def double_click_row(pilot, table, row, *, column=0)
async def click_row(pilot, table, row, *, column=0)
screen_text(app) -> list[str]
table_rows(table) -> list[list[str]]
tree_hash(root) -> str
render_pty(conf_dir, steps, *, cols=160, rows=48, ready_first=None) -> (lines, cells)
assert_screen_matches(name, lines, *, masks=DEFAULT_MASKS)
```

- **`make_cfg`**
  - Copies the fixture tree to `tmp_path/data`, then writes `tmp_path/config/config.yaml`
    (and `mappings.yaml`) pinning the data dir, the cache dir (`tmp_path/cache`), and empty
    org and capture files under `tmp_path/org`.
  - Returns the cfg the CLI's own loader produces from that config dir, so the desk sees
    exactly what `nw tui` would.
  - `extra` is a callable `extra(cfg)` run after the copy, so a test can add a live campaign
    using the existing `test_agentic_dashboard._campaign` helpers.
  - It fails with a clear message if the copy has no `campaigns/` directory (a wrong
    fixture layout).
  - It then asserts that every path in the cfg (data dir, cache dir, org files, ledger and
    mailbox roots) resolves under `tmp_path`. A test therefore can never touch the user's
    real data dir.
- **`running_desk`**
  - Builds `IdeaDesk(cfg, **desk_kw)` as `test_tui_queue_tab._desk` does today, with
    `app.notes` collecting `notify` calls.
  - Enters `app.run_test(size=size)`, waits for the first load with `settle`, and yields
    `(app, pilot)`.
  - On exit it waits for workers (bounded by `waits.WAIT_S`) so that no worker writes after
    teardown.
- **`settle`** is the current `test_tui_queue_tab.settle` moved here. It alternates
  `pilot.pause` with `app.workers.wait_for_complete`, bounded by `waits.WAIT_S`.
- **`double_click_row` / `click_row`**
  - Compute the on-screen offset of a `DataTable` row from its header height, scroll offset
    and row index, scroll the row into view first, and call `pilot.double_click(table,
    offset=...)` or `pilot.click`. Textual 8's `Pilot.double_click` is the only click
    primitive used, so the posted event carries `Click.chain == 2`.
  - They raise `AssertionError` if the row is out of range or not visible after scrolling.
- **`screen_text(app)`**
  - Returns the lines the active screen currently paints, as plain text.
  - It concatenates the segment text of the compositor's strips for the whole screen, and
    does not read widget-private state.
  - This uses a private Textual API, so it is the only place that touches it. A harness
    self-test pins it against a known widget text, so a Textual bump that breaks it fails
    loudly there.
  - `table_rows(table)` returns a `DataTable`'s cells as plain strings.
- **`tree_hash(root)`** returns a SHA-256 over relative path, size and content of every file
  under `root`, sorted. The epic's "data dir byte-identical" checks use it.
- **`render_pty`** is `test_tui_real_terminal._render` generalised and moved here.
  - Parameters: `cols`, `rows` (default 160x48), `steps` as before, and the first
    "has loaded" predicate (previously hard-wired to `IDEA-001`).
  - It keeps SPEC-0115's design: `pty.fork`, exec `python -m nightwright.cli tui` with
    `NW_CONFIG_DIR`, `NW_DATA_DIR`, `TERM`, `COLUMNS` and `LINES`, `TIOCSWINSZ`, `pyte`
    `Screen` and `ByteStream`, and `waits.WAIT_S`-bounded pumping. Fixed sleeps are never
    used.
  - `test_tui_real_terminal.py` is refactored to call it with its current 110x30 and its
    own `_loaded` predicate. Its assertions do not change.
- **`assert_screen_matches(name, lines, masks=DEFAULT_MASKS)`**
  - **Normalisation.** Right-strips each line, drops trailing blank lines, and applies
    `masks`, a list of `(regex, replacement)`. `DEFAULT_MASKS` covers the time-relative
    tokens the desk prints (elapsed and "ago" durations, clock times). Child 4 extends it
    for ticking timers. Closed-campaign data is already deterministic and needs no masks.
  - **Golden path.** `tests/snapshots/tui/<name>.txt`, one file per snapshot.
  - **Mode.** With `NW_UPDATE_SNAPSHOTS=1` it (re)writes the golden. Otherwise it compares,
    and a missing golden fails with "no snapshot <name>: regenerate with
    NW_UPDATE_SNAPSHOTS=1". On mismatch it fails with a unified diff.
  - **Size check.** Every golden must be at most 160 columns and 48 lines, so a wrong
    terminal size cannot be committed silently.

### 3. `tests/scale_clone.py`

```
clone_campaigns(src_data_dir, dst_data_dir, n, *, records_target=None, seed=0) -> CloneReport
```

- **Source.** Copies `src_data_dir`'s global files (`conductor/`, `escalations/`,
  `mailbox/`, `events.jsonl`, `runner.jsonl`, `reviews/`, `flaky-tests.json`, ...) once.
  `src_data_dir` is never modified.
- **Campaigns.**
  - Writes `n` campaigns by cycling over the source campaigns in sorted order.
  - Each clone gets a new id: the source id with its last dash-separated suffix replaced by
    a deterministic hex derived from `(seed, source cid, index)`. The result keeps the
    original id's shape (`IDEA-NNN-YYYYMMDD-xxxx`).
  - The first pass over the sources keeps the original ids, so the clone is a superset of
    the fixture.
  - Every occurrence of the old id inside the clone's `state.json`, `ledger.jsonl`, run
    files and gate files is rewritten. Run files are copied, never hard-linked, so a test
    can never write through to the committed fixture.
- **Global logs.**
  - Decisions, mailbox messages and events that name a source campaign are replicated per
    clone with the id rewritten, and with a fresh event or message id so ids stay unique.
  - With `records_target` set, extra per-campaign records are replicated until the
    ledger-finding, decision and mail rows together reach about that number. The epic's
    perf test uses 50 000.
- **Result.** A `CloneReport(campaigns, ledger_lines, decision_lines, mail_lines,
  finding_lines)`.
- **Determinism.** Same `(src, n, records_target, seed)` always gives a byte-identical tree.
  `tree_hash` is used to check this.
- **Speed.** A 500-campaign clone of the fixture builds in at most 30 s on the dev box
  (asserted with a generous bound, scaled by `NW_TUI_BUDGET_SCALE`).

### 4. Runtime budget: `tests/tui_budget.py`

A pytest plugin, listed in `tests/conftest.py` as `pytest_plugins = ["tui_budget"]`. Its
core is a pure function, `evaluate(durations, limits) -> list[Violation]`.

- **Scope.** Test items from files named `tests/test_tui_*.py`. Their duration is the sum of
  setup, call and teardown from `pytest_runtest_logreport`. Under xdist the controller
  receives every worker's reports, and the verdict is computed on the controller only (the
  plugin does nothing when `config.workerinput` exists).
- **Defaults** (summed test durations, not wall time):
  - per test: 30 s
  - per file: 180 s
  - all TUI files together: 900 s

  They are about 2x the current worst case (78 s for `test_tui_queue_tab.py`), so they
  leave room for the new e2e pilots but catch a runaway.
- **Scaling.** All three are multiplied by `NW_TUI_BUDGET_SCALE` (float, default 1). The
  individual limits can also be overridden with `NW_TUI_TEST_BUDGET_S`,
  `NW_TUI_FILE_BUDGET_S` and `NW_TUI_TOTAL_BUDGET_S`.
- **Partial runs.** A partial run (affected-tests, a single file) is judged only on the
  items that ran. The total is checked only against items that ran.
- **Outcome.** On a violation it prints a table of offenders (worst first) in the terminal
  summary and sets the session exit status to `TESTS_FAILED`. Otherwise it prints a single
  line with the slowest TUI file.

### 5. Existing-test touch points

- `test_tui_queue_tab.py` imports `settle` and `_desk`'s equivalent from the harness instead
  of defining them. No assertion changes. This is the only existing test body edited beyond
  guard removal.
- `test_tui_real_terminal.py` switches to `render_pty` (see above).

### 6. Harness self-tests: `tests/test_tui_harness.py`

These use the real `IdeaDesk` over a copy of the real fixture. See the Test plan for the
list. Fixture-backed tests fail, never skip, if `tests/fixtures/agentic_history/` is
missing.

## Alternatives considered

- **Keep the skips but make them loud (a `warnings.warn` or a count in the summary).**
  Lost: a warning is still a green run with no TUI test. The point is that a missing
  dependency must break the run.
- **A `pytest-textual-snapshot` SVG golden.** Lost: new dependency, and the SVGs change on
  every Textual minor bump. The repo already has pyte as its real-terminal oracle
  (SPEC-0115). This is the epic's decision.
- **Snapshot Textual's headless `run_test` output instead of a pty.** Lost: it does not show
  what a real terminal composes from Textual's incremental repaints, which is the failure
  class SPEC-0115 exists to catch.
- **Hard-link run files in the scale clone.** Lost: a test writing through a link would
  corrupt the committed fixture. Copying costs seconds and is safe.
- **Time budget as a wall-clock timer around the whole TUI set.** Lost: unreliable under
  xdist and the host-wide gate-slot pool. Summed test durations are stable and attributable
  to a file.
- **Make the guard a lint rule or a pre-commit hook.** Lost: `uv run pytest` is the one gate
  every campaign already runs, and the repo already pins rules with tests (for example
  `tests/test_test_waits.py`).

## Acceptance criteria

- [ ] **AC1.** `tests/conftest.py` defines no `collect_ignore` and does not import
  `importlib.util` for it. `grep -rnE
  'importorskip\("(textual|pyte)|needs_textual|textual_available\(\)'` over `tests/`
  matches nothing except the allowed `monkeypatch.setattr(T, "textual_available", ...)`
  lines in `test_tui_entry.py` and the guard test's own synthetic sources.
- [ ] **AC2.** `tests/test_no_textual_skips.py` fails with `path:line` messages on synthetic
  sources for each violation kind, and passes on the current tree: `importorskip`,
  `skipif`, `skip`, an `if` gate, `needs_textual`, and `collect_ignore`. It accepts the
  `monkeypatch.setattr(T, "textual_available", ...)` and `hasattr(os, "forkpty")` forms.
- [ ] **AC3.** Collecting `tests/test_tui_*.py` exits 0, collects tests from every one of
  those files, and no test of those files is reported skipped for Textual or pyte.
- [ ] **AC4.** `tests/tui_harness.py` exports `make_cfg`, `running_desk`, `settle`,
  `pilot_until`, `double_click_row`, `click_row`, `screen_text`, `table_rows`, `tree_hash`,
  `render_pty` and `assert_screen_matches`, with the behaviour in *Design*. It imports
  Textual and pyte unconditionally.
- [ ] **AC5.** `make_cfg` copies the fixture to `tmp_path` and returns a cfg whose data dir,
  cache dir, config dir and org files all resolve under `tmp_path`. After a desk session
  the committed fixture's `tree_hash` is unchanged.
- [ ] **AC6.** `running_desk` starts the real `IdeaDesk` at 160x48 through `run_test`.
  `double_click_row` delivers a click event with `chain == 2` and opens the same screen that
  Enter on that row opens. `screen_text` returns the text a user sees on the active screen,
  including text of a pushed screen.
- [ ] **AC7.** `render_pty(..., cols=160, rows=48)` returns a 48-line composited screen of
  the real `nw tui` over a fixture-backed config. `test_tui_real_terminal.py` uses it at its
  original size and its original assertions still pass.
- [ ] **AC8.** `assert_screen_matches` compares against `tests/snapshots/tui/<name>.txt`. It
  rewrites the file only when `NW_UPDATE_SNAPSHOTS=1` is set, fails on a missing golden
  without creating it, shows a diff on mismatch, and rejects a golden wider than 160
  columns or longer than 48 lines. At least two committed goldens exist (the Queue tab over
  the fixture, and the ideas tab over a throwaway org), and each renders identically twice
  in a row.
- [ ] **AC9.** `clone_campaigns(src, dst, n)` produces `n` campaigns with unique,
  well-formed ids and no residual old id inside a clone's `state.json` or `ledger.jsonl`.
  The source tree is unchanged (`tree_hash` before equals after). Two calls with the same
  arguments give identical trees. With `records_target=50_000` the record count lands within
  10 % of the target. A 500-campaign clone finishes within the 30 s bound.
- [ ] **AC10.** The budget plugin's `evaluate` flags per-test, per-file and total
  violations. A subprocess run of a tiny `test_tui_x.py` that sleeps, with a tiny
  `NW_TUI_TEST_BUDGET_S`, exits non-zero and names the offender. With default limits
  the repo's own TUI files pass, and the measured per-file times are recorded in this
  spec's *As shipped*.
- [ ] **AC11.** `test_tui_queue_tab.py` takes `settle` and the desk factory from the
  harness, with no changed assertions, and its 38 tests still pass.
- [ ] **AC12.** `python3 tools/spec_lint.py` exits 0, and the affected tests pass
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
- `tests/test_tui_harness.py` (real `IdeaDesk`, copy of the real fixture):
  - `test_make_cfg_is_isolated`: every cfg path resolves under `tmp_path`, and the source
    fixture is untouched after a session (AC5).
  - `test_running_desk_shows_fixture_queue`: starts the desk at 160x48, presses `3`,
    `pilot_until` the queue table is populated, and asserts `screen_text` contains a
    fixture campaign's id (AC4, AC6).
  - `test_double_click_matches_enter`: `make_cfg(extra=...)` adds a live campaign with the
    `test_agentic_dashboard._campaign` helper. `double_click_row` on its queue row pushes
    `CampaignScreen` for that campaign. Enter on a second row pushes it for the second
    campaign. A message spy confirms `chain == 2` for the double click (AC6).
  - `test_screen_text_reads_pushed_screens`: after the screen is pushed, `screen_text`
    contains the screen's header and not the underlying tab's rows (AC6).
  - `test_render_pty_160x48`: `render_pty` over a fixture-backed config returns exactly 48
    lines of at most 160 columns containing the tab bar (AC7).
  - `test_snapshot_queue_tab` and `test_snapshot_ideas_tab`: `assert_screen_matches` against
    the committed goldens. The first uses the fixture config, the second a throwaway org
    moved from `test_tui_real_terminal._throwaway_org` (AC8).
  - `test_snapshot_is_deterministic`: renders the same view twice through `render_pty`;
    the normalised lines are equal (AC8).
  - `test_snapshot_update_protocol`: with `monkeypatch` on `NW_UPDATE_SNAPSHOTS` and the
    golden dir redirected to `tmp_path`: missing golden fails and creates nothing; `=1`
    writes it; the next run without it passes; a changed screen fails with a diff; an
    oversize golden is rejected (AC8).
- `tests/test_scale_clone.py` (no Textual): clone a small throwaway data dir and the
  committed fixture; assert id shape and uniqueness, no residual old id, source `tree_hash`
  unchanged, determinism across two calls, record count within 10 % of `records_target`,
  and the 500-campaign build time bound (AC9).
- `tests/test_tui_budget.py`: unit tests of `evaluate`; the subprocess test with a tiny
  budget and a sleeping `test_tui_x.py` (AC10).
- `tests/test_tui_real_terminal.py`: existing tests, now on `render_pty` (AC7).
- `tests/test_tui_queue_tab.py`: existing tests, now on the shared helpers (AC11).

**Manual verification**

1. `uv run pytest tests/test_tui_harness.py tests/test_no_textual_skips.py -q` is green.
   Output shows no `s` (skipped) markers and no import errors.
2. Edit one golden by hand, run `uv run pytest tests/test_tui_harness.py -q -k snapshot`
   and confirm a diff failure. Then run it once with `NW_UPDATE_SNAPSHOTS=1`, run it again
   without the variable, and confirm it passes. Confirm that `git diff` on the golden shows
   only the expected text.
3. Prove the guard bites: temporarily add `pytest.importorskip("textual")` to any TUI test
   file and confirm `test_no_textual_skips.py` fails with that file and line. Revert it.
4. Prove that nothing is skipped when Textual is unavailable: run `uv run pytest
   tests/test_tui_queue_tab.py -q` in an environment where `textual` is made unimportable
   (for example a `sitecustomize` that blocks the import, in a throwaway directory). Confirm
   the result is a collection error, not a green run with the file ignored.
5. Record the per-file TUI timings from the budget plugin's summary line in *As shipped*.

**Regression guard**

- The existing `test_tui_*`, `test_idea_tree`, `test_desk_table`, `test_idea_project` and
  `test_clock_json_parity` suites run un-gated and stay green. Several previously
  ignored or skipped files now run in every environment, which is the intended change.
- `test_tui_real_terminal.py`'s assertions are unchanged, so they check that the harness
  refactor did not alter SPEC-0115's behaviour.
- `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Land after epic child 2 (history layer and fixture). Until the fixture exists, the
   fixture-backed tests in `test_tui_harness.py` cannot pass. On promotion `depends_on:
   [<child 2's spec id>]` is added to this spec's front matter.
2. If child 2 lays the fixture out differently from `tests/fixtures/agentic_history/` as a
   data-dir root, adjust only `FIXTURE_ROOT` and the first assertion in `make_cfg`.
3. Remove the guards and add the guard test in one change, so the tree never has a window
   in which a new skip can enter.
4. Add the harness, `scale_clone` and budget plugin. Refactor `test_tui_real_terminal.py`
   and `test_tui_queue_tab.py` onto them.
5. Generate the goldens once with `NW_UPDATE_SNAPSHOTS=1`, review them by eye, and commit.
6. Children 4 to 7 and the epic's `test_tui_history_e2e.py` and `test_history_perf.py` import
   from `tui_harness` and `scale_clone`. No data migration. The only behaviour change is
   that files previously ignored for a missing Textual now always run.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (affected tests via `python3
  tools/affected_tests.py --run uv run pytest -q`, plus the files touched); manual steps
  performed and noted here.
- [ ] No regressions at the queue's close gate.
- [ ] Spec body updated to match what shipped (including the measured TUI file timings).
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
  `python3 tools/spec_lint.py` exits 0.

## Open questions

None. Decided while writing:

- **Why is pyte covered by the guard, and not just Textual?** The snapshot path needs it,
  it is in the default `dev` group that `uv run` installs, and a skipped real-terminal test
  is the same silent hazard.
- **Why test durations and not wall time?** See *Alternatives considered*.
- **Why 30 s / 180 s / 900 s?** They are about 2x today's worst case (78 s per file).
  `NW_TUI_BUDGET_SCALE` is the tuning knob for slow hosts. The numbers are retuned from the
  evidence recorded in *As shipped* if real runs show they are too tight or too loose.
