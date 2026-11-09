"""Idea extensions: org storage, schema, lint, CLI (SPEC-0119–0122)."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import idea_ext as IX
from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.extension_schema import extension_schema_for, normalize_extension_schema
from nightwright.org import filter_tasks, load_tasks

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path, **overrides):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    (config / "mappings.yaml").write_text("topics: {}\n")
    base = {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "config_dir": str(config),
        "project_axis": "bucket",
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "idea_extensions": {},
    }
    base.update(overrides)
    return base


def _idea(cfg):
    return [t for t in load_tasks(cfg) if t.is_idea][0]


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


EXAMPLE_SCHEMA = {
    "required": ["DUT"],
    "properties": {
        "DUT": {"type": "string"},
        "NODE": {"type": "string"},
        "tier": {"type": "enum", "values": ["dev", "prod"]},
    },
}


def test_ext_round_trip(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "example ext", project="Example")
    task = _idea(cfg)
    IX.set_idea_extension(cfg, "IDEA-001", "DUT", "pc4")
    IX.set_idea_extension(cfg, "IDEA-001", "NODE", "n1")
    exts = IX.read_idea_extensions(cfg, task)
    assert exts == {"Example": {"DUT": "pc4", "NODE": "n1"}}
    IX.clear_idea_extension(cfg, "IDEA-001", "NODE")
    assert IX.read_idea_extension(cfg, task, "Example") == {"DUT": "pc4"}


def test_ext_json_payload(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "json ext", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "DUT", "pc4")
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["idea", "show", "IDEA-001", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["extensions"] == {"Example": {"DUT": "pc4"}}


def test_ext_headlines_do_not_leak_into_tasks(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    W.add_idea(cfg, "no ext leak", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "DUT", "pc4")
    tasks = load_tasks(cfg)
    assert filter_tasks(tasks, is_idea=True)
    assert not any("Ext" in t.heading for t in tasks)
    out = CliRunner().invoke(cli, ["tasks", "--all"]).output
    assert "pc4" not in out and "Ext" not in out


def test_schema_normalization():
    norm = normalize_extension_schema(EXAMPLE_SCHEMA, project="Example")
    assert norm["required"] == ["DUT"]
    assert norm["properties"]["tier"]["values"] == ["dev", "prod"]


def test_schema_required_must_be_in_properties():
    with pytest.raises(ValueError, match="missing from properties"):
        normalize_extension_schema({"required": ["X"], "properties": {}}, project="P")


def test_schema_empty_enum_fails():
    with pytest.raises(ValueError, match="non-empty values"):
        normalize_extension_schema(
            {"properties": {"X": {"type": "enum", "values": []}}}, project="P")


def test_extension_schema_for_missing_project(tmp_path):
    cfg = _cfg(tmp_path, idea_extensions={"Example": EXAMPLE_SCHEMA})
    assert extension_schema_for(cfg, "Other") is None


def test_write_rejects_unknown_key(tmp_path):
    cfg = _cfg(tmp_path, idea_extensions={"Example": EXAMPLE_SCHEMA})
    W.add_idea(cfg, "schema", project="Example")
    with pytest.raises(ValueError, match="unknown key"):
        IX.set_idea_extension(cfg, "IDEA-001", "BOGUS", "x")


def test_write_requires_required_keys_after_clear(tmp_path):
    cfg = _cfg(tmp_path, idea_extensions={"Example": EXAMPLE_SCHEMA})
    W.add_idea(cfg, "schema", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "DUT", "pc4")
    with pytest.raises(ValueError, match="missing required"):
        IX.clear_idea_extension(cfg, "IDEA-001", "DUT")


def test_freeform_when_no_schema(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "free", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "anything", "goes")
    assert IX.read_idea_extension(cfg, _idea(cfg), "Example") == {"anything": "goes"}


def test_lint_strict_exit(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, idea_extensions={"Example": EXAMPLE_SCHEMA})
    _patch(monkeypatch, cfg)
    W.add_idea(cfg, "lint me", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "DUT", "pc4")
    ok = CliRunner().invoke(cli, ["idea", "ext", "lint", "IDEA-001"])
    assert ok.exit_code == 0, ok.output
    path = Path(cfg["org_ideas_file"])
    path.write_text(path.read_text().replace(":DUT: pc4", ":DUT: pc4\n   :MYSTERY: x"))
    bad = CliRunner().invoke(cli, ["idea", "ext", "lint", "IDEA-001", "--strict"])
    assert bad.exit_code == 1
    assert "unknown key" in bad.output


def test_lint_project_mismatch(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "mismatch", project="Example")
    path = Path(cfg["org_ideas_file"])
    path.write_text(
        path.read_text().rstrip() + "\n** Ext Other\n   :PROPERTIES:\n   :X: 1\n   :END:\n"
    )
    task = _idea(cfg)
    problems = IX.lint_idea_extensions(cfg, task)
    assert any("does not match" in p for p in problems)


def test_cli_ext_set_show_clear(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, idea_extensions={"Example": EXAMPLE_SCHEMA})
    _patch(monkeypatch, cfg)
    W.add_idea(cfg, "cli ext", project="Example")
    r = CliRunner().invoke(cli, ["idea", "ext", "set", "IDEA-001", "DUT", "pc4"])
    assert r.exit_code == 0, r.output
    show = CliRunner().invoke(cli, ["idea", "ext", "show", "IDEA-001"])
    assert show.exit_code == 0, show.output
    assert "DUT" in show.output and "pc4" in show.output
    clear = CliRunner().invoke(cli, ["idea", "ext", "clear", "IDEA-001", "DUT"])
    assert clear.exit_code != 0


def test_cli_ext_enum_rejects_bad_value(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, idea_extensions={"Example": EXAMPLE_SCHEMA})
    _patch(monkeypatch, cfg)
    W.add_idea(cfg, "enum", project="Example")
    IX.set_idea_extension(cfg, "IDEA-001", "DUT", "pc4")
    r = CliRunner().invoke(cli, ["idea", "ext", "set", "IDEA-001", "tier", "staging"])
    assert r.exit_code != 0


def test_idea_help_lists_ext(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["idea", "--help"])
    assert r.exit_code == 0
    assert "ext" in r.output

