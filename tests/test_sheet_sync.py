"""nw sheet plan/record (SPEC-0125): pure local JSON sync-diff over the idea corpus."""
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import idea_ext as IX
from nightwright import org_write as W
from nightwright import sheet_schemes as SS
from nightwright import sheet_sync as SHS
from nightwright.cli import cli
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast

MANIFEST_TOML = """
kind = "sheet"
name = "example-pretriage-sheet"
description = "test manifest"
id_column = "ID"

[[columns]]
header = "ID"
source = "id"
owner = "nw"

[[columns]]
header = "Feature"
source = "title"
owner = "nw"

[[columns]]
header = "Status"
source = "status"
owner = "nw"

[[columns]]
header = "Costumer"
source = "costumer"
owner = "sheet"
"""


def _cfg(tmp_path, monkeypatch, **overrides):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "mappings.yaml").write_text("topics: {}\n")
    schemes_dir = config_dir / "schemes"
    schemes_dir.mkdir()
    (schemes_dir / "example.toml").write_text(MANIFEST_TOML)
    monkeypatch.setenv("NW_CONFIG_DIR", str(config_dir))

    base = {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "config_dir": str(config_dir),
        "project_axis": "bucket",
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "idea_extensions": {},
        "outbox_targets": {
            "Example": {
                "sheet": {
                    "spreadsheet_id": "abc123",
                    "tab": "Pre-triage Backlog",
                    "manifest": "example-pretriage-sheet",
                }
            }
        },
    }
    base.update(overrides)
    return base


def _ideas(cfg):
    return [t for t in load_tasks(cfg) if t.is_idea]


def _idea_by_id(cfg, idea_id):
    for t in _ideas(cfg):
        if (t.properties.get("ID") or t.id) == idea_id:
            return t
    raise AssertionError(f"no idea {idea_id}")


def test_plan_push_scope_requires_sheet_sync_yes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    W.add_idea(cfg, "not opted in", project="Example")
    W.add_idea(cfg, "opted in", project="Example")
    IX.set_idea_extension(cfg, "IDEA-002", "sheet_sync", "yes")

    push = SHS.plan_push(cfg, "Example")
    assert [e["idea"] for e in push] == ["IDEA-002"]
    assert push[0]["action"] == "new"
    assert push[0]["row"]["Feature"] == "opted in"


