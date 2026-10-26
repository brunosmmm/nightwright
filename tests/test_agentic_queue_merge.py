"""SPEC-0233: `wt queue merge` — a branch merge runs as a `kind: merge` campaign (QUEUED →
OPENING → CLOSING → MERGED), no persona, through the normal close. Offline: temp git repos,
sh gates, the fake runner (never used: a merge campaign spawns nothing)."""
import json
import os
from pathlib import Path

import pytest

from test_agentic_e2e import (ENV, _git, _write, added, impl_done, make_cfg, make_repo, review,
                              run_cli, script, write_review)
from wt.agentic import close as CL
from wt.agentic import followups as FU
from wt.agentic import queue as Q
from wt.agentic import schemas as SC
from wt.agentic import store as S


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("WT_AGENTIC_RUNNER", raising=False)
    monkeypatch.delenv("WT_AGENTIC_FAKE_SCRIPT", raising=False)


def branch_wt(tmp_path, repo, branch="fix/typo", *, files=None, adopt=True):
    """A branch one commit ahead of main (feature.txt by default). With `adopt` it is checked
    out in its own worktree, which is returned; otherwise only the branch exists."""
    wt = tmp_path / "wts" / branch.replace("/", "-")
    _git(repo, "worktree", "add", "-q", "-b", branch, str(wt), "main")
    for rel, text in (files or {"feature.txt": "hello\n"}).items():
        _write(wt, rel, text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", f"work on {branch}")
    if not adopt:
        _git(repo, "worktree", "remove", "--force", str(wt))
        return None
    return wt


@pytest.fixture
def world(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    return repo, cfg, tmp_path


def plan(cfg, branch="fix/typo", **kw):
    return Q.plan_merge(cfg, branch, project="Meta-Tools", **kw)


# ---- plan_merge ---------------------------------------------------------------------------------

def test_plan_adopts_the_branch_worktree(world):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo)
    p = plan(cfg, note="typo", requested_by="steward:Meta-Tools")
    assert p["kind"] == "merge" and p["own_worktree"] is False
    assert Path(p["worktree"]) == wt.resolve() and p["ahead"] == 1
    assert p["priority"] == Q.MERGE_PRIORITY_DEFAULT == 2 and p["base"] == "main"
    assert p["head_at_request"] == _git(repo, "rev-parse", "fix/typo")
    assert p["campaign"].startswith("MERGE-") and p["merge_policy"] == "merge"


def test_plan_creates_a_worktree_when_the_branch_has_none(world):
    repo, cfg, tmp = world
    branch_wt(tmp, repo, adopt=False)
    p = plan(cfg)
    assert p["own_worktree"] is True
    assert Path(p["worktree"]).name == p["campaign"]
    assert not Path(p["worktree"]).exists()


def test_plan_refuses_other_merge_policies(tmp_path):
    repo = make_repo(tmp_path, policy="pr")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    branch_wt(tmp_path, repo)
    with pytest.raises(ValueError, match="merge.policy"):
        plan(cfg)


def test_plan_refuses_missing_branch_and_base(world):
    repo, cfg, tmp = world
    with pytest.raises(ValueError, match="does not exist"):
        plan(cfg, "nope")
    with pytest.raises(ValueError, match="base branch"):
        plan(cfg, "main")


def test_plan_refuses_a_branch_checked_out_in_the_main_checkout(world):
    repo, cfg, tmp = world
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "x.txt", "x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    with pytest.raises(ValueError, match="main checkout"):
        plan(cfg, "side")


def test_plan_refuses_a_branch_not_ahead(world):
    repo, cfg, tmp = world
    _git(repo, "branch", "same")
    with pytest.raises(ValueError, match="nothing to merge"):
        plan(cfg, "same")


def test_plan_refuses_a_second_campaign_for_the_branch(world):
    repo, cfg, tmp = world
    branch_wt(tmp, repo)
    Q.apply_merge(cfg, plan(cfg))
    with pytest.raises(ValueError, match="already has an active campaign"):
        plan(cfg)


def test_plan_refuses_a_spec_campaigns_own_branch(world, monkeypatch):
    repo, cfg, tmp = world
    spec = repo / "docs/specs/0001-trivial.md"
    write_review(cfg, spec, "SPEC-0001")
    cid = added(cfg, monkeypatch)
    _git(repo, "branch", f"wt/{cid}", "main")        # what the open step would create
    _git(repo, "worktree", "add", "-q", str(tmp / "other"), f"wt/{cid}")
    _write(tmp / "other", "y.txt", "y\n")
    _git(tmp / "other", "add", "-A")
    _git(tmp / "other", "commit", "-q", "-m", "y")
    with pytest.raises(ValueError, match="already has an active campaign"):
        plan(cfg, f"wt/{cid}")


def test_plan_worktree_must_match(world):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo)
    assert Path(plan(cfg, worktree=str(wt))["worktree"]) == wt.resolve()
    with pytest.raises(ValueError, match="--worktree"):
        plan(cfg, worktree=str(tmp / "elsewhere"))


