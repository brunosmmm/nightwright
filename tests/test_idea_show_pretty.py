"""Pretty nw idea show via Rich + Pygments (SPEC-0035)."""
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from rich.console import Console

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright.cli import cli
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "config_dir": str(tmp_path / "config"),
        "outbox_dir": str(tmp_path / "outbox"),
    }


def test_idea_show_use_pretty_gates():
    assert EX.idea_show_use_pretty(plain=False, as_json=False, is_terminal=True) is True
    assert EX.idea_show_use_pretty(plain=True, is_terminal=True) is False
    assert EX.idea_show_use_pretty(as_json=True, is_terminal=True) is False
    assert EX.idea_show_use_pretty(plain=False, is_terminal=False) is False


def test_print_idea_show_plain_matches_format(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "pretty target")
    from nightwright.org_write import resolve_selector
    task = resolve_selector(cfg, "IDEA-001")
    EX.append_log(cfg, "IDEA-001", note="finding with ~code~")
    expected = EX.format_idea_show(cfg, resolve_selector(cfg, "IDEA-001"))

    EX.print_idea_show(cfg, resolve_selector(cfg, "IDEA-001"), plain=True)
    assert capsys.readouterr().out == expected


def test_idea_show_theme_default_and_blank():
    assert EX.idea_show_theme({}) == "nord"
    assert EX.idea_show_theme({"idea_show_theme": None}) == "nord"
    assert EX.idea_show_theme({"idea_show_theme": "  "}) == "nord"
    assert EX.idea_show_theme({"idea_show_theme": "monokai"}) == "monokai"


def test_print_idea_show_pretty_uses_cfg_theme(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg["idea_show_theme"] = "friendly"
    W.add_idea(cfg, "pretty target")
    from nightwright.org_write import resolve_selector
    task = resolve_selector(cfg, "IDEA-001")

    captured = {}

    class _FakeSyntax:
        def __init__(self, code, lexer, theme="monokai", **kwargs):
            captured["theme"] = theme
            captured["lexer"] = lexer

    monkeypatch.setattr("rich.syntax.Syntax", _FakeSyntax)
    c = Console(force_terminal=True, record=True)
    monkeypatch.setattr(c, "print", lambda renderable: captured.setdefault("printed", renderable))
    EX.print_idea_show(cfg, task, plain=False, console=c)
    assert captured.get("theme") == "friendly"
    assert captured.get("lexer") == "org"


def test_print_idea_show_pretty_defaults_to_nord(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)  # no idea_show_theme key
    W.add_idea(cfg, "pretty target")
    from nightwright.org_write import resolve_selector
    task = resolve_selector(cfg, "IDEA-001")

    captured = {}

    class _FakeSyntax:
        def __init__(self, code, lexer, theme="monokai", **kwargs):
            captured["theme"] = theme
            captured["background_color"] = kwargs.get("background_color")

    monkeypatch.setattr("rich.syntax.Syntax", _FakeSyntax)
    c = Console(force_terminal=True, record=True)
    monkeypatch.setattr(c, "print", lambda renderable: None)
    EX.print_idea_show(cfg, task, plain=False, console=c)
    assert captured.get("theme") == "nord"
    assert captured.get("background_color") == "default"


def test_cli_idea_show_plain_and_json(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "cli show")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)

    r_plain = CliRunner().invoke(cli, ["idea", "show", "IDEA-001", "--plain"])
    assert r_plain.exit_code == 0, r_plain.output
    assert "** Summary" in r_plain.output
    assert "\x1b[" not in r_plain.output

    r_json = CliRunner().invoke(cli, ["idea", "show", "IDEA-001", "--json"])
    assert r_json.exit_code == 0, r_json.output
    assert '"schema": "nw.idea.v1"' in r_json.output


def test_cli_idea_show_help_mentions_plain():
    r = CliRunner().invoke(cli, ["idea", "show", "--help"])
    assert r.exit_code == 0
    assert "--plain" in r.output


