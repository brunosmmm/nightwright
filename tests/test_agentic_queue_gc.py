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


def _campaign(cfg, cid, state, root, branch, worktree=None, *, kind="spec", own=True, base="main",
              merged_sha=None, project="demo"):
    st = {"id": cid, "kind": kind, "state": state, "project": project, "spec_id": None,
          "repo": {"root": str(root), "base": base, "branch": branch,
                   "worktree": str(worktree) if worktree else None}}
    if kind == "merge":
        st["merge"] = {"branch": branch, "own_worktree": own}
    if state == "MERGED":
        st["close"] = {"mode": "merge",
                       "merged_sha": merged_sha or _git(root, "rev-parse", f"refs/heads/{branch}")}
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
    """The first branch row fails (so rows after it prove apply_gc keeps going)."""
    root = world["root"]
    plan = Q.plan_gc(world["cfg"])
    deletes = [r["branch"] for r in plan["rows"] if r["kind"] == "branch" and r["action"] == "delete"]
    assert len(deletes) == 3
    bad, rest = deletes[0], deletes[1:]
    real = G.branch_delete

    def flaky(repo, branch, force=False):
        assert force is False
        if branch == bad:
            raise GitError(["git", "branch", "-d", branch], 1, "boom")
        return real(repo, branch, force=force)

    monkeypatch.setattr(G, "branch_delete", flaky)
    rows = _rows(Q.apply_gc(world["cfg"], plan))
    assert rows[("branch", bad)]["error"] == "boom" and rows[("branch", bad)]["done"] is False
    assert G.branch_exists(root, bad)
    for b in rest:
        assert rows[("branch", b)]["done"] is True and not G.branch_exists(root, b), b
    assert rows[("worktree", "m1")]["done"] is True and rows[("worktree", "wt-specs-1")]["done"] is True


def test_worktree_row_error_does_not_stop_others(world, monkeypatch):
    """The first worktree row fails; the other worktree and the branch rows still apply."""
    root = world["root"]
    plan = Q.plan_gc(world["cfg"])
    removes = [Path(r["path"]).name for r in plan["rows"]
               if r["kind"] == "worktree" and r["action"] == "remove"]
    assert sorted(removes) == ["m1", "wt-specs-1"]
    bad, other = removes
    real = G.worktree_remove

    def flaky(repo, path, force=False, prune=True):
        assert force is False
        if Path(path).name == bad:
            raise GitError(["git", "worktree", "remove", str(path)], 1, "boom")
        return real(repo, path, force=force, prune=prune)

    monkeypatch.setattr(G, "worktree_remove", flaky)
    rows = _rows(Q.apply_gc(world["cfg"], plan))
    assert rows[("worktree", bad)]["error"] == "boom" and rows[("worktree", bad)]["done"] is False
    assert rows[("worktree", other)]["done"] is True
    listed = {e["path"].name for e in G.worktree_list(root)}
    assert bad in listed and other not in listed
    # the failed worktree still holds its branch, so git refuses that one row; the rest go
    held = {"m1": "wt/m1", "wt-specs-1": "specs-1"}[bad]
    assert rows[("branch", held)]["done"] is False and G.branch_exists(root, held)
    for b in {"wt/m1", "specs-1", "wt/a1"} - {held}:
        assert rows[("branch", b)]["done"] is True and not G.branch_exists(root, b), b


