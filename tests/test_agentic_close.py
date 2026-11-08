"""SPEC-0175: close step — rebase, re-gate, ff-only merge or PR hand-off. Every repo lives in
tmp_path: a bare origin, a "main" clone (the main checkout), and a campaign worktree. `gh` is
a fake script on PATH; nothing calls the real gh or claude."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from nightwright import verify as V
from nightwright.agentic import close as CL
from nightwright.agentic import gates as GT
from nightwright.agentic import gitops as G
from nightwright.agentic import project as P

ENV = {
    "GIT_AUTHOR_NAME": "nightwright test", "GIT_AUTHOR_EMAIL": "nightwright@test",
    "GIT_COMMITTER_NAME": "nightwright test", "GIT_COMMITTER_EMAIL": "nightwright@test",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

SPEC_DONE = """---
id: SPEC-0001
title: "Trivial feature"
status: done
owner: t
created: 2026-10-25
---

## Context

c

## Decision

Ship the feature file.

## Acceptance criteria

- [x] feature.txt exists

## Test plan

- run the gates

## Definition of done

- [x] done
"""


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, name, text, msg):
    p = Path(repo) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _project_yaml(gates=("true",), policy="merge", **extra):
    gate_lines = "\n".join(
        f"  - {{name: g{i}, cmd: '{cmd}', timeout_s: 30, required: {str(req).lower()}}}"
        for i, (cmd, req) in enumerate(
            (g if isinstance(g, tuple) else (g, True)) for g in gates))
    merge = "\n".join(f"  {k}: {v}" for k, v in extra.items())
    return (f"schema: nw.project.v1\nbase: main\ngates:\n{gate_lines}\n"
            f"merge:\n  policy: {policy}\n{merge}\n")


@pytest.fixture
def world(tmp_path):
    """bare origin ← root clone (main checkout); worktree on branch nw/c1 with a done spec +
    feature file committed on top of main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "README.md", "hello\n", "init")
    (seed / ".nw").mkdir()
    (seed / ".nw" / "project.yaml").write_text(_project_yaml())
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "project config")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    root = tmp_path / "root"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(root)], check=True)
    wt_path = tmp_path / "wts" / "c1"
    G.worktree_add(root, wt_path, "nw/c1", "main")
    _commit(wt_path, "docs/specs/0001-t.md", SPEC_DONE, "spec done")
    _commit(wt_path, "feature.txt", "yes\n", "feature")
    cfg = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg")}
    state = {
        "id": "SPEC-0001-20261025-abcd", "spec_id": "SPEC-0001", "project": "demo",
        "repo": {"root": str(root), "base": "main", "branch": "nw/c1", "worktree": str(wt_path)},
        "spec": {"rel_path": "docs/specs/0001-t.md", "internal": True},
        "counters": {"loops": 1, "runs": 2},
        "cost": {"total_usd": 1.5, "cap_usd": 10.0, "by_persona": {"implementer": 1.5}},
    }
    cdir = tmp_path / "data" / "agentic" / "campaigns" / state["id"]
    cdir.mkdir(parents=True)
    # SPEC-0186: the accepted text the queue saves at open (before the implementer's edits)
    (cdir / "spec_at_open.md").write_text(
        SPEC_DONE.replace("status: done", "status: accepted").replace("- [x]", "- [ ]"))
    return {"origin": origin, "root": root, "nw": wt_path, "cfg": cfg, "state": state,
            "tmp": tmp_path}


def _pcfg(world, **kw):
    path = world["root"] / ".nw" / "project.yaml"
    if kw:
        path.write_text(_project_yaml(**kw))
        _git(world["root"], "commit", "-q", "-am", "cfg")     # keep main clean
    return P.load_project_config(path)


def _fake_gh(tmp_path, monkeypatch, *, fail=False):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    gh = d / "gh"
    log = tmp_path / "gh-args.txt"
    gh.write_text("#!/bin/sh\n" + (f"echo \"$@\" > {log}\n") +
                  ("echo boom >&2; exit 1\n" if fail else "echo https://example.test/pr/7\n"))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    return log


