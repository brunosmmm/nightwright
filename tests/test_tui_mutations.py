"""Explore mutations in the TUI desk (SPEC-0102).

Two layers: `nightwright.tui.model.apply_mutation` (no Textual — the dispatch every desk write goes
through) and pilot drives of the real key bindings and modals.
"""
import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import tui as T
from nightwright.cli import cli
from nightwright.org import load_tasks
from nightwright.tui import model as M

ROOT = Path(__file__).resolve().parent.parent

needs_textual = pytest.mark.skipif(not T.textual_available(),
                                   reason="optional tui extra not installed")


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
    assert CliRunner().invoke(cli, ["idea", "a desk idea"]).exit_code == 0
    return cfg


def _idea(cfg, idea_id="IDEA-001"):
    return [t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id][0]


def _questions(cfg, idea_id="IDEA-001"):
    return [(i["state"], i["text"]) for i in EX.read_idea_questions(cfg, _idea(cfg, idea_id))]


# --- dispatch --------------------------------------------------------------------------

def test_apply_mutation_covers_every_desk_action(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    M.apply_mutation(cfg, "log", "IDEA-001", note="a note")
    M.apply_mutation(cfg, "summary", "IDEA-001", text="a summary")
    M.apply_mutation(cfg, "question_add", "IDEA-001", text="q one?")
    M.apply_mutation(cfg, "question_add", "IDEA-001", text="q two?")
    M.apply_mutation(cfg, "question_resolve", "IDEA-001", index=1)
    M.apply_mutation(cfg, "question_unresolve", "IDEA-001", index=1)
    M.apply_mutation(cfg, "question_edit", "IDEA-001", index=2, text="q two reworded?")
    M.apply_mutation(cfg, "question_delete", "IDEA-001", index=1)
    M.apply_mutation(cfg, "retitle", "IDEA-001", title="a renamed desk idea")
    M.apply_mutation(cfg, "explored", "IDEA-001")

    detail = M.load_detail(cfg, _idea(cfg))
    assert detail.summary == "a summary"
    assert any("a note" in e for e in detail.log)
    assert detail.heading == "a renamed desk idea"
    assert _questions(cfg) == [("open", "q two reworded?")]
    assert EX.explored_count(_idea(cfg)) == 1


def test_apply_mutation_rejects_an_unknown_action(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="unknown desk action"):
        M.apply_mutation(cfg, "promote", "IDEA-001")


def test_apply_mutation_state_to_shipped(tmp_path, monkeypatch):
    """SPEC-0135: desk metadata state → set_idea_state."""
    cfg = _seed(tmp_path, monkeypatch)
    cfg["org_idea_keywords"] = [
        "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED"]
    cfg["org_ideas_archive_file"] = str(Path(cfg["data_dir"]) / "org" / "ideas-archive.org")
    M.apply_mutation(cfg, "state", "IDEA-001", value="SHIPPED")
    assert _idea(cfg).state == "SHIPPED"


def test_capture_idea_returns_the_new_id(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    new_id = M.capture_idea(cfg, "captured from the desk")
    assert new_id == "IDEA-002"
    assert _idea(cfg, "IDEA-002").heading == "captured from the desk"
    with pytest.raises(ValueError, match="must not be empty"):
        M.capture_idea(cfg, "   ")


def test_mutations_are_visible_to_the_classic_cli(tmp_path, monkeypatch):
    """AC: writes persist to org and `nw idea show` agrees."""
    cfg = _seed(tmp_path, monkeypatch)
    M.apply_mutation(cfg, "summary", "IDEA-001", text="written from the desk")
    M.apply_mutation(cfg, "question_add", "IDEA-001", text="still open?")
    r = CliRunner().invoke(cli, ["idea", "show", "IDEA-001"])
    assert r.exit_code == 0
    assert "written from the desk" in r.output
    assert "still open?" in r.output


# --- pilot: desk bindings ---------------------------------------------------------------

async def _type(pilot, text):
    for ch in text:
        await pilot.press("space" if ch == " " else ch)


@needs_textual
def test_log_binding_writes_and_refreshes(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            await _type(pilot, "from the desk")
            await pilot.press("enter")
            await pilot.pause()
            detail = M.load_detail(cfg, _idea(cfg))
            assert any("from the desk" in e for e in detail.log)

    asyncio.run(drive())


@needs_textual
def test_capture_binding_adds_an_idea_and_selects_it(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            await _type(pilot, "brand new")
            await pilot.press("enter")
            await pilot.pause()
            assert _idea(cfg, "IDEA-002").heading == "brand new"
            assert "IDEA-002" in [r.id for r, _ in app._pairs]
            assert app.selected_id == "IDEA-002"

    asyncio.run(drive())


@needs_textual
def test_escape_cancels_a_prompt_without_writing(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            before = Path(_idea(cfg).file).read_text()
            await pilot.press("l")
            await pilot.pause()
            await _type(pilot, "discard me")
            await pilot.press("escape")
            await pilot.pause()
            assert Path(_idea(cfg).file).read_text() == before

    asyncio.run(drive())


@needs_textual
def test_explored_binding_increments(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert EX.explored_count(_idea(cfg)) == 1

    asyncio.run(drive())


@needs_textual
def test_summary_modal_saves_with_ctrl_s(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await _type(pilot, "typed body")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert M.load_detail(cfg, _idea(cfg)).summary == "typed body"

    asyncio.run(drive())


# --- pilot: questions screen ------------------------------------------------------------

@needs_textual
def test_questions_screen_full_crud(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    EX.add_question(cfg, "IDEA-001", "first?")
    EX.add_question(cfg, "IDEA-001", "second?")

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()

            await pilot.press("enter")                  # resolve #1
            await pilot.pause()
            assert _questions(cfg)[0][0] == "resolved"

            await pilot.press("enter")                  # toggle back to open
            await pilot.pause()
            assert _questions(cfg)[0][0] == "open"

            await pilot.press("j")                      # move to #2, edit it
            await pilot.press("e")
            await pilot.pause()
            for _ in range(len("second?")):
                await pilot.press("backspace")
            await _type(pilot, "reworded")
            await pilot.press("enter")
            await pilot.pause()
            assert _questions(cfg)[1][1] == "reworded"

            await pilot.press("a")                      # add a third
            await pilot.pause()
            await _type(pilot, "third")
            await pilot.press("enter")
            await pilot.pause()
            assert len(_questions(cfg)) == 3

            await pilot.press("d")                      # delete the highlighted one
            await pilot.pause()
            assert len(_questions(cfg)) == 2

            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(drive())


@needs_textual
def test_questions_screen_survives_a_bad_index(tmp_path, monkeypatch):
    """Deleting the last question then acting again must not raise out of the screen."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    EX.add_question(cfg, "IDEA-001", "only one?")

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert _questions(cfg) == []
            for key in ("d", "e", "enter"):              # nothing selected → no-ops, no crash
                await pilot.press(key)
                await pilot.pause()
            assert _questions(cfg) == []
            assert app.is_running

    asyncio.run(drive())


# --- drift -------------------------------------------------------------------------------

@needs_textual
def test_drift_is_reported_and_non_fatal(tmp_path, monkeypatch):
    """A write that raises must leave the app running and reload from disk."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    def boom(*a, **kw):
        raise ValueError("drift: expected state 'IDEA', found 'INCUBATE'")

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(M, "apply_mutation", boom)
            await pilot.press("x")                       # mark-explored → raises
            await pilot.pause()
            assert app.is_running
            assert app._pairs                            # reloaded, not left empty

    asyncio.run(drive())
