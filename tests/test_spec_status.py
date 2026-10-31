"""SPEC-0179: `set_spec_status` / `spec_status` and `nw spec status`."""
import json
import pathlib

import click
import pytest
from click.testing import CliRunner

from nightwright.cli import cli
from nightwright.specs import SPEC_STATUSES, set_spec_status, spec_status

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "docs/specs/TEMPLATE.md"


def _spec(tmp_path, *, updated=None, name="0179-demo.md"):
    text = TEMPLATE.read_text(encoding="utf-8").replace("id: SPEC-NNNN", "id: SPEC-0179", 1)
    text = text.replace("created: YYYY-MM-DD", "created: 2026-10-01", 1)
    if updated:
        text = text.replace("# updated: YYYY-MM-DD", f"updated: {updated}", 1)
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path, text


def _changed(before, after):
    b, a = before.splitlines(), after.splitlines()
    return [x for x in a if x not in b], [x for x in b if x not in a]


def test_set_status_inserts_updated_after_created(tmp_path):
    path, before = _spec(tmp_path)
    assert set_spec_status(path, "in-progress", today="2026-10-25") == ("draft", "in-progress")
    after = path.read_text(encoding="utf-8")
    added, removed = _changed(before, after)
    assert added == ["status: in-progress", "updated: 2026-10-25"]
    assert removed == ["status: draft"]
    lines = after.splitlines()
    assert lines[lines.index("created: 2026-10-01") + 1] == "updated: 2026-10-25"
    assert len(after.splitlines()) == len(before.splitlines()) + 1


def test_set_status_replaces_existing_updated(tmp_path):
    path, before = _spec(tmp_path, updated="2026-10-02")
    set_spec_status(path, "done", today="2026-10-25")
    added, removed = _changed(before, path.read_text(encoding="utf-8"))
    assert added == ["status: done", "updated: 2026-10-25"]
    assert removed == ["status: draft", "updated: 2026-10-02"]


def test_same_status_refreshes_updated(tmp_path):
    path, before = _spec(tmp_path, updated="2026-10-02")
    assert set_spec_status(path, "draft", today="2026-10-25") == ("draft", "draft")
    added, removed = _changed(before, path.read_text(encoding="utf-8"))
    assert added == ["updated: 2026-10-25"] and removed == ["updated: 2026-10-02"]


def test_rest_of_file_byte_identical_and_crlf_preserved(tmp_path):
    path, before = _spec(tmp_path)
    path.write_bytes(before.replace("\n", "\r\n").encode())
    set_spec_status(path, "accepted", today="2026-10-25")
    raw = path.read_bytes().decode()
    assert "\n" not in raw.replace("\r\n", "")
    expected = before.replace("status: draft", "status: accepted", 1)
    expected = expected.replace("created: 2026-10-01\n", "created: 2026-10-01\nupdated: 2026-10-25\n", 1)
    assert raw == expected.replace("\n", "\r\n")


def test_default_today_is_iso_date(tmp_path):
    path, _ = _spec(tmp_path)
    set_spec_status(path, "proposed")
    assert len(spec_status(path)["updated"]) == 10


def test_errors(tmp_path):
    path, _ = _spec(tmp_path)
    with pytest.raises(ValueError, match="invalid status"):
        set_spec_status(path, "bogus")
    bare = tmp_path / "bare.md"
    bare.write_text("# no frontmatter\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no frontmatter"):
        set_spec_status(bare, "done")
    nostatus = tmp_path / "nostatus.md"
    nostatus.write_text("---\nid: SPEC-1\ncreated: 2026-10-01\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="status"):
        set_spec_status(nostatus, "done")


def test_spec_status_reads(tmp_path):
    path, _ = _spec(tmp_path, updated="2026-10-02")
    assert spec_status(path) == {"status": "draft", "updated": "2026-10-02"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    specs = tmp_path / "specs"
    specs.mkdir()
    _spec(specs)
    c = {"specs_dir": str(specs), "outbox_dir": str(tmp_path / "outbox")}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: c)
    return c


def test_cli_show_and_set(cfg, tmp_path):
    r = CliRunner().invoke(cli, ["spec", "status", "SPEC-0179"])
    assert r.exit_code == 0, r.output
    assert "SPEC-0179" in r.output and "draft" in r.output and "→" not in r.output
    r = CliRunner().invoke(cli, ["spec", "status", "SPEC-0179", "--set", "in-progress"])
    assert r.exit_code == 0, r.output
    assert "draft → in-progress" in r.output
    assert spec_status(tmp_path / "specs/0179-demo.md")["status"] == "in-progress"


def test_cli_json(cfg, tmp_path):
    r = CliRunner().invoke(cli, ["spec", "status", "SPEC-0179", "--set", "done", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "nw.spec.status.v1" and d["id"] == "SPEC-0179"
    assert d["status"] == "done" and d["path"].endswith("0179-demo.md") and d["updated"]


def test_cli_errors(cfg):
    r = CliRunner().invoke(cli, ["spec", "status", "SPEC-9999"])
    assert r.exit_code != 0 and "no spec file" in r.output
    r = CliRunner().invoke(cli, ["spec", "status", "SPEC-0179", "--set", "bogus"])
    assert r.exit_code == 2 and "Invalid value" in r.output


def test_set_option_is_choice_of_schema_enum():
    cmd = cli.commands["spec"].commands["status"]
    opt = next(p for p in cmd.params if p.name == "new_status")
    assert isinstance(opt.type, click.Choice) and tuple(opt.type.choices) == SPEC_STATUSES
