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


# ---- SPEC-0592: dependency id check, pathological spec files, real-checkout early returns ----

DEEP = "---\nstatus: " + "[" * 100000 + "\n---\n"
MISSING = (False, ["spec file missing"])


@pytest.mark.parametrize("bad", [["*"], ["../x"], ["/etc/x"], ["SPEC-0002/.."], ["SPEC-0002, SPEC-0003"],
                                 ["a-*"], ["0002"], ["SPEC-٢"], [" "]])
def test_readiness_dep_ids_reject_non_id_shapes(bad):
    assert Q._readiness_dep_ids({"depends_on": bad}) is None


def test_readiness_dep_ids_strip_and_accept_ids():
    assert Q._readiness_dep_ids({"depends_on": ["SPEC-0002 "]}) == ["SPEC-0002"]
    assert Q._readiness_dep_ids({"depends_on": "SPEC-0002"}) == ["SPEC-0002"]
    assert Q._readiness_dep_ids({"depends_on": ["SPEC-0002", "PROJ-0007"]}) == ["SPEC-0002", "PROJ-0007"]
    assert Q._readiness_dep_ids({}) == []


@pytest.mark.parametrize("dep", ["*", "/etc/x"])
def test_branch_dep_glob_star_is_not_a_wildcard(tmp_path, dep):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(repo / "docs/specs/0002-d.md", "done", id="SPEC-0002")
    _write_spec(wt / REL, "accepted", depends_on=f'["{dep}"]')
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "star dep")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == MISSING


@pytest.mark.parametrize("bad", ["*", "../x", "/abs", "", None, 7, "SPEC-0002\n", "0002"])
def test_find_dep_spec_refuses_malformed_ids(tmp_path, bad):
    (tmp_path / "0002-d.md").write_text("x")
    with pytest.raises(ValueError):
        Q._find_dep_spec(tmp_path, bad)


def test_find_dep_spec_resolves_well_formed_ids(tmp_path):
    (tmp_path / "0002-d.md").write_text("x")
    (tmp_path / "PROJ-0007-a.contract.md").write_text("x")
    (tmp_path / "PROJ-0007-a.md").write_text("x")
    assert Q._find_dep_spec(tmp_path, "SPEC-0002") == tmp_path / "0002-d.md"
    assert Q._find_dep_spec(tmp_path, "PROJ-0007") == tmp_path / "PROJ-0007-a.md"


@pytest.mark.parametrize("source", ["worktree", "blob", "base", "split"])
def test_deeply_nested_frontmatter_is_unreadable_on_every_source(tmp_path, source):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    if source in ("worktree", "blob"):
        _add_worktree(repo, wt)
        (wt / REL).parent.mkdir(parents=True, exist_ok=True)
        (wt / REL).write_text(DEEP)
        st = _campaign(repo, wt)
        if source == "blob":
            _git(wt, "add", REL)
            _git(wt, "commit", "-qm", "deep")
            _git(repo, "worktree", "remove", str(wt), "--force")
    elif source == "base":
        (repo / REL).write_text(DEEP)
        st = _campaign(repo, wt)
    else:
        (repo / REL).write_text(DEEP)
        st = {"kind": "split", "repo": {"root": str(repo), "branch": "nw/split", "worktree": str(wt)},
              "split": {"original_rel": REL, "review_sha": "x"}}
    assert Q.backlog_readiness({}, st) == MISSING


def test_invalid_yaml_base_spec_does_not_raise(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / REL).write_text("---\nstatus: [unclosed\n---\n")
    assert Q.backlog_readiness({}, _campaign(repo, tmp_path / "wt")) == MISSING


@pytest.mark.parametrize("exc", [RecursionError, MemoryError])
@pytest.mark.parametrize("source", ["base", "branch"])
def test_frontmatter_recursion_and_memory_errors_are_unreadable(tmp_path, monkeypatch, exc, source):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    if source == "branch":
        _add_worktree(repo, wt)
        _write_spec(wt / REL, "accepted")
    else:
        _write_spec(repo / REL, "accepted")

    def _boom(_text):
        raise exc()

    monkeypatch.setattr(Q, "split_frontmatter", _boom)
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == MISSING


