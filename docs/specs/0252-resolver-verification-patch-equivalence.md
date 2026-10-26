---
id: SPEC-0252
title: "Conflict resolver verification by patch equivalence, runner commits resolver changes"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
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
  (equivalent patch), or is absorbed because the target already contains its change. Extra
  commits are allowed and recorded.
- G2. Changes the resolver leaves in the tree after a `resolved` verdict are committed by the
  runner, as is done for the implementer.
- G3. A dropped campaign commit whose change is not in the target still fails verification and
  restores `pre_head`.

**Non-goals**
- Limiting which files an extra commit may touch. The re-gate after the resolution (SPEC-0221)
  and the review of the merged result are the guard.
- Changing dispatch, attempt budgets, restore, or the close flow of SPEC-0220/0221.

## Decision

`verify_resolution` stops comparing commit counts. It accounts for each old campaign commit
`c` in `<fork>..<pre_head>` and fails only when some `c` is unaccounted for. A commit counts as
**present** when range-diff pairs it with a new commit (`=` for an identical patch-id, `!` for
a commit the resolution changed). A commit counts as **absorbed** when its change is already
upstream: either `git cherry <target_sha> <pre_head>` marks it `-` (a patch-id match in the
target), or its own diff reverse-applies cleanly to `HEAD`, meaning the change is already
there. New commits that pair with nothing (`>`) are allowed and returned as **extra**. After a
`resolved` verdict, and before verification, the runner commits any tracked changes left in
the worktree with a resolver commit message. The attached-HEAD, no-rebase-in-progress,
no-unmerged-paths, target-is-ancestor and tracked-clean checks stay.

## Design

- **`gitops.verify_resolution`** returns a `ResolutionCheck` dataclass
  (`failed: str | None`, `extra: list[dict]`, `absorbed: list[dict]`; each dict is
  `{"sha", "subject"}`) instead of `str | None`. The checks run in this order:
  1. `head_ref(worktree) == refs/heads/<branch>`.
  2. `not rebase_in_progress`.
  3. `ls-files -u` is empty.
  4. `merge-base --is-ancestor <target_sha> HEAD`.
  5. **Commit accounting (replaces the count check).** Parse
     `git range-diff --no-color --creation-factor=95 <fork>..<pre_head> <target_sha>..HEAD`
     with the existing `_RANGE_DIFF_RE`, extended to capture the old and new short shas. An
     old commit on an `=`/`!` line is present. For each old commit on a `<` line:
     (a) if `git cherry <target_sha> <pre_head>` lists it with `-`, it is absorbed;
     (b) else, if `git diff <c>^ <c> | git apply --check --reverse` succeeds against the
     `HEAD` tree (run with `--cached` on a temporary index read from `HEAD`, so the worktree is
     never touched), it is absorbed;
     (c) else the check fails with
     `campaign commit <short> (<subject>) is missing after the resolution`.
     Every `>` line goes into `extra`.
  6. `status_porcelain(worktree, untracked=False)` is empty → else `the worktree is dirty`.
- **`gitops.commit_tracked(worktree, message, env=None) -> str | None`** (new): runs
  `git add -u`, then commits if `git diff --cached --quiet` reports changes. The commit
  includes paths the resolver already `git add`ed, new files among them. Other untracked files
  are not committed; the existing post-verify `clean_untracked` removes them. It returns the
  new sha, or `None` when nothing was staged. It does not use `commit_all`, because
  `add -A` would commit `.orig` residue.