def _events(out, type_):
    return [p for t, p in out.events if t == type_]


# ---- merge policy ---------------------------------------------------------------------

def test_happy_merge_ff_post_cmds_cleanup(world):
    marker = world["tmp"] / "post-ran"
    pcfg = _pcfg(world, post_cmds=f"['touch {marker}']", delete_branch="true")
    pcfg["worktrees"]["cleanup"] = "on_merge"
    head = G.rev_parse(world["nw"])
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED", out
    assert out.close["mode"] == "merge" and out.close["merged_sha"] == G.rev_parse(world["root"])
    assert G.rev_parse(world["root"]) != head          # rebased onto the cfg commit on main
    assert (world["root"] / "feature.txt").read_text() == "yes\n"
    assert marker.exists()
    assert not world["nw"].exists()
    assert "nw/c1" not in _git(world["root"], "branch", "--list", "nw/c1")
    assert [t for t, _ in out.events] == ["gate", "close"]
    assert out.gate_run.ok and _events(out, "gate")[0]["gate_seq"] == 1


def test_dirty_main_parks_but_untracked_is_fine(world):
    pcfg = _pcfg(world)
    (world["root"] / "notes.txt").write_text("untracked")            # untracked only
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED"


def test_dirty_main_tracked_change_parks(world):
    pcfg = _pcfg(world)
    (world["root"] / "README.md").write_text("edited\n")             # tracked, uncommitted
    before = G.rev_parse(world["root"])
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "main_dirty"
    assert "README.md" in out.park["detail"] and out.park["resume_to"] == "CLOSING"
    assert G.rev_parse(world["root"]) == before
    assert (world["root"] / "README.md").read_text() == "edited\n"


def test_main_on_another_branch_parks(world):
    pcfg = _pcfg(world)
    _git(world["root"], "checkout", "-q", "-b", "other")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "main_not_on_base"


def test_base_advanced_without_conflict_rebases_then_merges(world):
    pcfg = _pcfg(world)
    _commit(world["root"], "other.txt", "o\n", "base moves")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED"
    assert (world["root"] / "other.txt").exists() and (world["root"] / "feature.txt").exists()
    assert G.rev_parse(world["root"]) == out.close["merged_sha"]


def test_base_conflict_parks_and_restores_worktree(world):
    pcfg = _pcfg(world)
    _commit(world["root"], "feature.txt", "theirs\n", "conflicting base")
    head = G.rev_parse(world["nw"])
    tree = _git(world["nw"], "rev-parse", "HEAD^{tree}")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "rebase_conflict"
    assert "feature.txt" in out.park["detail"]
    assert G.rev_parse(world["nw"]) == head
    assert _git(world["nw"], "rev-parse", "HEAD^{tree}") == tree
    assert not (world["nw"] / ".git" / "rebase-merge").exists()
    assert _events(out, "gate") == []                   # parked before the re-gate


def test_base_moving_retries_then_parks(world):
    pcfg = _pcfg(world)
    moves = {"n": 0}

    def move_base():
        moves["n"] += 1
        _commit(world["root"], f"m{moves['n']}.txt", "x\n", f"move {moves['n']}")

    out = CL.close_campaign(world["cfg"], world["state"], pcfg, max_attempts=2,
                            _hooks={"before_merge": move_base})
    assert out.next_state == "PARKED" and out.park["reason"] == "base_moving"
    assert out.attempts == 2 and moves["n"] == 2
    assert len(_events(out, "gate")) == 2               # re-gated on each attempt


def test_base_moving_once_succeeds_on_second_attempt(world):
    pcfg = _pcfg(world)
    moves = {"n": 0}

    def move_once():
        if moves["n"] == 0:
            moves["n"] += 1
            _commit(world["root"], "m1.txt", "x\n", "move once")

    out = CL.close_campaign(world["cfg"], world["state"], pcfg, max_attempts=3,
                            _hooks={"before_merge": move_once})
    assert out.next_state == "MERGED" and out.attempts == 2
    assert (world["root"] / "m1.txt").exists() and (world["root"] / "feature.txt").exists()


