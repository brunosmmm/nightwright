"""SPEC-0382: `nw queue conductor-guard` (PreToolUse hook), the persona / help wording and the
nw-conduct / nw-orient rule text the conductor guard depends on."""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright import agent_permissions as AP
from nightwright import doctor as D
from nightwright.agentic import conductor_cost as CC
from nightwright.cli import cli

ROOT = Path(__file__).resolve().parent.parent
CONDUCTOR = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
         "_root": str(tmp_path / "projects")}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: c)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return c


def guard(sid=None, raw=None):
    payload = raw if raw is not None else json.dumps(
        {"session_id": sid, "tool_name": "AskUserQuestion"})
    return CliRunner().invoke(cli, ["queue", "conductor-guard"], input=payload)


def denied(r):
    """Exit 0 with the PreToolUse JSON deny decision on stdout (exit 2 would be a click usage
    error that Claude Code also reads as a block)."""
    if r.exit_code != 0:
        return False
    out = json.loads(r.stdout)["hookSpecificOutput"]
    return (out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
            and "conductor never asks the human" in out["permissionDecisionReason"]
            and "nw queue escalate" in out["permissionDecisionReason"])


def allowed(r):
    return r.exit_code == 0 and not r.stdout.strip()


@pytest.mark.parametrize("source", ["launch", "follower", "manual"])
def test_registered_conductor_is_denied(cfg, source):
    CC.record_session(cfg, "conductor", {}, source=source, session_id=CONDUCTOR)
    assert denied(guard(CONDUCTOR))


def test_registered_via_cli_and_via_follower_env(cfg):
    r = CliRunner().invoke(cli, ["queue", "conductor-session", "add", CONDUCTOR])
    assert r.exit_code == 0, r.output
    assert denied(guard(CONDUCTOR))
    CC.record_session(cfg, "conductor", {"CLAUDE_CODE_SESSION_ID": "from-env"})
    assert denied(guard("from-env"))


def test_other_session_and_unregistered_pass(cfg):
    assert allowed(guard(CONDUCTOR))          # no registry at all
    CC.record_session(cfg, "conductor", {}, source="launch", session_id=CONDUCTOR)
    assert allowed(guard("someone-else"))
    CC.record_session(cfg, "steward:Foo", {}, source="launch", session_id="steward-1")
    assert allowed(guard("steward-1"))        # a steward is not the conductor


def test_session_id_falls_back_to_env(cfg, monkeypatch):
    CC.record_session(cfg, "conductor", {}, source="launch", session_id=CONDUCTOR)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CONDUCTOR)
    assert denied(guard(raw="{}"))
    assert denied(guard(raw=""))


@pytest.mark.parametrize("raw", ["not json", "[]", '{"session_id": 5}', ""])
def test_unreadable_input_fails_open(cfg, raw):
    CC.record_session(cfg, "conductor", {}, source="launch", session_id=CONDUCTOR)
    assert allowed(guard(raw=raw))


def test_unreadable_registry_fails_open(cfg):
    CC.record_session(cfg, "conductor", {}, source="launch", session_id=CONDUCTOR)
    CC._sessions_path(cfg).write_bytes(b"\xff\xfe\x00 not utf-8\n")   # read_text raises
    with pytest.raises(UnicodeDecodeError):
        CC.sessions(cfg)                            # the guard's except is what saves the human
    assert allowed(guard(CONDUCTOR))


def test_registry_read_error_fails_open(cfg, monkeypatch):
    def boom(*a, **k):
        raise OSError("registry gone")
    monkeypatch.setattr(CC, "sessions", boom)
    assert allowed(guard(CONDUCTOR))


# ---- AC4: wording ---------------------------------------------------------------------------

