"""SPEC-0407 acceptance tests (Pilot) split out to keep the port file smaller."""
from __future__ import annotations

import asyncio
import hashlib
import re
import threading

import pytest
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.widgets import DataTable

import history_scale as HS
import tui_harness as TH
from test_agentic_dashboard import _ev, _fixture
from test_agentic_e2e import run_cli
from test_tui_queue_tab import SIZE, _desk, _run, press, select, settle, view
from nightwright import tui as T
from nightwright.agentic import store as S
from nightwright.tui import agentic_model as AM
from nightwright.tui import campaign_detail as CD
from nightwright.tui.campaign_detail import CampaignDetail, DetailOpener, OpenCampaign
from nightwright.tui.app import ChoiceScreen, IdeaDesk
from nightwright.tui.queue_tab import QueueView
from nightwright.agentic import history as Hist
from textual.widgets import TabbedContent
from pathlib import Path
from waits import pilot_until

from test_tui_campaign_detail import open_screen

MULTI = "SPEC-0336-20261031-6558"
SIZE_160 = TH.SIZE


def detail_screen(app) -> CD.CampaignDetail:
    assert isinstance(app.screen, CD.CampaignDetail)
    return app.screen


def detail(app) -> str:
    return str(detail_screen(app).query_one("#campaign-detail-text").content)


def overview(app) -> str:
    return str(detail_screen(app).query_one("#overview-text").content)


async def wait_detail(app, pilot, substring: str) -> None:
    assert await pilot_until(pilot, lambda: substring in detail(app))


class _OpenerTable(DetailOpener, DataTable):
    def detail_target(self, row_key):
        key = str(row_key or "")
        if not key or key.startswith("hdr"):
            return None
        return (key, None)


class _OpenerApp(App):
    def __init__(self, cfg, **kw):
        super().__init__(**kw)
        self.cfg = cfg
        self.posted: list = []

    def compose(self) -> ComposeResult:
        yield _OpenerTable(id="tbl", cursor_type="row")

    def on_mount(self) -> None:
        t = self.query_one(_OpenerTable)
        t.add_columns("id")
        t.add_row("hdr", key="hdr")
        t.add_row("SPEC-test-open", key="SPEC-test-open")
        t.move_cursor(row=1)
        t.focus()

    @on(OpenCampaign)
    def _open(self, event: OpenCampaign) -> None:
        self.posted.append((event.campaign_id, event.focus))
        self.push_screen(CampaignDetail(self.cfg, event.campaign_id))


def test_detail_opener_enter_and_double_click(tmp_path):
    _, cfg, ids = _fixture(tmp_path)
    app = _OpenerApp(cfg)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            assert app.posted == [("SPEC-test-open", None)]
            assert isinstance(app.screen, CampaignDetail)
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            await pilot.double_click("#tbl", offset=(1, 2))
            await settle(app, pilot)
            assert app.posted[-1] == ("SPEC-test-open", None)
            assert isinstance(app.screen, CampaignDetail)

    _run(drive())


def test_detail_opener_header_posts_nothing(tmp_path):
    _, cfg, _ = _fixture(tmp_path)
    app = _OpenerApp(cfg)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.double_click("#tbl", offset=(1, 1))
            await settle(app, pilot)
            assert app.posted == []
            assert not isinstance(app.screen, CampaignDetail)

    _run(drive())


def test_cli_merged_campaign_opens(tmp_path, monkeypatch):
    _, cfg, ids = _fixture(tmp_path)
    calls = []
    monkeypatch.setattr(T, "run", lambda cfg, **kw: calls.append(kw) or 0)
    ok = run_cli(cfg, monkeypatch, ["tui", "--campaign", ids["merged"]])
    assert ok.exit_code == 0 and calls[-1]["campaign"] == ids["merged"]


