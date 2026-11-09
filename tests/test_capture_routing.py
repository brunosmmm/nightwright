"""Capture routing soft warn for --kind bug (SPEC-0091)."""
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright.cli import cli
from nightwright.org import load_tasks
import pytest

pytestmark = pytest.mark.fast


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "config_dir": str(tmp_path / "config"),
        "workstreams": [],
        "project_axis": "bucket",
    }


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_kind_bug_warns_and_still_creates(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, [
        "idea", "flaky gate", "--kind", "bug", "--project", "Meta-Tools", "--tag", "ci",
    ])
    assert r.exit_code == 0, r.output
    assert "prefer a task" in r.output.lower() or "nw add" in r.output
    assert "nw add" in r.output
    assert "flaky gate" in r.output
    assert "--project" in r.output and "Meta-Tools" in r.output
    assert "--tag" in r.output
    tasks = load_tasks(cfg)
    assert len(tasks) == 1 and tasks[0].is_idea
    assert tasks[0].properties.get("KIND") == "bug"


def test_force_idea_suppresses_warn(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, [
        "idea", "design bug", "--kind", "bug", "--force-idea",
    ])
    assert r.exit_code == 0, r.output
    assert "prefer a task" not in r.output.lower()
    assert "`--kind bug` still writes" not in r.output
    assert load_tasks(cfg)[0].properties.get("KIND") == "bug"


def test_other_kinds_silent(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    for kind in ("improvement", "chore", "idea"):
        r = CliRunner().invoke(cli, ["idea", f"x-{kind}", "--kind", kind])
        assert r.exit_code == 0, r.output
        assert "prefer a task" not in r.output.lower()