def test_plan_push_skips_unchanged_and_flags_stale(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    W.add_idea(cfg, "synced idea", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "sheet_sync", "yes")

    task = _idea_by_id(cfg, "IDEA-001")
    ext = IX.read_idea_extension(cfg, task, "Example")
    from nightwright.sheet_schemes import resolve_sheet_manifest
    _, manifest = resolve_sheet_manifest(cfg, "Example")
    h = SHS.content_hash(manifest, task, ext)
    IX.set_idea_extension(cfg, "IDEA-001", "sheet_row", "EXAMPLE-0009")
    IX.set_idea_extension(cfg, "IDEA-001", "sheet_hash", h)

    assert SHS.plan_push(cfg, "Example") == []

    from nightwright import explore as EX
    EX.retitle_idea(cfg, "IDEA-001", "synced idea (renamed)")

    push = SHS.plan_push(cfg, "Example")
    assert len(push) == 1
    assert push[0]["idea"] == "IDEA-001"
    assert push[0]["action"] == "update"
    assert push[0]["row_id"] == "EXAMPLE-0009"


def test_plan_pull_flags_unknown_rows(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    W.add_idea(cfg, "known idea", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "sheet_sync", "yes")
    IX.set_idea_extension(cfg, "IDEA-001", "sheet_row", "EXAMPLE-0001")

    sheet_rows = [
        {"ID": "EXAMPLE-0001", "Feature": "known idea", "Status": "IDEA", "Costumer": "AB5"},
        {"ID": "EXAMPLE-0002", "Feature": "brand new row", "Status": "", "Costumer": "Imaging"},
    ]
    pull = SHS.plan_pull(cfg, "Example", sheet_rows)
    assert len(pull) == 1
    assert pull[0]["row"]["ID"] == "EXAMPLE-0002"
    assert pull[0]["reason"] == "unknown-id"


def test_make_plan_omits_pull_when_no_sheet_rows(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    plan = SHS.make_plan(cfg, "Example")
    assert plan["pull"] == []
    assert plan["manifest"] == "example-pretriage-sheet"


def test_record_pushed_persists_row_and_hash(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    W.add_idea(cfg, "to push", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "sheet_sync", "yes")

    result = SHS.record(cfg, "Example", {"pushed": [{"idea": "IDEA-001", "row_id": "EXAMPLE-0009"}]})
    assert result["pushed"] == ["IDEA-001"]

    task = _idea_by_id(cfg, "IDEA-001")
    ext = IX.read_idea_extension(cfg, task, "Example")
    assert ext["sheet_row"] == "EXAMPLE-0009"
    assert ext["sheet_hash"]

    # A second plan should now consider it synced (no change since record).
    assert SHS.plan_push(cfg, "Example") == []


def test_record_adopted_creates_new_idea(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    result = SHS.record(cfg, "Example", {"adopted": [
        {"row": {"ID": "EXAMPLE-0010", "Feature": "adopted from sheet", "Costumer": "Imaging"}},
    ]})
    assert len(result["adopted"]) == 1
    new_id = result["adopted"][0]

    task = _idea_by_id(cfg, new_id)
    assert task.project == "Example"
    ext = IX.read_idea_extension(cfg, task, "Example")
    assert ext["sheet_sync"] == "yes"
    assert ext["sheet_row"] == "EXAMPLE-0010"
    assert ext["sheet_hash"]


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_cli_sheet_plan_and_record(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    W.add_idea(cfg, "cli push me", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "sheet_sync", "yes")
    _patch(monkeypatch, cfg)

    r = CliRunner().invoke(cli, ["sheet", "plan", "--project", "Example"])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["push"][0]["idea"] == "IDEA-001"

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({"pushed": [{"idea": "IDEA-001", "row_id": "EXAMPLE-0009"}]}))
    r = CliRunner().invoke(cli, ["sheet", "record", "--project", "Example",
                                "--payload", str(payload_path)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["pushed"] == ["IDEA-001"]

    r = CliRunner().invoke(cli, ["sheet", "plan", "--project", "Example"])
    assert json.loads(r.output)["push"] == []


def test_cli_sheet_plan_unknown_project_errors(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["sheet", "plan", "--project", "NoSuchProject"])
    assert r.exit_code != 0
    assert "no sheet target" in r.output


# --- SPEC-0127: column value maps -------------------------------------------------------

VALUE_MAP_MANIFEST = """
kind = "sheet"
name = "example-pretriage-sheet"
id_column = "ID"

[[columns]]
header = "ID"
source = "id"
owner = "nw"

[[columns]]
header = "Feature"
source = "title"
owner = "nw"

[[columns]]
header = "Status"
source = "status"
owner = "nw"
[columns.value_map]
IDEA = "New"
INCUBATE = "New"
SPECCED = "Planned"
"""


def _manifest_with_value_map(tmp_path):
    p = tmp_path / "vm.toml"
    p.write_text(VALUE_MAP_MANIFEST)
    return SS.load_manifest(p)


def test_render_row_applies_value_map():
    manifest = _manifest_with_value_map(Path("/tmp"))
    task = SimpleNamespace(heading="some idea", state="SPECCED")
    row = SHS.render_row(manifest, task, {})
    assert row["Status"] == "Planned"


def test_content_hash_uses_mapped_value_not_raw():
    manifest = _manifest_with_value_map(Path("/tmp"))
    task_a = SimpleNamespace(heading="idea", state="IDEA")
    task_b = SimpleNamespace(heading="idea", state="INCUBATE")
    # IDEA and INCUBATE both map to "New" -- their hashes must be identical, proving the
    # hash is computed over the mapped value, not the raw nw state.
    assert SHS.content_hash(manifest, task_a, {}) == SHS.content_hash(manifest, task_b, {})


def test_render_row_unmapped_value_falls_back_with_warning(capsys):
    manifest = _manifest_with_value_map(Path("/tmp"))
    task = SimpleNamespace(heading="idea", state="RESEARCHED")
    row = SHS.render_row(manifest, task, {})
    assert row["Status"] == "RESEARCHED"
    err = capsys.readouterr().err
    assert "Status" in err and "RESEARCHED" in err


def test_render_row_no_value_map_is_unaffected(tmp_path):
    p = tmp_path / "novm.toml"
    p.write_text(MANIFEST_TOML)
    manifest = SS.load_manifest(p)
    task = SimpleNamespace(heading="idea", state="SPECCED")
    row = SHS.render_row(manifest, task, {})
    assert row["Status"] == "SPECCED"

