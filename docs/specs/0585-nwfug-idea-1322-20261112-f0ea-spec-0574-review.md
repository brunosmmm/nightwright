---
id: SPEC-0585
title: "SPEC-0574 review follow-ups: pin the sentinel, dangling-link, spec-reviews guard and sanitizer-boundary behaviour; reap parallel children; a real decoy-safety check"
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
kind: feature
source_idea: IDEA-1333
tags: [history-fixture, sanitization, tests]
depends_on: [SPEC-0574]
---

## Context

[SPEC-0574](./0574-nwfug-idea-1010-20261112-1dd9-spec-0568-review.md) is `done`: it hardened
`tools/capture_history_fixture.py` (name guards, containment of review and sentinel reads, the
common-word account rule) and `tests/test_history_fixture.py`. Its review (campaign
`IDEA-1322-20261112-f0ea`) left seven minor findings, filed as IDEA-1333. Six are test-strength
gaps in the same module; one is a conformance note. None is a product bug in the tool as it stands,
so this spec changes **tests only** (plus this spec). SPEC-0574 is past `draft` and is not edited.

| # | Finding | Where | Disposition |
|---|---|---|---|
| 1 | Dangling, looping and unreadable sentinel links, and the `_inside` strict-resolve change, have thin coverage; the sentinel call site's `except (OSError, RuntimeError)` branch has no test; "a dangling link in `reviews/` is now noted as skipped" is unpinned (AC3) | `tests/test_history_fixture.py` | Taken: Decision 1 |
| 2 | The parallel `--check` test leaves the remaining `Popen` children running when an early assertion fails | `test_check_is_independent_of_the_running_user` | Taken: Decision 2 |
| 3 | The `/abs` decoy-safety assertion (`assert not Path(where).exists()`) can fail spuriously if the path pre-exists on the box and proves nothing about the tool (AC6) | `test_unsafe_spec_id_and_project_are_skipped` | Taken: Decision 3 |
| 4 | The full capture with exit 0 runs only for `spec_id=["a"]`; AC2 promises it for a list, dict, `5` and `""` in either field | `test_non_string_ids_are_skipped_not_fatal` | Taken: Decision 4 |
| 5 | The `isinstance` guard on `spec-reviews.jsonl` rows in `collect_records` is untested | `tools/capture_history_fixture.py:645` | Taken: Decision 5 |
| 6 | Two edits go beyond SPEC-0574's stated Design (the `spec-reviews.jsonl` guard and the strict resolve in `_inside`) (AC9) | `tools/capture_history_fixture.py` | Ratified, not reverted: Decision 6 |
| 7 | Sanitizer boundary and counting behaviour of the new common-word rules is not pinned (the word boundary before `USER`, `xrunner@`, case, trailing boundary, per-rule counts) (AC4) | `tests/test_history_fixture.py` | Taken: Decision 7 |

Behaviour of the tool that the new tests pin (read from `tools/capture_history_fixture.py` on this
branch, not assumed):

- `_inside(path, root)` resolves with `strict=os.path.lexists(path)`: a path that exists or is a
  link (a dangling link included, as `lexists` is true for it) resolves strictly, so a dangling link
  raises `FileNotFoundError` (an `OSError`) and a loop raises on every Python version that
  supports strict mode; an `OSError` or `RuntimeError` is "outside". A plain missing path resolves
  non-strictly and is inside if it lies under `root`.
- The sentinel site reads `sentinel/<proj>.last.json` only if `f.resolve() == sd.resolve() /
  "<proj>.last.json"`; an `OSError` / `RuntimeError` from `resolve()` counts as "not itself"; the
  skip note reads `sentinel/<proj>.last.json does not resolve to itself: skipped`. A **self-loop**
  there is version-dependent: Python 3.13+'s non-strict `resolve()` returns the loop path
  unchanged (equal to the expected path), and `is_file()` is then false, so nothing is read and no
  note is emitted; older Pythons raise `RuntimeError` and emit the note. The tests therefore assert
  "not read, no crash" for a loop and assert the note only where it is version-independent.
