"""SPEC-0316: a campaign's gate `timeout_s` is the max of main's and its own branch's
`.nw/project.yaml`, capped at the nightwright-level `gate_budget_s`; nothing else is read from the branch."""
import copy
import json
from pathlib import Path

import pytest

from test_agentic_gates import CID, _cfg, _cli, _gate, _repo_with_project_yaml, _state
from nightwright.agentic import gates as G
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

pytestmark = pytest.mark.fast


def _branch_yaml(wt, *gates, raw=None):
    (Path(wt) / ".nw").mkdir(exist_ok=True)
    body = raw if raw is not None else (
        "schema: nw.project.v1\ngates:\n" + "".join(
            "  - {" + ", ".join(f"{k}: {v!r}" if isinstance(v, str) else f"{k}: {v}"
                                for k, v in g.items()) + "}\n" for g in gates))
    (Path(wt) / ".nw" / "project.yaml").write_text(body)


def _g(name, cmd="true", **kw):
    return {"name": name, "cmd": cmd, "cwd": ".", "timeout_s": 1200, "required": True,
            "retries": 0, **kw}


@pytest.fixture
def world(tmp_path):
    cfg = _cfg(tmp_path)
    wt = tmp_path / "worktree"
    wt.mkdir()
    st = _state(wt)
    S.create_campaign(cfg, st)
    return cfg, st, wt


# ---- gate_limits_overlay ------------------------------------------------------------------

def test_overlay_raises_and_reports(tmp_path):
    _branch_yaml(tmp_path, {"name": "t", "cmd": "true", "timeout_s": 30})
    main = {"gates": [_g("t", timeout_s=1), _g("other", timeout_s=5)]}
    out, raises = P.gate_limits_overlay(main, tmp_path, cap_s=3600)
    assert [g["timeout_s"] for g in out["gates"]] == [30, 5]
    assert raises == [{"gate": "t", "main_timeout_s": 1, "branch_timeout_s": 30,
                       "effective_timeout_s": 30, "capped": False}]


@pytest.mark.parametrize("branch_t", [30, 10])           # equal, lower
def test_overlay_never_lowers(tmp_path, branch_t):
    _branch_yaml(tmp_path, {"name": "t", "cmd": "true", "timeout_s": branch_t})
    main = {"gates": [_g("t", timeout_s=30)]}
    out, raises = P.gate_limits_overlay(main, tmp_path, cap_s=3600)
    assert out is main and raises == []


def test_overlay_ignores_everything_but_timeout(tmp_path):
    _branch_yaml(tmp_path, {"name": "t", "cmd": "rm -rf /", "required": False, "retries": 3,
                            "cwd": "sub", "timeout_s": 50},
                 {"name": "branch-only", "cmd": "true", "timeout_s": 9999})
    main = {"gates": [_g("t", cmd="true", timeout_s=1)]}
    out, raises = P.gate_limits_overlay(main, tmp_path, cap_s=3600)
    assert out["gates"] == [_g("t", cmd="true", timeout_s=50)]
    assert [r["gate"] for r in raises] == ["t"]


@pytest.mark.parametrize("raw", [None, "gates: [", "schema: nw.project.v1\nbogus: 1\n"])
def test_overlay_unusable_branch_yaml_is_ignored(tmp_path, raw):
    if raw is not None:
        _branch_yaml(tmp_path, raw=raw)
    main = {"gates": [_g("t", timeout_s=1)]}
    assert P.gate_limits_overlay(main, tmp_path, cap_s=3600) == (main, [])


def test_overlay_non_utf8_yaml_is_ignored(tmp_path):
    (tmp_path / ".nw").mkdir()
    (tmp_path / ".nw" / "project.yaml").write_bytes(b"schema: \xff\xfe\n")
    main = {"gates": [_g("t", timeout_s=1)]}
    assert P.gate_limits_overlay(main, tmp_path, cap_s=3600) == (main, [])


def test_overlay_does_not_mutate_input(tmp_path):
    _branch_yaml(tmp_path, {"name": "t", "cmd": "true", "timeout_s": 30})
    main = {"gates": [_g("t", timeout_s=1)]}
    snap = copy.deepcopy(main)
    out, _ = P.gate_limits_overlay(main, tmp_path, cap_s=3600)
    assert main == snap and out is not main and out["gates"][0] is not main["gates"][0]


def test_overlay_caps_the_branch_value(tmp_path):
    _branch_yaml(tmp_path, {"name": "t", "cmd": "true", "timeout_s": 10 ** 9})
    out, raises = P.gate_limits_overlay({"gates": [_g("t", timeout_s=1)]}, tmp_path, cap_s=30)
    assert out["gates"][0]["timeout_s"] == 30
    assert raises[0]["capped"] is True and raises[0]["branch_timeout_s"] == 10 ** 9
    assert "capped from 1000000000s" in P.gate_limits_finding(raises)["detail"]


def test_overlay_main_above_cap_is_never_reduced(tmp_path):
    _branch_yaml(tmp_path, {"name": "t", "cmd": "true", "timeout_s": 500})
    main = {"gates": [_g("t", timeout_s=100)]}
    assert P.gate_limits_overlay(main, tmp_path, cap_s=30) == (main, [])


