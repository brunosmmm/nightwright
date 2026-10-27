"""Git worktree / branch operations for campaigns (SPEC-0172).

A thin subprocess wrapper, no shell, no GitPython. Every function takes the repository (or
worktree) path explicitly and returns a structured value or raises `GitError`. `ff_merge` is
the only operation that touches a main checkout and it never forces; `rebase` aborts on
conflict and leaves the worktree exactly as it was. Commit identity comes from the env the
caller passes (tests set GIT_AUTHOR_* / GIT_COMMITTER_*), never from a global config edit."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_TIMEOUT = 300
_NETWORK_TIMEOUT = 1800
_TAIL_LINES = 20


class GitError(RuntimeError):
    """A git / gh command failed. `tail` is the last lines of its combined output."""

    def __init__(self, cmd: list[str], code: int, tail: str):
        self.cmd = list(cmd)
        self.code = code
        self.tail = tail
        super().__init__(f"{' '.join(self.cmd)} exited {code}:\n{tail}")


@dataclass(frozen=True)
class RebaseResult:
    ok: bool
    conflicts: tuple[str, ...]
    head: str
    regenerated: tuple[str, ...] = ()


def _tail(text: str, n: int = _TAIL_LINES) -> str:
    lines = (text or "").rstrip("\n").splitlines()
    return "\n".join(lines[-n:])


def _run(cmd: list[str], *, cwd=None, check=True, timeout=_DEFAULT_TIMEOUT, env=None, input=None,
         binary=False):
    merged_env = None if env is None else {**os.environ, **env}
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=not binary, timeout=timeout,
                           env=merged_env, input=input)
    except subprocess.TimeoutExpired as e:
        raise GitError(cmd, -1, f"timed out after {timeout}s") from e
    except OSError as e:
        raise GitError(cmd, -1, str(e)) from e
    if check and r.returncode != 0:
        out = (r.stdout or r.stdout[:0]) + (r.stderr or r.stderr[:0])
        if binary:
            out = out.decode("utf-8", "replace")
        raise GitError(cmd, r.returncode, _tail(out))
    return r


def git(repo, *args, check=True, timeout=_DEFAULT_TIMEOUT, env=None,
        input=None, binary=False) -> subprocess.CompletedProcess:
    """Run `git -C <repo> *args`; raise `GitError` on non-zero exit when `check`. With
    `binary`, stdout/stderr and `input` are bytes (no decoding, no newline translation)."""
    return _run(["git", "-C", str(repo), *args], check=check, timeout=timeout, env=env,
                input=input, binary=binary)


# ---- read-only --------------------------------------------------------------------------

def current_branch(repo) -> str | None:
    """Short branch name, or None when HEAD is detached."""
    r = git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    return r.stdout.strip() or None if r.returncode == 0 else None


def rev_parse(repo, ref="HEAD") -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def status_porcelain(repo, untracked=True) -> list[str]:
    args = ["status", "--porcelain"]
    if not untracked:
        args.append("--untracked-files=no")
    return [ln for ln in git(repo, *args).stdout.splitlines() if ln.strip()]


def is_clean(repo, untracked=False) -> bool:
    return not status_porcelain(repo, untracked=untracked)


def diff_stat(worktree, base) -> dict:
    """`{files, insertions, deletions}` for `base...HEAD`."""
    out = git(worktree, "diff", "--shortstat", f"{base}...HEAD").stdout.strip()
    stat = {"files": 0, "insertions": 0, "deletions": 0}
    for part in out.split(","):
        words = part.strip().split()
        if len(words) < 2 or not words[0].isdigit():
            continue
        n = int(words[0])
        if words[1].startswith("file"):
            stat["files"] = n
        elif words[1].startswith("insertion"):
            stat["insertions"] = n
        elif words[1].startswith("deletion"):
            stat["deletions"] = n
    return stat


def diff_text(worktree, base, max_lines=1500) -> str:
    """`git diff base...HEAD`, cut to `max_lines` with a note saying how much was dropped."""
    lines = git(worktree, "diff", f"{base}...HEAD").stdout.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines] + [f"… diff truncated: {len(lines) - max_lines} more "
                                          f"line(s); run `git diff {base}...HEAD` for the rest"])


def changed_files(repo, base, head="HEAD", *, renames=True) -> list[str]:
    """Paths touched between `base` and `head` (`git diff --name-only base...head`).
    `renames=False` passes `--no-renames`, so a moved file lists both its old and new path
    (SPEC-0234: a deletion must not hide behind a rename)."""
    args = ["diff", "--name-only", "-z"] + ([] if renames else ["--no-renames"])    # -z: no C-quoting
    return [p for p in git(repo, *args, f"{base}...{head}").stdout.split("\0") if p]


def docs_only_violations(paths, allow) -> list[str]:
    """SPEC-0234: the `paths` no `allow` entry covers. An entry covers a path equal to it, or
    (when it ends in `/`) any path under that directory prefix."""
    return [p for p in paths
            if not any(p == e or (e.endswith("/") and p.startswith(e)) for e in allow)]


def ahead_behind(worktree, base) -> tuple[int, int]:
    """(ahead, behind) of HEAD relative to `base`."""
    out = git(worktree, "rev-list", "--left-right", "--count", f"HEAD...{base}").stdout.split()
    return int(out[0]), int(out[1])


# ---- worktrees / branches ---------------------------------------------------------------

def worktree_add(repo, path, branch, base) -> Path:
    """`git worktree add -b <branch> <path> <base>`; errors if `path` or `branch` exists."""
    path = Path(path).resolve()
    if path.exists():
        raise GitError(["git", "worktree", "add", str(path)], 1, f"{path} already exists")
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0:
        raise GitError(["git", "worktree", "add", "-b", branch], 1, f"branch {branch} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path


def branch_exists(repo, branch) -> bool:
    return git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}",
               check=False).returncode == 0


def worktree_for_branch(repo, branch) -> Path | None:
    """The worktree (the main checkout included) that has `branch` checked out, or None.
    Parsed from `git worktree list --porcelain`."""
    path = None
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path:
            return Path(path).resolve()
    return None


def worktree_add_existing(repo, path, branch) -> Path:
    """`git worktree add <path> <branch>` for a branch that already exists; errors if `path`
    exists."""
    path = Path(path).resolve()
    if path.exists():
        raise GitError(["git", "worktree", "add", str(path)], 1, f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", str(path), branch)
    return path


def worktree_remove(repo, path, force=False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    git(repo, *args, str(path))
    git(repo, "worktree", "prune")


def branch_delete(repo, branch, force=False) -> None:
    git(repo, "branch", "-D" if force else "-d", branch)


def commit_all(worktree, message, env=None) -> str | None:
    """`add -A` then commit; None when nothing is staged."""
    git(worktree, "add", "-A", env=env)
    if git(worktree, "diff", "--cached", "--quiet", check=False, env=env).returncode == 0:
        return None
    git(worktree, "commit", "-q", "-m", message, env=env)
    return rev_parse(worktree)


# ---- stale index.lock (SPEC-0244) -----------------------------------------------------------

_LOCK_POLL_S = 0.5


def index_lock_path(worktree) -> Path:
    """Where git keeps this worktree's `index.lock` (for a linked worktree, under the main
    repo's `.git/worktrees/<name>/`)."""
    out = git(worktree, "rev-parse", "--git-path", "index.lock").stdout.strip()
    p = Path(out)
    return p if p.is_absolute() else Path(worktree) / p


