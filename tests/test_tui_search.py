"""Desk search bar: visibility + filter-as-you-type (SPEC-0105).

The bug these cover shipped green under SPEC-0100 because that test asserted the resulting
*query state* and never that the box was on screen. Every test here that could pass on an
invisible widget asserts the **rendered grid** instead.
"""
import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from waits import pilot_until

from nightwright.cli import cli
import pytest

pytestmark = pytest.mark.tui

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


def _seed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "aardvark migration plan"]).exit_code == 0
    assert run(cli, ["idea", "buffalo pipeline rewrite"]).exit_code == 0
    assert run(cli, ["idea", "cheetah dashboard"]).exit_code == 0
    return cfg


async def _settle(pilot):
    """Wait past the debounce, then let the reload paint."""
    from nightwright.tui.app import SEARCH_DEBOUNCE

    await pilot.pause(SEARCH_DEBOUNCE + 0.3)
    await pilot.pause()
    await pilot.pause()


async def _type(pilot, text):
    for ch in text:
        await pilot.press("space" if ch == " " else ch)


# --- visibility (the actual bug) ---------------------------------------------------------

def test_typed_text_is_actually_on_screen(tmp_path, monkeypatch):
    """The regression SPEC-0100 missed: the box collected keystrokes no terminal ever showed.

    The typed string must match **no idea**, and the assertion is scoped to the search box's
    own row. A first cut typed "buffalo" and asserted it appeared anywhere on screen — which
    passed on the broken build, because the seeded idea *"buffalo pipeline rewrite"* put that
    word in the list. A search term that collides with the data cannot prove the box renders.
    """
    from test_tui_desk import _grid

    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await _type(pilot, "zqxjk")
            await pilot.pause()
            grid = _grid(app)
            box_y = app.query_one("#search").region.y
            assert box_y < len(grid), f"search box at row {box_y}, screen has {len(grid)} rows"
            assert "zqxjk" in grid[box_y], (
                f"search box row {box_y} does not show the typed text: {grid[box_y]!r}")

    asyncio.run(drive())


def test_search_hint_and_footer_each_own_a_row(tmp_path, monkeypatch):
    """Root cause was three widgets laid out on the same line, Footer painted over the rest."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            rows = {}
            for sel in ("#search", "#hint", "Footer"):
                region = app.query_one(sel).region
                rows[sel] = (region.y, region.height)
            ys = [y for y, _ in rows.values()]
            assert len(set(ys)) == 3, f"widgets share a row: {rows}"
            assert rows["#search"][1] == 1, f"search box is {rows['#search'][1]} rows tall"
            screen_h = app.screen.size.height
            for sel, (y, h) in rows.items():
                assert y + h <= screen_h, f"{sel} extends past the screen edge: {rows}"

    asyncio.run(drive())


# --- filter as you type -------------------------------------------------------------------

def test_typing_filters_without_pressing_enter(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 20)) as pilot:
            assert await pilot_until(pilot, lambda: len(app._pairs) == 3)
            await pilot.press("slash")
            await pilot.pause()
            await _type(pilot, "buffalo")
            await _settle(pilot)
            assert app.query == "buffalo"
            # SPEC-0495: hits are a cross-project list over the board; the board keeps all.
            assert await pilot_until(pilot, lambda: [r.id for r in app._hits] == ["IDEA-002"])
            assert len(app._pairs) == 3

    asyncio.run(drive())


def test_clearing_the_query_live_restores_the_full_list(tmp_path, monkeypatch):
    """Backspacing out of a search must not strand the user on an empty result."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await _type(pilot, "buffalo")
            await _settle(pilot)
            assert await pilot_until(pilot, lambda: len(app._hits) == 1)

            for _ in range(len("buffalo")):
                await pilot.press("backspace")
            await _settle(pilot)
            assert app.query == ""
            assert app._hits == [] and len(app._pairs) == 3
            assert "SEARCH" not in "".join(str(n.label) for n in app.query_one("#board").root.children)

    asyncio.run(drive())


def test_a_burst_of_typing_costs_one_search(tmp_path, monkeypatch):
    """Debounce, not per-keystroke: each character restarts the timer."""
    from nightwright.tui import model as M
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    calls = []
    real = M.load_rows

    def counting(cfg_, **kw):
        if kw.get("query"):
            calls.append(kw["query"])
        return real(cfg_, **kw)

    monkeypatch.setattr(M, "load_rows", counting)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await _type(pilot, "buffalo")        # 7 keystrokes, no pause between
            await _settle(pilot)
            assert calls == ["buffalo"], f"expected one search, got {calls}"

    asyncio.run(drive())


# --- existing behaviour must survive -------------------------------------------------------

def test_enter_still_applies_and_closes_the_box(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await _type(pilot, "cheetah")
            await pilot.press("enter")
            await pilot.pause()
            assert app.query == "cheetah"
            assert await pilot_until(pilot, lambda: [r.id for r in app._hits] == ["IDEA-003"])
            assert not app.query_one("#search").has_class("visible")

    asyncio.run(drive())


def test_escape_cancels_a_live_filter(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await _type(pilot, "buffalo")
            await _settle(pilot)
            assert await pilot_until(pilot, lambda: len(app._hits) == 1)
            await pilot.press("escape")
            await pilot.pause()
            assert app.query == ""
            assert app._hits == [] and len(app._pairs) == 3
            assert not app.query_one("#search").has_class("visible")

    asyncio.run(drive())


def test_a_pending_debounce_does_not_fire_after_escape(tmp_path, monkeypatch):
    """Esc must cancel the timer, not merely reset the query before it lands."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await _type(pilot, "buffalo")
            await pilot.press("escape")          # before the debounce elapses
            await _settle(pilot)
            assert app.query == ""
            assert app._hits == []

    asyncio.run(drive())


# --- one search definition ------------------------------------------------------------------

def test_the_desk_defines_no_search_of_its_own():
    """Live filtering must reuse `search_idea_tasks`, not fork ranking into the UI."""
    app_src = (ROOT / "src" / "nightwright" / "tui" / "app.py").read_text()
    model_src = (ROOT / "src" / "nightwright" / "tui" / "model.py").read_text()
    assert "search_idea_tasks" in model_src
    for banned in ("score_idea_match", "tokenize_idea_query", "idea_match_snippet"):
        assert banned not in app_src, f"{banned} reimplemented in the desk UI"


