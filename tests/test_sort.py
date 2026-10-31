"""Optional --sort on nw ideas / nw tasks (SPEC-0094)."""
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import explore as EX
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
        "workstreams": [],
        "project_axis": "bucket",
    }


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_ideas_default_freshness_and_priority_sort(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "older A", priority="A")
    W.add_idea(cfg, "newer C", priority="C")
    default = [t.heading for t in R.collect_idea_tasks(cfg)]
    assert default == ["newer C", "older A"]
    by_pri = [t.heading for t in R.collect_idea_tasks(cfg, sort="priority")]
    assert by_pri == ["older A", "newer C"]
    desc = [t.heading for t in R.collect_idea_tasks(cfg, sort="priority", desc=True)]
    assert desc == ["newer C", "older A"]


def test_tasks_sort_priority(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "low", priority="C")
    W.add_task(cfg, "high", priority="A")
    W.add_task(cfg, "mid", priority="B")
    # Capture stdout via tasks as_json
    import io
    import json
    import sys
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        R.tasks(cfg, as_json=True, sort="priority")
    finally:
        sys.stdout = old
    data = json.loads(buf.getvalue())
    assert [t["heading"] for t in data["tasks"]] == ["high", "mid", "low"]


def test_query_ignores_sort(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "alpha needle", priority="C")
    EX.append_log(cfg, "IDEA-001", "needle detail")
    W.add_idea(cfg, "beta needle", priority="A")
    EX.append_log(cfg, "IDEA-002", "needle detail")
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, [
        "ideas", "--query", "needle", "--sort", "priority", "--json",
    ])
    assert r.exit_code == 0, r.output
    import json
    data = json.loads(r.output)
    # relevance-first: both match; sort must not force A before C
    heads = [i["heading"] for i in data["ideas"]]
    assert set(heads) == {"alpha needle", "beta needle"}
    # If sort applied, beta (A) would be first; score order should match without sort
    r2 = CliRunner().invoke(cli, ["ideas", "--query", "needle", "--json"])
    heads2 = [i["heading"] for i in json.loads(r2.output)["ideas"]]
    assert heads == heads2


def test_cli_ideas_sort(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    W.add_idea(cfg, "a", priority="A")
    W.add_idea(cfg, "c", priority="C")
    r = CliRunner().invoke(cli, ["ideas", "--sort", "priority", "--json"])
    assert r.exit_code == 0, r.output
    import json
    heads = [i["heading"] for i in json.loads(r.output)["ideas"]]
    assert heads == ["a", "c"]
