---
id: SPEC-0275
title: "wt queue gc: garbage-collect worktrees and branches of finished campaigns"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-070
tags: [agentic, git, worktrees, cli]
---

## Context

Promoted from idea `IDEA-070` (Worktree and branch garbage collection for finished
campaigns).

What close/abandon clean up today (verified on main):

- `close._cleanup` (`src/wt/agentic/close.py`) runs on a successful close. With
  `worktrees.cleanup: on_merge` (wt's `.wt/project.yaml`; default in `project.py`) it removes
  the campaign worktree only for `mode == "merge"`; with `on_close` also for PR hand-offs.
  It deletes the branch only for a merged *spec* campaign with `merge.delete_branch`. For a
  SPEC-0233 merge request it removes the worktree only when wt created it (`own_worktree`)
  and **never deletes the requester's branch**. Any `GitError` is recorded as a minor
  `cleanup` finding and the leftovers stay.
- `queue.apply_abandon` removes the worktree (unless `--keep-worktree` or an adopted
  merge-request worktree) and deliberately **keeps the branch**.

What accumulates as a result: branches of ABANDONED campaigns; requester branches (and
their clean worktrees) of MERGED merge requests — e.g. the `/home/bruno/work/wt-bugspecs-*`,
`wt-specs-*` sub-agent worktrees whose branches were merged through `wt queue merge`;
HANDED_OFF worktrees under `on_merge`; leftovers of failed cleanups; and directories under
`<repo>/../.wt-worktrees/<Project>/` (`project.worktree_root`) owned by no live campaign.
On 2026-10-27 the wt repo had 31 worktrees and 32 local branches, 30 of them merged into
main. There is no command that lists or removes these.

## Goals / Non-goals

**Goals**
- `wt queue gc` lists, per project repo, the worktrees and local branches that belong to
  terminal campaigns (MERGED / HANDED_OFF / ABANDONED) or to merged merge requests, with the
  action it would take and why; dry-run by default, `--yes` applies.
- It never removes a dirty worktree, a worktree or branch of a non-terminal campaign, the
  main checkout, the caller's cwd, or an unmerged branch.

**Non-goals**
- Deleting unmerged branches of ABANDONED campaigns (abandon keeps them on purpose; they are
  listed as `keep: unmerged`).
- Remote branch deletion or PR-branch cleanup for HANDED_OFF campaigns.
- Worktrees owned by no campaign (human/steward scratch worktrees) — listed for information
  only, never removed by this spec.
- A dashboard count (follow-up if wanted).

## Decision

New `wt queue gc [--project P] [--yes] [--json]` backed by `queue.plan_gc(cfg, project=None)`
and `queue.apply_gc(cfg, plan)`, following the plan/apply pattern of the other queue
commands. For each repo root referenced by any campaign (`store.list_campaigns(...,
include_terminal=True)`), git's own `worktree list --porcelain` is the source of truth.
Candidate rules:

1. **Campaign worktree** wt created (path equals a campaign's `repo.worktree`, not an adopted
   merge-request worktree): remove iff the campaign is terminal and
   `gitops.is_clean(path, untracked=True)`. Dirty → `keep: dirty`.
2. **Merge-request worktree** (adopted, `own_worktree` false) of a MERGED request: remove iff
   clean and its HEAD is an ancestor of the base (`merge-base --is-ancestor HEAD base`).
3. **Branch** of a terminal campaign or a MERGED merge request: delete (`branch -d`
   semantics, never `-D`) iff no remaining worktree has it checked out after step 1–2 and
   its tip is an ancestor of the base. Unmerged → `keep: unmerged`.
4. **Orphan dir** under `project.worktree_root(...)` that git does not list as a worktree
   and no campaign references: reported as `keep: orphan dir` and never removed (git no
   longer knows it, so cleanliness cannot be checked); the human deletes it. `git worktree
   prune` runs once per repo on `--yes`.
5. **Always kept:** the repo's main worktree, `os.getcwd()`'s worktree, anything belonging to
   a non-terminal campaign, and any worktree with a rebase/merge in progress.

Each row: `{kind: worktree|branch|orphan, path|branch, campaign, campaign_state, action:
remove|delete|keep, why}`. `--yes` applies rows in order (worktrees before branches); a
`GitError` on one row is reported on that row and does not stop the rest. No ledger events
(terminal campaigns' ledgers are closed); the output is the record.

## Design

- `src/wt/agentic/gitops.py`: `worktree_list(repo) -> list[dict]` (parse
  `git worktree list --porcelain`: path, head, branch, `bare`/`detached`/`locked`/`prunable`)
  and `is_ancestor(repo, rev, base) -> bool` (`merge-base --is-ancestor`, check=False).
  Existing `worktree_remove` (force=False here — git itself then refuses a dirty tree as a
  second guard), `branch_delete(force=False)`, `is_clean`, `rebase_in_progress` are reused.
- `src/wt/agentic/queue.py`: `plan_gc`, `apply_gc`, helper `_gc_rows(cfg, root, campaigns,
  base)`. Base per campaign: `repo.base` or the project's `merge.base`/`base`.
- `src/wt/agentic/cli_queue.py`: `@queue_group.command("gc")`, `--yes`, `--json` (schema
  `wt.queue.gc.v1`), `--project` filter; prints a table (kind, target, campaign, action, why)
  and a summary line `N worktrees, M branches removable (dry-run; --yes to apply)`.
- Docs: one line under the queue commands in `docs/` CLI reference where `wt queue abandon`
  is documented.

## Alternatives considered

- **Do it inside close/abandon** — close already tries; leftovers come from failed cleanups,
  deliberate keeps and requester worktrees that close must not touch at merge time
  (the requester may still be in it). A deferred, explicit sweep is the safe place.
- **`wt campaign gc`** (idea's name) — the queue group owns the other lifecycle write
  commands (`park/resume/abandon`), so `wt queue gc` is consistent.
- **Also remove unowned clean merged worktrees** — a freshly created sub-agent worktree sits
  exactly at main's tip and would look "merged"; rejected without an ownership signal.

## Acceptance criteria

- [x] `wt queue gc` (no `--yes`) prints the plan and changes nothing (worktree list and
      branch list identical before/after).
- [x] `--yes` removes the clean worktree and deletes the merged branch of a MERGED campaign
      left behind by a failed cleanup, the merged branch of an ABANDONED campaign, and the
      clean adopted worktree + merged branch of a MERGED merge request.
- [x] A dirty worktree of a terminal campaign is `keep: dirty`; an unmerged ABANDONED branch
      is `keep: unmerged`; both survive `--yes`.
- [x] Worktrees and branches of non-terminal campaigns, the main checkout and the cwd
      worktree never appear as remove/delete rows.
- [x] A `GitError` on one row is reported on that row and the remaining rows still apply.
- [x] `--json` emits `wt.queue.gc.v1` with the rows and `written`.

## Test plan

- **Automated tests** (`tests/test_agentic_queue_gc.py`, new; real git repos in `tmp_path`,
  campaigns written via `store.create_campaign` with hand-set states, as in
  `tests/test_agentic_close.py`):
  - `test_dry_run_changes_nothing` — fixture with every candidate kind; snapshot
    `git worktree list` / `git branch` before and after `queue gc`.
  - `test_yes_removes_merged_and_abandoned_leftovers` — MERGED campaign worktree + merged
    branch, ABANDONED merged branch, MERGED merge request with adopted clean worktree →
    all gone after `--yes`.
  - `test_dirty_and_unmerged_kept` — untracked file in a terminal worktree; ABANDONED branch
    with an extra commit → both reported `keep` with `dirty` / `unmerged` and still present.
  - `test_live_campaign_and_main_never_touched` — an IMPLEMENTING campaign's worktree +
    branch, the main checkout and `monkeypatch.chdir` into a terminal campaign's worktree →
    none are remove/delete rows.
  - `test_row_error_does_not_stop_others` — monkeypatch `gitops.branch_delete` to raise
    `GitError` for one branch; the other rows still apply and the failing row carries the
    error.
  - `test_json_schema` — `--json` has `schema == "wt.queue.gc.v1"`, `rows`, `written`.
- **Sabotage check:** drop the `is_clean` guard in `_gc_rows` → `test_dirty_and_unmerged_kept`
  fails (git's own refusal then surfaces as a row error, not `keep: dirty`); replace the
  ancestor check with `True` → the unmerged-branch assertion fails. Restore.
- **Manual verification:** on the live wt repo, `uv run wt queue gc` (dry-run only) lists the
  merged `wt-bugspecs-*`/`specs-*` requester worktrees and branches; compare with
  `git branch --merged main`. Do not `--yes` on the live repo during verification.
- **Regression guard:** full `uv run pytest`; close/abandon tests unchanged.

### Test record (2026-10-27)

- `uv run pytest -q tests/test_agentic_queue_gc.py` — 11 passed: the six planned tests plus
  `test_main_checkout_of_terminal_merge_request_kept`, `test_adopted_requester_worktrees_kept`
  (MERGED request with a post-merge commit → `unmerged`; ABANDONED request →
  `requester's worktree (ABANDONED merge request)`; MERGED request whose requester ran
  `git checkout -b specs-5 main` → `requester moved on (on specs-5)`),
  `test_rebase_in_progress_kept_with_its_branch` (stopped `edit` rebase, clean tree),
  `test_prunable_locked_and_detached` (hand-deleted dir → pruned; locked → kept; detached HEAD
  with unreachable commits → kept; a live campaign's `repo.base` → `base branch`) and
  `test_yes_exits_nonzero_on_row_failure`.
- **Sabotage check:** dropping the `is_clean` guard in `_gc_worktree_keep` fails
  `test_dirty_and_unmerged_kept` (plus dry-run/yes/json: git's refusal surfaces as a row
  error); replacing the branch `is_ancestor` check in `_gc_rows` with `True` fails
  `test_dirty_and_unmerged_kept` on the unmerged assertion. Disabling the main-checkout,
  requester-moved-on and rebase-in-progress guards each fails its dedicated test. All
  restored (file byte-identical to before).
- **Manual verification** (dry-run only, no `--yes`): `uv run wt queue gc --json` on the live
  wt repo (`/home/bruno/work/wt`, 37 worktrees, 38 branches, 33 merged into main) planned
  11 worktree removals (the merged requester worktrees `wt-bugspecs-b2`, `wt-specs-c3..f6`,
  `wt-spec-357`, `wt-fu-specs`, `wt-fix-tmphome`, `wt-gate-order`, `wt-resolver-fix-spec`,
  `wt-conduct-drain`) and 11 branch deletions; every delete row is in
  `git branch --merged main`. `wt-bugspecs-0341` was `keep: dirty` and its branch
  `keep: checked out in …`; 17 worktrees no campaign references were `keep: no campaign`.
  Nothing was written, and there were no errors.
- Full gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py`,
  `uv run pytest -q -x`.

### Test record (2026-10-27, review round 2)

- `uv run pytest -q tests/test_agentic_queue_gc.py`: 14 passed. `test_row_error_does_not_stop_others`
  now fails the *first* delete row of the plan, so two branch rows provably come after it, and it
  checks that both worktree rows were applied. `test_worktree_row_error_does_not_stop_others` makes
  `worktree_remove` raise on the first remove row; that row has `done is False` and `error`, the
  other worktree is removed, and the branch rows still apply (the branch the failed worktree still
  holds is refused by git on its own row). `test_requester_synced_past_the_merge_kept` covers a
  MERGED adopted request whose requester ran `git merge --ff-only main` after main moved (worktree
  and branch both `keep`), and a request branch force-moved to main with no worktree
  (`requester moved on (tip past the merge)`). `test_prune_failure_marks_missing_rows` covers a
  failed `git worktree prune` marking `missing on disk` rows `done: false`.
- **Sabotage check:** disabling the new worktree `merged_sha` guard, or the new branch guard,
  fails `test_requester_synced_past_the_merge_kept`. Adding a `break` after the first row error in
  `apply_gc` fails both row-error tests. All restored (file byte-identical).
- Full gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py`,
  `uv run pytest -q -x`.

### As shipped

- Code: `gitops.worktree_list` / `gitops.is_ancestor`; `queue._gc_rows`,
  `_gc_worktree_keep`, `plan_gc`, `apply_gc`; `wt queue gc` in `cli_queue.py`; one line in
  `docs/product/loop/operate.md`.
- Keep reasons beyond the Decision: `main checkout`, `current directory`, `locked`,
  `unreadable (<git error>)`, `requester's worktree (<state> merge request)` (an adopted
  worktree of a request that is not MERGED), `requester moved on (on <branch>)` (an adopted
  worktree whose checked-out branch is no longer the request's `merge.branch`; it is never
  removed), `detached, unreachable commits` (a detached terminal worktree whose HEAD neither
  base nor the campaign branch reaches), `no campaign` (info row for unowned worktrees,
  noting when `git worktree prune` will drop a prunable one), and for branches
  `base branch` (the campaign's base or any live campaign's `repo.base`) and
  `checked out in <path>`. A kept detached worktree (e.g. mid-rebase) holds its campaign's
  branch, so that branch is kept too.
- Prunable entries (directory deleted by hand) of terminal campaigns are `remove: missing on
  disk (git worktree prune)`. They are removed by the single `git worktree prune` that `--yes`
  runs per repo before the row-by-row removals. That prune drops every prunable, unlocked
  entry in the repo.
- Rows also carry `repo` and `project`. After `--yes` they carry `done` (bool) and, on
  failure, `error`. The plan (and `wt.queue.gc.v1` JSON) also has `project`, `repos`,
  `errors` (missing repo roots, unreadable worktree roots, prune failures),
  `worktrees_removable` and `branches_removable`.
- `--yes` exits 1 when any row failed or a repo error was recorded. The output still lists
  every row.
- A MERGED merge request's adopted worktree is removed only when its HEAD is an ancestor of the
  recorded `close.merged_sha` (where the merge left it). Otherwise it is
  `keep: requester moved on (HEAD past the merge)`, for example after the requester
  fast-forwarded the same branch to a newer base. The request's branch gets the same test
  (`keep: requester moved on (tip past the merge)`). A MERGED request with no recorded
  `merged_sha` is kept the same way.
- When a repo's `git worktree prune` fails, its `missing on disk` rows get `done: false` and
  `error: git worktree prune failed`. The docs line notes that a removed worktree takes its
  ignored files with it.

### Test record (2026-10-27, review round 3)

- `uv run pytest -q tests/test_agentic_queue_gc.py`: 20 passed. New:
  `test_handed_off_worktree_removed_branch_kept` (HANDED_OFF clean worktree `remove`, its
  unmerged branch `keep: unmerged`, both true after `--yes`, exit 0);
  `test_own_worktree_merge_request_follows_campaign_path` (merge requests with
  `own_worktree: true`: a MERGED one whose HEAD is past the recorded `merged_sha` and an
  ABANDONED one are both `remove: <state> campaign, clean` and are removed);
  `test_prunable_dir_still_on_disk_kept` (a worktree with its `.git` deleted by hand is kept,
  and its files survive `--yes`, exit 0); `test_missing_repo_root_is_a_plan_error`,
  `test_unreadable_worktree_root_is_a_plan_error` and `test_git_error_listing_a_repo_is_a_plan_error`
  (each error is in `errors`; dry-run exits 0 in text and JSON mode; `--yes` exits 1 in text and
  JSON mode on the repo error alone, with no failed row).
- **Sabotage check:** dropping the new `path.exists()` branch fails
  `test_prunable_dir_still_on_disk_kept`. Treating every merge request as adopted fails
  `test_own_worktree_merge_request_follows_campaign_path`. Both restored (file byte-identical).
- Full gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py`,
  `uv run pytest -q -x`.

### As shipped (review round 3)

- A prunable entry whose directory still exists (git also marks a worktree prunable when its
  `.git` file is gone or broken) is `keep: no .git (git worktree prune unregisters it;
  directory stays)`. Its files cannot be checked, so gc never removes the directory. The
  `--yes` prune still unregisters the entry, and the directory is then an orphan dir.

### Test record (2026-10-27, review round 4)

- `uv run pytest -q tests/test_agentic_queue_gc.py`: 32 passed. New:
  `test_cwd_in_a_subdirectory_of_a_terminal_worktree_kept` (cwd two levels inside a clean MERGED
  worktree → `keep: current directory`, its branch kept);
  `test_project_filter_does_not_prune_other_entries` (`--project demo --yes` unregisters demo's
  hand-deleted worktree only; another project's and an unowned prunable entry stay registered;
  the unfiltered run then prunes both); `test_project_base_decides_when_campaign_records_none`
  (no `repo.base`, `.wt/project.yaml` `base: trunk` and then `merge.base: trunk`: a branch
  merged into trunk only is deleted, one merged into main only is `unmerged`);
  `test_bad_project_config_is_a_repo_error` (invalid YAML, non-UTF-8, invalid value) and
  `test_unreadable_project_config_is_a_repo_error` (PermissionError) are per-repo plan errors,
  no traceback; `test_branch_and_path_shared_with_a_live_campaign_untouched`;
  `test_orphan_dir_referenced_by_terminal_or_live_campaign`; `test_second_yes_run_is_a_no_op`;
  `test_hidden_edits_kept` (skip-worktree and assume-unchanged edits kept; a flagged unmodified
  file still removed); `test_bisect_in_progress_kept`. `test_prune_failure_marks_missing_rows`
  became `test_prune_failure_is_a_repo_error`.
- **Sabotage check:** comparing only `path == cwd`, dropping the hidden-edits guard, dropping the
  bisect guard, pruning under `--project` (either the repo-wide prune or `worktree_remove`'s own),
  dropping the branch or orphan `live()` skips, hard-coding the base to `main`, and not catching
  `OSError` from the config read each fail the matching new test. All restored (files
  byte-identical).
- Full gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py`,
  `uv run pytest -q -x`.

### As shipped (review round 4)

- `apply_gc` order is now: worktree rows (each its own `git worktree remove` without --force and
  without the follow-up repo-wide prune; git also unregisters a directory already gone), then
  `git worktree prune` once per repo, then branch rows. A `missing on disk` row reads
  `remove: missing on disk (unregister only)` and no longer depends on the prune, so a failed
  prune is a repo error only.
- With `--project`, the repo-wide `git worktree prune` is skipped, since it would unregister
  other projects' and unowned entries. Rows are then planned without assuming it: `no campaign`
  rows carry no prune note, a branch held by an unowned prunable entry stays `checked out in …`,
  and a `.git`-less dir is `keep: no .git (directory stays)`.
- New keep reasons: `bisect in progress`, and `dirty (skip-worktree/assume-unchanged edits)`
  (`gitops.hidden_changes`: a flagged tracked file whose content on disk differs from the index).
- A `.wt/project.yaml` that exists but is unreadable, not UTF-8 or invalid is a per-repo plan
  error and the repo is skipped (its base is unknown). A missing one still means base `main`.
- Rule 4 orphan rows also cover a directory git does not list that only a terminal campaign
  references (`keep: orphan dir of <state> campaign: …`, naming the campaign). A directory that a
  live campaign references produces no row.

## Rollout / migration

None. Operators run `wt queue gc` then `--yes` when they want; the conductor may run it in
quiet periods (its skill can add that later).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
