"""Question rationale: display + capture (SPEC-0112).

SPEC-0111 made the body exist and reach `nw.idea.v1`; this covers rendering it in both surfaces
and writing it without leaving the desk.
"""
import asyncio
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright.cli import cli
from nightwright.org import load_tasks
from nightwright.tui import model as M

ROOT = Path(__file__).resolve().parent.parent


WHY = "ANSI, because hex was invisible on dark."


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


def _seed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "rationale probe"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "is textual pinned?"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "which theme?"]).exit_code == 0
    return cfg


def _idea(cfg):
    return [t for t in load_tasks(cfg) if t.is_idea][0]


def _items(cfg):
    return EX.read_idea_questions(cfg, _idea(cfg))


# --- library ------------------------------------------------------------------------------

def test_set_and_clear_a_rationale(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    EX.set_question_body(cfg, "IDEA-001", 2, WHY)
    assert _items(cfg)[1]["body"] == WHY
    assert "body" not in _items(cfg)[0]

    EX.set_question_body(cfg, "IDEA-001", 2, "")
    assert "body" not in _items(cfg)[1]


def test_rationale_on_an_already_resolved_question(tmp_path, monkeypatch):
    """The case a resolve-time prompt could never reach."""
    cfg = _seed(tmp_path, monkeypatch)
    EX.resolve_question(cfg, "IDEA-001", 2)
    EX.set_question_body(cfg, "IDEA-001", 2, WHY)
    item = _items(cfg)[1]
    assert item["state"] == "resolved" and item["body"] == WHY


@pytest.mark.parametrize("index", [0, -1, 3, 99, "2", None, True])
def test_bad_index_raises_with_no_write(tmp_path, monkeypatch, index):
    cfg = _seed(tmp_path, monkeypatch)
    before = Path(cfg["org_ideas_file"]).read_text()
    with pytest.raises(ValueError, match="no question"):
        EX.set_question_body(cfg, "IDEA-001", index, WHY)
    assert Path(cfg["org_ideas_file"]).read_text() == before


@pytest.mark.parametrize("argv", [
    ["--resolve", "2"], ["--unresolve", "2"],
    ["--edit", "2", "--text", "reworded?"], ["--priority", "2", "--pri", "A"],
])
def test_rationale_survives_other_mutations(tmp_path, monkeypatch, argv):
    cfg = _seed(tmp_path, monkeypatch)
    EX.set_question_body(cfg, "IDEA-001", 2, WHY)
    assert CliRunner().invoke(cli, ["idea", "questions", "IDEA-001"] + argv).exit_code == 0
    assert _items(cfg)[1]["body"] == WHY


def test_deleting_a_question_takes_its_rationale(tmp_path, monkeypatch):
    """Structural attachment is why stable ids were not needed: no dangling link to clean up."""
    cfg = _seed(tmp_path, monkeypatch)
    EX.set_question_body(cfg, "IDEA-001", 2, WHY)
    EX.delete_question(cfg, "IDEA-001", 2)
    assert [i["text"] for i in _items(cfg)] == ["is textual pinned?"]
    assert WHY not in EX.read_idea_enrichment(cfg, _idea(cfg))["questions"]


# --- CLI ------------------------------------------------------------------------------------

def test_cli_body_round_trip(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    assert run(cli, ["idea", "questions", "IDEA-001", "--body", "2", "--text", WHY]).exit_code == 0
    assert _items(cfg)[1]["body"] == WHY
    assert run(cli, ["idea", "questions", "IDEA-001", "--body", "2", "--text", ""]).exit_code == 0
    assert "body" not in _items(cfg)[1]


def test_cli_body_requires_text(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["idea", "questions", "IDEA-001", "--body", "2"])
    assert r.exit_code != 0 and "needs --text" in r.output


def test_cli_body_and_edit_stay_distinct(tmp_path, monkeypatch):
    """Both take --text; passing both must be rejected, not silently pick one."""
    _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["idea", "questions", "IDEA-001",
                                 "--body", "1", "--edit", "2", "--text", "x"])
    assert r.exit_code != 0


def test_nw_idea_show_renders_the_rationale(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    EX.set_question_body(cfg, "IDEA-001", 2, WHY)
    out = CliRunner().invoke(cli, ["idea", "show", "IDEA-001"]).output
    assert "*** OPEN which theme?" in out
    assert WHY in out
    # indented under its question, not flush with the headline
    assert f"    {WHY}" in out


def test_json_carries_the_body(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    EX.set_question_body(cfg, "IDEA-001", 2, WHY)
    payload = json.loads(CliRunner().invoke(
        cli, ["idea", "show", "IDEA-001", "--json"]).output)
    assert payload["question_items"][1]["body"] == WHY
    assert payload["open_questions"] == 2, "a rationale must not inflate the question count"


# --- desk ---------------------------------------------------------------------------------------

def test_apply_mutation_writes_a_rationale(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    M.apply_mutation(cfg, "question_body", "IDEA-001", index=2, text=WHY)
    assert _items(cfg)[1]["body"] == WHY


def test_desk_owns_no_body_write_rule():
    app_src = (ROOT / "src" / "nightwright" / "tui" / "app.py").read_text()
    assert "set_question_body" not in app_src        # goes through apply_mutation
    assert "question_body" in app_src


def test_modal_shows_the_rationale_under_its_question(tmp_path, monkeypatch):
    from test_tui_desk import _grid

    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    EX.resolve_question(cfg, "IDEA-001", 2)
    EX.set_question_body(cfg, "IDEA-001", 2, WHY)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(96, 24)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.pause()
            screen = "\n".join(_grid(app))
            assert "which theme?" in screen
            assert "ANSI, because hex" in screen
            assert "r rationale" in screen

    asyncio.run(drive())


def test_r_writes_then_edits_a_rationale(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(96, 24)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("r")                    # question #1
            await pilot.pause()
            for ch in "because":
                await pilot.press(ch)
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert _items(cfg)[0]["body"] == "because"

            await pilot.press("r")                    # edit it — prompt pre-filled
            await pilot.pause()
            for ch in " so":
                await pilot.press("space" if ch == " " else ch)
            await pilot.press("ctrl+s")
            await pilot.pause()
            # Appends rather than prepends: the pre-filled editor opens with the cursor at the
            # end. A first cut put it at 0 and produced "sobecause".
            assert _items(cfg)[0]["body"] == "because so"

    asyncio.run(drive())


def test_enter_still_resolves_with_no_prompt(tmp_path, monkeypatch):
    """`Enter` must stay a single keystroke — the whole reason rationale got its own key."""
    from nightwright.tui.app import BodyPrompt, IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(96, 24)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert _items(cfg)[0]["state"] == "resolved"
            assert not isinstance(app.screen, BodyPrompt), "resolve should not prompt"

    asyncio.run(drive())


def test_escaping_the_rationale_prompt_writes_nothing(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(96, 24)) as pilot:
            await pilot.pause()
            before = Path(cfg["org_ideas_file"]).read_text()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            for ch in "discard":
                await pilot.press(ch)
            await pilot.press("escape")
            await pilot.pause()
            assert Path(cfg["org_ideas_file"]).read_text() == before

    asyncio.run(drive())