def test_unknown_command_exits_2_but_the_hook_command_does_not_block(tmp_path):
    """An `nw` on PATH without `queue conductor-guard` (older install) fails in click with a
    usage error, exit 2 = Claude Code's block; the hook command maps every failure to 0."""
    r = CliRunner().invoke(cli, ["queue", "conductor-guard-from-the-future"])
    assert r.exit_code == 2                         # what an older nw does with our command
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "nw"
    fake.write_text("#!/bin/sh\necho \"Error: No such command 'conductor-guard'.\" >&2\nexit 2\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    p = subprocess.run(["sh", "-c", AP.HOOK_COMMAND], input="{}", env=env, text=True,
                       capture_output=True)
    assert p.returncode == 0 and not p.stdout.strip()


def test_hook_command_passes_the_deny_decision_through(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "nw"
    fake.write_text("#!/bin/sh\necho '{\"hookSpecificOutput\": {}}'\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    p = subprocess.run(["sh", "-c", AP.HOOK_COMMAND], input="{}", env=env, text=True,
                       capture_output=True)
    assert p.returncode == 0 and "hookSpecificOutput" in p.stdout


def test_no_the_human_must_wording(cfg):
    pdir = ROOT / "src" / "nightwright" / "agentic" / "personas"
    for name in ("gate_triager.md", "conflict_resolver.md", "review_triager.md"):
        text = (pdir / name).read_text(encoding="utf-8").lower()
        assert "the human must" not in text and "a human must" not in text, name
    assert "human-only decision" in (pdir / "gate_triager.md").read_text(encoding="utf-8")
    r = CliRunner().invoke(cli, ["queue", "escalate", "--help"])
    flat = " ".join(r.output.split()).lower()
    assert "the human-only decision needed" in flat
    assert "the human must" not in flat and "a human must" not in flat
    src = (ROOT / "src" / "nightwright" / "agentic" / "cli_queue.py").read_text(encoding="utf-8")
    assert "human must" not in src


# ---- AC5: skill text ------------------------------------------------------------------------

def test_conduct_skill_never_asks_the_human():
    text = (ROOT / "skills" / "nw-conduct" / "SKILL.md").read_text(encoding="utf-8")
    assert "Never call `AskUserQuestion`" in text and "message.human_order" in text
    paras = re.split(r"\n\s*\n", text)
    rule = [p for p in paras if p.startswith("**Never ask the human")]
    assert len(rule) == 1 and 'never "wait for the human"' in " ".join(rule[0].split())
    rest = " ".join(text.replace(rule[0], "").lower().split())
    for phrase in ("tell the human", "unless told", "wait for the human"):
        assert phrase not in rest, phrase


def test_orient_skill_says_conductor_never_asks():
    text = (ROOT / "skills" / "nw-orient" / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "The conductor never does: it routes to `steward:<P>` or `nw queue escalate`." in flat


# ---- AC6: doctor ----------------------------------------------------------------------------

def _doctor_cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "_root": str(tmp_path / "projects")}


def test_doctor_warns_on_unguarded_launch_only(tmp_path):
    cfg = _doctor_cfg(tmp_path)
    CC.record_session(cfg, "conductor", {}, source="launch", session_id="old")   # no deny field
    c = D.check_conductor(cfg)
    assert c["status"] == "warn" and "AskUserQuestion" in c["summary"] and "old" in c["detail"]
    CC.record_session(cfg, "conductor", {}, source="launch", session_id="new",
                      extra={"disallowed_tools": ["Bash(gh pr merge *)", "AskUserQuestion"]})
    assert D.check_conductor(cfg)["status"] == "ok"


def test_doctor_ignores_an_old_launch_behind_a_hand_started_conductor(tmp_path):
    cfg = _doctor_cfg(tmp_path)
    CC.record_session(cfg, "conductor", {}, source="launch", session_id="old")
    CC.record_session(cfg, "conductor", {}, source="follower", session_id="hand")
    assert D.check_conductor(cfg)["status"] == "ok"


def test_doctor_does_not_warn_for_hand_started_conductor(tmp_path):
    cfg = _doctor_cfg(tmp_path)
    CC.record_session(cfg, "conductor", {}, source="follower", session_id="hand")
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="hand2")
    assert D.check_conductor(cfg)["status"] == "ok"
