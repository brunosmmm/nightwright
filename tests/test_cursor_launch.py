"""SPEC-0437: conductor/steward launch through cursor-agent when resolve_runner is cursor."""
import os

import pytest
from click.testing import CliRunner

from nightwright import rules
from nightwright.agentic import conductor_cost as CC
from nightwright.agentic import launch as L
from nightwright.agentic import runner as R
from nightwright.agentic import runproc as RP
from nightwright.agentic import store
from nightwright.cli import cli

pytestmark = pytest.mark.fast

PRICE = {"input": 3.0, "output": 15.0}
cfg_holder = [None]


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "nw").mkdir()
    (tmp_path / "foo").mkdir()
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "_root": str(tmp_path / "projects"),
            "agentic": {"conductor": {"prices": {"claude-sonnet-5-5": PRICE}}}}


@pytest.fixture(autouse=True)
def _wire(cfg, monkeypatch, tmp_path):
    cfg_holder[0] = cfg
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    monkeypatch.setattr(rules, "projects_payload", lambda c: {"projects": [
        {"name": "Meta-Tools", "research": {"root": str(tmp_path / "nw")}},
        {"name": "Foo", "research": {"root": str(tmp_path / "foo")}}]})
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg_holder[0])
    monkeypatch.chdir(tmp_path)


class Exec:
    def __init__(self, cfg, monkeypatch):
        self.calls = []

        def fake(file, argv, env):
            self.calls.append((file, argv, env, os.getcwd()))
        monkeypatch.setattr(os, "execvpe", fake)


@pytest.fixture
def ex(cfg, monkeypatch):
    return Exec(cfg, monkeypatch)


def invoke(*args):
    return CliRunner().invoke(cli, ["queue", *args])


def test_claude_default_unchanged(cfg):
    plan = L.build_plan(cfg, "conductor")
    assert plan["runner"] == "claude" and plan["argv"][0] == "claude"
    assert "--permission-mode" in plan["argv"] and "--effort" in plan["argv"]
    assert plan["price"] == PRICE


def test_cursor_conductor_argv(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "cursor")
    plan = L.build_plan(cfg, "conductor")
    assert plan["runner"] == "cursor"
    argv = plan["argv"]
    assert argv[0] == "cursor-agent" and argv[1] == "--resume"
    assert "--force" in argv and "--trust" in argv and "--approve-mcps" in argv
    assert argv[argv.index("--workspace") + 1] == str(tmp_path / "nw")
    assert argv[argv.index("--model") + 1] == "grok-4.7-high"
    assert argv[-1].startswith(L.CONDUCTOR_PROMPT) and "AskUserQuestion" in argv[-1]
    assert "--permission-mode" not in argv and "--allowedTools" not in argv
    assert "--effort" not in argv and "--autocompact" not in argv and "-p" not in argv
    assert plan["env"] == {} and plan["price"] is None
    L.check_price(plan)
    r = invoke("conductor-launch", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "runner: cursor" in r.output and "cursor-agent --resume" in r.output
    assert "grok-4.7-high" in r.output
    assert "grok-4.7[reasoning_effort" not in r.output


def test_steward_follows_project_overlay(cfg, monkeypatch):
    cfg["agentic"]["runner"] = "claude"
    cfg["agentic"]["project_runners"] = {"Foo": "cursor"}
    cond = L.build_plan(cfg, "conductor")
    stew = L.build_plan(cfg, "steward", "Foo")
    assert cond["runner"] == "claude" and cond["argv"][0] == "claude"
    assert stew["runner"] == "cursor" and stew["argv"][0] == "cursor-agent"
    assert stew["argv"][stew["argv"].index("--model") + 1] == "grok-4.7-high"
    assert stew["argv"][-1].startswith("Run the /nw-orient") and "rebase" in stew["argv"][-1]


def test_cursor_needed_for_project_runners(cfg):
    assert R.cursor_needed(cfg) is False
    cfg["agentic"]["project_runners"] = {"Foo": "cursor"}
    assert R.cursor_needed(cfg) is True


def test_missing_prices_skipped_when_host_is_cursor(cfg, monkeypatch):
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "cursor")
    assert L.missing_prices(cfg) == []


