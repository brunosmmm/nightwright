"""SPEC-0539: backlog readiness reads the campaign spec from branch worktree or blob."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from nightwright.agentic import gitops as git
from nightwright.agentic import queue as Q

pytestmark = pytest.mark.fast

ENV = {
    "GIT_AUTHOR_NAME": "nightwright test",
    "GIT_AUTHOR_EMAIL": "nightwright@test",
    "GIT_COMMITTER_NAME": "nightwright test",
    "GIT_COMMITTER_EMAIL": "nightwright@test",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

REL = "docs/specs/0001-campaign.md"
BRANCH = "nw/camp-1"


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, **ENV},
    )


def _init_repo(tmp_path) -> Path:
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "README").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _spec_md(status: str = "accepted", **fm: str) -> str:
    lines = [f"id: SPEC-0001", f"status: {status}"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    return "---\n" + "\n".join(lines) + "\n---\n\nbody\n"


def _write_spec(path: Path, status: str = "accepted", **fm: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_spec_md(status, **fm))


def _campaign(repo, worktree, **over):
    st = {
        "kind": "implement",
        "id": "camp-1",
        "repo": {
            "root": str(repo),
            "branch": BRANCH,
            "worktree": str(worktree),
        },
        "spec": {"rel_path": REL},
    }
    st.update(over)
    return st


def _add_worktree(repo, wt_path, branch=BRANCH):
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(wt_path))


def test_worktree_accepted_spec_is_ready_when_base_lacks_the_file(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    spec = wt / REL
    _write_spec(spec, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "spec on branch")
    st = _campaign(repo, wt)
    assert Q.backlog_readiness({}, st) == (True, [])
    assert Q.pick_admissions([st], busy=0, slots=1, readiness=lambda s: Q.backlog_readiness({}, s)) == [st]


def test_worktree_status_governs_when_base_disagrees(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "proposed")
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec status is proposed"])
    (repo / REL).unlink()
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec status is proposed"])
    _write_spec(wt / REL, "in-progress")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec status is in-progress"])
    assert Q.backlog_readiness({}, {**_campaign(repo, wt), "force": True}) == (True, [])


def test_uncommitted_worktree_spec_is_the_readiness_source(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "accepted")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (True, [])


def test_worktree_file_beats_the_branch_blob(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "draft")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "draft on branch")
    _write_spec(wt / REL, "accepted")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (True, [])
    _write_spec(wt / REL, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "accepted on branch")
    _write_spec(wt / REL, "draft")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec status is draft"])


def test_absent_worktree_file_does_not_fall_through(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "on branch")
    (wt / REL).unlink()
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec file missing"])


def test_branch_blob_is_ready_when_the_worktree_is_not_the_checkout(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "on branch")
    _git(repo, "worktree", "remove", str(wt), "--force")
    ghost = tmp_path / "ghost-wt"
    assert Q.backlog_readiness({}, _campaign(repo, ghost)) == (True, [])


def test_missing_branch_blob_does_not_fall_back_to_base(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _git(wt, "commit", "--allow-empty", "-qm", "branch tip without spec")
    _git(repo, "worktree", "remove", str(wt), "--force")
    ghost = tmp_path / "ghost-wt"
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, _campaign(repo, ghost)) == (False, ["spec file missing"])


def test_planned_path_must_match_active_checkout_for_worktree_read(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "draft")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "draft on branch tip")
    ghost = tmp_path / "ghost"
    ghost.mkdir(parents=True)
    _write_spec(ghost / REL, "accepted")
    st = _campaign(repo, ghost)
    assert Q.backlog_readiness({}, st) == (False, ["spec status is draft"])


def test_existing_planned_dir_is_not_the_source_when_not_the_checkout(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "draft")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "draft blob")
    _git(repo, "worktree", "remove", str(wt), "--force")
    ghost = tmp_path / "ghost-wt"
    ghost.mkdir(parents=True)
    _write_spec(ghost / REL, "accepted")
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, _campaign(repo, ghost)) == (False, ["spec status is draft"])


def test_missing_branch_ignores_planned_dir_draft(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    st = _campaign(repo, wt)
    st["repo"] = {**st["repo"], "branch": "nw/missing"}
    wt.mkdir(parents=True)
    _write_spec(wt / REL, "draft")
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, st) == (True, [])


def test_missing_branch_reads_the_base_checkout(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    st = _campaign(repo, wt)
    st["repo"] = {**st["repo"], "branch": "nw/missing"}
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, st) == (True, [])
    _write_spec(repo / REL, "draft")
    assert Q.backlog_readiness({}, st) == (False, ["spec status is draft"])
    (repo / REL).unlink()
    assert Q.backlog_readiness({}, st) == (False, ["spec file missing"])


def test_branch_exists_git_error_reads_base_when_root_exists(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _write_spec(repo / REL, "accepted")

    def _git_err(_root, _branch):
        raise git.GitError(["git"], 1, "rev-parse failed")

    monkeypatch.setattr(git, "branch_exists", _git_err)
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (True, [])


def test_non_git_root_reads_the_base_file(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    st = {
        "kind": "implement",
        "repo": {"root": str(plain), "branch": "nw/c", "worktree": ""},
        "spec": {"rel_path": REL},
    }
    _write_spec(plain / REL, "accepted")
    assert Q.backlog_readiness({}, st) == (True, [])
    _write_spec(plain / REL, "draft")
    assert Q.backlog_readiness({}, st) == (False, ["spec status is draft"])
    (plain / REL).unlink()
    assert Q.backlog_readiness({}, st) == (False, ["spec file missing"])

    missing = tmp_path / "nope" / "root"
    miss_st = _campaign(missing, tmp_path / "wt")

    def _git_err(*_a, **_k):
        raise git.GitError(["git"], 1, "missing root")

    monkeypatch.setattr(git, "branch_exists", _git_err)
    assert Q.backlog_readiness({}, miss_st) == (False, ["spec file missing"])

    cwd_repo = tmp_path / "cwd-spec"
    cwd_repo.mkdir()
    _write_spec(cwd_repo / REL, "accepted")
    empty_root = {**st, "repo": {"root": "", "branch": "nw/x", "worktree": ""}}

    def _no_branch(*_a, **_k):
        raise AssertionError("branch_exists must not run")

    monkeypatch.chdir(cwd_repo)
    monkeypatch.setattr(git, "branch_exists", _no_branch)
    assert Q.backlog_readiness({}, empty_root) == (True, [])


def test_depends_on_resolves_against_the_base_specs_dir(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    dep_rel = "docs/specs/0002-d.md"
    _write_spec(repo / dep_rel, "done", id="SPEC-0002")
    _write_spec(wt / dep_rel, "accepted", id="SPEC-0002")
    _write_spec(wt / REL, "accepted", depends_on="[SPEC-0002]")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "campaign spec")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (True, [])
    (wt / dep_rel).unlink()
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (True, [])

    _write_spec(repo / dep_rel, "accepted", id="SPEC-0002")
    _write_spec(wt / dep_rel, "done", id="SPEC-0002")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (
        False,
        ["waiting on SPEC-0002 (accepted)"],
    )

    (repo / dep_rel).unlink()
    _write_spec(wt / dep_rel, "done", id="SPEC-0002")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["SPEC-0002 not found"])


def test_split_readiness_stays_on_the_base_file(tmp_path):
    repo = _init_repo(tmp_path)
    orig = "docs/specs/0005-original.md"
    base_bytes = _spec_md("accepted", id="SPEC-0005").encode()
    (repo / orig).write_bytes(base_bytes)
    review_sha = hashlib.sha256(base_bytes).hexdigest()
    _git(repo, "add", orig)
    _git(repo, "commit", "-qm", "original")
    wt = tmp_path / "wt"
    _add_worktree(repo, wt, branch="nw/split")
    _write_spec(wt / orig, "draft", id="SPEC-0005")
    st = {
        "kind": "split",
        "repo": {"root": str(repo), "branch": "nw/split", "worktree": str(wt)},
        "split": {"original_rel": orig, "review_sha": review_sha},
    }
    assert Q.backlog_readiness({}, st) == (True, [])
    (repo / orig).write_text((repo / orig).read_text() + "\nedited\n")
    assert Q.backlog_readiness({}, st) == (False, ["spec changed since the split review"])
    _write_spec(wt / orig, "accepted", id="SPEC-0005")
    (repo / orig).write_text(_spec_md("draft", id="SPEC-0005"))
    ready, blocked = Q.backlog_readiness({}, st)
    assert not ready and "spec status is draft" in blocked


def test_worktree_traversing_rel_path_is_not_ready(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    outside = tmp_path / "outside-accepted.md"
    _write_spec(outside, "accepted")
    _write_spec(wt / REL, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "on branch")
    st = _campaign(repo, wt)
    st["spec"] = {"rel_path": "../outside-accepted.md"}
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, st) == (False, ["spec file missing"])


def test_branch_blob_ignores_same_named_tag(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "on branch")
    _git(repo, "tag", BRANCH, "main")
    _git(repo, "worktree", "remove", str(wt), "--force")
    ghost = tmp_path / "ghost-wt"
    assert Q.backlog_readiness({}, _campaign(repo, ghost)) == (True, [])


def test_worktree_symlink_spec_is_not_ready(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    outside = tmp_path / "outside-spec.md"
    _write_spec(outside, "accepted")
    _write_spec(wt / REL, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "on branch")
    (wt / REL).unlink()
    (wt / REL).symlink_to(outside.resolve())
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec file missing"])


def test_branch_source_parse_failure_does_not_raise(tmp_path):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    bad_utf = wt / REL
    bad_utf.parent.mkdir(parents=True, exist_ok=True)
    bad_utf.write_bytes(b"---\nstatus: \xff\n---\n")
    _write_spec(repo / REL, "accepted")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec file missing"])

    (wt / REL).write_text("body only, no frontmatter fence\n")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec file missing"])

    (wt / REL).write_text("---\n- SPEC-0002\n---\n\nbody\n")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec file missing"])

    blob_parent = tmp_path / "blob-case"
    blob_parent.mkdir()
    repo2 = _init_repo(blob_parent)
    wt2 = tmp_path / "wt2"
    _add_worktree(repo2, wt2, branch="nw/blob")
    (wt2 / REL).parent.mkdir(parents=True, exist_ok=True)
    (wt2 / REL).write_text("---\n: bad\nstatus: accepted\n---\n")
    _git(wt2, "add", REL)
    _git(wt2, "commit", "-qm", "bad yaml")
    _git(repo2, "worktree", "remove", str(wt2), "--force")
    _write_spec(repo2 / REL, "accepted")
    st = _campaign(repo2, tmp_path / "ghost")
    st["repo"] = {"root": str(repo2), "branch": "nw/blob", "worktree": str(tmp_path / "ghost")}
    assert Q.backlog_readiness({}, st) == (False, ["spec file missing"])


def test_worktree_list_error_falls_through_to_the_blob(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "on branch")
    _write_spec(wt / REL, "draft")

    def _boom(*_a, **_k):
        raise git.GitError(["git", "worktree", "list"], 1, "fail")

    monkeypatch.setattr(git, "worktree_for_branch", _boom)
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (True, [])


@pytest.mark.parametrize("bad_dep", ["false", "no", "off"])
def test_branch_invalid_falsy_depends_on_still_waits_on_deps(tmp_path, bad_dep):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    dep_rel = "docs/specs/0002-d.md"
    _write_spec(repo / dep_rel, "accepted", id="SPEC-0002")
    fm = _spec_md("accepted").replace("status: accepted", f"status: accepted\ndepends_on: {bad_dep}")
    (wt / REL).parent.mkdir(parents=True, exist_ok=True)
    (wt / REL).write_text(fm)
    assert Q.backlog_readiness({}, _campaign(repo, wt)) != (True, [])
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "bad depends_on")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec file missing"])


@pytest.mark.parametrize("bad_dep", ["[false]", "[null]"])
def test_branch_invalid_depends_on_list_falsy_entries(tmp_path, bad_dep):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    fm = _spec_md("accepted").replace(
        "status: accepted", f"status: accepted\ndepends_on: {bad_dep}"
    )
    (wt / REL).parent.mkdir(parents=True, exist_ok=True)
    (wt / REL).write_text(fm)
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "bad depends_on list")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec file missing"])


@pytest.mark.parametrize("bad_dep", ["true", "yes", "on", "1"])
def test_branch_invalid_truthy_depends_on_does_not_raise(tmp_path, bad_dep):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    fm = _spec_md("accepted").replace("status: accepted", f"status: accepted\ndepends_on: {bad_dep}")
    (wt / REL).parent.mkdir(parents=True, exist_ok=True)
    (wt / REL).write_text(fm)
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "bad depends_on")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["spec file missing"])


def test_branch_blob_size_is_capped(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    huge = b"---\nstatus: accepted\n---\n\n" + b"x" * 500
    (wt / REL).parent.mkdir(parents=True, exist_ok=True)
    (wt / REL).write_bytes(huge)
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "huge spec")
    _git(repo, "worktree", "remove", str(wt), "--force")
    ghost = tmp_path / "ghost-wt"
    monkeypatch.setattr(Q, "MAX_WORKTREE_SPEC_BYTES", 200)
    assert Q.backlog_readiness({}, _campaign(repo, ghost)) == (False, ["spec file missing"])


def test_early_returns_still_precede_the_branch_read(tmp_path, monkeypatch):
    def _git_boom(*_a, **_k):
        raise AssertionError("git.git must not run")

    monkeypatch.setattr(git, "git", _git_boom)
    merge_ok = {
        "kind": "merge",
        "repo": {"root": str(tmp_path), "branch": "fix/x"},
        "spec": None,
    }
    monkeypatch.setattr(git, "branch_exists", lambda *_a, **_k: True)
    assert Q.backlog_readiness({}, merge_ok) == (True, [])
    monkeypatch.setattr(git, "branch_exists", lambda *_a, **_k: False)
    assert Q.backlog_readiness({}, merge_ok) == (False, ["branch fix/x missing"])
    revert = {"kind": "revert", "repo": {"root": str(tmp_path), "branch": "nw/r"}, "spec": {"rel_path": REL}}
    assert Q.backlog_readiness({}, revert) == (True, [])
