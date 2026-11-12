---
id: SPEC-0592
title: Backlog readiness spec reader hardening — dependency id check, pathological spec files, real-checkout early-return tests
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue]
depends_on: [SPEC-0539]
source_idea: IDEA-1168
---

## Context

[SPEC-0539](./0539-backlog-readiness-queue-py-1123-checks-spec-file.md) made
`backlog_readiness` (`src/nightwright/agentic/queue.py`) read the campaign spec from the
campaign branch line. Its review (IDEA-1166-20261110-26ae) left three advisories that were
out of its scope. Reading the shipped code confirms each:

1. **`depends_on` ids reach a glob unchecked.** `_readiness_dep_ids` accepts any non-empty
   string. `_find_dep_spec(specs_dir, dep)` then builds `Path(specs_dir).glob(f"{dep}-*.md")`,
   and `specs._find_spec` builds `glob(f"{num}-*.md")` from the text after the first `-`.
   A branch-sourced spec is authored by an agent, so the id is not trusted. An id of `*`
   matches the first spec file in the directory and is judged as if it were the dependency.
   An absolute id such as `/etc/x` makes `Path.glob` raise `NotImplementedError`, which is not
   in the `(OSError, ValueError)` catch, so the exception leaves `backlog_readiness` and the
   queue tick.
2. **Pathological spec files raise out of readiness.** Only the branch-source parse catches
   `yaml.YAMLError`. The base-source read, the split read, and the dependency file read catch
   `(OSError, ValueError)`, and `yaml.YAMLError` is not a `ValueError`, so an invalid-YAML
   base or dependency spec raises. No site catches `RecursionError` (PyYAML's composer
   recurses on deeply nested flow collections such as `[[[[…`, and 4 MiB of `[` fits under
   `MAX_WORKTREE_SPEC_BYTES`). `MemoryError` is caught only around the decode in
   `_campaign_spec_text`, not around the parse. Base, split and dependency files are read
   with no size cap. `dependency_met`'s superseded-replacement scan reads every spec in the
   directory with the same narrow catch.
3. **The checkout and early-return tests are thin.** `test_early_returns_still_precede_the_branch_read`
   monkeypatches `git.git` and `git.branch_exists` and never builds a campaign whose branch is
   really checked out with an `accepted` spec, so it cannot show that the early returns win
   over a ready branch source. It covers the merge and red-kind returns only: not the unmet
   `after` return and not the spec-less idea return.

## Goals / Non-goals

**Goals**

- A `depends_on` entry that is not a well-formed spec id never reaches a glob; the readiness
  result for it is `(False, ["spec file missing"])`, as for the other invalid `depends_on`
  shapes, and `backlog_readiness` does not raise.
- `_find_dep_spec` itself refuses a malformed id with `ValueError`, so every caller is safe.
- Every spec read reachable from `backlog_readiness` through its own code, `_spec_after_met`
  (the `after` prerequisite read) and `dependency_met`'s replacement scan (branch worktree,
  branch blob, base file, split file, dependency file, `after` spec file, replacement
  files) maps `yaml.YAMLError`, `RecursionError`, `MemoryError`, `UnicodeDecodeError`,
  `OSError`, and `ValueError` to the existing "unreadable" outcome (`spec file missing`,
  `<dep> not found`, or the MERGED branch), and the base, split, dependency, `after` and
  replacement reads are size-capped like the worktree read.
- Real-checkout fixtures prove the early returns (red kind, merge, unmet `after`, spec-less
  idea) win over a branch that is checked out with an `accepted` spec.

**Non-goals**

- Any new `blocked_by` string, or changing which status is ready.
- Changing where a spec is read from (SPEC-0539's source rule), `after` resolution (only
  the file read inside `_spec_after_met` is hardened, not which prerequisite is chosen),
  or open.
- `_spec_reasons` and `_dangling` (queue.py around lines 9450 and 10920) are not reached
  from `backlog_readiness`; they keep their narrow `(OSError, ValueError)` catch, and only
  their `ValueError` on a malformed id changes (it reads as "not found" there too).
- Refusing a symlinked base or dependency spec; only the byte cap is added there.
- Validating `depends_on` anywhere outside `backlog_readiness` and `_find_dep_spec`
  (spec lint already owns authoring-time validation).

## Decision

Add one id check and one frontmatter reader, and use them at every spec read in
`backlog_readiness`. A dependency id must full-match the existing `_ANY_ID_RE`
(`^[A-Za-z][A-Za-z0-9]*-\d+$`, the id shape for `SPEC-NNNN` and outbound `PROJ-NNNN`) after
`strip()`; otherwise it is invalid. `_find_dep_spec` enforces the same check as a
`ValueError`. A new `_read_frontmatter_text` reader wraps `split_frontmatter` and the file
read with one exception tuple and a byte cap. Hardening and tests only: no result for a
well-formed spec changes.

