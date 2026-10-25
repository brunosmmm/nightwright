---
id: SPEC-0233
title: "Queue merge requests: `wt queue merge` runs a branch merge as a campaign"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, close, merge, conductor, steward]
source_idea: IDEA-080
depends_on: [SPEC-0176, SPEC-0175, SPEC-0220]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-080` ("Queue merge requests: branch merges from stewards and the conductor run as
queue work items"). It also covers the core of `IDEA-068` (integrate branches built outside
the queue).

Only specs can enter the queue today. `queue.plan_add` / `apply_add`
([SPEC-0176](./0176-queue-state-machine-with-lease-guarded-tick-run.md)) create a
`kind: "implement"` campaign that runs OPENING → IMPLEMENTING → GATING → … → CLOSING. Branches
built by a steward or the human lane (spec edits, hand fixes, a sub-agent's worktree) are merged
by hand or by a background shell job that rebases, runs the gates and fast-forwards `main`.
Those jobs are invisible to the queue. On 2026-10-25 several of them stalled for 30+ minutes
behind a campaign's CLOSING hold, and no event or alert fired. The human: "this is why it is
important to be able to queue everything".

The close procedure ([SPEC-0175](./0175-close-step-rebase-re-gate-ff-only-merge-or-pr.md),
split into a worktree phase `prepare_close` and a main-thread phase `finish_close` by
[SPEC-0220](./0220-close-regate-off-main-thread.md)) already does what a merge needs: rebase
onto the base, regenerate generated files, re-run every gate on the rebased tree, check the main
checkout and ff-merge. Running merge requests through it as campaigns serializes them with
campaign closes, gives them a slot, and lets them emit the same ledger events and conductor
events.

## Goals / Non-goals

**Goals**
- G1. `wt queue merge BRANCH` creates a `kind: "merge"` campaign in QUEUED (dry run without
  `--yes`), with the same admission, slots, priority and lease as spec campaigns.
- G2. A merge campaign runs QUEUED → OPENING → CLOSING → MERGED. The close is the normal one
  (rebase, regenerate, full re-gate, main-checkout checks, ff-only merge) minus the spec steps.
  No persona runs.
- G3. Every failure parks with a reason the conductor can act on. A merge campaign never commits
  the requester's uncommitted work, never removes a worktree it did not create, and never
  deletes the requester's branch.
- G4. Merge campaigns are visible: `wt queue list`, `wt queue watch`, `wt campaign show/log`
  and the conductor event stream show them as `merge:<branch>`.

**Non-goals**
- `--docs-only` merges and the skill updates (`wt-conduct`, `wt-steward`): that is
  [SPEC-0234](./0234-docs-only-merge-requests-and-conductor-skill-routing.md).
- A REVIEWING phase for merge requests. The requester owns the content; the gates are the check.
- Merging under `merge.policy: pr | draft-pr`. `wt queue merge` refuses those projects.
- A conflict-resolver persona of its own. If [SPEC-0221](./0221-rebase-conflict-resolver-persona.md)
  has landed, its dispatch in the close rebase applies to merge campaigns unchanged. Otherwise a
  conflict parks `rebase_conflict` as today.
- Remote branches. The branch must exist locally in the project's repo.

## Decision

Add `queue.plan_merge` / `apply_merge` and `wt queue merge`. A merge campaign stores
`kind: "merge"`, `spec_id: null`, `spec: null` and a `merge` block. It reuses the existing
worktree that has the branch checked out, or creates one for the branch. The open step for
`kind: "merge"` goes straight to CLOSING. The close skips the spec guard and the spec DoD,
parks `worktree_dirty` instead of committing a snapshot, and maps a red re-gate to a new
`gates_failed` park instead of TRIAGING. Cleanup removes only a worktree wt created and never
deletes the branch. Renderers use one helper, `queue.campaign_label`.

## Design

### Campaign state (`wt.campaign.v1`, additive)

`apply_merge` writes the same shape as `apply_add` with these differences:
- `id`: `store.new_campaign_id(cfg, "MERGE")`, for example `MERGE-20261025-a3f9`.
- `kind: "merge"`, `spec_id: null`, `spec: null`, `force: false`.
- `repo.branch`: the requested branch. `repo.worktree`: the adopted or planned worktree.
  `repo.base`: the project's merge base, as in `plan_add`.
- `merge: {branch, requested_by, note, head_at_request, own_worktree, docs_only}`.
  `requested_by` is the `--by` value (default `human`; the conductor passes `conductor`, a
  steward `steward:<Project>`). `own_worktree` is true when wt creates the worktree.
  `docs_only` is always `false` here (SPEC-0234 sets it).
- `profile`: `resolve_profile(cfg, profile)["name"]`, as in `plan_add`. It only sizes the cap and
  the slots. `cost.cap_usd` is copied as usual.
- `priority`: default `MERGE_PRIORITY_DEFAULT = 2`, ahead of the spec default 3. Merges are
  short and usually unblock a person.

`store.create_campaign` adds the extras `kind` and `merge_branch` to the `open` event when
`state["kind"] == "merge"`. `schemas.EVENT_KEYS["open"]` is unchanged: `spec_id` is present
with value null.

### Planning (`queue.plan_merge`)

`plan_merge(cfg, branch, *, project=None, worktree=None, profile=None, priority=MERGE_PRIORITY_DEFAULT,
note=None, requested_by="human") -> dict` (no writes). It raises ValueError naming the fix
when any check fails:
1. `project` defaults to `P.find_project_for_cwd(cfg)`, else `Meta-Tools`.
   `pcfg = P.project_config(cfg, project)`. The checks below use `root = pcfg["_root"]` and
   `base` from `pcfg.merge.base` or `pcfg.base`.
2. `pcfg.merge.policy` must be `merge`.
3. `refs/heads/<branch>` exists in `root`; `branch != base`; the main checkout `root` is not on
   `branch` (`git.current_branch(root)`).
4. The branch is ahead of base: `git rev-list --count <base>..<branch>` in `root` (via
   `gitops.git`) is ≥ 1, otherwise "nothing to merge". The count is the plan's `ahead`.
5. No non-terminal campaign has `repo.branch == branch` in this project. That covers a second
   merge request for the same branch and a spec campaign's own `wt/<cid>` branch.
6. Worktree resolution. A new helper `gitops.worktree_for_branch(repo, branch) -> Path | None`
   parses `git worktree list --porcelain`.
   - `--worktree PATH` given: it must equal `worktree_for_branch` (resolved paths).
   - Not given: the branch's existing worktree is adopted (`own_worktree: false`). If there is
     none, the plan names `P.worktree_root(pcfg, root, project) / <cid>` (`own_worktree: true`).
   - An adopted worktree must be clean, untracked files included:
     `git.is_clean(path, untracked=True)`. Ignored files (`.venv`, caches) do not count.
7. `head_at_request = git.rev_parse(root, branch)` is recorded (it never raises).

The plan carries `campaign, kind: "merge", branch, project, profile, priority, base, repo_root,
worktree, own_worktree, head_at_request, ahead, requested_by, note, cap_usd, slots,
merge_policy`. `apply_merge(cfg, plan, *, by="human")` re-runs check 5 against live state and
then calls `store.create_campaign`.

### CLI (`cli_queue.py`)

`wt queue merge BRANCH [--project P] [--worktree PATH] [--profile NAME] [--priority 1..5]
[--note TEXT] [--by WHO] [--yes] [--json]`. Without `--yes` it is a dry run, like `wt queue add`.
`--json` emits `{"schema": "wt.queue.merge.v1", **plan, "written": bool}`. The text output
reuses `_print_plan` and then prints `✓ <cid>  QUEUED`.

### Loop (`queue.py`)

- `backlog_readiness` for `kind == "merge"` returns `(True, [])` when `refs/heads/<branch>`
  still exists in `repo.root`, and `(False, ["branch <b> missing"])` otherwise. It reads no spec.
- `_do_open` dispatches `kind == "merge"` to `_do_open_merge(ctx, state)`:
  - `own_worktree`: a new `gitops.worktree_add_existing(repo, path, branch)` runs
    `git worktree add <path> <branch>` and errors if `path` exists. Then the project's
    `worktrees.setup` commands run, as for spec campaigns.
  - Adopted: the worktree must still exist, be on `branch` and be clean (untracked files
    included, as in check 6).
    Setup commands are not run: wt does not change the requester's environment.
  - Both record `repo.base_sha_at_open` and `repo.head`. A failure is a GitError/ValueError
    and takes the existing path: park `open_failed`, `resume_to: QUEUED`.
- `_apply`, `kind == "open"`: `finish("CLOSING" if state.get("kind") == "merge" else
  "IMPLEMENTING", "opened")`. `_enter_slot_or_wait` and the WAITING logic need no change:
  the campaign already holds its slot from OPENING.
- `plan_step` for a merge campaign in IMPLEMENTING, GATING, TRIAGING or REVIEWING (unreachable)
  returns `Step("park", reason="human", detail="merge campaign in <state>",
  resume_to="CLOSING")`.
- `plan_resume`: for a merge campaign, `to` must be `CLOSING` or `QUEUED`. Anything else raises
  ValueError.

### Close (SPEC-0220 `prepare_close` / `finish_close`)

For `state["kind"] == "merge"`:
- `_do_close` skips `FU.file_followups` (a merge campaign has no `last_review`, so it would
  file nothing anyway; skipping it makes that explicit).
- Step 0 (`spec_guard`) and step 1 (`spec_dod`) are skipped.
- Step 2: a dirty worktree (`git.is_clean(worktree, untracked=True)` false, so untracked
  non-ignored files count too) returns a park `worktree_dirty` with the first 10
  `status_porcelain(worktree, untracked=True)` lines and `resume_to: CLOSING`. There is no
  close-time snapshot commit. Because untracked files park here, the rebase and the re-gate
  never run on a tree that holds files outside the branch, and an untracked file can never
  collide with a path the rebase brings in.
- **Branch tip.** The close merges the branch tip at close time, not `head_at_request`. The
  requester queues a branch, not a commit, and is the only one who commits to it. A steward
  that wants to stop a merge parks the campaign (`wt queue park`). The `close` event gains the
  extras `head_at_request` and `commits_since_request` (`git rev-list --count
  <head_at_request>..<pre-rebase tip>`, or null when `head_at_request` is no longer an
  ancestor), so a moved branch is visible after the fact.
- **The requester's branch is rewritten in place.** The rebase runs in `repo.worktree`, which
  for an adopted worktree is the requester's own checkout. After a successful rebase the
  branch history is rewritten (new shas onto the new base), even when the re-gate then fails
  (`gates_failed` leaves the branch rebased). `gitops.rebase` aborts a conflicting rebase, so
  a `rebase_conflict` park leaves the branch at its pre-rebase tip with no rebase in progress.
  The park detail of `gates_failed` says `branch rebased onto <base> <sha10>`, so the
  conductor can tell the requester.
- Steps 3–6 (target, rebase with `merge.regenerate`, re-gate, main-checkout checks, ff-merge,
  `post_cmds`, `push_after_merge`) are unchanged. The re-gate is the merge request's only gate
  run. There is no separate GATING phase.
- A red re-gate still returns `next_state == "TRIAGING"`. In `_apply`'s close branch, a merge
  campaign maps it to park `gates_failed`, `resume_to: CLOSING`. The detail is `gates failed:
  <name>, …` followed by the last 20 lines of each failing gate's tail. `last_gate` is
  recorded as today.
- `_cleanup`: for a merge campaign, the worktree is removed only when `merge.own_worktree` is
  true, following `worktrees.cleanup`. The branch is never deleted, whatever
  `merge.delete_branch` says: it belongs to the requester.
- `plan_abandon` / `apply_abandon` (`wt queue abandon`): for a merge campaign with
  `merge.own_worktree == false`, the plan's `remove_worktree` is always `false`, whatever
  `--keep-worktree` says, so `apply_abandon` never runs `worktree_remove` on the requester's
  worktree. No abandon path deletes a branch (true today, and kept). A created worktree
  (`own_worktree: true`) is removed as for spec campaigns.
- `PARK_REASONS` gains `gates_failed` and `worktree_dirty`. If
  [SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md)'s park-owner map exists,
  both map to `conductor`, which relays them to the requester.

### Visibility

- `queue.campaign_label(state) -> str` returns `spec_id`, or `merge:<merge.branch>` when
  `spec_id` is null. Every place that prints a campaign's spec id uses it:
  - `cli_queue._queue_table`, `_waiting_table`, `_backlog_table`;
  - the dashboard panels and tables;
  - `cli_campaign` `list` / `show` (today `str(r["spec_id"])` at `cli_campaign.py:132`);
  - `cli_conduct.py`: the `parked` / `active` items (built from `i["spec_id"]`) gain `label`,
    and its parked table prints `label`;
  - the event and digest rows in `events.py` that carry `spec_id`, which gain `label`.
- `_spec_title` returns the request's `note` (or `merge request`) for a merge campaign.
- `queue_payload` and `dashboard_payload` items gain `kind` and `merge_branch` (null for
  spec campaigns). This is additive under `wt.queue.v1` / `wt.queue.dashboard.v1`.
- `wt campaign show` adds a line `merge <branch> (requested by <who>; head at request
  <sha10>; worktree adopted|created)`.

## Alternatives considered

- **OPENING → GATING → CLOSING** (the idea's wording): rejected. The close re-runs every gate
  on the rebased tree, which is the exact tree that merges, so a pre-rebase gate run only
  duplicates it (a full pytest run for nothing). A red gate surfaces in the close as
  `gates_failed` either way.
- **Committing a dirty worktree like the spec close does**: rejected. A requester's worktree
  may hold unfinished work. Committing it would merge work nobody asked to merge.
- **A pseudo spec id (`MERGE:<branch>`) in `spec_id`**: rejected. Spec-id lookups
  (`active_campaign_for_spec`, `backlog_readiness`, completions) would treat it as a spec.
  A null `spec_id` with one label helper keeps those paths honest.
- **Merge only `head_at_request`, park `branch_moved` otherwise**: rejected. The requester owns
  the branch and is the only committer. Parking on every follow-up commit would add a
  conductor round trip for the common case ("one more fix before it lands"). The close event
  records the drift instead.
- **A gate-triager loop for red gates**: rejected. The requester, not a persona, owns the
  content. The conductor relays the failure.

## Acceptance criteria

- [ ] `plan_merge` enforces checks 2–6 with a ValueError naming the fix for each: the policy is
      not `merge`, the branch is missing or equals base, the main checkout is on the branch,
      the branch is not ahead of base, a non-terminal campaign already has the branch, a
      `--worktree` mismatch, or an adopted worktree with tracked or untracked changes. It adopts the branch's existing
      worktree or plans `worktree_root/<cid>`.
- [ ] `wt queue merge BRANCH` is a dry run without `--yes`. With `--yes` it creates a QUEUED
      campaign whose state has `kind: "merge"`, `spec_id: null`, `spec: null`, the `merge`
      block and priority 2. Its `open` event carries `kind` and `merge_branch` and passes
      `schemas.ledger_problems`. `--json` emits `wt.queue.merge.v1`.
- [ ] `backlog_readiness` admits a merge campaign without reading a spec and blocks it with
      `branch <b> missing` when the branch is gone. `plan_resume` on a merge campaign accepts
      only `CLOSING` / `QUEUED`, and `plan_step` parks a merge campaign found in
      IMPLEMENTING/GATING/TRIAGING/REVIEWING.
- [ ] The open step adopts or creates the worktree (setup commands only for a created one) and
      transitions OPENING → CLOSING. No `run.start` event is ever written for a merge campaign.
- [ ] Close on a merge campaign skips the spec guard and the DoD, rebases, re-gates and
      ff-merges the branch tip to MERGED with a `close` event carrying `head_at_request` and
      `commits_since_request` (1 in a test that commits after queueing). A worktree with
      tracked or untracked changes parks `worktree_dirty` without any commit. A red re-gate
      parks `gates_failed` (`resume_to: CLOSING`) with the failing gate names and the
      rebased-branch note.
- [ ] After MERGED, a created worktree is removed per `worktrees.cleanup` and an adopted one is
      left in place. The branch is never deleted.
- [ ] `wt queue abandon` of a PARKED merge campaign with an adopted worktree leaves the worktree
      (with its uncommitted and untracked files) and the branch in place, with or without
      `--keep-worktree`. The plan shows `remove_worktree: false`.
- [ ] `wt queue list` (table and `--json`), `wt queue watch`, `wt campaign list/show` and
      `wt queue events` and the `cli_conduct` status tables show the campaign as
      `merge:<branch>`. No output prints `None` for
      its spec id, and payload items carry `kind` / `merge_branch`.
- [ ] A merge campaign and a spec campaign that are both ready for CLOSING never merge at the
      same time (the existing close serialization), proven by an e2e test with two campaigns.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests** (fake runner, temp git repos as in the existing e2e fixtures):
  - `tests/test_agentic_queue_merge.py` (new):
    - every `plan_merge` refusal (checks 2–6);
    - adopted vs created worktree planning;
    - `apply_merge` state shape and `open` event extras;
    - `backlog_readiness` both ways;
    - `plan_resume` restriction;
    - `plan_step` defensive park;
    - `plan_abandon` / `apply_abandon` on an adopted worktree holding an uncommitted edit and an
      untracked file: both survive, with and without `--keep-worktree`.
  - `tests/test_agentic_close.py`: a merge campaign closes to MERGED with no spec file in the
    repo; a tracked edit and, separately, an untracked file ⇒ `worktree_dirty` and `git log`
    unchanged; red re-gate ⇒ TRIAGING outcome and the branch left rebased; a conflict ⇒
    branch at its pre-rebase tip with no rebase in progress; a commit added after queueing is
    merged and counted in `commits_since_request`; `_cleanup` keeps an adopted worktree and
    the branch; `FU.file_followups` is not called (spy).
  - `tests/test_agentic_e2e.py`: `wt queue merge --yes` then `queue.run(until_idle=True)` merges
    the branch into `main`. A ledger check asserts no `run.start`. A red-gate variant parks
    `gates_failed` and resumes to MERGED after the branch is fixed. A merge campaign and a
    spec campaign queued together both reach MERGED, one close at a time.
  - `tests/test_agentic_queue_cli.py`, `tests/test_agentic_dashboard.py`,
    `tests/test_agentic_campaign_cli.py`: `merge:<branch>` label in tables and `--json`
    `kind`/`merge_branch`; no `None` in output (the `wt campaign list` table row is asserted
    explicitly). `tests/test_agentic_conduct_cli.py` (new): the
    parked table shows `merge:<branch>`.
  - `tests/test_agentic_events.py`: digest and event rows of a parked merge campaign carry
    `label`.
- **Manual verification:** in a scratch worktree of this repo, commit a docs typo fix on a
  branch, run `uv run wt queue merge <branch>` (dry run, check the plan), then `--yes`. Watch
  `uv run wt queue watch` until MERGED and check that the scratch worktree still exists.
- **Regression guard:** full `uv run pytest`. In particular the existing close, queue and e2e
  tests pass unchanged for spec campaigns.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None required. Existing campaigns have `kind: "implement"` and take the unchanged paths. Until
SPEC-0234 updates the skills, the conductor may already use `wt queue merge` by hand.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