def test_prune_failure_is_a_repo_error(world, monkeypatch):
    """A missing-on-disk row is unregistered by its own `git worktree remove`, so a failed
    repo-wide prune is a repo error only: no row is reported failed for it."""
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    _merged_wt(root, "wt/gone", wts / "gone")
    shutil.rmtree(wts / "gone")
    _campaign(cfg, "g1", "MERGED", root, "wt/gone", wts / "gone")
    real = G.git

    def no_prune(repo, *args, **kw):
        if args[:2] == ("worktree", "prune"):
            raise GitError(["git", *args], 1, "locked index")
        return real(repo, *args, **kw)

    plan = Q.plan_gc(cfg)
    monkeypatch.setattr(G, "git", no_prune)
    rows = _rows(Q.apply_gc(cfg, plan))
    assert rows[("worktree", "gone")]["done"] is True and "error" not in rows[("worktree", "gone")]
    assert (wts / "gone").resolve() not in {e["path"] for e in G.worktree_list(root)}
    assert any("worktree prune: locked index" in e for e in plan["errors"])
    assert rows[("worktree", "m1")]["done"] is True
    assert not any(r.get("done") is False for r in plan["rows"]), plan["rows"]


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


def test_requester_synced_past_the_merge_kept(world):
    """A MERGED adopted request whose requester fast-forwarded the same branch to a newer main
    (clean, on the request branch, HEAD an ancestor of base) keeps its worktree and branch; so
    does a request branch moved past the merge with no worktree left."""
    root, cfg, tmp = world["root"], world["cfg"], world["tmp"]
    _merged_wt(root, "specs-6", tmp / "req" / "wt-specs-6")
    _campaign(cfg, "r6", "MERGED", root, "specs-6", tmp / "req" / "wt-specs-6", kind="merge", own=False)
    _merged_wt(root, "specs-7", tmp / "req" / "wt-specs-7")
    _campaign(cfg, "r7", "MERGED", root, "specs-7", tmp / "req" / "wt-specs-7", kind="merge", own=False)
    G.worktree_remove(root, tmp / "req" / "wt-specs-7")
    _commit(root, "newer.txt", "x\n", "main moves on")
    _git(tmp / "req" / "wt-specs-6", "merge", "-q", "--ff-only", "main")
    _git(root, "branch", "-f", "specs-7", "main")
    assert G.is_clean(tmp / "req" / "wt-specs-6", untracked=True)
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    assert (rows[("worktree", "wt-specs-6")]["action"], rows[("worktree", "wt-specs-6")]["why"]) == (
        "keep", "requester moved on (HEAD past the merge)")
    assert rows[("branch", "specs-6")]["action"] == "keep"
    assert (rows[("branch", "specs-7")]["action"], rows[("branch", "specs-7")]["why"]) == (
        "keep", "requester moved on (tip past the merge)")
    assert rows[("worktree", "wt-specs-1")]["action"] == "remove"
    Q.apply_gc(cfg, plan)
    assert (tmp / "req" / "wt-specs-6" / "newer.txt").exists()
    assert G.branch_exists(root, "specs-6") and G.branch_exists(root, "specs-7")


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
        "remove", "missing on disk (unregister only)")
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


def test_handed_off_worktree_removed_branch_kept(world, monkeypatch):
    """A HANDED_OFF campaign (PR hand-off, cleanup `on_merge`) is terminal: its clean worktree
    goes, its unmerged branch stays."""
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    G.worktree_add(root, wts / "h1", "wt/h1", "main")
    _commit(wts / "h1", "h1.txt", "x\n", "pr work")
    _campaign(cfg, "h1", "HANDED_OFF", root, "wt/h1", wts / "h1")
    rows = _rows(Q.plan_gc(cfg))
    assert (rows[("worktree", "h1")]["action"], rows[("worktree", "h1")]["why"]) == (
        "remove", "HANDED_OFF campaign, clean")
    assert (rows[("branch", "wt/h1")]["action"], rows[("branch", "wt/h1")]["why"]) == ("keep", "unmerged")
    r = run_cli(cfg, monkeypatch, ["queue", "gc", "--yes"])
    assert r.exit_code == 0, r.output
    assert not (wts / "h1").exists() and G.branch_exists(root, "wt/h1")


