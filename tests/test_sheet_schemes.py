"""SPEC-0124: sheet-sync manifest format + outbox_targets[project].sheet config."""
import importlib

import pytest

from nightwright import sheet_schemes as SS
from nightwright.export_schemes import template_scheme as TS

VALID_MANIFEST = """
kind = "sheet"
name = "example-pretriage-sheet"
description = "Example Pre-triage Backlog tab column mapping"
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


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_manifest_valid(tmp_path):
    p = _write(tmp_path, "example.toml", VALID_MANIFEST)
    m = SS.load_manifest(p)
    assert m.name == "example-pretriage-sheet"
    assert m.id_column == "ID"
    assert [c.header for c in m.columns] == ["ID", "Feature", "Status", "Costumer"]
    assert [c.header for c in m.nw_owned()] == ["ID", "Feature", "Status"]
    assert [c.header for c in m.sheet_owned()] == ["Costumer"]
    assert m.column("Feature").source == "title"
    assert m.column("Nope") is None
    assert m.column("Status").value_map == {}


VALUE_MAP_MANIFEST = """
kind = "sheet"
name = "value-map-test"
id_column = "ID"

[[columns]]
header = "ID"
source = "id"
owner = "nw"

[[columns]]
header = "Status"
source = "status"
owner = "nw"
[columns.value_map]
IDEA = "New"
SPECCED = "Planned"
"""


def test_load_manifest_with_value_map(tmp_path):
    p = _write(tmp_path, "vm.toml", VALUE_MAP_MANIFEST)
    m = SS.load_manifest(p)
    status_col = m.column("Status")
    assert status_col.value_map == {"IDEA": "New", "SPECCED": "Planned"}
    assert m.column("ID").value_map == {}


@pytest.mark.parametrize("broken, msg", [
    ('name = "x"\nid_column = "ID"\ncolumns = [{header = "ID", source = "id", owner = "nw", '
     'value_map = "not-a-table"}]\n', "value_map must be a table"),
    ('name = "x"\nid_column = "ID"\ncolumns = [{header = "ID", source = "id", owner = "nw", '
     'value_map = {IDEA = ""}}]\n', "non-empty strings"),
])
def test_load_manifest_invalid_value_map(tmp_path, broken, msg):
    p = _write(tmp_path, "bad_vm.toml", broken)
    with pytest.raises(ValueError, match=msg):
        SS.load_manifest(p)


@pytest.mark.parametrize("broken, msg", [
    ("", "name"),
    ('name = "x"\n', "id_column"),
    ('name = "x"\nid_column = "ID"\n', "columns"),
    ('name = "x"\nid_column = "ID"\ncolumns = [{header = "ID", source = "id", owner = "sheet"}]\n',
     "owner 'nw'"),
    ('name = "x"\nid_column = "MISSING"\n'
     'columns = [{header = "ID", source = "id", owner = "nw"}]\n', "not one of"),
    ('name = "x"\nid_column = "ID"\n'
     'columns = [{header = "ID", source = "id", owner = "weird"}]\n', "must be one of"),
    ('name = "x"\nid_column = "ID"\n'
     'columns = [{header = "ID", source = "id", owner = "nw"}, '
     '{header = "ID", source = "id2", owner = "sheet"}]\n', "duplicate"),
])
def test_load_manifest_invalid_shapes(tmp_path, broken, msg):
    p = _write(tmp_path, "bad.toml", broken)
    with pytest.raises(ValueError, match=msg):
        SS.load_manifest(p)


def test_available_and_get_sheet_manifest(tmp_path, monkeypatch):
    schemes_dir = tmp_path / "schemes"
    schemes_dir.mkdir()
    (schemes_dir / "example.toml").write_text(VALID_MANIFEST)
    monkeypatch.setenv("NW_CONFIG_DIR", str(tmp_path))

    manifests = SS.available_sheet_manifests({})
    assert set(manifests) == {"example-pretriage-sheet"}

    got = SS.get_sheet_manifest({}, "example-pretriage-sheet")
    assert got.id_column == "ID"

    with pytest.raises(ValueError, match="unknown sheet manifest"):
        SS.get_sheet_manifest({}, "nope")


def test_available_sheet_manifests_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NW_CONFIG_DIR", str(tmp_path))
    assert SS.available_sheet_manifests({}) == {}


def test_sheet_manifest_never_registers_as_export_scheme(tmp_path, monkeypatch):
    schemes_dir = tmp_path / "schemes"
    schemes_dir.mkdir()
    (schemes_dir / "example.toml").write_text(VALID_MANIFEST)
    monkeypatch.setenv("NW_CONFIG_DIR", str(tmp_path))
    try:
        importlib.reload(TS)
        names = {s.name for s in __import__("nightwright.export_schemes", fromlist=["available"]).available()}
        assert "example-pretriage-sheet" not in names
    finally:
        monkeypatch.delenv("NW_CONFIG_DIR", raising=False)
        importlib.reload(TS)


def test_sheet_target_for_and_resolve(tmp_path, monkeypatch):
    schemes_dir = tmp_path / "schemes"
    schemes_dir.mkdir()
    (schemes_dir / "example.toml").write_text(VALID_MANIFEST)
    monkeypatch.setenv("NW_CONFIG_DIR", str(tmp_path))

    cfg_no_target = {"outbox_targets": {"Example": {"repo_path": "~/work/example-repo-ats"}}}
    assert SS.sheet_target_for(cfg_no_target, "Example") is None
    with pytest.raises(ValueError, match="no sheet target"):
        SS.resolve_sheet_manifest(cfg_no_target, "Example")

    cfg_missing_keys = {"outbox_targets": {"Example": {"sheet": {"spreadsheet_id": "abc"}}}}
    with pytest.raises(ValueError, match="missing required key"):
        SS.resolve_sheet_manifest(cfg_missing_keys, "Example")

    cfg_ok = {"outbox_targets": {"Example": {"sheet": {
        "spreadsheet_id": "abc123", "tab": "Pre-triage Backlog",
        "manifest": "example-pretriage-sheet"}}}}
    target, manifest = SS.resolve_sheet_manifest(cfg_ok, "Example")
    assert target["spreadsheet_id"] == "abc123"
    assert manifest.name == "example-pretriage-sheet"

    assert SS.sheet_target_for({}, "Unknown") is None
