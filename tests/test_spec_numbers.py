"""Spec-number allocator (SPEC-0208): worktree/branch-aware reservations under a flock."""
import json
import multiprocessing as mp
import subprocess

import pytest
from click.testing import CliRunner

from test_promote import _cfg as _promote_cfg, _find, _seed_idea
from wt import org_write as W
from wt import spec_numbers as SN
from wt import specs as S
from wt.agentic import store
from wt.cli import cli


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": __import__("os").environ["PATH"], "HOME": str(cwd)})


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


def test_corrupt_store_raises_missing_and_empty_work(cfg, repo):
    store_path = SN._paths(cfg)[0]
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 2     # missing
    store_path.write_text("")
    assert SN.allocate(cfg, by="t", specs_dir=_sd(repo))["number"] == 2     # empty
    store_path.write_text("{nope")
    with pytest.raises(ValueError, match="spec-reservations.json"):
        SN.allocate(cfg, by="t", specs_dir=_sd(repo))
    store_path.write_text(json.dumps({"schema": "other", "reservations": []}))
    with pytest.raises(ValueError, match="wrong schema"):
        SN.allocate(cfg, by="t", specs_dir=_sd(repo))


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
    import wt.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: {**cfg, "specs_dir": str(_sd(repo))})
    monkeypatch.delenv("WT_MAILBOX_ADDR", raising=False)
    return lambda *a, **kw: CliRunner().invoke(cli, ["spec", *a], **kw)


def test_cli_reserve_human_and_json(run, monkeypatch):
    r = run("reserve")
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "SPEC-0002"
    monkeypatch.setenv("WT_MAILBOX_ADDR", "steward:Meta")
    r = run("reserve", "--for", "demo", "--json")
    d = json.loads(r.output)
    assert d["schema"] == "wt.spec.reservation.v1"
    assert (d["id"], d["by"], d["note"], d["campaign"]) == ("SPEC-0003", "steward:Meta",
                                                            "demo", None)
    assert json.loads(run("reserve", "--json").output)["by"] == "steward:Meta"


def test_cli_reservations_table_json_all(run, tmp_path):
    run("reserve", "--for", "alpha")
    other = _make_repo(tmp_path / "repo2")
    run("reserve", "--specs-dir", str(_sd(other)), "--for", "beta")
    table = run("reservations").output
    for col in ("id", "note", "by", "campaign", "age"):
        assert col in table
    assert "alpha" in table and "beta" not in table
    d = json.loads(run("reservations", "--json").output)
    assert d["schema"] == "wt.spec.reservations.v1" and len(d["reservations"]) == 1
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