- The common-word `Sanitizer` rules for a name in `COMMON_WORD_USERS` are: `/name(?![\w.-])` ->
  `/user`; `(?<![\w.-])name(?=@)` -> `user`; `\b(USER|LOGNAME|USERNAME)=name(?![\w.-])` ->
  `\1=user`, all case-sensitive; every substitution adds one to `counts["user_name"]`.

## Goals / Non-goals

**Goals**
- Every behaviour named in findings 1, 4, 5 and 7 has a test that fails when that behaviour is
  reverted (shown by the manual mutation step).
- The parallel `--check` test never leaves a child process behind, whichever assertion fails.
- The `/abs` and `{tmp}` decoy cases of `test_unsafe_spec_id_and_project_are_skipped` prove "the tool
  never reads that path" without asserting anything about what exists on the box.
- AC2 of SPEC-0574 (exit 0 on a source with a list, dict, `5` or `""` for `spec_id` or `project`)
  is shown for all eight (field, value) combinations.

**Non-goals**
- Any change to `tools/capture_history_fixture.py`, the committed fixture, `MANIFEST.json`, or any
  file other than `tests/test_history_fixture.py` and this spec. In particular the silent
  3.13+ sentinel self-loop (no note) is documented and pinned as is, not changed.
- Guarding `c.state.get("state") in CANDIDATE_STATES` (SPEC-0574's *As shipped* lists it as a
  separate follow-up).
- Redacting more account-name contexts, or changing the budget (20 s) or the 4 000-string corpora.
- A new test module: all tests extend `tests/test_history_fixture.py`.

## Decision

1. **Link-shape coverage for the sentinel and `_inside`.** New tests exercise, for both
   `reviews/<spec_id>/` files and `sentinel/<proj>.last.json`: a dangling link (target missing), a
   self-loop, and an `OSError` / `RuntimeError` raised by `resolve()` (simulated with a
   `monkeypatch` wrapper on `pathlib.Path.resolve` that raises for the one path under test and
   delegates for every other call, because a real unreadable link is not portable across Pythons
   or when the suite runs as root). `_inside` gets direct unit tests: dangling link -> `False`;
   link to a file inside the root -> `True`; link to a file outside -> `False`; loop -> `False`;
   `resolve()` raising `PermissionError` / `RuntimeError` -> `False`; plain missing path under the
   root -> `True`. **Where a dangling link points matters.** A non-strict `resolve()` of a dangling
   link returns its missing target, so a link whose target lies *outside* the root is "outside"
   under strict and non-strict resolve alike and cannot tell them apart. The dangling link in the
   `_inside` unit tests (`root/dangling.json`) and in the reviews test
   (`reviews/<MULTI[:9]>/dangling.json`) therefore targets a **missing file inside** the root /
   directory (`root/missing.json`, `missing.json` beside it): only strict resolve makes it
   "outside", so reverting `_inside` to `path.resolve()` makes those tests fail on every Python
   version. (A dangling link to a missing file outside the root is added as a second, non-killing
   case.) The loop case additionally kills that revert only on Python 3.13+; on older Pythons a
   non-strict loop raises `RuntimeError` and still counts as outside.
2. **Children are always reaped.** A helper `reap_all(procs)` kills every `Popen` that has not
   exited (`poll() is None`) and waits for it; `test_check_is_independent_of_the_running_user`
   collects the four results inside `try: ... finally: reap_all(procs)`, so a failing return-code
   assertion for the first child cannot leave the other three running. A test shows the helper
   kills and waits for a long-running child.