def test_plan_refuses_a_dirty_adopted_worktree(world):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo)
    _write(wt, "feature.txt", "edited\n")
    with pytest.raises(ValueError, match="uncommitted or untracked"):
        plan(cfg)
    _git(wt, "checkout", "--", "feature.txt")
    _write(wt, "scratch.txt", "untracked\n")
    with pytest.raises(ValueError, match="uncommitted or untracked"):
        plan(cfg)
    (wt / "scratch.txt").unlink()
    _write(wt, ".setup-ran", "")                      # ignored files do not count
    plan(cfg)


# ---- apply_merge --------------------------------------------------------------------------------

def test_apply_merge_state_shape_and_open_event(world):
    repo, cfg, tmp = world
    branch_wt(tmp, repo)
    st = Q.apply_merge(cfg, plan(cfg, note="typo", requested_by="conductor"), by="conductor")
    got = S.read_state(cfg, st["id"])
    assert got["kind"] == "merge" and got["spec_id"] is None and got["spec"] is None
    assert got["state"] == "QUEUED" and got["priority"] == 2 and got["force"] is False
    assert got["merge"] == {"branch": "fix/typo", "requested_by": "conductor", "note": "typo",
                            "head_at_request": _git(repo, "rev-parse", "fix/typo"),
                            "own_worktree": False, "docs_only": False}
    assert got["repo"]["branch"] == "fix/typo" and got["repo"]["base"] == "main"
    ev = S.read_ledger(cfg, st["id"], types=["open"])[0]
    assert ev["kind"] == "merge" and ev["merge_branch"] == "fix/typo" and ev["spec_id"] is None
    assert SC.ledger_problems(ev) == []
    assert Q.campaign_label(got) == "merge:fix/typo"


# ---- loop helpers -------------------------------------------------------------------------------

def test_backlog_readiness_both_ways(world):
    repo, cfg, tmp = world
    branch_wt(tmp, repo)
    st = Q.apply_merge(cfg, plan(cfg))
    assert Q.backlog_readiness(cfg, st) == (True, [])
    gone = {**st, "repo": {**st["repo"], "branch": "gone"}}
    assert Q.backlog_readiness(cfg, gone) == (False, ["branch gone missing"])


def test_plan_resume_only_closing_or_queued(world):
    repo, cfg, tmp = world
    branch_wt(tmp, repo)
    st = Q.apply_merge(cfg, plan(cfg))
    st["state"], st["park"] = "PARKED", {"reason": "human", "resume_to": "CLOSING"}
    S.write_state(cfg, st["id"], st)
    with pytest.raises(ValueError, match="CLOSING or QUEUED"):
        Q.plan_resume(cfg, st["id"], to="IMPLEMENTING")
    assert Q.plan_resume(cfg, st["id"], to="CLOSING")["to"] == "CLOSING"
    assert Q.plan_resume(cfg, st["id"], to="QUEUED")["to"] == "QUEUED"


@pytest.mark.parametrize("state", ["IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING"])
def test_plan_step_parks_a_merge_campaign_in_a_persona_state(world, state):
    repo, cfg, tmp = world
    branch_wt(tmp, repo)
    st = {**Q.apply_merge(cfg, plan(cfg)), "state": state}
    step = Q.plan_step(st, {})
    assert (step.kind, step.reason, step.resume_to) == ("park", "human", "CLOSING")
    assert Q.plan_step({**st, "state": "OPENING"}, {}).kind == "open"
    assert Q.plan_step({**st, "state": "CLOSING"}, {}).kind == "close"


# ---- abandon ------------------------------------------------------------------------------------

@pytest.mark.parametrize("keep", [False, True])
def test_abandon_never_removes_an_adopted_worktree(world, keep):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo)
    st = Q.apply_merge(cfg, plan(cfg))
    _write(wt, "feature.txt", "uncommitted\n")
    _write(wt, "scratch.txt", "untracked\n")
    st["state"], st["park"] = "PARKED", {"reason": "worktree_dirty", "resume_to": "CLOSING"}
    S.write_state(cfg, st["id"], st)
    p = Q.plan_abandon(cfg, st["id"], keep_worktree=keep)
    assert p["remove_worktree"] is False and p["worktree_present"] is True
    Q.apply_abandon(cfg, p)
    assert S.read_state(cfg, st["id"])["state"] == "ABANDONED"
    assert (wt / "feature.txt").read_text() == "uncommitted\n"
    assert (wt / "scratch.txt").exists()
    assert _git(repo, "rev-parse", "--verify", "fix/typo")