def test_regate_failure_goes_to_triaging(world):
    pcfg = _pcfg(world, gates=("true", "false"))
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "TRIAGING" and out.gate_run is not None and not out.gate_run.ok
    assert [r.name for r in GT.blocking_failures(out.gate_run)] == ["g1"]
    assert G.rev_parse(world["root"]) == _git(world["root"], "rev-parse", "HEAD")
    assert not (world["root"] / "feature.txt").exists()
    assert (world["tmp"] / "data" / "agentic" / "campaigns" / world["state"]["id"] / "gates"
            / "001-g1.log").is_file()


def test_optional_gate_failure_does_not_block(world):
    pcfg = _pcfg(world, gates=("true", ("false", False)))
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED" and out.gate_run.ok


@pytest.mark.parametrize("edit", ["status: in-progress", "- [ ] feature.txt exists"])
def test_spec_not_done_goes_to_triaging_with_spec_dod(world, edit):
    pcfg = _pcfg(world)
    spec = world["nw"] / "docs" / "specs" / "0001-t.md"
    text = SPEC_DONE.replace("status: done", edit) if edit.startswith("status") \
        else SPEC_DONE.replace("- [x] feature.txt exists", edit)
    spec.write_text(text)
    _git(world["nw"], "commit", "-q", "-am", "not done")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "TRIAGING"
    assert out.synthetic_gate["name"] == "spec-dod" and out.synthetic_gate["required"]
    assert "done" in out.synthetic_gate["tail"] or "unchecked" in out.synthetic_gate["tail"]
    assert _events(out, "finding")[0]["gate"] == "spec-dod"
    assert _events(out, "gate") == []                   # no gates were run
    assert not (world["root"] / "feature.txt").exists()


def test_uncommitted_worktree_changes_are_snapshotted(world):
    pcfg = _pcfg(world)
    (world["nw"] / "extra.txt").write_text("late\n")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED"
    assert _events(out, "commit")[0]["sha"]
    assert (world["root"] / "extra.txt").exists()


def test_push_after_merge(world):
    pcfg = _pcfg(world, push_after_merge="true")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED"
    assert _git(world["origin"], "rev-parse", "main") == out.close["merged_sha"]


# ---- pr policy -------------------------------------------------------------------------

def test_close_regate_is_local_only_for_a_merge(world, monkeypatch):
    """`local=True` is the merge policy. pr / draft-pr do not merge on the coordinator, so
    their close gates stay eligible for a worker."""
    seen = []
    real = CL.G.run_all

    def spy(*a, **kw):
        seen.append(kw.get("local"))
        return real(*a, **kw)

    monkeypatch.setattr(CL.G, "run_all", spy)
    out = CL.close_campaign(world["cfg"], world["state"], _pcfg(world))
    assert out.next_state == "MERGED" and seen == [True]


def test_pr_close_regate_is_not_forced_local(world, monkeypatch):
    seen = []
    real = CL.G.run_all

    def spy(*a, **kw):
        seen.append(kw.get("local"))
        return real(*a, **kw)

    monkeypatch.setattr(CL.G, "run_all", spy)
    _fake_gh(world["tmp"], monkeypatch)
    out = CL.close_campaign(world["cfg"], world["state"], _pcfg(world, policy="pr"))
    assert out.next_state == "HANDED_OFF", out
    assert seen == [False]


