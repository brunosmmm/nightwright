"""Spec-number allocator (SPEC-0208): worktree/branch-aware reservations under a flock."""
import json
import multiprocessing as mp
import os
import re
import shutil
import subprocess

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
