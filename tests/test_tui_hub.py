"""Read-only hub triage tab (SPEC-0104).

The tab is a view over the same `hub_payload` (`nw.hub.v1`) agents consume — SPEC-0052's
"hub is not a write surface" still stands, so the only interaction is drilling an *idea* row
into the ideas desk, or being shown a CLI hint for a spec row.
"""
import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from waits import pilot_until
from nightwright.cli import cli
from nightwright.tui import model as M
import pytest

pytestmark = pytest.mark.tui

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
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
        "specs_dir": str(specs),
        "outbox_dir": str(tmp_path / "outbox"),
    }


def _seed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "a hub idea"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "open one?"]).exit_code == 0
    assert run(cli, ["add", "a task for today"]).exit_code == 0
    (Path(cfg["specs_dir"]) / "0001-an-active-spec.md").write_text(
        "---\nid: SPEC-0001\ntitle: \"An active spec\"\nstatus: accepted\nowner: user\n"
        "created: 2026-07-30\nupdated: 2026-07-30\nkind: feature\n---\n\n## Context\n\nx\n")
    return cfg


# --- view model -------------------------------------------------------------------------

def test_load_hub_flattens_every_section(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    lines, summary = M.load_hub(cfg)
    sections = {ln.section for ln in lines}
    assert "ideas" in sections
    assert "internal specs" in sections
    assert "1 ideas" in summary and "1 internal" in summary


def test_only_idea_rows_are_drillable(tmp_path, monkeypatch):
    """A spec row must not pretend to open something the ideas desk can show."""
    cfg = _seed(tmp_path, monkeypatch)
    lines, _ = M.load_hub(cfg)
    for line in lines:
        if line.section == "ideas":
            assert line.idea_id
        else:
            assert line.idea_id is None


def test_spec_rows_carry_a_cli_hint_not_an_action(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    lines, _ = M.load_hub(cfg)
    internal = [ln for ln in lines if ln.section == "internal specs"]
    assert internal and internal[0].hint.startswith("nw spec generate SPEC-0001")


def test_idea_rows_surface_the_spec_0103_fields(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    lines, _ = M.load_hub(cfg)
    idea = [ln for ln in lines if ln.section == "ideas"][0]
    assert "1 open q" in idea.meta


def test_load_hub_honours_the_tasks_slice(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    none_sections = {ln.section for ln in M.load_hub(cfg, tasks_slice="none")[0]}
    assert not any(s.startswith("tasks") for s in none_sections)
    open_sections = {ln.section for ln in M.load_hub(cfg, tasks_slice="open")[0]}
    assert any(s.startswith("tasks") for s in open_sections)


def test_load_hub_does_not_write(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    org = Path(cfg["org_ideas_file"])
    before = org.read_text()
    M.load_hub(cfg)
    M.load_hub(cfg, tasks_slice="open")
    assert org.read_text() == before


def test_hub_json_for_agents_is_unchanged(tmp_path, monkeypatch):
    """AC: the tab is additive; `nw hub --json` still answers exactly as before."""
    import json

    _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["hub", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["schema"] == "nw.hub.v1"
    assert {"ideas", "internal", "outbound", "stale_ideas"} <= set(payload)


# --- pilot ------------------------------------------------------------------------------

def test_hub_tab_renders_and_drills_into_an_idea(tmp_path, monkeypatch):
    from test_tui_desk import _grid

    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.pause()
            assert app.query_one("#tabs").active == "tab-hub"
            screen = "\n".join(_grid(app))
            assert "a hub idea" in screen
            assert "An active spec" in screen
            assert "1 ideas" in screen               # summary line

            await pilot.press("enter")               # drill the highlighted idea row
            await pilot.pause()
            assert app.query_one("#tabs").active == "tab-ideas"
            assert app.selected_id == "IDEA-001"

    asyncio.run(drive())


def test_hub_loads_when_activated_without_the_h_binding(tmp_path, monkeypatch):
    """SPEC-0108 regression: clicking the tab produced a blank pane.

    Loading used to hang off `action_toggle_hub`, so every route that was *not* the `h` key —
    a mouse click, Textual's own tab navigation — reached a pane that had never been populated.
    This drives the tab the way a click does, on a **fresh** app that has never seen `h`.
    """
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.pause()
            # An actual mouse click on the tab header, not `active = …`: the reported symptom
            # was "clicking the hub tab", so the test should click.
            tab = [w for w in app.screen.query("ContentTab") if "hub" in (w.id or "")][0]
            await pilot.click(tab)
            await pilot.pause()
            await pilot.pause()
            hub = app.query_one("#hub")
            assert hub.row_count > 0, "hub pane is empty when reached by clicking the tab"
            assert len(hub.columns) == 4, "hub never got its columns"
            assert hub.has_focus, "focus did not follow the tab on the click route"

    asyncio.run(drive())


def test_both_routes_agree(tmp_path, monkeypatch):
    """`h` and direct activation must produce the same populated hub."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def rows_via(route):
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.pause()
            if route == "key":
                await pilot.press("h")
            else:
                app.query_one("#tabs").active = "tab-hub"
            await pilot.pause()
            await pilot.pause()
            return app.query_one("#hub").row_count

    async def drive():
        return await rows_via("key"), await rows_via("click")

    by_key, by_click = asyncio.run(drive())
    assert by_key == by_click > 0, f"routes disagree: h={by_key} click={by_click}"


def test_returning_to_ideas_by_any_route_restores_focus(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.pause()
            app.query_one("#tabs").active = "tab-hub"
            await pilot.pause()
            app.query_one("#tabs").active = "tab-ideas"
            await pilot.pause()
            await pilot.pause()
            assert app.query_one("#board").has_focus

    asyncio.run(drive())


def test_hub_reflects_changes_made_since_launch(tmp_path, monkeypatch):
    """Reload on every activation: the desk mutates the very things the hub summarises."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            assert await pilot_until(pilot, lambda: app.query_one("#hub").row_count > 0)
            before = app.query_one("#hub").row_count

            await pilot.press("h")                     # back to ideas
            await pilot.pause()
            M.capture_idea(cfg, "a brand new idea")    # world changes underneath
            await pilot.press("h")
            assert await pilot_until(pilot, lambda: app.query_one("#hub").row_count == before + 1)

    asyncio.run(drive())


def test_external_change_poll_during_teardown_is_a_no_op(tmp_path, monkeypatch):
    """The 1.5 s external-change poll can fire while the app tears its DOM down (seen when a
    loaded host stretched a test past the interval): it must return, not raise NoMatches."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 26)) as pilot:
            assert await pilot_until(pilot, lambda: app._pairs)
        M.capture_idea(cfg, "changed underneath")              # the poll sees a new signature
        await app._check_external_changes()                    # ... after the DOM is gone

    asyncio.run(drive())


def test_hub_toggles_back_to_ideas(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            assert app.query_one("#tabs").active == "tab-hub"
            await pilot.press("h")
            await pilot.pause()
            assert app.query_one("#tabs").active == "tab-ideas"

    asyncio.run(drive())


def test_browsing_the_hub_writes_nothing(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.pause()
            before = Path(cfg["org_ideas_file"]).read_text()
            await pilot.press("h")
            await pilot.pause()
            for key in ("j", "j", "k", "enter"):
                await pilot.press(key)
                await pilot.pause()
            assert Path(cfg["org_ideas_file"]).read_text() == before

    asyncio.run(drive())


def test_hub_tab_has_no_promote_or_export_call():
    """AC: no promote/export mutation from the tab — hints are strings, not calls."""
    app_src = (ROOT / "src" / "nightwright" / "tui" / "app.py").read_text()
    model_src = (ROOT / "src" / "nightwright" / "tui" / "model.py").read_text()
    for banned in ("promote_idea", "scaffold_outbound", "generate_from_spec", "export_spec",
                   "seed_ideas_from_epic"):
        assert banned not in app_src, f"{banned} reachable from the desk"
        assert banned not in model_src, f"{banned} reachable from the desk model"