def test_overlay_below_cap_is_not_marked_capped(tmp_path):
    _branch_yaml(tmp_path, {"name": "t", "cmd": "true", "timeout_s": 20})
    _, raises = P.gate_limits_overlay({"gates": [_g("t", timeout_s=1)]}, tmp_path, cap_s=30)
    assert raises[0]["capped"] is False
    assert "capped" not in P.gate_limits_finding(raises)["detail"]


# ---- through G.run_all ----------------------------------------------------------------------

def _sidecar_timeout(cfg, run):
    side = S.campaign_dir(cfg, CID) / run.results[0].log.replace(".log", ".json")
    return json.loads(side.read_text())["timeout_s"]


def test_run_all_with_overlay_passes_where_main_alone_times_out(world):
    cfg, st, wt = world
    _branch_yaml(wt, {"name": "slow", "cmd": "sleep 2", "timeout_s": 30})
    main = {"gates": [_gate("slow", "sleep 2", timeout_s=1)]}
    plain = G.run_all(main, st, cfg, seq=1)
    assert plain.results[0].timed_out and _sidecar_timeout(cfg, plain) == 1
    pcfg, raises = P.campaign_gate_config(cfg, main, wt)
    run = G.run_all(pcfg, st, cfg, seq=2)
    assert run.ok and raises and _sidecar_timeout(cfg, run) == 30


def test_run_all_branch_value_is_capped_at_gate_budget(world):
    cfg, st, wt = world
    cfg["agentic"] = {"profiles": {}, "events": {"gate_budget_s": 30}}
    _branch_yaml(wt, {"name": "g", "cmd": "true", "timeout_s": 10 ** 9})
    pcfg, raises = P.campaign_gate_config(cfg, {"gates": [_gate("g", "true", timeout_s=1)]}, wt)
    run = G.run_all(pcfg, st, cfg, seq=1)
    assert _sidecar_timeout(cfg, run) == 30 and raises[0]["capped"] is True


def test_run_all_main_above_cap_stays_at_main(world):
    cfg, st, wt = world
    cfg["agentic"] = {"profiles": {}, "events": {"gate_budget_s": 30}}
    _branch_yaml(wt, {"name": "g", "cmd": "true", "timeout_s": 500})
    pcfg, raises = P.campaign_gate_config(cfg, {"gates": [_gate("g", "true", timeout_s=100)]}, wt)
    run = G.run_all(pcfg, st, cfg, seq=1)
    assert _sidecar_timeout(cfg, run) == 100 and raises == []


# ---- _do_gate / _gate_timeouts ----------------------------------------------------------------

def test_do_gate_runs_raised_limit_and_appends_finding(world):
    cfg, st, wt = world
    _branch_yaml(wt, {"name": "slow", "cmd": "sleep 2", "timeout_s": 30})
    ctx = Q._Ctx(cfg)
    ctx._projects["demo"] = {"gates": [_gate("slow", "sleep 2", timeout_s=1)]}
    res = Q._do_gate(ctx, st)
    assert res.gate_run.ok and _sidecar_timeout(cfg, res.gate_run) == 30
    (kind, payload), = res.events_before
    assert kind == "finding" and payload["gate"] == "gate-limits"
    assert payload["severity"] == "info" and "slow: 1s -> 30s" in payload["detail"]
    assert S.read_ledger(cfg, CID, types=["finding"]) == []     # never writes the ledger itself


def test_do_gate_without_a_raise_appends_nothing(world):
    cfg, st, wt = world
    ctx = Q._Ctx(cfg)
    ctx._projects["demo"] = {"gates": [_gate("g", "true", timeout_s=5)]}
    res = Q._do_gate(ctx, st)
    assert res.gate_run.ok and res.events_before == []


def test_gate_timeouts_names_the_effective_limit(world):
    cfg, st, wt = world
    _branch_yaml(wt, {"name": "slow", "cmd": "true", "timeout_s": 3600})
    ctx = Q._Ctx(cfg)
    ctx._projects["demo"] = {"gates": [_gate("slow", "true", timeout_s=1200)]}
    assert Q._gate_timeouts(ctx, st) == {"slow": 3600}
    last = {"results": [{"name": "slow", "required": True, "ok": False, "timed_out": True}]}
    d = Q.decide_gate_timeout(dict(st, last_gate=last,
                                   counters={"timeout_regates": Q.TIMEOUT_REGATE_MAX}),
                              Q._gate_timeouts(ctx, st))
    assert d.kind == "park" and "slow (3600s)" in d.detail and "after 3600s" in d.detail


# ---- nw queue gates ---------------------------------------------------------------------------

def test_cli_queue_gates_applies_the_overlay(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    root = _repo_with_project_yaml(tmp_path, "  - {name: slow, cmd: 'sleep 2', timeout_s: 1}\n")
    cfg["nw_checkout"] = str(root)
    wt = tmp_path / "worktree"
    wt.mkdir()
    _branch_yaml(wt, {"name": "slow", "cmd": "sleep 2", "timeout_s": 30})
    S.create_campaign(cfg, dict(_state(wt), project="Meta-Tools"))
    r = _cli(cfg, monkeypatch, ["queue", "gates", CID, "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["ok"] is True and d["results"][0]["timed_out"] is False
    side = S.campaign_dir(cfg, CID) / d["results"][0]["log"].replace(".log", ".json")
    assert json.loads(side.read_text())["timeout_s"] == 30

