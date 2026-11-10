---
id: SPEC-0539
title: Backlog readiness reads the campaign spec from its worktree or branch
status: done
owner: user
created: 2026-11-10
updated: 2026-11-10
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue]
depends_on: [SPEC-0180, SPEC-0463]
source_idea: IDEA-1166
---

## Context

`backlog_readiness` in `src/nightwright/agentic/queue.py` decides whether a `QUEUED`
campaign may be admitted. After the red-kind, merge, `after`, and spec-less idea
returns, it loads the campaign spec from the project base checkout: `repo.root`
joined with `spec.rel_path` (for a split, `split.original_rel`). A missing or
unreadable file yields `blocked_by` `["spec file missing"]`. The status rule from
[SPEC-0180](./0180-queue-backlog-dependency-gated-prioritized.md) then requires
`accepted`, or `in-progress` when `force` is set. `depends_on` files are resolved
in that file's parent directory, which is the base checkout's specs directory.

That base path is the right source the first time a campaign is queued. `apply_add`
records `repo.branch` (`nw/<campaign-id>`) and `repo.worktree` (the planned path)
before either exists, and the accepted spec is on `repo.root`. Open creates the
branch from the base.

A campaign that already has a branch is different. The spec it will resume is the
one on that branch. [SPEC-0463](./0463-periodic-deterministic-resolution-of-queue.md)
resumes open into `repo.worktree` when that path is already the checkout of
`repo.branch`, and a worktree file whose status is `accepted` or `in-progress`
finishes in `IMPLEMENTING`. Readiness never reaches that resume. It still reads
`repo.root`. IDEA-889's accepted spec (SPEC-0412) was committed on the campaign
branch and was absent from main, so every tick reported `spec file missing`. The
campaign stayed `QUEUED`.

`repo.worktree` being set is not evidence of a checkout. Every queued campaign
has a planned path. The campaign branch existing is the signal that this campaign
already has its own spec line.

## Goals / Non-goals

**Goals**

- When `repo.branch` exists, readiness reads the campaign spec from that branch's
  line: the planned worktree's working tree when that path is the branch's
  checkout, otherwise the committed blob `branch:spec.rel_path`.
- An accepted spec that exists only on that line makes the campaign ready, so
  admission can reach the SPEC-0463 resume path. A non-accepted status on that
  line blocks with `spec status is <status>`, including when the base checkout
  has a different status or no file.
- When `repo.branch` does not exist, readiness keeps reading `repo.root / rel`.
  A missing `repo.root`, an empty `repo.root`, and a non-git `repo.root` stay on
  that read.
- `depends_on` files stay resolved in the base checkout's specs directory. The
  dependency id list is the one in the frontmatter readiness just read.
- Split campaigns keep reading `repo.root / split.original_rel` for status and
  for the review-sha comparison.

**Non-goals**

- Changing open, `worktree_add`, or `worktree_resume_state`. A branch that is not
  checked out at `repo.worktree` still follows today's open path.
- Creating, deleting, or resetting a worktree or branch. Copying the branch spec
  onto the base checkout.
- `after` resolution, `_specs_dir_of`, red-kind and merge early returns, and
  `has_no_spec`. An unmet `after` still blocks before the spec file is read.
- A new `blocked_by` string. Absence, a symlink, a path that escapes the
  worktree, and a branch-source frontmatter parse failure stay
  `["spec file missing"]` and do not raise. Invalid YAML and a non-UTF-8 file
  are parse failures. The base-source read keeps today's `(OSError, ValueError)`
  mapping.
- Rewriting SPEC-0180. This spec amends only the source of the campaign spec's
  own frontmatter inside `backlog_readiness`.

## Decision

`backlog_readiness` treats an existing `repo.branch` as the source of the
campaign's own spec. When `git.worktree_for_branch(repo.root, repo.branch)`
resolves to `Path(repo.worktree)`, the bytes are the working tree file resume
will read (`_read_worktree_spec_bytes`). Otherwise the bytes are
`git show <branch>:<spec.rel_path>` in `repo.root`. That frontmatter supplies
`status` and `depends_on`. The status rule is unchanged. Each dependency file
is still opened under the base specs directory, the parent of
`repo.root / spec.rel_path`. When the branch exists and that source has no
readable spec, the result is `(False, ["spec file missing"])`.

