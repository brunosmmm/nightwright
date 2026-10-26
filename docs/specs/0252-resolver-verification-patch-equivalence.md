---
id: SPEC-0252
title: "Conflict resolver verification by patch equivalence, runner commits resolver changes"
status: in-progress
owner: user
created: 2026-10-25
updated: 2026-10-26
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, close, personas, rebase, queue, bug]
source_idea: IDEA-328
depends_on: [SPEC-0221]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md). It fixes
`IDEA-328` (kind: bug) in the `conflict_resolver` shipped by
[SPEC-0221](./0221-rebase-conflict-resolver-persona.md).

`gitops.verify_resolution(worktree, branch, target_sha, fork, pre_head)` checks a resolver's
work in six steps. Step 5 requires `git rev-list --count <target_sha>..HEAD` to equal
`git rev-list --count <fork>..<pre_head>`. It also requires `git range-diff` to show no `<`
(dropped) or `>` (added) line. Two production closes on 2026-10-25 (SPEC-0237 and SPEC-0236)
reported `status: resolved` with the full suite green, and the runner still rejected them:

- `[campaign commit count changed (7 before, 8 after)]`: the resolver added a legitimate port
  commit, the code needed to adapt the campaign to what had landed on the target.
- `[campaign commit count changed (8 before, 7 after)]`: one campaign commit became empty during
  the rebase, because the target already contained its change, and git dropped it.

SPEC-0221's review had already raised this: equivalence should be checked with `git cherry` or
range-diff, not a raw count. Also, `personas.RESOLVER_INVARIANT` does not list `commit` among the
allowed git commands, and unlike the implementer path (`queue._do_step` calls `git.commit_all`
after an `implement` step), nothing commits for the resolver. One run left a required
golden-snapshot update staged and uncommitted. Check 6 (dirty tree) then rejected it.

## Goals / Non-goals

**Goals**
- G1. A resolution is accepted when every pre-resolver campaign commit is still present
  (equivalent patch), or is **absorbed into HEAD**, meaning the resolved `HEAD` already
  contains its change. That covers a change that landed on the target, and also one that a
  resolver commit re-adds or squashes. Extra commits are allowed and recorded, unless one
  reverts a campaign commit.
- G2. Changes the resolver leaves in the tree after a `resolved` verdict are committed by the
  runner, as is done for the implementer. The runner never commits over unmerged paths or an
  in-progress sequencer operation.
- G3. A dropped campaign commit whose change is not in `HEAD` still fails verification and
  restores `pre_head`.

**Non-goals**
- Limiting which files an extra commit may touch. Before the merge, the resolved branch gets
  only SPEC-0221's re-gate (the project's gates, tests only). No review persona runs on it.
  This spec adds one structural guard: an extra commit that reverts a campaign commit fails
  verification. Beyond that, the `extra_commits` list in the `rebase_resolved` event is the
  audit trail.
- Changing dispatch, attempt budgets, restore, or the close flow of SPEC-0220/0221.

## Decision

`verify_resolution` stops comparing commit counts. It accounts for each old campaign commit
`c` in `<fork>..<pre_head>` and fails only when some `c` is unaccounted for. A commit counts as
**present** when range-diff pairs it with a new commit (`=` for an identical patch-id, `!` for
a commit the resolution changed). A commit counts as **absorbed into HEAD** in any of these
cases:

- `git cherry <target_sha> <pre_head>` marks it `-` (a patch-id match in the target);
- its own binary diff reverse-applies cleanly to the `HEAD` tree;
- it was empty from the start.

New commits that pair with nothing (`>`) are allowed and returned as **extra**, unless an
extra commit reverts a non-empty campaign commit. After a `resolved` verdict, and before
verification, the runner commits any tracked changes left in the worktree with a resolver
commit message. It does this only when HEAD is attached, no rebase is running, there are no
unmerged paths, and no other sequencer state exists. The attached-HEAD, no-rebase-in-progress,
no-unmerged-paths, target-is-ancestor and tracked-clean checks stay.

## Design

- **Helper `gitops._patch_applies(worktree, commit, tree, reverse) -> bool`** (new). It builds
  a temporary index with `GIT_INDEX_FILE=<tmp>` and `git read-tree <tree>`. It then pipes
  `git diff --binary <commit>^ <commit>` into `git apply --cached --check [--reverse]`, run
  against that index. The worktree and the real index are never touched. An empty diff
  returns `True`.
