"""Spec-number allocator (SPEC-0208): worktree/branch-aware reservations under a flock."""
import fcntl
import json
import multiprocessing as mp
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from test_promote import _cfg as _promote_cfg, _find, _seed_idea
from nightwright import org_write as W
from nightwright import spec_numbers as SN
from nightwright import specs as S
from nightwright.agentic import store
from nightwright.cli import cli

pytestmark = pytest.mark.fast


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": os.environ["PATH"], "HOME": str(cwd)})


def _touch(repo, num, rel="docs/specs"):
    d = repo / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{num:04d}-x.md").write_text("x\n")


def _make_repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _touch(path, 1)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "one")
    return path


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "data").mkdir()
    return {"data_dir": str(tmp_path / "data")}


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path / "repo")


def _sd(repo):
    return repo / "docs" / "specs"


def _scratch(tmp_path, repo):
    """0001 on main; 0002 only on b1; 0003 uncommitted in main; 0004 uncommitted in w2."""
    _git(repo, "checkout", "-q", "-b", "b1")
    _touch(repo, 2)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    _git(repo, "checkout", "-q", "main")
    _touch(repo, 3)
    w2 = tmp_path / "w2"
    _git(repo, "worktree", "add", "-q", "--detach", str(w2), "main")
    _touch(w2, 4)
    return w2


def test_allocate_sees_branches_and_worktrees(tmp_path, cfg, repo):
    w2 = _scratch(tmp_path, repo)
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 5
    again = SN.allocate(cfg, by="t", specs_dir=_sd(w2))
    assert again["number"] == 6 and again["id"] == "SPEC-0006"


def test_each_source_is_load_bearing_alone(tmp_path, cfg):
    """Each scan source, as the unique maximum, moves the answer (0001 is on main)."""
    # branch only: 0003 committed on b1
    r1 = _make_repo(tmp_path / "r1")
    _git(r1, "checkout", "-q", "-b", "b1")
    _touch(r1, 3)
    _git(r1, "add", "-A")
    _git(r1, "commit", "-q", "-m", "three")
    _git(r1, "checkout", "-q", "main")
    assert SN.allocate(cfg, by="t", specs_dir=_sd(r1))["number"] == 4
    # main checkout working tree only: uncommitted 0003 (a linked worktree exists but is stale)
    r2 = _make_repo(tmp_path / "r2")
    _git(r2, "worktree", "add", "-q", "--detach", str(tmp_path / "r2w"), "main")
    _touch(r2, 3)
    assert SN.allocate(cfg, by="t", specs_dir=_sd(r2))["number"] == 4
    # linked worktree working tree only: uncommitted 0006 in the worktree
    r3 = _make_repo(tmp_path / "r3")
    w3 = tmp_path / "r3w"
    _git(r3, "worktree", "add", "-q", "--detach", str(w3), "main")
    _touch(w3, 6)
    assert SN.allocate(cfg, by="t", specs_dir=_sd(r3))["number"] == 7
    # a live reservation alone: nothing on disk beyond 0001
    r4 = _make_repo(tmp_path / "r4")
    assert SN.allocate(cfg, by="t", specs_dir=_sd(r4))["number"] == 2
    assert SN.allocate(cfg, by="t", specs_dir=_sd(r4))["number"] == 3


def test_prunable_worktree_is_skipped(tmp_path, cfg, repo):
    w = tmp_path / "gone"
    _git(repo, "worktree", "add", "-q", "--detach", str(w), "main")
    _touch(w, 9)
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 10
    shutil.rmtree(w)                         # now prunable: path missing, record still listed
    listing = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                             capture_output=True, text=True).stdout
    assert "prunable" in listing
    SN.release(cfg, 10, specs_dir=_sd(repo))
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 2


def _worker(args):
    cfg, sd = args
    return SN.allocate(cfg, by="w", specs_dir=sd)["number"]


def test_concurrent_allocate_distinct(cfg, repo):
    ctx = mp.get_context("fork")
    with ctx.Pool(8) as pool:
        nums = pool.map(_worker, [(cfg, _sd(repo))] * 8)
    assert len(set(nums)) == 8 and sorted(nums) == list(range(2, 10))


def test_same_campaign_is_idempotent(cfg, repo):
    a = SN.allocate(cfg, by="t", campaign="C-1", specs_dir=_sd(repo))
    b = SN.allocate(cfg, by="t", campaign="C-1", specs_dir=_sd(repo))
    c = SN.allocate(cfg, by="t", campaign="C-2", specs_dir=_sd(repo))
    assert a["number"] == b["number"] and c["number"] == a["number"] + 1