def test_own_worktree_merge_request_follows_campaign_path(world):
    """A merge request whose worktree wt created (own_worktree) is removed when clean like any
    campaign worktree: no requester guard (ABANDONED) and no merged_sha guard (MERGED)."""
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    older = _git(root, "rev-parse", "main~1")
    _merged_wt(root, "own-1", wts / "own1")
    _campaign(cfg, "o1", "MERGED", root, "own-1", wts / "own1", kind="merge", own=True, merged_sha=older)
    _merged_wt(root, "own-2", wts / "own2")
    _campaign(cfg, "o2", "ABANDONED", root, "own-2", wts / "own2", kind="merge", own=True)
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    for name, state in (("own1", "MERGED"), ("own2", "ABANDONED")):
        assert (rows[("worktree", name)]["action"], rows[("worktree", name)]["why"]) == (
            "remove", f"{state} campaign, clean"), name
    Q.apply_gc(cfg, plan)
    assert not (wts / "own1").exists() and not (wts / "own2").exists()
    assert rows[("worktree", "own1")]["done"] is True and rows[("worktree", "own2")]["done"] is True


def test_prunable_dir_still_on_disk_kept(world, monkeypatch):
    """Git marks a worktree whose `.git` file was deleted by hand prunable even though the
    directory is still there: it is kept (its files cannot be checked) and survives --yes."""
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    _merged_wt(root, "wt/nogit", wts / "nogit")
    (wts / "nogit" / "notes.txt").write_text("keep me\n")
    (wts / "nogit" / ".git").unlink()
    _campaign(cfg, "n1", "MERGED", root, "wt/nogit", wts / "nogit")
    row = _rows(Q.plan_gc(cfg))[("worktree", "nogit")]
    assert (row["action"], row["why"]) == ("keep", "no .git (git worktree prune unregisters it; directory stays)")
    r = run_cli(cfg, monkeypatch, ["queue", "gc", "--yes"])
    assert r.exit_code == 0, r.output
    assert (wts / "nogit" / "notes.txt").read_text() == "keep me\n"


def _assert_repo_error_exit(cfg, monkeypatch, needle):
    r = run_cli(cfg, monkeypatch, ["queue", "gc"])
    assert r.exit_code == 0 and needle in r.output, r.output
    r = run_cli(cfg, monkeypatch, ["queue", "gc", "--json"])
    assert r.exit_code == 0 and any(needle in e for e in json.loads(r.output)["errors"]), r.output
    r = run_cli(cfg, monkeypatch, ["queue", "gc", "--yes"])
    assert r.exit_code == 1 and needle in r.output, r.output
    r = run_cli(cfg, monkeypatch, ["queue", "gc", "--yes", "--json"])
    d = json.loads(r.output)
    assert r.exit_code == 1 and d["written"] is True and any(needle in e for e in d["errors"]), r.output
    assert not any(row.get("done") is False for row in d["rows"]), d["rows"]


def test_missing_repo_root_is_a_plan_error(world, monkeypatch):
    cfg, tmp = world["cfg"], world["tmp"]
    _campaign(cfg, "x1", "ABANDONED", tmp / "gone-repo", "wt/x1", None)
    plan = Q.plan_gc(cfg)
    assert plan["errors"] == [f"{(tmp / 'gone-repo').resolve()}: repo root missing"]
    assert str(world["root"].resolve()) in plan["repos"]
    _assert_repo_error_exit(cfg, monkeypatch, "repo root missing")


def test_unreadable_worktree_root_is_a_plan_error(world, monkeypatch):
    wts = world["wts"].resolve()
    real = Path.iterdir

    def iterdir(self):
        if self == wts:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    plan = Q.plan_gc(world["cfg"])
    assert any(e.startswith(f"{wts}: ") and "Permission denied" in e for e in plan["errors"]), plan["errors"]
    assert not any(r["kind"] == "orphan" for r in plan["rows"])
    assert _rows(plan)[("worktree", "m1")]["action"] == "remove"   # the repo's other rows still plan
    _assert_repo_error_exit(world["cfg"], monkeypatch, "Permission denied")


