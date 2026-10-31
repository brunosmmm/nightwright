"""Registry-driven sorting on every column (SPEC-0113).

The load-bearing test is the drift guard: `_apply_sort` used to take a `keys` registry and then
ignore it, validating against a hardcoded pair. If names and registry can disagree again,
adding a sort key silently does nothing.
"""
import json
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
    conf.mkdir(exist_ok=True)
    (conf / "mappings.yaml").write_text("a:\n  bucket: Alpha\nz:\n  bucket: Zulu\n")
    return {
        "config_dir": str(conf),
        "project_axis": "bucket",
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "INPROGRESS", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


def _seed(tmp_path, monkeypatch):
    """Three ideas with deliberately different project/state/kind/priority/question counts."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "zeta", "--project", "Zulu", "--kind", "bug"]).exit_code == 0
    assert run(cli, ["idea", "alpha", "--project", "Alpha"]).exit_code == 0
    assert run(cli, ["idea", "no project here"]).exit_code == 0
    assert run(cli, ["idea", "priority", "IDEA-002", "--set", "A"]).exit_code == 0
    assert run(cli, ["state", "IDEA-001", "SPECCED"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-003", "--add", "q1?"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-003", "--add", "q2?"]).exit_code == 0
    return cfg


def _ids(cfg, **kw):
    return [t.properties.get("ID") for t in R.collect_idea_tasks(cfg, **kw)]


# --- the drift guard ----------------------------------------------------------------------

def test_names_match_the_registries(tmp_path):
    """If these can disagree, adding a registry entry silently does nothing — the original bug."""
    cfg = _cfg(tmp_path)
    assert R.sort_key_names(R.idea_sort_keys(cfg)) == list(R.IDEA_SORT_NAMES)
    assert R.sort_key_names(R.task_sort_keys(cfg)) == list(R.TASK_SORT_NAMES)


def test_apply_sort_validates_from_the_registry():
    keys = {"default": lambda t: 0, "custom": lambda t: 1}
    assert R._apply_sort([], sort="custom", keys=keys) == []
    with pytest.raises(ValueError, match="expected one of: custom"):
        R._apply_sort([], sort="freshness", keys=keys)


def test_default_is_not_selectable_by_name():
    """`default` is an internal alias, not part of the vocabulary."""
    keys = R.idea_sort_keys(_cfg_stub := {"org_idea_keywords": ["IDEA"]})
    assert "default" not in R.sort_key_names(keys)
    with pytest.raises(ValueError):
        R._apply_sort([], sort="default", keys=keys)


def test_cli_and_desk_offer_the_same_vocabulary():
    assert tuple(M.SORT_FIELDS) == R.IDEA_SORT_NAMES


def test_every_name_is_accepted_end_to_end(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    for name in R.IDEA_SORT_NAMES:
        for argv in (["ideas", "--sort", name, "--json"],
                     ["ideas", "--sort", name, "--desc", "--json"]):
            r = CliRunner().invoke(cli, argv)
            assert r.exit_code == 0, f"--sort {name} failed: {r.output}"
            assert json.loads(r.output)["ideas"]
    for name in R.TASK_SORT_NAMES:
        r = CliRunner().invoke(cli, ["tasks", "--sort", name, "--json"])
        assert r.exit_code == 0, f"tasks --sort {name} failed: {r.output}"


def test_an_unknown_sort_is_rejected_by_click(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["ideas", "--sort", "nonsense"])
    assert r.exit_code != 0
    assert "freshness" in r.output and "questions" in r.output   # offers the vocabulary


# --- the individual keys ------------------------------------------------------------------

def test_state_sorts_by_workflow_order_not_alphabetically(tmp_path, monkeypatch):
    """The states are chosen so the two orders genuinely disagree — otherwise the test proves
    nothing. Workflow: IDEA < SPECCED < PROMOTED. Alphabetical: IDEA, PROMOTED, SPECCED."""
    cfg = _seed(tmp_path, monkeypatch)
    assert CliRunner().invoke(cli, ["state", "IDEA-002", "PROMOTED"]).exit_code == 0

    states = [t.state for t in R.collect_idea_tasks(cfg, sort="state", all_done=True)]
    assert states == ["IDEA", "SPECCED", "PROMOTED"]
    assert states != sorted(states), "workflow order must differ from alphabetical here"

    order = R._state_order(cfg, idea=True)
    ranks = [order[s.upper()] for s in states]
    assert ranks == sorted(ranks)


def test_unknown_state_sorts_last(tmp_path):
    cfg = _cfg(tmp_path)
    order = R._state_order(cfg, idea=True)
    assert order["IDEA"] == 0
    assert "|" not in order
    assert order.get("CUSTOM", len(order)) == len(order)


def test_project_sorts_alphabetically_with_blanks_last(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    projects = [(t.properties.get("PROJECT") or "") for t in
                R.collect_idea_tasks(cfg, sort="project")]
    named = [p for p in projects if p]
    assert named == sorted(named, key=str.lower)
    assert projects[-1] == "", "a missing project is unknown, not alphabetically first"


def test_questions_sorts_most_open_first(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    from nightwright.explore import open_question_count

    counts = [open_question_count(cfg, t) for t in R.collect_idea_tasks(cfg, sort="questions")]
    assert counts == sorted(counts, reverse=True)


def test_kind_and_id_sort(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert _ids(cfg, sort="id") == sorted(_ids(cfg, sort="id"))
    from nightwright.org_write import idea_kind
    kinds = [idea_kind(t) for t in R.collect_idea_tasks(cfg, sort="kind")]
    assert kinds == sorted(kinds)


def test_created_sorts_newest_first(tmp_path, monkeypatch):
    from nightwright.org_write import parse_inactive_stamp

    cfg = _seed(tmp_path, monkeypatch)
    stamps = [parse_inactive_stamp(t.properties.get("CREATED"))
              for t in R.collect_idea_tasks(cfg, sort="created")]
    dated = [s for s in stamps if s]
    assert dated == sorted(dated, reverse=True)


def test_desc_reverses_every_key(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    for name in R.IDEA_SORT_NAMES:
        assert _ids(cfg, sort=name, desc=True) == list(reversed(_ids(cfg, sort=name))), name


def test_sorts_are_stable_and_total(tmp_path, monkeypatch):
    """Every key ends in a tiebreaker, so repeated calls cannot reorder equal primaries."""
    cfg = _seed(tmp_path, monkeypatch)
    for name in R.IDEA_SORT_NAMES:
        first = _ids(cfg, sort=name)
        assert first == _ids(cfg, sort=name), name
        assert len(first) == 3, name


def test_default_order_is_unchanged(tmp_path, monkeypatch):
    """SPEC-0077's freshness default must survive the refactor."""
    cfg = _seed(tmp_path, monkeypatch)
    assert _ids(cfg) == _ids(cfg, sort="freshness")