When `repo.branch` is absent, empty, or not a branch of `repo.root`, and when
`repo.root` is missing, empty, or not a git checkout, readiness reads
`repo.root / spec.rel_path` as it does today. Split campaigns always use that
base path, including when their branch exists.

## Design

The early returns in `backlog_readiness` stay in order: red kinds, merge,
`_after_blocked`, then `has_no_spec`. The branch source applies only to the
spec-file read that follows, and only when `state.kind` is not `split`.

Add `_campaign_spec_text(state) -> tuple[str | None, bool]` next to
`backlog_readiness`. The bool is true when the campaign branch is the source.
`repo` is `state.get("repo") or {}`, `rel` is
`(state.get("spec") or {}).get("rel_path") or ""`, `root` is
`repo.get("root") or ""`, `branch` is `repo.get("branch") or ""`, and
`worktree` is `repo.get("worktree") or ""`. A missing key is empty and does
not raise. `allowed` is `("accepted", "in-progress")` when
`state.get("force")` is true, and `("accepted",)` otherwise.

The branch is the source only when `root` is a non-empty string, `branch` is a
non-empty string, and `git.branch_exists(root, branch)` is true. A `GitError`
from `branch_exists`, a false result, an empty `root`, or an empty `branch`
means the branch is not the source: the caller reads the base file and does
not call `worktree_for_branch` or `git show`. `branch_exists` stays the
`check=False` rev-parse it is today, so a non-git `root` is "no branch"
and does not raise out of `backlog_readiness`.

When the branch is the source:

- If `worktree` is a non-empty string and
  `git.worktree_for_branch(root, branch) == Path(worktree).resolve()`, the
  planned checkout is the source. Read it with `_read_worktree_spec_bytes`.
  `(data, None)` decodes through `_spec_bytes_as_text` and that string is the
  spec, including a file that is not committed yet. `(None, None)` and
  `(None, error)` both mean no readable file. Do not consult the branch blob
  and do not consult the base checkout. A `GitError` from
  `worktree_for_branch` means this checkout match failed; fall through to the
  blob below, still without reading the base checkout.
- Otherwise the source is the committed blob. Run
  `git.git(root, "show", f"{branch}:{rel}", check=False)` (one argument, no
  shell). Return code 0 and stdout is the spec text.

`_campaign_spec_text` catches `GitError`, `OSError`, and `UnicodeDecodeError`
from the worktree decode, from `git.git`, and from `_spec_bytes_as_text`. Any
of those, a non-zero `git show`, or an empty `rel` returns `(None, True)` when
the branch is the source, and `backlog_readiness` returns
`(False, ["spec file missing"])`. On branch text, `split_frontmatter` is
inside `except (OSError, ValueError, UnicodeDecodeError, yaml.YAMLError)`; a
non-mapping frontmatter, including `None` when the `---` block is absent,
returns `(False, ["spec file missing"])`.

Base source (the bool is false), including every split: keep today's
`Path(root or "") / rel` read and the same `(OSError, ValueError)` mapping.
Split keeps using `split.original_rel` and `_spec_sha` on that base path.

The status check is the existing one, applied to the frontmatter just parsed.
Any other status appends `spec status is <status or 'missing'>`.

For a non-split campaign, `depends_on` is taken from that frontmatter. The
directory passed to `_find_dep_spec` and `dependency_met` is the parent of
`Path(root) / rel` on the base checkout, including when that base file is
absent. The loop body is unchanged, including the SPEC-0347 `MERGED` carve-out
(`_dep_campaign`) and the `HANDED_OFF` suffix. A dependency file that exists
only in the worktree does not satisfy the dependency. A `done` file in the
base specs directory does, including when the worktree copy of that id is
`accepted` or absent.

Update the `backlog_readiness` docstring so it describes this source rule: the
campaign spec comes from the branch line when the branch exists, and from the
base checkout otherwise; dependency files come from the base specs directory.