3. **The decoy-safety check becomes a read spy.** The `elif bad == "/abs": assert not
   Path(where).exists()` branch is removed. In every case the test records the paths the tool
   opens during `collect_records` (a spy on `Path.read_bytes`, `Path.read_text`, `Path.open` and
   `builtins.open`, recording `os.path.normpath(os.fspath(path))`, delegating to the original) and
   asserts that the tool opened **no planted decoy file and nothing under an unplanted decoy
   path**. The check is narrowed to the decoy itself because for `spec_id` `..` and `""` the
   normalised `where` is `agentic/` and `agentic/reviews`, directories the tool legitimately reads
   other files from (`reviews/<MULTI[:9]>/r.json`, `spec-reviews.jsonl`, `sentinel/...`). The test
   records the exact decoy files it planted (`where/d.json` for `spec_id`, the `where` file for
   `project`); a recorded read must not equal any of them. For `/abs`, which is never planted
   (`where` is outside `tmp_path`), no recorded read may equal `where` or lie under it. The decoy
   is still planted only when `where` is inside `tmp_path`, so no test writes outside its tmp dir,
   and the result no longer depends on what exists outside it. The existing "`DECOY-TEXT` is in no
   collected file's content" assertion stays.
4. **AC2's full capture runs for every (field, value).** `test_non_string_ids_are_skipped_not_fatal`
   runs `run_tool("--src", ..., "--out", ...)` (the in-process `main`, cheap) for all eight
   combinations and asserts exit 0, empty stderr traceback (no `Traceback` text), and the valid
   sibling campaign's `agentic/reviews/<MULTI[:9]>/r.json` in the output. The one real-subprocess
   run (`run_tool_cli`) stays for `spec_id=["a"]`, so the CLI contract is still exercised.
5. **The `spec-reviews.jsonl` guard is pinned.** A new test rewrites the source's
   `spec-reviews.jsonl` with rows whose `spec_id` is a list, a dict, `5`, `None`, missing, and a
   non-dict row (`[1]`, a bare string), next to a valid row for the selected spec and a row for an
   unselected one; `collect_records` must not raise and the kept `spec-reviews.jsonl` contains
   exactly the valid selected row.
6. **The two edits beyond SPEC-0574's Design are ratified.** The `isinstance(..., str)` guard on
   `spec-reviews.jsonl` rows and the un-resolved-root / strict-resolve `_inside` are the governing
   behaviour from here on (this spec's Context and Decisions 1 and 5 state them; SPEC-0574's *As
   shipped* already records why). No tool change; they are covered by Decisions 1 and 5 so a
   revert fails a test.
7. **The common-word rules' boundaries and counting are pinned.** `COMMON_WORD_ROWS` gains the
   boundary and case rows listed in the Test plan, and a new test pins `counts["user_name"]`
   (and that the other rule counters stay zero) for representative strings.

## Design

All changes are in `tests/test_history_fixture.py`, which already carries its one module-level
marker `pytestmark = pytest.mark.integration`. No new module, so no second tier marker.

- **Helpers.** `reap_all(procs)` (Decision 2). `record_reads(monkeypatch)` returns a list that the
  patched `Path.read_bytes` / `read_text` / `open` and `builtins.open` append `os.fspath(path)` to
  (Decision 3). `resolve_raises(monkeypatch, exc, name_suffix)` patches `Path.resolve` to raise
  `exc` when `self.name.endswith(name_suffix)` and delegate otherwise (Decision 1); use `_collect`
  (existing) to run `collect_records`.
- **Sentinel tests** (new, next to `test_sentinel_symlink_escaping_source_is_not_read`): the
  source's `sentinel/Meta-Tools.last.json` replaced by (a) a dangling link, (b) a self-loop, (c) a
  regular file while `resolve` is patched to raise `PermissionError`, (d) the same with
  `RuntimeError`. Each: no exception, the file is not in `files`, and for (a), (c), (d) a note
  containing `Meta-Tools.last.json` and `skipped`; for (b) only the "not read, no crash" half.
  A sibling regular `sentinel/Other.last.json` need not be selected; the test checks the other
  selected project's output is unaffected where the source has one.
