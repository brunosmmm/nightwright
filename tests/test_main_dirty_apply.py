"""SPEC-0548: `apply_clear` / `nw queue main-dirty-apply` core: guards, `commit`, `resume`."""
from __future__ import annotations

import fcntl
import json
import os
import shlex
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


def test_merge_in_a_linked_worktree(tmp_path):
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "lw", str(linked))
    _gitdir(linked, "MERGE_HEAD").write_text(_git(linked, "rev-parse", "HEAD").stdout)
    _edit_trivial(linked)
    before = _snap(linked)
    out = MD.apply_clear(linked, plan={"schema": MD.SCHEMA, "action": "commit", "paths": ["docs/a.md"]})
    assert (out["ok"], out["escalate"], out["error"]) == (False, True, "merge in progress")
    assert _snap(linked) == before


def test_stale_plan_writes_nothing_and_plan_base_is_ignored(tmp_path):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    before = _snap(repo)
    for plan in ({"schema": MD.SCHEMA, "action": "save_branch", "paths": ["docs/a.md"]},
                 {"schema": MD.SCHEMA, "action": "commit", "paths": ["docs/other.md"]},
                 {"schema": MD.SCHEMA, "action": "commit", "paths": []}):
        out = MD.apply_clear(repo, plan=plan)
        assert (out["ok"], out["error"]) == (False, "plan is stale")
        assert _snap(repo) == before
    out = MD.apply_clear(repo, plan={"schema": MD.SCHEMA, "action": "commit", "paths": ["docs/a.md"],
                                     "base": "develop"})
    assert out["ok"] is True


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

    def lg(root, *args, check=True):
        if args and args[0] == "commit":
            return wrapper(real, root, *args, check=check)
        return real(root, *args, check=check)

    monkeypatch.setattr(MD, "_lg", lg)


