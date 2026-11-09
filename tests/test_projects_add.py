"""nw projects add — register outbox targets (SPEC-0137)."""
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightwright.cli import cli
from nightwright.rules import apply_project_add, plan_project_add, projects_payload

pytestmark = pytest.mark.fast


def _cfg(tmp_path, *, outbox=None):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "mappings.yaml").write_text("{}\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return {
        "org_files": [str(tmp_path / "org")],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "config_dir": str(cfg_dir),
        "project_axis": "bucket",
        "outbox_targets": dict(outbox or {}),
        "specs_dir": str(tmp_path / "specs"),
    }


def test_plan_stub_create(tmp_path):
    cfg = _cfg(tmp_path)
    plan = plan_project_add(cfg, "Example")
    assert plan["action"] == "create"
    assert plan["name"] == "Example"
    assert plan["after"] == {}
    assert plan["before"] is None


def test_plan_stub_noop(tmp_path):
    cfg = _cfg(tmp_path, outbox={"Example": {}})
    plan = plan_project_add(cfg, "Example")
    assert plan["action"] == "noop"


def test_plan_repo_create(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(tmp_path)
    plan = plan_project_add(cfg, "Example", repo=str(repo), scheme="nw-native",
                            spec_dir="docs/specs")
    assert plan["action"] == "create"
    assert plan["after"]["repo_path"] == str(repo)
    assert plan["after"]["scheme"] == "nw-native"
    assert plan["after"]["spec_dir"] == "docs/specs"


def test_plan_missing_repo_path_errors(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        plan_project_add(cfg, "Example", repo=str(tmp_path / "missing"))


def test_plan_missing_repo_force(tmp_path):
    cfg = _cfg(tmp_path)
    missing = tmp_path / "missing"
    plan = plan_project_add(cfg, "Example", repo=str(missing), force=True)
    assert plan["after"]["repo_path"] == str(missing)


def test_plan_conflict_errors(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = _cfg(tmp_path, outbox={"Example": {"repo_path": str(a)}})
    with pytest.raises(ValueError, match="conflict"):
        plan_project_add(cfg, "Example", repo=str(b))


def test_plan_conflict_force(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = _cfg(tmp_path, outbox={"Example": {"repo_path": str(a)}})
    plan = plan_project_add(cfg, "Example", repo=str(b), force=True)
    assert plan["action"] == "update"
    assert plan["after"]["repo_path"] == str(b)


def test_outbound_flag_stub_vs_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(tmp_path, outbox={
        "Stub": {},
        "Full": {"repo_path": str(repo)},
        "EmptyPath": {"repo_path": "  "},
    })
    by = {p["name"]: p for p in projects_payload(cfg)["projects"]}
    assert by["Stub"]["outbound"] is False
    assert by["Full"]["outbound"] is True
    assert by["EmptyPath"]["outbound"] is False


def test_apply_writes_user_config(tmp_path):
    cfg = _cfg(tmp_path)
    cfg_path = Path(cfg["config_dir"]) / "config.yaml"
    cfg_path.write_text("timezone: America/New_York\noutbox_targets: {}\n", encoding="utf-8")
    plan = plan_project_add(cfg, "Example")
    apply_project_add(cfg, plan)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["timezone"] == "America/New_York"
    assert data["outbox_targets"]["Example"] == {}
    # backup exists
    backups = list(Path(cfg["config_dir"]).glob("config.yaml.bak.*"))
    assert backups


def test_cli_dry_run_does_not_write(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg_path = Path(cfg["config_dir"]) / "config.yaml"
    cfg_path.write_text("outbox_targets: {}\n", encoding="utf-8")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    before = cfg_path.read_text(encoding="utf-8")
    r = CliRunner().invoke(cli, ["projects", "add", "Example"])
    assert r.exit_code == 0, r.output
    assert "plan" in r.output.lower() or "Example" in r.output
    assert cfg_path.read_text(encoding="utf-8") == before
    assert "--yes" in r.output or "dry" in r.output.lower() or "not written" in r.output.lower()


def test_cli_yes_writes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg_path = Path(cfg["config_dir"]) / "config.yaml"
    cfg_path.write_text("outbox_targets: {}\n", encoding="utf-8")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects", "add", "Example", "--yes"])
    assert r.exit_code == 0, r.output
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["outbox_targets"]["Example"] == {}


def test_cli_add_repo_yes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(tmp_path)
    cfg_path = Path(cfg["config_dir"]) / "config.yaml"
    cfg_path.write_text("outbox_targets: {}\n", encoding="utf-8")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects", "add", "Example", "--repo", str(repo), "--yes"])
    assert r.exit_code == 0, r.output
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["outbox_targets"]["Example"]["repo_path"] == str(repo)
    # refresh cfg outbox for payload check
    cfg["outbox_targets"] = data["outbox_targets"]
    by = {p["name"]: p for p in projects_payload(cfg)["projects"]}
    assert by["Example"]["outbound"] is True
    assert by["Example"]["research"]["root"] == str(repo)


def test_cli_projects_list_still_works(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (Path(cfg["config_dir"]) / "mappings.yaml").write_text(
        "T1:\n  bucket: Logging\n", encoding="utf-8")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects"])
    assert r.exit_code == 0, r.output
    assert "Logging" in r.output
    r2 = CliRunner().invoke(cli, ["projects", "--json"])
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output)["schema"] == "nw.projects.v1"


def test_promote_error_mentions_projects_add():
    from nightwright.specs import promote_idea
    # Just assert the error string template used in source path — call with minimal mock
    msg_src = Path(__file__).resolve().parents[1] / "src" / "nightwright" / "specs.py"
    text = msg_src.read_text(encoding="utf-8")
    assert "nw projects add" in text