## Design

**Dependency id check.** In `src/nightwright/agentic/queue.py`:

- `_readiness_dep_ids(fm)` strips each string entry (a bare string value too) and returns
  `None` (invalid) when any stripped id is empty or does not full-match `_ANY_ID_RE`. The
  returned ids are the stripped ones. Existing `None` cases (booleans, non-list, non-string
  entries) stay.
- `_find_dep_spec(specs_dir, dep)` raises `ValueError(f"invalid dependency id {dep!r}")` when
  `dep` is not a string that full-matches `_ANY_ID_RE`, before any glob. It also builds
  the exact-prefix glob with `glob.escape(dep)`. Its callers already catch `ValueError`
  (queue.py around lines 1361, 1409, 9450, 10920). A malformed id therefore reads as "not
  found"; the lines 9450 and 10920 sites are otherwise unchanged (see Non-goals).
- `_find_spec` in `specs.py` is not changed; it is reached only after `_find_dep_spec`
  passed the check.

**One reader.** Add `_read_frontmatter_text(path) -> dict | None` next to
`_readiness_dep_ids`. Its steps:

1. `os.stat(path)`; a size over `MAX_WORKTREE_SPEC_BYTES` returns `None` (symlinks are followed,
   as today).
2. `Path(path).read_text(encoding="utf-8")` then `split_frontmatter`.
3. Any of `OSError`, `ValueError` (covers `UnicodeDecodeError`), `yaml.YAMLError`,
   `RecursionError`, `MemoryError` returns `None`. A non-mapping frontmatter returns `{}`
   (today's base and dependency behavior, so an absent `---` block still reads as empty
   frontmatter).

A second helper `_parse_frontmatter_text(text) -> dict | None` does steps 2's parse and
exception tuple on already-decoded text and returns `None` for a non-mapping (today's
branch-source behavior: `(False, ["spec file missing"])`).

**Call sites in `backlog_readiness`:**

- Split read: `_read_frontmatter_text(root / rel)`; `None` returns `["spec file missing"]`.
- Branch source: `_parse_frontmatter_text(text)`; `None` returns `["spec file missing"]`.
- Base source: `_read_frontmatter_text(root / rel)`; `None` returns `["spec file missing"]`.
- Dependency loop: replace the `try: split_frontmatter(_find_dep_spec(...).read_text())
  except (OSError, ValueError)` with
  `try: found = _find_dep_spec(spec_path.parent, dep)` / `except ValueError:` (a malformed
  or unmatched id; the existing handler) followed by `dep_fm = _read_frontmatter_text(found)`.
  Both `ValueError` and `dep_fm is None` (oversize, unreadable, invalid YAML, recursion,
  memory) run the same handler body: append `"<dep> not found"` unless `merged`, then
  `continue`. A non-mapping dependency frontmatter returns `{}` (not `None`), so it still
  reaches `dependency_met` and gives `waiting on <dep> (no status)`. `{}` means a present
  spec with no status; `None` means unreadable.
- `_spec_after_met`: the same two-step shape: `_find_dep_spec(_specs_dir_of(state), spec_id)`
  with `except (OSError, ValueError)` for the lookup, then `_read_frontmatter_text(found)`;
  on `ValueError` or `None` return `(True, "", live)` when the campaign is `MERGED`, else
  `(False, f"{spec_id} not found", live)`, as today. A `{}` result goes on to
  `dependency_met`.
- `_campaign_spec_text` keeps its `(GitError, OSError, UnicodeDecodeError, MemoryError)` catch
  and gains `RecursionError` for symmetry; its decode step is unchanged.
- `dependency_met`'s replacement scan uses `_read_frontmatter_text(f)` and skips a file that
  returns `None`.

**Tests, real fixtures.** All new tests extend
`tests/test_backlog_readiness_branch_spec.py` (module marker `pytestmark = pytest.mark.fast`
already present; no second tier). A new helper `_checked_out_accepted(tmp_path)` builds a
temp git repo, a worktree on `nw/camp-1` at the planned path, and commits an `accepted` spec
there, so `backlog_readiness` without an early return would give `(True, [])`.

## Alternatives considered

- **Escape the glob only (`glob.escape`) and keep arbitrary ids.** An id with a path
  separator or an absolute path still raises or reaches outside the specs directory; an
  id-shape check is simpler and matches what spec lint already requires.
