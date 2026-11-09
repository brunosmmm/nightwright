"""SPEC-0410: the grouped queue table in a mounted desk (Textual pilot tests)."""
import json

from test_agentic_dashboard import _campaign, _fixture, _iso, _park_fixture
from test_tui_queue_tab import SIZE, _desk, _run, select, settle, view
from nightwright.agentic import store as S
from waits import pilot_until
import pytest

pytestmark = pytest.mark.tui


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


def _set_cost(cfg, cid, **cost):
    st = S.read_state(cfg, cid)
    st["cost"] = {**st.get("cost", {}), **cost}
    S.write_state(cfg, cid, st)


def test_over_cap_cost_is_flagged_and_no_cap_is_money_only(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    _set_cost(cfg, ids["impl"], total_usd=12.5, cap_usd=10.0)               # over its cap
    _set_cost(cfg, ids["gating"], total_usd=2.0, cap_usd=None)              # no cap
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v, t = view(app), view(app).table
            at = lambda cid: next(i for i, r in enumerate(v.rows)   # noqa: E731
                                  if r.campaign == cid and r.kind == "campaign")
            over = t.get_row_at(at(ids["impl"]))[5]
            assert over.plain == "$12.50 ████████" and str(over.style) == "bold red"
            money = t.get_row_at(at(ids["gating"]))[5]
            assert money.plain == "$2.00" and str(money.style) == ""
            # a mid-way cap: a partial bar, not flagged
            _set_cost(cfg, ids["gating"], cap_usd=4.0)
            v.reload()
            await settle(app, pilot)
            half = t.get_row_at(at(ids["gating"]))[5]
            assert half.plain == "$2.00 ████░░░░" and str(half.style) == ""

    _run(drive())


def test_a_non_numeric_cap_never_fails_the_paint(tmp_path):
    import dataclasses
    from nightwright.tui import agentic_model as AM
    from nightwright.tui.queue_tab import QueueView
    repo, cfg, ids = _fixture(tmp_path)
    rows, _ = AM.load_queue(cfg)
    row = next(r for r in rows if r.campaign == ids["impl"])
    for cap in ("n/a", "10", [], object()):
        cells = QueueView._cells(dataclasses.replace(row, cap_usd=cap), 20, 20)
        assert cells[5].plain.startswith("$1.25") and str(cells[5].style) != "bold red"

    app = _desk(cfg, tab="queue")
    _set_cost(cfg, ids["impl"], cap_usd="n/a")                          # a hand-edited state.json

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            assert any(r.campaign == ids["impl"] for r in v.rows)       # not a blank tab
            assert "queue failed" not in str(v.query_one("#queue-status").render())

    _run(drive())


def _first_data_id(v):
    return v.rows[_first_data_index(v)].campaign


def _first_data_index(v):
    return next(i for i, r in enumerate(v.rows) if r.kind == "campaign")


def test_enter_key_on_a_header_row_posts_nothing(tmp_path, monkeypatch):
    from nightwright.tui import queue_tab as QT
    # keep the snap handler from moving the cursor off the header, so the Enter binding itself
    # is what meets the header row
    monkeypatch.setattr(QT.QueueTable, "on_data_table_row_highlighted", lambda self, event: None)
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")
    posted = []

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v, t = view(app), view(app).table
            from nightwright.tui.campaign_detail import OpenCampaign

            real = v.post_message
            v.post_message = (
                lambda m: (posted.append(m) if isinstance(m, OpenCampaign) else None) or real(m)
            )
            header = next(i for i, r in enumerate(v.rows) if r.kind == "header")
            t.move_cursor(row=header)
            await settle(app, pilot)
            assert t.cursor_row == header and v.current_row() is None
            await pilot.press("enter")
            await settle(app, pilot)
            assert posted == [] and len(app.screen_stack) == 1
            # the same key on a data row opens the campaign
            t.move_cursor(row=_data(app)[0])
            await pilot.press("enter")
            await settle(app, pilot)
            assert len(posted) == 1 and posted[0].campaign_id == _first_data_id(v)

    _run(drive())


def test_a_vanished_campaign_leaves_the_cursor_on_a_neighbouring_data_row(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v, t = view(app), view(app).table
            old = select(app, "gating", ids["gating"])
            # the campaign leaves the table altogether: merged long ago, out of the 24 h window
            st = S.read_state(cfg, ids["gating"])
            st["state"] = "MERGED"
            S.write_state(cfg, ids["gating"], st)
            p = S.campaign_dir(cfg, ids["gating"]) / "state.json"
            d = json.loads(p.read_text())
            d["updated_at"] = _iso(3000)
            p.write_text(json.dumps(d))
            v.reload()
            await settle(app, pilot)
            assert all(r.campaign != ids["gating"] for r in v.rows)
            assert _on_data(app) and v.current_row().campaign != ids["gating"]
            assert abs(t.cursor_row - old) <= 2             # next to where it was, never a header

    _run(drive())


def test_layout_at_wide_and_narrow_sizes_clips_why_before_title(tmp_path):
    """Stands in for the pyte 160x48 snapshot, which times and costs make unstable: header
    labels and order, fixed column widths, and which of title/why is clipped first."""
    import dataclasses
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=(160, 48)) as pilot:
            await settle(app, pilot)
            v, t = view(app), view(app).table
            rows = [dataclasses.replace(r, title="T" * 30, why="w" * 200)
                    if r.kind == "campaign" else r for r in v.rows]
            assert [str(c.label) for c in t.columns.values()] == [
                "pri", "campaign / spec", "title", "state", "loop", "cost", "time", "why"]
            first = _data(app)[0]
            v.rows = rows
            v.paint()
            wide = [c.plain for c in t.get_row_at(first)]
            assert wide[2] == "T" * 30                              # title fits: not clipped
            assert 12 <= len(wide[7]) < 200 and wide[7].endswith("…")   # why clipped
            assert len(wide[1]) <= 30 and len(wide[3]) <= 26        # fixed-width columns
            hdr = [c.plain for c in t.get_row_at(0)]
            assert hdr[1] == f"{rows[0].group} ({rows[0].count})" and hdr[0] == "" and hdr[7] == ""
            await pilot.resize_terminal(110, 30)
            await settle(app, pilot)
            v.paint()
            narrow = [c.plain for c in t.get_row_at(first)]
            assert len(narrow[2]) < 30 and narrow[2].endswith("…")  # no room: title clipped too
            assert len(narrow[7]) >= 12 and len(narrow[7]) <= len(wide[7])
            assert len(narrow[2]) + len(narrow[7]) < len(wide[2]) + len(wide[7])

    _run(drive())
