"""Desk table overhaul (SPEC-0110): shared planner, auto-drop, filters, sort, wide mode.

Two invariants carry the design: the desk and the CLI share **one** column planner, and the
in-app filters produce **the same ids** as the equivalent `nw ideas` invocation. If either
drifts, the desk is quietly lying about the corpus.
"""
import asyncio
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from waits import pilot_until

from nightwright import report as R
from nightwright import tui as T
from nightwright.cli import cli
from nightwright.tui import model as M

ROOT = Path(__file__).resolve().parent.parent

needs_textual = pytest.mark.skipif(not T.textual_available(),
                                   reason="optional tui extra not installed")


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


# --- filter expression parsing ------------------------------------------------------------

def test_parse_filter_expr_round_trip():
    filters, problems = M.parse_filter_expr("project=Example kind=bug")
    assert filters == {"project": "Example", "kind": "bug"}
    assert problems == []
    assert M.format_filter_expr(filters) == "kind=bug project=Example"


def test_parse_filter_expr_reports_problems_rather_than_dropping_them():
    """A filter that quietly does nothing looks identical to one that matched nothing."""
    filters, problems = M.parse_filter_expr("nonsense project=Example bogus=x state=")
    assert filters == {"project": "Example"}
    assert any("not key=value" in p for p in problems)
    assert any("unknown filter" in p for p in problems)
    assert any("no value" in p for p in problems)


def test_parse_filter_expr_empty_clears():
    assert M.parse_filter_expr("") == ({}, [])
    assert M.format_filter_expr({}) == ""


def test_filter_vocabulary_is_exactly_the_collectors():
    """No filter the CLI cannot express, and no matching logic in the UI."""
    import inspect

    params = inspect.signature(R.collect_idea_tasks).parameters
    for key in M.FILTER_KEYS:
        assert key in params, f"{key} is not a collect_idea_tasks argument"


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


@needs_textual
def test_auto_drop_sheds_columns_narrow_and_keeps_all_wide():
    from nightwright.tui.app import DESK_COLUMNS, DROP_ORDER, HEADING_FLOOR, plan_desk_columns

    prows = [{"id": "  IDEA-143", "state": "INCUBATE", "pri": "B", "q": "3",
              "kind": "🐛", "project": "Meta-Tools", "updated": "12m"}]

    wide_keys, _w, wide_budget = plan_desk_columns(prows, 200)
    assert wide_keys == DESK_COLUMNS, "a wide pane should keep every column"
    assert wide_budget > HEADING_FLOOR

    narrow_keys, _w, narrow_budget = plan_desk_columns(prows, 60)
    assert set(narrow_keys) < set(DESK_COLUMNS), "a narrow pane should shed columns"
    assert narrow_budget >= HEADING_FLOOR, "heading must keep its floor after dropping"
    # dropped in the declared order — kind goes before pri, pri before state
    dropped = [k for k in DROP_ORDER if k not in narrow_keys]
    assert dropped[0] == "kind"


@needs_textual
def test_id_and_updated_are_never_dropped():
    """`id` is how you refer to a row; `updated` drives the default sort."""
    from nightwright.tui.app import plan_desk_columns

    prows = [{k: "x" for k in ("id", "state", "pri", "q", "kind", "project", "updated")}]
    for width in (20, 30, 40, 60, 100):
        keys, _w, _b = plan_desk_columns(prows, width)
        assert "id" in keys
        assert "updated" in keys or len(keys) == 1


# --- status line ------------------------------------------------------------------------------

def test_status_line_shows_filters_sort_and_mode():
    line = M.status_line([], all_done=False, query=None,
                         filters={"project": "Example"}, sort="priority", desc=True, mode="wide")
    assert "project=Example" in line
    assert "sort priority desc" in line
    assert "wide" in line


def test_status_line_announces_the_query_visibility_override():
    """SPEC-0110 precedence: a query widens the corpus, overriding the non-active toggle
    (SPEC-0086 'implies full corpus'). The one surprising interaction must be visible."""
    line = M.status_line([], all_done=False, query="textual")
    assert "all states (search)" in line
    assert "query 'textual'" in line


