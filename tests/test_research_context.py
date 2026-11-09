"""SPEC-0042: resolve_research_context + idea show research block."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright import rules as R
from nightwright.cli import cli
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path, *, outbox_targets=None):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED",
        ],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
        "specs_dir": str(tmp_path / "specs"),
        "config_dir": str(tmp_path / "config"),
        "outbox_dir": str(tmp_path / "outbox"),
        "outbox_targets": outbox_targets or {},
        "project_axis": "bucket",
    }


def test_resolve_outbox_targets():
    cfg = {
        "outbox_targets": {
            "PFW-Intelligence": {
                "repo_path": "~/work/demo-project",
                "spec_dir": "docs/specs",
                "scheme": "nw-native",
            },
        },
    }
    ctx = R.resolve_research_context(cfg, "PFW-Intelligence")
    assert ctx["source"] == "outbox_targets"
    assert ctx["root"].endswith("work/demo-project")
    assert "~" not in ctx["root"]
    assert ctx["spec_dir"] == "docs/specs"
    assert ctx["scheme"] == "nw-native"


def test_resolve_meta_tools_internal():
    ctx = R.resolve_research_context({}, "Meta-Tools")
    assert ctx["source"] == "internal"
    assert ctx["root"] is not None
    assert (Path(ctx["root"]) / "src" / "nightwright").is_dir()
    assert (Path(ctx["root"]) / "pyproject.toml").is_file()


def test_resolve_unconfigured_project_no_guess():
    ctx = R.resolve_research_context({"outbox_targets": {}}, "DemoCloud")
    assert ctx["root"] is None
    assert ctx["source"] == "none"
    assert "outbox_targets" in ctx["note"]
    assert "DemoCloud" in ctx["note"]


def test_resolve_no_project():
    ctx = R.resolve_research_context({}, None)
    assert ctx["root"] is None
    assert ctx["source"] == "none"
    assert "PROJECT" in ctx["note"]


def test_idea_show_json_includes_research(tmp_path, monkeypatch):
    target = tmp_path / "pfw"
    target.mkdir()
    cfg = _cfg(tmp_path, outbox_targets={
        "PFW-Intelligence": {"repo_path": str(target), "spec_dir": "docs/specs"},
    })
    W.add_idea(cfg, "alerts epic", state="INCUBATE", project="PFW-Intelligence")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)

    r = CliRunner().invoke(cli, ["idea", "show", "IDEA-001", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["research"]["source"] == "outbox_targets"
    assert data["research"]["root"] == str(target)
    assert data["research"]["spec_dir"] == "docs/specs"


def test_idea_show_plain_research_line(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={
        "Example": {"repo_path": "/tmp/example-ats"},
    })
    W.add_idea(cfg, "plain research", state="INCUBATE", project="Example")
    from nightwright.org import load_tasks
    task = next(t for t in load_tasks(cfg) if t.properties.get("ID") == "IDEA-001")
    text = EX.format_idea_show(cfg, task)
    assert "research: /tmp/example-ats (outbox_targets)" in text


def test_discover_nw_checkout_matches_repo():
    root = R.discover_nw_checkout()
    assert root is not None
    assert Path(root) == ROOT.resolve() or (Path(root) / "src" / "nightwright").is_dir()