def test_create_chat_then_exec(cfg, ex, monkeypatch, tmp_path):
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "cursor")
    monkeypatch.setattr(RP, "resolve_cursor_agent", lambda environ=None: "/bin/cursor-agent")
    monkeypatch.setattr(RP, "create_cursor_chat", lambda exe=None, **k: "chat-uuid")
    monkeypatch.setattr(RP, "with_cursor_agent",
                        lambda argv, env: (["/bin/cursor-agent", *argv[1:]], env))
    r = invoke("conductor-launch")
    assert r.exit_code == 0, r.output
    file, argv, env, cwd = ex.calls[0]
    assert file == "/bin/cursor-agent" and argv[argv.index("--resume") + 1] == "chat-uuid"
    rec = CC.sessions(cfg, "conductor")[-1]
    assert rec["session_id"] == "chat-uuid" and rec["runner"] == "cursor"
    assert cwd == str(tmp_path / "nw")


def test_resume_cursor_skips_transcript(cfg, ex, monkeypatch):
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "cursor")
    CC.record_session(cfg, "conductor", {}, source="launch", session_id="old-chat",
                      extra={"runner": "cursor", "launch_hash": "x"})
    monkeypatch.setattr(RP, "resolve_cursor_agent", lambda environ=None: "/bin/cursor-agent")
    monkeypatch.setattr(RP, "create_cursor_chat",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no create-chat")))
    monkeypatch.setattr(RP, "with_cursor_agent",
                        lambda argv, env: (["/bin/cursor-agent", *argv[1:]], env))
    r = invoke("conductor-launch", "--resume")
    assert r.exit_code == 0, r.output
    argv = ex.calls[0][1]
    assert argv[argv.index("--resume") + 1] == "old-chat"


def test_resume_claude_record_as_cursor_refused(cfg, monkeypatch):
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "cursor")
    CC.record_session(cfg, "conductor", {}, source="launch", session_id="claude-uuid")
    r = invoke("conductor-launch", "--resume", "--dry-run")
    assert r.exit_code != 0 and "not cursor-agent" in r.output


