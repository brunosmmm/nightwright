"""SPEC-0320: `wt skills install` merges the conductor/steward allow rules into the Claude Code
user settings. ALL tests use a tmp settings path — never the real ~/.claude/settings.json."""
import json

from click.testing import CliRunner

from wt import agent_permissions as AP
from wt.cli import cli


def _read(p):
    return json.loads(p.read_text())


def test_rules_cover_the_conductor_command_groups_and_addressed_forms():
    for group in ("queue", "mailbox", "campaign", "escalations", "idea", "spec"):
        assert f"Bash(wt {group} *)" in AP.ALLOW_RULES
        assert f"Bash(WT_MAILBOX_ADDR=* wt {group} *)" in AP.ALLOW_RULES
    assert len(set(AP.ALLOW_RULES)) == len(AP.ALLOW_RULES)
    assert not any(r.startswith(("Bash(gh", "Bash(git")) for r in AP.ALLOW_RULES)


def test_install_creates_a_missing_file(tmp_path):
    p = tmp_path / "sub" / "settings.json"
    added = AP.install(p)
    assert added == list(AP.ALLOW_RULES)
    assert _read(p) == {"permissions": {"allow": list(AP.ALLOW_RULES)}}


def test_install_merges_and_keeps_everything_else(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {"Stop": []},
                             "permissions": {"allow": ["Bash(ls)", AP.ALLOW_RULES[0]],
                                             "deny": ["Read(.env)"]}}))
    p.chmod(0o600)
    added = AP.install(p)
    doc = _read(p)
    assert added == list(AP.ALLOW_RULES[1:])
    assert doc["model"] == "opus" and doc["hooks"] == {"Stop": []}
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
