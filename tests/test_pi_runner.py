"""SPEC-0451: PiRunner argv, map, remote job, doctor, tail helpers."""
import json
from pathlib import Path

import pytest

from nightwright import doctor as D
from nightwright.agentic import queue as Q
from nightwright.agentic import runproc as RP
from nightwright.agentic import runner as R
from nightwright.agentic import tail as TL
from nightwright.agentic import worker as W
from nightwright.agentic import pi_map as PM
from nightwright.agentic.personas import IMPLEMENTER_SCHEMA


def _req(tmp_path, **over):
    base = dict(
        persona="implementer", prompt="do the thing", system_append="invariant tail",
        cwd=tmp_path / "nw", model="anthropic/claude-sonnet-4-6", effort="high",
        max_budget_usd=4.0, timeout_s=30, permission_mode="bypassPermissions", read_only=False,
        allowed_tools=(), add_dirs=(tmp_path / "nw",), json_schema=IMPLEMENTER_SCHEMA, env={},
        run_dir=tmp_path / "runs" / "001",
        pi_provider="anthropic", pi_id="claude-sonnet-4-6", pi_thinking="high",
    )
    base.update(over)
    return R.RunRequest(**base)


def test_build_pi_argv_no_credential(tmp_path):
    argv = R.build_pi_argv(_req(tmp_path))
    assert argv[0] == "node" and argv[-1] == str(tmp_path / "runs" / "001")
    assert "pi_exec.mjs" in argv[1]
    assert "ANTHROPIC" not in " ".join(argv)


def test_with_agent_leaves_node(tmp_path):
    argv, env = RP.with_agent(["node", "/x/pi_exec.mjs", "run"], {"PATH": "/bin"})
    assert argv[0] == "node"


def test_select_runner_pi():
    assert isinstance(R.select_runner({}, runner="pi"), R.PiRunner)


def test_map_pi_builtin():
    row = PM.map_pi_model("sonnet", "high", {})
    assert row["model"] == "anthropic/claude-sonnet-4-6"
    assert row["thinking"] == "high"


def test_map_pi_rejects_slug():
    with pytest.raises(ValueError, match="no Pi mapping"):
        PM.map_pi_model("grok-4.7-high", "high", {})


def test_map_pi_rejects_string_alias_value():
    problems = PM.validate_pi_map_doc(
        {"schema": PM.SCHEMA_ID, "aliases": {"sonnet": "claude-sonnet-4-6"}},
        origin="test", in_file=True)
    assert problems


def test_mapped_model_pi():
    cfg = {"agentic": {"runner": "pi"}}
    model, alias = R.mapped_model(cfg, "sonnet", "high")
    assert model == "anthropic/claude-sonnet-4-6" and alias == "sonnet"


def test_request_round_trip_pi_fields(tmp_path):
    req = _req(tmp_path)
    doc = R.request_to_job(req, tmp_path / "nw")
    assert doc["pi_provider"] == "anthropic"
    back = R.request_from_job(doc, tmp_path / "nw", req.run_dir)
    assert back.pi_provider == "anthropic" and back.pi_thinking == "high"


def test_request_doc_omits_pi_for_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    req = R.RunRequest(
        persona="x", prompt="p", system_append="", cwd=tmp_path, model="sonnet", effort=None,
        max_budget_usd=1, timeout_s=30, permission_mode="x", read_only=True, allowed_tools=(),
        add_dirs=(), json_schema={}, env={}, run_dir=tmp_path / "r")
    doc = R._request_doc(req, "t")
    assert "pi_provider" not in doc


def test_resolve_runner_pi_campaign(monkeypatch):
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    cfg = {"agentic": {"runner": "claude"}}
    assert R.resolve_runner(cfg, campaign={"runner": "pi"}) == "pi"


def test_worker_validate_accepts_pi_runner():
    def bad(msg):
        raise AssertionError(msg)
    W._validate_persona({"run_id": "r1", "persona": "implementer", "read_only": True,
                         "timeout_s": 30, "max_budget_usd": 1.0, "runner": "pi",
                         "request": {"persona": "implementer", "prompt": "p",
                                     "system_append": "", "cwd": ".", "model": "m",
                                     "max_budget_usd": 1, "timeout_s": 30,
                                     "permission_mode": "x", "read_only": True,
                                     "allowed_tools": [], "add_dirs": [], "json_schema": {},
                                     "env": {}}}, bad)


def test_doctor_pi_skip_and_probe(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "claude"}}
    assert D.check_pi_agent(cfg)["status"] == "skip"
    cfg["agentic"]["runner"] = "pi"
    monkeypatch.setattr(RP, "probe_pi",
                        lambda providers, environ=None, run=None: {"ok": False, "error": "no node",
                                                                   "missing": providers})
    assert D.check_pi_agent(cfg)["status"] == "fail"
    monkeypatch.setattr(RP, "probe_pi",
                        lambda providers, environ=None, run=None: {"ok": True, "missing": []})
    assert D.check_pi_agent(cfg)["status"] == "ok"


def test_ensure_pi_persona_provider_only(tmp_path, monkeypatch):
    calls = []
    def fake_probe(providers, environ=None, run=None):
        calls.append(providers)
        return {"ok": providers == ["openai"], "missing": []}
    monkeypatch.setattr(RP, "probe_pi", fake_probe)
    with pytest.raises(FileNotFoundError):
        RP.ensure_pi_persona("anthropic")
    assert calls == [["anthropic"]]
    RP.ensure_pi_persona("openai")


def test_renderer_feed_pi_read_tool():
    ev = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "x"}},
    ]}}
    lines = TL.Renderer().feed_line(json.dumps(ev))
    assert lines and lines[0].plain.strip()


def test_extract_result_pi_stream(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    result = {"type": "result", "result": json.dumps({"status": "done", "summary": "ok"}),
              "total_cost_usd": 0.01, "session_id": "s1", "usage": {}}
    (d / "stream.jsonl").write_text(json.dumps({"type": "assistant", "message": {}}) + "\n"
                                + json.dumps(result) + "\n", encoding="utf-8")
    assert RP.extract_result(d)
    raw = json.loads((d / "result.json").read_text())
    p = R.parse_result(json.dumps(raw), IMPLEMENTER_SCHEMA, exit_code=0)
    assert p["verdict"]["status"] == "done" and p["cost_usd"] == 0.01


def test_queue_pin_pi(tmp_path):
    assert Q._campaign_runner_pin("pi") == {"runner": "pi"}


def test_plan_backend_pi(tmp_path):
    from nightwright.agentic import store
    cfg = {"data_dir": str(tmp_path / "data")}
    cid = "SPEC-pi"
    store.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-pi", "state": "IMPLEMENTING"})
    plan = Q.plan_backend(cfg, cid, "pi")
    Q.apply_backend(cfg, plan)
    assert store.read_state(cfg, cid)["runner"] == "pi"