def test_focus_run_record_and_unknown(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = None
    hist = None
    for c in _manifest_cids():
        h = Hist.campaign_history(cfg, c)
        if h.get("records") and h.get("runs"):
            cid, hist = c, h
            break
    assert cid and hist
    run_id = hist["runs"][0]["run_id"]
    record_id = hist["records"][0]["id"]
    app = IdeaDesk(cfg, tab="queue")
    app.notes = []
    app.notify = lambda msg, **k: app.notes.append((msg, k))

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs", "run": run_id})
            await settle(app, pilot)
            s = detail_screen(app)
            assert s.query_one("#detail-body", TabbedContent).active == "tab-runs"
            assert f"placeholder: runs ({run_id})" in str(
                s.query_one("#placeholder-text").content)
            await press(app, pilot, ["escape"])
            app.open_campaign_detail(cid, focus={"tab": "records", "record": record_id})
            await settle(app, pilot)
            assert f"placeholder: records ({record_id})" in str(
                detail_screen(app).query_one("#placeholder-records").content)
            await press(app, pilot, ["escape"])
            app.open_campaign_detail(cid, focus={"tab": "records", "record": "nope-id"})
            await settle(app, pilot)
            d = detail_screen(app)
            assert d.query_one("#detail-body", TabbedContent).active == "tab-overview"
            notes = [m for m, _ in app.notes]
            assert any("not found: nope-id" in m for m in notes)

    _run(drive())


def test_runs_focus_timeline_kept_across_poll(tmp_path, monkeypatch):
    """Open on Runs focus, move to Timeline; a poll reload must not snap back to Runs."""
    from test_tui_campaign_detail import seed

    monkeypatch.setattr(CD, "POLL_S", 0.1)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    run_id = "001-implementer"
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs", "run": run_id})
            await settle(app, pilot)
            await pilot_until(
                pilot,
                lambda: "placeholder: runs"
                in str(detail_screen(app).query_one("#placeholder-text").content),
            )
            s = detail_screen(app)
            assert s.query_one("#detail-body", TabbedContent).active == "tab-runs"
            await press(app, pilot, ["2", "f", "f", "f"])
            await settle(app, pilot)
            filt = s.filter
            assert s._active_tab() == "tab-timeline"
            n = len(s._event_rows())
            _ev(cfg, cid, "gate", {"gate_seq": n + 1, "ok": True, "results": []})
            assert await pilot_until(
                pilot, lambda: len(s._event_rows()) > n)
            await settle(app, pilot)
            assert s._active_tab() == "tab-timeline"
            assert s.filter == filt
            assert s.query_one("#detail-body", TabbedContent).active == "tab-timeline"

    _run(drive())


def test_popup_frame_and_pane_keys(tmp_path):
    _, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE_160) as pilot:
            await settle(app, pilot)
            select(app, "queued", ids["ready"])
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            s = detail_screen(app)
            frame = s.query_one("#detail-frame")
            w, h = app.size.width, app.size.height
            region = frame.region
            assert abs(region.width - round(0.96 * w)) <= 1
            assert abs(region.height - round(0.94 * h)) <= 1
            tabs = app.query_one("#tabs")
            assert tabs.active == "tab-queue"
            body = s.query_one("#detail-body", TabbedContent)
            for key, want in (
                ("1", "tab-overview"),
                ("2", "tab-timeline"),
                ("3", "tab-runs"),
                ("4", "tab-records"),
                ("5", "tab-mail"),
            ):
                await press(app, pilot, [key])
                await settle(app, pilot)
                assert tabs.active == "tab-queue"
                assert body.active == want
            assert "placeholder: runs" in str(s.query_one("#placeholder-text").content)
            assert "placeholder: records" in str(s.query_one("#placeholder-records").content)
            assert "placeholder: mail" in str(s.query_one("#placeholder-mail").content)
            assert ids["ready"] in overview(app)
            assert overview(app) == "\n".join(
                t.plain for t in AM.load_detail(cfg, ids["ready"]).header_lines)
            assert "dominant:" not in overview(app) and "gaps:" not in overview(app)

    _run(drive())