def test_oversize_base_split_and_dep_files_are_unreadable(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    big = "---\nstatus: accepted\n---\n\n" + "x" * 500
    monkeypatch.setattr(Q, "MAX_WORKTREE_SPEC_BYTES", 200)
    (repo / REL).write_text(big)
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == MISSING
    split = {"kind": "split", "repo": {"root": str(repo), "branch": "nw/split", "worktree": str(wt)},
             "split": {"original_rel": REL, "review_sha": "x"}}
    assert Q.backlog_readiness({}, split) == MISSING
    # a small campaign spec with an oversize dependency file
    small = "---\nid: SPEC-0001\nstatus: accepted\ndepends_on: [SPEC-0002]\n---\n"
    assert len(small) < 200
    (repo / REL).write_text(small)
    (repo / "docs/specs/0002-d.md").write_text("---\nstatus: done\n---\n\n" + "x" * 500)
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, ["SPEC-0002 not found"])


def test_special_files_are_unreadable(tmp_path):
    fifo = tmp_path / "0002-fifo.md"
    os.mkfifo(fifo)
    assert Q._read_frontmatter_text(fifo) is None            # must not block
    assert Q._read_frontmatter_text(tmp_path) is None
    assert Q._read_frontmatter_text(tmp_path / "absent.md") is None
    real = tmp_path / "real.md"
    real.write_text("---\nstatus: done\n---\n")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    assert Q._read_frontmatter_text(link) == {"status": "done"}


def _dep_campaign_with(tmp_path, dep_body, **fm):
    repo = _init_repo(tmp_path)
    (repo / REL).write_text(_spec_md("accepted", depends_on="[SPEC-0002]", **fm))
    (repo / "docs/specs/0002-d.md").write_text(dep_body)
    return repo, _campaign(repo, tmp_path / "wt")


@pytest.mark.parametrize("body", [DEEP, "---\nstatus: [unclosed\n---\n", "---\nstatus: \xff\n---\n"])
def test_pathological_dependency_file_is_not_found(tmp_path, body):
    repo, st = _dep_campaign_with(tmp_path, "")
    (repo / "docs/specs/0002-d.md").write_bytes(body.encode("latin-1", "replace")
                                                if "\xff" in body else body.encode())
    assert Q.backlog_readiness({}, st) == (False, ["SPEC-0002 not found"])


