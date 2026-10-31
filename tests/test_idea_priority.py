"""Idea priority surfacing parity (SPEC-0093)."""
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright import report as R
from nightwright.cli import cli
from nightwright.org import load_tasks


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


def test_idea_row_and_show_priority(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "hot", priority="A")
    W.add_idea(cfg, "plain")
    rows = {r["heading"]: r for r in R.collect_idea_rows(cfg)}
    assert rows["hot"]["priority"] == "A"
    assert "priority" not in rows["plain"]
    hot = next(t for t in load_tasks(cfg) if t.heading == "hot")
    assert EX.idea_show_payload(cfg, hot)["priority"] == "A"


def test_filter_by_priority(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "a item", priority="A")
    W.add_idea(cfg, "b item", priority="B")
    W.add_idea(cfg, "none")
    assert [t.heading for t in R.collect_idea_tasks(cfg, priority="A")] == ["a item"]


def test_set_idea_priority(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "later")
    W.set_idea_priority(cfg, "IDEA-001", "C")
    t = load_tasks(cfg)[0]
    assert t.priority == "C"
    assert "[#C]" in open(cfg["org_ideas_file"]).read()
    W.set_idea_priority(cfg, "IDEA-001", "A")
    assert load_tasks(cfg)[0].priority == "A"


def test_set_idea_priority_invalid(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "x")
    with pytest.raises(ValueError, match="invalid priority"):
        W.set_idea_priority(cfg, "IDEA-001", "Z")


def test_default_order_still_freshness(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "older high", priority="A")
    W.add_idea(cfg, "newer low", priority="C")
    rows = R.collect_idea_tasks(cfg)
    assert [t.heading for t in rows] == ["newer low", "older high"]


def test_cli_priority_filter_and_mutate(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    runner = CliRunner()
    assert runner.invoke(cli, ["idea", "one", "--priority", "A"]).exit_code == 0
    assert runner.invoke(cli, ["idea", "two", "--priority", "B"]).exit_code == 0
    r = runner.invoke(cli, ["ideas", "--priority", "A", "--json"])
    assert r.exit_code == 0, r.output
    assert "one" in r.output and "two" not in r.output
    r = runner.invoke(cli, ["idea", "priority", "IDEA-002", "--set", "A"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["ideas", "--priority", "A", "--json"])
    assert "one" in r.output and "two" in r.output