def test_desk_digit_one_under_priority_picker_does_not_switch_tab(tmp_path):
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            select(app, "queued", cid)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            tabs = app.query_one("#tabs")
            assert tabs.active == "tab-queue"
            await press(app, pilot, ["P"])
            await pilot.pause(0.3)
            assert isinstance(app.screen, ChoiceScreen)
            await pilot.press("1")
            await pilot.pause(0.2)
            assert tabs.active == "tab-queue"
            assert isinstance(app.screen, ChoiceScreen)
            await press(app, pilot, ["escape"])
            await pilot.pause(0.3)
            if not isinstance(app.screen, CD.CampaignDetail):
                await press(app, pilot, ["escape"])
            await pilot_until(pilot, lambda: isinstance(app.screen, CD.CampaignDetail))

    _run(drive())


def test_timeline_time_includes_month_day(tmp_path):
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    _ev(cfg, cid, "note", {"text": "today"})
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_via_queue(app, pilot, cid)
            s = detail_screen(app)
            last = s._data_indexes()[-1]
            cell = s.table.get_row_at(last)[1]
            assert re.match(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}", cell.plain)

    _run(drive())


async def open_via_queue(app, pilot, cid):
    await settle(app, pilot)
    section = next((r.group for r in view(app).rows if r.campaign == cid), "queued")
    await open_screen(app, pilot, cid, section)


def test_poll_signature_runs_off_ui_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    main = threading.current_thread()
    seen = []
    real_sig = AM.detail_signature

    def sig(cfg, campaign_id):
        seen.append(threading.current_thread())
        return real_sig(cfg, campaign_id)

    monkeypatch.setattr(AM, "detail_signature", sig)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            await pilot_until(pilot, lambda: seen and seen[0] is not main)

    _run(drive())


