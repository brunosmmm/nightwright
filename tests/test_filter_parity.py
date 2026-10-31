"""Filter parity for nw next / hub / agenda (SPEC-0096)."""
import datetime as dt
import json
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import org_write as W
from nightwright import report as R
from nightwright.cli import cli


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
        "outbox_dir": str(tmp_path / "outbox"),
        "outbox_targets": {},
        "workstreams": ["ops"],
        "hub_noise_states": ["CATEGORIZE"],
        "hub_tasks_cap": 40,
        "project_axis": "bucket",
    }


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_next_filters_by_project_and_kind(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "meta one", project="Meta-Tools", kind="bug")
    W.add_idea(cfg, "example one", project="Example", kind="idea")
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["next", "--project", "Meta-Tools", "--kind", "bug", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert [i["heading"] for i in data["ideas"]] == ["meta one"]


def test_hub_filters_ideas_not_specs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "0001-x.md").write_text(
        "---\nid: SPEC-0001\ntitle: \"X\"\nstatus: draft\nkind: feature\n"
        "milestone: M1\ncreated: 2026-07-01\nupdated: 2026-07-01\n---\n\n## S\n\nx\n",
        encoding="utf-8")
    W.add_idea(cfg, "keep", project="Meta-Tools")
    W.add_idea(cfg, "drop", project="Example")
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["hub", "--json", "--project", "Meta-Tools",
                                 "--tasks-slice", "none"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert {i["heading"] for i in data["ideas"]} == {"keep"}
    assert len(data["internal"]) == 1


def test_agenda_filters_project(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    today = dt.datetime.now(cfg["_tz"]).date().isoformat()
    W.add_task(cfg, "meta task", project="Meta-Tools", scheduled=today)
    W.add_task(cfg, "other task", project="Example", scheduled=today)
    _patch(monkeypatch, cfg)
    # Render via report capture
    from nightwright.console import console
    prev_w, prev_h = console._width, console._height
    console._width, console._height = 120, 40
    try:
        with console.capture() as cap:
            R.agenda(cfg, day=today, project="Meta-Tools")
    finally:
        console._width, console._height = prev_w, prev_h
    out = cap.get()
    assert "meta task" in out
    assert "other task" not in out