- **Catch `Exception` at the top of `backlog_readiness`.** It would hide real bugs and
  would turn a bad dependency into a whole-campaign "spec file missing" instead of the
  specific `<dep> not found`.
- **Reuse `_read_bounded_regular_file` for base and dependency files.** It refuses
  symlinks, which would change which base checkouts are ready. Only the size cap is wanted.
- **Leave `RecursionError`/`MemoryError` unhandled (the tick is crash-supervised).** A
  single pathological spec would then stall every queued campaign on every tick.

## Acceptance criteria

- [x] `_readiness_dep_ids` returns `None` for `depends_on: ["*"]`, `["../x"]`, `["/etc/x"]`,
      `["SPEC-0002/.."]`, `["SPEC-0002, SPEC-0003"]`, and `["a-*"]`, and returns
      `["SPEC-0002"]` for `["SPEC-0002 "]` and for the bare string `SPEC-0002`.
- [x] A branch-sourced spec with `depends_on: ["*"]` yields `(False, ["spec file missing"])`
      even when the base specs directory holds a `done` spec; it is not judged against the
      first file in the directory. `depends_on: ["/etc/x"]` yields the same and does not raise.
- [x] `_find_dep_spec(specs_dir, dep)` raises `ValueError` for `*`, `../x`, `/abs`, `""`, and
      a non-string, and returns the same path as today for `SPEC-0002` and an outbound
      `PROJ-0007` (exact `PROJ-0007-*.md` wins over a `.contract.md` sidecar).
- [x] A campaign spec containing a 100000-deep `[` flow sequence in its frontmatter (within
      the size cap) yields `(False, ["spec file missing"])` and does not raise, for the
      branch worktree source, the branch blob source, the base source, and a `split` base file.
- [x] A base-source spec with invalid YAML frontmatter yields `(False, ["spec file missing"])`
      and does not raise (it raised `yaml.YAMLError` before).
- [x] A `split_frontmatter` that raises `RecursionError`, and one that raises `MemoryError`,
      each yield `(False, ["spec file missing"])` for the base and branch sources.
- [x] A base-source, split, `after`, or dependency spec file larger than `MAX_WORKTREE_SPEC_BYTES`
      (patched to a small value) is treated as unreadable: `spec file missing` for the
      campaign spec, `<dep> not found` for a dependency.
- [x] A dependency file, or an `after` spec file read by `_spec_after_met`, with pathological
      frontmatter (deep nesting, invalid YAML) yields `["SPEC-0002 not found"]` and does not
      raise; with a `MERGED` campaign the dependency is still met. A non-mapping dependency
      frontmatter still yields `waiting on SPEC-0002 (no status)`. `dependency_met` for a
      `superseded` dependency skips an unparsable replacement file and decides from the
      readable ones.
- [x] A `done` dependency and an `accepted` campaign spec with well-formed ids still yield
      `(True, [])` (no change for valid specs).
- [x] With a real checkout whose branch holds an `accepted` spec (the
      `_checked_out_accepted` fixture): a `revert` campaign returns `(True, [])` and a
      `merge` campaign with a real existing branch returns `(True, [])` without
      `_campaign_spec_text` being called; an unmet `after` returns `(False, [<after text>])`;
      an idea campaign with no spec id and an unmet `after` returns the `after` text, and one
      with no unmet `after` is decided by its idea, as in `test_idea_ordering`; in each case
      `_campaign_spec_text` is not called.

## Test plan

