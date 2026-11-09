"""Clock + open-question count in shared JSON, and the desk's metadata/clock actions
(SPEC-0103).

Epic Q3 ruled these signals belong in `nw.idea.v1` / row envelopes rather than a TUI-only
`Task` read, so agents get them without ever launching the desk. Fields are **additive** — the
schema string stays `nw.idea.v1`.
"""
import asyncio
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from waits import pilot_until

from nightwright import explore as EX
from nightwright import report as R
from nightwright.cli import cli
from nightwright.org import load_tasks
from nightwright.tui import model as M

pytestmark = pytest.mark.tui


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
        "workstreams": ["build", "research"],
    }


def _seed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "clocked idea"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "still open?"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "also open?"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "done one?"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--resolve", "3"]).exit_code == 0
    return cfg


def _idea(cfg, idea_id="IDEA-001"):
    return [t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id][0]


# --- nw.idea.v1 -------------------------------------------------------------------------

def test_idea_show_json_carries_clock_and_open_question_count(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["idea", "show", "IDEA-001", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["schema"] == "nw.idea.v1"          # additive: no version bump
    assert payload["open_questions"] == 2
    assert payload["clock_open"] is False
    assert payload["clocked_hours"] == 0.0


def test_clock_open_flips_with_clock_in_and_out(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke

    assert run(cli, ["idea", "clock-in", "IDEA-001"]).exit_code == 0
    payload = EX.idea_show_payload(cfg, _idea(cfg))
    assert payload["clock_open"] is True
    # an *open* interval must not inflate the total — same rule as report._clocked_hours
    assert payload["clocked_hours"] == 0.0

    assert run(cli, ["idea", "clock-out", "IDEA-001"]).exit_code == 0
    payload = EX.idea_show_payload(cfg, _idea(cfg))
    assert payload["clock_open"] is False
    assert payload["clocked_hours"] >= 0.0


def test_clocked_hours_sums_only_closed_intervals(tmp_path, monkeypatch):
    import datetime as dt

    from nightwright import org_write as W

    cfg = _seed(tmp_path, monkeypatch)
    base = dt.datetime(2026, 7, 30, 9, 0)
    W.add_clock_entry(cfg, "IDEA-001", base, base + dt.timedelta(hours=2))
    W.add_clock_entry(cfg, "IDEA-001", base.replace(hour=13),
                      base.replace(hour=13) + dt.timedelta(minutes=30))
    payload = EX.idea_show_payload(cfg, _idea(cfg))
    assert payload["clocked_hours"] == pytest.approx(2.5)
    assert payload["clock_open"] is False

    CliRunner().invoke(cli, ["idea", "clock-in", "IDEA-001"])
    payload = EX.idea_show_payload(cfg, _idea(cfg))
    assert payload["clock_open"] is True
    assert payload["clocked_hours"] == pytest.approx(2.5)   # open one adds nothing


# --- list rows --------------------------------------------------------------------------

def test_ideas_json_rows_carry_the_same_fields(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["ideas", "--json"])
    assert r.exit_code == 0
    rows = json.loads(r.output)["ideas"]
    row = [x for x in rows if x["id"] == "IDEA-001"][0]
    assert row["open_questions"] == 2
    assert row["clock_open"] is False
    assert row["clocked_hours"] == 0.0


def test_row_and_payload_agree_on_open_question_count(tmp_path, monkeypatch):
    """The two builders must not drift: one definition of "open"."""
    cfg = _seed(tmp_path, monkeypatch)
    task = _idea(cfg)
    assert R.idea_row(cfg, task)["open_questions"] == \
        EX.idea_show_payload(cfg, task)["open_questions"]


def test_next_and_hub_rows_inherit_the_fields(tmp_path, monkeypatch):
    """`nw next` / `nw hub` share `idea_row`, so they get the fields for free."""
    cfg = _seed(tmp_path, monkeypatch)
    for argv, key in ((["next", "--json"], "ideas"), (["hub", "--json"], "ideas")):
        r = CliRunner().invoke(cli, argv)
        assert r.exit_code == 0
        rows = json.loads(r.output)[key]
        assert rows and all("open_questions" in row for row in rows)


def test_an_idea_with_no_questions_reports_zero_not_missing(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert CliRunner().invoke(cli, ["idea", "no questions here"]).exit_code == 0
    payload = EX.idea_show_payload(cfg, _idea(cfg, "IDEA-002"))
    assert payload["open_questions"] == 0
    assert payload["clock_open"] is False


# --- desk wiring ------------------------------------------------------------------------

def test_desk_metadata_and_clock_actions(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    M.apply_mutation(cfg, "kind", "IDEA-001", value="bug")
    M.apply_mutation(cfg, "priority", "IDEA-001", value="A")
    M.apply_mutation(cfg, "workstream", "IDEA-001", value="build")
    M.apply_mutation(cfg, "tags", "IDEA-001", tags=["tui"])
    M.apply_mutation(cfg, "clock_in", "IDEA-001")

    detail = M.load_detail(cfg, _idea(cfg))
    assert detail.clock_open is True
    payload = EX.idea_show_payload(cfg, _idea(cfg))
    assert payload["kind"] == "bug"
    assert payload["priority"] == "A"
    assert payload["workstream"] == "build"
    assert payload["tags"] == ["tui"]

    M.apply_mutation(cfg, "clock_out", "IDEA-001")
    assert M.load_detail(cfg, _idea(cfg)).clock_open is False


def test_desk_close_action(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    M.apply_mutation(cfg, "close", "IDEA-001", outcome="drop", reason="superseded by the desk")
    assert _idea(cfg).state == "DROPPED"


def test_desk_rows_expose_clock_and_open_questions(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    row = M.load_rows(cfg)[0][0]
    assert row.open_questions == 2
    assert row.clock_open is False
    M.apply_mutation(cfg, "clock_in", "IDEA-001")
    assert M.load_rows(cfg)[0][0].clock_open is True


def test_clock_toggle_binding_is_bidirectional(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            assert EX.idea_show_payload(cfg, _idea(cfg))["clock_open"] is True
            await pilot.press("i")
            await pilot.pause()
            assert EX.idea_show_payload(cfg, _idea(cfg))["clock_open"] is False

    asyncio.run(drive())


def test_metadata_screen_sets_a_value(tmp_path, monkeypatch):
    from nightwright.tui.app import ChoiceScreen, IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            assert await pilot_until(pilot, lambda: app.selected_id)
            await pilot.press("m")            # state / kind / priority / …
            assert await pilot_until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await pilot.press("j")
            await pilot.press("j")            # state → kind → priority
            await pilot.press("enter")
            assert await pilot_until(
                pilot, lambda: isinstance(app.screen, ChoiceScreen)
                and str(app.screen.title_text).startswith("priority"))
            await pilot.press("j")            # A → B
            await pilot.press("enter")
            assert await pilot_until(pilot, lambda: _idea(cfg).priority == "B")

    asyncio.run(drive())


def test_desk_shows_open_question_count_and_clock_marker(tmp_path, monkeypatch):
    from test_tui_desk import _grid

    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 24)) as pilot:
            def shown(*needles):
                return all(n in "\n".join(_grid(app)) for n in needles)

            await pilot_until(pilot, lambda: shown("2 open"))
            assert "2 open" in "\n".join(_grid(app))     # detail questions rule
            await pilot.press("i")                       # clock in → marker appears
            await pilot_until(pilot, lambda: shown("◕", "clocked in"))
            screen = "\n".join(_grid(app))
            assert "◕" in screen
            assert "clocked in" in screen

    asyncio.run(drive())

