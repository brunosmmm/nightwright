"""SPEC-0410: the grouped queue table in a mounted desk (Textual pilot tests)."""
import pytest

pytest.importorskip("textual")  # module-level skip: the imports below need the tui extra

from test_agentic_dashboard import _campaign, _fixture, _park_fixture
from test_tui_queue_tab import SIZE, _desk, _run, select, settle, view
from nightwright.agentic import store as S
from waits import pilot_until


def _data(app):
    return [i for i, r in enumerate(view(app).rows) if r.kind == "campaign"]


def _on_data(app):
    v = view(app)
    return v.rows[v.table.cursor_row].kind == "campaign"


def test_cursor_skips_headers_with_every_movement_key(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    _campaign(cfg, repo, "SPEC-0001-dirty", "PARKED",
              park={"reason": "main_dirty", "detail": "d", "resume_to": "GATING"})
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v, t = view(app), view(app).table
            data = _data(app)
            assert len(data) >= 6 and len(v.rows) > len(data)       # headers exist
            assert t.cursor_row == data[0]                           # first row is a header: skip it
            seen = [t.cursor_row]
            for _ in range(len(v.rows)):
                await pilot.press("j")
                await settle(app, pilot, 1)
                assert _on_data(app)
                seen.append(t.cursor_row)
            assert seen[-1] == data[-1] and seen[:len(data)] == data   # ends put at the last row
            for _ in range(len(v.rows)):
                await pilot.press("k")
                await settle(app, pilot, 1)
                assert _on_data(app)
            assert t.cursor_row == data[0]
            await pilot.press("down", "down", "up")
            await settle(app, pilot, 1)
            assert _on_data(app) and t.cursor_row == data[1]
            await pilot.press("end")
            await settle(app, pilot, 1)
            assert t.cursor_row == data[-1]
            await pilot.press("pagedown")
            await settle(app, pilot, 1)
            assert t.cursor_row == data[-1]
            await pilot.press("home")
            await settle(app, pilot, 1)
            assert t.cursor_row == data[0]
            await pilot.press("pageup")
            await settle(app, pilot, 1)
            assert t.cursor_row == data[0]
            await pilot.press("pagedown")
            await settle(app, pilot, 1)
            assert _on_data(app)

    _run(drive())


def test_a_click_on_a_header_snaps_to_a_data_row_and_enter_on_one_does_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")
    posted = []

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v, t = view(app), view(app).table
            header = next(i for i, r in enumerate(v.rows) if r.kind == "header" and i)
            await pilot.click("#queue", offset=(8, header + 1))     # +1: the column header line
            await settle(app, pilot)
            assert _on_data(app) and t.cursor_row > header
            # Enter on a header row (before the snap is processed) posts nothing
            real = v.post_message
            v.post_message = lambda m: posted.append(m) or real(m)
            t.move_cursor(row=header)
            assert v.current_row() is None
            v.open_selected()
            v.start_action("park")
            assert posted == [] and not app.notes
            await settle(app, pilot)
            assert _on_data(app)

    _run(drive())


def test_a_reload_without_a_change_leaves_rows_keys_and_cursor(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v, t = view(app), view(app).table
            select(app, "conductor", ids["req"])
            rows, keys = list(v.rows), [k.value for k in t.rows]
            cursor = (t.cursor_row, v.current_row().group, v.current_row().campaign)
            for _ in range(2):
                v.reload()
                await settle(app, pilot)
            assert v.rows == rows and [k.value for k in t.rows] == keys
            assert (t.cursor_row, v.current_row().group, v.current_row().campaign) == cursor

    _run(drive())


def test_the_cursor_follows_a_campaign_to_its_new_group(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            select(app, "building", ids["impl"])
            st = S.read_state(cfg, ids["impl"])
            st["state"] = "CLOSING"
            S.write_state(cfg, ids["impl"], st)
            v.reload()
            await settle(app, pilot)
            row = v.current_row()
            assert (row.group, row.campaign) == ("closing", ids["impl"])
            groups = [r.group for r in v.rows if r.kind == "header"]
            assert groups.index("closing") < groups.index("gating")

    _run(drive())


def test_the_deferred_park_row_keeps_its_own_group_across_a_reload(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            keys = [k.value for k in v.table.rows]
            assert f"conductor|{ids['req']}" in keys and f"building|{ids['req']}" in keys
            assert len(keys) == len(set(keys))
            for group in ("conductor", "building"):
                select(app, group, ids["req"])
                idx = v.table.cursor_row
                v.reload()
                await settle(app, pilot)
                assert v.table.cursor_row == idx and v.current_row().group == group

    _run(drive())


def test_the_table_renders_the_eight_columns_and_a_cost_bar(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v, t = view(app), view(app).table
            assert [str(c.label) for c in t.columns.values()] == [
                "pri", "campaign / spec", "title", "state", "loop", "cost", "time", "why"]
            i = next(i for i, r in enumerate(v.rows) if r.campaign == ids["impl"])
            cells = [c.plain for c in t.get_row_at(i)]
            assert cells[0] == "3" and cells[3].startswith("IMPLEMENTING implementer")
            assert cells[5].startswith("$1.25 ") and "█" in cells[5] and "░" in cells[5]
            hdr = [c.plain for c in t.get_row_at(0)]
            assert hdr[1] == f"{v.rows[0].group} ({v.rows[0].count})"

    _run(drive())


def test_a_poll_driven_reload_keeps_the_cursor_on_its_row(tmp_path, monkeypatch):
    from nightwright.tui import queue_tab as QT
    monkeypatch.setattr(QT, "POLL_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            select(app, "queued", ids["blocked"])
            from nightwright.agentic import queue as Q
            Q.apply_prioritize(cfg, Q.plan_prioritize(cfg, ids["blocked"], 1))
            assert await pilot_until(pilot, lambda: v.current_row() is not None
                                     and v.current_row().pri == "1")
            assert v.current_row().campaign == ids["blocked"]

    _run(drive())