def test_prune_on_commit_abandon_and_release(cfg, repo):
    sd = _sd(repo)
    a = SN.allocate(cfg, by="t", specs_dir=sd)           # 2
    _touch(repo, a["number"])                            # uncommitted: not pruned
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == [2]
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    assert SN.list_reservations(cfg, specs_dir=sd) == []
    assert SN.allocate(cfg, by="t", specs_dir=sd)["number"] == 3

    b = SN.allocate(cfg, by="t", campaign="C-AB", specs_dir=sd)
    store.campaign_dir(cfg, "C-AB").mkdir(parents=True)
    store.write_state(cfg, "C-AB", {"id": "C-AB", "state": "RUNNING"})
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == [3, b["number"]]
    store.write_state(cfg, "C-AB", {"id": "C-AB", "state": "ABANDONED"})
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == [3]

    assert SN.release(cfg, "SPEC-0003", specs_dir=sd)["number"] == 3
    with pytest.raises(ValueError):
        SN.release(cfg, 3, specs_dir=sd)
    with pytest.raises(ValueError):
        SN.release(cfg, 99, specs_dir=sd)


def test_abandoned_split_campaign_frees_its_per_part_reservations(cfg, repo):
    sd = _sd(repo)
    a = SN.allocate(cfg, by="t", campaign="C-SP#1", specs_dir=sd)      # SPEC-0290: `<campaign>#k`
    b = SN.allocate(cfg, by="t", campaign="C-SP#2", specs_dir=sd)
    assert a["number"] != b["number"]
    store.campaign_dir(cfg, "C-SP").mkdir(parents=True)
    store.write_state(cfg, "C-SP", {"id": "C-SP", "state": "SPEC_WRITING"})
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == [
        a["number"], b["number"]]
    store.write_state(cfg, "C-SP", {"id": "C-SP", "state": "ABANDONED"})
    assert SN.list_reservations(cfg, specs_dir=sd) == []
    assert SN.allocate(cfg, by="t", specs_dir=sd)["number"] == a["number"]


def test_release_is_per_key_and_rejects_bad_numbers(tmp_path, cfg, repo):
    other = _make_repo(tmp_path / "repo2")
    assert SN.allocate(cfg, by="t", specs_dir=_sd(other))["number"] == 2
    with pytest.raises(ValueError, match="not reserved"):
        SN.release(cfg, 2, specs_dir=_sd(repo))          # reserved only under the other key
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=_sd(other))] == [2]
    for bad in ("abc", "SPEC-", "", "12x"):
        with pytest.raises(ValueError, match="not a spec number"):
            SN.release(cfg, bad, specs_dir=_sd(repo))
    assert SN.release(cfg, "spec-0002", specs_dir=_sd(other))["number"] == 2


def test_prune_on_commit_on_unchecked_out_branch(tmp_path, cfg, repo):
    sd = _sd(repo)
    a = SN.allocate(cfg, by="t", specs_dir=sd)           # 2
    w = tmp_path / "wb"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(w), "main")
    _touch(w, a["number"])
    _git(w, "add", "-A")
    _git(w, "commit", "-q", "-m", "two")
    _git(w, "checkout", "-q", "--detach")                # 'feat' is now checked out nowhere
    shutil.rmtree(w)
    _git(repo, "worktree", "prune")
    assert [r["number"] for r in SN._load(cfg)] == [2]   # still stored until a call prunes it
    b = SN.allocate(cfg, by="t", specs_dir=sd)           # allocate itself prunes
    assert b["number"] == 3
    assert [r["number"] for r in SN._load(cfg)] == [3]


def test_commit_prunes_only_the_callers_key(tmp_path, cfg, repo):
    other = _make_repo(tmp_path / "repo2")
    sd, osd = _sd(repo), _sd(other)
    n = SN.allocate(cfg, by="t", specs_dir=osd)["number"]            # 2 under repo2
    _git(repo, "checkout", "-q", "-b", "b1")
    _touch(repo, n)                                                  # same number committed in repo
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    assert SN.list_reservations(cfg, specs_dir=sd) == []
    assert [r["number"] for r in SN._load(cfg)] == [n]               # repo2's stays in the store
    SN.allocate(cfg, by="t", specs_dir=sd)
    SN.list_reservations(cfg, specs_dir=sd, all_keys=True)
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=osd)] == [n]
    assert SN.allocate(cfg, by="t", specs_dir=osd)["number"] == n + 1


def test_all_keys_prunes_abandoned_campaigns_of_other_keys(tmp_path, cfg, repo):
    other = _make_repo(tmp_path / "repo2")
    SN.allocate(cfg, by="t", campaign="C-X", specs_dir=_sd(other))
    store.campaign_dir(cfg, "C-X").mkdir(parents=True)
    store.write_state(cfg, "C-X", {"id": "C-X", "state": "ABANDONED"})
    assert SN.list_reservations(cfg, specs_dir=_sd(repo), all_keys=True) == []


