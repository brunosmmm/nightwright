"""Curated :WORKSTREAM: on ideas and tasks (SPEC-0090)."""
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright import report as R
from nightwright.cli import cli
from nightwright.org import filter_tasks, load_tasks

pytestmark = pytest.mark.fast


def _cfg(tmp_path, workstreams=("ops", "platform")):
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
        "workstreams": list(workstreams),
        "project_axis": "bucket",
    }


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_default_config_workstreams_empty():
    from nightwright.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["workstreams"] == []


def test_normalize_hard_fail_empty_config(tmp_path):
    cfg = _cfg(tmp_path, workstreams=())
    with pytest.raises(ValueError, match="none configured"):
        W.normalize_workstream(cfg, "ops")


def test_normalize_hard_fail_unknown(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="invalid workstream"):
        W.normalize_workstream(cfg, "unknown")


def test_add_idea_writes_workstream(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "lane thought", workstream="ops")
    t = load_tasks(cfg)[0]
    assert t.properties.get("WORKSTREAM") == "ops"
    assert ":WORKSTREAM: ops" in open(cfg["org_ideas_file"]).read()


def test_add_task_writes_workstream(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "ops chore", workstream="platform")
    t = next(x for x in load_tasks(cfg) if not x.is_idea)
    assert t.properties.get("WORKSTREAM") == "platform"


def test_add_idea_invalid_workstream_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="invalid workstream"):
        W.add_idea(cfg, "nope", workstream="bogus")


def test_set_idea_workstream(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "later")
    W.set_idea_workstream(cfg, "IDEA-001", "platform")
    t = load_tasks(cfg)[0]
    assert t.properties.get("WORKSTREAM") == "platform"


def test_filter_ideas_and_tasks_by_workstream(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "ops idea", workstream="ops")
    W.add_idea(cfg, "plat idea", workstream="platform")
    W.add_task(cfg, "ops task", workstream="ops")
    W.add_task(cfg, "plat task", workstream="platform")
    ideas = R.collect_idea_tasks(cfg, workstream="ops")
    assert [t.heading for t in ideas] == ["ops idea"]
    tasks = filter_tasks(load_tasks(cfg), is_idea=False, workstream="ops")
    assert [t.heading for t in tasks] == ["ops task"]


def test_idea_row_and_show_omit_empty_workstream(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "no ws")
    W.add_idea(cfg, "has ws", workstream="ops")
    rows = {r["heading"]: r for r in R.collect_idea_rows(cfg)}
    assert "workstream" not in rows["no ws"]
    assert rows["has ws"]["workstream"] == "ops"
    bare = next(t for t in load_tasks(cfg) if t.heading == "no ws")
    assert "workstream" not in EX.idea_show_payload(cfg, bare)
    tagged = next(t for t in load_tasks(cfg) if t.heading == "has ws")
    assert EX.idea_show_payload(cfg, tagged)["workstream"] == "ops"


def test_project_ai_workstreams_independent(tmp_path):
    """Project AI-workstreams is not a workstream facet."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "under ai ws project", project="AI-workstreams", workstream="ops")
    t = load_tasks(cfg)[0]
    assert t.project == "AI-workstreams"
    assert t.properties.get("WORKSTREAM") == "ops"
    assert t.properties.get("PROJECT") == "AI-workstreams"


def test_cli_idea_workstream_and_filters(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    runner = CliRunner()
    r = runner.invoke(cli, ["idea", "capture me", "--workstream", "ops"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["ideas", "--workstream", "ops", "--json"])
    assert r.exit_code == 0, r.output
    assert "capture me" in r.output
    assert '"workstream": "ops"' in r.output
    r = runner.invoke(cli, ["idea", "workstream", "IDEA-001", "--set", "platform"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["ideas", "--workstream", "platform", "--json"])
    assert "capture me" in r.output
    r = runner.invoke(cli, ["add", "tasky", "--workstream", "ops"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["tasks", "--workstream", "ops"])
    assert r.exit_code == 0, r.output
    assert "tasky" in r.output


def test_cli_unknown_workstream_fails(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    runner = CliRunner()
    r = runner.invoke(cli, ["idea", "x", "--workstream", "nope"])
    assert r.exit_code != 0
    assert "invalid workstream" in r.output