def test_git_error_listing_a_repo_is_a_plan_error(world, monkeypatch):
    def boom(repo):
        raise GitError(["git", "worktree", "list", "--porcelain"], 128, "not a git repository")

    monkeypatch.setattr(G, "worktree_list", boom)
    plan = Q.plan_gc(world["cfg"])
    assert plan["errors"] == [f"{world['root'].resolve()}: not a git repository"]
    assert plan["repos"] == [] and plan["rows"] == []
    _assert_repo_error_exit(world["cfg"], monkeypatch, "not a git repository")


def test_cwd_in_a_subdirectory_of_a_terminal_worktree_kept(world, monkeypatch):
    root, wts = world["root"], world["wts"]
    (wts / "m1" / "sub" / "deeper").mkdir(parents=True)       # empty dirs: still clean
    monkeypatch.chdir(wts / "m1" / "sub" / "deeper")
    plan = Q.plan_gc(world["cfg"])
    rows = _rows(plan)
    assert (rows[("worktree", "m1")]["action"], rows[("worktree", "m1")]["why"]) == ("keep", "current directory")
    assert rows[("branch", "wt/m1")]["action"] == "keep"
    Q.apply_gc(world["cfg"], plan)
    assert (wts / "m1" / "sub" / "deeper").is_dir() and G.branch_exists(root, "wt/m1")


def test_project_filter_does_not_prune_other_entries(world, monkeypatch):
    """`--project demo --yes` unregisters demo's hand-deleted worktree only: another project's
    and an unowned prunable entry in the same repo stay registered (no repo-wide prune)."""
    root, cfg, wts, tmp = world["root"], world["cfg"], world["wts"], world["tmp"]
    for name, path in (("wt/gone", wts / "gone"), ("wt/b-gone", tmp / "other" / "gone"),
                       ("loose", tmp / "loose")):
        _merged_wt(root, name, path)
        shutil.rmtree(path)
    _campaign(cfg, "g1", "MERGED", root, "wt/gone", wts / "gone")
    _campaign(cfg, "b1", "MERGED", root, "wt/b-gone", tmp / "other" / "gone", project="other")
    plan = Q.plan_gc(cfg, project="demo")
    rows = _rows(plan)
    assert not any(r["project"] == "other" for r in plan["rows"])
    assert rows[("worktree", "loose")]["why"] == "no campaign"         # no prune note under a filter
    r = run_cli(cfg, monkeypatch, ["queue", "gc", "--project", "demo", "--yes"])
    assert r.exit_code == 0, r.output
    listed = {e["path"] for e in G.worktree_list(root)}
    assert (wts / "gone").resolve() not in listed and not G.branch_exists(root, "wt/gone")
    assert (tmp / "other" / "gone").resolve() in listed and (tmp / "loose").resolve() in listed
    assert G.branch_exists(root, "wt/b-gone")
    # unfiltered, the repo-wide prune drops them (and frees the other project's branch)
    assert "prunable: git worktree prune drops it" in _rows(Q.plan_gc(cfg))[("worktree", "loose")]["why"]
    r = run_cli(cfg, monkeypatch, ["queue", "gc", "--yes"])
    assert r.exit_code == 0, r.output
    listed = {e["path"] for e in G.worktree_list(root)}
    assert (tmp / "other" / "gone").resolve() not in listed and (tmp / "loose").resolve() not in listed
    assert not G.branch_exists(root, "wt/b-gone")


