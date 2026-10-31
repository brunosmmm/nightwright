"""Question CRUD + idea tag mutators (SPEC-0101).

Fills the gaps SPEC-0102's mutation desk needs: unresolve / edit / delete / per-question
priority, addressed by the same 1-based index as `resolve_question`, plus `set_idea_tags`.
"""
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import load_tasks

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


def _seed(tmp_path, monkeypatch, *, questions=("first?", "second?", "third?")):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "an idea with questions"]).exit_code == 0
    for q in questions:
        assert run(cli, ["idea", "questions", "IDEA-001", "--add", q]).exit_code == 0
    return cfg


def _idea(cfg):
    return [t for t in load_tasks(cfg) if t.is_idea][0]


def _states(cfg):
    return [(i["state"], i["text"], i.get("priority"))
            for i in EX.read_idea_questions(cfg, _idea(cfg))]


# --- unresolve -------------------------------------------------------------------------

def test_unresolve_flips_back_to_open_preserving_text(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    EX.resolve_question(cfg, "IDEA-001", 2)
    assert _states(cfg)[1][0] == "resolved"
    EX.unresolve_question(cfg, "IDEA-001", 2)
    assert _states(cfg) == [("open", "first?", None), ("open", "second?", None),
                            ("open", "third?", None)]


def test_unresolve_preserves_priority_cookie(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    EX.set_question_priority(cfg, "IDEA-001", 1, "A")
    EX.resolve_question(cfg, "IDEA-001", 1)
    EX.unresolve_question(cfg, "IDEA-001", 1)
    assert _states(cfg)[0] == ("open", "first?", "A")


def test_unresolve_of_an_open_question_is_a_noop(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    before = _states(cfg)
    EX.unresolve_question(cfg, "IDEA-001", 1)
    assert _states(cfg) == before


# --- edit ------------------------------------------------------------------------------

def test_edit_replaces_text_keeping_state_and_priority(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    EX.set_question_priority(cfg, "IDEA-001", 2, "B")
    EX.resolve_question(cfg, "IDEA-001", 2)
    EX.edit_question(cfg, "IDEA-001", 2, "sharper wording?")
    assert _states(cfg)[1] == ("resolved", "sharper wording?", "B")
    assert [t for _, t, _ in _states(cfg)] == ["first?", "sharper wording?", "third?"]


def test_edit_rejects_empty_text_without_writing(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    before = _states(cfg)
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="must not be empty"):
            EX.edit_question(cfg, "IDEA-001", 1, bad)
    assert _states(cfg) == before


# --- delete ----------------------------------------------------------------------------

def test_delete_removes_one_and_reindexes_the_rest(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    EX.delete_question(cfg, "IDEA-001", 2)
    assert [t for _, t, _ in _states(cfg)] == ["first?", "third?"]
    # addressing is positional, so what was #3 is now #2
    EX.resolve_question(cfg, "IDEA-001", 2)
    assert _states(cfg)[1][0] == "resolved"


def test_delete_last_question_leaves_an_empty_section(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch, questions=("only one?",))
    EX.delete_question(cfg, "IDEA-001", 1)
    assert _states(cfg) == []
    assert "Open questions" in Path(_idea(cfg).file).read_text()


# --- per-question priority -------------------------------------------------------------

def test_set_and_clear_question_priority(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    EX.set_question_priority(cfg, "IDEA-001", 3, "c")
    assert _states(cfg)[2] == ("open", "third?", "C")
    EX.set_question_priority(cfg, "IDEA-001", 3, None)
    assert _states(cfg)[2] == ("open", "third?", None)


def test_invalid_question_priority_refuses_without_writing(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    before = _states(cfg)
    with pytest.raises(ValueError, match="invalid priority"):
        EX.set_question_priority(cfg, "IDEA-001", 1, "Z")
    assert _states(cfg) == before


# --- index validation (shared contract) ------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda cfg, i: EX.unresolve_question(cfg, "IDEA-001", i),
    lambda cfg, i: EX.edit_question(cfg, "IDEA-001", i, "text"),
    lambda cfg, i: EX.delete_question(cfg, "IDEA-001", i),
    lambda cfg, i: EX.set_question_priority(cfg, "IDEA-001", i, "A"),
])
@pytest.mark.parametrize("index", [0, -1, 4, 99, "2", None, 1.0, True])
def test_bad_index_raises_valueerror_with_no_write(tmp_path, monkeypatch, call, index):
    cfg = _seed(tmp_path, monkeypatch)
    before = Path(_idea(cfg).file).read_text()
    with pytest.raises(ValueError, match="no question"):
        call(cfg, index)
    assert Path(_idea(cfg).file).read_text() == before


def test_mutators_reject_a_non_idea(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert CliRunner().invoke(cli, ["add", "a plain task"]).exit_code == 0
    task = [t for t in load_tasks(cfg) if not t.is_idea][0]
    sel = task.properties.get("ID") or task.heading
    with pytest.raises(ValueError, match="not an idea"):
        EX.unresolve_question(cfg, sel, 1)


# --- tags ------------------------------------------------------------------------------

def test_set_idea_tags_replaces_the_block(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_tags(cfg, "IDEA-001", ["tui", "ideas"])
    assert sorted(_idea(cfg).tags) == ["ideas", "tui"]
    W.set_idea_tags(cfg, "IDEA-001", ["explore"])
    assert sorted(_idea(cfg).tags) == ["explore"]


def test_set_idea_tags_clears_with_empty(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_tags(cfg, "IDEA-001", ["tui"])
    W.set_idea_tags(cfg, "IDEA-001", [])
    assert list(_idea(cfg).tags) == []
    W.set_idea_tags(cfg, "IDEA-001", None)
    assert list(_idea(cfg).tags) == []


def test_set_idea_tags_preserves_state_and_priority_cookie(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_priority(cfg, "IDEA-001", "A")
    W.set_idea_tags(cfg, "IDEA-001", ["tui"])
    idea = _idea(cfg)
    assert idea.state == "IDEA"              # adding a question does not advance the state
    assert idea.priority == "A"
    assert idea.heading == "an idea with questions"
    assert list(idea.tags) == ["tui"]


def test_set_idea_tags_dedups_and_strips_colons(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_tags(cfg, "IDEA-001", [":tui:", "tui", " ideas "])
    line = [ln for ln in Path(_idea(cfg).file).read_text().splitlines()
            if "an idea with questions" in ln][0]
    assert line.rstrip().endswith(":tui:ideas:")


def test_invalid_tag_refuses_without_writing(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    before = Path(_idea(cfg).file).read_text()
    for bad in ("has space", "no/slash", "dot.dot"):
        with pytest.raises(ValueError, match="invalid tag"):
            W.set_idea_tags(cfg, "IDEA-001", [bad])
    assert Path(_idea(cfg).file).read_text() == before


def test_tags_survive_a_retitle_and_a_later_tag_rewrite(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_tags(cfg, "IDEA-001", ["tui"])
    EX.retitle_idea(cfg, "IDEA-001", "a renamed idea")
    assert list(_idea(cfg).tags) == ["tui"]
    W.set_idea_tags(cfg, "IDEA-001", ["tui", "ideas"])
    assert _idea(cfg).heading == "a renamed idea"
    assert sorted(_idea(cfg).tags) == ["ideas", "tui"]


# --- CLI -------------------------------------------------------------------------------

def test_cli_question_crud_roundtrip(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    assert run(cli, ["idea", "questions", "IDEA-001", "--resolve", "1"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--unresolve", "1"]).exit_code == 0
    assert _states(cfg)[0][0] == "open"

    r = run(cli, ["idea", "questions", "IDEA-001", "--edit", "2", "--text", "reworded?"])
    assert r.exit_code == 0
    assert _states(cfg)[1][1] == "reworded?"

    assert run(cli, ["idea", "questions", "IDEA-001", "--priority", "2", "--pri", "A"]).exit_code == 0
    assert _states(cfg)[1][2] == "A"
    assert run(cli, ["idea", "questions", "IDEA-001", "--priority", "2",
                     "--pri", "none"]).exit_code == 0
    assert _states(cfg)[1][2] is None

    assert run(cli, ["idea", "questions", "IDEA-001", "--delete", "3"]).exit_code == 0
    assert len(_states(cfg)) == 2


def test_cli_rejects_ambiguous_or_incomplete_flag_combos(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    assert run(cli, ["idea", "questions", "IDEA-001", "--edit", "1"]).exit_code != 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--priority", "1"]).exit_code != 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--unresolve", "1",
                     "--delete", "2"]).exit_code != 0
    assert run(cli, ["idea", "questions", "IDEA-001"]).exit_code != 0


def test_cli_bad_index_is_a_clean_error_not_a_traceback(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["idea", "questions", "IDEA-001", "--delete", "9"])
    assert r.exit_code != 0
    assert "no question #9" in r.output
    assert "Traceback" not in r.output


def test_cli_tags_set_and_clear(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    assert run(cli, ["idea", "tags", "IDEA-001", "--set", "tui, ideas"]).exit_code == 0
    assert sorted(_idea(cfg).tags) == ["ideas", "tui"]
    assert run(cli, ["idea", "tags", "IDEA-001", "--set", ""]).exit_code == 0
    assert list(_idea(cfg).tags) == []
    assert run(cli, ["idea", "tags", "IDEA-001"]).exit_code != 0


def test_cli_tags_feed_the_existing_tag_filter(tmp_path, monkeypatch):
    """SPEC-0089's `nw ideas --tag` must see tags written by the new mutator."""
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    assert run(cli, ["idea", "tags", "IDEA-001", "--set", "tui"]).exit_code == 0
    r = run(cli, ["ideas", "--tag", "tui", "--json"])
    assert r.exit_code == 0 and "IDEA-001" in r.output
    r = run(cli, ["ideas", "--tag", "nope", "--json"])
    assert r.exit_code == 0 and "IDEA-001" not in r.output