def test_corrupt_store_raises_missing_and_empty_work(cfg, repo):
    store_path = SN._paths(cfg)[0]
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 2     # missing
    store_path.write_text("")
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 2     # empty
    store_path.write_text("{nope")
    with pytest.raises(ValueError, match="spec-reservations.json"):
        SN.allocate(cfg, by="t", specs_dir=_sd(repo))
    assert store_path.read_text() == "{nope"                                 # never reset
    other = json.dumps({"schema": "other", "reservations": []})
    store_path.write_text(other)
    with pytest.raises(ValueError, match="wrong schema"):
        SN.allocate(cfg, by="t", specs_dir=_sd(repo))
    assert store_path.read_text() == other


def _hammer(args):
    cfg, sd, i = args
    got = []
    for _ in range(5):
        got.append(SN.allocate(cfg, by=f"p{i}", specs_dir=sd)["number"])
        SN.list_reservations(cfg, specs_dir=sd)
    return got


def _releaser(args):
    cfg, sd, number = args
    return SN.release(cfg, number, specs_dir=sd)["number"]


def test_concurrent_release_and_list_lose_nothing(cfg, repo):
    sd = _sd(repo)
    victim = SN.allocate(cfg, by="seed", specs_dir=sd)["number"]            # 2
    ctx = mp.get_context("fork")
    with ctx.Pool(5) as pool:
        rel = pool.apply_async(_releaser, ((cfg, sd, victim),))
        res = pool.map(_hammer, [(cfg, sd, i) for i in range(4)])
        assert rel.get() == victim
    got = sorted(n for r in res for n in r)
    assert len(set(got)) == 20
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == got


def test_non_git_directory_uses_listing(tmp_path, cfg):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "TEMPLATE.md").write_text("t")
    (d / "SCHEMA.md").write_text("s")
    (d / "0007-a.md").write_text("a")
    assert SN.allocate(cfg, by="t", specs_dir=d)["number"] == 8
    assert SN.allocate(cfg, by="t", specs_dir=d)["number"] == 9
    assert len(SN.list_reservations(cfg, specs_dir=d)) == 2


def test_missing_specs_dir_keeps_key(tmp_path, cfg):
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    sd = _sd(repo)
    assert SN.allocate(cfg, by="t", specs_dir=sd)["number"] == 1
    sd.mkdir(parents=True)
    assert len(SN.list_reservations(cfg, specs_dir=sd)) == 1


# ---- CLI -------------------------------------------------------------------------------------

@pytest.fixture
def run(cfg, monkeypatch, repo):
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: {**cfg, "specs_dir": str(_sd(repo))})
    monkeypatch.delenv("NW_MAILBOX_ADDR", raising=False)
    return lambda *a, **kw: CliRunner().invoke(cli, ["spec", *a], **kw)


def test_cli_reserve_human_and_json(run, monkeypatch):
    r = run("reserve")
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "SPEC-0002"
    monkeypatch.setenv("NW_MAILBOX_ADDR", "steward:Meta")
    r = run("reserve", "--for", "demo", "--json")
    d = json.loads(r.output)
    assert d["schema"] == "nw.spec.reservation.v1"
    assert (d["id"], d["by"], d["note"], d["campaign"]) == ("SPEC-0003", "steward:Meta",
                                                            "demo", None)
    assert json.loads(run("reserve", "--json").output)["by"] == "steward:Meta"
    monkeypatch.delenv("NW_MAILBOX_ADDR")
    assert json.loads(run("reserve", "--json").output)["by"] == "human"


def test_cli_reservations_table_json_all(run, tmp_path):
    run("reserve", "--for", "alpha")
    other = _make_repo(tmp_path / "repo2")
    run("reserve", "--specs-dir", str(_sd(other)), "--for", "beta")
    table = run("reservations").output
    header = next(ln for ln in table.splitlines() if "note" in ln)
    assert re.findall(r"[a-z]+", header) == ["id", "note", "by", "campaign", "age"]
    assert "alpha" in table and "beta" not in table
    d = json.loads(run("reservations", "--json").output)
    assert d["schema"] == "nw.spec.reservations.v1" and len(d["reservations"]) == 1
    both = json.loads(run("reservations", "--all", "--json").output)
    assert sorted(r["note"] for r in both["reservations"]) == ["alpha", "beta"]
    assert [r["note"] for r in json.loads(
        run("reservations", "--json", "--specs-dir", str(_sd(other))).output
    )["reservations"]] == ["beta"]


def test_cli_release_dry_run_then_apply(run):
    run("reserve")
    r = run("release", "SPEC-0002")
    assert r.exit_code == 0 and "pass --yes" in r.output
    assert "SPEC-0002" in run("reservations").output
    assert run("release", "2", "--yes").exit_code == 0
    assert "SPEC-0002" not in run("reservations").output
    assert run("release", "2", "--yes").exit_code != 0
    assert run("release", "2").exit_code != 0
    bad = run("release", "abc", "--yes")
    assert bad.exit_code != 0 and "not a spec number" in bad.output