Admission (`pick_admissions`), the tick, and resume are unchanged. A campaign
this function now marks ready is admitted under the existing slot and priority
rules, and open then applies SPEC-0463.

## Alternatives considered

- **Fall back to the base file when the branch source has no readable spec.**
  A campaign whose branch exists would become ready from main's copy, then
  SPEC-0463 would park `open_failed` because the worktree file resume reads is
  absent. The branch line is the spec open will see.
- **Always `git show` the branch tip and ignore the working tree.** Resume
  reads the working tree through `_read_worktree_spec_bytes`. An uncommitted
  `accepted` file would stay `spec file missing`, and an uncommitted `draft`
  would be admitted from an older commit.
- **Treat any existing `repo.worktree` directory as the source.** `apply_add`
  stores that path before the checkout exists. A first queue would stop seeing
  the accepted spec on the base.
- **Copy the branch spec into the base checkout before the check.** Readiness
  must not write the main checkout. The base remains the dependency authority
  from SPEC-0180.

## Acceptance criteria

- [x] A `QUEUED` campaign whose `repo.branch` is checked out at `repo.worktree`,
      whose `spec.rel_path` there is status `accepted`, and whose base checkout
      has no file at that path, gets `(True, [])` from `backlog_readiness`.
      `pick_admissions` with one free slot includes that campaign.
- [x] On that same checkout, status `proposed` yields
      `(False, ["spec status is proposed"])` when the base file is `accepted`
      or absent. Status `in-progress` yields
      `(False, ["spec status is in-progress"])` when `force` is false, and
      `(True, [])` when `force` is true.
- [x] An uncommitted `accepted` file in that checkout is ready when the branch
      tip and the base checkout both lack the path.
- [x] On the planned checkout, a working-tree status `accepted` yields
      `(True, [])` when the branch-tip blob is `draft` and the base file is
      absent. A working-tree status `draft` yields
      `(False, ["spec status is draft"])` when the branch-tip blob is
      `accepted` and the base file is absent.
- [x] When that checkout is the branch's worktree and the working-tree path is
      absent, readiness is `(False, ["spec file missing"])` when the branch tip
      has an `accepted` spec and when the base checkout has an `accepted` spec.
- [x] When the branch exists and is not checked out at `repo.worktree`, an
      `accepted` blob at `branch:spec.rel_path` yields `(True, [])` when the
      base checkout has no such file.
- [x] When `repo.branch` names a branch that does not exist, an `accepted` file
      at `repo.root / spec.rel_path` yields `(True, [])`, a `draft` file yields
      `(False, ["spec status is draft"])`, and a missing file yields
      `(False, ["spec file missing"])`.
- [x] When `repo.root` is not a git checkout and `repo.branch` is set, those
      same three base-file results hold, and `backlog_readiness` does not raise.
      When `repo.root` is absent or `""`, `branch_exists` is not called, and an
      accepted file at `spec.rel_path` relative to the current directory yields
      `(True, [])`. When `repo.root` is a non-empty path whose directory is
      absent, `branch_exists` raises `GitError`, that error is caught, and the
      result is `(False, ["spec file missing"])` without raising.
- [x] With the campaign spec read from the worktree and
      `depends_on: [SPEC-0002]`: a base specs file `SPEC-0002` at status `done`
      yields `(True, [])` when the worktree copy of SPEC-0002 is `accepted` or
      absent; a base file at status `accepted` yields
      `(False, ["waiting on SPEC-0002 (accepted)"])` when the worktree copy is
      `done`; a missing base file yields `(False, ["SPEC-0002 not found"])`
      when the worktree copy is `done`.
- [x] A `kind: split` campaign whose branch is checked out at `repo.worktree`
      is judged from `repo.root / split.original_rel`. An `accepted` base file
      whose sha matches `split.review_sha` yields `(True, [])` when the
      worktree copy is `draft`. Editing the base file yields
      `(False, ["spec changed since the split review"])`. A base status of
      `draft` yields a blocked list containing `spec status is draft` when the
      worktree copy is `accepted`.