- **Reviews tests**: a dangling `reviews/<MULTI[:9]>/dangling.json -> missing.json` link (the target
  is a missing file in the same directory, i.e. inside the root; Decision 1) is noted (`dangling.json`
  and `resolves outside`) and `r.json` is still captured; the `resolve`-raising variants for a
  reviews file give the same note.
- **`_inside` unit tests** replace nothing: they sit beside `test_inside_treats_a_symlink_loop_as_outside`.
  The dangling case builds `root/dangling.json -> root/missing.json` (missing, in-root; the case that
  separates strict from non-strict resolve) and, separately, `root/dangling_out.json ->
  tmp/outside_missing.json` (missing, outside; `False` under both, kept as a plain regression).
- **`test_unsafe_spec_id_and_project_are_skipped`**: the existing parameters, the existing
  in-tmp decoy plant (now also recording the planted decoy file paths: `where/d.json` or `where`,
  normalised), the spy of Decision 3, and removal of the `/abs` existence assertion. The spy
  assertion is `not any(r in decoy_files for r in reads)` when a decoy was planted, and `not
  any(r == where or r.startswith(where + os.sep) for r in reads)` when `where` is outside `tmp_path`
  (`/abs`); it never compares against `where` as a directory prefix for a planted case.
- **spec-reviews test**: builds the rows with `json.dumps` per line, overwriting
  `src/agentic/spec-reviews.jsonl` (written by `build_source`, line 209).
- **Common-word rows** appended to `COMMON_WORD_ROWS` (all `Sanitizer(users=...).text(in) == out`):

  | users | input | expected output |
  |---|---|---|
  | `{"runner"}` | `export USER=runner` | `export USER=user` |
  | `{"runner"}` | `XUSER=runner` | `XUSER=runner` |
  | `{"runner"}` | `MY_USER=runner` | `MY_USER=runner` |
  | `{"runner"}` | `User=runner` | `User=runner` |
  | `{"runner"}` | `user=runner` | `user=runner` |
  | `{"runner"}` | `xrunner@localhost` | `xrunner@localhost` |
  | `{"runner"}` | `.runner@localhost` | `.runner@localhost` |
  | `{"runner"}` | `-runner@localhost` | `-runner@localhost` |
  | `{"runner"}` | `Runner@localhost` | `Runner@localhost` |
  | `{"runner"}` | `RUNNER@localhost` | `RUNNER@localhost` |
  | `{"runner"}` | `USER=runnerx` | `USER=runnerx` |
  | `{"runner"}` | `USER=runner-x` | `USER=runner-x` |
  | `{"runner"}` | `USER=runner_x` | `USER=runner_x` |
  | `{"runner"}` | `USER=runner,` | `USER=user,` |
  | `{"runner"}` | `/runner/a runner@x` | `/user/a user@x` |

- **Counting test** `test_common_word_user_counts_are_per_substitution`, rows
  `(users, text, expected user_name count)`: `({"runner"}, "runner@localhost and USER=runner", 2)`,
  `({"runner"}, "/runner/a runner@x USER=runner LOGNAME=runner", 4)`,
  `({"runner"}, "the runner log XUSER=runner", 0)`,
  `({"runner", "alice"}, "alice@x runner@x /runner/a alice runner", 4)`. Each asserts
  `san.counts["user_name"] == n` and that `home_path`, `email`, `secret`, `host`, `ipv4`,
  `pr_url_host` and `env` are all 0 for that string. One more row,
  `({"runner"}, "/home/runner/x", ...)`, asserts `home_path == 1` and `user_name == 0` (the home
  path rule wins first; nothing is double counted).

## Alternatives considered

- **Make the sentinel site strict so a loop always notes** — a tool change for a cosmetic note
  (nothing is read either way); the risk of the silent case is nil, so the behaviour is pinned
  and documented instead.
