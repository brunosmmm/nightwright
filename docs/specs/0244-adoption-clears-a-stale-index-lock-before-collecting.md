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
this spec closes. A runner killed by SIGKILL or the OOM killer in the middle of `git
add`/`git commit` leaves `index.lock` in the worktree's git dir; so does an orphaned `git`
child that died later. Every later git command that writes the index then fails with
`Unable to create '…/index.lock': File exists`. The adopted run takes the existing
commit-error path (`StepResult.error`), and every retry fails the same way until a human
deletes the file. The implementer's work is safe in the worktree but cannot be committed.

A SPEC-0194 `execve` is **not** a cause. SPEC-0225 makes the implementer's commit part of the
non-detachable `collect` phase: `try_detach_all` returns None while any task is in `collect`,
so an exec cannot interrupt `commit_all`.

The worktrees are linked worktrees, so the lock is not at `<worktree>/.git/index.lock`. It
is at `git rev-parse --git-path index.lock`, which resolves to
`<main>/.git/worktrees/<name>/index.lock`.

## Goals / Non-goals

**Goals**
- When an adopted implementer flight is about to commit, a stale `index.lock` in its worktree
  is removed first, and the removal is recorded in the campaign ledger.
- A lock is stale only when **no live process** holds it open or works in the worktree. It is
  never judged stale by its age.

**Non-goals**
- Fresh (non-adopted) runs. Their commit runs in the same process that started the run, so a
  leftover lock means something else is wrong, and the existing error path stays.
- Other git locks (`HEAD.lock`, `refs/**.lock`, `packed-refs.lock`), and the close-time
  snapshot commit in `close.py`.
- Platforms without `/proc`. There the lock is never removed, and the error path stays.
- Repairing a half-written index. Git writes the new index to the lock file and renames it,
  so removing the lock leaves the previous index intact.

## Decision

Add `gitops.clear_stale_index_lock(worktree)`. An adopted implementer flight calls it in
`_do_persona` right before `commit_all`. The helper removes the lock only when a `/proc` scan
finds no process that holds the lock file open (`/proc/<pid>/fd/*`) and no process whose
working directory is inside the worktree (`/proc/<pid>/cwd`). The cwd check covers
`git commit` running hooks: git closes the lock's descriptor before it runs
`pre-commit`/`commit-msg`, but git and its hooks run with their cwd in the worktree. A held
lock is re-checked for up to 10 s, then left in place. Every outcome other than "no lock" is
recorded as a ledger `note`.

## Design

- **`gitops.index_lock_path(worktree) -> Path`**: `git -C <worktree> rev-parse --git-path
  index.lock`, resolved against the worktree when the output is relative.
- **`gitops.lock_holders(lock, worktree, *, proc=Path("/proc")) -> list[int] | None`**:
  - returns None when `proc` is not a directory (holders cannot be determined);
  - otherwise returns the sorted pids, other than `os.getpid()`, for which either:
    - some `proc/<pid>/fd/*` symlink resolves to `lock`; or
    - `proc/<pid>/cwd` resolves to the worktree or a path inside it.
  - A pid whose entries vanish or are unreadable (`OSError`, including `PermissionError`)
    while it is scanned is skipped. Processes of other users cannot be inspected; the
    runner and its git children run as the same user, which is the case that matters.
  - `proc` is injectable, so tests can use a fake tree.
- **`gitops.clear_stale_index_lock(worktree, *, wait_s=10, proc=Path("/proc"),
  clock=time) -> dict | None`**:
  - It loops. On every iteration (every 0.5 s through `clock.sleep`) it re-stats the lock
    and re-runs `lock_holders`. It does not trust a previous reading.
  - No lock file (at the first check or any later one): return None.
  - `lock_holders` is None: return `{"path", "removed": False, "reason": "unverifiable",
    "holders": None}` at once.
  - Holders found: keep looping until `wait_s` has passed by `clock.monotonic()`, then
    return `{"path", "removed": False, "reason": "held", "holders": [pids]}`.
  - No holders: `unlink` the lock and return `{"path", "removed": True, "reason": None,
    "holders": []}`. An `OSError` from `unlink` returns `{"path", "removed": False,
    "reason": "error: <e>", "holders": []}`.
  - Every returned dict has exactly the keys `path` (str), `removed` (bool), `reason`
    (str or None) and `holders` (list of ints, or None).
  - The helper never raises for the lock itself. A failing `rev-parse` raises `GitError`, as
    every gitops call does.
  - `clock`, with `monotonic()` and `sleep()`, is injectable, so tests need no real waiting.