def test_cli_release_specs_dir(run, tmp_path):
    other = _make_repo(tmp_path / "repo2")
    osd = str(_sd(other))
    run("reserve", "--specs-dir", osd, "--for", "beta")
    assert run("release", "2", "--yes").exit_code != 0                # not reserved under repo's key
    dry = run("release", "2", "--specs-dir", osd)
    assert dry.exit_code == 0 and "pass --yes" in dry.output
    assert "SPEC-0002" in run("reservations", "--specs-dir", osd).output
    assert run("release", "SPEC-0002", "--specs-dir", osd, "--yes").exit_code == 0
    assert "SPEC-0002" not in run("reservations", "--specs-dir", osd).output


# ---- spec new ---------------------------------------------------------------------------------

def _git_specs_cfg(tmp_path):
    cfg = _promote_cfg(tmp_path)
    repo = tmp_path / "scaf"
    (repo / "docs").mkdir(parents=True)
    sd = repo / "docs" / "specs"
    (tmp_path / "specs").rename(sd)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "b1")
    _touch(repo, 1)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "checkout", "-q", "main")
    cfg["specs_dir"] = str(sd)
    return cfg, sd


def test_spec_new_sees_branch_number(tmp_path):
    cfg, sd = _git_specs_cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")
    path, spec_id = S.scaffold_from_idea(cfg, "instant review agent")
    assert spec_id == "SPEC-0002" and path.name.startswith("0002-")
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == [2]


def test_spec_new_failure_releases(tmp_path, monkeypatch):
    cfg, sd = _git_specs_cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(W, "set_property", boom)
    with pytest.raises(RuntimeError):
        S.scaffold_from_idea(cfg, "instant review agent")
    assert SN.list_reservations(cfg, specs_dir=sd) == []
    monkeypatch.undo()

    real_write = type(sd).write_text

    def bad_write(self, *a, **k):
        if self.name.endswith(".md") and self.name[:4].isdigit():
            raise OSError("disk")
        return real_write(self, *a, **k)

    monkeypatch.setattr(type(sd), "write_text", bad_write)
    with pytest.raises(OSError):
        S.scaffold_from_idea(cfg, "instant review agent")
    monkeypatch.undo()
    assert SN.list_reservations(cfg, specs_dir=sd) == []
    assert _find(cfg, "instant review agent").properties.get("SPEC") is None


def test_spec_new_release_failure_keeps_original_error(tmp_path, monkeypatch):
    cfg, sd = _git_specs_cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")

    def boom(*a, **k):
        raise RuntimeError("original")

    def bad_release(*a, **k):
        raise OSError("release failed")

    monkeypatch.setattr(W, "set_property", boom)
    monkeypatch.setattr(SN, "release", bad_release)
    with pytest.raises(RuntimeError, match="original"):
        S.scaffold_from_idea(cfg, "instant review agent")
    monkeypatch.undo()
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == [2]  # stale, kept


# ---- SPEC-0569: remote refs, collisions, number_taken, check-numbers ----------------------------

def _commit_on(repo, branch, name, *, base="main", text="x\n"):
    """Commit docs/specs/<name> on `branch` (created from `base`); leave `main` checked out."""
    _git(repo, "checkout", "-q", "-B", branch, base)
    (_sd(repo) / name).write_text(text)
    _git(repo, "add", f"docs/specs/{name}")
    _git(repo, "commit", "-q", "-m", name)
    _git(repo, "checkout", "-q", "main")