def test_pr_policy_hands_off_with_body(world, monkeypatch):
    log = _fake_gh(world["tmp"], monkeypatch)
    pcfg = _pcfg(world, policy="pr")
    pcfg["merge"]["pr"]["labels"] = ["nw-agentic"]
    pcfg["worktrees"]["cleanup"] = "on_close"
    main_before = G.rev_parse(world["root"])
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "HANDED_OFF", out
    assert out.close == {"mode": "pr", "merged_sha": None, "pr_url": "https://example.test/pr/7",
                         "gates_seq": 1, "target_sha": out.close["target_sha"]}
    args = log.read_text()
    assert "--base main --head nw/c1" in args and "--label nw-agentic" in args
    assert "--draft" not in args
    assert "--title SPEC-0001: Trivial feature" in args
    assert _git(world["origin"], "rev-parse", "nw/c1")            # branch pushed
    body = (world["tmp"] / "data" / "agentic" / "campaigns" / world["state"]["id"] / "runs"
            / "pr-body.md").read_text()
    assert "Ship the feature file." in body
    assert "- [x] feature.txt exists" in body
    assert "| g0 | ok |" in body and "SPEC-0001-20261025-abcd" in body
    assert body.rstrip().endswith("🤖 Generated with [Claude Code](https://claude.com/claude-code)")
    assert not world["nw"].exists()                                  # on_close cleanup
    assert "nw/c1" in _git(world["root"], "branch", "--list", "nw/c1")   # branch kept
    assert G.rev_parse(world["root"]) == main_before                 # main untouched


def test_draft_pr_passes_draft_flag(world, monkeypatch):
    log = _fake_gh(world["tmp"], monkeypatch)
    pcfg = _pcfg(world, policy="draft-pr")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "HANDED_OFF" and out.close["mode"] == "draft-pr"
    assert "--draft" in log.read_text()
    assert world["nw"].exists()                                      # on_merge: PR keeps worktree


def test_gh_failure_parks(world, monkeypatch):
    _fake_gh(world["tmp"], monkeypatch, fail=True)
    pcfg = _pcfg(world, policy="pr")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "gh_failed"
    assert "boom" in out.park["detail"] and out.park["resume_to"] == "CLOSING"


# ---- pr_body is pure ------------------------------------------------------------------

def test_pr_body_pure_and_complete(world):
    from nightwright.specmeta import sections, split_frontmatter
    fm, body = split_frontmatter(SPEC_DONE)
    a = CL.pr_body(world["state"], fm, sections(body), None)
    b = CL.pr_body(world["state"], fm, sections(body), None)
    assert a == b
    assert "(no gate run recorded)" in a and "loops: 1 · runs: 2" in a and "$1.50" in a


# ---- verify_spec_file parity -----------------------------------------------------------

def test_verify_spec_file_matches_verify_spec(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    path = specs / "0001-t.md"
    path.write_text(SPEC_DONE)
    via_id = V.verify_spec({"specs_dir": str(specs)}, "SPEC-0001")
    via_path = V.verify_spec_file(path, specs_dir=specs)
    assert via_id == via_path
    assert via_path["ok"] and via_path["status"] == "done" and via_path["id"] == "SPEC-0001"
    no_dir = V.verify_spec_file(path)
    assert no_dir["ok"] and [c["status"] for c in no_dir["checks"]
                              if c["name"].startswith("epic_")] == ["skip", "skip"]
    with pytest.raises(ValueError, match="not found"):
        V.verify_spec_file(specs / "missing.md")


# ---- SPEC-0191: generated-file conflicts are regenerated, gates still re-run -------------

def test_ledger_style_conflict_is_regenerated_then_gated_and_merged(world):
    _commit(world["root"], "gen.txt", "orig\n", "gen")
    _git(world["nw"], "rebase", "-q", "main")
    _commit(world["nw"], "gen.txt", "branch\n", "branch gen")
    _commit(world["root"], "gen.txt", "main\n", "main gen")
    pcfg = _pcfg(world)
    pcfg["merge"]["regenerate"] = [{"path": "gen.txt", "cmd": "echo regenerated > gen.txt"}]
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "gen.txt").read_text() == "regenerated\n"
    gate_idx = [t for t, _ in out.events].index("gate")
    info = [p for t, p in out.events if t == "finding"
            and p["title"] == "generated files regenerated during rebase"]
    assert info and info[0]["severity"] == "info" and "gen.txt" in info[0]["detail"]
    assert gate_idx >= 0 and out.gate_run.ok