- **`gitops.verify_resolution`** returns a `ResolutionCheck` dataclass
  (`failed: str | None`, `extra: list[dict]`, `absorbed: list[dict]`; each dict is
  `{"sha", "subject"}` with the full sha) instead of `str | None`. The checks run in this order:
  1. `head_ref(worktree) == refs/heads/<branch>`.
  2. `not rebase_in_progress`.
  3. `ls-files -u` is empty.
  4. `merge-base --is-ancestor <target_sha> HEAD`.
  5. **Commit accounting (replaces the count check).** Parse
     `git range-diff --no-color --creation-factor=95 <fork>..<pre_head> <target_sha>..HEAD`
     with the existing `_RANGE_DIFF_RE`, extended to capture the old and new abbreviated shas.
     Each abbreviated sha is resolved with `git rev-parse --verify <abbrev>^{commit}`, and all
     comparisons use full shas. Subjects come from `git log -1 --format=%s <sha>`. The
     `git cherry <target_sha> <pre_head>` output (full shas) is read once into a set of `-`
     shas. An old commit on an `=`/`!` line is present. For each old commit `c` on a `<` line:
     (a) if `c` is in the cherry `-` set, it is absorbed;
     (b) else, if `c` is empty (`git diff --binary c^ c` produces nothing), it is absorbed;
     (c) else, if `_patch_applies(worktree, c, "HEAD", reverse=True)`, it is absorbed;
     (d) else the check fails with
     `campaign commit <short> (<subject>) is missing after the resolution`.
     For each new commit `e` on a `>` line, a **revert guard** runs. For every non-empty old
     commit `c`, if `_patch_applies(worktree, c, e + "^", reverse=True)` and
     `_patch_applies(worktree, c, e, reverse=False)`, then `e` removed `c`'s change, and the
     check fails with `extra commit <short> (<subject>) reverts campaign commit <short>`.
     Otherwise `e` goes into `extra`.
  6. `status_porcelain(worktree, untracked=False)` is empty → else `the worktree is dirty`.
     After the runner commit, the only way to reach this is a commit that failed, for example
     a `pre-commit` hook that rejected it.
- **`gitops.commit_tracked(worktree, message, env=None) -> str | None`** (new): runs
  `git add -u`, then commits if `git diff --cached --quiet` reports changes. The commit
  includes paths the resolver already `git add`ed, new files among them. Other untracked files
  are not committed; the existing post-verify `clean_untracked` removes them. It returns the
  new sha, or `None` when nothing was staged. It does not use `commit_all`, because
  `add -A` would commit `.orig` residue. A failed `git commit` raises `GitError` as usual.
