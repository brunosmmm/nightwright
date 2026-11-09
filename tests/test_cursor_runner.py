"""SPEC-0436: CursorRunner argv, resolve_runner, campaign pin, doctor/worker probe."""
import json
from pathlib import Path

import pytest

from nightwright import doctor as D
from nightwright.agentic import queue as Q
from nightwright.agentic import runproc as RP
from nightwright.agentic import runner as R
from nightwright.agentic import store
from nightwright.agentic.personas import IMPLEMENTER_SCHEMA

pytestmark = pytest.mark.fast


def _req(tmp_path, **over):
    base = dict(
        persona="implementer", prompt="do the thing", system_append="invariant",
        cwd=tmp_path / "nw", model="grok-4.7-high", effort="high", max_budget_usd=4.0,
        timeout_s=30, permission_mode="bypassPermissions", read_only=False,
        allowed_tools=("Bash(uv run nw *)",), add_dirs=(tmp_path / "nw",),
        json_schema=IMPLEMENTER_SCHEMA, env={}, run_dir=tmp_path / "runs" / "001",
    )
    base.update(over)
    return R.RunRequest(**base)


def test_select_runner_cursor(monkeypatch):
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    from nightwright.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["agentic"]["runner"] == "claude"
    assert isinstance(R.select_runner({}), R.ClaudeRunner)
    assert isinstance(R.select_runner({"agentic": {"runner": "cursor"}}), R.CursorRunner)
    assert isinstance(R.select_runner({}, runner="cursor"), R.CursorRunner)


def test_resolve_runner_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    from nightwright.agentic import project as PJ
    monkeypatch.setattr(PJ, "project_config",
                        lambda cfg, name: {"agents": {"runner": "cursor"}})
    cfg = {"agentic": {"runner": "claude", "project_runners": {"Other": "cursor"}}}
    assert R.resolve_runner(cfg) == "claude"
    assert R.resolve_runner(cfg, project="Other") == "cursor"
    assert R.resolve_runner(cfg, project="Mine") == "cursor"
    assert R.resolve_runner(cfg, campaign={"runner": "claude", "project": "Mine"}) == "claude"
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "cursor")
    assert R.resolve_runner(cfg, campaign={"runner": "claude"}) == "cursor"
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "fake")
    assert R.resolve_runner(cfg) == "fake"
    monkeypatch.delenv("NW_AGENTIC_RUNNER")
    with pytest.raises(ValueError, match="only valid from NW_AGENTIC_RUNNER"):
        R.resolve_runner(cfg, campaign={"runner": "fake"})


def test_build_cursor_argv_write_and_read_only(tmp_path):
    write = R.build_cursor_argv(_req(tmp_path))
    assert write[1:4] == ["-m", "nightwright.agentic.cursor_exec", str(tmp_path / "runs" / "001")]
    assert "cursor-agent" not in write and "--force" not in write and "--mode" not in write
    assert "--read-only" not in write
    assert write[write.index("--add-dir") + 1] == str(tmp_path / "nw")
    assert "CURSOR_API_KEY" not in " ".join(write)
    read = R.build_cursor_argv(_req(tmp_path, read_only=True))
    assert "--read-only" in read and "--force" not in read


def test_cursor_prompt_includes_schema_footer(tmp_path):
    text = R.cursor_prompt(_req(tmp_path))
    assert "do the thing" in text and "invariant" in text
    assert "status" in text and "Reply with a single JSON object" in text


def test_parse_cursor_envelope_and_missing_cost():
    verdict = {"status": "done", "summary": "implemented"}
    env = json.dumps({"type": "result", "result": json.dumps(verdict), "session_id": "c1"})
    p = R.parse_result(env, IMPLEMENTER_SCHEMA, exit_code=0)
    assert p["verdict"] == verdict and p["verdict_ok"]
    assert p["cost_usd"] is None and p["session_id"] == "c1" and not p["is_error"]
    err = R.parse_result("no json", IMPLEMENTER_SCHEMA, exit_code=1)
    assert err["is_error"] and not err["verdict_ok"] and err["cost_usd"] is None


def test_add_pin_vs_live():
    assert Q._campaign_runner_pin(None) == {}
    assert Q._campaign_runner_pin("cursor") == {"runner": "cursor"}
    assert Q._campaign_runner_pin("claude") == {"runner": "claude"}
    with pytest.raises(ValueError, match="unknown runner"):
        Q._campaign_runner_pin("fake")


def test_campaign_pin_and_backend_on_implementing(tmp_path):
    cfg = {"data_dir": str(tmp_path / "data")}
    cid = "SPEC-0001-x"
    store.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "state": "IMPLEMENTING",
                                "project": "Meta-Tools"})
    assert "runner" not in store.read_state(cfg, cid)
    plan = Q.plan_backend(cfg, cid, "cursor")
    assert plan["from"] is None and plan["to"] == "cursor" and plan["state"] == "IMPLEMENTING"
    Q.apply_backend(cfg, plan)
    assert store.read_state(cfg, cid)["runner"] == "cursor"
    kinds = [r["kind"] for r in Q.read_journal(cfg)]
    assert "runner_changed" in kinds
    Q.apply_backend(cfg, Q.plan_backend(cfg, cid, "default"))
    assert "runner" not in store.read_state(cfg, cid)