def test_undeclared_conflict_still_parks(world):
    pcfg = _pcfg(world)
    pcfg["merge"]["regenerate"] = [{"path": "gen.txt", "cmd": "true"}]
    _commit(world["root"], "feature.txt", "theirs\n", "conflicting base")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "rebase_conflict"


# ---- SPEC-0233: a merge request closes without a spec ------------------------------------------

def _merge_world(world):
    """The campaign becomes a merge request for `nw/c1` (adopted worktree, no spec file)."""
    head = G.rev_parse(world["nw"])
    G.git(world["nw"], "reset", "-q", "--hard", "main")             # drop the spec commit
    (world["nw"] / "feature.txt").write_text("yes\n")
    G.commit_all(world["nw"], "feature")
    st = world["state"]
    st.update(kind="merge", spec_id=None, spec=None, merge={
        "branch": "nw/c1", "head_at_request": G.rev_parse(world["nw"]), "own_worktree": False})
    (world["tmp"] / "data" / "agentic" / "campaigns" / st["id"] / "spec_at_open.md").unlink()
    return st


def test_merge_request_closes_without_a_spec_and_keeps_the_adopted_worktree(world):
    st = _merge_world(world)
    pcfg = _pcfg(world, delete_branch="true")
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "feature.txt").exists()
    assert world["nw"].is_dir()                                      # not ours to remove
    assert "nw/c1" in _git(world["root"], "branch", "--list", "nw/c1")   # never deleted
    close = _events(out, "close")[0]
    assert close["commits_since_request"] == 0 and close["head_at_request"]


def test_merge_request_dirty_worktree_parks_without_a_commit(world):
    st = _merge_world(world)
    before = _git(world["nw"], "log", "--format=%H")
    (world["nw"] / "feature.txt").write_text("edited\n")
    out = CL.close_campaign(world["cfg"], st, _pcfg(world))
    assert out.next_state == "PARKED" and out.park["reason"] == "worktree_dirty"
    assert out.park["resume_to"] == "CLOSING" and "feature.txt" in out.park["detail"]
    assert _git(world["nw"], "log", "--format=%H") == before


def test_merge_request_red_regate_is_triaging_with_the_branch_left_rebased(world):
    st = _merge_world(world)
    pcfg = _pcfg(world, gates=("echo boom >&2; false",))
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "TRIAGING" and not out.gate_run.ok
    assert out.gate_detail.startswith("gates failed: g0") and "boom" in out.gate_detail
    assert "branch rebased onto main" in out.gate_detail
    assert G.git(world["nw"], "merge-base", "--is-ancestor", "main", "HEAD", check=False).returncode == 0


def test_merge_request_in_progress_rebase_of_the_requester_is_parked_not_aborted(world):
    st = _merge_world(world)
    _commit(world["root"], "feature.txt", "theirs\n", "conflicting base")
    G.git(world["nw"], "rebase", "main", check=False)                # the requester's rebase
    assert (world["nw"] / ".git").exists()
    (world["nw"] / "feature.txt").write_text("resolved by hand\n")   # partly resolved
    out = CL.close_campaign(world["cfg"], st, _pcfg(world))
    assert out.next_state == "PARKED" and out.park["reason"] == "worktree_dirty"
    assert out.park["resume_to"] == "CLOSING" and "rebase in progress" in out.park["detail"]
    assert (world["nw"] / "feature.txt").read_text() == "resolved by hand\n"
    assert CL._rebase_in_progress(world["nw"])


