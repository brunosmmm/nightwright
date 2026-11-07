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
from nightwright.tui.app import IdeaDesk
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
    BINDINGS = [
        *getattr(DetailOpener, "BINDINGS", ()),
        *getattr(DataTable, "BINDINGS", ()),
    ]

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
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["impl"]
    hist = Hist.campaign_history(cfg, cid)
    run_id = hist["runs"][0]["run_id"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs", "run": run_id})
            await settle(app, pilot)
            s = detail_screen(app)
            assert f"placeholder: runs ({run_id})" in str(
                s.query_one("#placeholder-text").content)
            await press(app, pilot, ["escape"])
            app.open_campaign_detail(cid, focus={"tab": "records", "record": "nope-id"})
            await settle(app, pilot)
            assert isinstance(app.screen, CampaignDetail)
            notes = [m for m, _ in app.notes]
            assert any("not found: nope-id" in m for m in notes)

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
            assert abs(frame.size.width - round(0.96 * w)) <= 3
            assert abs(frame.size.height - round(0.94 * h)) <= 3
            tabs = app.query_one("#tabs")
            assert tabs.active == "tab-queue"
            panes = []
            for key in ("1", "2"):
                await press(app, pilot, [key])
                await settle(app, pilot)
                panes.append(s.query_one("#detail-body", TabbedContent).active)
            assert panes == ["tab-overview", "tab-timeline"]
            assert tabs.active == "tab-queue"
            assert "placeholder: runs" in str(s.query_one("#placeholder-text").content)
            assert "placeholder: records" in str(s.query_one("#placeholder-records").content)
            assert "placeholder: mail" in str(s.query_one("#placeholder-mail").content)
            assert ids["ready"] in overview(app)
            assert overview(app) == "\n".join(
                t.plain for t in AM.load_detail(cfg, ids["ready"]).header_lines)
            assert "dominant:" not in overview(app) and "gaps:" not in overview(app)

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

    def sig(cfg, campaign_id):
        seen.append(threading.current_thread())
        return AM.detail_signature(cfg, campaign_id)

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
            await press(app, pilot, ["g"])
            await settle(app, pilot)
            loop0 = s.shown[s.table.cursor_row].get("loop", 0)
            await press(app, pilot, ["right_bracket"])
            await settle(app, pilot)
            assert s.shown[s.table.cursor_row].get("loop", 0) > loop0
            loop1 = s.shown[s.table.cursor_row].get("loop", 0)
            await press(app, pilot, ["left_bracket"])
            await settle(app, pilot)
            assert s.shown[s.table.cursor_row].get("loop", 0) < loop1

    _run(drive())


def test_terminal_idle_no_extra_model_calls(tmp_path, monkeypatch):
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["merged"]
    counts = {"sig": 0, "load": 0}

    def sig(*a, **k):
        counts["sig"] += 1
        return AM.detail_signature(*a, **k)

    def load(*a, **k):
        counts["load"] += 1
        return AM.load_detail(*a, **k)

    monkeypatch.setattr(AM, "detail_signature", sig)
    monkeypatch.setattr(AM, "load_detail", load)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            select(app, "delivered", cid)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            sig0, load0 = counts["sig"], counts["load"]
            qv = app.query_one("#queue-view", QueueView)
            qv.deactivate()
            await pilot.pause(2.6)
            assert counts["sig"] == sig0 and counts["load"] == load0
            assert detail_screen(app)._poll_timer is None

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
