"""Capture kinds as labels (SPEC-0044): :KIND:, --kind, filter, show/json, next unchanged."""
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright import report as R
from nightwright.cli import cli
from nightwright.org import load_tasks
from nightwright.workflow import next_step_for_idea


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
        # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
        "specs_dir": str(tmp_path / "specs"),
        "config_dir": str(tmp_path / "config"),
    }


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_add_idea_kind_writes_property(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "broken gate", kind="bug")
    t = load_tasks(cfg)[0]
    assert t.properties.get("KIND") == "bug"
    assert W.idea_kind(t) == "bug"
    assert ":KIND: bug" in open(cfg["org_ideas_file"]).read()


def test_add_idea_omits_kind_defaults_to_idea(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "vague thought")
    t = load_tasks(cfg)[0]
    assert "KIND" not in t.properties
    assert W.idea_kind(t) == "idea"
    assert ":KIND:" not in open(cfg["org_ideas_file"]).read()


def test_normalize_rejects_invalid_kind():
    with pytest.raises(ValueError, match="invalid kind"):
        W.normalize_idea_kind("feature")


def test_add_idea_invalid_kind_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="invalid kind"):
        W.add_idea(cfg, "nope", kind="epic")


def test_set_idea_kind(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "polish later")
    W.set_idea_kind(cfg, "IDEA-001", "improvement")
    t = load_tasks(cfg)[0]
    assert t.properties.get("KIND") == "improvement"
    assert W.idea_kind(t) == "improvement"


def test_idea_row_and_show_include_kind(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "chore item", kind="chore")
    t = load_tasks(cfg)[0]
    row = R.idea_row(cfg, t)
    assert row["kind"] == "chore"
    payload = EX.idea_show_payload(cfg, t)
    assert payload["kind"] == "chore"
    plain = EX.format_idea_show(cfg, t)
    assert "kind: chore" in plain


def test_ideas_filter_by_kind(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "a bug", kind="bug")
    W.add_idea(cfg, "an idea")
    W.add_idea(cfg, "another bug", kind="bug")
    bugs = R.collect_idea_rows(cfg, kind="bug")
    assert [r["heading"] for r in bugs] == ["a bug", "another bug"]
    ideas = R.collect_idea_rows(cfg, kind="idea")
    assert [r["heading"] for r in ideas] == ["an idea"]


def test_next_step_unchanged_by_kind(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "same next")
    t = load_tasks(cfg)[0]
    before = next_step_for_idea(cfg, t)
    W.set_idea_kind(cfg, "IDEA-001", "bug")
    t = load_tasks(cfg)[0]
    after = next_step_for_idea(cfg, t)
    assert before == after == "nw idea log IDEA-001"


def test_cli_capture_kind_and_filter(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    runner = CliRunner()

    r = runner.invoke(cli, ["idea", "flaky CI", "--kind", "bug", "--project", "Meta-Tools"])
    assert r.exit_code == 0, r.output
    assert "IDEA-001" in r.output
    assert ":KIND: bug" in open(cfg["org_ideas_file"]).read()

    bad = runner.invoke(cli, ["idea", "nope", "--kind", "feature"])
    assert bad.exit_code != 0

    listed = runner.invoke(cli, ["ideas", "--kind", "bug", "--json"])
    assert listed.exit_code == 0, listed.output
    import json
    data = json.loads(listed.output)
    assert len(data["ideas"]) == 1
    assert data["ideas"][0]["kind"] == "bug"

    kind_set = runner.invoke(cli, ["idea", "kind", "IDEA-001", "--set", "chore"])
    assert kind_set.exit_code == 0, kind_set.output
    show = runner.invoke(cli, ["idea", "show", "IDEA-001", "--json"])
    assert json.loads(show.output)["kind"] == "chore"
