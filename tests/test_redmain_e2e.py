"""SPEC-0420 real verification: a real git repo whose loop and close `pytest` gate runs real
pytest over a 3-test tree (the full suite only when `NW_AFFECTED_FULL` / `NW_SENTINEL` is set,
like the SPEC-0378 affected-only close gate), a real `NW_DATA_DIR`, real `sentinel.run()` and
real queue ticks. Only the implementer persona is a scripted FakeRunner that edits files."""
import json
import sys

import pytest

from test_agentic_e2e import ENV, _git, _write, make_cfg, run_cli, script
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import redmain as RM
from nightwright.agentic import sentinel as SN
from nightwright.agentic import store as S

PROJ = "Meta-Tools"
GOOD = "def f():\n    return 1\n"
BAD = "def f():\n    return 2\n"
TESTS = ("from mod import f\n\n\ndef test_a():\n    assert f() == 1\n\n\n"
         "def test_b():\n    assert True\n\n\ndef test_c():\n    assert True\n")
WEAK = "def test_b():\n    assert True\n\n\ndef test_c():\n    assert True\n"
YAML = """schema: nw.project.v1
base: main
gates:
  - {name: pytest, cmd: 'sh gates/pytest.sh', timeout_s: 120, required: true,
     results: {format: pytest}}
merge:
  policy: merge
  base: main
agents:
  commit_trailer: "Co-Authored-By: nightwright test <nightwright@test>"
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    for k in ("NW_AGENTIC_RUNNER", "NW_AGENTIC_FAKE_SCRIPT", "NW_AFFECTED_FULL", "NW_SENTINEL"):
        monkeypatch.delenv(k, raising=False)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "nightwright").mkdir(parents=True)
    _write(repo, "pyproject.toml", "[project]\nname='nw'\n")
    _write(repo, "mod.py", GOOD)
    _write(repo, "tests/test_t.py", TESTS)
    _write(repo, "pytest.ini", "[pytest]\npythonpath = .\n")
    _write(repo, ".gitignore", "__pycache__/\n.pytest_cache/\n")
    # full suite only under the flag (SPEC-0378's affected-only close gate selects nothing here)
    _write(repo, "gates/pytest.sh",
           'if [ -n "$NW_AFFECTED_FULL$NW_SENTINEL" ]; then\n'
           f'  exec {sys.executable} -m pytest -q -p no:cacheprovider tests\nfi\n'
           'echo "affected-only: no test selected"\n')
    _write(repo, ".nw/project.yaml", YAML)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _cfg(tmp_path, repo, entries, **red):
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries))
    cfg["agentic"]["escalations"] = {"notify": "none"}
    cfg["agentic"]["red_main"] = {
        "probe_cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider", **red}
    return cfg


def _commit_main(repo, rel, text, msg):
    _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _branch(tmp_path, repo, branch, files):
    wt = tmp_path / "wts" / branch.replace("/", "-")
    _git(repo, "worktree", "add", "-q", "-b", branch, str(wt), "main")
    for rel, text in files.items():
        _write(wt, rel, text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", f"work on {branch}")
    _git(repo, "worktree", "remove", "--force", str(wt))


def _merge(cfg, branch, **kw):
    return Q.apply_merge(cfg, Q.plan_merge(cfg, branch, project=PROJ, **kw))["id"]


def _impl(edits, status="done"):
    return {"persona": "implementer", "edits": edits, "cost_usd": 0.1,
            "verdict": {"status": status, "summary": "red-base fix", "changed_files": [],
                        "tests_run": [], "ac_status": [], "blockers": [],
                        "followup_ideas": []}}


def _sentinel(cfg):
    return SN.run(cfg, PROJ, project_cfg=P.project_config(cfg, PROJ))


def _tick_until(cfg, done, monkeypatch=None, n=25):
    for _ in range(n):
        if monkeypatch is not None:                     # the real CLI: `nw queue tick`
            r = run_cli(cfg, monkeypatch, ["queue", "tick", "--json"])
            assert r.exit_code == 0, r.output
        else:
            Q.tick(cfg)
        if done():
            return
    raise AssertionError("condition not reached")


def _kind(cfg, kind):
    return [s for s in S.list_campaigns(cfg, include_terminal=True) if s.get("kind") == kind]


def _state(cfg, cid):
    return S.read_state(cfg, cid)


def _types(cfg, cid, t):
    return S.read_ledger(cfg, cid, types=[t])


def test_red_main_fix_forward_holds_merges_and_green_releases(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cfg = _cfg(tmp_path, repo, [_impl([{"path": "mod.py", "write": GOOD}])])
    _commit_main(repo, "mod.py", BAD, "break main directly")
    # 1. red sentinel -> episode, no escalation
    out = _sentinel(cfg)
    assert not out["ok"], out
    red = RM.read(cfg, PROJ)
    assert red["open"] and red["episode"] == 1
    assert "tests/test_t.py::test_a" in red["failing"], red["failing"]
    assert ESC.list_all(cfg, "open") == []
    assert len(E.read_since(cfg, 0, types=["red_main"])) == 1
    # a non-fix merge request reaches CLOSING while red
    _branch(tmp_path, repo, "feat/x", {"feature.txt": "x\n"})
    held = _merge(cfg, "feat/x")
    # 2. one tick: exactly one fix-forward campaign, priority 1, full suite, briefed
    run_cli(cfg, monkeypatch, ["queue", "tick", "--json"])
    (fix,) = _kind(cfg, "fixforward")
    assert fix["priority"] == 1 and fix["full_suite"] is True and fix["red_episode"] == 1
    assert "tests/test_t.py::test_a" in fix["red_brief"]
    assert "tests/test_t.py::test_a" in fix["pending_brief"]["must_fix"]
    # 3. fix: real full-suite loop gate -> CLOSING (no REVIEWING) -> merged ff-only
    _tick_until(cfg, lambda: _state(cfg, fix["id"])["state"] == "MERGED", monkeypatch)
    assert len(_kind(cfg, "fixforward")) == 1, "one fix campaign per episode"
    trans = [(e["from"], e["to"]) for e in _types(cfg, fix["id"], "transition")]
    assert ("GATING", "CLOSING") in trans and not any(t == "REVIEWING" for _f, t in trans)
    gates = _types(cfg, fix["id"], "gate")
    assert gates and all(g["ok"] for g in gates[-2:])
    logs = list((S.campaign_dir(cfg, fix["id"])).rglob("*pytest*.log"))
    assert logs and any("3 passed" in p.read_text() for p in logs), "the full suite ran"
    assert (repo / "mod.py").read_text() == GOOD
    assert _git(repo, "rev-parse", "HEAD") == _state(cfg, fix["id"])["close"]["merged_sha"]
    # the non-fix merge was held: CLOSING, one close_held, no attempt, not merged
    h = _state(cfg, held)
    assert h["state"] == "CLOSING" and h["counters"]["close_attempts"] == 0
    assert len(_types(cfg, held, "close_held")) == 1
    assert not (repo / "feature.txt").exists()
    # 4. the fix made the sentinel due at once; green closes the episode; the hold releases
    assert RM.due_reason(cfg, PROJ) and SN.check_due(cfg, PROJ,
                                                     project_cfg=P.project_config(cfg, PROJ))
    assert _sentinel(cfg)["ok"]
    assert not RM.is_red(cfg, PROJ)
    _tick_until(cfg, lambda: _state(cfg, held)["state"] == "MERGED", monkeypatch)
    assert (repo / "feature.txt").exists()
    assert ESC.list_all(cfg, "open") == [] and not ESC.list_all(cfg)


def _culprit_world(tmp_path, entries, **red):
    repo = _repo(tmp_path)
    cfg = _cfg(tmp_path, repo, entries, **red)
    assert _sentinel(cfg)["ok"], "green before the merges"
    _branch(tmp_path, repo, "feat/a", {"a.txt": "a\n"})
    _branch(tmp_path, repo, "feat/b", {"mod.py": BAD})       # breaks test_a; gates select none
    m1 = _merge(cfg, "feat/a")
    _tick_until(cfg, lambda: _state(cfg, m1)["state"] == "MERGED")
    m2 = _merge(cfg, "feat/b")
    _tick_until(cfg, lambda: _state(cfg, m2)["state"] == "MERGED")
    return repo, cfg, m1, m2


def test_failed_fix_twice_reverts_the_culprit_and_weakening_is_refused(tmp_path):
    weaken = _impl([{"path": "tests/test_t.py", "write": WEAK}])   # deletes the failing test
    repo, cfg, m1, m2 = _culprit_world(tmp_path, [weaken])
    assert not _sentinel(cfg)["ok"]
    assert [m["campaign"] for m in RM.read(cfg, PROJ)["merges"]] == [m1, m2]
    _tick_until(cfg, lambda: any(s["state"] == "MERGED" for s in _kind(cfg, "revert")), n=40)
    fixes = _kind(cfg, "fixforward")
    assert len(fixes) == 2, "fixforward_attempts (2) attempts, then the revert"
    for fx in fixes:
        parks = _types(cfg, fx["id"], "park")
        assert parks and parks[0]["reason"] == "fixforward_weakens"
        assert "removes 1 test function" in parks[0]["detail"]
        assert fx["state"] == "ABANDONED"
    red = RM.read(cfg, PROJ)
    assert red["culprit"]["campaign"] == m2 and red["localize"]["probes"] <= 2
    (rv,) = _kind(cfg, "revert")
    assert rv["revert"]["campaign"] == m2
    assert (repo / "mod.py").read_text() == GOOD and (repo / "a.txt").exists()
    assert "def test_a" in (repo / "tests/test_t.py").read_text(), "the weakening never landed"
    assert _sentinel(cfg)["ok"] and not RM.is_red(cfg, PROJ)
    assert ESC.list_all(cfg) == [], "no escalation: automatic fixing worked"


def test_revert_conflict_escalates_exactly_once_and_green_withdraws(tmp_path):
    repo, cfg, m1, m2 = _culprit_world(tmp_path, [], fixforward_attempts=0)
    _commit_main(repo, "mod.py", "def f():\n    return 3\n", "touch the culprit's lines")
    assert not _sentinel(cfg)["ok"]
    _tick_until(cfg, lambda: bool(ESC.list_all(cfg, "open")), n=20)
    (rv,) = _kind(cfg, "revert")
    assert _types(cfg, rv["id"], "park")[0]["reason"] == "revert_conflict"
    for _ in range(5):
        Q.tick(cfg)
    assert not _sentinel(cfg)["ok"]                     # red again at the same sha: retried
    for _ in range(3):
        Q.tick(cfg)
    (esc,) = ESC.list_all(cfg)
    assert esc["status"] == "open" and esc["kind"] == "red_main" and esc["recommendation"] == 1
    assert RM.read(cfg, PROJ)["escalation"] == esc["id"]
    assert len(_kind(cfg, "revert")) == 1
    _commit_main(repo, "mod.py", GOOD, "human fixes main")
    assert _sentinel(cfg)["ok"]
    assert ESC.read(cfg, esc["id"])["status"] == "withdrawn"


def test_fixes_red_merge_is_full_suite_unheld_and_refused_when_green(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cfg = _cfg(tmp_path, repo, [], fixforward_attempts=0, revert=False)
    _branch(tmp_path, repo, "early", {"note.txt": "n\n"})
    r = run_cli(cfg, monkeypatch, ["queue", "merge", "early", "--project", PROJ, "--by",
                                   "conductor", "--fixes-red", "--yes"])
    assert r.exit_code != 0 and "no open red episode" in r.output
    _commit_main(repo, "mod.py", BAD, "break")
    assert not _sentinel(cfg)["ok"]
    _branch(tmp_path, repo, "fix/red", {"mod.py": GOOD})        # a hand-made fix
    r = run_cli(cfg, monkeypatch, ["queue", "merge", "fix/red", "--project", PROJ, "--by",
                                   "conductor", "--fixes-red", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    cid = json.loads(r.output)["campaign"]
    st = _state(cfg, cid)
    assert st["priority"] == 1 and st["fixes_red"] and st["full_suite"]
    _tick_until(cfg, lambda: _state(cfg, cid)["state"] in ("MERGED", "PARKED"))
    assert _state(cfg, cid)["state"] == "MERGED"
    assert not _types(cfg, cid, "close_held")
    assert [f["kind"] for f in RM.read(cfg, PROJ)["fixes"]] == ["merge"]
    assert _sentinel(cfg)["ok"] and not RM.is_red(cfg, PROJ)
