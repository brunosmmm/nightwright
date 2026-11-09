"""TUI desk resilience to external idea updates (SPEC-0128).

`org.org_files_mtime_signature` needs no Textual; the pilot tests simulate an external writer
mutating the org file directly (line-shifting inserts, state changes) between the desk's last
`reload()` and a subsequent read, then assert the read self-heals instead of surfacing
`explore._idea_span`'s drift guard as a raw error string.
"""
import asyncio
import html
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from waits import pilot_until

from nightwright import org as O
from nightwright.cli import cli
import pytest

pytestmark = pytest.mark.tui


def _grid(app) -> list[str]:
    """Same SVG-reconstruction approach as test_tui_desk.py's `_grid` — there is no
    `App.export_text()`, so the rendered screen is recovered from Textual's SVG export."""
    svg = app.export_screenshot()
    runs: list[tuple[float, float, str]] = []
    for x, y, body in re.findall(
            r'<text[^>]*\sx="([\d.]+)"[^>]*\sy="([\d.]+)"[^>]*>(.*?)</text>', svg, re.S):
        chunk = html.unescape(re.sub(r"<[^>]+>", "", body)).replace("\xa0", " ")
        if chunk:
            runs.append((float(x), float(y), chunk))
    if not runs:
        return []
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
    return out[1:] if out else out


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
        "data_dir": str(tmp_path / "data"),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "config_dir": str(tmp_path / "config"),
    }, org


# --- org_files_mtime_signature (no Textual) ---------------------------------------------

def test_signature_stable_when_nothing_changes(tmp_path):
    cfg, org = _cfg(tmp_path)
    (org / "a.org").write_text("* TODO x\n")
    sig1 = O.org_files_mtime_signature(cfg)
    sig2 = O.org_files_mtime_signature(cfg)
    assert sig1 == sig2


def test_signature_changes_on_edit(tmp_path):
    cfg, org = _cfg(tmp_path)
    f = org / "a.org"
    f.write_text("* TODO x\n")
    sig1 = O.org_files_mtime_signature(cfg)
    time.sleep(0.01)
    os.utime(f, None)  # bump mtime without necessarily changing content
    sig2 = O.org_files_mtime_signature(cfg)
    assert sig1 != sig2


def test_signature_changes_on_new_file(tmp_path):
    cfg, org = _cfg(tmp_path)
    (org / "a.org").write_text("* TODO x\n")
    sig1 = O.org_files_mtime_signature(cfg)
    (org / "b.org").write_text("* TODO y\n")
    sig2 = O.org_files_mtime_signature(cfg)
    assert sig1 != sig2


# --- pilot: self-healing reads -----------------------------------------------------------

def _seed(tmp_path, monkeypatch):
    cfg, _org = _cfg(tmp_path)
    (Path(cfg["data_dir"])).mkdir(exist_ok=True)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "first idea"]).exit_code == 0
    assert run(cli, ["idea", "second idea"]).exit_code == 0
    return cfg


def test_detail_pane_self_heals_after_external_line_shift(tmp_path, monkeypatch):
    """A cached Task's line number goes stale when a new headline is inserted earlier in the
    same file by an external writer — the drift class this spec fixes."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            assert await pilot_until(pilot, lambda: app._pairs)
            # Select IDEA-002 explicitly so its cached Task's line is known.
            app.jump_to("IDEA-002")
            assert app.selected_id == "IDEA-002"

            # External writer: insert a brand-new idea *above* IDEA-002 in the same file,
            # shifting every subsequent line number without going through the desk at all.
            CliRunner().invoke(cli, ["idea", "inserted before, shifts lines"])

            # Re-render the still-selected row without a reload() first -- this is exactly
            # the stale-Task path: before the fix, show_detail reused the cached Task whose
            # .line no longer points at IDEA-002's headline.
            app._shown_id = None
            app.show_detail("IDEA-002")
            await pilot.pause()

            screen = "\n".join(_grid(app))
            assert "could not read idea" not in screen
            assert "second idea" in screen

    asyncio.run(drive())


def test_poll_picks_up_external_new_idea_without_keypress(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert not any(r.id == "IDEA-003" for r, _t in app._pairs)

            CliRunner().invoke(cli, ["idea", "added externally while desk is open"])

            # Simulate the poll tick directly rather than sleeping in real time.
            await app._check_external_changes()
            await pilot.pause()

            assert any(r.id == "IDEA-003" for r, _t in app._pairs)

    asyncio.run(drive())


def test_poll_is_a_noop_when_nothing_changed(tmp_path, monkeypatch):
    """No signature change -> no reload -> no hint noise."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.set_hint("")
            await app._check_external_changes()
            await pilot.pause()
            assert "changed externally" not in "\n".join(_grid(app))

    asyncio.run(drive())


