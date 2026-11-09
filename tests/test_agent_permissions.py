"""SPEC-0320: `nw skills install` merges the conductor/steward allow rules into the Claude Code
user settings. ALL tests use a tmp settings path — never the real ~/.claude/settings.json."""
import json

import pytest

from click.testing import CliRunner

from nightwright import agent_permissions as AP
from nightwright.cli import cli

pytestmark = pytest.mark.fast


def _read(p):
    return json.loads(p.read_text())


def test_rules_cover_the_conductor_command_groups_and_addressed_forms():
    for group in ("queue", "mailbox", "campaign", "escalations", "idea", "spec"):
        assert f"Bash(nw {group} *)" in AP.ALLOW_RULES
        assert f"Bash(NW_MAILBOX_ADDR=* nw {group} *)" in AP.ALLOW_RULES
    assert len(set(AP.ALLOW_RULES)) == len(AP.ALLOW_RULES)
    assert not any(r.startswith(("Bash(gh", "Bash(git")) for r in AP.ALLOW_RULES)


def test_install_creates_a_missing_file(tmp_path):
    p = tmp_path / "sub" / "settings.json"
    added = AP.install(p)
    assert added == list(AP.ALLOW_RULES)
    assert _read(p) == {"permissions": {"allow": list(AP.ALLOW_RULES)},
                        "hooks": {"PreToolUse": [AP.HOOK_ENTRY]}}


def test_install_merges_and_keeps_everything_else(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {"Stop": []},
                             "permissions": {"allow": ["Bash(ls)", AP.ALLOW_RULES[0]],
                                             "deny": ["Read(.env)"]}}))
    p.chmod(0o600)
    added = AP.install(p)
    doc = _read(p)
    assert added == list(AP.ALLOW_RULES[1:])
    assert doc["model"] == "opus" and doc["hooks"] == {"Stop": [], "PreToolUse": [AP.HOOK_ENTRY]}
    assert doc["permissions"]["deny"] == ["Read(.env)"]
    assert doc["permissions"]["allow"][:2] == ["Bash(ls)", AP.ALLOW_RULES[0]]
    assert doc["permissions"]["allow"].count(AP.ALLOW_RULES[0]) == 1
    assert (p.stat().st_mode & 0o777) == 0o600


def test_install_is_idempotent_and_leaves_file_untouched(tmp_path):
    p = tmp_path / "settings.json"
    AP.install(p)
    before = p.read_bytes()
    assert AP.install(p) == []
    assert AP.missing(p) == []
    assert p.read_bytes() == before


def test_invalid_json_is_refused_not_rewritten(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    try:
        AP.install(p)
    except ValueError as e:
        assert "not valid JSON" in str(e)
    else:
        raise AssertionError("expected ValueError")
    assert p.read_text() == "{not json"


def test_cli_install_with_settings_writes_rules(tmp_path):
    dest, settings = tmp_path / "skills", tmp_path / "settings.json"
    r = CliRunner().invoke(cli, ["skills", "install", "--dest", str(dest),
                                 "--settings", str(settings)])
    assert r.exit_code == 0, r.output
    assert "allow rules" in r.output
    assert AP.missing(settings) == []
    r = CliRunner().invoke(cli, ["skills", "list", "--dest", str(dest), "--settings", str(settings)])
    assert r.exit_code == 0 and "allow rules  present" in r.output


def test_cli_install_with_dest_only_never_touches_settings(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(AP, "install", lambda *a, **k: calls.append(a) or [])
    monkeypatch.setattr(AP, "missing", lambda *a, **k: calls.append(a) or [])
    for args in (["skills", "install", "--dest", str(tmp_path / "s")],
                 ["skills", "list", "--dest", str(tmp_path / "s")]):
        r = CliRunner().invoke(cli, args)
        assert r.exit_code == 0, r.output
    assert calls == []


def test_cli_no_permissions_skips_rules(tmp_path):
    settings = tmp_path / "settings.json"
    r = CliRunner().invoke(cli, ["skills", "install", "--dest", str(tmp_path / "s"),
                                 "--settings", str(settings), "--no-permissions"])
    assert r.exit_code == 0, r.output
    assert not settings.exists()


# SPEC-0382: the conductor-guard PreToolUse hook
def test_hook_entry_is_the_askuserquestion_guard():
    assert AP.HOOK_ENTRY == {"matcher": "AskUserQuestion", "hooks": [
        {"type": "command", "command": "nw queue conductor-guard || true"}]}


def test_hook_installed_once_and_unrelated_hooks_kept(tmp_path):
    p = tmp_path / "settings.json"
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    p.write_text(json.dumps({"hooks": {"PreToolUse": [other], "Stop": [{"hooks": []}]}}))
    assert AP.hook_missing(p)
    assert AP.sync(p)["hook"] is True
    doc = _read(p)
    assert doc["hooks"]["PreToolUse"] == [other, AP.HOOK_ENTRY]
    assert doc["hooks"]["Stop"] == [{"hooks": []}]
    before = p.read_text()
    res = AP.sync(p)
    assert res == {"added": [], "removed": [], "hook": False}
    assert p.read_text() == before and not AP.hook_missing(p)
    assert _read(p)["hooks"]["PreToolUse"].count(AP.HOOK_ENTRY) == 1


def test_null_hooks_value_is_treated_as_empty(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": None}))
    assert AP.sync(p)["hook"] is True
    assert _read(p)["hooks"] == {"PreToolUse": [AP.HOOK_ENTRY]}


def test_hook_added_when_rules_already_present(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"permissions": {"allow": list(AP.ALLOW_RULES)}}))
    res = AP.sync(p)
    assert res["added"] == [] and res["hook"] is True
    assert _read(p)["permissions"]["allow"] == list(AP.ALLOW_RULES)


def test_install_command_reports_hook_and_is_idempotent(tmp_path):
    p = tmp_path / "settings.json"
    args = ["skills", "install", "--dest", str(tmp_path / "d"), "--settings", str(p)]
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == 0 and "conductor-guard hook" in r.output, r.output
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == 0 and "up-to-date" in r.output
    assert _read(p)["hooks"]["PreToolUse"] == [AP.HOOK_ENTRY]


def test_malformed_hooks_value_is_an_error_not_a_rewrite(tmp_path):
    import pytest
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"PreToolUse": "x"}}))
    with pytest.raises(ValueError):
        AP.sync(p)
    assert _read(p) == {"hooks": {"PreToolUse": "x"}}


@pytest.mark.parametrize("bad", [[], "x", 0, False, ""])
def test_non_dict_hooks_value_is_an_error_not_a_rewrite(tmp_path, bad):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": bad}))
    with pytest.raises(ValueError):
        AP.sync(p)
    assert _read(p) == {"hooks": bad}