- [x] A symlink at the spec path inside the planned checkout, pointing at an
      `accepted` spec outside the worktree, yields
      `(False, ["spec file missing"])` when the branch tip has an `accepted`
      blob at that path and the base checkout has an `accepted` file at that
      path.
- [x] A worktree file that is not valid UTF-8, and a branch blob whose
      frontmatter is invalid YAML, each yield `(False, ["spec file missing"])`
      and `backlog_readiness` does not raise, including when the base checkout
      has an `accepted` file at that path.
- [x] Red-kind, merge, unmet `after`, and `has_no_spec` returns still happen
      before the branch read. An idea campaign with an unmet `after` stays
      blocked with the `after` text while it has no spec id.

## Test plan

- **Automated tests:** new `tests/test_backlog_readiness_branch_spec.py`. The
  module imports `pytest` and the queue and git helpers it needs. It does not
  import `tui_harness`, `tui_budget`, `remote_loopback`, `remote_support`, or
  `remote_workers_golden`, and it does not call `run_test`. Its one
  module-level speed-tier marker is `pytestmark = pytest.mark.fast`. SPEC-0490
  classifies that name as `fast`. `python3 tools/test_tiers.py --check` exits 0.
  Each test builds a temp git repo with `git init -b main` and one commit that
  does not add the campaign spec, except the non-git, empty-root, and
  missing-directory states.
  Campaign state is a dict passed to `queue.backlog_readiness` (and
  `pick_admissions` in the first test): `repo.root`, `repo.branch`,
  `repo.worktree`, `spec.rel_path`, `kind`, `force`, and for the split case
  `split.original_rel` and `split.review_sha`.
  - `test_worktree_accepted_spec_is_ready_when_base_lacks_the_file`:
    `git worktree add -b` the planned path from main, write an `accepted`
    spec only in the worktree, commit it there. Assert `(True, [])` and that
    `pick_admissions` of that one-item backlog with `busy=0` and `slots=1`
    returns it.
  - `test_worktree_status_governs_when_base_disagrees`: the worktree file is
    `proposed` while the base file is `accepted` (and again with the base file
    absent). Assert `["spec status is proposed"]`. Rewrite the worktree file to
    `in-progress` and assert the `force` false and true results above.
  - `test_uncommitted_worktree_spec_is_the_readiness_source`: checkout at the
    planned path, spec `accepted` on disk, not committed, absent from main.
    Assert `(True, [])`.
  - `test_worktree_file_beats_the_branch_blob`: on the planned checkout, with
    the base file absent, commit `draft` on the branch, rewrite the worktree
    file to `accepted` without committing, and assert `(True, [])`. Commit
    `accepted`, rewrite the worktree file to `draft` without committing, and
    assert `(False, ["spec status is draft"])`.
  - `test_absent_worktree_file_does_not_fall_through`: commit the `accepted`
    spec on the branch, delete the working-tree file without committing, and
    also write an `accepted` copy on main. Assert
    `(False, ["spec file missing"])`.
  - `test_branch_blob_is_ready_when_the_worktree_is_not_the_checkout`: commit
    the `accepted` spec on the branch, remove that worktree, leave
    `repo.worktree` pointing at the removed path, and leave main without the
    file. Assert `(True, [])`.
  - `test_missing_branch_reads_the_base_checkout`: `repo.branch` set to a name
    `branch_exists` rejects, spec only under `repo.root`. Assert ready,
    `spec status is draft`, and `spec file missing` for the three base files.
  - `test_non_git_root_reads_the_base_file`: `repo.root` is a plain directory,
    `repo.branch` is `nw/some-campaign`. Assert the same three results and that
    the call returns. A second state with `repo.root` `""` uses
    `monkeypatch.chdir` onto a directory that contains an `accepted` spec, and
    `monkeypatch` makes `git.branch_exists` raise if called. Assert `(True, [])`.
    A third state sets `repo.root` to a non-empty path whose directory does not
    exist, with `repo.branch` set. Assert `(False, ["spec file missing"])` and
    that the call does not raise.
  - `test_depends_on_resolves_against_the_base_specs_dir`: worktree campaign
    spec `depends_on: [SPEC-0002]`, base file
    `docs/specs/0002-d.md`. Assert the three results in the dependency
    acceptance criterion (base `done` with worktree copy `accepted`, base
    `accepted` with worktree copy `done`, base file absent with worktree copy
    `done`).
  - `test_split_readiness_stays_on_the_base_file`: `kind` `split`, branch
    checked out at `repo.worktree`, `split.review_sha` equal to the sha256 of
    the base file bytes. Assert the three split results in the acceptance
    criterion.
  - `test_worktree_symlink_spec_is_not_ready`: commit the `accepted` spec on
    the branch, replace the worktree path with a symlink pointing at an
    `accepted` spec outside the worktree, write an `accepted` copy on main,
    and assert `(False, ["spec file missing"])`.
  - `test_branch_source_parse_failure_does_not_raise`: on the planned checkout,
    the worktree file is not valid UTF-8. Separately, the branch is not checked
    out at `repo.worktree` and the blob's frontmatter is invalid YAML. In both
    cases the base checkout has an `accepted` file at that path. Each yields
    `(False, ["spec file missing"])` and `backlog_readiness` does not raise.