def test_restore_index_locked(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    lock = _index_path(repo).parent / "index.lock"

    def holder(real, root, *args, check):             # a concurrent git process takes the lock
        lock.write_text("holder")
        return real(root, *args, check=check)

    _wrap_commit(monkeypatch, holder)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"]) == (False, "index locked, not restored")
    assert "sha" not in out and lock.read_text() == "holder"


def test_restore_skipped_when_head_moved(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)

    def lands_then_fails(real, root, *args, check):
        real(root, *args, check=check)
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
    out = MD.apply_clear(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert (out["ok"], out["sha"], out["error"]) == (False, head, "main not tracked-clean after commit")
    assert not _tracked_clean(repo)


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
    return repo, "save not supported yet: save_branch", False


BAD_PATHS = {"symlink-target": _sc_symlink_target, "file-to-symlink": _sc_file_to_symlink,
             "symlink-to-file": _sc_symlink_to_file, "symlinked-parent": _sc_symlinked_parent_real,
             "gitlink": _sc_gitlink, "absolute": _patched("/etc/passwd"),
             "dotdot": _patched("../x.md"), "in-neither": _patched("docs/ghost.md")}


@pytest.mark.parametrize("name", sorted(BAD_PATHS))
def test_refusal_bad_paths(tmp_path, monkeypatch, name):
    repo, error, escalate = BAD_PATHS[name](tmp_path, monkeypatch)
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"], out["escalate"]) == (False, error, escalate)
    assert _snap(repo) == before


def test_deleted_path_with_a_star_is_not_a_bad_path(tmp_path, monkeypatch):
    repo, error, _ = _sc_deleted_star(tmp_path, monkeypatch)
    plan = MD.plan_clear(repo)
    assert (plan["action"], plan["paths"]) == ("save_branch", ["docs/a*b.md"])
    assert MD.verb_refusal(repo, plan) is None
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"], out["escalate"]) == (False, error, False)
    assert _snap(repo) == before


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


def _sc_save_branch(tmp_path, mp):
    fenced = "# T\n\n```\ncode\n```\n"
    repo = _repo(tmp_path, {"docs/f.md": fenced})
    _put(repo, "docs/f.md", fenced.replace("code", "code2"))
    return repo, "save not supported yet: save_branch", False


def _sc_branch_and_escalate(tmp_path, mp):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n"})
    _put(repo, "src/x.py", "x = 2\n")
    return repo, "save not supported yet: branch_and_escalate", True


def _sc_hold_trivial(tmp_path, mp):
    repo = _repo(tmp_path)
    _on_feature(repo)
    _edit_trivial(repo)
    return repo, "save not supported yet: hold", False


def _sc_hold_substantive(tmp_path, mp):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n"})
    _on_feature(repo)
    _put(repo, "src/x.py", "x = 2\n")
    return repo, "save not supported yet: hold", True


def _sc_hold_branch(tmp_path, mp):
    repo = _repo(tmp_path, {"skills/s.md": "a\n"})
    _on_feature(repo)
    _put(repo, "skills/s.md", "b\n")
    return repo, "save not supported yet: hold", False


def _sc_hold_clean(tmp_path, mp):
    repo = _repo(tmp_path)
    _on_feature(repo)
    return repo, "save not supported yet: hold", False


SAVES = {"save_branch": _sc_save_branch, "branch_and_escalate": _sc_branch_and_escalate,
         "hold-trivial": _sc_hold_trivial, "hold-substantive": _sc_hold_substantive,
         "hold-branch": _sc_hold_branch, "hold-clean": _sc_hold_clean}


@pytest.mark.parametrize("name", sorted(SAVES))
def test_refusal_save_actions_not_supported(tmp_path, monkeypatch, name):
    repo, error, escalate = SAVES[name](tmp_path, monkeypatch)
    _put(repo, "untracked.txt", "u\n")
    before = _snap(repo)
    out = MD.apply_clear(repo)
    assert (out["ok"], out["error"], out["escalate"]) == (False, error, escalate)
    assert out["action"] == error.rsplit(": ", 1)[1] and "sha" not in out
    assert _snap(repo) == before


def _sc_merge(tmp_path, mp):
    repo = _repo(tmp_path)
    _real_conflict(repo)
    return repo, "merge in progress", True


@pytest.mark.parametrize("name", sorted({**BAD_PATHS, **SAVES, "partial": _sc_partial,
                                         "merge": _sc_merge}))
def test_refusals_leave_index_bytes(tmp_path, monkeypatch, name):
    """The guards run with GIT_OPTIONAL_LOCKS=0: a repo with stale stat data keeps its index."""
    table = {**BAD_PATHS, **SAVES, "partial": _sc_partial, "merge": _sc_merge}
    repo, error, _ = table[name](tmp_path, monkeypatch)
    for f in ("README.md", "docs/a.md"):                       # stale stat data: git would refresh
        if (repo / f).is_file() and not (repo / f).is_symlink():
            os.utime(repo / f, (1_000_000_000, 1_000_000_000))
    index = _index_path(repo).read_bytes()
    out = MD.apply_clear(repo)
    assert out["ok"] is False
    assert _index_path(repo).read_bytes() == index


# ---- AC4: argv, untracked bytes ---------------------------------------------------------------

def test_recorded_git_argv(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _edit_trivial(repo)
    _put(repo, "untracked.txt", "u\n")
    calls = []
    real = subprocess.run

    def rec(argv, *a, **kw):
        calls.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(MD.subprocess, "run", rec)
    out = MD.apply_clear(repo)
    assert out["ok"] is True and (repo / "untracked.txt").read_bytes() == b"u\n"
    assert calls and all(c[0] == "git" for c in calls)            # never `nw`
    verbs = [next(t for t in c[3:] if not t.startswith("-")) for c in calls]   # after `-C <root>`
    banned = {"reset", "restore", "clean", "checkout", "stash", "push", "switch"}
    assert not banned & set(verbs)
    assert not any(c[3:5] == ["branch", "-D"] or c[3:5] == ["rm", "-r"] for c in calls)
    assert not any("commit" == v and "-a" in c for v, c in zip(verbs, calls))
    assert {"add", "commit", "hash-object", "ls-files"} <= set(verbs)
    for v, c in zip(verbs, calls):
        if v in {"add", "commit", "hash-object", "ls-files", "ls-tree", "diff"}:
            assert "--literal-pathspecs" in c, c


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
    assert (doc["ok"], doc["error"]) == (False, "save not supported yet: hold")


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
