---
id: SPEC-0275
title: "wt queue gc: garbage-collect worktrees and branches of finished campaigns"
status: in-progress
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

- [ ] `wt queue gc` (no `--yes`) prints the plan and changes nothing (worktree list and
      branch list identical before/after).
- [ ] `--yes` removes the clean worktree and deletes the merged branch of a MERGED campaign
      left behind by a failed cleanup, the merged branch of an ABANDONED campaign, and the
      clean adopted worktree + merged branch of a MERGED merge request.
- [ ] A dirty worktree of a terminal campaign is `keep: dirty`; an unmerged ABANDONED branch
      is `keep: unmerged`; both survive `--yes`.
- [ ] Worktrees and branches of non-terminal campaigns, the main checkout and the cwd
      worktree never appear as remove/delete rows.
- [ ] A `GitError` on one row is reported on that row and the remaining rows still apply.
- [ ] `--json` emits `wt.queue.gc.v1` with the rows and `written`.

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

## Rollout / migration

None. Operators run `wt queue gc` then `--yes` when they want; the conductor may run it in
quiet periods (its skill can add that later).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