- **`queue._do_resolve`**: when the verdict is `resolved` and the run did not fail, it calls
  `git.commit_tracked` only when all of the following hold:
  - `git.head_ref(worktree) == f"refs/heads/{branch}"`;
  - `not git.rebase_in_progress(worktree)`;
  - `git ls-files -u` is empty;
  - none of `CHERRY_PICK_HEAD`, `MERGE_HEAD`, `REVERT_HEAD` or `sequencer` exists under
    `git rev-parse --git-path`.

  Otherwise nothing is committed and verification fails as before. A `GitError` from the
  commit (such as a hook rejection) is caught. Verification still runs, fails check 6, and
  the attempt is restored like any other failed attempt. The message is
  `wt(<campaign id>): conflict resolver run <run_id[:3]> — <first summary line[:72]>`. When the
  summary is empty it is `wt(<campaign id>): conflict resolver run <run_id[:3]>`, with no
  ` — `. The project's `agents.commit_trailer` is appended, as for the implementer.
  `res.resolution` gains `extra`, `absorbed` and `commit` (the runner's sha or `None`). A
  failed verification restores `pre_head`, so the runner commit is discarded with the rest.
- **`queue._apply_resolve`** adds `extra_commits`, `absorbed_commits` and `resolver_commit`
  to the `rebase_resolved` event. `schemas.py` lists the three keys for `rebase_resolved`.
  `render.rebase_resolved_text` appends `; +N extra, M absorbed` when either is non-zero, and
  shows old events without the keys unchanged.
- **`personas.RESOLVER_INVARIANT`**: `commit` (never `--amend`) joins the allowed git list. New
  lines say that changes left in the tree after a `resolved` verdict are committed by the
  runner, that additional commits are allowed when the target needs them, and that an extra
  commit must not revert a campaign commit.

## Alternatives considered

- **Patch-id only (`git cherry` both ways), no range-diff.** Rejected. A commit the resolution
  changed has a new patch-id, so every truly conflicted commit would look dropped. Range-diff
  pairing is still needed for `!` commits.
- **Let the resolver commit and leave the runner out.** Rejected as the only fix. A resolver
  that forgets to commit (the observed failure) would still be rejected. The runner commit is
  deterministic, and the resolver may still commit too.
- **Only allow extra commits that touch campaign or conflicted paths (IDEA-328's suggestion).**
  Deferred. A port commit can legitimately touch target-side files. The revert guard and the
  re-gate cover the dangerous case.
- **Run a review persona on extra commits before the merge.** Deferred. It costs a run on
  every resolution. The event's `extra_commits` list keeps the trail for later audit.

## Acceptance criteria

- [x] `gitops.verify_resolution` no longer compares commit counts. It returns a
      `ResolutionCheck(failed, extra, absorbed)` with full shas, and the string
      `campaign commit count changed` no longer appears in `src/`.
- [x] An old campaign commit is accepted in four cases: range-diff pairs it (`=`/`!`, with
      abbreviated shas resolved through `git rev-parse`); `git cherry <target_sha> <pre_head>`
      marks it `-`; it is empty; or its `git diff --binary` reverse-applies to the `HEAD` tree
      (absorbed into HEAD, through a temporary index). Any other missing commit fails with
      `campaign commit <short> (<subject>) is missing after the resolution`, and `pre_head` is
      restored.
- [x] Unpaired new commits (`>`) are returned in `extra` and pass, unless one reverts a
      non-empty campaign commit (that commit's patch reverse-applies to `e^` and forward-applies
      to `e`). That case fails with `extra commit <short> (<subject>) reverts campaign commit
      <short>`. The attached-HEAD, no-rebase, no-unmerged, target-ancestor and tracked-clean
      checks behave as before.
- [x] After a `resolved` verdict, `queue._do_resolve` commits tracked and staged changes
      through `gitops.commit_tracked` before verifying. It does so only when HEAD is attached
      to the campaign branch, no rebase is in progress, `ls-files -u` is empty, and none of
      CHERRY_PICK_HEAD, MERGE_HEAD, REVERT_HEAD or sequencer exists. The message is
      `wt(<id>): conflict resolver run <run_id[:3]>`, plus ` — <summary>` only when the
      summary is non-empty, plus the project trailer. Untracked, unstaged files are not
      committed. A commit that fails (such as a hook rejection) is caught, and the attempt
      fails `the worktree is dirty` and is restored.
- [x] The `rebase_resolved` event carries `extra_commits`, `absorbed_commits` and
      `resolver_commit`, which `schemas.py` lists. `render.rebase_resolved_text` shows
      `+N extra, M absorbed` when non-zero, and shows old events unchanged.
- [x] `personas.RESOLVER_INVARIANT` allows `commit` (never `--amend`). It says that the runner
      commits leftover changes after a `resolved` verdict and that an extra commit must not
      revert a campaign commit.
- [x] The Test plan's tests pass and `uv run pytest` is green.

## Test plan

- **Automated tests** (`tests/test_agentic_resolver.py`, real git in a tmp repo, FakeRunner
  `shell`):
  - `test_extra_commit_verified`: the resolver runs `git rebase --continue`, then commits an
    extra non-empty port commit. The result is verified, `rebase_resolved.extra_commits` names
    it, and the close merges. The existing `FAILS["extra_commit"]` case moves here as a passing
    case.
  - `test_emptied_commit_verified`: the target already contains one campaign commit's change
    (cherry-picked to the base), so the rebase drops it as empty. The result is verified and
    `absorbed_commits` names it. Two more variants are also verified: in one, the context
    differs so the patch-id does not match but the change reverse-applies to `HEAD`; in the
    other, the commit was empty from the start (`--allow-empty`) and is dropped.
  - `FAILS["dropped_commit"]` (`git rebase --skip` of a non-empty commit not in `HEAD`) still
    fails, now with `is missing after the resolution`. `FAILS["swapped_commit"]` still fails
    the same way.
  - New `FAILS["reverting_extra_commit"]`: after `rebase --continue`, the resolver runs
    `git revert --no-edit <a campaign commit>`. The attempt fails with
    `reverts campaign commit`, and `pre_head` is restored.
  - New `FAILS["unmerged_no_rebase"]`: the resolver finishes the rebase, then leaves a
    conflicted `git cherry-pick` (unmerged paths, no rebase running) and reports `resolved`.
    Nothing is committed (the branch tip is unchanged before the restore). The attempt fails
    with `unmerged paths remain` and is restored.
  - New `FAILS["hook_rejects_commit"]`: a `pre-commit` hook that exits 1 is installed, and the
    resolver leaves a tracked edit. The attempt fails with `dirty`, and the branch is
    restored. This is the reachable case for check 6.
  - `test_staged_changes_committed_by_runner`: the resolver finishes the rebase and leaves a
    `git add`ed file change plus an unstaged tracked edit and an untracked `.orig`. The
    result is verified, HEAD is a commit with the resolver message and the trailer containing
    both tracked changes, the `.orig` is neither committed nor left behind, and
    `rebase_resolved.resolver_commit` equals that sha. The old `FAILS["dirty_tree"]` becomes
    this passing case. A variant with an empty summary asserts the message has no ` — `.
  - Direct unit tests: `verify_resolution` for each accounting branch (paired, cherry `-`,
    empty, reverse-apply including a binary file, missing) and the revert guard;
    `_patch_applies` leaves the index and worktree untouched; `commit_tracked` returns `None`
    when nothing is staged.
  - `tests/test_agentic_personas.py`: the rendered invariant lists `commit`, the
    runner-commit sentence and the no-revert rule. `tests/test_render*.py` / events: the
    extended `rebase_resolved_text`, and an old event without the new keys renders unchanged.
- **Sabotage checks:** each of these must make a test fail:
  - reinstating the count check fails `test_extra_commit_verified` and
    `test_emptied_commit_verified`;
  - dropping the reverse-apply branch fails the context variant;
  - dropping `--binary` fails the binary unit test;
  - accepting every `<` line fails `[dropped_commit]`;
  - dropping the revert guard fails `[reverting_extra_commit]`;
  - dropping the `ls-files -u` guard on the runner commit fails `[unmerged_no_rebase]`;
  - removing the runner commit fails `test_staged_changes_committed_by_runner`.
- **Manual verification:** on the next real conflicted close, check that a resolver run that
  adds or empties a commit records `rebase resolved: … +N extra, M absorbed` and merges.
- **Regression guard:** full `uv run pytest`. The remaining SPEC-0221 resolver cases
  (restore, budgets, interrupted, opt-out) pass unchanged.

### Test record (2026-10-26)

- `uv run pytest -q` (full suite): 2708 passed, 3 skipped. `python3 tools/spec_lint.py --write-ledger`
  then `python3 tools/spec_lint.py`: 250 specs OK.
- `tests/test_agentic_resolver.py` (62 tests): `test_extra_commit_verified`, `test_emptied_commit_verified`
  (patch-id, context and empty variants), `test_staged_changes_committed_by_runner` (with and without a
  summary), `test_unmerged_no_rebase_commits_nothing`, the `FAILS` cases (`dropped_commit`,
  `swapped_commit`, `reverting_extra_commit`, `unmerged_no_rebase`, `hook_rejects_commit`), and unit
  tests for each accounting branch (paired, cherry `-`, empty, reverse-apply with and without a
  binary file, missing), the revert guard, `_patch_applies` and `commit_tracked`.
  `tests/test_agentic_personas.py` and `tests/test_agentic_logs.py` cover the invariant text and the
  `rebase_resolved` keys. `rebase_resolved_text` is tested for the new suffix and for old events.
- Sabotage checks, each run against the named tests and each made them fail: no reverse-apply branch
  (`test_emptied_commit_verified[context]`), no `--binary` (binary reverse-apply unit test), accepting
  every missing commit (`dropped_commit`, `swapped_commit`), no revert guard
  (`reverting_extra_commit`), no unmerged guard on the runner commit (`unmerged_no_rebase`; both the
  `ls-files -u` check and the sequencer-state check had to go, as a conflicted cherry-pick trips both),
  no runner commit (`test_staged_changes_committed_by_runner`). The sources were restored and the
  resolver tests re-run green.
- Review round 1 tests (`tests/test_agentic_resolver.py`, 71 tests, all green): runner-commit guards
  one at a time (`FAILS["unmerged_no_sequencer"]`, unmerged index entries with no sequencer;
  `FAILS["sequencer_only"]`, a clean `git merge --no-commit` leaving MERGE_HEAD), the cherry-only
  absorption (target holds the patch, then changes the lines again), an empty campaign commit plus a
  port commit (kept and dropped), user diff/apply config (`diff.noprefix`, `color.diff=always`,
  `apply.whitespace=error`), and an unusable patch raising `GitError`. Each was sabotaged alone and
  failed its test: only the `ls-files -u` guard, only the sequencer guard, `c in cherry_gone`, the
  empty-commit skip on the revert guard, and plain `git diff --binary` for the patches.
- Review round 2 tests (`tests/test_agentic_resolver.py`, 76 tests, all green): the revert guard on an
  absorbed (cherry `-`, unpaired) campaign commit; patches with CRLF and Latin-1 content verify with a
  port commit and a revert is still caught; `queue._commit_resolver_changes` cuts a multi-line summary
  to its first line at 72 chars and adds no trailer block when none is configured. Sabotage: dropping
  `guarded.append((c, c))` fails the absorbed-revert test; text-mode patches fail both byte tests.
- Review round 3 test (`tests/test_agentic_resolver.py`, 77 tests): a campaign commit and the target
  edit the same Latin-1 line, the resolver rewrites the commit (a `!` pair) and the commit subject is
  non-UTF-8; `verify_resolution` returns a verified `ResolutionCheck` instead of raising. Sabotage:
  range-diff in text mode without `-s` fails it with `UnicodeDecodeError`. A campaign-level variant
  (full close with a Latin-1 commit) was tried and parks earlier, in the review step's own diff
  decoding, which is outside this spec (filed as a follow-up idea).
- Manual verification (a real conflicted close recording `+N extra, M absorbed`) has not been done yet:
  it needs the next real conflicted close.

### Test record (manual step, 2026-10-26)

- The manual verification step is executed as the automated end-to-end tests
  `tests/test_agentic_resolver.py::test_extra_commit_verified` and
  `::test_emptied_commit_verified` (patch_id, context and empty variants). Each runs a real git
  conflict in a tmp repo through the full close path with a FakeRunner-scripted resolver (an extra
  port commit; a campaign commit emptied by the rebase), asserts verification accepts and the close
  merges, and asserts the `rebase_resolved` event records `extra_commits` / `absorbed_commits`
  (and that `rebase_resolved_text` renders `+N extra, M absorbed`). Production observation will be
  logged by the conductor on the first real conflicted close after merge.

### As shipped (2026-10-26)

- The revert guard checks the new commit that range-diff pairs with an old campaign commit (`=`/`!`),
  not the old commit's own patch: a commit the resolution rewrote no longer reverse-applies to the
  resolved tree. Unpaired old commits (absorbed or not) are guarded with their own patch. The guard
  skips empty patches.
- `range-diff` is not run when either commit range is empty (git rejects it); the extra commits are the
  new commits no old commit pairs with, which is the same set as the `>` lines. Old and new shas from
  the `=`/`!` lines are resolved with `git rev-parse`.
- `_run`/`git` gained an `input` argument (stdin for `git apply`); `gitops.sequencer_in_progress` covers
  CHERRY_PICK_HEAD, MERGE_HEAD, REVERT_HEAD and `sequencer`, and `rebase_in_progress` shares its path
  helper. The runner commit is `queue._commit_resolver_changes`.
- Short shas in failure messages are 10 characters.
- Review round 1: the patches come from `git diff-tree -p --binary --full-index --no-color
  --no-ext-diff --no-textconv --src-prefix=a/ --dst-prefix=b/` (not porcelain `git diff`), and
  `git apply` runs with `apply.whitespace=nowarn`, so user or repo diff/apply config cannot change
  the equivalence checks. `_is_empty` compares the commit's tree with its parent's. A patch that
  fails to apply to its own parent raises `GitError` (a `verification error`) rather than counting
  as "does not apply", so the revert guard fails closed.
- Review round 2: patches are handled as bytes end to end (`_run`/`git` gained a `binary` mode;
  `_commit_patch` and the `git apply` call use it), so CRLF files and non-UTF-8 text neither fail
  `does not apply to its own parent` nor raise `UnicodeDecodeError`.
- Review round 3: round 2 covered the patches only. `git range-diff` also printed raw file content
  (the diff-of-diffs body of every `!` pair, i.e. every commit the resolver changed), and decoding it
  as strict UTF-8 raised `UnicodeDecodeError` (parked as `open_failed`). Now `range-diff` runs with
  `-s` (headers only, so body lines can no longer be mistaken for pairings either) and its output is
  read as bytes and decoded with `errors="replace"`; `_commit_entry` reads `git log --format=%s` the
  same way, so a non-UTF-8 commit subject cannot crash verification.

## Clock Log

## Rollout / migration

No data migration. Old `rebase_resolved` events lack the new keys and render as before.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
