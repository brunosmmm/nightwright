"""Hierarchical idea view: project > epic > idea (SPEC-0114).

The load-bearing invariant is **every idea appears exactly once** — that is what a grouping bug
breaks, and a shape assertion alone would not notice a duplicated or dropped row.
"""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import report as R
from nightwright.cli import cli


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    conf = tmp_path / "config"
    conf.mkdir()
    (conf / "mappings.yaml").write_text("a:\n  bucket: Alpha\nz:\n  bucket: Zulu\n")
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


def _spec(cfg, num, title, *, parent=None, source=None, kind="feature"):
    fm = [f"id: SPEC-{num:04d}", f'title: "{title}"', "status: accepted", "owner: user",
          "created: 2026-07-31", "updated: 2026-07-31", f"kind: {kind}"]
    if parent:
        fm.append(f"parent: {parent}")
    if source:
        fm.append(f"source_idea: {source}")
    (Path(cfg["specs_dir"]) / f"{num:04d}-x.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n## Context\n\nx\n")


def _seed(tmp_path, monkeypatch):
    """Alpha: an epic with 2 children + 1 loose. Zulu: 1 idea. Plus a project-less idea."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    for text, proj in (("the epic", "Alpha"), ("child one", "Alpha"), ("child two", "Alpha"),
                       ("loose alpha", "Alpha"), ("zulu thing", "Zulu")):
        assert run(cli, ["idea", text, "--project", proj]).exit_code == 0
    assert run(cli, ["idea", "homeless idea"]).exit_code == 0

    _spec(cfg, 1, "epic", source="IDEA-001", kind="epic")
    _spec(cfg, 2, "child one", parent="SPEC-0001", source="IDEA-002")
    _spec(cfg, 3, "child two", parent="SPEC-0001", source="IDEA-003")
    for idea, spec in (("IDEA-001", "SPEC-0001"), ("IDEA-002", "SPEC-0002"),
                       ("IDEA-003", "SPEC-0003")):
        from nightwright.org_write import resolve_selector, set_property
        set_property(cfg, resolve_selector(cfg, idea), "SPEC", spec)
    return cfg


def _tree(cfg, **kw):
    tasks = R.collect_idea_tasks(cfg, **{k: v for k, v in kw.items() if k != "sort"})
    return R.build_idea_tree(cfg, tasks, sort=kw.get("sort"))


def _all_ids(tree):
    out = []
    for g in tree:
        stack = list(g["nodes"])
        while stack:
            n = stack.pop()
            out.append(n["id"])
            stack.extend(n["children"])
    return out


# --- shape --------------------------------------------------------------------------------

def test_derived_epic_edges(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert R._epic_parent_map(cfg) == {"IDEA-002": "IDEA-001", "IDEA-003": "IDEA-001"}


def test_tree_groups_project_then_epic_then_idea(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    tree = _tree(cfg)
    by_project = {g["project"]: g for g in tree}
    assert set(by_project) == {"Alpha", "Zulu", R.NO_PROJECT}

    epic = [n for n in by_project["Alpha"]["nodes"] if n["children"]]
    assert len(epic) == 1 and epic[0]["id"] == "IDEA-001"
    assert {c["id"] for c in epic[0]["children"]} == {"IDEA-002", "IDEA-003"}
    # the loose Alpha idea sits directly under its project, not under the epic
    assert "IDEA-004" in {n["id"] for n in by_project["Alpha"]["nodes"]}


def test_every_idea_appears_exactly_once(tmp_path, monkeypatch):
    """The invariant a grouping bug breaks."""
    cfg = _seed(tmp_path, monkeypatch)
    tasks = R.collect_idea_tasks(cfg)
    ids = _all_ids(R.build_idea_tree(cfg, tasks))
    assert sorted(ids) == sorted(t.properties.get("ID") for t in tasks)
    assert len(ids) == len(set(ids)), "an idea was duplicated"


def test_no_project_group_sorts_last(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert _tree(cfg)[-1]["project"] == R.NO_PROJECT


def test_counts_are_descendant_counts(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    alpha = [g for g in _tree(cfg) if g["project"] == "Alpha"][0]
    epic = [n for n in alpha["nodes"] if n["children"]][0]
    assert epic["count"] == 3, "epic + 2 children"
    assert alpha["count"] == 4, "epic subtree + the loose idea"


def test_a_bad_sort_name_is_rejected(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="invalid sort"):
        R.build_idea_tree(cfg, R.collect_idea_tasks(cfg), sort="nonsense")


# --- filtering keeps ancestors ----------------------------------------------------------------

def test_filtered_view_keeps_the_epic_as_context(tmp_path, monkeypatch):
    """A matched child must never appear without its epic."""
    cfg = _seed(tmp_path, monkeypatch)
    child = [t for t in R.collect_idea_tasks(cfg)
             if t.properties.get("ID") == "IDEA-002"]
    tree = R.build_idea_tree(cfg, child, matched={"IDEA-002"})
    alpha = [g for g in tree if g["project"] == "Alpha"][0]
    epic = alpha["nodes"][0]
    assert epic["id"] == "IDEA-001"
    assert epic["matched"] is False, "the pulled-in ancestor should be marked as context"
    assert [c["id"] for c in epic["children"]] == ["IDEA-002"]
    assert epic["children"][0]["matched"] is True


# --- sort composition ---------------------------------------------------------------------------

def test_sort_applies_within_groups(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    for name in ("freshness", "id", "state", "kind"):
        tree = _tree(cfg, sort=name)
        assert _all_ids(tree), name
        assert len(_all_ids(tree)) == 6, name


def test_groups_order_by_their_best_member(tmp_path, monkeypatch):
    """`--sort id` should put the group holding the lowest id first, `(no project)` excepted."""
    cfg = _seed(tmp_path, monkeypatch)
    tree = _tree(cfg, sort="id")
    named = [g for g in tree if g["project"] != R.NO_PROJECT]
    firsts = [min(n["id"] for n in g["nodes"]) for g in named]
    assert firsts == sorted(firsts)


# --- CLI -----------------------------------------------------------------------------------------

def test_cli_tree_renders_groups_and_nesting(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    out = CliRunner().invoke(cli, ["ideas", "--tree"]).output
    assert "▾ Alpha" in out and "▾ Zulu" in out
    assert R.NO_PROJECT in out
    assert "IDEA-001" in out and "IDEA-002" in out
    assert out.index("▾ Alpha") < out.index("IDEA-002")


def test_cli_flat_output_unchanged_without_tree(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    flat = CliRunner().invoke(cli, ["ideas"]).output
    assert "▾ Alpha" not in flat
    assert "IDEA-001" in flat


def test_cli_tree_composes_with_filters_and_sort(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    out = run(cli, ["ideas", "--tree", "--project", "Alpha"]).output
    assert "▾ Alpha" in out and "▾ Zulu" not in out
    for name in R.IDEA_SORT_NAMES:
        r = run(cli, ["ideas", "--tree", "--sort", name])
        assert r.exit_code == 0, f"--tree --sort {name}: {r.output}"


def test_json_is_unaffected_by_tree(tmp_path, monkeypatch):
    """`--tree` is a view mode; the agent contract does not change."""
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    a = json.loads(run(cli, ["ideas", "--json"]).output)
    b = json.loads(run(cli, ["ideas", "--tree", "--json"]).output)
    assert a == b