def test_runner_flag_selects_cursor_without_env(cfg, tmp_path):
    plan = L.build_plan(cfg, "conductor", runner="cursor")
    assert plan["runner"] == "cursor" and plan["argv"][0] == "cursor-agent"
    assert plan["argv"][plan["argv"].index("--model") + 1] == "grok-4.7-high"
    r = invoke("conductor-launch", "--runner", "cursor", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "runner: cursor" in r.output and "cursor-agent --resume" in r.output


def test_runner_flag_claude_beats_env(cfg, monkeypatch):
    monkeypatch.setenv("NW_AGENTIC_RUNNER", "cursor")
    plan = L.build_plan(cfg, "conductor", runner="claude")
    assert plan["runner"] == "claude" and plan["argv"][0] == "claude"
    r = invoke("conductor-launch", "--runner", "claude", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "runner: claude" in r.output


def test_resume_without_flag_uses_stored_cursor(cfg, ex, monkeypatch):
    CC.record_session(cfg, "conductor", {}, source="launch", session_id="old-chat",
                      extra={"runner": "cursor", "launch_hash": "x"})
    monkeypatch.setattr(RP, "resolve_cursor_agent", lambda environ=None: "/bin/cursor-agent")
    monkeypatch.setattr(RP, "create_cursor_chat",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no create-chat")))
    monkeypatch.setattr(RP, "with_cursor_agent",
                        lambda argv, env: (["/bin/cursor-agent", *argv[1:]], env))
    r = invoke("conductor-launch", "--resume")
    assert r.exit_code == 0, r.output
    assert ex.calls[0][1][ex.calls[0][1].index("--resume") + 1] == "old-chat"


def test_resume_runner_mismatch_refused(cfg):
    CC.record_session(cfg, "conductor", {}, source="launch", session_id="claude-uuid",
                      extra={"runner": "claude"})
    r = invoke("conductor-launch", "--resume", "--runner", "cursor", "--dry-run")
    assert r.exit_code != 0 and "claude" in r.output


def test_conductor_dry_run_does_not_write_host(cfg, tmp_path):
    r = invoke("conductor-launch", "--runner", "cursor", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "host: cursor" in r.output
    assert not (tmp_path / "config" / "config.yaml").exists()


def test_conductor_runner_writes_host(cfg, ex, tmp_path, monkeypatch):
    monkeypatch.setattr(RP, "resolve_cursor_agent", lambda environ=None: "/bin/cursor-agent")
    monkeypatch.setattr(RP, "create_cursor_chat", lambda exe=None, **k: "chat-uuid")
    monkeypatch.setattr(RP, "with_cursor_agent",
                        lambda argv, env: (["/bin/cursor-agent", *argv[1:]], env))
    r = invoke("conductor-launch", "--runner", "cursor")
    assert r.exit_code == 0, r.output
    assert "runner: cursor" in (tmp_path / "config" / "config.yaml").read_text()


def test_steward_runner_does_not_write_host(cfg, tmp_path):
    r = invoke("steward-launch", "Foo", "--runner", "cursor", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "host:" not in r.output
    assert not (tmp_path / "config" / "config.yaml").exists()


def test_cli_host_backend_writes_config(cfg, tmp_path):
    r = invoke("backend", "--host", "cursor", "--yes")
    assert r.exit_code == 0, r.output
    assert "runner: cursor" in (tmp_path / "config" / "config.yaml").read_text()
    r = invoke("backend", "--host", "cursor")
    assert r.exit_code == 0 and "not written" in r.output
    r = invoke("backend", "--host", "default", "--yes")
    assert r.exit_code == 0, r.output
    assert "runner:" not in (tmp_path / "config" / "config.yaml").read_text()


def test_cli_backend_all_and_campaign(cfg, tmp_path):
    live, dead = "SPEC-0001-live", "SPEC-0001-dead"
    store.create_campaign(cfg, {"id": live, "spec_id": "SPEC-0001", "state": "IMPLEMENTING"})
    store.create_campaign(cfg, {"id": dead, "spec_id": "SPEC-0001", "state": "MERGED"})
    r = invoke("backend", "--all", "cursor", "--yes")
    assert r.exit_code == 0, r.output
    assert store.read_state(cfg, live)["runner"] == "cursor"
    assert "runner" not in store.read_state(cfg, dead)
    other = "SPEC-0002-x"
    store.create_campaign(cfg, {"id": other, "spec_id": "SPEC-0002", "state": "QUEUED"})
    r = invoke("backend", other, "claude", "--yes")
    assert r.exit_code == 0, r.output
    assert store.read_state(cfg, other)["runner"] == "claude"
    r = invoke("backend", live, "cursor", "--all")
    assert r.exit_code != 0
    r = invoke("backend", "--host", "--all", "cursor", "--yes")
    assert r.exit_code == 0, r.output
    assert store.read_state(cfg, other)["runner"] == "cursor"
    assert "runner: cursor" in (tmp_path / "config" / "config.yaml").read_text()


def test_create_cursor_chat_parses_uuid():
    class Rec:
        returncode = 0
        stdout = "02268e29-7401-4047-93ca-48efb917b3f2\n"
        stderr = ""
    assert RP.create_cursor_chat(exe="/bin/cursor-agent", run=lambda *a, **k: Rec()) == (
        "02268e29-7401-4047-93ca-48efb917b3f2")

