"""SPEC-0548: `apply_clear` / `nw queue main-dirty-apply` core: guards, `commit`, `resume`."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import signal
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nightwright.agentic import main_dirty as MD
from nightwright.cli import cli

pytestmark = pytest.mark.fast

REPO_ROOT = Path(__file__).resolve().parents[1]
SUBJECT = "chore: land stray trivial main-checkout edits"


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def _put(repo, rel, content):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode())


def _repo(tmp_path, files=None, base="main", project=True, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", base)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    if project:
        _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                       "base": base}))
    _put(repo, "README.md", "# hi\n")
    _put(repo, "docs/a.md", "# T\n")
    for rel, content in (files or {}).items():
        _put(repo, rel, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _index_path(repo):
    p = _git(repo, "rev-parse", "--git-path", "index").stdout.strip()
    return Path(p) if os.path.isabs(p) else Path(repo) / p


def _snap(repo):
    """HEAD, index bytes, worktree bytes (untracked included) and the branch list."""
    files = {}
    for dp, dn, fn in os.walk(repo):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            full = Path(dp) / f
            rel = str(full.relative_to(repo))
            files[rel] = os.readlink(full) if full.is_symlink() else full.read_bytes()
    return {"head": _git(repo, "rev-parse", "HEAD").stdout, "index": _index_path(repo).read_bytes(),
            "files": files, "branches": _git(repo, "branch", "--list").stdout}


def _edit_trivial(repo, rel="docs/a.md"):
    _put(repo, rel, "# T\n\nmore\n")


def _invoke(*args, env=None):
    return CliRunner().invoke(cli, ["queue", "main-dirty-apply", *args], env=env)


def _tracked_clean(repo):
    return _git(repo, "status", "--porcelain", "--untracked-files=no").stdout == ""


# ---- AC1: an operation in progress, before everything else ------------------------------------

def _gitdir(repo, name):
    p = _git(repo, "rev-parse", "--git-path", name).stdout.strip()
    return Path(p) if os.path.isabs(p) else Path(repo) / p


def _marker_file(name):
    def build(repo):
        _gitdir(repo, name).write_text(_git(repo, "rev-parse", "HEAD").stdout)
    return build


def _marker_dir(name):
    def build(repo):
        _gitdir(repo, name).mkdir()
    return build


def _real_conflict(repo):
    _git(repo, "switch", "-qc", "side")
    _put(repo, "docs/c.md", "# side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "switch", "-q", "main")
    _put(repo, "docs/c.md", "# main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    assert _git(repo, "merge", "side", check=False).returncode != 0


OPS = {
    "MERGE_HEAD": (_marker_file("MERGE_HEAD"), "merge in progress"),
    "CHERRY_PICK_HEAD": (_marker_file("CHERRY_PICK_HEAD"), "cherry-pick in progress"),
    "REVERT_HEAD": (_marker_file("REVERT_HEAD"), "revert in progress"),
    "rebase-merge": (_marker_dir("rebase-merge"), "rebase in progress"),
    "rebase-apply": (_marker_dir("rebase-apply"), "rebase in progress"),
    "real-conflict": (_real_conflict, "merge in progress"),
}


@pytest.mark.parametrize("claim", [None, "commit", "save_branch"])
@pytest.mark.parametrize("op", sorted(OPS))
def test_operation_in_progress_refuses_before_plan_check(tmp_path, op, claim):
    repo = _repo(tmp_path)
    build, error = OPS[op]
    build(repo)
    if op != "real-conflict":
        _edit_trivial(repo)
    _put(repo, "untracked.txt", "u\n")
    before = _snap(repo)
    plan = None if claim is None else {"schema": MD.SCHEMA, "action": claim, "paths": ["docs/a.md"]}
    out = MD.apply_clear(repo, plan=plan)
    assert (out["ok"], out["escalate"], out["error"]) == (False, True, error)
    assert out["error"] != "plan is stale"
    assert _snap(repo) == before


@pytest.mark.parametrize("code", sorted(MD._UNMERGED))
def test_every_unmerged_status_code_is_a_merge(tmp_path, monkeypatch, code):
    repo = _repo(tmp_path)
    row = {"xy": code, "path": "docs/a.md", "orig": None}
    monkeypatch.setattr(MD, "_status", lambda root: [dict(row)])
    before = _snap(repo)
    out = MD.apply_clear(repo, plan={"schema": MD.SCHEMA, "action": "commit", "paths": ["docs/a.md"]})
    assert (out["ok"], out["escalate"], out["error"]) == (False, True, "conflict in progress")
    assert _snap(repo) == before


def _rename_rename(repo):
    """a -> b on one side, a -> c on the other: git status shows `DD a` (plus `AU b`, `UA c`)."""
    _git(repo, "switch", "-qc", "side")
    _git(repo, "mv", "docs/a.md", "docs/b.md")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "switch", "-q", "main")
    _git(repo, "mv", "docs/a.md", "docs/c.md")
    _git(repo, "commit", "-qm", "main")
    assert _git(repo, "merge", "side", check=False).returncode != 0


@pytest.mark.parametrize("claim", [None, "commit"])
def test_real_dd_conflict_is_a_merge(tmp_path, claim):
    repo = _repo(tmp_path)
    _rename_rename(repo)
    assert "DD docs/a.md" in _git(repo, "status", "--porcelain").stdout
    _put(repo, "untracked.txt", "u\n")
    before = _snap(repo)
    plan = None if claim is None else {"schema": MD.SCHEMA, "action": claim, "paths": ["docs/a.md"]}
    out = MD.apply_clear(repo, plan=plan)
    assert (out["ok"], out["escalate"]) == (False, True) and out["error"].endswith("in progress")
    assert out["error"] != "plan is stale"
    assert _snap(repo) == before


@pytest.mark.parametrize("claim", [None, "commit", "save_branch"])
def test_merge_in_a_linked_worktree(tmp_path, claim):
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "lw", str(linked))
    _gitdir(linked, "MERGE_HEAD").write_text(_git(linked, "rev-parse", "HEAD").stdout)
    _edit_trivial(linked)
    before = _snap(linked)
    plan = None if claim is None else {"schema": MD.SCHEMA, "action": claim, "paths": ["docs/a.md"]}
    out = MD.apply_clear(linked, plan=plan)
    assert (out["ok"], out["escalate"], out["error"]) == (False, True, "merge in progress")
    assert _snap(linked) == before


def test_stale_plan_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    before = _snap(repo)
    for plan in ({"schema": MD.SCHEMA, "action": "save_branch", "paths": ["docs/a.md"]},
                 {"schema": MD.SCHEMA, "action": "commit", "paths": ["docs/other.md"]},
                 {"schema": MD.SCHEMA, "action": "commit", "paths": []}):
        out = MD.apply_clear(repo, plan=plan)
        assert (out["ok"], out["error"]) == (False, "plan is stale")
        assert _snap(repo) == before


def test_plan_base_is_never_used(tmp_path, monkeypatch):
    """No project config, checkout on `develop`: only a forwarded base could make this a commit."""
    repo = _repo(tmp_path, base="develop", project=False)
    _edit_trivial(repo)
    plan = {"schema": MD.SCHEMA, "action": "hold", "paths": ["docs/a.md"], "base": "develop"}
    before = _snap(repo)
    out = MD.apply_clear(repo, plan=plan)
    assert (out["ok"], out["error"]) == (False, "bad base main")   # SPEC-0549 guard 7: no `main`
    assert _snap(repo) == before
    seen = []
    real = MD.plan_clear
    monkeypatch.setattr(MD, "plan_clear",
                        lambda root, base=None: (seen.append(base), real(root, base=base))[1])
    MD.apply_clear(repo, plan=plan)
    assert seen == [None]


def test_plan_paths_order_does_not_matter(tmp_path):
    repo = _repo(tmp_path, {"docs/b.md": "# b\n"})
    _edit_trivial(repo)
    _edit_trivial(repo, "docs/b.md")
    plan = {"schema": MD.SCHEMA, "action": "commit", "paths": ["docs/b.md", "docs/a.md"]}
    assert MD.plan_clear(repo)["paths"] == ["docs/a.md", "docs/b.md"]
    assert MD.apply_clear(repo, plan=plan)["ok"] is True


def test_guard_order_stale_then_bad_path_then_partial(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    live = MD.plan_clear(repo)
    stale = {"schema": MD.SCHEMA, "action": "commit", "paths": ["docs/other.md"]}
    # a stale plan and a bad live path: the stale plan wins
    monkeypatch.setattr(MD, "plan_clear", lambda root, base=None: {**live, "paths": ["../x.md"]})
    assert MD.apply_clear(repo, plan=stale)["error"] == "plan is stale"
    assert MD.apply_clear(repo)["error"] == "bad path ../x.md"
    monkeypatch.undo()
    # a path that is a symlink and also partially staged: the bad path wins
    repo2 = _repo(tmp_path, name="r2")
    os.symlink("a.md", repo2 / "docs/link.md")
    _git(repo2, "add", "-A")
    _git(repo2, "commit", "-qm", "link")
    (repo2 / "docs/link.md").unlink()
    os.symlink("b.md", repo2 / "docs/link.md")
    _git(repo2, "add", "docs/link.md")
    (repo2 / "docs/link.md").unlink()
    os.symlink("c.md", repo2 / "docs/link.md")
    out = MD.apply_clear(repo2)
    assert out["error"] == "bad path docs/link.md"


# ---- AC2: the trivial commit ------------------------------------------------------------------

def test_commit_trivial(tmp_path):
    repo = _repo(tmp_path, {"docs/a*b.md": "# T\n"})
    _put(repo, "docs/a*b.md", "# T\n\nx\n")
    _put(repo, "docs/axb.md", "sibling\n")
    _put(repo, "untracked.txt", "u\n")
    _edit_trivial(repo)
    untracked = {p: (repo / p).read_bytes() for p in ("docs/axb.md", "untracked.txt")}
    out = MD.apply_clear(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert (out["ok"], out["action"], out["escalate"], out["sha"]) == (True, "commit", False, head)
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == SUBJECT
    body = _git(repo, "log", "-1", "--format=%b").stdout.splitlines()
    assert {"docs/a*b.md", "docs/a.md"} <= set(body)
    assert sorted(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.split()
                  ) == ["docs/a*b.md", "docs/a.md"]
    assert _tracked_clean(repo)
    assert {p: (repo / p).read_bytes() for p in untracked} == untracked
    assert _git(repo, "status", "--porcelain").stdout.count("??") == 2


def test_resume_on_clean_checkout_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "untracked.txt", "u\n")
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["action"], out["escalate"]) == (True, "resume", False)
    assert "error" not in out and _snap(repo) == before


def _hook(repo, name, body):
    h = Path(_git(repo, "rev-parse", "--git-path", "hooks").stdout.strip())
    h = h if h.is_absolute() else repo / h
    h.mkdir(exist_ok=True)
    (h / name).write_text("#!/bin/sh\n" + body)
    (h / name).chmod(0o755)


def test_failing_pre_commit_hook_restores_index_bytes_and_mode(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    _hook(repo, "pre-commit", "echo hook-said-no >&2\nexit 1\n")
    os.chmod(_index_path(repo), 0o640)
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "hook-said-no" in out["error"] and "sha" not in out
    assert _snap(repo) == before
    assert stat.S_IMODE(os.stat(_index_path(repo)).st_mode) == 0o640
    assert not (_index_path(repo).parent / "index.lock").exists()


def test_file_modified_between_plan_and_add_is_refused(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    real = MD._stage

    def racing(root, paths):
        _put(repo, "docs/a.md", "# T\n\nchanged after the hash\n")
        return real(root, paths)

    monkeypatch.setattr(MD, "_stage", racing)
    before = _snap(repo)
    before["files"]["docs/a.md"] = b"# T\n\nchanged after the hash\n"
    head = _git(repo, "rev-parse", "HEAD").stdout
    index = _index_path(repo).read_bytes()
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, "changed since plan docs/a.md")
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _index_path(repo).read_bytes() == index
    assert _snap(repo) == before


def _wrap_commit(monkeypatch, wrapper):
    real = MD._lg

    def lg(root, *args, check=True, **kw):
        if args and args[0] == "commit":
            return wrapper(real, root, *args, check=check, **kw)
        return real(root, *args, check=check, **kw)

    monkeypatch.setattr(MD, "_lg", lg)


def test_restore_index_locked(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    lock = _index_path(repo).parent / "index.lock"

    def holder(real, root, *args, check, **kw):       # a concurrent git process takes the lock
        lock.write_text("holder")
        return real(root, *args, check=check, **kw)

    _wrap_commit(monkeypatch, holder)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, "index locked, not restored")
    assert "sha" not in out and lock.read_text() == "holder"


def test_restore_skipped_when_head_moved(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)

    def lands_then_fails(real, root, *args, check, **kw):
        real(root, *args, check=check, **kw)
        return subprocess.CompletedProcess(args, 1, b"", b"post-commit fault")

    _wrap_commit(monkeypatch, lands_then_fails)
    head0 = _git(repo, "rev-parse", "HEAD").stdout.strip()
    index0 = _index_path(repo).read_bytes()
    out = MD.apply_clear(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head != head0 and out["ok"] is False and out["sha"] == head
    assert "post-commit fault" in out["error"]
    assert _index_path(repo).read_bytes() != index0 and _tracked_clean(repo)


def test_commit_lands_but_main_dirty(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    _hook(repo, "post-commit", "echo extra >> docs/a.md\n")
    index0 = _index_path(repo).read_bytes()
    out = MD.apply_clear(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert (out["ok"], out["sha"], out["error"]) == (False, head, "main not tracked-clean after commit")
    assert not _tracked_clean(repo)
    assert _index_path(repo).read_bytes() != index0                  # the pre-add index is not restored
    assert _git(repo, "write-tree").stdout.strip() == _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


def test_staged_names_must_equal_the_planned_paths(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    real = MD._stage

    def greedy(root, paths):                              # an unplanned file gets staged too
        _put(repo, "docs/new.md", "# new\n")
        _git(repo, "add", "docs/new.md")
        return real(root, paths)

    monkeypatch.setattr(MD, "_stage", greedy)
    head = _git(repo, "rev-parse", "HEAD").stdout
    index = _index_path(repo).read_bytes()
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, "changed since plan docs/new.md")
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _index_path(repo).read_bytes() == index


def test_failed_git_add_reports_gits_error_and_restores_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    monkeypatch.setattr(MD, "_stage", lambda root, paths: subprocess.CompletedProcess(
        paths, 1, b"", b"fatal: add exploded"))
    restored = []
    monkeypatch.setattr(MD, "_restore_index", lambda *a: restored.append(a))
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, "fatal: add exploded") and not restored


def test_add_under_a_held_index_lock_shows_gits_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    lock = _index_path(repo).parent / "index.lock"
    real = MD._stage

    def holder(root, paths):                              # a concurrent git takes the lock first
        lock.write_text("holder")
        return real(root, paths)

    monkeypatch.setattr(MD, "_stage", holder)
    index = _index_path(repo).read_bytes()
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "index.lock" in out["error"]
    assert out["error"] != "index locked, not restored"
    assert lock.read_text() == "holder" and _index_path(repo).read_bytes() == index


def test_restore_index_os_error_branch(tmp_path):
    err = MD._restore_index(str(tmp_path / "no-such-dir" / "index"), b"x", 0o644)
    assert err and err.startswith("index not restored: ")
    idx = tmp_path / "index"
    idx.write_bytes(b"old")
    assert MD._restore_index(str(idx), b"new", 0o640) is None
    assert idx.read_bytes() == b"new" and stat.S_IMODE(idx.stat().st_mode) == 0o640
    assert not (tmp_path / "index.lock").exists()


def test_commit_timeout_does_not_restore_under_a_running_hook(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    seen = {}

    def times_out(real, root, *args, check, timeout=None, **kw):
        seen["timeout"] = timeout
        raise MD.PlanError("git commit: timed out after 1800 seconds")

    _wrap_commit(monkeypatch, times_out)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and out["error"].endswith("; index not restored")
    assert seen["timeout"] and seen["timeout"] > 120          # hooks get more than the 120 s default
    assert "docs/a.md" in _git(repo, "diff", "--cached", "--name-only").stdout


def test_resume_not_tracked_clean_says_why(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(MD, "_tracked_clean", lambda root: False)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["action"], out["error"]) == (False, "resume", "main not tracked-clean")


def test_commit_paths_with_space_and_non_ascii(tmp_path):
    repo = _repo(tmp_path, {"docs/my file.md": "# a\n", "docs/caf\u00e9.md": "# b\n"})
    _put(repo, "docs/my file.md", "# a\n\nx\n")
    _put(repo, "docs/caf\u00e9.md", "# b\n\nx\n")
    out = MD.apply_clear(repo)
    assert out["ok"] is True and _tracked_clean(repo)
    names = _git(repo, "-c", "core.quotepath=off", "diff-tree", "--no-commit-id", "--name-only",
                 "-r", "HEAD").stdout.split("\n")
    assert {"docs/my file.md", "docs/caf\u00e9.md"} <= set(names)


def test_commit_in_a_linked_worktree(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "switch", "-qc", "other")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "main")
    _edit_trivial(linked)
    assert not (linked / ".git").is_dir()                      # `.git` is a file here
    index = _index_path(linked)
    assert ".git/index" not in str(index) or "worktrees" in str(index)
    out = MD.apply_clear(linked)
    head = _git(linked, "rev-parse", "HEAD").stdout.strip()
    assert (out["ok"], out["sha"]) == (True, head) and _tracked_clean(linked)
    # and a failed commit there restores that worktree's own index
    _edit_trivial(linked, "README.md")
    _hook(linked, "pre-commit", "exit 1\n")
    before = _index_path(linked).read_bytes()
    out = MD.apply_clear(linked)
    assert out["ok"] is False and _index_path(linked).read_bytes() == before


# ---- AC3: refusals with no write --------------------------------------------------------------

def _sc_symlink_target(tmp_path, mp):
    repo = _repo(tmp_path)
    os.symlink("a.md", repo / "docs/link.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "link")
    (repo / "docs/link.md").unlink()
    os.symlink("elsewhere.md", repo / "docs/link.md")
    return repo, "bad path docs/link.md", False


def _sc_file_to_symlink(tmp_path, mp):
    repo = _repo(tmp_path)
    (repo / "docs/a.md").unlink()
    os.symlink("README.md", repo / "docs/a.md")
    return repo, "bad path docs/a.md", False


def _sc_symlink_to_file(tmp_path, mp):
    repo = _repo(tmp_path)
    os.symlink("a.md", repo / "docs/link.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "link")
    (repo / "docs/link.md").unlink()
    _put(repo, "docs/link.md", "now a file\n")
    return repo, "bad path docs/link.md", False


def _sc_symlinked_parent_real(tmp_path, mp):
    repo = _repo(tmp_path, {"d/x.md": "# x\n"})
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "x.md").write_text("# x\n")
    import shutil
    shutil.rmtree(repo / "d")
    os.symlink(other, repo / "d")
    return repo, "bad path d/x.md", False


def _sc_gitlink(tmp_path, mp):
    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
    _git(repo, "commit", "-qm", "gitlink")
    sub = repo / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    _git(sub, "config", "user.email", "t@t")
    _git(sub, "config", "user.name", "t")
    _git(sub, "commit", "-q", "--allow-empty", "-m", "inner")
    assert _git(repo, "status", "--porcelain").stdout.startswith(" M sub")
    return repo, "bad path sub", False


def _patched(path):
    def build(tmp_path, mp):
        repo = _repo(tmp_path)
        _edit_trivial(repo)
        live = MD.plan_clear(repo)
        mp.setattr(MD, "plan_clear",
                   lambda root, base=None: {**live, "paths": [path], "action": "commit"})
        return repo, f"bad path {path}", False
    return build


def _sc_deleted_star(tmp_path, mp):
    repo = _repo(tmp_path, {"docs/a*b.md": "# T\n"})
    (repo / "docs/a*b.md").unlink()
    return repo, None, False


BAD_PATHS = {"symlink-target": _sc_symlink_target, "file-to-symlink": _sc_file_to_symlink,
             "symlink-to-file": _sc_symlink_to_file, "symlinked-parent": _sc_symlinked_parent_real,
             "gitlink": _sc_gitlink, "absolute": _patched("/etc/passwd"),
             "dotdot": _patched("docs/../README.md"), "in-neither": _patched("docs/ghost.md")}


@pytest.mark.parametrize("name", sorted(BAD_PATHS))
def test_refusal_bad_paths(tmp_path, monkeypatch, name):
    repo, error, escalate = BAD_PATHS[name](tmp_path, monkeypatch)
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"], out["escalate"]) == (False, error, escalate)
    assert _snap(repo) == before


def test_bad_path_symlinked_parent_in_head_only(tmp_path):
    """`d` is a symlink in HEAD but a real directory in the worktree: `d/x.md` is still unsafe."""
    repo = _repo(tmp_path)
    os.symlink("docs", repo / "d")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "d is a symlink")
    (repo / "d").unlink()
    (repo / "d").mkdir()
    _put(repo, "d/x.md", "# x\n")
    assert not (repo / "d").is_symlink()
    assert MD._bad_path(repo, "d/x.md") is True
    assert MD._bad_path(repo, "docs/a.md") is False


def test_deleted_path_with_a_star_is_not_a_bad_path(tmp_path, monkeypatch):
    repo, error, _ = _sc_deleted_star(tmp_path, monkeypatch)
    plan = MD.plan_clear(repo)
    assert (plan["action"], plan["paths"]) == ("save_branch", ["docs/a*b.md"])
    assert MD.verb_refusal(repo, plan) is None
    out = MD.apply_clear(repo)                    # SPEC-0549: guards pass and the save runs
    assert (out["ok"], out["escalate"], "error" in out) == (True, False, False)
    assert _tracked_clean(repo)


def _sc_partial(tmp_path, mp):
    repo = _repo(tmp_path)
    _put(repo, "docs/a.md", "# T\n\nstaged\n")
    _git(repo, "add", "docs/a.md")
    _put(repo, "docs/a.md", "# T\n\nstaged\n\nand more\n")
    return repo, "partially staged docs/a.md", False


def test_refusal_partially_staged(tmp_path, monkeypatch):
    repo, error, _ = _sc_partial(tmp_path, monkeypatch)
    plan = MD.plan_clear(repo)
    assert (plan["class"], plan["action"]) == ("trivial", "commit")
    assert MD.verb_refusal(repo, plan) == error
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"], out["escalate"]) == (False, error, False)
    assert _snap(repo) == before


def test_fully_staged_edit_is_committed(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    _git(repo, "add", "docs/a.md")
    assert MD.apply_clear(repo)["ok"] is True and _tracked_clean(repo)


def test_refusal_config_and_lock(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, ".nw/project.yaml", "project: [unclosed\n")
    out = MD.apply_clear(repo)
    assert (out["ok"], out["action"], out["escalate"]) == (False, "none", False)
    assert out["error"]
    repo2 = _repo(tmp_path, name="r2")
    _edit_trivial(repo2)
    gdir = _git(repo2, "rev-parse", "--absolute-git-dir").stdout.strip()
    before = _snap(repo2)
    with open(os.path.join(gdir, "nw-main-dirty.lock"), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = MD.apply_clear(repo2)
    assert (out["ok"], out["action"], out["escalate"], out["error"]) == (
        False, "none", False, "clear already in progress")
    assert _snap(repo2) == before


def _on_feature(repo):
    _git(repo, "switch", "-qc", "feat")


def _sc_save_no_config(tmp_path, mp):
    fenced = "# T\n\n```\ncode\n```\n"
    repo = _repo(tmp_path, {"docs/f.md": fenced}, project=False)
    _put(repo, "docs/f.md", fenced.replace("code", "code2"))
    return repo, "no project config to place the WIP worktree", False


def _sc_substantive_no_project_key(tmp_path, mp):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n"})
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1"}))
    _put(repo, "src/x.py", "x = 2\n")
    return repo, "no project config to place the WIP worktree", True


def _sc_bad_project(tmp_path, mp):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n"})
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "../x"}))
    _put(repo, "src/x.py", "x = 2\n")
    return repo, "bad project name", True


def _sc_hold_bad_base(tmp_path, mp):
    repo = _repo(tmp_path)
    _on_feature(repo)
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                   "base": "ghost"}))
    _edit_trivial(repo)
    return repo, "bad base ghost", False


SAVES = {"save-no-config": _sc_save_no_config, "no-project-key": _sc_substantive_no_project_key,
         "bad-project": _sc_bad_project, "hold-bad-base": _sc_hold_bad_base}


@pytest.mark.parametrize("name", sorted(SAVES))
def test_save_refusals_write_nothing(tmp_path, monkeypatch, name):
    repo, error, escalate = SAVES[name](tmp_path, monkeypatch)
    _put(repo, "untracked.txt", "u\n")
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"], out["escalate"]) == (False, error, escalate)
    assert "sha" not in out and "wip_branch" not in out
    assert _snap(repo) == before
    assert not (tmp_path / ".nw-worktrees").exists()


def _sc_merge(tmp_path, mp):
    repo = _repo(tmp_path)
    _real_conflict(repo)
    return repo, "merge in progress", True


def _sc_config(tmp_path, mp):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    _put(repo, ".nw/project.yaml", "project: [unclosed\n")
    return repo, None, False


def _sc_lock(tmp_path, mp):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    return repo, "clear already in progress", False


def _sc_stale_plan(tmp_path, mp):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    return repo, "plan is stale", False


REFUSALS = {**BAD_PATHS, **SAVES, "partial": _sc_partial, "merge": _sc_merge,
            "config": _sc_config, "lock": _sc_lock,
            "stale-plan": _sc_stale_plan}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_refusals_leave_index_bytes(tmp_path, monkeypatch, name):
    """The guards run with GIT_OPTIONAL_LOCKS=0: a repo with stale stat data keeps its index."""
    repo, error, _ = REFUSALS[name](tmp_path, monkeypatch)
    for f in ("README.md", "docs/a.md"):                       # stale stat data: git would refresh
        if (repo / f).is_file() and not (repo / f).is_symlink():
            os.utime(repo / f, (1_000_000_000, 1_000_000_000))
    index = _index_path(repo).read_bytes()
    plan = ({"schema": MD.SCHEMA, "action": "commit", "paths": ["docs/other.md"]}
            if name == "stale-plan" else None)
    if name == "lock":
        gdir = _git(repo, "rev-parse", "--absolute-git-dir").stdout.strip()
        with open(os.path.join(gdir, "nw-main-dirty.lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = MD.apply_clear(repo)
    else:
        out = MD.apply_clear(repo, plan=plan)
    assert out["ok"] is False
    if error:
        assert out["error"] == error
    assert _index_path(repo).read_bytes() == index


# ---- AC4: argv, untracked bytes ---------------------------------------------------------------

def _record(monkeypatch):
    calls = []
    real = subprocess.run

    def rec(argv, *a, **kw):
        calls.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(MD.subprocess, "run", rec)
    return calls


def _verbs(calls):
    return [next(t for t in c[3:] if not t.startswith("-")) for c in calls]   # after `-C <root>`


def _assert_safe_argv(calls):
    """SPEC-0549's never-run list: `reset`, `restore`, `clean`, `stash`, `push`, `checkout`
    without `HEAD --`, `switch -f` / `--discard-changes` / `-C`, `branch -D`, `rm -r`, no `nw`."""
    assert calls and all(c[0] == "git" for c in calls)            # never `nw`
    verbs = _verbs(calls)
    assert not {"reset", "restore", "clean", "stash", "push"} & set(verbs)
    for v, c in zip(verbs, calls):
        rest = c[c.index(v) + 1:]                                 # after the verb, `_lg` flags before it
        assert not (v == "checkout" and rest[:2] != ["HEAD", "--"]), c
        assert not (v == "switch" and (rest[:1] != ["--no-guess"] or any(
            a in ("-f", "--force", "--discard-changes", "-C", "--force-create") for a in rest))), c
        assert not (v == "branch" and any(a.startswith("-D") or a == "--delete" for a in rest)), c
        assert not (v == "rm" and any(a in ("-r", "-R") or (a.startswith("-") and "r" in a[1:])
                                      for a in rest if not a.startswith("--"))), c
        assert not (v == "rm" and "--recursive" in rest), c
        assert not (v == "commit" and any(a == "--all" or (a.startswith("-") and not a.startswith("--")
                                                           and "a" in a[1:]) for a in rest
                                          if not a.startswith("-m"))), c
        assert not (v == "add" and any(a in ("-A", "--all", ".", "-u", "--update") for a in rest)), c
        if v in {"add", "commit", "hash-object", "ls-files", "ls-tree", "diff", "rm", "checkout",
                 "update-index"}:
            assert "--literal-pathspecs" in c, c
        if v in {"add", "hash-object", "ls-files", "ls-tree", "rm", "checkout"}:
            assert "--" in c[c.index(v) + 1:], c                  # paths only after `--`


@pytest.mark.parametrize("argv", [
    ["git", "-C", "/r", "--literal-pathspecs", "rm", "-r", "--", "x"],
    ["git", "-C", "/r", "--literal-pathspecs", "branch", "-D", "x"],
    ["git", "-C", "/r", "--literal-pathspecs", "commit", "-am", "x"],
    ["git", "-C", "/r", "--literal-pathspecs", "commit", "--all"],
    ["git", "-C", "/r", "--literal-pathspecs", "add", "-A"],
    ["git", "-C", "/r", "--literal-pathspecs", "add", "--", "."],
    ["git", "-C", "/r", "--literal-pathspecs", "add", "--all"],
    ["git", "-C", "/r", "--literal-pathspecs", "add", "x"],
    ["git", "-C", "/r", "--literal-pathspecs", "hash-object", "x"],
    ["git", "-C", "/r", "--literal-pathspecs", "reset", "--hard"],
    ["git", "-C", "/r", "--literal-pathspecs", "restore", "--", "x"],
    ["git", "-C", "/r", "--literal-pathspecs", "clean", "-fd"],
    ["git", "-C", "/r", "--literal-pathspecs", "stash"],
    ["git", "-C", "/r", "--literal-pathspecs", "checkout", "--", "."],
    ["git", "-C", "/r", "--literal-pathspecs", "checkout", "."],
    ["git", "-C", "/r", "--literal-pathspecs", "checkout", "--", "x"],
    ["git", "-C", "/r", "rm", "-f", "x"],                         # no `--literal-pathspecs`, no `--`
    ["git", "-C", "/r", "switch", "-f", "main"],
    ["git", "-C", "/r", "switch", "--no-guess", "--discard-changes", "main"],
    ["git", "-C", "/r", "switch", "-C", "main"],
    ["git", "-C", "/r", "switch", "main"],
    ["git", "-C", "/r", "push"],
])
def test_assert_safe_argv_catches_banned_forms_behind_literal_pathspecs(argv):
    with pytest.raises(AssertionError):
        _assert_safe_argv([argv])


def test_recorded_git_argv(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    _put(repo, "untracked.txt", "u\n")
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    assert out["ok"] is True and (repo / "untracked.txt").read_bytes() == b"u\n"
    _assert_safe_argv(calls)
    assert {"add", "commit", "hash-object", "ls-files"} <= set(_verbs(calls))


@pytest.mark.parametrize("scenario", ["hook-failure", "resume", "refusal", "changed-since-plan"])
def test_recorded_git_argv_on_other_paths(tmp_path, monkeypatch, scenario):
    repo = _repo(tmp_path)
    if scenario == "hook-failure":
        _edit_trivial(repo)
        _hook(repo, "pre-commit", "exit 1\n")
    elif scenario == "refusal":
        _real_conflict(repo)
    elif scenario == "changed-since-plan":
        _edit_trivial(repo)
        real = MD._stage

        def racing(root, paths):
            _put(repo, "docs/a.md", "# T\n\nchanged\n")
            return real(root, paths)

        monkeypatch.setattr(MD, "_stage", racing)
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    assert out["ok"] is (scenario == "resume")
    _assert_safe_argv(calls)
    if scenario in ("hook-failure", "changed-since-plan"):
        assert "add" in _verbs(calls)           # it staged, so the restore path really ran


# ---- AC5: the CLI -----------------------------------------------------------------------------

def test_cli_json_exit_zero_on_success_and_refusal(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    r = _invoke("--root", str(repo), "--json")
    doc = json.loads(r.output)
    assert r.exit_code == 0 and doc["schema"] == "nw.main_dirty_apply.v1" and doc["ok"] is True
    assert doc["escalate"] is False and doc["sha"] == _git(repo, "rev-parse", "HEAD").stdout.strip()
    _gitdir(repo, "MERGE_HEAD").write_text(doc["sha"])
    r = _invoke("--root", str(repo), "--json")
    doc = json.loads(r.output)
    assert r.exit_code == 0 and doc["ok"] is False and doc["escalate"] is True


def test_cli_text_mode_exit_codes(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    r = _invoke("--root", str(repo))
    assert r.exit_code == 0 and r.output.splitlines()[0] == "main-dirty-apply ok: commit"
    assert "sha: " in r.output
    _gitdir(repo, "MERGE_HEAD").write_text(_git(repo, "rev-parse", "HEAD").stdout)
    r = _invoke("--root", str(repo))
    assert r.exit_code == 1
    assert r.output.splitlines() == ["main-dirty-apply failed: escalate", "error: merge in progress"]


def _plan_file(tmp_path, content):
    p = tmp_path / "plan.json"
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    return str(p)


@pytest.mark.parametrize("plan", [
    {"schema": MD.SCHEMA, "action": "commit", "paths": 5},
    {"a": 1}, [None], '"just a string"', "{not json",
    {"schema": MD.SCHEMA, "action": "commit", "paths": [None]},
    {"schema": MD.SCHEMA, "action": "commit", "paths": {"docs/a.md": 1}},
    {"schema": MD.SCHEMA, "action": "commit", "paths": "docs/a.md"},
    {"schema": MD.SCHEMA, "action": 5, "paths": []},
    {"schema": "other.v1", "action": "commit", "paths": []},
])
def test_cli_malformed_plan_exits_2(tmp_path, plan):
    repo = _repo(tmp_path)
    before = _snap(repo)
    r = _invoke("--root", str(repo), "--plan", _plan_file(tmp_path, plan))
    assert r.exit_code == 2 and r.output.startswith("error:") and "Traceback" not in r.output
    assert len(r.output.strip().splitlines()) == 1
    assert _snap(repo) == before


def test_cli_non_utf8_hook_output_still_gives_a_verdict(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    _hook(repo, "pre-commit", "printf 'lint \\351 bad\\n\\033[31mred\\n' >&2\nexit 1\n")
    index = _index_path(repo).read_bytes()
    r = _invoke("--root", str(repo), "--json")
    assert r.exit_code == 0 and r.exception is None
    doc = json.loads(r.output)
    assert doc["ok"] is False and "lint \ufffd bad" in doc["error"]
    assert _index_path(repo).read_bytes() == index
    r = _invoke("--root", str(repo))                          # text mode: one line, no raw ESC/newline
    assert r.exit_code == 1
    assert "\x1b" not in r.output and "Traceback" not in r.output
    assert r.output.splitlines()[0] == "main-dirty-apply failed: commit"
    assert len(r.output.splitlines()) == 2


def test_cli_non_utf8_path_gives_a_verdict(tmp_path):
    repo = _repo(tmp_path)
    bad = os.fsencode(repo) + b"/docs/bad\xe9.md"
    with open(bad, "wb") as f:
        f.write(b"# x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "non-utf8 name")
    os.unlink(bad)
    os.symlink("README.md", bad)                              # typechange: `bad path <p>`
    r = _invoke("--root", str(repo), "--json")
    assert r.exception is None
    doc = json.loads(r.output)
    assert doc["ok"] is False and doc["error"] == "bad path docs/bad\ufffd.md"
    r = _invoke("--root", str(repo))
    assert r.exit_code == 1
    assert "bad path docs/bad\ufffd.md" in r.output


def test_cli_missing_plan_file_exits_2(tmp_path):
    repo = _repo(tmp_path)
    r = _invoke("--root", str(repo), "--plan", str(tmp_path / "nope.json"))
    assert r.exit_code == 2 and r.output.startswith("error:") and "Traceback" not in r.output


def test_cli_good_plan_file_and_stale_plan_file(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    plan = json.loads(CliRunner().invoke(cli, ["queue", "main-dirty-plan", "--root", str(repo),
                                                "--json"]).output)
    stale = {**plan, "paths": ["docs/other.md"]}
    r = _invoke("--root", str(repo), "--plan", _plan_file(tmp_path, stale), "--json")
    assert json.loads(r.output)["error"] == "plan is stale"
    r = _invoke("--root", str(repo), "--plan", _plan_file(tmp_path, plan), "--json")
    assert r.exit_code == 0 and json.loads(r.output)["ok"] is True


def test_cli_non_git_and_subdirectory_roots_exit_2(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = _invoke("--root", str(plain))
    assert r.exit_code == 2 and r.output.startswith("error:") and "Traceback" not in r.output
    repo = _repo(tmp_path)
    r = _invoke("--root", str(repo / "docs"))
    assert r.exit_code == 2 and "not the top" in r.output


def test_cli_non_git_exit_2_holds_in_a_non_english_locale(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = _invoke("--root", str(plain), env={"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8",
                                           "LANGUAGE": "de"})
    assert r.exit_code == 2


def test_cli_exit_3_when_rev_parse_fails_for_another_reason(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    real = subprocess.run

    def run(argv, *a, **kw):
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: detected dubious ownership")
        return real(argv, *a, **kw)

    monkeypatch.setattr(MD.subprocess, "run", run)
    r = _invoke("--root", str(repo))
    assert r.exit_code == 3 and "dubious ownership" in r.output


def test_cli_git_calls_run_with_lc_all_c(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    seen = []
    real = subprocess.run

    def run(argv, *a, **kw):
        seen.append((kw.get("env") or {}).get("LC_ALL"))
        return real(argv, *a, **kw)

    monkeypatch.setattr(MD.subprocess, "run", run)
    _invoke("--root", str(repo), "--json", env={"LC_ALL": "de_DE.UTF-8"})
    assert seen and set(seen) == {"C"}


def test_cli_base_reaches_plan_clear(tmp_path):
    repo = _repo(tmp_path, base="develop", project=False)
    _edit_trivial(repo)
    r = _invoke("--root", str(repo), "--base", "develop", "--json")
    doc = json.loads(r.output)
    assert (doc["ok"], doc["action"]) == (True, "commit")
    repo2 = _repo(tmp_path, base="develop", project=False, name="r2")
    _edit_trivial(repo2)
    doc = json.loads(_invoke("--root", str(repo2), "--json").output)
    assert (doc["ok"], doc["error"]) == (False, "bad base main")


def test_cli_base_forwarded_to_plan_clear(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    seen = []
    real = MD.plan_clear
    monkeypatch.setattr(MD, "plan_clear", lambda root, base=None: (seen.append(base), real(root, base=base))[1])
    _invoke("--root", str(repo), "--base", "develop", "--json")
    _invoke("--root", str(repo), "--json")
    assert seen == ["develop", None]


# ---- AC6: `recommended` for commit ------------------------------------------------------------

def test_recommended_base_suffix_and_quoting(tmp_path):
    repo = _repo(tmp_path, base="develop", project=False, name="my repo")
    _edit_trivial(repo)
    R = shlex.quote(str(repo))
    assert " " in str(repo) and R.startswith("'")
    p = MD.plan_clear(repo, base="develop")
    assert p["recommended"] == [f"nw queue main-dirty-apply --root {R} --base develop"]
    p = MD.plan_clear(repo)
    assert p["action"] == "hold"                                    # main base vs develop checkout
    repo2 = _repo(tmp_path, name="other repo")
    _edit_trivial(repo2)
    assert MD.plan_clear(repo2)["recommended"] == [
        f"nw queue main-dirty-apply --root {shlex.quote(str(repo2))}"]
    # the quoted command round-trips through the CLI
    argv = shlex.split(MD.plan_clear(repo, base="develop")["recommended"][0])
    r = CliRunner().invoke(cli, argv[1:] + ["--json"])
    assert json.loads(r.output)["ok"] is True


def test_recommended_raw_when_partially_staged(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "docs/a.md", "# T\n\nstaged\n")
    _git(repo, "add", "docs/a.md")
    _put(repo, "docs/a.md", "# T\n\nstaged\n\nmore\n")
    p = MD.plan_clear(repo)
    R = shlex.quote(str(repo))
    assert p["action"] == "commit"
    assert p["recommended"] == [f"git -C {R} --literal-pathspecs add -- docs/a.md",
                                f"git -C {R} commit -m '{SUBJECT}'"]


def test_recommended_raw_when_monkeypatched_symlink_parent(tmp_path, monkeypatch):
    repo, _, _ = _sc_symlinked_parent_real(tmp_path, monkeypatch)
    monkeypatch.setattr(MD, "_classify", lambda root, row, raws: "docs")   # force `commit`
    p = MD.plan_clear(repo)
    R = shlex.quote(str(repo))
    assert (p["action"], p["paths"]) == ("commit", ["d/x.md"])
    assert MD.verb_refusal(repo, p) == "bad path d/x.md"
    assert p["recommended"] == [f"git -C {R} --literal-pathspecs add -- d/x.md",
                                f"git -C {R} commit -m '{SUBJECT}'"]


# ---- AC7: skills ------------------------------------------------------------------------------

def test_skills_name_the_verb_for_the_steward_only():
    steward = (REPO_ROOT / "skills/nw-steward/SKILL.md").read_text(encoding="utf-8")
    conduct = (REPO_ROOT / "skills/nw-conduct/SKILL.md").read_text(encoding="utf-8")
    assert "nw queue main-dirty-apply --root" in steward
    assert "main-dirty-apply" not in conduct


# ---- review loop 2: caught classes, interrupted commit, guard faults, bounded git calls -------

@pytest.mark.parametrize("exc", [MD.PlanError("git broke"), OSError("disk"), ValueError("bad"),
                                 TypeError("typed"), AttributeError("attr"), MD.NotAWorktree("nope")])
def test_apply_clear_turns_each_caught_class_into_json(tmp_path, monkeypatch, exc):
    repo = _repo(tmp_path)
    _edit_trivial(repo)

    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(MD, "plan_clear", boom)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["action"], out["escalate"]) == (False, "none", False)
    assert isinstance(out["error"], str) and out["error"]
    assert out["schema"] == "nw.main_dirty_apply.v1"


@pytest.mark.parametrize("plan", [5, "commit", ["a"]])
def test_apply_clear_non_dict_plan_is_ok_false_not_a_traceback(tmp_path, plan):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    head, index = _git(repo, "rev-parse", "HEAD").stdout, _index_path(repo).read_bytes()
    out = MD.apply_clear(repo, plan=plan)
    assert out["ok"] is False and out["error"]
    assert _git(repo, "rev-parse", "HEAD").stdout == head and _index_path(repo).read_bytes() == index


@pytest.mark.parametrize("exc", [ValueError("odd ls-files record"), KeyboardInterrupt()])
def test_unexpected_exception_after_add_restores_the_index(tmp_path, monkeypatch, exc):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    before = _snap(repo)

    def boom(real, root, *args, check, **kw):             # raised from inside `git commit`
        raise exc

    _wrap_commit(monkeypatch, boom)
    if isinstance(exc, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            MD.apply_clear(repo)
    else:
        out = MD.apply_clear(repo)
        assert out["ok"] is False and "odd ls-files record" in out["error"]
    assert _snap(repo) == before


def test_value_error_between_add_and_commit_restores_the_index(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    before = _snap(repo)
    real_stage = MD._stage

    def stage_then_break(root, paths):
        r = real_stage(root, paths)
        monkeypatch.setattr(MD, "_index_map", lambda *a, **k: (_ for _ in ()).throw(ValueError("x y")))
        return r

    monkeypatch.setattr(MD, "_stage", stage_then_break)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and out["error"] == "x y"
    monkeypatch.undo()
    assert _snap(repo) == before


@pytest.mark.parametrize("exc", [MD.PlanError("git broke"), OSError("disk"), ValueError("bad")])
def test_plan_clear_keeps_the_raw_list_when_the_guard_faults(tmp_path, monkeypatch, exc):
    repo = _repo(tmp_path)
    _edit_trivial(repo)

    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(MD, "verb_refusal", boom)
    p = MD.plan_clear(repo)
    R = shlex.quote(str(repo))
    assert p["action"] == "commit"
    assert p["recommended"] == [f"git -C {R} --literal-pathspecs add -- docs/a.md",
                                f"git -C {R} commit -m '{SUBJECT}'"]
    r = CliRunner().invoke(cli, ["queue", "main-dirty-plan", "--root", str(repo), "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["recommended"] == p["recommended"]


def test_large_trivial_plan_runs_a_bounded_number_of_git_calls(tmp_path, monkeypatch):
    files = {f"docs/d{i % 20}/s{i % 7}/f{i}.md": f"# {i}\n" for i in range(200)}
    repo = _repo(tmp_path, files=files)
    for rel in files:
        _put(repo, rel, f"# {rel}\n\nedited\n")
    calls = _record(monkeypatch)
    plan = MD.plan_clear(repo)
    assert plan["action"] == "commit" and len(plan["paths"]) == 200
    assert plan["recommended"] == [f"nw queue main-dirty-apply --root {shlex.quote(str(repo))}"]
    guard = [v for v in _verbs(calls) if v in ("ls-tree", "ls-files", "hash-object")]
    assert 0 < len(guard) <= 8, guard                      # the guard is not per path
    calls.clear()
    out = MD.apply_clear(repo)
    assert out["ok"] is True and _tracked_clean(repo)
    mine = [v for v in _verbs(calls) if v in ("ls-tree", "ls-files", "hash-object", "add", "commit")]
    assert len(mine) <= 24, mine      # the classifier's own per-path `cat-file` (SPEC-0546) is not counted
    assert len(_git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()) == 200
    _assert_safe_argv(calls)


# ---- review loop 3 ----------------------------------------------------------------------------

def test_dotdot_fixture_reaches_an_existing_file(tmp_path, monkeypatch):
    """Only the `..` rule can refuse it: the file exists, so the 'in neither' rule does not."""
    repo, error, _ = _patched("docs/../README.md")(tmp_path, monkeypatch)
    assert (repo / "docs/../README.md").exists() and error == "bad path docs/../README.md"
    monkeypatch.setattr(MD, "_structurally_bad", lambda path: not path)
    assert MD._bad_path(repo, "docs/../README.md") is False       # without the `..` clause it passes


def test_commit_path_starting_with_a_dash(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"-notes.md": "# n\n"})
    _put(repo, "-notes.md", "# n\n\nmore\n")
    monkeypatch.setattr(MD, "_classify", lambda root, row, raws: "docs")   # force `commit`
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    assert out["ok"] is True and _tracked_clean(repo)
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split() == ["-notes.md"]
    _assert_safe_argv(calls)


def test_batches_merge_across_chunks(tmp_path, monkeypatch):
    files = {f"docs/b{i}.md": f"# {i}\n" for i in range(10)}
    repo = _repo(tmp_path, files=files)
    for rel in files:
        _put(repo, rel, f"# {rel}\n\nedited\n")
    monkeypatch.setattr(MD, "_BATCH", 3)
    calls = _record(monkeypatch)
    plan = MD.plan_clear(repo)
    assert plan["action"] == "commit" and len(plan["paths"]) == 10
    assert MD.verb_refusal(repo, plan) is None
    assert _verbs(calls).count("hash-object") == 0                 # nothing suspect: all unstaged
    # a bad path in the last chunk is still found
    bad = {**plan, "paths": plan["paths"] + ["docs/ghost.md"]}
    assert MD.verb_refusal(repo, bad) == "bad path docs/ghost.md"
    calls.clear()
    out = MD.apply_clear(repo)
    assert out["ok"] is True and _tracked_clean(repo)
    assert sorted(_git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()) == sorted(files)
    assert _verbs(calls).count("ls-files") >= 8                    # chunks of 3, run more than once
    _assert_safe_argv(calls)


def test_batched_partial_staging_found_in_a_later_chunk(tmp_path, monkeypatch):
    files = {f"docs/b{i}.md": f"# {i}\n" for i in range(7)}
    repo = _repo(tmp_path, files=files)
    for rel in files:
        _put(repo, rel, f"# {rel}\n\nedited\n")
    _git(repo, "add", "docs/b6.md")
    _put(repo, "docs/b6.md", "# again\n")
    monkeypatch.setattr(MD, "_BATCH", 2)
    assert MD.verb_refusal(repo, MD.plan_clear(repo)) == "partially staged docs/b6.md"


def test_staged_new_file_is_committed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _put(repo, "docs/new.md", "# new\n")
    _git(repo, "add", "docs/new.md")
    # a new file is never `trivial` for the classifier; force `commit` to reach the HEAD-absent branch
    monkeypatch.setattr(MD, "_classify", lambda root, row, raws: "docs")
    plan = MD.plan_clear(repo)
    assert (plan["action"], plan["paths"]) == ("commit", ["docs/new.md"])
    assert MD.verb_refusal(repo, plan) is None
    out = MD.apply_clear(repo)
    assert out["ok"] is True and _tracked_clean(repo)
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split() == ["docs/new.md"]


def test_staged_new_file_modified_again_is_partially_staged(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _put(repo, "docs/new.md", "# new\n")
    _git(repo, "add", "docs/new.md")
    _put(repo, "docs/new.md", "# new\n\nmore\n")
    # a new file is never `trivial` for the classifier; force `commit` to reach the HEAD-absent branch
    monkeypatch.setattr(MD, "_classify", lambda root, row, raws: "docs")
    plan = MD.plan_clear(repo)
    assert (plan["action"], plan["paths"]) == ("commit", ["docs/new.md"])
    assert MD.verb_refusal(repo, plan) == "partially staged docs/new.md"
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, "partially staged docs/new.md")
    assert _snap(repo) == before


def test_recommended_base_suffix_with_a_project_config_and_a_quoted_base(tmp_path):
    repo = _repo(tmp_path, base="develop", name="proj repo")      # project.yaml names base develop
    _edit_trivial(repo)
    R = shlex.quote(str(repo))
    assert MD.plan_clear(repo, base="develop")["recommended"] == [
        f"nw queue main-dirty-apply --root {R} --base develop"]
    assert MD.plan_clear(repo)["recommended"] == [f"nw queue main-dirty-apply --root {R}"]
    odd = _repo(tmp_path, base="feat$x", project=False, name="odd repo")
    _edit_trivial(odd)
    cmd = MD.plan_clear(odd, base="feat$x")["recommended"][0]
    assert cmd.endswith(" --base 'feat$x'")
    assert shlex.split(cmd)[-2:] == ["--base", "feat$x"]


def test_sigterm_after_add_restores_the_index_and_the_handler(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    before = _snap(repo)
    real_stage = MD._stage
    old = signal.getsignal(signal.SIGTERM)

    def stage_then_term(root, paths):
        r = real_stage(root, paths)
        os.kill(os.getpid(), signal.SIGTERM)
        return r

    monkeypatch.setattr(MD, "_stage", stage_then_term)
    with pytest.raises(SystemExit) as e:
        MD.apply_clear(repo)
    assert e.value.code == 128 + signal.SIGTERM
    monkeypatch.undo()
    assert signal.getsignal(signal.SIGTERM) == old
    assert _snap(repo) == before
    assert not Path(str(_index_path(repo)) + ".lock").exists()


def test_a_second_interrupt_inside_the_restore_leaves_no_stale_lock(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    idx = str(_index_path(repo))

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(MD.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        MD._restore_index(idx, b"x", 0o644)
    assert not Path(idx + ".lock").exists()


def test_failing_head_lookup_in_fail_still_restores_the_index(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    before = _snap(repo)
    real_head, calls = MD._head, []

    def head(root):
        calls.append(1)
        if len(calls) > 1:                                    # the pre-add call works, later ones fail
            raise MD.PlanError("git rev-parse HEAD: timed out")
        return real_head(root)

    real_diff = MD._lg

    def lg(root, *args, check=True, **kw):
        if args and args[0] == "diff":                        # a PlanError after the add
            raise MD.PlanError("git diff: timed out")
        return real_diff(root, *args, check=check, **kw)

    monkeypatch.setattr(MD, "_head", head)
    monkeypatch.setattr(MD, "_lg", lg)
    out = MD.apply_clear(repo)
    monkeypatch.undo()
    assert out["ok"] is False and "timed out" in out["error"]
    assert _snap(repo) == before


def test_a_real_ls_tree_failure_is_not_read_as_no_head_entries(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    real = MD._lg

    def lg(root, *args, check=True, **kw):
        if args and args[0] == "ls-tree":
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: boom")
        return real(root, *args, check=check, **kw)

    monkeypatch.setattr(MD, "_lg", lg)
    with pytest.raises(MD.PlanError, match="ls-tree"):
        MD._head_map(repo, ["docs/a.md"])
    plan = {"action": "commit", "paths": ["docs/a.md"]}
    with pytest.raises(MD.PlanError):
        MD.verb_refusal(repo, plan)
    monkeypatch.undo()
    # an unborn HEAD is the one expected failure: no entries, no error
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-q", "-b", "main")
    assert MD._head_map(fresh, ["a.md"]) == {}


def test_cli_deeply_nested_plan_exits_2_without_a_traceback(tmp_path):
    repo = _repo(tmp_path)
    f = tmp_path / "deep.json"
    f.write_text("[" * 100000)
    r = _invoke("--root", str(repo), "--plan", str(f), "--json")
    assert r.exit_code == 2 and not isinstance(r.exception, RecursionError)
    assert "traceback" not in r.output.lower() and len(r.output.strip().splitlines()) == 1


# ---- SPEC-0549: save to a WIP branch ----------------------------------------------------------

import datetime  # noqa: E402
import types  # noqa: E402


def _worktrees(repo):
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [ln[9:] for ln in out.splitlines() if ln.startswith("worktree ")]


def _wips(repo):
    return _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/wip/").stdout.split()


def _wip_root(tmp_path):
    return tmp_path / ".nw-worktrees" / "T"


def _freeze(monkeypatch):
    class FixedDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(MD, "dt", types.SimpleNamespace(datetime=FixedDT, timezone=datetime.timezone))


def _mode(repo, rel):
    return "100755" if os.stat(Path(repo) / rel).st_mode & 0o111 else "100644"


def _ls(repo, rev, rel):
    out = _git(repo, "ls-tree", rev, "--", rel).stdout.split()
    return (out[0], out[2]) if out else None


def _blob_bytes(repo, rev, rel):
    return subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{rev}:{rel}"],
                          capture_output=True, check=True).stdout


def _saveable(tmp_path, files=None, name="r", **kw):
    return _repo(tmp_path, {"src/x.py": "x = 1\n", "config/a.json": "{}\n", **(files or {})},
                 name=name, **kw)


def _dirt_text(repo):
    _put(repo, "config/a.json", '{"a": 1}\n')


def _dirt_binary(repo):
    _put(repo, "bin/blob.dat", b"\x00\x01\xff" * 100)


def _dirt_deleted(repo):
    (repo / "config/a.json").unlink()


def _dirt_new_file(repo):
    _put(repo, "config/new.json", "{}\n")
    _git(repo, "add", "config/new.json")


def _dirt_mode_only(repo):
    os.chmod(repo / "config/a.json", 0o755)


def _dirt_new_exec(repo):
    _put(repo, "tools/run.sh", "#!/bin/sh\necho hi\n")
    os.chmod(repo / "tools/run.sh", 0o755)
    _git(repo, "add", "tools/run.sh")


def _dirt_exec_edit(repo):
    os.chmod(repo / "config/a.json", 0o755)
    _git(repo, "add", "config/a.json")
    _git(repo, "commit", "-qm", "exec")
    _put(repo, "config/a.json", '{"b": 2}\n')


DIRT = {"text": _dirt_text, "binary": _dirt_binary, "deleted": _dirt_deleted,
        "new": _dirt_new_file, "mode-only": _dirt_mode_only, "new-exec": _dirt_new_exec,
        "exec-edit": _dirt_exec_edit}


@pytest.mark.parametrize("kind", sorted(DIRT))
def test_save_writes_a_wip_commit_and_restores_main(tmp_path, kind):
    repo = _saveable(tmp_path, {"bin/seed.dat": b"\x00"})
    DIRT[kind](repo)
    if kind == "binary":                                     # tracked binary edit
        _put(repo, "bin/seed.dat", b"\x00\x01\xfe" * 50)
    _put(repo, "untracked.txt", "u\n")
    head = _git(repo, "rev-parse", "HEAD").stdout
    plan = MD.plan_clear(repo)
    assert plan["action"] in ("save_branch", "branch_and_escalate")
    want = {}
    for p in plan["paths"]:
        want[p] = (_mode(repo, p), (repo / p).read_bytes()) if (repo / p).exists() else None
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    assert out["escalate"] is (plan["class"] == "substantive") and out["action"] == plan["action"]
    wip, path, sha = out["wip_branch"], Path(out["wip_path"]), out["wip_sha"]
    assert re.fullmatch(r"wip/main-dirty-\d{8}T\d{6}Z", wip)
    assert path.parent == _wip_root(tmp_path) and path.name == wip.split("/")[1]
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == wip
    assert _git(repo, "rev-parse", wip).stdout.strip() == sha
    assert _git(repo, "log", "-1", "--format=%s", sha).stdout.strip() == \
        f"WIP: stray main-checkout edits ({plan['class']})"
    for p, exp in want.items():
        got = _ls(repo, sha, p)
        if exp is None:
            assert got is None, p
        else:
            assert got[0] == exp[0], p
            assert _blob_bytes(repo, sha, p) == exp[1], p
    assert _tracked_clean(repo) and _git(repo, "rev-parse", "HEAD").stdout == head
    assert _git(repo, "rev-parse", f"{sha}^").stdout == head
    assert (repo / "untracked.txt").read_bytes() == b"u\n"


def test_save_under_a_custom_worktrees_root(tmp_path):
    repo = _saveable(tmp_path)
    custom = tmp_path / "elsewhere"
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                   "worktrees": {"root": str(custom)}}))
    _git(repo, "commit", "-qam", "cfg")
    _dirt_text(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is True and Path(out["wip_path"]).parent == custom


@pytest.mark.parametrize("project", ["../x", ".", "..", "a\\b", "a\0b", " x ", "a/b"])
def test_bad_project_names_write_nothing(tmp_path, project):
    repo = _saveable(tmp_path)
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": project}))
    _put(repo, "untracked.txt", "u\n")
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, "bad project name")
    assert _snap(repo) == before and not (tmp_path / ".nw-worktrees").exists()


def test_empty_project_name_writes_nothing(tmp_path):
    repo = _saveable(tmp_path)
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": ""}))
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and out["error"]
    assert _snap(repo) == before and not (tmp_path / ".nw-worktrees").exists()


@pytest.mark.parametrize("worktrees", [["x"], "x", 3])
def test_malformed_worktrees_is_ok_false_json(tmp_path, worktrees):
    repo = _saveable(tmp_path)
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                   "worktrees": worktrees}))
    _dirt_text(repo)
    before = _snap(repo)
    r = _invoke("--root", str(repo), "--json")
    doc = json.loads(r.output)
    assert (r.exit_code, doc["ok"]) == (0, False) and doc["error"]
    assert _snap(repo) == before and not (tmp_path / ".nw-worktrees").exists()


def test_commit_still_works_without_a_project_config(tmp_path):
    repo = _repo(tmp_path, project=False)
    _edit_trivial(repo)
    assert MD.apply_clear(repo)["ok"] is True


def test_save_under_eol_conversion_stores_the_raw_bytes(tmp_path):
    repo = _repo(tmp_path, {".gitattributes": "* text=auto eol=crlf\n", "notes.txt": "a\r\nb\r\n"})
    raw = b"a\r\nb\nc\r\n"
    _put(repo, "notes.txt", raw)
    plan = MD.plan_clear(repo)
    assert plan["action"] == "save_branch"
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    assert _blob_bytes(repo, out["wip_sha"], "notes.txt") == raw
    assert _tracked_clean(repo)


# ---- ordering and argv ------------------------------------------------------------------------

def _save_calls(tmp_path, monkeypatch, **kw):
    repo = _saveable(tmp_path, {"a*b": "1\n", "axb": "1\n"})
    (repo / "a*b").unlink()
    _dirt_text(repo)
    _put(repo, "src/x.py", "x = 2\n")
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    return repo, out, calls


def test_save_orders_commit_and_check_before_any_restore_and_stays_literal(tmp_path, monkeypatch):
    repo, out, calls = _save_calls(tmp_path, monkeypatch)
    assert out["ok"] is True, out
    _assert_safe_argv(calls)
    wip = out["wip_path"]
    verbs = _verbs(calls)
    main = str(repo)
    commit_i = next(i for i, c in enumerate(calls) if verbs[i] == "commit" and c[2] == wip)
    restores = [i for i, c in enumerate(calls) if c[2] == main and verbs[i] in ("checkout", "rm")]
    assert restores and commit_i < min(restores)
    check_i = [i for i, c in enumerate(calls) if verbs[i] in ("ls-tree", "diff-tree") and out["wip_sha"] in c]
    assert {verbs[i] for i in check_i} == {"ls-tree", "diff-tree"} and max(check_i) < min(restores)
    # every path call, in main and in the WIP tree, is literal
    path_verbs = {"rm", "checkout", "hash-object", "update-index", "ls-tree", "ls-files", "commit"}
    seen = {(c[2], verbs[i]) for i, c in enumerate(calls) if verbs[i] in path_verbs}
    assert {(wip, "rm"), (wip, "update-index"), (wip, "hash-object"), (main, "checkout"),
            (main, "hash-object"), (main, "ls-tree")} <= seen
    assert all("--literal-pathspecs" in c for i, c in enumerate(calls) if verbs[i] in path_verbs)
    assert [c for i, c in enumerate(calls) if verbs[i] == "checkout" and c[2] == main]


def test_star_name_removes_only_itself_and_the_wip_commit_holds_the_planned_paths(tmp_path):
    repo = _saveable(tmp_path, {"a*b": "1\n", "axb": "1\n"})
    (repo / "a*b").unlink()
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    assert (repo / "axb").read_bytes() == b"1\n"
    changed = _git(repo, "diff-tree", "-r", "--name-only", "--no-renames", "HEAD",
                   out["wip_sha"]).stdout.split("\n")
    assert [c for c in changed if c] == ["a*b"]
    assert _ls(repo, out["wip_sha"], "axb") is not None and _ls(repo, out["wip_sha"], "a*b") is None
    assert _tracked_clean(repo)


def test_a_non_literal_wip_removal_would_fail_phase_d(tmp_path, monkeypatch):
    """The mutation, in the test: without `--literal-pathspecs` in the WIP tree `rm -f -- a*b`
    also removes `axb`, an unplanned path in the WIP commit (phase d)."""
    repo = _saveable(tmp_path, {"a*b": "1\n", "axb": "1\n"})
    (repo / "a*b").unlink()
    monkeypatch.setattr(MD, "_lg", lambda root, *a, **kw: MD._git(root, *a, **kw))
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "differ" in out["error"] and out["wip_sha"]
    assert (repo / "axb").exists() and (repo / "docs").exists()
    assert not _tracked_clean(repo)            # main untouched: still holds the deletion


# ---- AC3: failure phases ----------------------------------------------------------------------

def _state(repo):
    return _worktrees(repo), _wips(repo)


def _only_main(repo):
    assert len(_worktrees(repo)) == 1
    assert _wips(repo) == []


def _wrap_run(monkeypatch, fn):
    real = subprocess.run

    def run(argv, *a, **kw):
        fake = fn(list(argv))
        return fake if fake is not None else real(argv, *a, **kw)

    monkeypatch.setattr(MD.subprocess, "run", run)


def test_row_a_unwritable_parent_leaves_no_worktree_or_branch(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    locked = tmp_path / ".nw-worktrees"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        before = _snap(repo)
        out = MD.apply_clear(repo)                  # real git fault: unwritable parent directory
    finally:
        locked.chmod(0o700)
    assert out["ok"] is False and out["error"] and not {"wip_branch", "wip_path", "wip_sha"} & set(out)
    _only_main(repo)
    assert list(locked.iterdir()) == []
    after = _snap(repo)
    assert after["files"] == before["files"] and after["head"] == before["head"]


def test_row_a_failing_post_checkout_hook_removes_worktree_then_branch(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    _hook(repo, "post-checkout", "echo 'hook said no' >&2\nexit 1\n")     # real git fault
    calls = _record(monkeypatch)
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "hook said no" in out["error"]
    assert not {"wip_branch", "wip_path", "wip_sha"} & set(out)
    _only_main(repo)
    assert not _wip_root(tmp_path).exists() or list(_wip_root(tmp_path).iterdir()) == []
    assert _snap(repo)["files"] == before["files"]
    verbs = [(v, c) for v, c in zip(_verbs(calls), calls)]
    rm = next(i for i, (v, c) in enumerate(verbs) if v == "worktree" and "remove" in c)
    br = next(i for i, (v, c) in enumerate(verbs) if v == "branch" and "-d" in c)
    assert rm < br


def test_row_b_failed_copy_removes_worktree_then_branch(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _dirt_text(repo)

    def boom(*a, **kw):                                        # monkeypatched helper
        raise OSError("disk full")

    monkeypatch.setattr(MD, "_copy_to_wip", boom)
    calls = _record(monkeypatch)
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "disk full" in out["error"]
    assert not {"wip_branch", "wip_path", "wip_sha"} & set(out)
    _only_main(repo)
    assert _snap(repo)["files"] == before["files"]
    verbs = _verbs(calls)
    rm = next(i for i, c in enumerate(calls) if verbs[i] == "worktree" and "remove" in c)
    br = next(i for i, c in enumerate(calls) if verbs[i] == "branch" and "-d" in c)
    assert rm < br
    assert not (_wip_root(tmp_path).exists() and list(_wip_root(tmp_path).iterdir()))


def test_row_c_failing_pre_commit_hook(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    _hook(repo, "pre-commit", "echo 'lint says no' >&2\nexit 1\n")      # real git fault
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "lint says no" in out["error"]
    assert not {"wip_branch", "wip_path", "wip_sha"} & set(out)
    _only_main(repo)
    assert not _tracked_clean(repo)
    verbs = [(v, c) for v, c in zip(_verbs(calls), calls)]
    rm = next(i for i, (v, c) in enumerate(verbs) if v == "worktree" and "remove" in c)
    br = next(i for i, (v, c) in enumerate(verbs) if v == "branch" and "-d" in c)
    assert rm < br


def test_row_c_cleanup_failure_names_both_and_keeps_what_remains(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    _hook(repo, "pre-commit", "echo 'lint says no' >&2\nexit 1\n")

    def fail_remove(argv):
        if "worktree" in argv and "remove" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"cannot remove it")

    _wrap_run(monkeypatch, fail_remove)
    calls = _record(monkeypatch)
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "lint says no" in out["error"] and "cannot remove it" in out["error"]
    assert _snap(repo)["files"] == before["files"]
    assert not {"wip_branch", "wip_path", "wip_sha"} & set(out)
    assert len(_worktrees(repo)) == 2 and len(_wips(repo)) == 1          # kept
    assert not any(v == "branch" and ("-d" in c or "-D" in c) for v, c in zip(_verbs(calls), calls))


def _hook_in_wip_tree(repo, body):
    """A pre-commit hook that only acts inside the WIP worktree (a real fault between staging and
    the check)."""
    _hook(repo, "pre-commit", f'case "$PWD" in *main-dirty-*) {body};; esac\nexit 0\n')


def _keeps(repo, out):
    assert out["ok"] is False
    assert out["wip_sha"] == _git(repo, "rev-parse", out["wip_branch"]).stdout.strip()
    assert out["wip_branch"] in _wips(repo) and os.path.isdir(out["wip_path"])
    assert len(_worktrees(repo)) == 2


def test_row_d_blob_mismatch_keeps_worktree_and_branch(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    _hook_in_wip_tree(repo, 'o=$(echo tampered | git hash-object -w --stdin); '
                            'git update-index --cacheinfo "100644,$o,config/a.json"')
    calls = _record(monkeypatch)
    before = _snap(repo)
    out = MD.apply_clear(repo)
    _keeps(repo, out)
    assert "config/a.json" in out["error"]
    assert _snap(repo)["files"] == before["files"] and not _tracked_clean(repo)
    assert not any(v == "branch" and ("-d" in c or "-D" in c) for v, c in zip(_verbs(calls), calls))


def test_row_d_unplanned_path_in_the_wip_commit(tmp_path):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    _hook_in_wip_tree(repo, "echo x > extra.txt; git add extra.txt")
    before = _snap(repo)
    out = MD.apply_clear(repo)
    _keeps(repo, out)
    assert "extra.txt" in out["error"]
    assert _snap(repo)["files"] == before["files"] and not _tracked_clean(repo)


def _three(tmp_path):
    repo = _saveable(tmp_path, {"config/b.json": "{}\n", "config/c.json": "{}\n"})
    for n in "abc":
        _put(repo, f"config/{n}.json", f'{{"{n}": 1}}\n')
    return repo


def test_row_e_reverify_failure_on_the_second_of_three_paths(tmp_path, monkeypatch):
    repo = _three(tmp_path)
    real = MD._reverify_path

    def second_fails(root, p, mode, oid):                      # monkeypatched helper
        return "changed since the save" if p == "config/b.json" else real(root, p, mode, oid)

    monkeypatch.setattr(MD, "_reverify_path", second_fails)
    out = MD.apply_clear(repo)
    _keeps(repo, out)
    assert "config/b.json" in out["error"]
    status = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout
    assert "config/a.json" not in status and "config/b.json" in status and "config/c.json" in status
    assert (repo / "config/a.json").read_bytes() == b"{}\n"                       # restored
    assert (repo / "config/b.json").read_bytes() == b'{"b": 1}\n'                 # untouched
    assert (repo / "config/c.json").read_bytes() == b'{"c": 1}\n'
    for n in "abc":                                                               # all on the commit
        assert _blob_bytes(repo, out["wip_sha"], f"config/{n}.json") == f'{{"{n}": 1}}\n'.encode()


def test_row_e_real_edit_between_save_and_restore(tmp_path, monkeypatch):
    repo = _three(tmp_path)
    real = MD._restore_main

    def edit_then_restore(root, paths, modes, oids, head):
        _put(repo, "config/c.json", "someone else\n")
        return real(root, paths, modes, oids, head)

    monkeypatch.setattr(MD, "_restore_main", edit_then_restore)
    out = MD.apply_clear(repo)
    _keeps(repo, out)
    assert "config/c.json" in out["error"] and (repo / "config/c.json").read_bytes() == b"someone else\n"


def test_row_f_post_restore_check_keeps_everything(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    real = MD._restore_main

    def restore_then_dirty(*a):                                # monkeypatched helper
        res = real(*a)
        _put(repo, "README.md", "dirtied meanwhile\n")
        return res

    monkeypatch.setattr(MD, "_restore_main", restore_then_dirty)
    out = MD.apply_clear(repo)
    _keeps(repo, out)
    assert "README.md" in out["error"]


# ---- AC4: name clash --------------------------------------------------------------------------

def test_name_clash_gives_suffixes_for_branch_and_directory(tmp_path, monkeypatch):
    _freeze(monkeypatch)
    repo = _saveable(tmp_path)
    base = "wip/main-dirty-20260101T000000Z"
    names = []
    for i in range(3):
        _put(repo, "config/a.json", f'{{"n": {i}}}\n')
        out = MD.apply_clear(repo)
        assert out["ok"] is True, out
        names.append((out["wip_branch"], Path(out["wip_path"]).name))
    assert names == [(base, "main-dirty-20260101T000000Z"), (base + "-2", "main-dirty-20260101T000000Z-2"),
                     (base + "-3", "main-dirty-20260101T000000Z-3")]


def test_name_clash_on_the_directory_alone_moves_both(tmp_path, monkeypatch):
    _freeze(monkeypatch)
    repo = _saveable(tmp_path)
    (_wip_root(tmp_path) / "main-dirty-20260101T000000Z").mkdir(parents=True)
    _dirt_text(repo)
    out = MD.apply_clear(repo)
    assert out["wip_branch"].endswith("T000000Z-2") and out["wip_path"].endswith("T000000Z-2")


# ---- AC5: escalate ----------------------------------------------------------------------------

def _hold(builder):
    def build(tmp_path):
        repo = _saveable(tmp_path)
        _on_feature(repo)
        builder(repo)
        return repo
    return build


def _plain(builder):
    def build(tmp_path):
        repo = _saveable(tmp_path, {"docs/a.md": "# T\n"})
        builder(repo)
        return repo
    return build


def _merge(repo):
    _real_conflict(repo)


def _gitlink(repo):
    sub = repo / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    _git(sub, "config", "user.email", "t@t")
    _git(sub, "config", "user.name", "t")
    _git(sub, "commit", "-q", "--allow-empty", "-m", "i")
    _git(repo, "add", "sub")
    _git(repo, "commit", "-qm", "sub")
    _git(sub, "commit", "-q", "--allow-empty", "-m", "inner")


ESCALATE_CASES = {
    "resume-clean": _plain(lambda r: None), "commit-trivial": _plain(_edit_trivial),
    "save-branch": _plain(_dirt_text), "save-substantive": _plain(lambda r: _put(r, "src/x.py", "x = 2\n")),
    "hold-clean": _hold(lambda r: None), "hold-trivial": _hold(_edit_trivial),
    "hold-branch": _hold(_dirt_text), "hold-substantive": _hold(lambda r: _put(r, "src/x.py", "x = 2\n")),
    "merge": _plain(_merge), "gitlink-escalate": _plain(_gitlink),
    "refusal-no-config-substantive": lambda t: _saveable(t, project=False),
}


@pytest.mark.parametrize("name", sorted(ESCALATE_CASES))
def test_escalate_is_true_exactly_for_substantive_and_merge(tmp_path, name):
    repo = ESCALATE_CASES[name](tmp_path)
    if name == "refusal-no-config-substantive":
        _put(repo, "src/x.py", "x = 2\n")
    plan = MD.plan_clear(repo)
    out = MD.apply_clear(repo)
    assert out["escalate"] is (plan["class"] in ("substantive", "merge")), (plan["class"], out)
    assert out["action"] == plan["action"]


def test_escalate_on_the_two_no_plan_returns(tmp_path):
    assert MD.apply_clear(tmp_path / "nope")["escalate"] is False and \
        MD.apply_clear(tmp_path / "nope")["action"] == "none"
    repo = _saveable(tmp_path)
    _put(repo, "src/x.py", "x = 2\n")
    gdir = _git(repo, "rev-parse", "--absolute-git-dir").stdout.strip()
    with open(os.path.join(gdir, "nw-main-dirty.lock"), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = MD.apply_clear(repo)
    assert (out["action"], out["escalate"], out["error"]) == ("none", False, "clear already in progress")


def test_an_error_after_a_substantive_save_keeps_escalate_true(tmp_path):
    repo = _saveable(tmp_path)
    _put(repo, "src/x.py", "x = 2\n")
    _hook(repo, "pre-commit", "exit 1\n")
    out = MD.apply_clear(repo)
    assert (out["ok"], out["escalate"]) == (False, True)


# ---- AC6: hold --------------------------------------------------------------------------------

def _branch(repo):
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def test_hold_saves_then_switches_to_the_base(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _on_feature(repo)
    _put(repo, "src/x.py", "x = 2\n")
    _put(repo, "untracked.txt", "u\n")
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["escalate"], out["action"]) == (True, True, "hold"), out
    assert out["replanned"] == {"on_base": True, "action": "resume", "class": "clean"}
    assert _branch(repo) == "main" and _tracked_clean(repo)
    assert _blob_bytes(repo, out["wip_sha"], "src/x.py") == b"x = 2\n"
    _assert_safe_argv(calls)
    verbs = _verbs(calls)
    sw = [c for v, c in zip(verbs, calls) if v == "switch"]
    assert len(sw) == 1 and sw[0][-2:] == ["--no-guess", "main"]
    first_restore = next(i for i, v in enumerate(verbs) if v == "checkout")
    assert verbs.index("switch") > first_restore
    assert (repo / "untracked.txt").read_bytes() == b"u\n"


def test_hold_failed_switch_keeps_the_wip_and_reports_hold(tmp_path):
    repo = _saveable(tmp_path, {"collide.txt": "main's\n"})
    _on_feature(repo)
    _git(repo, "rm", "-q", "collide.txt")
    _git(repo, "commit", "-qm", "drop")
    _put(repo, "collide.txt", "untracked on feat\n")           # overwritten by the switch: it refuses
    _put(repo, "src/x.py", "x = 2\n")
    out = MD.apply_clear(repo)
    assert out["ok"] is False and out["error"]
    assert out["wip_branch"] in _wips(repo) and out["wip_sha"]
    assert out["replanned"]["action"] == "hold" and set(out["replanned"]) == {"on_base", "action", "class"}
    assert out["escalate"] is True and _branch(repo) == "feat"
    assert (repo / "collide.txt").read_bytes() == b"untracked on feat\n"


def test_hold_switch_that_fails_after_head_moved_reports_the_replan(tmp_path):
    repo = _saveable(tmp_path)
    _on_feature(repo)
    _put(repo, "src/x.py", "x = 2\n")
    real = os.path.realpath(repo)
    _hook(repo, "post-checkout", f'[ "$PWD" = "{real}" ] && [ "$3" = 1 ] && exit 1\nexit 0\n')   # main, branch switch only
    out = MD.apply_clear(repo)
    assert out["ok"] is False and out["wip_branch"] in _wips(repo)
    assert out["replanned"] == {"on_base": True, "action": "resume", "class": "clean"}
    assert _branch(repo) == "main"


def test_hold_with_a_failed_save_does_not_switch(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _on_feature(repo)
    _put(repo, "src/x.py", "x = 2\n")
    _hook(repo, "pre-commit", "exit 1\n")
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "replanned" not in out and _branch(repo) == "feat"
    assert "switch" not in _verbs(calls)


@pytest.mark.parametrize("base", ["-x", "a..b", "ghost"])
def test_hold_refuses_an_invalid_base_before_any_write(tmp_path, base):
    repo = _saveable(tmp_path)
    _on_feature(repo)
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                   "base": base}))
    _put(repo, "src/x.py", "x = 2\n")
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and out["error"]
    assert _snap(repo) == before and not (tmp_path / ".nw-worktrees").exists()


def test_hold_detached_head_not_on_a_branch_is_refused(tmp_path):
    repo = _saveable(tmp_path)
    _git(repo, "switch", "-q", "--detach")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "orphan")
    _put(repo, "src/x.py", "x = 2\n")
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, "detached HEAD not on any branch")
    assert _snap(repo) == before and not (tmp_path / ".nw-worktrees").exists()


def test_hold_detached_head_at_a_branch_tip_is_accepted(tmp_path):
    repo = _saveable(tmp_path)
    _git(repo, "switch", "-q", "--detach")
    _put(repo, "src/x.py", "x = 2\n")
    out = MD.apply_clear(repo)
    assert out["ok"] is True and _branch(repo) == "main", out


def test_hold_reads_the_base_after_the_restore(tmp_path):
    repo = _saveable(tmp_path)
    _on_feature(repo)
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                   "base": "-x"}))
    _git(repo, "commit", "-qam", "bad base in HEAD")
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                   "base": "main"}))          # the stray dirty copy
    out = MD.apply_clear(repo)
    assert out["ok"] is False and out["wip_branch"] in _wips(repo) and "replanned" not in out
    assert _branch(repo) == "feat" and _tracked_clean(repo)


def test_hold_without_dirt_needs_no_project_config(tmp_path):
    repo = _repo(tmp_path, project=False)
    _on_feature(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is True and _branch(repo) == "main", out
    assert out["replanned"] == {"on_base": True, "action": "resume", "class": "clean"}
    assert not ({"wip_branch", "wip_path", "wip_sha"} & set(out))
    assert not (tmp_path / ".nw-worktrees").exists()


# ---- review loop: AC2 `rm -f --` restore, AC7 raw lists for save actions, awkward names ---------

def _wip_add_path(calls):
    """The WIP directory the verb created: the path argument of `worktree add -b <branch> <path>`."""
    add = next(c for c in calls if "worktree" in c and "add" in c)
    return add[add.index("add") + 3]


def _main_rms(calls, main):
    return [c for c in calls if c[2] == main and "rm" in c[3:] and c[c.index("rm") - 1] != "worktree"]


def _star_new_file(tmp_path):
    """A newly added `a*b` beside a tracked `axb`: a glob `a*b` would take both."""
    repo = _saveable(tmp_path, {"axb": "1\n"})
    _put(repo, "a*b", "new\n")
    _git(repo, "add", "--", "a*b")
    _dirt_text(repo)
    return repo


def test_new_star_file_restore_uses_a_literal_rm_and_leaves_its_sibling(tmp_path, monkeypatch):
    repo = _star_new_file(tmp_path)
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    _assert_safe_argv(calls)                                  # every `rm` carries the flag and `--`
    rms = _main_rms(calls, str(repo))
    assert rms and all("--literal-pathspecs" in c and c[-2:] == ["--", "a*b"] and "-f" in c for c in rms), rms
    assert (repo / "axb").read_bytes() == b"1\n" and not (repo / "a*b").exists()
    assert _tracked_clean(repo)
    assert _blob_bytes(repo, out["wip_sha"], "a*b") == b"new\n"
    assert _blob_bytes(repo, out["wip_sha"], "axb") == b"1\n"
    commit_i = next(i for i, c in enumerate(calls) if "commit" in c[3:] and c[2] == out["wip_path"])
    assert commit_i < calls.index(rms[0])


def test_a_non_literal_main_rm_restore_would_be_caught(tmp_path, monkeypatch):
    """The mutation, in the test: `_restore_path`'s `rm` branch through `_git` (no flag) takes the
    sibling `axb` along, so main is not restored (phase f) and the check sees it."""
    repo = _star_new_file(tmp_path)

    def restore(root, p, in_head):
        if in_head:
            MD._lg(root, "checkout", "HEAD", "--", p)
        else:
            MD._git(root, "rm", "-f", "--", p)

    monkeypatch.setattr(MD, "_restore_path", restore)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "axb" in out["error"] and out["wip_sha"]


# AC7: the raw list stays for a save action whose verb refuses (guards 5/6), `.nw/project.yaml`
# committed so guard 7 itself passes.

def _gitlink_row(tmp_path, mp):
    mp.setattr(MD, "_is_gitlink", lambda *a, **k: False)     # the classifier would escalate it
    return _sc_gitlink(tmp_path, mp)


def _partial_save(tmp_path, mp):
    repo = _repo(tmp_path, {"config/a.json": "{}\n"})
    _put(repo, "config/a.json", '{"a": 1}\n')
    _git(repo, "add", "config/a.json")
    _put(repo, "config/a.json", '{"a": 2}\n')
    return repo, "partially staged config/a.json", False


RAW_SAVES = {"symlink-target": _sc_symlink_target, "typechange": _sc_file_to_symlink,
             "symlink-to-file": _sc_symlink_to_file, "symlinked-parent": _sc_symlinked_parent_real,
             "gitlink": _gitlink_row, "partial": _partial_save}


@pytest.mark.parametrize("name", sorted(RAW_SAVES))
def test_recommended_keeps_the_raw_list_for_a_save_the_verb_refuses(tmp_path, monkeypatch, name):
    repo, error, _ = RAW_SAVES[name](tmp_path, monkeypatch)
    assert _git(repo, "show", "HEAD:.nw/project.yaml").stdout                 # guard 7 can pass
    p = MD.plan_clear(repo)
    assert p["action"] in ("save_branch", "branch_and_escalate"), p["action"]
    assert MD.verb_refusal(repo, p) == error
    R = shlex.quote(str(repo))
    rec = p["recommended"]
    assert len(rec) == 4 and not any(c.startswith("nw ") for c in rec), rec
    assert rec[0].startswith(f"git -C {R} switch -c wip/main-dirty-")
    assert rec[1].startswith(f"git -C {R} --literal-pathspecs add -- ") and p["paths"][0] in rec[1]
    assert rec[2].startswith(f"git -C {R} commit -m 'WIP: stray main-checkout edits (")
    assert rec[3] == f"git -C {R} switch main"


# AC1 / test plan: awkward tracked names go through `diff-tree -z`, `--cacheinfo`, `ls-tree -z`.

def test_backslash_name_is_kept_and_saved(tmp_path):
    repo = _saveable(tmp_path, {"config/a\\b.json": "{}\n"})
    _put(repo, "config/a\\b.json", '{"a": 1}\n')
    plan = MD.plan_clear(repo)
    assert plan["paths"] == ["config/a\\b.json"]
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    assert _blob_bytes(repo, out["wip_sha"], "config/a\\b.json") == b'{"a": 1}\n'
    assert _tracked_clean(repo)
    assert _git(repo, "show", "HEAD:config/a\\b.json").stdout == "{}\n"


def _edit_named(name):
    def build(tmp_path):
        repo = _saveable(tmp_path, {name: "{}\n"})
        _put(repo, name, f'{{"{len(name)}": 1}}\n')
        return repo, name, f'{{"{len(name)}": 1}}\n'.encode()
    return build


def _delete_named(name):
    def build(tmp_path):
        repo = _saveable(tmp_path, {name: "{}\n"})
        (repo / name).unlink()
        return repo, name, None
    return build


def _new_nested(tmp_path):
    repo = _saveable(tmp_path)
    _put(repo, "n e w/ünï/d-1/f.json", "{}\n")
    _git(repo, "add", "--", "n e w/ünï/d-1/f.json")
    return repo, "n e w/ünï/d-1/f.json", b"{}\n"


AWKWARD = {"backslash": _edit_named("config/a\\b.json"), "space": _edit_named("config/a b.json"),
           "non-ascii": _edit_named("config/ünï.json"), "leading-dash": _edit_named("-dash.json"),
           "dash-dir": _edit_named("-d/x.json"), "quote": _edit_named('config/q"uo\'te.json'),
           "deleted-backslash": _delete_named("config/a\\b.json"),
           "deleted-leading-dash": _delete_named("-dash.json"), "new-nested-dir": _new_nested}


@pytest.mark.parametrize("name", sorted(AWKWARD))
def test_save_with_awkward_tracked_names(tmp_path, name):
    repo, rel, content = AWKWARD[name](tmp_path)
    plan = MD.plan_clear(repo)
    assert plan["paths"] == [rel] and plan["action"] == "save_branch"
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    if content is None:
        assert _ls(repo, out["wip_sha"], rel) is None
        assert (repo / rel).read_bytes() == b"{}\n"          # main restored from HEAD
    else:
        assert _blob_bytes(repo, out["wip_sha"], rel) == content
    changed = _git(repo, "diff-tree", "-r", "--name-only", "--no-renames", "-z", "HEAD",
                   out["wip_sha"]).stdout.split("\0")
    assert [c for c in changed if c] == [rel]
    assert _tracked_clean(repo)


# As shipped: planned paths that equal HEAD, and the empty WIP commit.

def _wip_is_empty(repo, out):
    head = _git(repo, "rev-parse", "HEAD").stdout
    assert _git(repo, "rev-parse", f"{out['wip_sha']}^").stdout == head
    assert _git(repo, "diff-tree", "-r", "--name-only", "HEAD", out["wip_sha"]).stdout == ""
    assert _git(repo, "rev-parse", f"{out['wip_sha']}^{{tree}}").stdout == \
        _git(repo, "rev-parse", "HEAD^{tree}").stdout


def test_a_staged_mode_change_reverted_in_the_worktree_saves_an_empty_wip_commit(tmp_path):
    repo = _saveable(tmp_path)
    os.chmod(repo / "config/a.json", 0o755)
    _git(repo, "add", "config/a.json")
    os.chmod(repo / "config/a.json", 0o644)                   # worktree bytes and mode equal HEAD
    plan = MD.plan_clear(repo)
    assert plan["paths"] == ["config/a.json"] and plan["files"][0]["status"] == "MM"
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    _wip_is_empty(repo, out)
    assert _tracked_clean(repo) and _git(repo, "ls-files", "-s", "config/a.json").stdout.startswith("100644")


@pytest.mark.parametrize("kind", ["staged-edit-reverted", "added-then-deleted"])
def test_a_planned_path_equal_to_head_is_restored_with_an_empty_wip_commit(tmp_path, monkeypatch, kind):
    """Guard 6 refuses these two live (partial staging); with it bypassed the verb still restores
    main and the WIP commit is empty (`commit --allow-empty`; the `AD` path is not in HEAD)."""
    repo = _saveable(tmp_path)
    if kind == "staged-edit-reverted":
        _put(repo, "config/a.json", '{"a": 1}\n')
        _git(repo, "add", "config/a.json")
        _put(repo, "config/a.json", "{}\n")
        rel = "config/a.json"
    else:
        _put(repo, "config/n.json", "{}\n")
        _git(repo, "add", "config/n.json")
        (repo / "config/n.json").unlink()
        rel = "config/n.json"
    plan = MD.plan_clear(repo)
    assert plan["paths"] == [rel] and MD.verb_refusal(repo, plan) == f"partially staged {rel}"
    monkeypatch.setattr(MD, "verb_refusal", lambda *a, **k: None)
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    _wip_is_empty(repo, out)
    assert _tracked_clean(repo)
    assert (repo / rel).exists() is (kind == "staged-edit-reverted")
    assert _git(repo, "ls-files", "--", rel).stdout.strip() == (rel if kind == "staged-edit-reverted" else "")


# ---- review loop: tightened AC3 / AC4 / AC6 ------------------------------------------------------

def _worktree_removes(calls):
    return [c for c in calls if "worktree" in c and "remove" in c]


@pytest.mark.parametrize("row", ["a", "b", "c"])
def test_worktree_remove_force_targets_only_the_wip_path(tmp_path, monkeypatch, row):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    if row == "a":
        _hook(repo, "post-checkout", "exit 1\n")
    elif row == "c":
        _hook(repo, "pre-commit", "exit 1\n")
    else:
        monkeypatch.setattr(MD, "_copy_to_wip", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    calls = _record(monkeypatch)
    assert MD.apply_clear(repo)["ok"] is False
    wip = _wip_add_path(calls)
    removes = _worktree_removes(calls)
    assert len(removes) == 1 and removes[0][-3:] == ["remove", "--force", wip], removes
    assert Path(wip).parent == _wip_root(tmp_path)
    assert _wips(repo) == [] and len(_worktrees(repo)) == 1


def test_row_b_orders_worktree_remove_before_branch_delete_on_the_real_calls(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    monkeypatch.setattr(MD, "_copy_to_wip", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    calls = _record(monkeypatch)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "full" in out["error"]
    rm = next(i for i, c in enumerate(calls) if "worktree" in c and "remove" in c)
    br = next(i for i, c in enumerate(calls) if _verbs(calls)[i] == "branch" and "-d" in c)
    assert rm < br


def test_row_c_main_is_byte_identical(tmp_path):
    repo = _three(tmp_path)
    _put(repo, "untracked.txt", "u\n")
    _hook(repo, "pre-commit", "echo 'lint says no' >&2\nexit 1\n")
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and "lint says no" in out["error"]
    after = _snap(repo)
    assert after["files"] == before["files"] and after["head"] == before["head"]
    assert not _wip_root(tmp_path).exists() or list(_wip_root(tmp_path).iterdir()) == []


def test_name_clash_on_the_branch_alone_moves_both(tmp_path, monkeypatch):
    _freeze(monkeypatch)
    repo = _saveable(tmp_path)
    _git(repo, "branch", "wip/main-dirty-20260101T000000Z")           # a branch, no directory
    _dirt_text(repo)
    out = MD.apply_clear(repo)
    assert out["ok"] is True, out
    assert out["wip_branch"] == "wip/main-dirty-20260101T000000Z-2"
    assert Path(out["wip_path"]).name == "main-dirty-20260101T000000Z-2"


def test_empty_project_name_error_text(tmp_path):
    repo = _saveable(tmp_path)
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": ""}))
    out = MD.apply_clear(repo)
    # the config loader rejects an empty `project:` before guard 7's own `bad project name`
    assert (out["ok"], out["error"]) == (
        False, "invalid project config:\n  - project: must be a non-empty string")
    assert not (tmp_path / ".nw-worktrees").exists()


@pytest.mark.parametrize("base", ["ghost", "a..b"])
def test_hold_invalid_base_error_text(tmp_path, base):
    repo = _saveable(tmp_path)
    _on_feature(repo)
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                   "base": base}))
    _put(repo, "src/x.py", "x = 2\n")
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, f"bad base {base}")
    assert "wip_branch" not in out


def test_row_d_mode_mismatch(tmp_path):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    _hook_in_wip_tree(repo, 'git update-index --chmod=+x config/a.json')
    before = _snap(repo)
    out = MD.apply_clear(repo)
    _keeps(repo, out)
    assert out["error"] == "WIP blob or mode differs from main for config/a.json"
    assert _snap(repo)["files"] == before["files"] and not _tracked_clean(repo)


def test_row_d_wip_commit_still_holds_a_deleted_path(tmp_path):
    repo = _saveable(tmp_path)
    _dirt_deleted(repo)
    _hook_in_wip_tree(repo, 'git update-index --add --cacheinfo '
                            '"100644,$(git rev-parse HEAD:config/a.json),config/a.json"')
    out = MD.apply_clear(repo)
    _keeps(repo, out)
    assert out["error"] == "WIP commit still holds deleted config/a.json"
    assert not (repo / "config/a.json").exists() and not _tracked_clean(repo)


def test_row_e_a_failing_restore_command(tmp_path, monkeypatch):
    repo = _three(tmp_path)
    real = MD._restore_path

    def second_fails(root, p, in_head):
        if p == "config/b.json":
            raise MD.PlanError("restore boom")
        return real(root, p, in_head)

    monkeypatch.setattr(MD, "_restore_path", second_fails)
    out = MD.apply_clear(repo)
    _keeps(repo, out)
    assert out["error"] == "config/b.json: restore boom"
    assert (repo / "config/a.json").read_bytes() == b"{}\n"                       # before k: restored
    assert (repo / "config/b.json").read_bytes() == b'{"b": 1}\n'                 # k and later: kept
    assert (repo / "config/c.json").read_bytes() == b'{"c": 1}\n'
    assert _blob_bytes(repo, out["wip_sha"], "config/b.json") == b'{"b": 1}\n'


def test_row_f_wip_commit_missing(tmp_path, monkeypatch):
    repo = _saveable(tmp_path)
    _dirt_text(repo)
    real = MD._restore_main

    def restore_then_drop_the_branch(*a):
        res = real(*a)
        (br,) = _wips(repo)
        _git(repo, "update-ref", "-d", f"refs/heads/{br}")
        return res

    monkeypatch.setattr(MD, "_restore_main", restore_then_drop_the_branch)
    out = MD.apply_clear(repo)
    assert out["ok"] is False and out["error"] == "main not restored: the WIP commit is missing"
    assert out["wip_branch"] and out["wip_sha"] and os.path.isdir(out["wip_path"])


# ---- text mode names the WIP branch (the edits live only there after a late failure) -------------

def _text_lines(output):
    return dict(ln.split(": ", 1) for ln in output.splitlines()[1:])


def test_cli_text_mode_prints_the_wip_keys_on_success_and_the_hold_replan(tmp_path):
    repo = _saveable(tmp_path)
    _on_feature(repo)
    _put(repo, "src/x.py", "x = 2\n")
    r = _invoke("--root", str(repo))
    assert r.exit_code == 0 and r.output.splitlines()[0] == "main-dirty-apply ok: hold"
    keys = _text_lines(r.output)
    (wip,) = _wips(repo)
    assert keys["wip_branch"] == wip and keys["wip_path"] == str(_wip_root(tmp_path) / wip.split("/")[1])
    assert keys["wip_sha"] == _git(repo, "rev-parse", wip).stdout.strip()
    assert keys["replanned"] == "action=resume class=clean on_base=True"
    assert "error" not in keys


def test_cli_text_mode_prints_the_wip_keys_when_a_restore_fails(tmp_path, monkeypatch):
    repo = _three(tmp_path)
    real = MD._reverify_path
    monkeypatch.setattr(MD, "_reverify_path", lambda root, p, mode, oid:
                        "changed since the save" if p == "config/b.json" else real(root, p, mode, oid))
    r = _invoke("--root", str(repo))
    assert r.exit_code == 1 and r.output.splitlines()[0] == "main-dirty-apply failed: save_branch"
    keys = _text_lines(r.output)
    (wip,) = _wips(repo)
    assert keys["wip_branch"] == wip and os.path.isdir(keys["wip_path"])
    assert keys["wip_sha"] == _git(repo, "rev-parse", wip).stdout.strip()
    assert keys["error"] == "config/b.json: changed since the save"
    assert all(len(ln.splitlines()) == 1 for ln in r.output.splitlines())


def test_cli_text_mode_has_no_wip_keys_for_a_failure_before_the_save(tmp_path):
    repo = _saveable(tmp_path)
    _put(repo, "src/x.py", "x = 2\n")
    _hook(repo, "pre-commit", "exit 1\n")
    r = _invoke("--root", str(repo))
    assert r.exit_code == 1 and not any(ln.startswith("wip_") for ln in r.output.splitlines())