- **Manual verification:** none. The tests use a temp repo. Do not point them
  at the live data directory or the existing IDEA-889 worktree.
- **Regression guard:** `tests/test_agentic_queue.py::test_backlog_readiness_reasons`
  and `tests/test_agentic_split.py::test_split_readiness_ignores_depends_on`
  pass with their assertions unchanged. `tests/test_idea_ordering.py::test_after_idea_waits_then_is_ready_when_prerequisite_merges`
  still blocks on the `after` text and becomes ready only once that prerequisite
  is `MERGED`. Run
  `python3 tools/affected_tests.py --run uv run pytest -q tests/test_backlog_readiness_branch_spec.py tests/test_agentic_queue.py tests/test_agentic_split.py tests/test_idea_ordering.py`
  and `python3 tools/spec_lint.py`. Extending
  `tests/test_agentic_queue.py`, `tests/test_agentic_split.py`, and
  `tests/test_idea_ordering.py` is not required; each already has its one
  module-level tier marker.

### Test record (2026-11-10)

- `uv run pytest -q tests/test_backlog_readiness_branch_spec.py tests/test_agentic_queue.py::test_backlog_readiness_reasons tests/test_agentic_split.py::test_split_readiness_ignores_depends_on tests/test_idea_ordering.py::test_after_idea_waits_then_is_ready_when_prerequisite_merges` — 17 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (2026-11-10, loop 2)

- `uv run pytest -q tests/test_backlog_readiness_branch_spec.py tests/test_agentic_queue.py::test_backlog_readiness_reasons tests/test_agentic_split.py::test_split_readiness_ignores_depends_on tests/test_idea_ordering.py::test_after_idea_waits_then_is_ready_when_prerequisite_merges` — 28 passed.
- `python3 tools/test_tiers.py --check` — exit 0.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### As shipped (2026-11-10, loop 2)

- Branch `git show` bytes are capped at `MAX_WORKTREE_SPEC_BYTES` like the worktree read.
- Invalid branch-sourced `depends_on` (booleans, non-lists) yields `spec file missing` via `_readiness_dep_ids`.

## Rollout / migration

Ship the readiness change with the new tests. No campaign rewrite and no
checkout rewrite. The next queue tick re-reads every `QUEUED` campaign. A
campaign whose branch source is `accepted` (or `in-progress` with `force`) and
whose dependencies are met on the base becomes ready and is admitted under the
existing rules; SPEC-0463 resume then applies when the planned worktree is
already that branch's checkout. A campaign whose branch source is some other
status stays queued with `spec status is <status>`. A branch that exists and
is not checked out at `repo.worktree` can pass readiness and then hit today's
open path.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the automated tests above pass. No manual steps.
- [x] No regressions in the readiness, split, and `after` tests named above.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

- None.