def test_merge_request_parks_when_the_adopted_worktree_left_the_branch(world):
    st = _merge_world(world)
    G.git(world["nw"], "switch", "-q", "-c", "wip")                  # the requester moved on
    wip = G.rev_parse(world["nw"])
    _commit(world["root"], "other.txt", "x\n", "base moves")
    out = CL.close_campaign(world["cfg"], st, _pcfg(world))
    assert out.next_state == "PARKED" and out.park["reason"] == "worktree_dirty"
    assert out.park["resume_to"] == "CLOSING" and "not nw/c1" in out.park["detail"]
    assert G.rev_parse(world["nw"]) == wip                           # nothing was rebased
    G.git(world["nw"], "switch", "-q", "--detach")
    out = CL.close_campaign(world["cfg"], st, _pcfg(world))
    assert out.park["reason"] == "worktree_dirty" and "detached HEAD" in out.park["detail"]


# ---- SPEC-0234: a docs-only merge request skips the gates --------------------------------------

def _docs_only_world(world, files, **cfg_kw):
    st = _merge_world(world)
    G.git(world["nw"], "reset", "-q", "--hard", "main")
    for rel, text in files.items():
        (world["nw"] / rel).parent.mkdir(parents=True, exist_ok=True)
        (world["nw"] / rel).write_text(text)
    G.commit_all(world["nw"], "docs")
    st["merge"].update(head_at_request=G.rev_parse(world["nw"]), docs_only=True)
    pcfg = _pcfg(world, gates=("echo boom >&2; false",),
                 docs_only_paths=json.dumps(["docs/design/"]), **cfg_kw)
    return st, pcfg


def test_docs_only_merge_request_merges_with_no_gate(world):
    st, pcfg = _docs_only_world(world, {"docs/design/a.md": "x\n"})
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "docs/design/a.md").exists()
    assert _events(out, "gate") == [] and out.gate_run is None
    close = _events(out, "close")[0]
    assert close["gates_seq"] is None and close["docs_only"] is True


def test_docs_only_violation_parks_before_ff_merge(world, monkeypatch):
    st, pcfg = _docs_only_world(world, {"docs/design/a.md": "x\n", "src/x.py": "y\n"})
    merged = []
    monkeypatch.setattr(G, "ff_merge", lambda *a, **k: merged.append(1))
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "PARKED" and merged == []
    assert out.park["reason"] == "not_docs_only" and out.park["resume_to"] == "QUEUED"
    assert "1 paths outside merge.docs_only_paths: src/x.py" in out.park["detail"]
    assert "nw queue abandon SPEC-0001-20261025-abcd --yes" in out.park["detail"]
    assert "nw queue merge nw/c1 --yes without --docs-only" in out.park["detail"]
    assert _events(out, "gate") == []


def test_docs_only_violation_lists_at_most_ten_paths(world):
    files = {f"src/m{i:02d}.py": "y\n" for i in range(12)}
    st, pcfg = _docs_only_world(world, files)
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.park["reason"] == "not_docs_only"
    detail = out.park["detail"]
    assert "changes 12 paths outside" in detail and "(+2 more)" in detail
    assert "src/m09.py" in detail and "src/m10.py" not in detail


def test_docs_only_close_counts_a_rename_out_of_src_as_a_violation(world):
    _commit(world["root"], "src/x.py", "print('x')\n" * 20, "add x")
    st, pcfg = _docs_only_world(world, {})
    (world["nw"] / "docs/design").mkdir(parents=True)
    G.git(world["nw"], "mv", "src/x.py", "docs/design/x.py")
    G.commit_all(world["nw"], "move")
    st["merge"]["head_at_request"] = G.rev_parse(world["nw"])
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "not_docs_only"
    assert "src/x.py" in out.park["detail"] and "docs/design/x.py" not in out.park["detail"]


def test_docs_only_close_rereads_the_current_allowlist(world):
    st, _ = _docs_only_world(world, {"docs/design/a.md": "x\n"})
    pcfg = _pcfg(world, gates=("false",), docs_only_paths=json.dumps(["docs/other/"]))
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "not_docs_only"
    assert "docs/design/a.md" in out.park["detail"]


# ---- SPEC-0283: gh pr view ---------------------------------------------------------------

