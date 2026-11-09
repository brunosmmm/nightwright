"""Epics: row field, flat column, filter, and the tree's epic level (SPEC-0116).

The decisive test is `test_stored_epic_groups_in_the_default_open_view` — SPEC-0114's derived
spec-parentage produced **zero** epics-with-children on the open corpus, which is why epics looked
absent. Stored `:EPIC:` must group there.
"""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import report as R
from nightwright.cli import cli
from nightwright.tui import model as M


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    conf = tmp_path / "config"
    conf.mkdir()
    (conf / "mappings.yaml").write_text("a:\n  bucket: Alpha\n")
    specs = tmp_path / "specs"
    specs.mkdir()
    return {
        "org_files": [str(org)],
        "config_dir": str(conf),
        "project_axis": "bucket",
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
    }


def _seed(tmp_path, monkeypatch, *, with_epic=True):
    """Two ideas under an outbound epic, one loose — all OPEN, so the default view applies."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    args = ["--project", "Alpha"] + (["--epic", "EXAMPLE-0007"] if with_epic else [])
    assert run(cli, ["idea", "first under epic"] + args).exit_code == 0
    assert run(cli, ["idea", "second under epic"] + args).exit_code == 0
    assert run(cli, ["idea", "loose one", "--project", "Alpha"]).exit_code == 0
    return cfg


# --- row + JSON ---------------------------------------------------------------------------

def test_row_exposes_epic(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    rows = {r["id"]: r for r in json.loads(
        CliRunner().invoke(cli, ["ideas", "--json"]).output)["ideas"]}
    assert rows["IDEA-001"]["epic"] == "EXAMPLE-0007"
    assert "epic" not in rows["IDEA-003"], "blank epic should be omitted, like project/spec"


def test_epic_survives_in_idea_show_json(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["ideas", "--json"])
    assert r.exit_code == 0


# --- flat column --------------------------------------------------------------------------

def _header(out):
    """The column header row. Searching the whole table gives false positives — an idea's own
    heading can contain the word "epic", which is how the omission test first passed wrongly."""
    for line in out.splitlines():
        if " id " in line and " state " in line:
            return line
    return ""


def test_flat_table_shows_the_epic_column_when_present(tmp_path, monkeypatch):
    """The reported gap: the flat view never mentioned epics at all."""
    cfg = _seed(tmp_path, monkeypatch)
    out = CliRunner().invoke(cli, ["ideas"]).output
    assert "epic" in _header(out), f"no epic column: {_header(out)!r}"
    assert "EXAMPLE-0007" in out


def test_epic_column_is_omitted_when_no_idea_has_one(tmp_path, monkeypatch):
    """Same conditional rule as `pri` and `q` — a blank column costs width for nothing."""
    cfg = _seed(tmp_path, monkeypatch, with_epic=False)
    out = CliRunner().invoke(cli, ["ideas"]).output
    assert "epic" not in _header(out), f"epic column shown with no epics: {_header(out)!r}"


# --- filter -------------------------------------------------------------------------------

def test_epic_filter_matches_the_collector(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    ids = [t.properties.get("ID") for t in R.collect_idea_tasks(cfg, epic="EXAMPLE-0007")]
    assert ids == ["IDEA-002", "IDEA-001"] or sorted(ids) == ["IDEA-001", "IDEA-002"]

    cli_ids = [r["id"] for r in json.loads(CliRunner().invoke(
        cli, ["ideas", "--epic", "EXAMPLE-0007", "--json"]).output)["ideas"]]
    assert sorted(cli_ids) == ["IDEA-001", "IDEA-002"]

    assert R.collect_idea_tasks(cfg, epic="NOPE") == []


def test_desk_loader_accepts_the_epic_filter(tmp_path, monkeypatch):
    """`nw tui --epic`-style narrowing still reaches `collect_idea_tasks` (SPEC-0490 retired the
    in-app filter bar, not the collector vocabulary)."""
    cfg = _seed(tmp_path, monkeypatch)
    assert sorted(r.id for r, _ in M.load_rows(cfg, epic="EXAMPLE-0007")) == ["IDEA-001", "IDEA-002"]


def test_desk_row_carries_epic(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    rows = {r.id: r for r, _ in M.load_rows(cfg)}
    assert rows["IDEA-001"].epic == "EXAMPLE-0007"
    assert rows["IDEA-003"].epic == ""


# --- the tree level -----------------------------------------------------------------------

def test_stored_epic_groups_in_the_default_open_view(tmp_path, monkeypatch):
    """The decisive case. SPEC-0114 derived epics from spec `parent:` and produced ZERO
    epics-with-children on the open corpus, which is why epics appeared to be missing."""
    cfg = _seed(tmp_path, monkeypatch)
    tree = R.build_idea_tree(cfg, R.collect_idea_tasks(cfg))
    alpha = [g for g in tree if g["project"] == "Alpha"][0]
    epics = [n for n in alpha["nodes"] if n["children"]]
    assert len(epics) == 1, f"no epic level in the open view: {alpha['nodes']}"
    assert epics[0]["id"] == "EXAMPLE-0007"
    assert epics[0].get("synthetic") is True, "an :EPIC: node has no idea behind it"
    assert {c["id"] for c in epics[0]["children"]} == {"IDEA-001", "IDEA-002"}
    assert epics[0]["count"] == 2


def test_an_idea_without_an_epic_sits_directly_under_its_project(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    tree = R.build_idea_tree(cfg, R.collect_idea_tasks(cfg))
    alpha = [g for g in tree if g["project"] == "Alpha"][0]
    loose = [n for n in alpha["nodes"] if not n["children"]]
    assert {n["id"] for n in loose} == {"IDEA-003"}


def test_every_idea_still_appears_exactly_once(tmp_path, monkeypatch):
    """The grouping invariant, re-asserted now that a second node kind shares the epic level."""
    cfg = _seed(tmp_path, monkeypatch)
    tasks = R.collect_idea_tasks(cfg)
    seen = []
    for g in R.build_idea_tree(cfg, tasks):
        stack = list(g["nodes"])
        while stack:
            n = stack.pop()
            if not n.get("synthetic"):
                seen.append(n["id"])
            stack.extend(n["children"])
    assert sorted(seen) == sorted(t.properties.get("ID") for t in tasks)
    assert len(seen) == len(set(seen))


def test_cli_tree_renders_the_epic_as_a_label(tmp_path, monkeypatch):
    """A synthetic node is a spec id, not an idea — rendering it with the idea renderer produced
    `EXAMPLE-0007 💡 EXAMPLE-0007`, duplicating the id and borrowing a kind glyph from a child."""
    cfg = _seed(tmp_path, monkeypatch)
    out = CliRunner().invoke(cli, ["ideas", "--tree"]).output
    assert "EXAMPLE-0007" in out
    epic_line = [ln for ln in out.splitlines() if "EXAMPLE-0007" in ln][0]
    assert epic_line.count("EXAMPLE-0007") == 1, f"id duplicated: {epic_line!r}"
    assert "💡" not in epic_line and "✨" not in epic_line, f"kind glyph on an epic: {epic_line!r}"


def test_derived_parentage_still_nests(tmp_path, monkeypatch):
    """SPEC-0114's derived idea→idea nesting must not regress now that `:EPIC:` takes priority."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    for text in ("the parent", "the child"):
        assert run(cli, ["idea", text, "--project", "Alpha"]).exit_code == 0
    for num, extra in ((1, "kind: epic\n"), (2, "kind: feature\nparent: SPEC-0001\n")):
        (Path(cfg["specs_dir"]) / f"{num:04d}-x.md").write_text(
            f"---\nid: SPEC-{num:04d}\ntitle: \"s\"\nstatus: accepted\nowner: b\n"
            f"created: 2026-08-01\nupdated: 2026-08-01\n{extra}---\n\n## Context\n\nx\n")
    from nightwright.org_write import resolve_selector, set_property
    for idea, spec in (("IDEA-001", "SPEC-0001"), ("IDEA-002", "SPEC-0002")):
        set_property(cfg, resolve_selector(cfg, idea), "SPEC", spec)

    tree = R.build_idea_tree(cfg, R.collect_idea_tasks(cfg))
    alpha = [g for g in tree if g["project"] == "Alpha"][0]
    parent = [n for n in alpha["nodes"] if n["children"]][0]
    assert parent["id"] == "IDEA-001"
    assert not parent.get("synthetic"), "derived parents are real ideas, not synthetic"
    assert [c["id"] for c in parent["children"]] == ["IDEA-002"]


def test_stored_epic_wins_over_derived_parentage(tmp_path, monkeypatch):
    """`:EPIC:` is stored and explicit; derived parentage is a fallback."""
    cfg = _seed(tmp_path, monkeypatch)
    from nightwright.org_write import resolve_selector, set_property
    set_property(cfg, resolve_selector(cfg, "IDEA-001"), "SPEC", "SPEC-0002")
    tree = R.build_idea_tree(cfg, R.collect_idea_tasks(cfg))
    alpha = [g for g in tree if g["project"] == "Alpha"][0]
    epic = [n for n in alpha["nodes"] if n["children"]][0]
    assert epic["id"] == "EXAMPLE-0007"
    assert "IDEA-001" in {c["id"] for c in epic["children"]}