- **Create a real unreadable link with `chmod 000`** — does not fail when the suite runs as root
  and resolves differently across Python versions; a `resolve()` wrapper is deterministic.
- **Keep an existence assertion for `/abs` but skip it if the path exists** — still asserts a
  fact about the box; the read spy asserts a fact about the tool.
- **Run every (field, value) capture as a real subprocess** — eight launches on a module with a
  tight time budget (SPEC-0574 Decision 5); `main` in-process is the same code, and one real
  subprocess still covers the CLI contract.
- **Revert the two out-of-Design edits to match SPEC-0574's Design** — the strict resolve fixes
  real symlink-loop failures on Python 3.13+ and the guard prevents a `TypeError`; reverting
  would reintroduce both.

## Acceptance criteria

- [x] **AC1.** `_inside` returns `False` for a dangling link whose missing target is **inside** the
  root (`root/dangling.json -> root/missing.json`) and for one whose missing target is outside, a
  loop, a link to a file outside the root, and when `Path.resolve` raises `PermissionError` or
  `RuntimeError`; `True` for a link to a file inside the root and for a plain missing path under it.
- [x] **AC2.** A dangling link in `reviews/<spec_id>/` whose target is a missing file inside that
  directory (`dangling.json -> missing.json`) is skipped with a note naming it and
  `resolves outside`, and the directory's regular files are still captured.
- [x] **AC3.** A dangling sentinel link is skipped with the `does not resolve to itself: skipped`
  note; a self-looping sentinel link is not read and does not crash the capture; with `resolve`
  raising `PermissionError` or `RuntimeError` for the sentinel file the capture does not crash,
  reads nothing from it and notes the skip (so the `except (OSError, RuntimeError)` branch
  executes in the suite).
- [x] **AC4.** `reap_all` kills and waits for a still-running child (its return code is negative
  after the call), and `test_check_is_independent_of_the_running_user` calls it in a `finally`, so
  no child of that test survives it when an assertion fails.
- [x] **AC5.** `test_unsafe_spec_id_and_project_are_skipped` has no assertion about the existence
  of any path outside `tmp_path`; for every parametrised (`field`, `bad`) combination, including `spec_id`
  `..` and `""`, it asserts the tool opened no planted decoy file (`where/d.json` for `spec_id`, the
  `where` file for `project`) and, for the unplanted `/abs`, nothing equal to or under `where`;
  reads of other files under the same directories (e.g. `reviews/<MULTI[:9]>/r.json`) are allowed;
  it creates nothing outside `tmp_path`; it passes when the suite runs as root and when `/abs`
  exists.
- [x] **AC6.** For every combination of `field` in (`spec_id`, `project`) and `bad` in
  (`["a"]`, `{"a": 1}`, `5`, `""`), a full `run_tool` capture exits 0, prints no `Traceback`, and
  the output contains the valid sibling campaign's review file.
- [x] **AC7.** With `spec-reviews.jsonl` rows whose `spec_id` is a list, a dict, `5`, `None`,
  absent, plus non-dict rows, `collect_records` does not raise and the kept file holds only the
  valid row for the selected spec.
- [x] **AC8.** Every row of the Design's common-word table holds exactly, and the counting test
  holds: `counts["user_name"]` equals the number of substitutions and every other rule counter is 0
  for those strings (`/home/runner/x` gives `home_path == 1`, `user_name == 0`).
- [x] **AC9.** `tools/capture_history_fixture.py`, the committed fixture and `MANIFEST.json` are
  byte-identical to `main`; `git diff main --stat` lists only `tests/test_history_fixture.py`, this
  spec and the generated `docs/LEDGER.md`; `python3 tools/capture_history_fixture.py --check`,
  `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py` exit 0; the module
  passes under its 20 s budget guard.