def _fake_run(monkeypatch, stdout, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")
    monkeypatch.setattr(G, "_run", run)
    monkeypatch.setattr(G.shutil, "which", lambda n: "/bin/gh")


def test_gh_pr_view_parses_json(monkeypatch):
    out = json.dumps({"state": "MERGED", "mergedAt": "2026-10-27T10:00:00Z",
                      "mergeCommit": {"oid": "abc123"}})
    _fake_run(monkeypatch, out)
    assert G.gh_pr_view("https://github.com/x/y/pull/7") == {
        "state": "MERGED", "merged_at": "2026-10-27T10:00:00Z", "merge_sha": "abc123"}
    _fake_run(monkeypatch, json.dumps({"state": "OPEN", "mergedAt": None, "mergeCommit": None}))
    assert G.gh_pr_view("u") == {"state": "OPEN", "merged_at": None, "merge_sha": None}
    _fake_run(monkeypatch, "not json")
    with pytest.raises(G.GitError):
        G.gh_pr_view("u")


def test_gh_pr_view_missing_gh(monkeypatch):
    monkeypatch.setattr(G.shutil, "which", lambda n: None)
    with pytest.raises(G.GitError, match="gh not found"):
        G.gh_pr_view("u")


def test_gh_pr_view_uses_short_timeout(monkeypatch):
    seen = {}
    _fake_run(monkeypatch, json.dumps({"state": "OPEN"}), seen)
    G.gh_pr_view("https://github.com/x/y/pull/7")
    assert seen["timeout"] == 30 and seen["timeout"] != G._NETWORK_TIMEOUT
    assert seen["cmd"][1:4] == ["pr", "view", "https://github.com/x/y/pull/7"]


# ---- SPEC-0290: a split campaign closes without the spec guard or the spec DoD ---------------

def _split_world(world, *, original_status="superseded"):
    """The campaign becomes a split of SPEC-0001 into SPEC-0002 and SPEC-0003, both written and
    committed on the branch, the original superseded by part 1."""
    wt = world["nw"]
    G.git(wt, "reset", "-q", "--hard", "main")
    sup = "status: superseded\nsuperseded_by: SPEC-0002" if original_status == "superseded" \
        else f"status: {original_status}"
    _commit(wt, "docs/specs/0001-t.md", SPEC_DONE.replace("status: done", sup), "supersede")
    parts = []
    for n, (num, dep) in enumerate([(2, None), (3, "SPEC-0002")], 1):
        extra = "supersedes: [SPEC-0001]" + (f"\ndepends_on: [{dep}]" if dep else "")
        text = SPEC_DONE.replace("SPEC-0001", f"SPEC-{num:04d}").replace(
            "status: done", f"status: accepted\n{extra}").replace("- [x]", "- [ ]")
        rel = f"docs/specs/{num:04d}-p{n}.md"
        _commit(wt, rel, text, f"part {n}")
        parts.append({"title": f"P{n}", "scope": "s", "number": num, "rel_path": rel,
                      "spec_id": f"SPEC-{num:04d}"})
    st = world["state"]
    st.update(kind="split", split={"original": "SPEC-0001",
                                   "original_rel": "docs/specs/0001-t.md", "review_sha": "x",
                                   "parts": parts})
    st["spec"] = {"rel_path": "docs/specs/0002-p1.md", "internal": True}   # rewritten: not trusted
    return st


def test_split_check_passes_for_a_finalized_split(world):
    st = _split_world(world)
    assert CL.split_check(world["nw"], st) == []


def test_split_check_missing_part_fails(world):
    st = _split_world(world)
    (world["nw"] / "docs/specs/0003-p2.md").unlink()
    assert CL.split_check(world["nw"], st) == ["part 2 (P2): spec file missing"]
    _commit(world["nw"], "x.txt", "x\n", "commit the deletion")
    out = CL.prepare_close(world["cfg"], st, _pcfg(world)).outcome
    assert out.next_state == "PARKED"
    assert out.park["reason"] == "spec_write_failed" and out.park["resume_to"] == "SPEC_WRITING"
    assert "part 2 (P2): spec file missing" in out.park["detail"]
    assert [e for t, e in out.events if t == "finding"][0]["gate"] == "split-check"


def test_split_check_original_not_superseded_fails(world):
    st = _split_world(world, original_status="accepted")
    (problem,) = CL.split_check(world["nw"], st)
    assert "original SPEC-0001 is 'accepted'" in problem and "superseded by SPEC-0002" in problem


def test_split_check_broken_chain_fails(world):
    st = _split_world(world)
    path = world["nw"] / "docs/specs/0003-p2.md"
    path.write_text(path.read_text().replace("depends_on: [SPEC-0002]", "depends_on: [SPEC-0009]"))
    (problem,) = CL.split_check(world["nw"], st)
    assert problem.startswith("part 2 SPEC-0003: depends_on is")


def test_split_skips_spec_guard(world):
    st = _split_world(world)
    # the original is superseded (not `done`) and differs from spec_at_open.md: for an ordinary
    # campaign the spec DoD and the drift guard would send it back to TRIAGING
    pcfg = _pcfg(world)
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "docs/specs/0003-p2.md").exists()
    assert not [t for t, _ in out.events if t == "spec_drift"]
    st2 = dict(st, kind="implement")
    assert CL._is_spec_less(st) and not CL._is_spec_less(st2)


