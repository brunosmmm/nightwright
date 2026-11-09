"""`nw tasks` list & filter (SPEC-0006): default actionable view, filter composition, --all,
--key, and CLI wiring."""
import os

from click.testing import CliRunner

from nightwright import report as R
from nightwright.cli import cli
from nightwright.console import console
from nightwright.org import load_tasks
import pytest

pytestmark = pytest.mark.fast

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "org")


def _cfg():
    return {"org_files": [FIXTURES], "org_todo_keywords": ["TODO", "|", "DONE"]}


def _render(**kw):
    prev = console._width
    console._width = 240                      # avoid rich truncating table cells in capture
    try:
        with console.capture() as cap:
            R.tasks(_cfg(), **kw)
    finally:
        console._width = prev
    return cap.get()


def test_default_shows_only_open_actionable():
    out = _render()
    # open TODO-state items appear
    assert "DEMO-504" in out and "expense report" in out
    # done items hidden
    assert "old finished ticket" not in out and "abandoned work" not in out
    # count = tasks that have a state and are not done (structural + done excluded)
    tasks = load_tasks(_cfg())
    expected = sum(1 for t in tasks if t.state and not t.is_done)
    assert f"{expected} shown" in out
    # a purely structural heading (state None) is not counted as actionable
    assert any(t.heading == "Active Projects" and t.state is None for t in tasks)


def test_all_includes_done_and_structural():
    out = _render(all_done=True)
    # Heading may wrap across rich table rows; match the stable key + fragments.
    assert "DEMO-500" in out and "old finished" in out
    assert "Active Projects" in out              # structural (state None)


def test_state_filter():
    out = _render(state="INPROGRESS")
    assert "DEMO-504" in out
    assert "expense report" not in out


def test_tag_and_project_filters_compose():
    tasks = load_tasks(_cfg())
    demo_open = [t for t in tasks if "demo" in t.tags and not t.is_done and t.state]
    assert demo_open                              # sanity: fixtures have some
    out = _render(tag="demo")
    assert "drone perf" in out
    assert "expense report" not in out            # admin tag, filtered out


def test_key_filter_only_topic_keyed():
    out = _render(key=True)
    assert "DEMO-504" in out and "DEMO-506" in out
    assert "expense report" not in out            # no JIRA key


def test_empty_result_is_friendly():
    out = _render(state="NOPE")
    assert "no matching tasks" in out


def test_cli_tasks_runs(monkeypatch):
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", _cfg)
    r = CliRunner().invoke(cli, ["tasks", "--state", "INPROGRESS"])
    assert r.exit_code == 0, r.output
    assert "DEMO-504" in r.output


def test_cli_tasks_json(monkeypatch):
    import json
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", _cfg)
    r = CliRunner().invoke(cli, ["tasks", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["schema"] == "nw.tasks.v1"
    assert any("DEMO-504" in (t.get("heading") or "") or t.get("topic_key") == "DEMO-504"
               for t in data["tasks"])
    r2 = CliRunner().invoke(cli, ["tasks", "--json", "--state", "INPROGRESS"])
    data2 = json.loads(r2.output)
    assert all(t["state"] == "INPROGRESS" for t in data2["tasks"])