def _with_origin(tmp_path, repo):
    """A bare `origin` fed from `repo`, fetched; returns the bare path (origin/HEAD is set)."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "remote", "set-head", "origin", "main")
    return bare


def _clone_push(tmp_path, bare, name, branch="main"):
    """Another clone pushes docs/specs/<name> to origin/<branch>."""
    other = tmp_path / f"clone-{name}"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    d = other / "docs" / "specs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("x\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", name)
    _git(other, "push", "-q", "origin", f"HEAD:{branch}")


def _store_bytes(cfg):
    p = SN._paths(cfg)[0]
    return p.read_bytes() if p.exists() else b""


def test_allocate_sees_remote_tracking_refs_and_ignores_origin_head(tmp_path, cfg, repo):
    bare = _with_origin(tmp_path, repo)
    assert "refs/remotes/origin/HEAD" in subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname)"], capture_output=True,
        text=True).stdout
    _clone_push(tmp_path, bare, "0490-remote.md")
    _git(repo, "fetch", "-q", "origin")
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 491
    assert [r for r, _ in SN._scan_refs(str(repo))].count("refs/remotes/origin/HEAD") == 0


def test_collisions_filename_other_branch_remote_and_worktree(tmp_path, cfg, repo):
    bare = _with_origin(tmp_path, repo)
    _touch(repo, 7)                                    # 0007-x.md in the checked tree (untracked)
    assert SN.collisions(cfg, specs_dir=_sd(repo)) == []
    _commit_on(repo, "b1", "0007-b.md")
    (c,) = SN.collisions(cfg, specs_dir=_sd(repo))
    assert (c["number"], c["id"], c["kind"], c["file"]) == (7, "SPEC-0007", "filename",
                                                           "docs/specs/0007-x.md")
    assert c["other"] == ["b1"] and c["other_files"] == ["0007-b.md"]
    _git(repo, "branch", "-q", "-D", "b1")
    _clone_push(tmp_path, bare, "0007-r.md", branch="feature")
    _git(repo, "fetch", "-q", "origin")
    (c,) = SN.collisions(cfg, specs_dir=_sd(repo))
    assert c["other"] == ["origin/feature"] and c["other_files"] == ["0007-r.md"]
    w = tmp_path / "w9"
    _git(repo, "worktree", "add", "-q", "--detach", str(w), "main")
    _touch(w, 7)
    (w / "docs/specs/0007-x.md").rename(w / "docs/specs/0007-w.md")
    (c,) = SN.collisions(cfg, specs_dir=_sd(repo))
    assert f"worktree:{os.path.realpath(w)}" in c["other"]
    assert c["other_files"] == ["0007-r.md", "0007-w.md"]


def test_collisions_same_filename_elsewhere_is_not_a_collision(tmp_path, cfg, repo):
    _with_origin(tmp_path, repo)
    _commit_on(repo, "b1", "0007-x.md")
    _touch(repo, 7)
    assert SN.collisions(cfg, specs_dir=_sd(repo)) == []


def test_collisions_stale_ref_with_another_slug_is_reported(tmp_path, cfg, repo):
    """Accepted limitation: a leftover branch with an old slug is reported until it is deleted."""
    _touch(repo, 7)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seven")
    _commit_on(repo, "stale", "0007-old.md", base="main~1")
    assert [c["other"] for c in SN.collisions(cfg, specs_dir=_sd(repo))] == [["stale"]]
    _git(repo, "branch", "-q", "-D", "stale")
    assert SN.collisions(cfg, specs_dir=_sd(repo)) == []


def test_collisions_reservation_cases(tmp_path, cfg, repo):
    _touch(repo, 7)
    sd = _sd(repo)
    SN.allocate(cfg, by="t", campaign="C1", specs_dir=sd)        # number 8
    SN.allocate(cfg, by="t", note="human", specs_dir=sd)         # number 9, no campaign
    for n in (8, 9):
        _touch(repo, n)
    got = SN.collisions(cfg, specs_dir=sd, campaign="C2")
    assert [(c["number"], c["kind"], c["other"]) for c in got] == [(8, "reservation", ["C1"])]
    assert SN.collisions(cfg, specs_dir=sd, campaign="C1") == []
    assert SN.collisions(cfg, specs_dir=sd, campaign="C1#2") == []      # split suffix stripped
    _git(repo, "checkout", "-q", "-b", "nw/C1")
    assert SN.collisions(cfg, specs_dir=sd) == []                       # branch detection
    _git(repo, "checkout", "-q", "-b", "nw/C1#2", "main")
    assert SN.collisions(cfg, specs_dir=sd) == []                       # `#k` on the branch side
    _git(repo, "checkout", "-q", "main")
    assert [c["number"] for c in SN.collisions(cfg, specs_dir=sd)] == [8]


def test_collisions_pruned_reservation_and_store_untouched(tmp_path, cfg, repo):
    sd = _sd(repo)
    SN.allocate(cfg, by="t", campaign="C1", specs_dir=sd)        # number 2
    _touch(repo, 2)
    assert [c["kind"] for c in SN.collisions(cfg, specs_dir=sd, campaign="C2")] == ["reservation"]
    before = _store_bytes(cfg)
    SN.collisions(cfg, specs_dir=sd, campaign="C2")
    assert _store_bytes(cfg) == before
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")                      # committed: pruned in memory
    before = _store_bytes(cfg)
    assert SN.collisions(cfg, specs_dir=sd, campaign="C2") == []
    assert _store_bytes(cfg) == before and json.loads(before)["reservations"]


def test_prune_drops_a_reservation_committed_only_on_a_remote_tracking_ref(tmp_path, cfg, repo):
    bare = _with_origin(tmp_path, repo)
    sd = _sd(repo)
    n = SN.allocate(cfg, by="t", campaign="C1", specs_dir=sd)["number"]
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == [n]
    _clone_push(tmp_path, bare, f"{n:04d}-remote.md")            # only origin/main has it
    _git(repo, "fetch", "-q", "origin")
    assert not (_sd(repo) / f"{n:04d}-remote.md").exists()       # not in the local main
    assert SN.list_reservations(cfg, specs_dir=sd) == []         # pruned through refs/remotes
    assert SN.allocate(cfg, by="t", campaign="C1", specs_dir=sd)["number"] > n


def test_number_taken_locations_and_exclusions(tmp_path, cfg, repo):
    bare = _with_origin(tmp_path, repo)
    sd = _sd(repo)
    SN.allocate(cfg, by="t", campaign="C1", specs_dir=sd)
    before = _store_bytes(cfg)
    assert SN.number_taken(cfg, 7, specs_dir=sd) == []
    _commit_on(repo, "b1", "0007-b.md")
    _clone_push(tmp_path, bare, "0007-r.md", branch="feature")
    _git(repo, "fetch", "-q", "origin")
    w = tmp_path / "w9"
    _git(repo, "worktree", "add", "-q", "--detach", str(w), "main")
    _touch(w, 7)
    assert SN.number_taken(cfg, "SPEC-0007", specs_dir=sd) == sorted(
        ["b1", "origin/feature", f"worktree:{os.path.realpath(w)}"])
    # the tree's own leftover file and the campaign's own ref do not count
    _touch(repo, 8)
    assert SN.number_taken(cfg, 8, specs_dir=sd) == []
    _commit_on(repo, "nw/C1", "0009-mine.md")
    assert SN.number_taken(cfg, 9, specs_dir=sd) == ["nw/C1"]
    assert SN.number_taken(cfg, 9, specs_dir=sd, exclude_campaign="C1#2") == []
    assert _store_bytes(cfg) == before


def test_cli_check_numbers(tmp_path, cfg, repo, run):
    r = run("check-numbers")
    assert r.exit_code == 0 and r.output.strip() == "no spec-number collisions"
    _commit_on(repo, "b1", "0001-other.md")
    r = run("check-numbers")
    assert r.exit_code == 1, r.output
    assert "SPEC-0001: docs/specs/0001-x.md also exists as 0001-other.md on b1" in r.output
    assert "renumber one of them: nw spec reserve" in r.output
    d = json.loads(run("check-numbers", "--json").output)
    assert d["schema"] == "nw.spec.collisions.v1"
    assert [(c["number"], c["kind"]) for c in d["collisions"]] == [(1, "filename")]
    other = _make_repo(tmp_path / "repo2")
    r = run("check-numbers", "--specs-dir", str(_sd(other)))
    assert r.exit_code == 0 and r.output.strip() == "no spec-number collisions"
    SN.allocate(cfg, by="t", campaign="C9", specs_dir=_sd(repo))      # number 2
    _touch(repo, 2)
    assert "SPEC-0002: docs/specs/0002-x.md is reserved by campaign C9" in run(
        "check-numbers").output


def test_contract_docs_name_the_allocator_and_the_check():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("AGENTS.md", os.path.join("docs", "README.md")):
        text = open(os.path.join(root, name), encoding="utf-8").read()
        assert "nw spec reserve" in text and "nw spec check-numbers" in text, name
        assert "(next free number)" not in text, name
        assert "--from-idea" in text, name
    agents = open(os.path.join(root, "AGENTS.md"), encoding="utf-8").read()
    step = next(ln for ln in agents.splitlines() if "**New work:**" in ln)
    assert "nw spec reserve" in step


# ---- SPEC-0583: cheaper scans, read-only collisions, own-branch rename -------------------------

def _count_git(monkeypatch):
    """Count `SN._git` calls by subcommand."""
    real, calls = SN._git, []

    def counted(cwd, *args):
        calls.append(args[0])
        return real(cwd, *args)
    monkeypatch.setattr(SN, "_git", counted)
    return calls


def test_allocate_runs_one_ls_tree_per_distinct_commit(tmp_path, cfg, repo, monkeypatch):
    _with_origin(tmp_path, repo)                      # origin/main shares main's commit
    _git(repo, "branch", "same", "main")              # a second local ref at that commit
    _commit_on(repo, "b1", "0002-b.md")               # one ref at a different commit
    SN.allocate(cfg, by="t", campaign="C0", specs_dir=_sd(repo))      # a record to prune
    calls = _count_git(monkeypatch)
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 4
    assert calls.count("for-each-ref") == 1
    assert calls.count("ls-tree") == 2                # main/same/origin/main + b1


def test_prune_without_records_runs_no_ls_tree(tmp_path, cfg, repo, monkeypatch):
    _with_origin(tmp_path, repo)
    sd = _sd(repo)
    calls = _count_git(monkeypatch)
    assert SN.list_reservations(cfg, specs_dir=sd) == []
    with pytest.raises(ValueError):
        SN.release(cfg, 5, specs_dir=sd)
    assert "ls-tree" not in calls and "for-each-ref" not in calls
    rec = SN.allocate(cfg, by="t", specs_dir=sd)
    calls.clear()
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=sd)] == [rec["number"]]
    assert calls.count("for-each-ref") == 1 and calls.count("ls-tree") == 1
    calls.clear()
    assert SN.release(cfg, rec["number"], specs_dir=sd)["number"] == rec["number"]
    assert calls.count("for-each-ref") == 1 and calls.count("ls-tree") == 1
    assert SN.list_reservations(cfg, specs_dir=sd) == []


def test_collisions_is_read_only(tmp_path, repo):
    data = tmp_path / "fresh" / "data"
    cfg = {"data_dir": str(data)}
    _touch(repo, 7)
    assert SN.collisions(cfg, specs_dir=_sd(repo)) == []
    assert not data.exists() and not data.parent.exists()
    data.mkdir(parents=True)
    fd = os.open(data / "spec-reservations.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)                # an allocate in flight
        _commit_on(repo, "b1", "0007-b.md")
        assert [c["kind"] for c in SN.collisions(cfg, specs_dir=_sd(repo))] == ["filename"]
    finally:
        os.close(fd)
    assert [p.name for p in data.iterdir()] == ["spec-reservations.lock"]


def test_collisions_is_read_only_with_a_prunable_store(tmp_path, cfg, repo):
    sd = _sd(repo)
    done = SN.allocate(cfg, by="t", campaign="C-DONE", specs_dir=sd)["number"]
    gone = SN.allocate(cfg, by="t", campaign="C-AB", specs_dir=sd)["number"]
    keep = SN.allocate(cfg, by="t", campaign="C-LIVE", specs_dir=sd)["number"]
    store.campaign_dir(cfg, "C-AB").mkdir(parents=True)
    store.write_state(cfg, "C-AB", {"id": "C-AB", "state": "ABANDONED"})
    _touch(repo, done)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "done")           # `done` is committed on main
    data = Path(cfg["data_dir"])
    store_path, lock_path = SN._paths(cfg)
    before = (_store_bytes(cfg), store_path.stat().st_mtime_ns)
    files = sorted(str(p) for p in data.rglob("*"))
    assert {r["number"] for r in json.loads(before[0])["reservations"]} == {done, gone, keep}
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)                # an allocate in flight
        SN.collisions(cfg, specs_dir=sd, campaign="C-OTHER")
    finally:
        os.close(fd)
    assert (_store_bytes(cfg), store_path.stat().st_mtime_ns) == before
    assert sorted(str(p) for p in data.rglob("*")) == files


def test_collisions_own_branch_slug_rename_is_not_a_collision(tmp_path, cfg, repo):
    _with_origin(tmp_path, repo)
    _git(repo, "branch", "-q", "--set-upstream-to=origin/main", "main")
    sd = _sd(repo)
    (sd / "0001-x.md").rename(sd / "0001-renamed.md")  # uncommitted rename, number 1
    assert SN.collisions(cfg, specs_dir=sd) == []      # main and origin/main still hold 0001-x
    _commit_on(repo, "stale", "0001-old.md")           # another branch, another slug
    (c,) = SN.collisions(cfg, specs_dir=sd)
    assert c["other"] == ["stale"] and "0001-old.md" in c["other_files"]
    _git(repo, "branch", "-q", "-D", "stale")
    _clone_push(tmp_path, tmp_path / "origin.git", "0001-r.md", branch="feature")
    _git(repo, "fetch", "-q", "origin")
    (c,) = SN.collisions(cfg, specs_dir=sd)
    assert c["other"] == ["origin/feature"]


def _tracking_repo(tmp_path, repo):
    """`repo` with a bare origin and `main` tracking `origin/main`; returns the bare path."""
    bare = _with_origin(tmp_path, repo)
    _git(repo, "branch", "-q", "--set-upstream-to=origin/main", "main")
    return bare


def _rename(sd, old, new):
    (sd / old).rename(sd / new)


def test_collisions_upstream_other_filename_is_a_collision(tmp_path, cfg, repo):
    bare = _tracking_repo(tmp_path, repo)
    sd = _sd(repo)
    _clone_push(tmp_path, bare, "0001-r.md")           # another clone, same number, new slug
    _git(repo, "fetch", "-q", "origin")
    (c,) = SN.collisions(cfg, specs_dir=sd)
    assert c["kind"] == "filename" and c["other"] == ["origin/main"]
    assert c["other_files"] == ["0001-r.md"]
    # ahead of the upstream with the tree's own committed rename: that name is the tree's own
    _git(repo, "pull", "-q", "--ff-only")
    _git(repo, "rm", "-q", "docs/specs/0001-r.md")
    _git(repo, "commit", "-q", "-m", "drop r")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    assert SN.collisions(cfg, specs_dir=sd) == []
    _git(repo, "mv", "docs/specs/0001-x.md", "docs/specs/0001-renamed.md")
    _git(repo, "commit", "-q", "-m", "rename")         # committed, unpushed
    assert SN.collisions(cfg, specs_dir=sd) == []
    # ...and a name that appears upstream after the merge-base is still reported while ahead
    _clone_push(tmp_path, bare, "0001-late.md")
    _git(repo, "fetch", "-q", "origin")
    (c,) = SN.collisions(cfg, specs_dir=sd)
    assert c["other"] == ["origin/main"] and "0001-late.md" in c["other_files"]


def test_collisions_upstream_renamed_elsewhere_while_behind(tmp_path, cfg, repo):
    bare = _tracking_repo(tmp_path, repo)
    sd = _sd(repo)
    _rename(sd, "0001-x.md", "0001-renamed.md")        # uncommitted rename: only own state
    assert SN.collisions(cfg, specs_dir=sd) == []
    _rename(sd, "0001-renamed.md", "0001-x.md")
    other = tmp_path / "clone-y"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    _git(other, "mv", "docs/specs/0001-x.md", "docs/specs/0001-y.md")
    _git(other, "commit", "-q", "-m", "y")
    _git(other, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")                # tree is behind; upstream renamed
    (c,) = SN.collisions(cfg, specs_dir=sd)
    assert c["other"] == ["origin/main"] and c["other_files"] == ["0001-y.md"]


def test_collisions_duplicate_number_in_own_tree_is_reported(tmp_path, cfg, repo):
    sd = _sd(repo)
    (sd / "0005-a.md").write_text("x\n")
    (sd / "0005-b.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dup")
    cs = [c for c in SN.collisions(cfg, specs_dir=sd) if c["number"] == 5]
    assert [c["file"] for c in cs] == ["docs/specs/0005-a.md", "docs/specs/0005-b.md"]
    tree = os.path.realpath(repo)
    assert all(f"worktree:{tree}" in c["other"] for c in cs)
    _git(repo, "rm", "-q", "-f", "docs/specs/0005-b.md")
    _git(repo, "commit", "-q", "-m", "one")
    (sd / "0005-b.md").write_text("x\n")               # untracked second file
    cs = [c for c in SN.collisions(cfg, specs_dir=sd) if c["number"] == 5]
    assert len(cs) == 2 and all(f"worktree:{tree}" in c["other"] for c in cs)
    (sd / "0005-b.md").unlink()
    _git(repo, "mv", "docs/specs/0005-a.md", "docs/specs/0005-c.md")   # old name only on main
    assert [c for c in SN.collisions(cfg, specs_dir=sd) if c["number"] == 5] == []


def test_own_refs_shapes(tmp_path, cfg, repo):
    _tracking_repo(tmp_path, repo)
    sd = _sd(repo)
    assert SN._own_refs(str(repo)) == ("main", "origin/main")
    _git(repo, "branch", "-q", "lone", "main")
    _git(repo, "checkout", "-q", "lone")
    assert SN._own_refs(str(repo)) == ("lone", None)
    _git(repo, "checkout", "-q", "main")
    # detached HEAD: owns no ref, so the uncommitted rename is reported against every ref
    _git(repo, "checkout", "-q", "--detach")
    assert SN._own_refs(str(repo)) == (None, None)
    _rename(sd, "0001-x.md", "0001-renamed.md")
    (c,) = SN.collisions(cfg, specs_dir=sd)
    assert {"main", "origin/main", "lone"} <= set(c["other"])
    _rename(sd, "0001-renamed.md", "0001-x.md")
    _git(repo, "checkout", "-q", "main")
    # a linked worktree on nw/<id> with an upstream
    _commit_on(repo, "nw/IDEA-9-1", "0002-a.md")
    _git(repo, "push", "-q", "origin", "nw/IDEA-9-1")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "branch", "-q", "--set-upstream-to=origin/nw/IDEA-9-1", "nw/IDEA-9-1")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "nw/IDEA-9-1")
    assert SN._own_refs(str(wt)) == ("nw/IDEA-9-1", "origin/nw/IDEA-9-1")
    assert SN.collisions(cfg, specs_dir=sd, tree=wt) == []
    _rename(wt / "docs" / "specs", "0002-a.md", "0002-b.md")
    assert SN.collisions(cfg, specs_dir=sd, tree=wt) == []


def test_own_refs_ambiguous_short_name(tmp_path, cfg, repo):
    _tracking_repo(tmp_path, repo)
    sd = _sd(repo)
    _git(repo, "tag", "main")                          # `rev-parse --abbrev-ref HEAD` -> heads/main
    assert SN._own_refs(str(repo)) == ("main", "origin/main")
    _rename(sd, "0001-x.md", "0001-renamed.md")
    assert SN.collisions(cfg, specs_dir=sd) == []
