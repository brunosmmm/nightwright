"""SPEC-0172: git worktree / branch operations for campaigns. Every repo lives in tmp_path."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

from nightwright.agentic import gitops as G

ENV = {
    "GIT_AUTHOR_NAME": "nightwright test", "GIT_AUTHOR_EMAIL": "nightwright@test",
    "GIT_COMMITTER_NAME": "nightwright test", "GIT_COMMITTER_EMAIL": "nightwright@test",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _write_commit(repo, name, text, msg):
    (Path(repo) / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _write_commit(r, "README.md", "hello\n", "init")
    return r


@pytest.fixture
def wt(repo, tmp_path):
    return G.worktree_add(repo, tmp_path / "wts" / "c1", "nw/c1", "main")


def test_read_only_helpers(repo):
    assert G.current_branch(repo) == "main"
    assert G.rev_parse(repo) == _git(repo, "rev-parse", "HEAD")
    assert G.is_clean(repo)
    (repo / "untracked.txt").write_text("x")
    assert G.is_clean(repo, untracked=False)
    assert not G.is_clean(repo, untracked=True)
    assert G.status_porcelain(repo) == ["?? untracked.txt"]
    _git(repo, "checkout", "-q", "--detach")
    assert G.current_branch(repo) is None


def test_worktree_add_and_remove(repo, tmp_path):
    path = G.worktree_add(repo, tmp_path / "wts" / "a", "nw/a", "main")
    assert path.is_dir() and (path / "README.md").exists()
    assert G.current_branch(path) == "nw/a"
    with pytest.raises(G.GitError, match="already exists"):
        G.worktree_add(repo, tmp_path / "wts" / "a", "nw/other", "main")
    with pytest.raises(G.GitError, match="branch nw/a already exists"):
        G.worktree_add(repo, tmp_path / "wts" / "b", "nw/a", "main")
    G.worktree_remove(repo, path)
    assert not path.exists()
    assert "wts/a" not in _git(repo, "worktree", "list")
    G.branch_delete(repo, "nw/a")
    assert "nw/a" not in _git(repo, "branch", "--list", "nw/a")


def test_commit_all_none_when_clean_and_sha_with_trailer(wt):
    assert G.commit_all(wt, "nothing") is None
    (wt / "f.txt").write_text("1\n")
    msg = "nw(c1): implementer run 001 — add f\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
    sha = G.commit_all(wt, msg)
    assert sha == _git(wt, "rev-parse", "HEAD")
    assert _git(wt, "log", "-1", "--format=%B").strip() == msg
    assert _git(wt, "log", "-1", "--format=%an") == "nightwright test"


def test_diff_stat_and_ahead_behind(repo, wt):
    (wt / "f.txt").write_text("a\nb\n")
    G.commit_all(wt, "two lines")
    assert G.diff_stat(wt, "main") == {"files": 1, "insertions": 2, "deletions": 0}
    assert G.ahead_behind(wt, "main") == (1, 0)
    _write_commit(repo, "other.txt", "x\n", "main moves")
    assert G.ahead_behind(wt, "main") == (1, 1)


def test_rebase_clean_fast_forwards_onto_new_base(repo, wt):
    (wt / "f.txt").write_text("feature\n")
    G.commit_all(wt, "feature")
    base = _write_commit(repo, "other.txt", "x\n", "main moves")
    res = G.rebase(wt, "main")
    assert res.ok and res.conflicts == ()
    assert _git(wt, "rev-parse", "HEAD~1") == base
    assert (wt / "other.txt").exists() and (wt / "f.txt").read_text() == "feature\n"


def test_rebase_conflict_aborts_and_restores_tree(repo, wt):
    (wt / "README.md").write_text("branch side\n")
    G.commit_all(wt, "branch edit")
    _write_commit(repo, "README.md", "main side\n", "main edit")
    head_before = _git(wt, "rev-parse", "HEAD")
    tree_before = _git(wt, "rev-parse", "HEAD^{tree}")
    res = G.rebase(wt, "main")
    assert res.ok is False
    assert res.conflicts == ("README.md",)
    assert res.head == head_before
    assert _git(wt, "rev-parse", "HEAD^{tree}") == tree_before
    assert (wt / "README.md").read_text() == "branch side\n"
    assert not (Path(_git(wt, "rev-parse", "--git-dir")) / "rebase-merge").exists()
    assert G.is_clean(wt)


def test_ff_merge_succeeds_when_ahead_and_refuses_when_diverged(repo, wt):
    (wt / "f.txt").write_text("1\n")
    sha = G.commit_all(wt, "feature")
    assert G.ff_merge(repo, "nw/c1") == sha
    assert _git(repo, "rev-parse", "HEAD") == sha
    (wt / "f.txt").write_text("2\n")
    G.commit_all(wt, "feature 2")
    main_sha = _write_commit(repo, "other.txt", "x\n", "main diverges")
    with pytest.raises(G.GitError) as exc:
        G.ff_merge(repo, "nw/c1")
    assert exc.value.code != 0 and "ff-only" in " ".join(exc.value.cmd)
    assert _git(repo, "rev-parse", "HEAD") == main_sha


def test_fetch_and_push_against_bare_remote(repo, wt, tmp_path):
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")
    (wt / "f.txt").write_text("1\n")
    sha = G.commit_all(wt, "feature")
    G.push_branch(wt, "origin", "nw/c1")
    assert _git(bare, "rev-parse", "refs/heads/nw/c1") == sha
    assert _git(wt, "rev-parse", "--abbrev-ref", "nw/c1@{upstream}") == "origin/nw/c1"
    G.fetch(repo, "origin", "nw/c1")
    assert _git(repo, "rev-parse", "FETCH_HEAD") == sha


def _fake_gh(tmp_path, body, code=0):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    log = tmp_path / "gh-argv.txt"
    script = d / "gh"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log}\n{body}\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return d, log


def test_gh_pr_create_uses_path_gh_and_returns_url(wt, tmp_path, monkeypatch):
    d, log = _fake_gh(tmp_path, "echo 'Creating pull request'; echo https://github.com/x/y/pull/7")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    body = tmp_path / "body.md"
    body.write_text("# PR\n")
    url = G.gh_pr_create(wt, base="main", head="nw/c1", title="SPEC-1: t", body_file=body,
                         draft=True, labels=("nw-agentic", "x"))
    assert url == "https://github.com/x/y/pull/7"
    argv = log.read_text().split("\n")
    assert argv[:9] == ["pr", "create", "--base", "main", "--head", "nw/c1", "--title",
                        "SPEC-1: t", "--body-file"]
    assert argv[9] == str(body)
    assert "--draft" in argv and argv[argv.index("--label") + 1] == "nw-agentic"


def test_gh_pr_create_failure_raises_with_tail(wt, tmp_path, monkeypatch):
    d, _ = _fake_gh(tmp_path, "echo 'auth required' >&2", code=4)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(G.GitError) as exc:
        G.gh_pr_create(wt, base="main", head="nw/c1", title="t", body_file=tmp_path / "b.md")
    assert exc.value.code == 4 and "auth required" in exc.value.tail


def test_git_error_on_bad_command(repo):
    with pytest.raises(G.GitError) as exc:
        G.git(repo, "rev-parse", "--verify", "nope^{commit}")
    assert exc.value.code != 0 and exc.value.tail
    assert G.git(repo, "rev-parse", "nope", check=False).returncode != 0


def test_no_new_dependency():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert "gitpython" not in text.lower() and "dulwich" not in text.lower()


# ---- SPEC-0191: regenerate generated files on rebase conflict -------------------------------

REGEN = {"gen.txt": "echo regenerated > gen.txt"}


@pytest.fixture
def conflicting(repo, tmp_path):
    _write_commit(repo, "gen.txt", "orig\n", "gen")
    w = G.worktree_add(repo, tmp_path / "wts" / "rg", "nw/rg", "main")
    _write_commit(w, "gen.txt", "branch\n", "branch gen")
    _write_commit(repo, "gen.txt", "main\n", "main gen")
    return repo, w


def test_rebase_regenerates_declared_conflict(conflicting):
    repo, w = conflicting
    r = G.rebase(w, G.rev_parse(repo), regenerate=REGEN)
    assert r.ok and r.regenerated == ("gen.txt",)
    assert (w / "gen.txt").read_text() == "regenerated\n"
    assert G.is_clean(w)
    assert _git(w, "merge-base", "--is-ancestor", "main", "HEAD") == ""


def test_rebase_without_regenerate_still_aborts(conflicting):
    repo, w = conflicting
    head = G.rev_parse(w)
    r = G.rebase(w, G.rev_parse(repo))
    assert not r.ok and r.conflicts == ("gen.txt",) and r.regenerated == ()
    assert G.rev_parse(w) == head


def test_rebase_mixed_conflict_aborts_and_restores(conflicting):
    repo, w = conflicting
    _write_commit(w, "other.txt", "branch\n", "branch other")
    _write_commit(repo, "other.txt", "main\n", "main other")
    # make the branch's own commits conflict in both files at once
    head, tree = G.rev_parse(w), _git(w, "rev-parse", "HEAD^{tree}")
    r = G.rebase(w, G.rev_parse(repo), regenerate=REGEN)
    assert not r.ok and "other.txt" in r.conflicts
    assert G.rev_parse(w) == head and _git(w, "rev-parse", "HEAD^{tree}") == tree
    assert G.is_clean(w)


def test_rebase_failing_regen_cmd_aborts(conflicting):
    repo, w = conflicting
    head = G.rev_parse(w)
    r = G.rebase(w, G.rev_parse(repo), regenerate={"gen.txt": "exit 3"})
    assert not r.ok and r.conflicts == ("gen.txt",) and r.regenerated == ()
    assert G.rev_parse(w) == head and G.is_clean(w)


def test_changed_files_head_and_no_renames(repo):
    (repo / "src").mkdir()
    _write_commit(repo, "src/x.py", "print('x')\n" * 20, "add x")
    _git(repo, "checkout", "-q", "-b", "mv")
    (repo / "docs").mkdir()
    _git(repo, "mv", "src/x.py", "docs/x.py")
    _git(repo, "commit", "-q", "-m", "move")
    _git(repo, "checkout", "-q", "main")
    assert G.changed_files(repo, "main", head="mv") == ["docs/x.py"]          # rename hides src/x.py
    assert sorted(G.changed_files(repo, "main", head="mv", renames=False)) == [
        "docs/x.py", "src/x.py"]
    assert G.changed_files(repo, "main") == []                                # head defaults to HEAD


def test_changed_files_returns_unquoted_unusual_names(repo):
    _git(repo, "checkout", "-q", "-b", "odd")
    (repo / "docs").mkdir()
    names = ["docs/caf\u00e9.md", 'docs/a "q" b.md', "docs/tab\there.md"]
    for n in names:
        (repo / n).write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "odd")
    _git(repo, "checkout", "-q", "main")
    assert sorted(G.changed_files(repo, "main", head="odd")) == sorted(names)
    assert G.docs_only_violations(G.changed_files(repo, "main", head="odd"), ["docs/"]) == []


def test_docs_only_violations_exact_and_prefix_only():
    allow = ["docs/design/", "docs/WORKFLOW.md"]
    assert G.docs_only_violations(["docs/design/a.py", "docs/design/sub/b.scss",
                                   "docs/WORKFLOW.md"], allow) == []
    paths = ["docs/designer.md", "docs/WORKFLOW.md.bak", "docs/design", "docs/specs/x.md",
             "docs/LEDGER.md", "README.md", "skills/x/SKILL.md", "src/nightwright/agentic/personas/x.md"]
    assert G.docs_only_violations(paths, allow) == paths
    assert G.docs_only_violations(["a"], []) == ["a"]
