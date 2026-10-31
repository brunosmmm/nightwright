"""nw projects rename — outbox bucket migrate (SPEC-0159)."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from click.testing import CliRunner

from nightwright.cli import cli
from nightwright.idea_ext import read_idea_extensions
from nightwright.org import filter_tasks, invalidate_tasks_cache, load_tasks
from nightwright.outbox_migrate import apply_project_rename, plan_project_rename
from nightwright.specmeta import split_frontmatter


def _cfg(tmp_path, *, outbox=None, idea_extensions=None):
    org = tmp_path / "org"
    org.mkdir(exist_ok=True)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "mappings.yaml").write_text("{}\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "config_dir": str(cfg_dir),
        "project_axis": "bucket",
        "outbox_targets": dict(outbox or {}),
        "idea_extensions": dict(idea_extensions or {}),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


def _seed_idea(org_dir: Path, *, project="DemoCo") -> str:
    ideas = org_dir / "ideas.org"
    ideas.write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED DROPPED\n\n"
        f"* IDEA seed idea\n"
        f"  :PROPERTIES:\n"
        f"  :ID: IDEA-001\n"
        f"  :PROJECT: {project}\n"
        f"  :END:\n"
        f"** Ext {project}\n"
        f"   :PROPERTIES:\n"
        f"   :TICKET: TF-9\n"
        f"   :END:\n",
        encoding="utf-8",
    )
    return "IDEA-001"


def _seed_outbox(data: Path, project: str, *, outbound_id="DEMO-0001") -> Path:
    d = data / "outbox" / project
    d.mkdir(parents=True)
    path = d / f"{outbound_id}-demo.md"
    path.write_text(
        "---\n"
        f"id: {outbound_id}\n"
        "title: Demo\n"
        "status: draft\n"
        "owner: test\n"
        "created: 2026-09-09\n"
        f"target_project: {project}\n"
        "target_repo: /tmp/repo\n"
        "target_spec_path: /tmp/repo/docs/specs/DEMO-0001-demo.md\n"
        "---\n\n# Demo\n",
        encoding="utf-8",
    )
    return path


def test_plan_dry_run_pure(tmp_path):
    cfg = _cfg(tmp_path, outbox={"DemoCo": {"repo_path": "/tmp/tf"}})
    cfg_path = Path(cfg["config_dir"]) / "config.yaml"
    cfg_path.write_text("outbox_targets:\n  DemoCo:\n    repo_path: /tmp/tf\n", encoding="utf-8")
    before = cfg_path.read_text(encoding="utf-8")
    _seed_idea(Path(cfg["org_files"][0]))
    _seed_outbox(Path(cfg["data_dir"]), "DemoCo")
    plan = plan_project_rename(cfg, "DemoCo", "DemoLabs")
    assert plan["action"] == "rename"
    assert plan["idea_count"] == 1
    assert plan["ext_headline_hits"] == 1
    assert plan["outbox_exists"] is True
    assert any("mint prefix" in w for w in plan["warnings"])
    assert cfg_path.read_text(encoding="utf-8") == before
    assert (Path(cfg["data_dir"]) / "outbox" / "DemoCo").is_dir()
    assert not (Path(cfg["data_dir"]) / "outbox" / "DemoLabs").exists()


def test_apply_happy_path(tmp_path):
    repo = "/tmp/tf"
    cfg = _cfg(
        tmp_path,
        outbox={"DemoCo": {"repo_path": repo}},
        idea_extensions={"DemoCo": {"TICKET": {"type": "string"}}},
    )
    cfg_path = Path(cfg["config_dir"]) / "config.yaml"
    cfg_path.write_text(
        "outbox_targets:\n  DemoCo:\n    repo_path: /tmp/tf\n"
        "idea_extensions:\n  DemoCo:\n    TICKET:\n      type: string\n",
        encoding="utf-8",
    )
    _seed_idea(Path(cfg["org_files"][0]))
    out_path = _seed_outbox(Path(cfg["data_dir"]), "DemoCo")
    plan = plan_project_rename(cfg, "DemoCo", "DemoLabs")
    result = apply_project_rename(cfg, plan)
    assert result["written"] is True
    assert result["ideas_updated"] == 1
    assert result["ext_renamed"] == 1
    assert result["outbox_renamed"] is True

    disk = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "DemoCo" not in disk["outbox_targets"]
    assert disk["outbox_targets"]["DemoLabs"]["repo_path"] == repo
    assert "DemoCo" not in disk["idea_extensions"]
    assert "DemoLabs" in disk["idea_extensions"]
    assert list(Path(cfg["config_dir"]).glob("config.yaml.bak.*"))

    assert not (Path(cfg["data_dir"]) / "outbox" / "DemoCo").exists()
    new_md = Path(cfg["data_dir"]) / "outbox" / "DemoLabs" / "DEMO-0001-demo.md"
    assert new_md.is_file()
    fm, _ = split_frontmatter(new_md.read_text(encoding="utf-8"))
    assert fm["id"] == "DEMO-0001"
    assert fm["target_project"] == "DemoLabs"
    assert (Path(cfg["data_dir"]) / "outbox" / "INDEX.md").is_file()

    invalidate_tasks_cache()
    ideas = filter_tasks(load_tasks(cfg), is_idea=True)
    assert len(ideas) == 1
    assert ideas[0].properties.get("PROJECT") == "DemoLabs"
    exts = read_idea_extensions(cfg, ideas[0])
    assert "DemoLabs" in exts
    assert "DemoCo" not in exts
    assert exts["DemoLabs"]["TICKET"] == "TF-9"
    # original path object is stale after rename
    assert not out_path.exists()


def test_conflict_new_exists(tmp_path):
    cfg = _cfg(tmp_path, outbox={"DemoCo": {}, "DemoLabs": {}})
    try:
        plan_project_rename(cfg, "DemoCo", "DemoLabs")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "conflict" in str(e).lower()


def test_conflict_missing_old(tmp_path):
    cfg = _cfg(tmp_path, outbox={})
    try:
        plan_project_rename(cfg, "DemoCo", "DemoLabs")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "no key" in str(e)


def test_unsafe_name(tmp_path):
    cfg = _cfg(tmp_path, outbox={"DemoCo": {}})
    try:
        plan_project_rename(cfg, "DemoCo", "a/b")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "unsafe" in str(e).lower()


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, outbox={"DemoCo": {"repo_path": "/tmp/tf"}})
    cfg_path = Path(cfg["config_dir"]) / "config.yaml"
    cfg_path.write_text("outbox_targets:\n  DemoCo:\n    repo_path: /tmp/tf\n", encoding="utf-8")
    before = cfg_path.read_text(encoding="utf-8")
    _seed_outbox(Path(cfg["data_dir"]), "DemoCo")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects", "rename", "DemoCo", "DemoLabs"])
    assert r.exit_code == 0, r.output
    assert "not written" in r.output.lower() or "--yes" in r.output
    assert cfg_path.read_text(encoding="utf-8") == before
    assert (Path(cfg["data_dir"]) / "outbox" / "DemoCo").is_dir()


def test_cli_yes_applies(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, outbox={"DemoCo": {}})
    cfg_path = Path(cfg["config_dir"]) / "config.yaml"
    cfg_path.write_text("outbox_targets:\n  DemoCo: {}\n", encoding="utf-8")
    _seed_idea(Path(cfg["org_files"][0]))
    _seed_outbox(Path(cfg["data_dir"]), "DemoCo")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects", "rename", "DemoCo", "DemoLabs", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["schema"] == "nw.projects.rename.v1"
    assert payload["written"] is True
    disk = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "DemoLabs" in disk["outbox_targets"]
    assert "DemoCo" not in disk["outbox_targets"]


def test_cli_conflict_nonzero(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, outbox={"DemoCo": {}, "DemoLabs": {}})
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects", "rename", "DemoCo", "DemoLabs", "--yes"])
    assert r.exit_code != 0
    assert "conflict" in r.output.lower()