- **Automated tests:** extend `tests/test_backlog_readiness_branch_spec.py`. It already has its
  one module-level marker `pytestmark = pytest.mark.fast`; the name is `fast` under SPEC-0490,
  and no second marker is added. If `tools/test_tiers.py` exists,
  `python3 tools/test_tiers.py --check` exits 0.
  - `test_readiness_dep_ids_reject_non_id_shapes` (parametrized): the id lists in the first
    criterion against `Q._readiness_dep_ids`.
  - `test_branch_dep_glob_star_is_not_a_wildcard`: worktree spec `depends_on: ["*"]` committed
    on the branch, a `done` `0002-d.md` in base; assert `(False, ["spec file missing"])`.
    A second case with `["/etc/x"]` asserts the same result and no exception.
  - `test_find_dep_spec_refuses_malformed_ids` (parametrized): raises `ValueError` for each
    bad id; returns the file for `SPEC-0002` and `PROJ-0007` with a `.contract.md` sidecar.
  - `test_deeply_nested_frontmatter_is_unreadable_on_every_source`: parametrized over the
    four sources (planned worktree, committed blob with the worktree removed, base file with no
    branch, `kind: split` base file); the file body is `"---\nstatus: " + "[" * 100000 +
    "\n---\n"`; assert `(False, ["spec file missing"])`.
  - `test_invalid_yaml_base_spec_does_not_raise`: base file with `status: [unclosed`.
  - `test_frontmatter_recursion_and_memory_errors_are_unreadable` (parametrized
    `RecursionError`, `MemoryError`): `monkeypatch` `Q.split_frontmatter` to raise; base and
    branch source.
  - `test_oversize_base_split_and_dep_files_are_unreadable`: patch
    `Q.MAX_WORKTREE_SPEC_BYTES` to 200 and write 500-byte files; assert the campaign-spec
    and dependency results above.
  - `test_pathological_dependency_file_is_not_found`: dependency file deeply nested and
    invalid-YAML variants; assert `["SPEC-0002 not found"]`; plus an outbound `PROJ-0007`
    dependency with a `MERGED` campaign stays met (stub `Q._dep_campaign`).
  - `test_after_spec_file_pathological_is_not_found`: `_spec_after_met` with the `after`
    spec file deeply nested, invalid YAML and oversize; assert `(False, "SPEC-0002 not found",
    live)`, and `(True, "", live)` with a `MERGED` campaign (stub `store.list_campaigns`).
  - `test_non_mapping_dependency_frontmatter_has_no_status`: dependency file whose
    frontmatter is a YAML list; assert `["waiting on SPEC-0002 (no status)"]`.
  - `test_dependency_met_skips_unparsable_replacement`: `superseded` dependency with one
    readable replacement (`done`) and one unparsable spec in the directory; assert met.
  - `test_early_returns_win_over_a_checked_out_accepted_spec`: `_checked_out_accepted`
    repo; `monkeypatch` `Q._campaign_spec_text` to raise `AssertionError`; assert the red kind,
    real-branch `merge`, unmet-`after` spec campaign, and spec-less idea campaign with an
    unmet `after` results, plus a spec-less idea campaign with no unmet `after` decided by
    its idea.
  - Existing tests, including the monkeypatch-based early-return test, are kept.
- **Manual verification:** none; temp repos only. Do not point the tests at the live data
  directory.
- **Regression guard:** the existing tests in the module and `tests/test_agentic_queue.py`,
  `tests/test_agentic_split.py`, `tests/test_idea_ordering.py` pass unchanged. Run
  `python3 tools/affected_tests.py --run uv run pytest -q tests/test_backlog_readiness_branch_spec.py tests/test_agentic_queue.py tests/test_agentic_split.py tests/test_idea_ordering.py`
  and `python3 tools/spec_lint.py`.

### Test record (2026-11-12)

- `uv run pytest -q tests/test_backlog_readiness_branch_spec.py`: 75 passed (the 30-odd new cases
  for the ten test-plan items plus the existing ones).
- `uv run pytest -q -n 3 tests/test_backlog_readiness_branch_spec.py tests/test_agentic_queue.py
  tests/test_agentic_split.py tests/test_idea_ordering.py`: 325 passed (the plan's regression-guard
  files; the `affected_tests.py` wrapper stalled under machine load, so the same files were run
  directly).
- `python3 tools/test_tiers.py --check`: exit 0. `python3 tools/spec_lint.py`: exit 0.
- The full suite was left to gating.

### As shipped

- `_is_dep_id` (ASCII + `_ANY_ID_RE.fullmatch`) is the one id predicate; `_readiness_dep_ids`
  and `_find_dep_spec` both use it. `glob.escape` was not added (the check leaves only
  `[A-Za-z0-9-]`). `0002` (bare number) and non-ASCII digits now read as `spec file missing` /
  `<dep> not found`; tested.
- `_read_frontmatter_text` also refuses a non-regular file (`S_ISREG` on the followed stat, so a
  symlink to a regular file still reads) and reads at most `MAX_WORKTREE_SPEC_BYTES + 1` bytes,
  so a FIFO cannot block the tick and a file that grew past the cap is unreadable. It decodes
  CRLF to LF like `_spec_bytes_as_text`.
- `dependency_met` skips a replacement file whose reader result is empty (`None` or `{}`),
  which matches the old `not isinstance(rfm, dict) or not rfm.get("id")` skip.
- Extra tests beyond the plan: `test_special_files_are_unreadable`,
  `test_after_spec_malformed_id_is_not_found`, `test_well_formed_ids_are_unchanged`.

## Rollout / migration

Ship the code change with the tests. No data migration. A queued campaign whose branch spec
declares a malformed `depends_on` id moves from an uncaught exception or a wrong-file match
to `spec file missing` on the next tick; a malformed id in the base spec behaves the same.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (affected tests); no manual steps.
- [x] No regressions in the readiness, split, and `after` tests named above.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

- None.