def test_project_base_decides_when_campaign_records_none(world, monkeypatch):
    """No `repo.base`: the base comes from `.wt/project.yaml` (here `trunk`, not main)."""
    root, cfg = world["root"], world["cfg"]
    (root / ".wt").mkdir()
    (root / ".wt" / "project.yaml").write_text("schema: wt.project.v1\nbase: trunk\n")
    _git(root, "branch", "trunk", "main~2")
    _git(root, "branch", "wt/on-trunk", "trunk")
    _git(root, "branch", "wt/on-main", "main")
    _campaign(cfg, "p1", "ABANDONED", root, "wt/on-trunk", None, base=None)
    _campaign(cfg, "p2", "ABANDONED", root, "wt/on-main", None, base=None)
    rows = _rows(Q.plan_gc(cfg))
    assert (rows[("branch", "wt/on-trunk")]["action"], rows[("branch", "wt/on-trunk")]["why"]) == (
        "delete", "merged into trunk")
    assert (rows[("branch", "wt/on-main")]["action"], rows[("branch", "wt/on-main")]["why"]) == (
        "keep", "unmerged")
    (root / ".wt" / "project.yaml").write_text("schema: wt.project.v1\nmerge: {base: trunk}\nbase: main\n")
    assert _rows(Q.plan_gc(cfg))[("branch", "wt/on-trunk")]["why"] == "merged into trunk"


@pytest.mark.parametrize("content", [b"schema: [unclosed\n", b"\xff\xfe base: main\n",
                                     b"schema: wt.project.v1\nbase: 3\n"])
def test_bad_project_config_is_a_repo_error(world, monkeypatch, content):
    root = world["root"]
    (root / ".wt").mkdir()
    (root / ".wt" / "project.yaml").write_bytes(content)
    plan = Q.plan_gc(world["cfg"])
    assert plan["rows"] == [] and plan["repos"] == []
    assert len(plan["errors"]) == 1 and plan["errors"][0].startswith(f"{root.resolve()}: "), plan["errors"]
    _assert_repo_error_exit(world["cfg"], monkeypatch, "project.yaml")


def test_unreadable_project_config_is_a_repo_error(world, monkeypatch):
    path = (world["root"] / ".wt" / "project.yaml")
    path.parent.mkdir()
    path.write_text("schema: wt.project.v1\n")
    real = Path.read_text

    def read_text(self, *a, **kw):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    plan = Q.plan_gc(world["cfg"])
    assert plan["rows"] == [] and any("unreadable" in e and "Permission denied" in e for e in plan["errors"])
    _assert_repo_error_exit(world["cfg"], monkeypatch, "Permission denied")


def test_branch_and_path_shared_with_a_live_campaign_untouched(world):
    """A re-queued spec: a terminal campaign and a later live one share the branch and the
    worktree path, so neither produces a row."""
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    _merged_wt(root, "wt/re", wts / "re")
    _campaign(cfg, "re-old", "MERGED", root, "wt/re", wts / "re")
    _campaign(cfg, "re-new", "IMPLEMENTING", root, "wt/re", wts / "re")
    _git(root, "branch", "wt/re2")
    _campaign(cfg, "re2-old", "ABANDONED", root, "wt/re2", None)
    _campaign(cfg, "re2-new", "QUEUED", root, "wt/re2", None)
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    for key in (("worktree", "re"), ("branch", "wt/re"), ("branch", "wt/re2")):
        assert key not in rows, key
    Q.apply_gc(cfg, plan)
    assert (wts / "re").is_dir() and G.branch_exists(root, "wt/re") and G.branch_exists(root, "wt/re2")


def test_orphan_dir_referenced_by_terminal_or_live_campaign(world):
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    (wts / "ghost").mkdir()
    (wts / "ghost" / "left.txt").write_text("x\n")
    _campaign(cfg, "gh1", "ABANDONED", root, "wt/gh1", wts / "ghost")
    (wts / "alive").mkdir()
    _campaign(cfg, "al1", "IMPLEMENTING", root, "wt/al1", wts / "alive")
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    row = rows[("orphan", "ghost")]
    assert (row["action"], row["campaign"], row["campaign_state"]) == ("keep", "gh1", "ABANDONED")
    assert row["why"] == "orphan dir of ABANDONED campaign: git does not list it; delete by hand"
    assert rows[("orphan", "stale")]["campaign"] is None
    assert rows[("orphan", "stale")]["why"] == "orphan dir: git does not list it; delete by hand"
    assert ("orphan", "alive") not in rows
    Q.apply_gc(cfg, plan)
    assert (wts / "ghost" / "left.txt").exists() and (wts / "alive").is_dir()


