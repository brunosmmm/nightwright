"""SPEC-0372: `nw queue watch` stays cheap when idle and shows the workers/gate pools."""
import io

from rich.console import Console

from nightwright.agentic import cli_queue as CQ
from nightwright.agentic import profiles as PR
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.agentic import workers as WK
import pytest

pytestmark = pytest.mark.fast


def _fixture(tmp_path):
    from test_agentic_dashboard import _campaign
    from test_agentic_e2e import impl_done, make_cfg, make_repo, script
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    return cfg, repo, _campaign


def test_state_json_parsed_once_until_it_changes(tmp_path, monkeypatch):
    cfg, repo, camp = _fixture(tmp_path)
    camp(cfg, repo, "SPEC-0001-a", "IMPLEMENTING")
    S._STATE_CACHE.clear()
    calls = []
    real = S.json.loads
    monkeypatch.setattr(S.json, "loads", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = S.list_campaigns(cfg, include_terminal=True)
    n = len(calls)
    assert n >= 1
    second = S.list_campaigns(cfg, include_terminal=True)
    assert len(calls) == n and first == second
    second[0]["state"] = "MUTATED"                       # callers get private copies
    assert S.list_campaigns(cfg, include_terminal=True)[0]["state"] == "IMPLEMENTING"
    S.write_state(cfg, "SPEC-0001-a", {**first[0], "state": "GATING"})
    assert S.list_campaigns(cfg, include_terminal=True)[0]["state"] == "GATING"


def test_profile_yaml_parsed_once_until_it_changes(tmp_path, monkeypatch):
    cfg = {"config_dir": str(tmp_path / "cfg"), "agentic": {}}
    (tmp_path / "cfg" / "profiles").mkdir(parents=True)
    f = tmp_path / "cfg" / "profiles" / "mine.yaml"
    f.write_text("schema: nw.profile.v1\nextends: balanced\ndescription: one\n")
    PR._DIR_CACHE.clear()
    assert PR.resolve_profile(cfg, "mine")["description"] == "one"
    calls = []
    real = PR.yaml.safe_load
    monkeypatch.setattr(PR.yaml, "safe_load", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(3):
        assert PR.resolve_profile(cfg, "mine")["description"] == "one"
    assert calls == []                                   # nothing re-parsed while unchanged
    f.write_text("schema: nw.profile.v1\nextends: balanced\ndescription: two!\n")
    assert PR.resolve_profile(cfg, "mine")["description"] == "two!"


def test_watch_fingerprint_changes_only_with_data(tmp_path):
    cfg, repo, camp = _fixture(tmp_path)
    camp(cfg, repo, "SPEC-0001-a", "IMPLEMENTING")
    fp = CQ._watch_fingerprint(cfg)
    assert CQ._watch_fingerprint(cfg) == fp
    camp(cfg, repo, "SPEC-0001-b", "QUEUED")
    assert CQ._watch_fingerprint(cfg) != fp


def test_idle_watch_does_not_rebuild_the_payload(tmp_path, monkeypatch):
    from test_agentic_e2e import run_cli
    cfg, repo, camp = _fixture(tmp_path)
    camp(cfg, repo, "SPEC-0001-a", "IMPLEMENTING")
    calls = []
    real = Q.dashboard_payload
    monkeypatch.setattr(Q, "dashboard_payload", lambda *a, **k: calls.append(1) or real(*a, **k))
    r = run_cli(cfg, monkeypatch, ["queue", "watch", "--interval", "0.01", "--iterations", "30",
                                   "--height", "40"])
    assert r.exit_code == 0, r.output
    assert len(calls) == 1                               # 29 idle ticks: stat only


def test_workers_strip_and_gate_markers(tmp_path, monkeypatch):
    cfg, repo, camp = _fixture(tmp_path)
    for cid in ("SPEC-0001-rem", "SPEC-0001-loc", "SPEC-0001-wait"):
        camp(cfg, repo, cid, "GATING")
    monkeypatch.setattr(WK, "load", lambda c: [
        {"name": "box1", "enabled": True, "slots": 2, "draining": False, "cooldown_until": None},
        {"name": "box2", "enabled": False, "slots": 4, "draining": False, "cooldown_until": None}])
    hold = lambda cid, slot: {"slot": slot, "campaign": cid, "pid": 1, "since": None}
    monkeypatch.setattr(Q, "_gate_occupancy", lambda c: {
        "slots": 2, "held": [hold("SPEC-0001-loc", 1)],
        "waiting": [{"campaign": "SPEC-0001-wait", "pid": 2, "since": None}],
        "workers": {"box1": {"slots": 2, "held": [hold("SPEC-0001-rem", 1)]},
                    "box2": {"slots": 4, "held": []}}})
    p = Q.dashboard_payload(cfg)
    where = {c["campaign"]: c["where"] for c in p["gate_pools"]["campaigns"]}
    assert where == {"SPEC-0001-rem": "box1", "SPEC-0001-loc": "local",
                     "SPEC-0001-wait": "waiting"}
    c = Console(width=200, file=io.StringIO(), force_terminal=False, color_system=None)
    c.print(CQ._dashboard(p, 200, 24, 60))
    out = c.file.getvalue()
    for want in ("🖥️ box1", "box1 ● on 1/2", "box2 ○ off 0/4", "🏠 local 1/2",
                 "🖥️ box1 SPEC-0001-rem", "🏠 SPEC-0001-loc", "⏳ SPEC-0001-wait"):
        assert want in out, (want, out)
    assert "🖥️ box1 · slot 1" in out or "🖥️ box1" in out


def test_no_workers_no_strip(tmp_path, monkeypatch):
    cfg, repo, camp = _fixture(tmp_path)
    camp(cfg, repo, "SPEC-0001-a", "IMPLEMENTING")
    p = Q.dashboard_payload(cfg)
    assert p["gate_pools"] is None and CQ._workers_line(p) is None
