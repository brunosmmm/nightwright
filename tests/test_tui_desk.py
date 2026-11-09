"""Read-only ideas desk (SPEC-0100): view models on any install, pilot smoke with the extra.

The `nightwright.tui.model` half imports no Textual, so the desk's data layer is covered on a base
install. The widget half is skipped without the `tui` extra.
"""
import asyncio
import html
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from waits import pilot_until

from nightwright import tui as T
from nightwright.cli import cli
from nightwright.org import load_tasks
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
        "data_dir": str(tmp_path / "data"),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
        "config_dir": str(tmp_path / "config"),
    }


def _seed(tmp_path, monkeypatch):
    """Two ideas: one enriched + active, one DROPPED (non-active)."""
    cfg = _cfg(tmp_path)
    (Path(cfg["data_dir"])).mkdir(exist_ok=True)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "desk needs panes"]).exit_code == 0
    assert run(cli, ["idea", "second thing entirely"]).exit_code == 0
    assert run(cli, ["idea", "summary", "IDEA-001", "--set", "Two panes beat one table"]).exit_code == 0
    assert run(cli, ["idea", "log", "IDEA-001", "--note", "first pass"]).exit_code == 0
    assert run(cli, ["idea", "log", "IDEA-001", "--note", "second pass"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "which pane gets focus?"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "fixed or proportional?"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--resolve", "2"]).exit_code == 0
    assert run(cli, ["state", "IDEA-002", "DROPPED"]).exit_code == 0
    return cfg


def _task(cfg, idea_id):
    return [t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id][0]


# --- log splitting ---------------------------------------------------------------------

def test_split_log_returns_entries_not_characters():
    """Regression: `idea_show_payload`'s log is a *string* body, so list() would explode it."""
    body = "*** [2026-07-30 Thu 17:00]\nnewest note\n*** [2026-07-29 Wed 09:00]\nolder note"
    assert M.split_log(body) == ["[2026-07-30 Thu 17:00]\nnewest note",
                                 "[2026-07-29 Wed 09:00]\nolder note"]


def test_split_log_keeps_a_legacy_plain_body():
    assert M.split_log("just prose, no headline") == ["just prose, no headline"]


def test_split_log_empty():
    assert M.split_log("") == []
    assert M.split_log("   \n\n") == []


# --- list rows -------------------------------------------------------------------------

def test_rows_default_to_active_only_and_toggle_reveals_non_active(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    active = M.load_rows(cfg)
    assert [r.id for r, _ in active] == ["IDEA-001"]

    everything = M.load_rows(cfg, all_done=True)
    ids = [r.id for r, _ in everything]
    assert set(ids) == {"IDEA-001", "IDEA-002"}
    dropped = [r for r, _ in everything if r.id == "IDEA-002"][0]
    assert dropped.is_done and dropped.state == "DROPPED"


def test_load_rows_dedupes_duplicate_ids_across_org_files(tmp_path, monkeypatch):
    """Copied rotation siblings under org_files must not yield DuplicateKey in the desk."""
    cfg = _cfg(tmp_path)
    org = Path(cfg["org_files"][0])
    ideas = org / "ideas.org"
    hist = org / "ideas-20260830.org"
    body = """#+TODO: IDEA INCUBATE | PROMOTED DROPPED
* INCUBATE same id in two files
  :PROPERTIES:
  :ID: IDEA-001
  :END:
"""
    ideas.write_text(body, encoding="utf-8")
    hist.write_text(body.replace("same id in two files", "historical copy"), encoding="utf-8")
    cfg["org_files"] = [str(org)]
    cfg["org_ideas_file"] = str(ideas)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    rows = M.load_rows(cfg, all_done=True)
    ids = [r.id for r, _ in rows]
    assert ids.count("IDEA-001") == 1
    row = next(r for r, _ in rows if r.id == "IDEA-001")
    assert "same id in two files" in row.heading


def test_rows_agree_with_the_classic_collector(tmp_path, monkeypatch):
    """Parity guard: the desk must never disagree with `nw ideas` on order or membership."""
    from nightwright import report as R

    cfg = _seed(tmp_path, monkeypatch)
    for all_done in (False, True):
        desk = [r.id for r, _ in M.load_rows(cfg, all_done=all_done)]
        classic = [(t.properties.get("ID") or t.id)
                   for t in R.collect_idea_tasks(cfg, all_done=all_done)]
        assert desk == classic


def test_rows_carry_the_presentation_cells(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    row = M.load_rows(cfg)[0][0]
    assert row.id == "IDEA-001"
    assert row.heading == "desk needs panes"
    assert row.kind == "idea" and row.kind_glyph == "\U0001F4A1"
    assert row.age and row.age != ""
    assert row.state_style  # INCUBATE → cyan (SPEC-0082)


def test_filters_pass_through(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert M.load_rows(cfg, state="INCUBATE")
    assert M.load_rows(cfg, state="SPECCED") == []
    assert M.load_rows(cfg, kind="bug") == []


def test_query_mode_ranks_and_scans_closed_ideas(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    hits = M.load_rows(cfg, query="panes")
    assert [r.id for r, _ in hits] == ["IDEA-001"]
    assert hits[0][0].score and hits[0][0].snippet
    # search always scans open+closed, so the non-active switch does not gate it
    assert [r.id for r, _ in M.load_rows(cfg, query="entirely")] == ["IDEA-002"]


# --- detail ----------------------------------------------------------------------------

def test_detail_splits_questions_by_lowercase_state(tmp_path, monkeypatch):
    """Regression: `question_items` states are "open"/"resolved", not the org keywords."""
    cfg = _seed(tmp_path, monkeypatch)
    d = M.load_detail(cfg, _task(cfg, "IDEA-001"))
    assert d.open_questions == ["which pane gets focus?"]
    assert d.resolved_questions == ["fixed or proportional?"]
    assert (d.open_count, d.resolved_count) == (1, 1)


def test_detail_carries_summary_log_and_hint(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    d = M.load_detail(cfg, _task(cfg, "IDEA-001"))
    assert d.summary == "Two panes beat one table"
    assert len(d.log) == 2
    assert all(e.startswith("[") for e in d.log)   # entries keep their stamp headline
    assert d.next_hint                              # display-only next step
    assert any(m.startswith("kind ") for m in d.meta)


# --- rendering -------------------------------------------------------------------------

def test_detail_markup_escapes_org_prose(tmp_path, monkeypatch):
    """Org text is full of `[#A]` / `[[links]]` / `[2026-07-30 Thu]`; Rich must not eat them."""
    from rich.console import Console

    from nightwright.tui.app import _render_detail

    d = M.DeskDetail(id="IDEA-009", state="IDEA", heading="ship [[IDEA-042]] first",
                     summary="see [#A] and [bold]not bold[/bold]",
                     open_questions=["[#B] which one?"],
                     log=["[2026-07-30 Thu 17:00]\nnote"])
    console = Console(width=100, no_color=True)
    with console.capture() as cap:
        console.print(_render_detail(d))
    text = cap.get()
    assert "[[IDEA-042]]" in text
    assert "[#A]" in text and "[bold]not bold[/bold]" in text
    assert "[#B] which one?" in text
    assert "[2026-07-30 Thu 17:00]" in text


# --- pilot smoke -----------------------------------------------------------------------

def _grid(app) -> list[str]:
    """The rendered screen as a character grid, reconstructed from Textual's SVG export.

    There is no `App.export_text()`, and rasterizing the SVG needs a renderer this repo does not
    depend on — but Rich's SVG places every styled run at a known x/y, so the grid is
    recoverable as plain text. Runs are placed by **column** rather than concatenated: Rich
    omits whitespace-only runs, so concatenating collapses the gutter between the two panes and
    makes neighbouring text look adjacent when it isn't.
    """
    svg = app.export_screenshot()
    runs: list[tuple[float, float, str]] = []
    for x, y, body in re.findall(
            r'<text[^>]*\sx="([\d.]+)"[^>]*\sy="([\d.]+)"[^>]*>(.*?)</text>', svg, re.S):
        # `html.unescape` rather than a hand-rolled table: Rich emits numeric entities too
        # (`&#x27;` for an apostrophe), and a partial table makes assertions quietly wrong.
        chunk = html.unescape(re.sub(r"<[^>]+>", "", body)).replace("\xa0", " ")
        if chunk:
            runs.append((float(x), float(y), chunk))
    if not runs:
        return []

    # Advance width per cell: the modal (x2-x1)/len(run1) across same-line neighbours.
    by_line: dict[float, list[tuple[float, str]]] = {}
    for x, y, chunk in runs:
        by_line.setdefault(y, []).append((x, chunk))
    deltas = []
    for cells in by_line.values():
        cells.sort()
        for (x1, c1), (x2, _) in zip(cells, cells[1:]):
            if len(c1):
                deltas.append((x2 - x1) / len(c1))
    char_w = min(deltas) if deltas else 1.0
    origin = min(x for x, _, _ in runs)

    out = []
    for y in sorted(by_line):
        line: list[str] = []
        for x, chunk in sorted(by_line[y]):
            col = int(round((x - origin) / char_w))
            if col > len(line):
                line.extend(" " * (col - len(line)))
            line[col:col + len(chunk)] = list(chunk)
        out.append("".join(line).rstrip())
    # Rich's SVG draws a fake window title-bar above the terminal. Dropping it makes
    # `grid[i]` line up with screen row `i`, so a test can assert against a widget's
    # `region.y` directly instead of silently being one row off.
    return out[1:] if out else out


def test_desk_lists_ideas_selects_and_shows_detail(tmp_path, monkeypatch):
    """SPEC-0495: the project view lists OPEN ideas and hides DROPPED until `d`."""
    from nightwright.tui import ideas_board as B
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(180, 30)) as pilot:
            assert await pilot_until(pilot, lambda: app._pairs)
            app.select_project(B.NO_PROJECT)
            await pilot_until(pilot, lambda: "Two panes beat one table" in "\n".join(_grid(app)))
            screen = "\n".join(_grid(app))
            assert "IDEA-001" in screen
            assert "desk needs panes" in screen          # detail header
            assert "Two panes beat one table" in screen  # summary
            assert "which pane gets focus?" in screen    # open question
            assert "1 open" in screen and "1 resolved" in screen
            assert "1 dropped hidden" in screen
            assert "IDEA-002" not in screen              # DROPPED hidden by default

            await pilot.press("d")                       # reveal the DROPPED bucket
            await pilot_until(pilot, lambda: "IDEA-002" in "\n".join(_grid(app)))
            assert "DROPPED" in "\n".join(_grid(app))

            await pilot.press("d")
            await pilot.pause()
            assert "IDEA-002" not in "\n".join(_grid(app))

    asyncio.run(drive())


def test_desk_search_filters_then_escape_clears(tmp_path, monkeypatch):
    """`/` lists hits across projects (closed ideas too); Esc restores the board."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            assert await pilot_until(pilot, lambda: app._pairs)
            await pilot.press("slash")
            await pilot.pause()
            for ch in "entirely":
                await pilot.press(ch)
            await pilot.press("enter")
            assert await pilot_until(pilot, lambda: [r.id for r in app._hits] == ["IDEA-002"])
            assert app.query == "entirely"

            await pilot.press("escape")
            await pilot.pause()
            assert app.query == "" and app._hits == []

    asyncio.run(drive())


def test_desk_moves_selection_and_repaints_detail(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg, all_done=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            first = app.selected_id
            await pilot.press("j")
            await pilot.pause()
            assert app.selected_id != first
            assert app.selected_id in ("IDEA-001", "IDEA-002")
            assert "second thing entirely" in "\n".join(_grid(app))

    asyncio.run(drive())


def test_desk_never_shells_out_and_owns_no_write_rules():
    """Epic invariant (SPEC-0098): one mutator path.

    SPEC-0100's original form asserted the app module named *no* mutator at all; SPEC-0102
    deliberately supersedes that half, so what remains is the permanent rule — the desk must
    never spawn `nw`, and must not reach into org files itself. Every write goes through
    `nightwright.tui.model.apply_mutation` → `explore` / `org_write`.
    """
    app_src = (ROOT / "src" / "nightwright" / "tui" / "app.py").read_text()
    model_src = (ROOT / "src" / "nightwright" / "tui" / "model.py").read_text()
    for banned in ("subprocess", "os.system", "popen", "_atomic_backup_write",
                   "open(task.file", "splitlines(keepends=True)"):
        assert banned not in app_src, f"{banned} leaked into the desk UI"
        assert banned not in model_src, f"{banned} leaked into the desk model"
    # the UI names actions, the model owns the dispatch to the library
    assert "apply_mutation" in app_src


# --- CLI wiring ------------------------------------------------------------------------

def test_cli_passes_filters_and_flags_through(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    seen = {}
    monkeypatch.setattr(T, "run", lambda cfg, **kw: seen.update(kw))
    r = CliRunner().invoke(cli, ["tui", "--all", "--project", "Example", "--priority", "A",
                                 "--query", "panes", "--sort", "priority", "--desc"])
    assert r.exit_code == 0, r.output
    assert seen["all_done"] is True
    assert seen["query"] == "panes"
    assert seen["filters"]["project"] == "Example"
    assert seen["filters"]["priority"] == "A"
    assert seen["filters"]["sort"] == "priority"
    assert seen["filters"]["desc"] is True


