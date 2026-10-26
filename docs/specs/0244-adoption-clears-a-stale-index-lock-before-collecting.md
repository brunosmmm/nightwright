---
id: SPEC-0244
title: "Adoption clears a stale .git/index.lock left by a killed implementer before collecting"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, git, reexec]
source_idea: IDEA-104
depends_on: [SPEC-0225]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from bug `IDEA-104`.

[SPEC-0225](./0225-detached-persona-runs-adopted-across-runner-restarts.md) (accepted, not yet
built) lets a new runner process **adopt** persona runs that a previous process left open, and
collect them. For an implementer, collecting ends in `queue._do_persona`, which calls
`gitops.commit_all(worktree, msg)` (`git add -A`, then `git commit`). SPEC-0229 notes the gap
this spec closes: a runner thread killed in the middle of `git add`/`git commit` (SIGKILL, an
OOM kill, or the old image replaced by a SPEC-0194 `execve` while a commit was running)
leaves `index.lock` in the worktree's git dir. Every later git command that writes the index
then fails with `Unable to create '…/index.lock': File exists`. The adopted run takes the
existing commit-error path (`StepResult.error`), and every retry fails the same way until a
human deletes the file. The work the implementer did is safe in the worktree but cannot be
committed.

The worktrees are linked worktrees, so the lock is not at `<worktree>/.git/index.lock`. It
is at `git rev-parse --git-path index.lock`, which resolves to
`<main>/.git/worktrees/<name>/index.lock`.

## Goals / Non-goals

**Goals**
- When an adopted implementer flight is about to commit, a stale `index.lock` in its worktree
  is removed first, and the removal is recorded in the campaign ledger.
- A lock that could belong to a live git process is never removed.

**Non-goals**
- Fresh (non-adopted) runs. Their commit runs in the same process that started the run, so a
  leftover lock means something else is wrong, and the existing error path stays.
- Other git locks (`HEAD.lock`, `refs/**.lock`, `packed-refs.lock`), and the close-time
  snapshot commit in `close.py`.
- Repairing a half-written index. Git writes the new index to the lock file and renames it,
  so removing the lock leaves the previous index intact.

## Decision

Add `gitops.clear_stale_index_lock(worktree, *, before, min_age_s)`. An adopted implementer
flight calls it in `_do_persona` right before `commit_all`, with `before` = the moment this
process's adoption pass began and `min_age_s` = 30. A lock is stale only if it was last
modified before this process adopted the run **and** it is at least 30 s old. A younger lock
is waited on (it may belong to a git process that is still finishing). The flight records
a `note` for a removal, and for a removal that failed.

## Design

- **`gitops.index_lock_path(worktree) -> Path`**: `git -C <worktree> rev-parse --git-path
  index.lock`, resolved against the worktree when the output is relative.
- **`gitops.clear_stale_index_lock(worktree, *, before, min_age_s=30, wait_s=60,
  clock=time) -> dict | None`**:
  - no lock file: return None;
  - `mtime >= before` (the lock was taken after this process adopted the run, so possibly by
    this process): return `{"path", "removed": False, "reason": "recent"}` and leave it;
  - otherwise, while `now - mtime < min_age_s` and the lock still exists, sleep 0.5 s, for at
    most `wait_s` in total (it is a worker thread, so no tick is blocked). Then:
    - the lock is gone: return None, because a live git finished;
    - still too young after `wait_s`: return `{..., "removed": False, "reason": "young"}`;
    - else `unlink` it and return `{"path", "removed": True, "mtime", "age_s"}`. An
      `OSError` from `unlink` returns `{..., "removed": False, "reason": "error: <e>"}`.
      The helper never raises for the lock itself; a failing `rev-parse` raises `GitError`,
      as every gitops call does.
  - `clock` (with `time()` and `sleep()`) is injectable, so tests need no real waiting.
- **Why these bounds.** Before adoption, no thread of this process touches the worktree, so
  any lock older than `before` was taken by a dead thread or by a git child left over from
  the previous image. Such a child (a `git commit` running hooks when the old process was
  replaced) can outlive the image by a little. 30 s of quiet covers a commit hook; a lock
  that is still being refreshed is not quiet. 60 s of waiting bounds the worst case for the
  worker thread.
- **`before`**: `_adopt` (SPEC-0225) records `pool.adopted_at` (an aware datetime) when its
  pass begins, and every adopted flight carries it as `adopted_at`, next to SPEC-0225's
  `adopted=True`. It is not the lease's `started_at`: that is kept across an `execve`
  (SPEC-0225 handover), and a lock left by a thread that the exec killed is newer than it.
- **`_do_persona`**: for `step.kind == "implement"`, not interrupted, and only for an adopted
  flight, call `clear_stale_index_lock(worktree, before=adopted_at)` before `commit_all`, and
  keep its result on the `StepResult` (`res.lock_cleanup`). `_apply` turns it into a ledger
  `note`:
  - removed: `removed stale <path> (age <n>s, left by a killed run)`, `author: "queue"`;
  - not removed (`recent`, `young`, `error: …`): `index.lock present and not removed
    (<reason>): <path>`. The commit then fails and takes today's error path.
  Nothing is recorded for None. Fresh flights never call the helper.

## Alternatives considered

- **Remove the lock whenever the run is dead** — a `git` child of the old image can still hold
  it; deleting a live lock lets two writers race on the index.
- **Scan `/proc` for git processes whose cwd is the worktree** — not portable, and SPEC-0225
  already rejected process-table scans as fragile.
- **Use `git commit` with `GIT_INDEX_FILE` pointing at a fresh index** — it hides the problem
  for one commit, and leaves the lock for the next git command.
- **Park the campaign for a human** — the case is mechanical and safe to fix with the bounds
  above; a park costs human attention for nothing.

## Acceptance criteria

- [ ] `index_lock_path` returns the linked worktree's `<main>/.git/worktrees/<name>/index.lock`
      for a linked worktree, and `<repo>/.git/index.lock` for a main checkout.
- [ ] `clear_stale_index_lock` (fake clock): no lock gives None; a lock with `mtime >= before`
      is kept (`recent`); a lock older than `before` and at least `min_age_s` old is removed
      (`removed: True`); a young lock that disappears while waiting gives None; a young lock
      still present after `wait_s` is kept (`young`); an `unlink` OSError is reported as
      `error: …` and not raised.
- [ ] An adopted implementer flight whose worktree has uncommitted edits and a stale
      `index.lock` (mtime set before `adopted_at`) removes the lock, commits the edits
      (`commit` event with the sha), and records the `removed stale … index.lock` note.
- [ ] An adopted flight whose lock is newer than `adopted_at` keeps the lock, records the
      `not removed (recent)` note, and takes the existing commit-error path. The tick does
      not raise.
- [ ] A fresh (non-adopted) implementer flight with an `index.lock` present never calls the
      helper (spy), and its behaviour is unchanged: commit error, no note.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_index_lock.py`.
  - Unit tests for `index_lock_path` on a scratch repo with a linked worktree.
  - Unit tests for `clear_stale_index_lock`, with a fake clock object and `os.utime` to set
    the lock's mtime.
  - Adoption tests reuse SPEC-0225's adoption fixtures (a run dir with `proc.json` and
    `exit.json`, the campaign in IMPLEMENTING) with a FakeRunner implementer, plus a
    hand-made `index.lock`. The tick's ledger is checked for the note and the `commit`
    event.
- **Regression guard:** full `uv run pytest`, in particular the SPEC-0225 adoption suite and
  `tests/test_agentic_e2e.py`.

## Rollout / migration

Ships after SPEC-0225. No data changes.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
