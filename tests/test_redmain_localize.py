"""SPEC-0420 AC6: `redmain.localize` bisects a red episode's merges in the sentinel worktree,
running real `pytest` on only the failing ids. Real git repo with four merge commits (the
third breaks `tests/test_t.py::test_b`) and synthetic `close` events for them."""
import sys

import pytest

from test_agentic_close import _commit, _git, _git_env  # noqa: F401
from nightwright.agentic import redmain as RM
from nightwright.agentic import sentinel as SN
from nightwright.agentic import store

pytestmark = pytest.mark.fast

PROJECT = "demo"
GOOD = "def test_a():\n    assert True\n\n\ndef test_b():\n    from mod import f\n    assert f() == 1\n"


def _merged(cfg, cid, target, merged):
    store.campaign_dir(cfg, cid).mkdir(parents=True, exist_ok=True)
    store.write_state(cfg, cid, {"id": cid, "kind": "implement", "spec_id": f"SPEC-{cid[-1]}",
                                 "project": PROJECT, "state": "MERGED",
                                 "created_at": store.now_iso()})
    store.append_event(cfg, cid, "close", {"mode": "merge", "merged_sha": merged,
                                           "target_sha": target})


@pytest.fixture
def episode(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "mod.py", "def f():\n    return 1\n", "mod")
    _commit(root, "tests/test_t.py", GOOD, "tests")
    _commit(root, "pytest.ini", "[pytest]\npythonpath = .\n", "ini")
    green = _git(root, "rev-parse", "HEAD")
    cfg = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
           "agentic": {"escalations": {"notify": "none"}, "red_main": {
               "probe_cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider"}}}
    SN._write(SN.state_path(cfg, PROJECT), {**SN.empty_state(), "last_green_sha": green})
    prev, shas = green, []
    for i, (rel, text) in enumerate([("a.txt", "a\n"), ("b.txt", "b\n"),
                                     ("mod.py", "def f():\n    return 2\n"),
                                     ("d.txt", "d\n")], 1):
        sha = _commit(root, rel, text, f"merge {i}")
        _merged(cfg, f"C-{i}", prev, sha)
        shas.append(sha)
        prev = sha
    RM.on_red(cfg, PROJECT, prev, ["tests/test_t.py::test_b"], root=str(root))
    probes = []
    real = RM._probe

    def counting(wt, ids, cmd, timeout_s):
        probes.append((_git(wt, "rev-parse", "HEAD"), list(ids)))
        return real(wt, ids, cmd, timeout_s)
    monkeypatch.setattr(RM, "_probe", counting)
    return cfg, root, shas, probes


def test_localize_names_the_third_merge_with_real_pytest_probes(episode):
    cfg, root, shas, probes = episode
    out = RM.localize(cfg, PROJECT, root=str(root), project_cfg={"_root": str(root)})
    assert out["culprit"]["campaign"] == "C-3", out
    assert out["culprit"]["target_sha"] == shas[1] and out["culprit"]["merged_sha"] == shas[2]
    assert 1 <= len(probes) <= 3 and out["probes"] == len(probes)
    assert all(ids == ["tests/test_t.py::test_b"] for _sha, ids in probes), "only the ids"
    rec = RM.set_localized(cfg, PROJECT, out)
    assert rec["culprit"]["campaign"] == "C-3" and rec["localize"]["done"]


def test_one_candidate_runs_no_pytest(episode):
    cfg, root, shas, probes = episode
    with RM.locked(cfg, PROJECT):
        rec = RM._load(cfg, PROJECT)
        rec["merges"] = rec["merges"][2:3]
        RM._save(cfg, PROJECT, rec)
    out = RM.localize(cfg, PROJECT, root=str(root), project_cfg={"_root": str(root)})
    assert out["culprit"]["campaign"] == "C-3" and out["probes"] == 0 and probes == []


def test_a_running_sentinel_defers_localize(episode):
    cfg, root, _shas, probes = episode
    with SN.run_lock(cfg, PROJECT):
        out = RM.localize(cfg, PROJECT, root=str(root), project_cfg={"_root": str(root)})
    assert out.get("retry") and out["culprit"] is None and probes == []