- **`queue._do_resolve`**: when the verdict is `resolved` and the run did not fail, and only if
  `git.head_ref(worktree) == f"refs/heads/{branch}"` and `not git.rebase_in_progress(worktree)`
  (otherwise verification fails as before), it calls `git.commit_tracked` with
  `wt(<campaign id>): conflict resolver run <run_id[:3]> — <first summary line[:72]>` plus the
  project's `agents.commit_trailer` (built the same way as the implementer message). Then it
  calls `verify_resolution`. `res.resolution` gains `extra`, `absorbed` and `commit`
  (the runner's sha or `None`). A failed verification restores `pre_head`, so the runner commit
  is discarded with the rest.
- **`queue._apply_resolve`** adds `extra_commits`, `absorbed_commits` and `resolver_commit`
  to the `rebase_resolved` event. `schemas.py` lists the three keys for `rebase_resolved`.
  `render.rebase_resolved_text` appends `; +N extra, M absorbed` when either is non-zero, and
  shows old events without the keys unchanged.
- **`personas.RESOLVER_INVARIANT`**: `commit` (never `--amend`) joins the allowed git list. A new
  line says that changes left in the tree after a `resolved` verdict are committed by the
  runner, and that additional commits are allowed when the target needs them.

## Alternatives considered

- **Patch-id only (`git cherry` both ways), no range-diff.** Rejected. A commit the resolution
  changed has a new patch-id, so every truly conflicted commit would look dropped. Range-diff
  pairing is still needed for `!` commits.
- **Let the resolver commit and leave the runner out.** Rejected as the only fix. A resolver
  that forgets to commit (the observed failure) would still be rejected. The runner commit is
  deterministic, and the resolver may still commit too.
- **Only allow extra commits that touch campaign or conflicted paths (IDEA-328's suggestion).**
  Deferred. A port commit can legitimately touch target-side files. The re-gate is the guard.

## Acceptance criteria

- [ ] `gitops.verify_resolution` no longer compares commit counts. It returns a
      `ResolutionCheck(failed, extra, absorbed)`, and the string
      `campaign commit count changed` no longer appears in `src/`.
- [ ] An old campaign commit is accepted when range-diff pairs it (`=`/`!`), when
      `git cherry <target_sha> <pre_head>` marks it `-`, or when its diff reverse-applies to
      `HEAD`. Any other missing commit fails with
      `campaign commit <short> (<subject>) is missing after the resolution`, and `pre_head` is
      restored.
- [ ] Unpaired new commits (`>`) do not fail verification and are returned in `extra`. The
      attached-HEAD, no-rebase, no-unmerged, target-ancestor and tracked-clean checks behave as
      before.
- [ ] After a `resolved` verdict with HEAD attached to the campaign branch and no rebase in
      progress, `queue._do_resolve` commits tracked and staged changes through the new
      `gitops.commit_tracked`, with the message
      `wt(<id>): conflict resolver run <run_id[:3]> — <summary>` (plus the project trailer),
      before verifying. Untracked, unstaged files are not committed.
- [ ] The `rebase_resolved` event carries `extra_commits`, `absorbed_commits` and
      `resolver_commit`, which `schemas.py` lists. `render.rebase_resolved_text` shows
      `+N extra, M absorbed` when non-zero, and shows old events unchanged.
- [ ] `personas.RESOLVER_INVARIANT` allows `commit` (never `--amend`) and says that the runner
      commits leftover changes after a `resolved` verdict.
- [ ] The Test plan's tests pass and `uv run pytest` is green.

## Test plan

- **Automated tests** (`tests/test_agentic_resolver.py`, real git in a tmp repo, FakeRunner
  `shell`):
  - `test_extra_commit_verified`: the resolver runs `git rebase --continue`, then commits an
    extra non-empty port commit. The result is verified, `rebase_resolved.extra_commits` names
    it, and the close merges. The existing `FAILS["extra_commit"]` case moves here as a passing
    case.
  - `test_emptied_commit_verified`: the target already contains one campaign commit's change
    (cherry-picked to the base), so the rebase drops it as empty. The result is verified and
    `absorbed_commits` names it. A second variant, where context differs so the patch-id does
    not match but the change reverse-applies to `HEAD`, is also verified.
  - `FAILS["dropped_commit"]` (`git rebase --skip` of a non-empty commit not in the target)
    still fails, now with `is missing after the resolution`. `FAILS["swapped_commit"]` still
    fails the same way.
  - `test_staged_changes_committed_by_runner`: the resolver finishes the rebase and leaves a
    `git add`ed file change plus an unstaged tracked edit and an untracked `.orig`. The
    result is verified, HEAD is a commit with the resolver message and the trailer containing
    both tracked changes, the `.orig` is neither committed nor left behind, and
    `rebase_resolved.resolver_commit` equals that sha. `FAILS["dirty_tree"]` becomes this
    passing case. A dirty tree remains a failure only when the commit cannot happen
    (HEAD detached or rebase running), and those cases are already in `FAILS`.
  - Direct unit tests of `verify_resolution` for each accounting branch (paired, cherry `-`,
    reverse-apply, missing), and of `commit_tracked` (returns `None` when nothing is staged).
  - `tests/test_agentic_personas.py`: the rendered invariant lists `commit` and the
    runner-commit sentence. `tests/test_render*.py` / events: the extended
    `rebase_resolved_text`, and an old event without the new keys renders unchanged.
- **Sabotage checks:** reinstating the count check fails `test_extra_commit_verified` and
  `test_emptied_commit_verified`. Dropping the reverse-apply branch fails the second emptied
  variant. Accepting every `<` line fails `[dropped_commit]`. Removing the runner commit fails
  `test_staged_changes_committed_by_runner`.
- **Manual verification:** on the next real conflicted close, check that a resolver run that
  adds or empties a commit records `rebase resolved: … +N extra, M absorbed` and merges.
- **Regression guard:** full `uv run pytest`. The remaining SPEC-0221 resolver cases
  (restore, budgets, interrupted, opt-out) pass unchanged.

## Clock Log

## Rollout / migration

No data migration. Old `rebase_resolved` events lack the new keys and render as before.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