def test_abandon_removes_a_worktree_wt_created(world):
    repo, cfg, tmp = world
    branch_wt(tmp, repo, adopt=False)
    st = Q.apply_merge(cfg, plan(cfg))
    wt = Path(st["repo"]["worktree"])
    _git(repo, "worktree", "add", "-q", str(wt), "fix/typo")     # as the open step would
    st["state"], st["park"] = "PARKED", {"reason": "human", "resume_to": "CLOSING"}
    S.write_state(cfg, st["id"], st)
    p = Q.plan_abandon(cfg, st["id"])
    assert p["remove_worktree"] is True
    Q.apply_abandon(cfg, p)
    assert not wt.exists()


# ---- e2e ----------------------------------------------------------------------------------------

def queue_merge(cfg, monkeypatch, branch="fix/typo", *extra):
    r = run_cli(cfg, monkeypatch, ["queue", "merge", branch, "--project", "Meta-Tools", "--yes",
                                   "--json", *extra])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.merge.v1" and d["written"] is True
    return d["campaign"]


def run_until_idle(cfg, monkeypatch):
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output


def test_cli_dry_run_writes_nothing(world, monkeypatch):
    repo, cfg, tmp = world
    branch_wt(tmp, repo)
    r = run_cli(cfg, monkeypatch, ["queue", "merge", "fix/typo", "--project", "Meta-Tools"])
    assert r.exit_code == 0, r.output
    assert S.list_campaigns(cfg) == []
    r = run_cli(cfg, monkeypatch, ["queue", "merge", "nope", "--project", "Meta-Tools"])
    assert r.exit_code != 0 and "does not exist" in r.output