def test_second_yes_run_is_a_no_op(world, monkeypatch):
    r = run_cli(world["cfg"], monkeypatch, ["queue", "gc", "--yes"])
    assert r.exit_code == 0, r.output
    before = _snapshot(world["root"])
    r = run_cli(world["cfg"], monkeypatch, ["queue", "gc", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert (d["worktrees_removable"], d["branches_removable"], d["errors"]) == (0, 0, [])
    assert not any(row["action"] != "keep" for row in d["rows"])
    assert _snapshot(world["root"]) == before


def test_hidden_edits_kept(world):
    """skip-worktree / assume-unchanged files edited on disk are invisible to `git status`:
    such a worktree is kept dirty. A flagged but unmodified file does not block removal."""
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    for name, flag in (("sw", "--skip-worktree"), ("au", "--assume-unchanged"), ("ok", "--skip-worktree")):
        _merged_wt(root, f"wt/{name}", wts / name)
        _git(wts / name, "update-index", flag, "README.md")
        if name != "ok":
            (wts / name / "README.md").write_text("local tweak\n")
            assert G.is_clean(wts / name, untracked=True)
        _campaign(cfg, name, "MERGED", root, f"wt/{name}", wts / name)
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    for name in ("sw", "au"):
        assert (rows[("worktree", name)]["action"], rows[("worktree", name)]["why"]) == (
            "keep", "dirty (skip-worktree/assume-unchanged edits)"), name
        assert rows[("branch", f"wt/{name}")]["action"] == "keep"
    assert rows[("worktree", "ok")]["action"] == "remove"
    Q.apply_gc(cfg, plan)
    for name in ("sw", "au"):
        assert (wts / name / "README.md").read_text() == "local tweak\n"
    assert not (wts / "ok").exists()


def test_bisect_in_progress_kept(world):
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    _merged_wt(root, "wt/bs", wts / "bs")
    _campaign(cfg, "bs", "MERGED", root, "wt/bs", wts / "bs")
    _git(wts / "bs", "bisect", "start")
    assert G.is_clean(wts / "bs", untracked=True)
    plan = Q.plan_gc(cfg)
    rows = _rows(plan)
    assert (rows[("worktree", "bs")]["action"], rows[("worktree", "bs")]["why"]) == ("keep", "bisect in progress")
    Q.apply_gc(cfg, plan)
    assert G.bisect_in_progress(wts / "bs")


def test_gc_removes_terminal_tmp_residue(world):
    """SPEC-0287: a terminal campaign's leftover `tmp/` gets one `tmp` row that `--yes`
    removes; a live campaign's `tmp/` gets none."""
    cfg, root = world["cfg"], world["root"]
    _campaign(cfg, "h1", "HANDED_OFF", root, "wt/h1")
    for cid in ("h1", "l1"):
        (S.child_tmp(cfg, cid, "run-001-x", create=True) / "f").write_text("x")
    plan = Q.plan_gc(cfg)
    tmp_rows = [r for r in plan["rows"] if r["kind"] == "tmp"]
    assert [(r["campaign"], r["action"]) for r in tmp_rows] == [("h1", "remove")]
    assert tmp_rows[0]["why"] == "HANDED_OFF campaign temp residue"
    assert tmp_rows[0]["path"] == str(S.campaign_tmp(cfg, "h1"))
    assert plan["tmp_removable"] == 1
    assert S.campaign_tmp(cfg, "h1").is_dir()                    # dry run wrote nothing
    Q.apply_gc(cfg, plan)
    assert tmp_rows[0]["done"] is True and "error" not in tmp_rows[0]
    assert not S.campaign_tmp(cfg, "h1").exists()
    assert (S.campaign_tmp(cfg, "l1") / "run-001-x" / "f").is_file()
    assert not [r for r in Q.plan_gc(cfg)["rows"] if r["kind"] == "tmp"]


def test_gc_tmp_residue_honours_project(world):
    """SPEC-0287: `--project` limits the `tmp` rows too: another project's terminal residue is
    neither listed nor removed."""
    cfg, root = world["cfg"], world["root"]
    _campaign(cfg, "h1", "HANDED_OFF", root, "wt/h1")
    _campaign(cfg, "h2", "HANDED_OFF", root, "wt/h2", project="other")
    for cid in ("h1", "h2"):
        (S.child_tmp(cfg, cid, "run-001-x", create=True) / "f").write_text("x")
    plan = Q.plan_gc(cfg, project="other")
    tmp_rows = [r for r in plan["rows"] if r["kind"] == "tmp"]
    assert [r["campaign"] for r in tmp_rows] == ["h2"] and plan["tmp_removable"] == 1
    Q.apply_gc(cfg, plan)
    assert not S.campaign_tmp(cfg, "h2").exists()
    assert (S.campaign_tmp(cfg, "h1") / "run-001-x" / "f").is_file()


def test_gc_skips_symlinked_terminal_tmp(world, tmp_path):
    """SPEC-0287: a terminal campaign whose `tmp/` is a symlink gets no `tmp` row, and the
    link's target survives `apply_gc`."""
    cfg, root = world["cfg"], world["root"]
    _campaign(cfg, "h1", "HANDED_OFF", root, "wt/h1")
    target = tmp_path / "elsewhere"
    (target / "run-001-x").mkdir(parents=True)
    (target / "run-001-x" / "f").write_text("x")
    S.campaign_tmp(cfg, "h1").parent.mkdir(parents=True, exist_ok=True)
    S.campaign_tmp(cfg, "h1").symlink_to(target, target_is_directory=True)
    plan = Q.plan_gc(cfg)
    assert not [r for r in plan["rows"] if r["kind"] == "tmp"] and plan["tmp_removable"] == 0
    Q.apply_gc(cfg, plan)
    assert S.campaign_tmp(cfg, "h1").is_symlink()
    assert (target / "run-001-x" / "f").is_file()


def test_gc_tmp_removal_failure_lands_on_row(world, monkeypatch):
    """SPEC-0287: a `tmp` dir still present after `clear_campaign_tmp` (or a removal that
    raises) marks its row `done: false` with an error; the worktree and branch rows still apply."""
    root, cfg, wts = world["root"], world["cfg"], world["wts"]
    _campaign(cfg, "h1", "HANDED_OFF", root, "wt/h1")
    _campaign(cfg, "h2", "ABANDONED", root, "wt/h2")
    for cid in ("h1", "h2"):
        (S.child_tmp(cfg, cid, "run-001-x", create=True) / "f").write_text("x")
    _merged_wt(root, "wt/ok", wts / "ok")
    _campaign(cfg, "ok", "MERGED", root, "wt/ok", wts / "ok")

    def clear(cfg, cid):
        if cid == "h2":
            raise OSError("permission denied")       # h1: a silent no-op

    monkeypatch.setattr(S, "clear_campaign_tmp", clear)
    plan = Q.plan_gc(cfg)
    Q.apply_gc(cfg, plan)
    rows = _rows(plan)
    tmp = {r["campaign"]: r for r in plan["rows"] if r["kind"] == "tmp"}
    assert (tmp["h1"]["done"], tmp["h1"]["error"]) == (False, "still present after removal")
    assert (tmp["h2"]["done"], tmp["h2"]["error"]) == (False, "permission denied")
    assert S.campaign_tmp(cfg, "h1").is_dir() and S.campaign_tmp(cfg, "h2").is_dir()
    assert rows[("worktree", "ok")]["done"] is True and not (wts / "ok").exists()
    assert rows[("branch", "wt/ok")]["done"] is True
