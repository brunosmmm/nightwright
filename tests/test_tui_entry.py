"""Optional Textual packaging + `wt tui` entry (SPEC-0099).

These tests must pass on a base install (no `tui` extra), so the only thing exercised
unconditionally is the import guard. The launch path is skipped unless Textual is present.
"""
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from wt import tui as T
from wt.cli import cli

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
        "data_dir": str(tmp_path),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


# --- packaging -------------------------------------------------------------------------

def test_textual_is_an_optional_extra_not_a_hard_dep():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert meta["optional-dependencies"]["tui"] == ["textual>=8,<9"]
    assert not any("textual" in d for d in meta["dependencies"])


def test_cli_module_does_not_import_textual_or_the_tui_app():
    """`wt <anything>` must not pull Textual in — cli.py imports wt.tui lazily."""
    import subprocess
    import sys

    code = (
        "import sys; import wt.cli; "
        "assert 'textual' not in sys.modules, 'textual imported eagerly'; "
        "assert 'wt.tui' not in sys.modules, 'wt.tui imported eagerly'; "
        "print('clean')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout


# --- import guard ----------------------------------------------------------------------

def test_run_raises_missing_textual_when_extra_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "textual_available", lambda: False)
    with pytest.raises(T.MissingTextual) as e:
        T.run(_cfg(tmp_path))
    assert "uv sync --extra tui" in str(e.value)


def test_wt_tui_exits_nonzero_with_install_hint(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    monkeypatch.setattr(T, "textual_available", lambda: False)
    r = CliRunner().invoke(cli, ["tui"])
    assert r.exit_code != 0
    assert "optional extra" in r.output
    assert "--extra tui" in r.output


def test_missing_dependency_guard_does_not_swallow_app_bugs(tmp_path, monkeypatch):
    """An ImportError raised *inside* wt.tui.app must surface, not read as 'install Textual'."""
    monkeypatch.setattr(T, "textual_available", lambda: True)

    def boom(*a, **kw):
        raise ImportError("no module named 'somewhere_in_app'")

    monkeypatch.setitem(__import__("sys").modules, "wt.tui.app",
                        type("M", (), {"run_app": staticmethod(boom)})())
    with pytest.raises(ImportError) as e:
        T.run(_cfg(tmp_path))
    assert "somewhere_in_app" in str(e.value)


# --- launch path (needs the extra) ------------------------------------------------------

needs_textual = pytest.mark.skipif(not T.textual_available(),
                                   reason="optional tui extra not installed")


@needs_textual
def test_app_shell_constructs_and_runs_headless(tmp_path):
    from wt.tui.app import IdeaDesk

    app = IdeaDesk(_cfg(tmp_path))

    async def drive():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.is_running

    import asyncio

    asyncio.run(drive())


# --- SPEC-0241: `wt tui --tab` ------------------------------------------------------------

def _capture_run(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    seen = {}
    monkeypatch.setattr(T, "run", lambda c, **kw: seen.update(kw))
    return seen


@pytest.mark.parametrize("args,expect", [([], "ideas"), (["--tab", "queue"], "queue"),
                                         (["--tab", "HUB"], "hub"), (["--tab", "ideas"], "ideas")])
def test_tui_tab_option_is_passed_through(tmp_path, monkeypatch, args, expect):
    seen = _capture_run(monkeypatch, tmp_path)
    r = CliRunner().invoke(cli, ["tui", *args])
    assert r.exit_code == 0, r.output
    assert seen["tab"] == expect


def test_tui_tab_option_rejects_unknown_tab(tmp_path, monkeypatch):
    seen = _capture_run(monkeypatch, tmp_path)
    r = CliRunner().invoke(cli, ["tui", "--tab", "mailbox"])
    assert r.exit_code != 0 and "tab" in r.output and not seen