- **`_do_persona`**: only for an adopted flight (SPEC-0225's `adopted=True`) with
  `step.kind == "implement"` that was not interrupted, call
  `clear_stale_index_lock(worktree)` before `commit_all`, and keep the result on the
  `StepResult` as `res.lock_cleanup`. Fresh flights never call the helper.
- **`_apply`**: when `res.lock_cleanup` is not None, its note is appended to `events`
  **first**, before the `res.error and res.config_error` and `res.error and kind !=
  "close"` branches. The note therefore reaches the ledger on the error/park path as well as
  on the success path. The note text is:
  - removed: `removed stale <path> (no process held it; left by a killed run)`;
  - not removed: `index.lock present and not removed (<reason>; holders <pids>): <path>`.
    The commit then fails and takes today's error path.
  Both notes use `author: "queue"`.

## Alternatives considered

- **An age bound (remove after N seconds without an mtime change)** — git does not touch the
  lock while hooks run. A `git commit` orphaned by a killed runner and running a slow
  pre-commit hook (linters, tests) would lose its lock, and two writers would race on the
  index and HEAD.
- **Remove the lock whenever the run is dead** — a `git` child of the dead runner can still
  hold it.
- **Shell out to `fuser`/`lsof`** — they are not always installed, and they report only open
  descriptors, which misses the hook case. Reading `/proc` directly does both checks with
  no dependency.
- **Use `git commit` with `GIT_INDEX_FILE` pointing at a fresh index** — it hides the problem
  for one commit, and leaves the lock for the next git command.
- **Park the campaign for a human** — when no process is involved, the fix is mechanical and
  safe; a park costs human attention for nothing.

## Acceptance criteria

- [ ] `index_lock_path` returns the linked worktree's `<main>/.git/worktrees/<name>/index.lock`
      for a linked worktree, and `<repo>/.git/index.lock` for a main checkout.
- [ ] `lock_holders`, on a fake `/proc` tree, finds a pid by an fd symlink to the lock and a
      pid by a cwd inside the worktree. It skips the current pid and pids whose entries
      raise `OSError`. It returns None when the proc dir is missing. On the real `/proc`, a
      `sleep` subprocess started with `cwd=worktree` is reported.
- [ ] `clear_stale_index_lock` (fake clock and fake proc), with each case returning exactly
      the specified keys:
      - no lock: None;
      - no holders: removed;
      - holders throughout `wait_s`: `held` with the pids, and the lock is kept;
      - holders that go away while waiting: removed (the holders are re-read every
        iteration);
      - a lock that disappears while waiting: None;
      - no proc dir: `unverifiable`;
      - an `unlink` OSError: `error: …`, not raised.
- [ ] An adopted implementer flight whose worktree has uncommitted edits and an unheld
      `index.lock` removes the lock, commits the edits (`commit` event with the sha), and
      records the `removed stale … index.lock` note.
- [ ] An adopted flight whose lock is held (a live process with its cwd in the worktree)
      keeps the lock, records the `not removed (held; …)` note **and** the commit-error
      park, and the tick does not raise.
- [ ] A fresh (non-adopted) implementer flight with an `index.lock` present never calls the
      helper (spy), and its behaviour is unchanged: commit error, no note.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_index_lock.py`.
  - Unit tests for `index_lock_path` on a scratch repo with a linked worktree.
  - Unit tests for `lock_holders` and `clear_stale_index_lock`, with a fake clock object and
    a fake proc tree (`tmp_path/proc/<pid>/fd/<n>` symlinks and `cwd` symlinks), plus one
    real-`/proc` check with a `sleep` subprocess (skipped without `/proc`).
  - Adoption tests reuse SPEC-0225's adoption fixtures (a run dir with `proc.json` and
    `exit.json`, the campaign in IMPLEMENTING) with a FakeRunner implementer, plus a
    hand-made `index.lock`. The tick's ledger is checked for the note and the `commit`
    event, or for the note and the error park.
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