def test_renamed_spec_is_what_close_checks(world):
    wt = world["nw"]
    src = "docs/specs/0001-t.md"
    dest = "docs/specs/0002-t.md"
    st = world["state"]
    assert CL.follow_renamed_spec(wt, src) is None          # the file is still there
    _git(wt, "mv", src, dest)
    _git(wt, "commit", "-q", "-m", "renumber the spec")
    assert CL.spec_guard(world["cfg"], st)[0] is False      # the old path is gone
    assert CL.adopt_renamed_spec(st, wt) == dest
    assert st["spec"]["rel_path"] == dest and st["spec_id"] == "SPEC-0001"
    ok, gate, _ev = CL.spec_guard(world["cfg"], st)
    assert ok and gate is None


# ---- SPEC-0316: the branch's gate timeouts reach the close-time re-gate -------------------

def _slow_yaml(timeout_s):
    return ("schema: nw.project.v1\nbase: main\ngates:\n"
            f"  - {{name: slow, cmd: 'sleep 2', timeout_s: {timeout_s}, required: true}}\n"
            "merge:\n  policy: merge\n")


def _gate_limit_findings(out):
    return [p for p in _events(out, "finding") if p["gate"] == "gate-limits"]


def test_branch_raised_gate_timeout_passes_the_regate_after_a_rebase(world):
    (world["root"] / ".nw" / "project.yaml").write_text(_slow_yaml(1))
    _git(world["root"], "commit", "-q", "-am", "slow gate, tight limit")
    pcfg = P.load_project_config(world["root"] / ".nw" / "project.yaml")
    _git(world["nw"], "rebase", "-q", "main")                 # branch now carries main's yaml
    (world["nw"] / ".nw" / "project.yaml").write_text(_slow_yaml(30))
    _git(world["nw"], "commit", "-q", "-am", "raise the gate timeout")
    _commit(world["root"], "other.txt", "o\n", "base moves")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED", out
    (finding,) = _gate_limit_findings(out)
    assert finding["severity"] == "info" and "slow: 1s -> 30s" in finding["detail"]


def test_unraised_gate_timeout_still_times_out_with_no_finding(world):
    (world["root"] / ".nw" / "project.yaml").write_text(_slow_yaml(1))
    _git(world["root"], "commit", "-q", "-am", "slow gate, tight limit")
    pcfg = P.load_project_config(world["root"] / ".nw" / "project.yaml")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "TRIAGING" and out.gate_run.results[0].timed_out
    assert _gate_limit_findings(out) == []