- [x] **AC10.** Mutation check (manual step 2): reverting the `isinstance` guard on
  `spec-reviews.jsonl`, the strict resolve in `_inside`, the `\b` before `USER`, or the
  `(?<![\w.-])` before `name@` each makes at least one new test fail, and the tool is restored.
  For the strict-resolve revert the failing test is the in-root dangling case (in
  `test_inside_link_shapes` and `test_review_dangling_link_is_noted_and_skipped`), which fails on
  any Python; the record names the Python version the check ran on.

## Test plan

**Automated tests** (all in `tests/test_history_fixture.py`, which already carries its single
module-level marker `pytestmark = pytest.mark.integration`; no new test module is introduced, so
no second tier marker is added; `python3 tools/test_tiers.py --check` must exit 0 when run):

- `test_inside_link_shapes` (AC1): parametrised dangling-in-root / dangling-outside / loop / outside link / inside link /
  missing plain path / `resolve` raising `PermissionError` / `RuntimeError`.
- `test_review_dangling_link_is_noted_and_skipped` (AC2), including the `resolve`-raising
  variants for a reviews file.
- `test_sentinel_dangling_loop_and_unreadable_links` (AC3): the four shapes of Design; the
  self-loop case asserts only absence and no crash (version-dependent note).
- `test_reap_all_kills_running_children` and the edited
  `test_check_is_independent_of_the_running_user` (AC4): the first starts
  `python -c "import time; time.sleep(60)"`, calls `reap_all`, asserts `returncode < 0`; the second
  wraps its collection in `try/finally`.
- Edited `test_unsafe_spec_id_and_project_are_skipped` (AC5) with the read spy against the planted
  decoy files (and `where` plus children for the unplanted `/abs`).
- Edited `test_non_string_ids_are_skipped_not_fatal` (AC6): full in-process capture for all eight
  combinations; the real subprocess for `spec_id=["a"]` only.
- `test_spec_reviews_rows_with_non_string_ids_are_skipped` (AC7).
- Extended `COMMON_WORD_ROWS` / `test_common_word_user_edge_cases` and the new
  `test_common_word_user_counts_are_per_substitution` (AC8).
- Existing `test_committed_fixture_*` and `test_check_is_independent_of_the_running_user`
  (AC9, regression).

**Manual verification.**
1. `uv run pytest -q --durations=0 tests/test_history_fixture.py` (explicitly; the gate skips
   `integration`): passes; record pass count, the summed per-test time and the summed time of the
   new tests in this spec (the module budget stays 20 s). Run it once as a non-root user; if a
   root shell is available, once as root too (AC5).
2. Mutation check (AC10), one at a time, each followed by `git checkout --
   tools/capture_history_fixture.py`: (a) drop `isinstance(o.get("spec_id"), str) and` from the
   `spec-reviews.jsonl` filter; (b) change `_inside` to `path.resolve()` (the in-root dangling cases must fail, on the Python in use: record `python3 --version`); (c) drop the `\b` in the
   `USER|LOGNAME|USERNAME` rule; (d) drop `(?<![\w.-])` in the `name@` rule. The named new tests fail
   each time; record which, and the Python version for (b). Nothing from this step is committed.
3. `python3 tools/capture_history_fixture.py --check`, `python3 tools/test_tiers.py --check` and
   `python3 tools/spec_lint.py` exit 0; `git diff main --stat` lists only the AC9 files.

**Regression guard.** `python3 tools/affected_tests.py --run uv run pytest -q`, plus
`uv run pytest -q tests/test_history_fixture.py`, `python3 tools/spec_lint.py` and
`python3 tools/spec_lint.py --write-ledger`. The full suite is not run by the implementer.

### Test record (2026-11-12)

Python 3.14.7, non-root (no root shell available, so the AC5 "as root" run was not done; the spy
no longer asserts anything about what exists on the box, so it does not depend on the uid).
The box was heavily loaded (load average 10-14), so absolute times are inflated.