def lock_holders(lock, worktree, *, proc=Path("/proc")) -> list[int] | None:
    """Pids (not ours) that hold `lock` open or have their cwd in `worktree`; None when
    `proc` is not a directory, so holders cannot be determined. A pid whose entries vanish or
    cannot be read (another user's) is skipped."""
    proc = Path(proc)
    if not proc.is_dir():
        return None
    lock_real = os.path.realpath(lock)
    wt_real = os.path.realpath(worktree)
    me = os.getpid()
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            hit = os.path.realpath(os.readlink(entry / "cwd"))
            hit = hit == wt_real or hit.startswith(wt_real + os.sep)
            if not hit:
                for fd in (entry / "fd").iterdir():
                    try:
                        if os.path.realpath(os.readlink(fd)) == lock_real:
                            hit = True
                            break
                    except OSError:
                        continue                # this descriptor closed under us
        except OSError:
            continue
        if hit:
            found.append(int(entry.name))
    return sorted(found)


def clear_stale_index_lock(worktree, *, wait_s=10, proc=Path("/proc"), clock=time) -> dict | None:
    """Remove `worktree`'s `index.lock` when no live process holds it open or works in the
    worktree. Returns None when there is no lock (at any check), else
    `{"path", "removed", "reason", "holders"}`: removed, or kept with a `reason` of
    "unverifiable" (no /proc), "held" (still held after `wait_s`) or "error: <e>". Every
    0.5 s the lock is stat'ed and the holders re-read; nothing from an earlier look is trusted."""
    lock = index_lock_path(worktree)
    start = clock.monotonic()
    while True:
        if not os.path.lexists(lock):
            return None
        holders = lock_holders(lock, worktree, proc=proc)
        out = {"path": str(lock), "removed": False, "reason": None, "holders": holders}
        if holders is None:
            out["reason"] = "unverifiable"
            return out
        if not holders:
            try:
                lock.unlink()
            except FileNotFoundError:
                return None
            except OSError as e:
                out["reason"] = f"error: {e}"
                return out
            out["removed"] = True
            return out
        if clock.monotonic() - start >= wait_s:
            out["reason"] = "held"
            return out
        clock.sleep(_LOCK_POLL_S)