def test_host_backend_writes_and_clears_user_config(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("outbox_targets: {}\nagentic:\n  gates:\n    slots: 2\n")
    cfg = {"data_dir": str(tmp_path / "data"), "config_dir": str(cfg_dir), "agentic": {}}
    plan = Q.plan_host_backend(cfg, "cursor")
    assert plan["from"] is None and plan["to"] == "cursor"
    Q.apply_host_backend(cfg, plan)
    text = (cfg_dir / "config.yaml").read_text()
    assert "runner: cursor" in text and "slots: 2" in text
    assert cfg["agentic"]["runner"] == "cursor"
    Q.apply_host_backend(cfg, Q.plan_host_backend(cfg, "default"))
    text = (cfg_dir / "config.yaml").read_text()
    assert "runner:" not in text and "slots: 2" in text
    assert "runner" not in cfg["agentic"]


def test_backend_all_pins_non_terminal_only(tmp_path):
    cfg = {"data_dir": str(tmp_path / "data")}
    live = "SPEC-0001-live"
    dead = "SPEC-0001-dead"
    store.create_campaign(cfg, {"id": live, "spec_id": "SPEC-0001", "state": "IMPLEMENTING"})
    store.create_campaign(cfg, {"id": dead, "spec_id": "SPEC-0001", "state": "MERGED"})
    plan = Q.plan_backend_all(cfg, "cursor")
    assert [i["campaign"] for i in plan["items"]] == [live]
    Q.apply_backend_all(cfg, plan)
    assert store.read_state(cfg, live)["runner"] == "cursor"
    assert "runner" not in store.read_state(cfg, dead)
    again = Q.plan_backend_all(cfg, "cursor")
    assert again["items"] == []


def test_backend_refused_on_terminal(tmp_path):
    cfg = {"data_dir": str(tmp_path / "data")}
    cid = "SPEC-0001-y"
    store.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "state": "MERGED"})
    with pytest.raises(ValueError, match="MERGED"):
        Q.plan_backend(cfg, cid, "cursor")


def test_validate_runner_cfg_accepts_cursor():
    Q.validate_runner_cfg({"agentic": {"poll_interval_s": 1, "lease_ttl_s": 5,
                                       "runner": "cursor"}})
    with pytest.raises(ValueError, match="project_runners"):
        Q.validate_runner_cfg({"agentic": {"poll_interval_s": 1, "lease_ttl_s": 5,
                                           "project_runners": {"P": "fake"}}})


def test_doctor_cursor_skip_and_fail(tmp_path, monkeypatch):
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "claude"}}
    (tmp_path / "d").mkdir()
    assert D.check_cursor_agent(cfg)["status"] == "skip"
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "cursor")
    monkeypatch.setattr(RP, "probe_cursor_sdk",
                        lambda environ=None: {"ok": False, "version": None, "login": None,
                                              "error": "cursor-sdk is not installed"})
    c = D.check_cursor_agent(cfg)
    assert c["status"] == "fail" and "not installed" in c["summary"]
    monkeypatch.setattr(RP, "probe_cursor_sdk",
                        lambda environ=None: {"ok": True, "version": "1.3.1",
                                              "login": "CURSOR_API_KEY is set", "error": None})
    assert D.check_cursor_agent(cfg)["status"] == "ok"


def test_probe_cursor_agent_login(tmp_path, monkeypatch):
    exe = tmp_path / "cursor-agent"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(RP, "resolve_cursor_agent", lambda environ=None: str(exe))

    def run(cmd, **kw):
        name = Path(cmd[0]).name if cmd else ""
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "--version":
            return type("P", (), {"returncode": 0, "stdout": "2026.11.05\n", "stderr": ""})()
        if sub == "status":
            return type("P", (), {"returncode": 0, "stdout": "Logged in as x\n", "stderr": ""})()
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": "nope"})()

    rec = RP.probe_cursor_agent(run=run)
    assert rec["ok"] and "Logged in" in rec["login"]


def test_with_cursor_agent_resolves_local_bin(tmp_path, monkeypatch):
    d = tmp_path / ".local" / "bin"
    d.mkdir(parents=True)
    c = d / "cursor-agent"
    c.write_text("#!/bin/sh\n")
    c.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin-nonexistent")
    argv, env = RP.with_cursor_agent(["cursor-agent", "-p"], {"PATH": ""})
    assert argv[0] == str(c) and str(d) in env["PATH"]


def test_with_cursor_agent_raises_when_missing(monkeypatch):
    monkeypatch.setattr(RP, "resolve_cursor_agent", lambda environ=None: None)
    with pytest.raises(FileNotFoundError):
        RP.with_cursor_agent(["cursor-agent"], {"PATH": ""})


def test_project_agents_runner_accepts_cursor(tmp_path):
    from nightwright.agentic import project as PJ
    p = tmp_path / "project.yaml"
    p.write_text("schema: nw.project.v1\nagents:\n  runner: cursor\n")
    assert PJ.load_project_config(p)["agents"]["runner"] == "cursor"