def test_multi_loop_brackets_and_separators(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    app = IdeaDesk(cfg, tab="queue")

    def _group_starts(s):
        return s._loop_starts()

    def _at_group_start(s):
        i = s.table.cursor_row
        assert not s.shown[i].get("separator")
        return i in _group_starts(s)

    async def drive():
        async with app.run_test(size=SIZE_160) as pilot:
            assert await pilot_until(
                pilot,
                lambda: any(r.campaign == MULTI
                            for r in app.query_one("#queue-view", QueueView).rows))
            rows = app.query_one("#queue-view", QueueView).rows
            i = next(i for i, r in enumerate(rows) if r.campaign == MULTI)
            app.query_one("#queue-view", QueueView).table.move_cursor(row=i)
            await press(app, pilot, ["enter", "2"])
            await settle(app, pilot)
            s = detail_screen(app)
            seps = [r.get("label", "") for r in s.shown if r.get("separator")]
            assert any("── loop" in (lb or "") for lb in seps)
            for idx in s._data_indexes():
                assert not s.shown[idx].get("separator")
            starts = _group_starts(s)
            assert len(starts) >= 2
            last_idx = s._data_indexes()[-1]
            assert s.table.cursor_row == last_idx
            await press(app, pilot, ["right_square_bracket"])
            await settle(app, pilot)
            assert s.table.cursor_row == last_idx
            s.table.move_cursor(row=starts[0])
            await settle(app, pilot)
            await press(app, pilot, ["right_square_bracket"])
            await settle(app, pilot)
            assert _at_group_start(s)
            assert s.table.cursor_row == starts[1]
            mid = starts[1] + 2
            s.table.move_cursor(row=mid)
            await settle(app, pilot)
            await press(app, pilot, ["left_square_bracket"])
            await settle(app, pilot)
            assert _at_group_start(s)
            assert s.table.cursor_row == starts[0]
            sep_rows = [i for i, r in enumerate(s.shown) if r.get("separator")]
            assert sep_rows
            before_sep = sep_rows[0] - 1
            s.table.move_cursor(row=before_sep)
            await settle(app, pilot)
            for key in ("down", "j"):
                await press(app, pilot, [key])
                await settle(app, pilot)
                assert not s.shown[s.table.cursor_row].get("separator")
            s.table.move_cursor(row=starts[1] + 1)
            await settle(app, pilot)
            for key in ("up", "k"):
                await press(app, pilot, [key])
                await settle(app, pilot)
                assert not s.shown[s.table.cursor_row].get("separator")

    _run(drive())


def test_terminal_idle_no_extra_model_calls(tmp_path, monkeypatch):
    import builtins
    import os
    from nightwright.agentic import store as St

    _, cfg, ids = _fixture(tmp_path)
    cid = ids["merged"]
    counts = {"sig": 0, "load": 0, "tail": 0, "stat": 0, "open": 0}
    camp_root = str(St.campaign_dir(cfg, cid).resolve())

    real_sig = AM.detail_signature
    real_load = AM.load_detail
    real_tail = AM.stream_tail

    def sig(*a, **k):
        counts["sig"] += 1
        return real_sig(*a, **k)

    def load(*a, **k):
        counts["load"] += 1
        return real_load(*a, **k)

    def tail(*a, **k):
        counts["tail"] += 1
        return real_tail(*a, **k)

    real_stat = os.stat
    real_open = builtins.open

    def stat(path, *a, **k):
        if camp_root in str(Path(path).resolve()):
            counts["stat"] += 1
        return real_stat(path, *a, **k)

    def tracked_open(path, *a, **k):
        if camp_root in str(Path(path).resolve()):
            counts["open"] += 1
        return real_open(path, *a, **k)

    monkeypatch.setattr(AM, "detail_signature", sig)
    monkeypatch.setattr(AM, "load_detail", load)
    monkeypatch.setattr(AM, "stream_tail", tail)
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(builtins, "open", tracked_open)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            select(app, "delivered", cid)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            s = detail_screen(app)
            assert s.view is not None and s.view.terminal
            assert cid in overview(app)
            err = str(s.query_one("#load-error").content)
            assert "history unavailable:" not in err
            assert s._poll_timer is None
            sig0, load0, tail0 = counts["sig"], counts["load"], counts["tail"]
            qv = app.query_one("#queue-view", QueueView)
            qv.deactivate()
            counts["stat"] = counts["open"] = 0
            await pilot.pause(2.5)
            assert counts["sig"] == sig0 and counts["load"] == load0
            assert counts["tail"] == tail0
            assert counts["stat"] == 0 and counts["open"] == 0
            assert s._poll_timer is None

    _run(drive())


def _hashes(root):
    root = Path(root)
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_popup_tour_no_writes(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    data = Path(cfg["data_dir"])
    before = _hashes(data)
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE_160) as pilot:
            assert await pilot_until(pilot, lambda: "delivered" in TH.visible_text(app))
            await pilot.double_click("#queue", offset=(2, 4))
            await settle(app, pilot)
            s = detail_screen(app)
            for key in ("1", "2", "3", "4", "5", "f", "left_bracket", "right_bracket",
                        "g", "G", "j", "k", "v"):
                await press(app, pilot, [key])
                await settle(app, pilot)
            await press(app, pilot, ["escape"])
            await settle(app, pilot)

    _run(drive())
    assert _hashes(data) == before


def _manifest_cids() -> list[str]:
    import json
    import history_scale as HS
    manifest = json.loads((HS.FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    return sorted(entry["cid"] for entry in manifest["campaigns"])


def _queue_row_index(rows, group, campaign=None, *, kind="campaign"):
    if kind == "header":
        return next(i for i, r in enumerate(rows) if r.kind == "header" and r.group == group)
    return next(i for i, r in enumerate(rows) if r.kind == "campaign"
                and r.group == group and r.campaign == campaign)


async def _double_click_row(pilot, row: int) -> None:
    await pilot.double_click("#queue", offset=(2, row + 1))


def test_queue_enter_double_click_reload_dismiss(tmp_path, monkeypatch):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cids = _manifest_cids()
    cid_a, cid_b = cids[-2], cids[-1]
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE_160) as pilot:
            assert await pilot_until(pilot, lambda: "delivered" in TH.visible_text(app))
            qv = app.query_one("#queue-view", QueueView)
            rows = qv.rows
            ia = _queue_row_index(rows, "delivered", cid_a)
            ib = _queue_row_index(rows, "delivered", cid_b)
            ih = _queue_row_index(rows, "delivered", kind="header")
            before_stack = len(app.screen_stack)
            qv.table.move_cursor(row=ia)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            assert detail_screen(app).campaign_id == cid_a
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            qv.table.move_cursor(row=ib)
            await _double_click_row(pilot, ib)
            await settle(app, pilot)
            assert detail_screen(app).campaign_id == cid_b
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            qv.table.move_cursor(row=ih)
            await _double_click_row(pilot, ih)
            await settle(app, pilot)
            assert len(app.screen_stack) == before_stack
            qv.table.move_cursor(row=ia)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            opened = detail_screen(app).campaign_id
            qv.reload()
            await settle(app, pilot)
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            assert view(app).current_row().campaign == opened
            qv.table.move_cursor(row=ia)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            opened = detail_screen(app).campaign_id
            qv.reload()
            await settle(app, pilot)
            await press(app, pilot, ["q"])
            await settle(app, pilot)
            assert view(app).current_row().campaign == opened
            idx_before = qv.table.cursor_row
            qv.table.move_cursor(row=ia)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            gone = detail_screen(app).campaign_id
            real_load = AM.load_queue

            def drop_opened(cfg):
                rows, hdr = real_load(cfg)
                rows = [r for r in rows
                        if not (r.kind == "campaign" and r.campaign == gone)]
                return rows, hdr

            monkeypatch.setattr(AM, "load_queue", drop_opened)
            qv.reload()
            await settle(app, pilot)
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            assert qv.table.cursor_row == idx_before

    _run(drive())


def _fixture_timeline_record(cfg) -> tuple[str, dict]:
    for cid in _manifest_cids():
        hist = Hist.campaign_history(cfg, cid)
        for row in hist.get("timeline") or []:
            if row.get("seq") is None and row.get("ref"):
                return cid, row
    pytest.skip("no seq-null timeline row in fixture")


def test_seq_null_record_detail(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid, row = _fixture_timeline_record(cfg)
    hist = Hist.campaign_history(cfg, cid)
    record_id = row["ref"]
    records = {r["id"]: r for r in hist.get("records") or []}
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_via_queue(app, pilot, cid)
            s = detail_screen(app)
            ref = row.get("ref")
            for i, shown in enumerate(s.shown):
                if not shown.get("separator") and shown.get("ref") == ref:
                    s.table.move_cursor(row=i)
                    break
            await settle(app, pilot)
            text = detail(app)
            if record_id in records:
                rec = records[record_id]
                assert record_id in text
                for key in ("kind", "severity", "title"):
                    if rec.get(key):
                        assert str(rec[key]) in text
                assert "links:" in text
                links = rec.get("links") or {}
                for k, v in links.items():
                    if v is not None:
                        assert str(v) in text
                if rec.get("linked_by") is not None:
                    assert "linked_by" in text
                    assert str(rec["linked_by"]) in text
            else:
                assert f"record {record_id} not in history" in text

    _run(drive())


def test_load_detail_failure_on_open_shows_history_unavailable(tmp_path, monkeypatch):
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    def boom(cfg_, campaign_id):
        raise RuntimeError("no history disk")

    monkeypatch.setattr(AM, "load_detail", boom)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            select(app, "queued", cid)
            await press(app, pilot, ["enter"])
            assert await pilot_until(
                pilot,
                lambda: "history unavailable:"
                in str(detail_screen(app).query_one("#load-error").content),
            )
            s = detail_screen(app)
            assert s.view is None
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            assert not isinstance(app.screen, CampaignDetail)

    _run(drive())


def test_history_with_gaps_opens(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = None
    for c in _manifest_cids():
        if Hist.campaign_history(cfg, c).get("gaps"):
            cid = c
            break
    if cid is None:
        pytest.skip("no fixture campaign with history gaps")
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_via_queue(app, pilot, cid)
            assert isinstance(app.screen, CampaignDetail)

    _run(drive())