- `NW_HISTORY_FIXTURE_BUDGET_S=300 uv run pytest -q tests/test_history_fixture.py`: 187 passed
  (151 before this spec; 36 new test cases). The module's summed test time was 33-41 s under that
  load; the **unchanged** `HEAD` module measured 45.7 s in the same conditions, so the 20 s guard
  fails on this box before and after (a load effect, not this change). The new/edited tests
  (new tests plus the edited `unsafe_spec_id`, `non_string_ids` and parallel `--check` tests) sum to
  about 4.1 s under that load.
- Mutation check (AC10), each followed by `git checkout -- tools/capture_history_fixture.py`
  (tool restored, `git status` shows it unmodified), Python 3.14.7:
  (a) dropping `isinstance(o.get("spec_id"), str) and` fails `test_spec_reviews_rows_with_non_string_ids_are_skipped`;
  (b) `_inside` -> `path.resolve()` fails `test_inside_link_shapes[dangling_in_root]`,
  `test_review_dangling_link_is_noted_and_skipped` (in-root dangling cases) and
  `test_inside_link_shapes[loop]` / `test_inside_treats_a_symlink_loop_as_outside` (3.13+ loop);
  (c) dropping `\b` before `USER` fails `test_common_word_user_edge_cases[XUSER=runner]`,
  `[MY_USER=runner]` and `test_common_word_user_counts_are_per_substitution[...XUSER=runner...]`;
  (d) dropping `(?<![\w.-])` before `name@` fails the `xrunner@`, `.runner@` and `-runner@` rows.
- `python3 tools/capture_history_fixture.py --check`: ok (441 files). `python3 tools/test_tiers.py --check`: exit 0.
- The full suite was left to gating.

### As shipped (2026-11-12)

Tests only, in `tests/test_history_fixture.py`; no tool change. Deviations from the Design text:
the reviews `resolve`-raising variants use a regular `unreadable.json` file (resolve raising for
that name); `COUNT_ROWS` holds the counting rows and the `/home/runner/x` row is its own test
(`test_common_word_user_home_path_is_not_double_counted`); the read spy also asserts that the
tool's own read of `reviews/<MULTI[:9]>/r.json` was recorded, so a broken spy cannot pass vacuously.

AC9 was left unticked in the first loop: everything in it holds (tool, fixture and manifest untouched, `--check`,
`test_tiers --check` and `spec_lint` exit 0, only the test module, this spec and the ledger changed)
except the 20 s module budget, which could not be shown on this box (load average 14 from other
campaigns): the guard errors at 33-44 s here, and the unchanged `HEAD` module errors at 45.7 s. The new
tests add about 4 s under that load. The affected-tests run (`tools/affected_tests.py --run`) was
started but did not finish on the saturated box.

### Test record, loop 1 (2026-11-12)

`uv run pytest -q --durations=0 tests/test_history_fixture.py` with the default 20 s budget, once the
box load dropped (load average 6.7, 8 cores): 187 passed, no budget error, 11.4 s wall, 10.4 s summed
per-test time, of which the new and edited tests of this spec (`inside_*`, `review_dangling`,
`review_file_whose`, `sentinel_dangling`, `reap_all`, `spec_reviews_rows`, the counting tests, `unsafe_spec`,
`non_string`) account for about 1.3 s. The earlier 33-45 s readings were a load effect (load average 10-14),
as the first run's note says. The pytest-affected gate (which excludes `integration`) passed 1541 tests;
the full suite was left to gating. `python3 tools/spec_lint.py` exits 0 with the ledger regenerated.

## Rollout / migration

No data migration and no tool change. The committed fixture is not re-captured; `--check` exits 0
before and after.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed (module run explicitly, measured time and mutation results recorded);
  automated tests pass; manual steps performed.
- [x] No regressions: `--check` exits 0 and the committed fixture is unchanged.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
  `python3 tools/spec_lint.py` exits 0.

## Open questions

- None.