def test_pathological_outbound_dependency_stays_met_when_merged(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    (repo / REL).write_text(_spec_md("accepted", depends_on="[PROJ-0007]"))
    (repo / "docs/specs/PROJ-0007-x.md").write_text(DEEP)
    st = _campaign(repo, tmp_path / "wt")
    assert Q.backlog_readiness({}, st) == (False, ["PROJ-0007 not found"])
    monkeypatch.setattr(Q, "_dep_campaign", lambda *_a, **_k: {"state": "MERGED"})
    assert Q.backlog_readiness({}, st) == (True, [])


@pytest.mark.parametrize("kind", ["deep", "yaml", "oversize"])
def test_after_spec_file_pathological_is_not_found(tmp_path, monkeypatch, kind):
    specs = tmp_path / "docs" / "specs"
    specs.mkdir(parents=True)
    body = {"deep": DEEP, "yaml": "---\nstatus: [unclosed\n---\n",
            "oversize": "---\nstatus: done\n---\n\n" + "x" * 500}[kind]
    if kind == "oversize":
        monkeypatch.setattr(Q, "MAX_WORKTREE_SPEC_BYTES", 200)
    (specs / "0002-d.md").write_text(body)
    st = {"project": "p", "repo": {"root": str(tmp_path)}, "spec": {"rel_path": "docs/specs/0001-c.md"}}
    camps: list = []
    monkeypatch.setattr(Q.store, "list_campaigns", lambda *_a, **_k: camps)
    assert Q._spec_after_met({}, st, "SPEC-0002") == (False, "SPEC-0002 not found", False)
    camps.append({"id": "c2", "state": "MERGED"})
    assert Q._spec_after_met({}, st, "SPEC-0002") == (True, "", False)


def test_after_spec_malformed_id_is_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(Q.store, "list_campaigns", lambda *_a, **_k: [])
    st = {"project": "p", "repo": {"root": str(tmp_path)}, "spec": {"rel_path": "docs/specs/0001-c.md"}}
    assert Q._spec_after_met({}, st, "*") == (False, "* not found", False)


def test_non_mapping_dependency_frontmatter_has_no_status(tmp_path):
    _repo, st = _dep_campaign_with(tmp_path, "---\n- a\n- b\n---\n")
    assert Q.backlog_readiness({}, st) == (False, ["waiting on SPEC-0002 (no status)"])


def test_dependency_met_skips_unparsable_replacement(tmp_path):
    (tmp_path / "0002-old.md").write_text("---\nid: SPEC-0002\nstatus: superseded\n---\n")
    (tmp_path / "0003-new.md").write_text("---\nid: SPEC-0003\nstatus: done\nsupersedes: [SPEC-0002]\n---\n")
    (tmp_path / "0004-bad.md").write_text(DEEP)
    (tmp_path / "0005-bad.md").write_text("---\nid: [unclosed\n---\n")
    old = {"id": "SPEC-0002", "status": "superseded"}
    assert Q.dependency_met(tmp_path, "SPEC-0002", old, set()) == (True, "superseded")
    (tmp_path / "0003-new.md").write_text(
        "---\nid: SPEC-0003\nstatus: accepted\nsupersedes: [SPEC-0002]\n---\n")
    assert Q.dependency_met(tmp_path, "SPEC-0002", old, set()) == (
        False, "superseded; waiting on SPEC-0003")


def test_well_formed_ids_are_unchanged(tmp_path):
    _repo, st = _dep_campaign_with(tmp_path, "---\nid: SPEC-0002\nstatus: done\n---\n")
    assert Q.backlog_readiness({}, st) == (True, [])


def _checked_out_accepted(tmp_path):
    """A repo whose branch `nw/camp-1` is really checked out at the planned worktree and holds an
    `accepted` spec: without an early return `backlog_readiness` would say `(True, [])`."""
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _add_worktree(repo, wt)
    _write_spec(wt / REL, "accepted")
    _git(wt, "add", REL)
    _git(wt, "commit", "-qm", "accepted on branch")
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (True, [])
    return repo, wt


def test_early_returns_win_over_a_checked_out_accepted_spec(tmp_path, monkeypatch):
    from types import SimpleNamespace

    repo, wt = _checked_out_accepted(tmp_path)

    def _no_read(_state):
        raise AssertionError("_campaign_spec_text must not run")

    monkeypatch.setattr(Q, "_campaign_spec_text", _no_read)
    assert Q.backlog_readiness({}, _campaign(repo, wt, kind="revert")) == (True, [])
    assert Q.backlog_readiness({}, _campaign(repo, wt, kind="merge")) == (True, [])
    gone = {**_campaign(repo, wt, kind="merge"), "repo": {"root": str(repo), "branch": "nw/absent"}}
    assert Q.backlog_readiness({}, gone) == (False, ["branch nw/absent missing"])

    unmet = ["waiting on SPEC-0099 (accepted)"]
    monkeypatch.setattr(Q, "_after_blocked", lambda _c, _s: unmet)
    assert Q.backlog_readiness({}, _campaign(repo, wt)) == (False, unmet)
    idea = _campaign(repo, wt, kind="idea", spec=None, idea={"id": "IDEA-1"})
    assert Q.backlog_readiness({}, idea) == (False, unmet)

    monkeypatch.setattr(Q, "_after_blocked", lambda _c, _s: [])
    monkeypatch.setattr(Q, "_idea_task",
                        lambda _c, _i: SimpleNamespace(state="IDEA", properties={}, id="IDEA-1"))
    assert Q.backlog_readiness({}, idea) == (True, [])
    monkeypatch.setattr(Q, "_idea_task",
                        lambda _c, _i: SimpleNamespace(state="DONE", properties={}, id="IDEA-1"))
    assert Q.backlog_readiness({}, idea) == (False, ["idea IDEA-1 is DONE"])