def commit_tracked(worktree, message, env=None) -> str | None:
    """`add -u` (tracked changes; paths already staged stay staged, new files among them) then
    commit; None when nothing is staged. Unlike `commit_all` it never picks up untracked
    residue such as `.orig` backups (SPEC-0252)."""
    git(worktree, "add", "-u", env=env)
    if git(worktree, "diff", "--cached", "--quiet", check=False, env=env).returncode == 0:
        return None
    git(worktree, "commit", "-q", "-m", message, env=env)
    return rev_parse(worktree)


# ---- history rewriting / integration ------------------------------------------------------

_REGEN_MAX_STEPS = 50


def _conflicted(worktree) -> tuple[str, ...]:
    return tuple(sorted(
        ln.strip() for ln in
        git(worktree, "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
        if ln.strip()))


def run_regenerate(worktree, cmd) -> None:
    """Run one `merge.regenerate` command (`sh -c`) in `worktree` (SPEC-0191); `GitError` with
    the output tail when it exits non-zero or cannot run (SPEC-0255 shares it with close)."""
    res = _run(["sh", "-c", cmd], cwd=str(worktree), check=False, timeout=_DEFAULT_TIMEOUT)
    if res.returncode != 0:
        raise GitError(["sh", "-c", cmd], res.returncode,
                       _tail((res.stdout or "") + (res.stderr or "")))


def rebase(worktree, onto, *, regenerate=None) -> RebaseResult:
    """`git rebase <onto>`; on conflict collect the unmerged paths, abort, and verify the
    worktree is back at its pre-rebase HEAD.

    `regenerate` maps repo-relative path -> shell command (SPEC-0191). When every conflicted
    path of a step is declared there, each is checked out from one side, regenerated by its
    command in the worktree, staged, and the rebase continues (bounded). Any other conflict,
    or a failing command, aborts exactly as without it."""
    regenerate = dict(regenerate or {})
    before = rev_parse(worktree)
    r = git(worktree, "rebase", onto, check=False)
    regenerated: list[str] = []
    steps = 0
    while r.returncode != 0:
        conflicts = _conflicted(worktree)
        ok = bool(conflicts) and all(c in regenerate for c in conflicts) \
            and steps < _REGEN_MAX_STEPS
        if ok:
            steps += 1
            try:
                for path in conflicts:
                    git(worktree, "checkout", "--theirs", "--", path)
                    run_regenerate(worktree, regenerate[path])
                    git(worktree, "add", "--", path)
            except GitError:
                ok = False
            else:
                regenerated.extend(p for p in conflicts if p not in regenerated)
                r = git(worktree, "rebase", "--continue", check=False,
                        env={"GIT_EDITOR": "true"})
                continue
        git(worktree, "rebase", "--abort", check=False)
        after = rev_parse(worktree)
        if after != before:
            raise GitError(["git", "rebase", "--abort"], 1,
                           f"worktree HEAD {after} differs from pre-rebase {before}")
        if not conflicts:
            raise GitError(["git", "rebase", onto], r.returncode, _tail(r.stdout + r.stderr))
        return RebaseResult(False, conflicts, after)
    return RebaseResult(True, (), rev_parse(worktree), tuple(regenerated))


# ---- rebase-conflict resolver support (SPEC-0221) -------------------------------------------------

def merge_base(repo, a, b) -> str:
    return git(repo, "merge-base", a, b).stdout.strip()


def _git_path_exists(worktree, names) -> bool:
    for name in names:
        r = git(worktree, "rev-parse", "--git-path", name, check=False)
        path = Path((r.stdout or "").strip())
        if r.returncode == 0 and path.name:
            if not path.is_absolute():
                path = Path(worktree) / path
            if path.exists():
                return True
    return False


def rebase_in_progress(worktree) -> bool:
    return _git_path_exists(worktree, ("rebase-merge", "rebase-apply"))


def sequencer_in_progress(worktree) -> bool:
    """A cherry-pick, merge, revert or multi-commit sequence is under way (SPEC-0252)."""
    return _git_path_exists(worktree, ("CHERRY_PICK_HEAD", "MERGE_HEAD", "REVERT_HEAD",
                                       "sequencer"))


def head_ref(worktree) -> str | None:
    """Full ref HEAD is attached to (`refs/heads/x`), or None when detached."""
    r = git(worktree, "symbolic-ref", "-q", "HEAD", check=False)
    return r.stdout.strip() or None if r.returncode == 0 else None


def restore_branch(worktree, branch, pre_head) -> None:
    """Put the campaign branch back, checked out, at `pre_head` with a clean tree and no
    rebase in progress. Only the campaign branch ref moves; raises `GitError` when the end
    state cannot be reached."""
    if rebase_in_progress(worktree):
        git(worktree, "rebase", "--abort", check=False)
    ref = f"refs/heads/{branch}"
    if head_ref(worktree) != ref:
        git(worktree, "checkout", "-f", branch)
    tip = git(worktree, "rev-parse", "--verify", "-q", ref, check=False).stdout.strip()
    if tip != pre_head or status_porcelain(worktree, untracked=False):
        git(worktree, "reset", "--hard", pre_head)
    clean_untracked(worktree)
    if rebase_in_progress(worktree) or head_ref(worktree) != ref \
            or rev_parse(worktree) != pre_head or status_porcelain(worktree, untracked=False):
        raise GitError(["git", "restore", branch], 1,
                       f"cannot restore {branch} to {pre_head}: "
                       f"HEAD {rev_parse(worktree)}, ref {head_ref(worktree)}")


def clean_untracked(worktree) -> None:
    """Remove untracked, non-ignored leftovers (`.orig` backups, scratch files) so the close
    snapshot cannot commit a resolver's residue onto the campaign branch."""
    git(worktree, "clean", "-fdq")


_RANGE_DIFF_RE = re.compile(
    r"^\s*(?:-|\d+):\s+([0-9a-f]+|-+)\s+([=!<>])\s+(?:-|\d+):\s+([0-9a-f]+|-+)")


@dataclass
class ResolutionCheck:
    """Outcome of `verify_resolution`: `failed` names the first failed check (None = verified);
    `extra` are new commits that pair with no old one, `absorbed` old campaign commits whose
    change `HEAD` already holds; each is `{"sha": <full sha>, "subject": ...}`."""
    failed: str | None = None
    extra: list = field(default_factory=list)
    absorbed: list = field(default_factory=list)


def _commit_patch(worktree, commit) -> bytes:
    """`commit`'s binary diff against its first parent, from plumbing with every output-shaping
    option pinned so user or repo diff config (`diff.noprefix`, colour, external diff, textconv)
    cannot change what `git apply` is fed. Bytes, so CRLF and non-UTF-8 content survive."""
    return git(worktree, "diff-tree", "-p", "--binary", "--full-index", "--no-color",
               "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
               f"{commit}^", commit, binary=True).stdout


def _apply_check(worktree, tree, diff, reverse) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(worktree, "read-tree", tree, env=env)
        args = ["-c", "apply.whitespace=nowarn", "apply", "--cached", "--check"] \
            + (["--reverse"] if reverse else [])
        return git(worktree, *args, check=False, env=env, input=diff,
                   binary=True).returncode == 0


def _patch_applies(worktree, commit, tree, reverse) -> bool:
    """Whether `commit`'s binary diff against its first parent applies (or, with `reverse`,
    reverse-applies) to `tree`, checked in a temporary index so the worktree and the real
    index are never touched. An empty diff applies. A patch that does not even apply to the
    commit's own parent is unusable, not a "no": that raises `GitError` (fail closed)."""
    diff = _commit_patch(worktree, commit)
    if not diff:
        return True
    if _apply_check(worktree, tree, diff, reverse):
        return True
    if not _apply_check(worktree, f"{commit}^", diff, reverse=False):
        raise GitError(["git", "apply", "--check"], 1,
                       f"the patch of {commit[:10]} does not apply to its own parent")
    return False


def _is_empty(worktree, commit) -> bool:
    return git(worktree, "rev-parse", f"{commit}^{{tree}}").stdout \
        == git(worktree, "rev-parse", f"{commit}^^{{tree}}").stdout


def _commit_entry(worktree, sha) -> dict:
    raw = git(worktree, "log", "-1", "--format=%s", sha, binary=True).stdout
    return {"sha": sha, "subject": raw.decode("utf-8", "replace").strip()}


def verify_resolution(worktree, branch, target_sha, fork, pre_head) -> ResolutionCheck:
    """Runner-side check of a resolver's work (SPEC-0221, SPEC-0252): `failed` names the first
    failed check, None when the branch is attached, rebased onto `target_sha`, still accounts
    for every campaign commit and is tracked-clean. A campaign commit is accounted for when
    range-diff pairs it, `git cherry` finds it in the target, it was empty, or its change
    reverse-applies to `HEAD` (absorbed). New unpaired commits are allowed (`extra`) unless one
    reverts a campaign commit."""
    def fail(why):
        return ResolutionCheck(failed=why)

    if head_ref(worktree) != f"refs/heads/{branch}":
        return fail("HEAD is not attached to the campaign branch")
    if rebase_in_progress(worktree):
        return fail("a rebase is still in progress")
    if git(worktree, "ls-files", "-u").stdout.strip():
        return fail("unmerged paths remain")
    if git(worktree, "merge-base", "--is-ancestor", target_sha, "HEAD", check=False).returncode:
        return fail("the rebase target is not an ancestor of HEAD")
    old_commits = git(worktree, "rev-list", "--reverse", f"{fork}..{pre_head}").stdout.split()
    new_commits = git(worktree, "rev-list", f"{target_sha}..HEAD").stdout.split()
    out = ""
    if old_commits and new_commits:             # range-diff refuses an empty range
        # `-s`: only the pair headers matter; the diff-of-diffs body holds raw file content
        raw = git(worktree, "range-diff", "-s", "--no-color", "--creation-factor=95",
                  f"{fork}..{pre_head}", f"{target_sha}..HEAD", binary=True).stdout
        out = raw.decode("utf-8", "replace")
    paired: dict = {}                   # old sha -> the new commit range-diff pairs it with
    for line in out.splitlines():
        m = _RANGE_DIFF_RE.match(line)
        if not m:
            continue
        old, mark, new = m.groups()
        if mark in "=!":
            paired[rev_parse(worktree, old)] = rev_parse(worktree, new)
    # the `>` lines of range-diff, by full sha: new commits nothing pairs with
    added = [n for n in reversed(new_commits) if n not in paired.values()]
    cherry_gone = {ln.split()[1] for ln in
                   git(worktree, "cherry", target_sha, pre_head).stdout.splitlines()
                   if ln.startswith("-") and len(ln.split()) > 1}
    absorbed, guarded = [], []          # guarded: (old campaign sha, commit whose patch to guard)
    for c in old_commits:
        empty = _is_empty(worktree, c)
        if c in paired:
            # a resolution may have rewritten the patch (`!`): guard the new one, which is
            # what the branch carries
            if not empty and not _is_empty(worktree, paired[c]):
                guarded.append((c, paired[c]))
            continue
        if not empty:
            guarded.append((c, c))
        if c in cherry_gone or empty or _patch_applies(worktree, c, "HEAD", reverse=True):
            absorbed.append(_commit_entry(worktree, c))
            continue
        subject = _commit_entry(worktree, c)["subject"]
        return fail(f"campaign commit {c[:10]} ({subject}) is missing after the resolution")
    extra = []
    for e in added:
        for c, patch in guarded:
            if _patch_applies(worktree, patch, f"{e}^", reverse=True) \
                    and _patch_applies(worktree, patch, e, reverse=False):
                return fail(f"extra commit {e[:10]} ({_commit_entry(worktree, e)['subject']}) "
                            f"reverts campaign commit {c[:10]}")
        extra.append(_commit_entry(worktree, e))
    if status_porcelain(worktree, untracked=False):
        return fail("the worktree is dirty")
    return ResolutionCheck(None, extra, absorbed)


def ff_merge(repo, branch) -> str:
    """`git merge --ff-only <branch>` in `repo`; returns the new HEAD sha."""
    git(repo, "merge", "--ff-only", branch)
    return rev_parse(repo)


def fetch(repo, remote, ref) -> None:
    git(repo, "fetch", remote, ref, timeout=_NETWORK_TIMEOUT)


def push_branch(worktree, remote, branch, set_upstream=True) -> None:
    args = ["push"]
    if set_upstream:
        args.append("-u")
    git(worktree, *args, remote, branch, timeout=_NETWORK_TIMEOUT)


def gh_pr_create(worktree, *, base, head, title, body_file, draft=False, labels=()) -> str:
    """`gh pr create …` with cwd=worktree; returns the PR URL (last line of stdout)."""
    exe = shutil.which("gh")
    if not exe:
        raise GitError(["gh", "pr", "create"], -1, "gh not found on PATH")
    cmd = [exe, "pr", "create", "--base", base, "--head", head, "--title", title,
           "--body-file", str(body_file)]
    if draft:
        cmd.append("--draft")
    for label in labels:
        cmd += ["--label", label]
    r = _run(cmd, cwd=str(worktree), timeout=_NETWORK_TIMEOUT)
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        raise GitError(cmd, 0, "gh printed no URL")
    return lines[-1]
