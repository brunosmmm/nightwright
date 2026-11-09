"""CLI --json on ideas / next / idea show (SPEC-0026)."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import org_write as W
from nightwright.cli import cli
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        (specs / name).write_text((ROOT / "docs" / "specs" / name).read_text())
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
        "config_dir": str(tmp_path / "config"),
        "outbox_dir": str(tmp_path / "outbox"),
        "project_axis": "bucket",
    }


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_ideas_json_envelope(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "json list me", state="IDEA", tags=("cli",), project="Meta-Tools")
    _patch(monkeypatch, cfg)

    r = CliRunner().invoke(cli, ["ideas", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["schema"] == "nw.ideas.v1"
    assert len(data["ideas"]) == 1
    row = data["ideas"][0]
    assert row["id"] == "IDEA-001"
    assert row["state"] == "IDEA"
    assert row["heading"] == "json list me"
    assert row["tags"] == ["cli"]
    assert row["project"] == "Meta-Tools"
    assert "next" in row and row["next"]
    assert "spec" not in row  # omitted when empty
    assert "Ideas" not in r.output  # no Rich panel title in JSON path


def test_next_json_envelope(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "next json", state="INCUBATE")
    _patch(monkeypatch, cfg)

    r = CliRunner().invoke(cli, ["next", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["schema"] == "nw.next.v1"
    assert data["ideas"][0]["id"] == "IDEA-001"
    assert "next" in data["ideas"][0]
    assert "Next" not in r.output


def test_idea_show_json_envelope(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "show json", state="INCUBATE", project="Meta-Tools")
    from nightwright import explore as EX
    EX.set_summary(cfg, "IDEA-001", "a summary")
    EX.set_questions(cfg, "IDEA-001", "- q1")
    EX.append_log(cfg, "IDEA-001", "a finding")
    _patch(monkeypatch, cfg)

    r = CliRunner().invoke(cli, ["idea", "show", "IDEA-001", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["schema"] == "nw.idea.v1"
    assert data["id"] == "IDEA-001"
    assert data["project"] == "Meta-Tools"
    assert data["summary"] == "a summary"
    assert "q1" in data["questions"]
    assert "a finding" in data["log"]
    assert "## Summary" not in r.output
    assert data["research"]["source"] == "internal"
    assert data["research"]["root"]
    from pathlib import Path
    assert (Path(data["research"]["root"]) / "src" / "nightwright").is_dir()


def test_ideas_default_still_rich(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "human table", state="IDEA")
    _patch(monkeypatch, cfg)

    r = CliRunner().invoke(cli, ["ideas"])
    assert r.exit_code == 0, r.output
    assert "Ideas" in r.output
    assert "IDEA-001" in r.output
    try:
        json.loads(r.output)
        assert False, "default ideas output should not be pure JSON"
    except json.JSONDecodeError:
        pass


def test_help_documents_json():
    for args in (["ideas", "--help"], ["next", "--help"], ["idea", "show", "--help"]):
        r = CliRunner().invoke(cli, args)
        assert r.exit_code == 0
        assert "--json" in r.output