def test_action_summary_prefill_reflects_external_change(tmp_path, monkeypatch):
    """The action_summary pre-fill bug (SPEC-0128 Q4): swallowed the drift error into a blank
    prefill instead of showing current content."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    CliRunner().invoke(cli, ["idea", "summary", "IDEA-002", "--set", "current summary text"])

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            assert await pilot_until(pilot, lambda: app._pairs)
            app.jump_to("IDEA-002")

            # External line-shift, same as the detail-pane test above.
            CliRunner().invoke(cli, ["idea", "inserted before, shifts lines again"])

            current = ""
            try:
                from nightwright.tui import model as M
                current = M.load_detail(app.cfg, app._resolve_fresh("IDEA-002")).summary
            except (ValueError, OSError):
                current = ""
            assert current == "current summary text"

    asyncio.run(drive())


# --- SPEC-0129: poll reload off the event-loop thread -----------------------------------

def test_poll_reload_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """A slow load_rows (simulated) must not prevent other async work from running
    concurrently -- proving the reparse actually happens off the event-loop thread."""
    from nightwright.tui import ideas_board as M  # SPEC-0495: the board load is the reparse
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    real_load_rows = M.load_board

    def slow_load_rows(*args, **kwargs):
        time.sleep(0.2)
        return real_load_rows(*args, **kwargs)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            CliRunner().invoke(cli, ["idea", "added while a slow reload runs"])
            monkeypatch.setattr(M, "load_board", slow_load_rows)

            task = asyncio.create_task(app._check_external_changes())
            await asyncio.sleep(0.02)          # give the executor call a chance to start
            assert app._reload_in_flight is True
            # The event loop is free to do other work while load_rows sleeps in its thread.
            other_ticks = 0
            for _ in range(5):
                await asyncio.sleep(0.01)
                other_ticks += 1
            assert other_ticks == 5            # loop kept running, not frozen for 0.2s solid
            await task
            assert app._reload_in_flight is False

    asyncio.run(drive())


def test_concurrent_poll_ticks_only_run_one_reload(tmp_path, monkeypatch):
    """The in-flight guard prevents a second poll tick from starting a second load_rows
    before the first one's result has been applied."""
    from nightwright.tui import ideas_board as M  # SPEC-0495: the board load is the reparse
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    real_load_rows = M.load_board
    calls = []

    def counting_load_rows(*args, **kwargs):
        calls.append(1)
        time.sleep(0.1)
        return real_load_rows(*args, **kwargs)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            CliRunner().invoke(cli, ["idea", "second external change"])
            monkeypatch.setattr(M, "load_board", counting_load_rows)

            t1 = asyncio.create_task(app._check_external_changes())
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(app._check_external_changes())  # fires while t1 in flight
            await t1
            await t2

            assert len(calls) == 1

    asyncio.run(drive())


def test_manual_reload_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """SPEC-0134: r / mutate / search reload uses the same off-thread path as the poll."""
    from nightwright.tui import ideas_board as M  # SPEC-0495: the board load is the reparse
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    real_load_rows = M.load_board

    def slow_load_rows(*args, **kwargs):
        time.sleep(0.2)
        return real_load_rows(*args, **kwargs)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(M, "load_board", slow_load_rows)

            task = asyncio.create_task(app._reload_async())
            await asyncio.sleep(0.02)
            assert app._reload_in_flight is True
            other_ticks = 0
            for _ in range(5):
                await asyncio.sleep(0.01)
                other_ticks += 1
            assert other_ticks == 5
            await task
            assert app._reload_in_flight is False

    asyncio.run(drive())


def test_overlapping_reload_async_coalesces(tmp_path, monkeypatch):
    """SPEC-0134: in-flight guard coalesces overlapping _reload_async calls."""
    from nightwright.tui import ideas_board as M  # SPEC-0495: the board load is the reparse
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    real_load_rows = M.load_board
    calls = []

    def counting_load_rows(*args, **kwargs):
        calls.append(1)
        time.sleep(0.1)
        return real_load_rows(*args, **kwargs)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(M, "load_board", counting_load_rows)

            t1 = asyncio.create_task(app._reload_async())
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(app._reload_async())
            await t1
            await t2
            assert len(calls) == 1

    asyncio.run(drive())


