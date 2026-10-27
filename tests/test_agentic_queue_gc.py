"""SPEC-0275: `wt queue gc` — worktrees and merged branches of finished campaigns. Real git repos
in tmp_path; campaigns are written with hand-set states (no queue run)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_agentic_e2e import ENV, make_cfg, run_cli
from wt.agentic import gitops as G
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.gitops import GitError


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, name, text, msg):
    (Path(repo) / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _campaign(cfg, cid, state, root, branch, worktree=None, *, kind="spec", own=True, base="main"):
    st = {"id": cid, "kind": kind, "state": state, "project": "demo", "spec_id": None,
          "repo": {"root": str(root), "base": base, "branch": branch,
                   "worktree": str(worktree) if worktree else None}}
    if kind == "merge":
        st["merge"] = {"branch": branch, "own_worktree": own}
    S.create_campaign(cfg, st)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A main checkout plus one of every gc candidate kind:
    m1 MERGED (worktree left by a failed cleanup, merged branch) · r1 MERGED merge request
    (adopted clean requester worktree, merged branch) · a1 ABANDONED (merged branch, no
    worktree) · d1 MERGED with a dirty worktree · u1 ABANDONED with an unmerged branch ·
    l1 IMPLEMENTING · a human worktree · an orphan dir under the worktree root."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "README.md", "hello\n", "init")
    wts = tmp_path / ".wt-worktrees" / "demo"
    cfg = make_cfg(tmp_path, root, tmp_path / "script.json")
    paths = {}

    def merged(name, path):
        G.worktree_add(root, path, name, "main")
        _commit(path, f"{name.replace('/', '-')}.txt", "x\n", name)
        _git(root, "merge", "-q", "--ff-only", name)
        paths[name] = path

    merged("wt/m1", wts / "m1")
    _campaign(cfg, "m1", "MERGED", root, "wt/m1", wts / "m1")
    merged("specs-1", tmp_path / "req" / "wt-specs-1")
    _campaign(cfg, "r1", "MERGED", root, "specs-1", tmp_path / "req" / "wt-specs-1",
              kind="merge", own=False)
    _git(root, "branch", "wt/a1")
    _campaign(cfg, "a1", "ABANDONED", root, "wt/a1", wts / "a1")
    merged("wt/d1", wts / "d1")
    (wts / "d1" / "scratch.txt").write_text("untracked\n")
    _campaign(cfg, "d1", "MERGED", root, "wt/d1", wts / "d1")
    G.worktree_add(root, wts / "u1", "wt/u1", "main")
    _commit(wts / "u1", "u1.txt", "x\n", "unmerged work")
    G.worktree_remove(root, wts / "u1", force=True)
    _campaign(cfg, "u1", "ABANDONED", root, "wt/u1", wts / "u1")
    G.worktree_add(root, wts / "l1", "wt/l1", "main")
    _campaign(cfg, "l1", "IMPLEMENTING", root, "wt/l1", wts / "l1")
    G.worktree_add(root, tmp_path / "human", "human-scratch", "main")
    (wts / "stale").mkdir()
    return {"root": root, "cfg": cfg, "wts": wts, "tmp": tmp_path}


def _snapshot(root):
    return _git(root, "worktree", "list", "--porcelain"), _git(root, "branch", "--list")


def _rows(plan):
    return {(r["kind"], r.get("branch") or Path(r["path"]).name): r for r in plan["rows"]}


def test_dry_run_changes_nothing(world, monkeypatch):
    before = _snapshot(world["root"])
    r = run_cli(world["cfg"], monkeypatch, ["queue", "gc"])
    assert r.exit_code == 0, r.output
    assert "2 worktrees, 3 branches removable (dry-run; --yes to apply)" in r.output
    assert _snapshot(world["root"]) == before
    rows = _rows(Q.plan_gc(world["cfg"]))
    assert rows[("worktree", "m1")]["action"] == "remove"
    assert rows[("worktree", "wt-specs-1")]["action"] == "remove"
    assert rows[("orphan", "stale")]["action"] == "keep"
    assert rows[("worktree", "human")]["why"] == "no campaign"
    assert _snapshot(world["root"]) == before


def test_yes_removes_merged_and_abandoned_leftovers(world, monkeypatch):
    root = world["root"]
    r = run_cli(world["cfg"], monkeypatch, ["queue", "gc", "--yes"])
    assert r.exit_code == 0, r.output
    for b in ("wt/m1", "specs-1", "wt/a1"):
        assert not G.branch_exists(root, b), b
    listed = {e["path"] for e in G.worktree_list(root)}
    assert (world["wts"] / "m1").resolve() not in listed and not (world["wts"] / "m1").exists()
    req = (world["tmp"] / "req" / "wt-specs-1")
    assert req.resolve() not in listed and not req.exists()
    assert (world["tmp"] / "human").exists() and (world["wts"] / "stale").is_dir()


def test_dirty_and_unmerged_kept(world, monkeypatch):
    root = world["root"]
    rows = _rows(Q.plan_gc(world["cfg"]))
    assert (rows[("worktree", "d1")]["action"], rows[("worktree", "d1")]["why"]) == ("keep", "dirty")
    assert rows[("branch", "wt/d1")]["action"] == "keep"
    assert (rows[("branch", "wt/u1")]["action"], rows[("branch", "wt/u1")]["why"]) == ("keep", "unmerged")
    plan = Q.apply_gc(world["cfg"], Q.plan_gc(world["cfg"]))
    assert not any(r.get("error") for r in plan["rows"]), plan["rows"]
    assert (world["wts"] / "d1" / "scratch.txt").exists()
    assert G.branch_exists(root, "wt/d1") and G.branch_exists(root, "wt/u1")


def test_live_campaign_and_main_never_touched(world, monkeypatch):
    root = world["root"]
    monkeypatch.chdir(world["wts"] / "m1")
    plan = Q.plan_gc(world["cfg"])
    rows = _rows(plan)
    acting = [r for r in plan["rows"] if r["action"] != "keep"]
    targets = {r.get("branch") or str(Path(r["path"]).resolve()) for r in acting}
    assert "wt/l1" not in targets and str((world["wts"] / "l1").resolve()) not in targets
    assert str(root.resolve()) not in targets and "main" not in targets
    assert not any(r["campaign"] == "l1" for r in plan["rows"])
    assert rows[("worktree", "m1")]["why"] == "current directory"
    assert rows[("branch", "wt/m1")]["action"] == "keep"
    Q.apply_gc(world["cfg"], plan)
    assert (world["wts"] / "l1").exists() and G.branch_exists(root, "wt/l1")
    assert (world["wts"] / "m1").exists() and G.branch_exists(root, "wt/m1")
    assert G.current_branch(root) == "main"


def test_row_error_does_not_stop_others(world, monkeypatch):
    root = world["root"]
    real = G.branch_delete

    def flaky(repo, branch, force=False):
        assert force is False
        if branch == "wt/a1":
            raise GitError(["git", "branch", "-d", branch], 1, "boom")
        return real(repo, branch, force=force)

    monkeypatch.setattr(G, "branch_delete", flaky)
    plan = Q.apply_gc(world["cfg"], Q.plan_gc(world["cfg"]))
    rows = _rows(plan)
    assert rows[("branch", "wt/a1")]["error"] == "boom" and rows[("branch", "wt/a1")]["done"] is False
    assert G.branch_exists(root, "wt/a1")
    assert not G.branch_exists(root, "wt/m1") and not G.branch_exists(root, "specs-1")
    assert rows[("branch", "wt/m1")]["done"] is True


def test_json_schema(world, monkeypatch):
    r = run_cli(world["cfg"], monkeypatch, ["queue", "gc", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.gc.v1" and d["written"] is False
    assert isinstance(d["rows"], list) and d["rows"]
    assert set(d["rows"][0]) >= {"kind", "campaign", "campaign_state", "action", "why"}
    r = run_cli(world["cfg"], monkeypatch, ["queue", "gc", "--json", "--yes", "--project", "demo"])
    d = json.loads(r.output)
    assert d["written"] is True and d["branches_removable"] == 3
    r = run_cli(world["cfg"], monkeypatch, ["queue", "gc", "--json", "--project", "other"])
    assert json.loads(r.output)["rows"] == []


def _merged_wt(root, name, path):
    G.worktree_add(root, path, name, "main")
    _commit(path, f"{name.replace('/', '-')}.txt", "x\n", name)
    _git(root, "merge", "-q", "--ff-only", name)


def test_main_checkout_of_terminal_merge_request_kept(world):
    root, cfg = world["root"], world["cfg"]
    _git(root, "branch", "feat0")
    _campaign(cfg, "r0", "MERGED", root, "feat0", root, kind="merge", own=False)
    plan = Q.plan_gc(cfg)
    row = _rows(plan)[("worktree", "root")]
    assert (row["action"], row["why"], row["campaign"]) == ("keep", "main checkout", "r0")
    Q.apply_gc(cfg, plan)
    assert (root / "README.md").exists() and G.current_branch(root) == "main"


def test_adopted_requester_worktrees_kept(world):
    """MERGED request whose worktree gained a commit; ABANDONED request; MERGED request whose
    requester checked out a new branch in the same directory."""
    root, cfg, tmp = world["root"], world["cfg"], world["tmp"]
    _merged_wt(root, "specs-2", tmp / "req" / "wt-specs-2")
    _commit(tmp / "req" / "wt-specs-2", "later.txt", "x\n", "after the merge")
    _campaign(cfg, "r2", "MERGED", root, "specs-2", tmp / "req" / "wt-specs-2", kind="merge", own=False)
    _merged_wt(root, "specs-3", tmp / "req" / "wt-specs-3")
    _campaign(cfg, "r3", "ABANDONED", root, "specs-3", tmp / "req" / "wt-specs-3", kind="merge", own=False)
    _merged_wt(root, "specs-4", tmp / "req" / "wt-specs-4")
    _git(tmp / "req" / "wt-specs-4", "checkout", "-q", "-b", "specs-5", "main")
    _campaign(cfg, "r4", "MERGED", root, "specs-4", tmp / "req" / "wt-specs-4", kind="merge", own=False)
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    assert (rows[("worktree", "wt-specs-2")]["action"], rows[("worktree", "wt-specs-2")]["why"]) == ("keep", "unmerged")
    assert rows[("branch", "specs-2")]["action"] == "keep"
    assert rows[("worktree", "wt-specs-3")]["why"] == "requester's worktree (ABANDONED merge request)"
    assert rows[("worktree", "wt-specs-4")]["why"] == "requester moved on (on specs-5)"
    assert all(rows[("worktree", f"wt-specs-{i}")]["action"] == "keep" for i in (2, 3, 4))
    Q.apply_gc(cfg, plan)
    for i in (2, 3, 4):
        assert (tmp / "req" / f"wt-specs-{i}").is_dir(), i
    assert G.branch_exists(root, "specs-2") and G.branch_exists(root, "specs-5")
    assert G.current_branch(tmp / "req" / "wt-specs-4") == "specs-5"


def test_rebase_in_progress_kept_with_its_branch(world, monkeypatch):
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    _merged_wt(root, "wt/rb", wts / "rb")
    _campaign(cfg, "rb", "MERGED", root, "wt/rb", wts / "rb")
    monkeypatch.setenv("GIT_SEQUENCE_EDITOR", "sed -i -e s/^pick/edit/")
    _git(wts / "rb", "rebase", "-q", "-i", "HEAD~1")
    assert G.rebase_in_progress(wts / "rb") and G.is_clean(wts / "rb", untracked=True)
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    assert (rows[("worktree", "rb")]["action"], rows[("worktree", "rb")]["why"]) == ("keep", "rebase/merge in progress")
    assert rows[("branch", "wt/rb")]["action"] == "keep"
    Q.apply_gc(cfg, plan)
    assert G.rebase_in_progress(wts / "rb") and G.branch_exists(root, "wt/rb")


def test_prunable_locked_and_detached(world):
    """A worktree deleted by hand is pruned; a locked one and a detached one holding commits no
    branch reaches are kept; a live campaign's base branch is never deleted."""
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    _merged_wt(root, "wt/gone", wts / "gone")
    shutil.rmtree(wts / "gone")
    _campaign(cfg, "g1", "MERGED", root, "wt/gone", wts / "gone")
    _merged_wt(root, "wt/lk", wts / "lk")
    _git(root, "worktree", "lock", str(wts / "lk"))
    _campaign(cfg, "k1", "MERGED", root, "wt/lk", wts / "lk")
    _merged_wt(root, "wt/dt", wts / "dt")
    _git(wts / "dt", "checkout", "-q", "--detach")
    _commit(wts / "dt", "dt2.txt", "x\n", "detached work")
    _campaign(cfg, "t1", "MERGED", root, "wt/dt", wts / "dt")
    _git(root, "branch", "release")
    _campaign(cfg, "b1", "ABANDONED", root, "release", None)
    _campaign(cfg, "b2", "IMPLEMENTING", root, "wt/b2", None, base="release")
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    assert (rows[("worktree", "gone")]["action"], rows[("worktree", "gone")]["why"]) == (
        "remove", "missing on disk (git worktree prune)")
    assert (rows[("worktree", "lk")]["action"], rows[("worktree", "lk")]["why"]) == ("keep", "locked")
    assert (rows[("worktree", "dt")]["action"], rows[("worktree", "dt")]["why"]) == (
        "keep", "detached, unreachable commits")
    assert (rows[("branch", "release")]["action"], rows[("branch", "release")]["why"]) == ("keep", "base branch")
    Q.apply_gc(cfg, plan)
    listed = {e["path"] for e in G.worktree_list(root)}
    assert (wts / "gone").resolve() not in listed and not G.branch_exists(root, "wt/gone")
    assert (wts / "lk").is_dir() and (wts / "dt" / "dt2.txt").exists()
    assert G.branch_exists(root, "release")


def test_yes_exits_nonzero_on_row_failure(world, monkeypatch):
    def boom(repo, branch, force=False):
        raise GitError(["git", "branch", "-d", branch], 1, "boom")

    monkeypatch.setattr(G, "branch_delete", boom)
    r = run_cli(world["cfg"], monkeypatch, ["queue", "gc", "--yes"])
    assert r.exit_code == 1 and "failed" in r.output
