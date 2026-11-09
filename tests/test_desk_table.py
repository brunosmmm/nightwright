"""Shared idea-table planner and `load_rows` sort parity (SPEC-0110 leftovers).

SPEC-0495 retired the desk's table (column auto-drop, filter bar, sort keys, wide mode); what
stays covered here is the CLI planner and that `load_rows` — still the desk's search loader —
orders exactly like `nw ideas`.
"""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import report as R
from nightwright.cli import cli
from nightwright.tui import model as M

import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    conf = tmp_path / "config"
    conf.mkdir()
    (conf / "mappings.yaml").write_text("t:\n  bucket: Example\nm:\n  bucket: Meta-Tools\n")
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path),
        "config_dir": str(conf),
        "project_axis": "bucket",
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


def _seed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "alpha example thing", "--project", "Example"]).exit_code == 0
    assert run(cli, ["idea", "beta meta thing", "--project", "Meta-Tools",
                     "--kind", "bug"]).exit_code == 0
    assert run(cli, ["idea", "gamma meta thing", "--project", "Meta-Tools"]).exit_code == 0
    assert run(cli, ["idea", "priority", "IDEA-002", "--set", "A"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-003", "--add", "still open?"]).exit_code == 0
    return cfg


# --- the shared planner --------------------------------------------------------------------

def test_planner_takes_an_explicit_width_and_defaults_to_console():
    prow = [{"id": "IDEA-001", "state": "INCUBATE", "pri": "A", "q": "3",
             "kind": "bug", "project": "Meta-Tools", "updated": "12m"}]
    keys = ("id", "state", "pri", "q", "kind", "project", "updated")
    narrow = R._idea_table_widths(prow, keys, width=90)[1]
    wide = R._idea_table_widths(prow, keys, width=200)[1]
    assert wide > narrow
    # default path still works (reads console.width) — CLI callers unchanged
    assert R._idea_table_widths(prow, keys)[1] >= 1


def test_q_is_registered_in_the_shared_column_table():
    assert "q" in R._IDEA_META_COLS
    header, min_w, cap = R._IDEA_META_COLS["q"]
    assert header == "q" and min_w >= 1 and cap is not None   # capped => shrinkable


# --- sort matches the CLI ---------------------------------------------------------------------

def test_sort_matches_the_cli(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    for sort, desc in (("freshness", False), ("priority", False), ("priority", True)):
        desk = [r.id for r, _ in M.load_rows(cfg, sort=sort, desc=desc)]
        argv = ["ideas", "--sort", sort, "--json"] + (["--desc"] if desc else [])
        cli_ids = [r["id"] for r in json.loads(CliRunner().invoke(cli, argv).output)["ideas"]]
        assert desk == cli_ids, f"sort={sort} desc={desc} disagrees with the CLI"


# --- CLI q column --------------------------------------------------------------------------------

def test_cli_shows_q_only_when_some_idea_has_open_questions(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    r = run(cli, ["ideas"])
    assert r.exit_code == 0
    assert " q " in r.output, "q column missing while an idea has open questions"

    assert run(cli, ["idea", "questions", "IDEA-003", "--resolve", "1"]).exit_code == 0
    r = run(cli, ["ideas"])
    header = r.output.splitlines()[3] if len(r.output.splitlines()) > 3 else r.output
    assert " q " not in header, "q column shown when no idea has open questions"
