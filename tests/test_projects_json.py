"""nw projects --json agent map (SPEC-0095)."""
import json
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright.cli import cli
from nightwright.rules import projects_payload


def _cfg(tmp_path, *, with_outbox=True):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "mappings.yaml").write_text(
        "DEMO-1:\n  bucket: Example\nDEMO-2:\n  bucket: Logging\n", encoding="utf-8")
    repo = tmp_path / "example-repo"
    repo.mkdir()
    outbox = {}
    if with_outbox:
        outbox = {
            "Example": {"repo_path": str(repo), "spec_dir": "docs/specs", "scheme": "nw-native"},
            "PFW-Intelligence": {"repo_path": str(tmp_path / "pfw"), "spec_dir": "docs/specs"},
        }
        (tmp_path / "pfw").mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "config_dir": str(cfg_dir),
        "project_axis": "bucket",
        "outbox_targets": outbox,
        "specs_dir": str(tmp_path / "specs"),
    }


def test_projects_payload_union_and_research(tmp_path):
    cfg = _cfg(tmp_path)
    payload = projects_payload(cfg)
    assert payload["schema"] == "nw.projects.v1"
    by_name = {p["name"]: p for p in payload["projects"]}
    assert "Meta-Tools" in by_name
    assert by_name["Example"]["outbound"] is True
    assert by_name["Example"]["topics"] == 1
    assert by_name["Example"]["research"]["source"] == "outbox_targets"
    assert by_name["Example"]["research"]["root"] == str(tmp_path / "example-repo")
    assert by_name["PFW-Intelligence"]["outbound"] is True
    assert by_name["PFW-Intelligence"]["topics"] == 0
    assert by_name["Logging"]["outbound"] is False
    assert by_name["Logging"]["topics"] == 1


def test_cli_projects_json(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["schema"] == "nw.projects.v1"
    assert {p["name"] for p in data["projects"]} >= {"Example", "Logging", "Meta-Tools",
                                                       "PFW-Intelligence"}