def test_status_line_default_is_unchanged_in_shape():
    assert M.status_line([], all_done=False, query=None) == "0 shown · active only · sort freshness"


# --- filters match the CLI ---------------------------------------------------------------------

def test_in_app_filters_match_the_equivalent_cli_invocation(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    for expr, argv in (
        ("project=Example", ["ideas", "--project", "Example", "--json"]),
        ("project=Meta-Tools", ["ideas", "--project", "Meta-Tools", "--json"]),
        ("kind=bug", ["ideas", "--kind", "bug", "--json"]),
        ("priority=A", ["ideas", "--priority", "A", "--json"]),
    ):
        filters, problems = M.parse_filter_expr(expr)
        assert problems == []
        desk = [r.id for r, _ in M.load_rows(cfg, **filters)]
        out = CliRunner().invoke(cli, argv)
        cli_ids = [row["id"] for row in json.loads(out.output)["ideas"]]
        assert desk == cli_ids, f"{expr} disagrees with {' '.join(argv)}"


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


# --- pilot ---------------------------------------------------------------------------------------

@needs_textual
def test_filter_bar_applies_and_clears(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            assert len(app._pairs) == 3
            await pilot.press("f")
            await pilot.pause()
            for ch in "project=Example":
                await pilot.press(*( ["equals_sign"] if ch == "=" else [ch] ))
            await pilot.press("enter")
            await pilot.pause()
            assert app.filters == {"project": "Example"}
            assert [r.id for r, _ in app._pairs] == ["IDEA-001"]

            await pilot.press("f")
            await pilot.pause()
            for _ in range(40):
                await pilot.press("backspace")
            await pilot.press("enter")
            await pilot.pause()
            assert app.filters == {}
            assert len(app._pairs) == 3

    asyncio.run(drive())


@needs_textual
def test_sort_hotkeys_cycle_and_reverse(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert app.sort == "priority"
            forward = [r.id for r, _ in app._pairs]
            await pilot.press("O")
            await pilot.pause()
            assert app.desc is True
            assert [r.id for r, _ in app._pairs] == list(reversed(forward))

            # SPEC-0113 widened the cycle from two fields to the whole registry, so `o` walks
            # IDEA_SORT_NAMES in order and wraps at the end rather than toggling a pair.
            assert M.SORT_FIELDS == R.IDEA_SORT_NAMES
            for expected in M.SORT_FIELDS[2:] + M.SORT_FIELDS[:1]:
                await pilot.press("o")
                await pilot.pause()
                assert app.sort == expected

    asyncio.run(drive())


@needs_textual
def test_wide_mode_hides_detail_and_replans_columns(tmp_path, monkeypatch):
    from nightwright.tui.app import DESK_COLUMNS, IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            split_cols = app._columns
            assert app.query_one("#detail-pane").display is True

            await pilot.press("z")
            await pilot.pause()
            await pilot.pause()
            assert app.wide is True
            assert app.query_one("#detail-pane").display is False
            assert app._columns == DESK_COLUMNS, "wide mode should regain every column"
            assert len(app._columns) > len(split_cols)

            await pilot.press("z")
            await pilot.pause()
            await pilot.pause()
            assert app.query_one("#detail-pane").display is True

    asyncio.run(drive())


@needs_textual
def test_project_column_is_visible_in_the_desk(tmp_path, monkeypatch):
    """The reported gap: you could not see which project an idea belonged to."""
    from test_tui_desk import _grid

    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(140, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert "project" in app._columns
            screen = "\n".join(_grid(app))
            assert "Example" in screen
            # a long name is clipped to the planned column width in the split pane…
            assert "Meta-" in screen

            await pilot.press("z")             # …and shown in full once wide
            await pilot_until(pilot, lambda: "Meta-Tools" in "\n".join(_grid(app)))
            assert "Meta-Tools" in "\n".join(_grid(app))

    asyncio.run(drive())
