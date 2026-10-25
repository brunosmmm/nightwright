---
id: SPEC-0172
title: "Git worktree and branch operations for campaigns (gitops)"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, git, worktrees]
source_idea: IDEA-019
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-019`).

Every campaign works in its own git worktree on its own branch and lands by fast-forward
merge or PR. wt's git use so far is read-only plumbing: `rev-parse`, `log`, `show`
(`export.py:210-229`), `git archive` (`release_archive.py`). There is no branch, worktree,
commit, rebase, merge, push or `gh` code anywhere. This spec adds exactly that, as a small
subprocess wrapper with structured results, so the close step (SPEC-0175) and the queue
(SPEC-0176) never shell out to git themselves.

## Goals / Non-goals

**Goals**
- One module, `src/wt/agentic/gitops.py`, wrapping the git operations the loop needs, each
  returning a structured value or raising `GitError(cmd, code, tail)`.
- Rebase reports conflicts and leaves the worktree exactly as it was (abort on conflict).
- `ff_merge` is the only operation that touches a main checkout, and it never forces.
- `gh pr create` wrapped so tests can substitute a script on `PATH`.

**Non-goals**
- Conflict resolution, policy decisions, preconditions on the main checkout (SPEC-0175).
- GitPython or any new dependency.
- Remote credential handling; `git` and `gh` use whatever the user has configured.

## Decision

`gitops.py` exposes narrow functions over `subprocess.run(["git", "-C", repo, ...],
capture_output=True, text=True, timeout=…)`. Nothing uses a shell. Every function takes the
repository (or worktree) path explicitly. Identity for commits comes from the environment
the caller passes (tests set `GIT_AUTHOR_NAME` etc.), never from a global git config edit.

## Design

```python
class GitError(RuntimeError):
    def __init__(self, cmd: list[str], code: int, tail: str): ...

def git(repo, *args, check=True, timeout=300, env=None) -> subprocess.CompletedProcess
```

| function | behavior |
|---|---|
| `current_branch(repo) -> str \| None` | `symbolic-ref --short -q HEAD`; None when detached |
| `rev_parse(repo, ref="HEAD") -> str` | full sha |
| `status_porcelain(repo, untracked=True) -> list[str]` | `status --porcelain [--untracked-files=no]` |
| `is_clean(repo, untracked=False) -> bool` | empty porcelain |
| `worktree_add(repo, path, branch, base) -> Path` | `worktree add -b <branch> <path> <base>`; errors if `path` exists or `branch` exists |
| `worktree_remove(repo, path, force=False)` | `worktree remove [--force] <path>`; then `worktree prune` |
| `branch_delete(repo, branch, force=False)` | `branch -d/-D` |
| `commit_all(worktree, message, env=None) -> str \| None` | `add -A`; if `diff --cached --quiet` ⇒ None; else `commit -m` and return the sha |
| `diff_stat(worktree, base) -> dict` | `diff --shortstat <base>...HEAD` → `{files, insertions, deletions}` |
| `ahead_behind(worktree, base) -> tuple[int, int]` | `rev-list --left-right --count HEAD...<base>` |
| `rebase(worktree, onto) -> RebaseResult(ok, conflicts: list[str], head: str)` | `rebase <onto>`; non-zero ⇒ collect `diff --name-only --diff-filter=U`, then `rebase --abort`, assert `HEAD` equals the pre-rebase sha |
| `ff_merge(repo, branch) -> str` | `merge --ff-only <branch>`; returns new HEAD sha; non-ff ⇒ `GitError` |
| `fetch(repo, remote, ref)` | `fetch <remote> <ref>` |
| `push_branch(worktree, remote, branch, set_upstream=True)` | `push [-u] <remote> <branch>` |
| `gh_pr_create(worktree, *, base, head, title, body_file, draft=False, labels=()) -> str` | runs `gh pr create --base --head --title --body-file [--draft] [--label …]` with `cwd=worktree`; returns the URL (last line of stdout); non-zero ⇒ `GitError` |

Timeouts: 300 s default, 1800 s for `push`/`fetch`/`gh`. `GitError.tail` is the last 20
lines of stderr+stdout. All paths are returned absolute.

### Tests substitute `gh`

`gh_pr_create` resolves the executable with `shutil.which("gh")`; tests prepend a tmp dir
containing an executable `gh` script that records its argv to a file and prints a fixed URL.

## Alternatives considered

- **GitPython / dulwich** — rejected. A new runtime dependency for ~15 commands; the
  subprocess wrapper is smaller and matches the repo's existing style.
- **`git worktree list` parsing for discovery** — not needed. Campaign state records the
  worktree path (SPEC-0171); SPEC-0130 already chose plumbing over `worktree list`.
- **Auto-resolve rebase conflicts here** — rejected. Policy lives in SPEC-0175 and, in wave
  1, parks the campaign.

## Acceptance criteria

- [ ] On a temp repo: `worktree_add` creates the directory and branch from `base`;
      `worktree_remove` removes it and prunes; adding twice errors.
- [ ] `commit_all` returns None when nothing changed and a sha (with the message verbatim,
      including a trailer line) when something did; identity comes from env.
- [ ] `rebase` onto a diverged base without conflicts fast-forwards the branch; with a
      conflicting file it returns `ok=False`, names the file, and the worktree HEAD and
      tree are byte-identical to before the call.
- [ ] `ff_merge` succeeds when the branch is ahead of the base and raises `GitError`
      (base untouched) when the base has diverged.
- [ ] `is_clean(untracked=False)` ignores untracked files; `ahead_behind` and `diff_stat`
      report correct counts.
- [ ] `fetch` / `push_branch` work against a bare temp remote; `gh_pr_create` calls the
      `gh` found on `PATH` with the documented flags and returns its printed URL; a
      non-zero `gh` raises `GitError` with the tail.
- [ ] No new dependency in `pyproject.toml`.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_gitops.py` builds repos with `git init -b main`
  in `tmp_path`, identity via `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env, a bare remote for
  fetch/push, a fake `gh` script on a prepended `PATH`; each row of the table above has a
  test; the conflict test asserts tree hashes (`rev-parse HEAD^{tree}`) before and after.
- **Manual verification:** none beyond the suite; the functions are exercised for real by
  SPEC-0175's manual run.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Internal work: clock on IDEA-019 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Library only; nothing calls it until SPEC-0175/0176 land.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