def test_open_parks_a_worktree_that_turned_dirty_while_queued(world, monkeypatch):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo)
    cid = queue_merge(cfg, monkeypatch)
    _write(wt, "scratch.txt", "x\n")
    run_until_idle(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed", st
    assert st["park"]["resume_to"] == "QUEUED"


def test_e2e_merge_adopted_worktree(world, monkeypatch):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo)
    cid = queue_merge(cfg, monkeypatch, "fix/typo", "--note", "typo", "--by", "conductor")
    st = S.read_state(cfg, cid)
    assert st["state"] == "QUEUED" and st["priority"] == 2
    _write(wt, "more.txt", "after queueing\n")                  # a commit after the request
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "one more fix")
    spy = []
    monkeypatch.setattr(FU, "file_followups", lambda *a, **k: spy.append(1))
    run_until_idle(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert spy == []
    assert (repo / "feature.txt").exists() and (repo / "more.txt").exists()
    assert st["close"]["merged_sha"] == _git(repo, "rev-parse", "main")
    assert wt.is_dir() and _git(repo, "rev-parse", "--verify", "fix/typo")   # kept
    ledger = S.read_ledger(cfg, cid)
    types = [e["type"] for e in ledger]
    assert "run.start" not in types and "run.end" not in types
    assert types.count("gate") == 1
    assert [(e["from"], e["to"]) for e in ledger if e["type"] == "transition"] == [
        ("QUEUED", "OPENING"), ("OPENING", "CLOSING"), ("CLOSING", "MERGED")]
    close = next(e for e in ledger if e["type"] == "close")
    assert close["head_at_request"] == st["merge"]["head_at_request"]
    assert close["commits_since_request"] == 1
    assert not (wt / ".setup-ran").exists()                    # no setup in an adopted worktree


def test_e2e_merge_created_worktree_is_removed(world, monkeypatch):
    repo, cfg, tmp = world
    branch_wt(tmp, repo, adopt=False)
    cid = queue_merge(cfg, monkeypatch)
    wt = Path(S.read_state(cfg, cid)["repo"]["worktree"])
    run_until_idle(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert not wt.exists()
    assert _git(repo, "rev-parse", "--verify", "fix/typo")     # the branch is never deleted
    assert (repo / "feature.txt").exists()


@pytest.mark.parametrize("dirt", ["tracked", "untracked"])
def test_e2e_dirty_worktree_parks_without_a_commit(world, monkeypatch, dirt):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo)
    cid = queue_merge(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "tick", "--json"])      # admit + open only
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["state"] == "CLOSING"
    if dirt == "tracked":
        _write(wt, "feature.txt", "edited\n")
    else:
        _write(wt, "scratch.txt", "x\n")
    before = _git(wt, "log", "--format=%H")
    run_until_idle(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "worktree_dirty", st
    assert st["park"]["resume_to"] == "CLOSING"
    assert ("feature.txt" if dirt == "tracked" else "scratch.txt") in st["park"]["detail"]
    assert _git(wt, "log", "--format=%H") == before
    assert not (repo / "feature.txt").exists()


def test_e2e_red_gate_parks_rebased_then_resumes_to_merged(world, monkeypatch):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo, files={"other.txt": "no feature.txt yet\n"})   # gate: test -f feature.txt
    _write(repo, "base.txt", "moved\n")                         # main moved: the rebase is real
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main moves")
    cid = queue_merge(cfg, monkeypatch)
    run_until_idle(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "gates_failed", st
    assert st["park"]["resume_to"] == "CLOSING"
    assert "gates failed: test" in st["park"]["detail"]
    assert f"branch rebased onto main {_git(repo, 'rev-parse', 'main')[:10]}" in st["park"]["detail"]
    assert st["last_gate"]["ok"] is False
    assert _git(wt, "merge-base", "--is-ancestor", "main", "HEAD") == ""   # branch sits on main
    _write(wt, "feature.txt", "fixed\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "fix")
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--yes"])
    assert r.exit_code == 0, r.output
    run_until_idle(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert "run.start" not in [e["type"] for e in S.read_ledger(cfg, cid)]


def test_e2e_rebase_conflict_leaves_the_branch_untouched(world, monkeypatch):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo, files={"feature.txt": "branch\n"})
    _write(repo, "feature.txt", "main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "conflicting")
    tip = _git(wt, "rev-parse", "HEAD")
    cid = queue_merge(cfg, monkeypatch)
    run_until_idle(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "rebase_conflict", st
    assert _git(wt, "rev-parse", "HEAD") == tip
    assert not (Path(_git(wt, "rev-parse", "--absolute-git-dir")) / "rebase-merge").exists()
    assert _git(wt, "status", "--porcelain") == ""


def test_e2e_merge_and_spec_campaign_close_one_at_a_time(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, test_gate="true")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    branch_wt(tmp_path, repo, files={"merged.txt": "m\n"})
    spec_cid = added(cfg, monkeypatch)
    merge_cid = queue_merge(cfg, monkeypatch)
    run_until_idle(cfg, monkeypatch)
    a, b = S.read_state(cfg, spec_cid), S.read_state(cfg, merge_cid)
    assert a["state"] == "MERGED" and b["state"] == "MERGED", (a["state"], b["state"])
    shas = sorted([a["close"]["merged_sha"], b["close"]["merged_sha"]],
                  key=lambda s: int(_git(repo, "rev-list", "--count", s)))
    assert _git(repo, "merge-base", "--is-ancestor", shas[0], shas[1]) == ""   # linear history
    assert shas[1] == _git(repo, "rev-parse", "main")
    assert b["merge"]["branch"] == "fix/typo"
    assert (repo / "merged.txt").exists()


# ---- visibility ---------------------------------------------------------------------------------

def test_visibility_label_everywhere(world, monkeypatch):
    repo, cfg, tmp = world
    branch_wt(tmp, repo)
    monkeypatch.setattr("wt.console.console._width", 200)         # no wrapped cells
    cid = queue_merge(cfg, monkeypatch, "fix/typo", "--note", "typo")
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    item = json.loads(r.output)["items"][0]
    assert item["kind"] == "merge" and item["merge_branch"] == "fix/typo"
    assert item["label"] == "merge:fix/typo" and item["spec_id"] is None
    for args in (["queue", "list"], ["queue", "backlog"], ["campaign", "list"],
                 ["campaign", "show", cid]):
        r = run_cli(cfg, monkeypatch, args)
        assert r.exit_code == 0, r.output
        assert "spec_id=None" not in r.output and "spec None" not in r.output, (args, r.output)
        if args[0] != "campaign" or args[1] != "show":      # the open event prints base_sha=None
            assert "None" not in r.output, (args, r.output)
        assert "merge:fix/typo" in r.output or "merge fix/typo" in r.output, (args, r.output)
    r = run_cli(cfg, monkeypatch, ["campaign", "show", cid])
    assert "merge fix/typo (requested by human; head at request" in r.output
    assert "worktree adopted)" in r.output
    dash = Q.dashboard_payload(cfg)
    assert dash["backlog"][0]["merge_branch"] == "fix/typo"
    assert dash["backlog"][0]["spec_title"] == "typo"
    from rich.console import Console
    from wt.agentic import cli_queue as CQ
    con = Console(record=True, width=160)
    con.print(CQ._dash_backlog(dash["backlog"]))
    assert "merge:fix/typo" in con.export_text() and "None" not in con.export_text()
