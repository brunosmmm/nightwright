"""SPEC-0476: campaign detail Runs pane."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import threading
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import DataTable, TabbedContent

import tui_harness as TH
from test_agentic_dashboard import _fixture
from test_tui_campaign_detail import open_screen
from test_tui_campaign_detail_ac import (
    MULTI,
    _hashes,
    _manifest_cids,
    detail_screen,
    overview,
)
from test_tui_queue_tab import SIZE, _desk, _run, press, select, settle
from nightwright.agentic import history as Hist
from nightwright.agentic import store as S
from nightwright.agentic import tail as TL
from nightwright.tui import agentic_model as AM
from nightwright.tui import campaign_detail as CD
from nightwright.tui.app import IdeaDesk
from waits import pilot_until

GATE_TITLE = "Command reference page does not list the new top-level `worker` command"
MAJOR_TITLE = "`origin/<rest>` base name path (refs/remotes/origin/*) untested"
CID_MULTI = MULTI


def runs_detail(app) -> str:
    return str(detail_screen(app).query_one("#runs-detail-text").content)


def runs_table(app) -> DataTable:
    return detail_screen(app).query_one("#campaign-runs", DataTable)


def run_row_keys(tbl: DataTable) -> set[str]:
    return {str(tbl.ordered_rows[i].key.value) for i in range(tbl.row_count)}


def _cid_with_runs(cfg):
    for cid in _manifest_cids():
        if Hist.campaign_history(cfg, cid).get("runs"):
            return cid
    pytest.skip("no fixture campaign with runs")


def test_runs_table_and_columns(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _cid_with_runs(cfg)
    hist = Hist.campaign_history(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            await press(app, pilot, ["3"])
            assert await pilot_until(pilot, lambda: runs_table(app).row_count > 0)
            s = detail_screen(app)
            assert "placeholder: runs" not in runs_detail(app)
            tbl = runs_table(app)
            assert len(tbl.columns) == len(AM.RUNS_TABLE_COLUMNS)
            assert tbl.row_count == len(hist["runs"])
            keys = run_row_keys(tbl)
            for row in hist["runs"]:
                assert row["run_id"] in keys

    _run(drive())


def test_focus_select_run_and_unknown(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    hist = Hist.campaign_history(cfg, cid)
    run_id = "001-implementer"
    assert any(r["run_id"] == run_id for r in hist["runs"])
    app = IdeaDesk(cfg, tab="queue")
    app.notes = []

    def notify(msg, **k):
        app.notes.append((msg, k))

    app.notify = notify

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs", "run": run_id})
            await settle(app, pilot)
            s = detail_screen(app)
            assert s.query_one("#detail-body", TabbedContent).active == "tab-runs"
            assert runs_table(app).cursor_row == next(
                i for i, r in enumerate(hist["runs"]) if r["run_id"] == run_id)
            assert "done" in runs_detail(app) or "status" in runs_detail(app).lower()
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs", "run": "nope-run"})
            await settle(app, pilot)
            s = detail_screen(app)
            assert s.query_one("#detail-body", TabbedContent).active == "tab-overview"
            assert any("not found: nope-run" in m for m, _ in app.notes)
            app.open_campaign_detail(cid)
            await settle(app, pilot)
            s = detail_screen(app)
            await press(app, pilot, ["1"])
            await settle(app, pilot)
            s.select_run(run_id)
            await settle(app, pilot)
            assert s.query_one("#detail-body", TabbedContent).active == "tab-runs"
            tab_before = s.query_one("#detail-body", TabbedContent).active
            s.select_run("missing-id")
            assert any("not found: missing-id" in m for m, _ in app.notes)
            assert s.query_one("#detail-body", TabbedContent).active == tab_before

    _run(drive())


def test_gate_and_reviewer_blockers_in_pane(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    mark = "[bold]tagged[/bold]"
    (S.runs_dir(cfg, cid) / "002-gate_triager" / "stderr.log").write_text(
        "\n".join(f"e{i}" for i in range(250)) + "\n" + mark, encoding="utf-8")
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run("002-gate_triager")
            assert await pilot_until(pilot, lambda: GATE_TITLE in runs_detail(app))
            assert "docs/product/reference/index.md" in runs_detail(app)
            assert mark in runs_detail(app)
            s.select_run("020-test_reviewer")
            assert await pilot_until(pilot, lambda: MAJOR_TITLE in runs_detail(app))
            conf = json.loads(
                (S.runs_dir(cfg, cid) / "033-conformance_reviewer" / "run.json").read_text(
                    encoding="utf-8"))
            for nit in (f["title"] for f in conf["verdict"]["findings"]
                        if f.get("severity") == "nit"):
                assert nit not in runs_detail(app)

    _run(drive())


def test_verdict_blockers_string_rendered(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    impl = S.runs_dir(cfg, cid) / "001-implementer"
    rj = json.loads((impl / "run.json").read_text(encoding="utf-8"))
    rj.setdefault("verdict", {})["blockers"] = ["[/]network down"]
    (impl / "run.json").write_text(json.dumps(rj), encoding="utf-8")
    text = AM.load_run_pane_detail(cfg, cid, "001-implementer").text.plain
    assert "major: [/]network down" in text


def test_stream_result_and_stream_tail_unit(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    hist = Hist.campaign_history(cfg, cid)
    stream_id = next((r["run_id"] for r in hist["runs"] if r.get("has_stream")), None)
    no_stream = next((r["run_id"] for r in hist["runs"] if not r.get("has_stream")), None)
    if stream_id is None or no_stream is None:
        pytest.skip("fixture missing stream / pre-stream runs")
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            await press(app, pilot, ["3"])
            s = detail_screen(app)
            s.select_run(stream_id)
            await settle(app, pilot)
            text = runs_detail(app)
            assert text.strip()
            s.select_run(no_stream)
            assert await pilot_until(pilot, lambda: runs_detail(app).strip())
            detail = runs_detail(app)
            assert detail.strip() and "total_cost_usd" not in detail[:200]
            rd = S.runs_dir(cfg, cid) / no_stream
            result = rd / "result.json"
            if result.is_file():
                result.unlink()
            s = detail_screen(app)
            s.select_run(no_stream)
            assert await pilot_until(pilot, lambda: "no result.json" in runs_detail(app))

    _run(drive())
    run_dir = S.runs_dir(cfg, cid) / stream_id
    renderer = AM._run_renderer(cfg, run_dir)
    lines1, off1 = AM.stream_tail(cfg, cid, stream_id, 0, renderer)
    lines2, off2 = AM.stream_tail(cfg, cid, stream_id, off1, renderer)
    assert off2 >= off1
    assert isinstance(lines1, list) and isinstance(lines2, list)


def test_load_run_detail_off_main_thread(tmp_path, monkeypatch):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _cid_with_runs(cfg)
    main = threading.current_thread()
    seen = []
    real = AM.load_run_pane_detail

    def tracked(cfg, campaign_id, run_id):
        seen.append(threading.current_thread())
        return real(cfg, campaign_id, run_id)

    monkeypatch.setattr(AM, "load_run_pane_detail", tracked)
    app = _desk(cfg, tab="queue")
    run_id = Hist.campaign_history(cfg, cid)["runs"][0]["run_id"]

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            detail_screen(app).select_run(run_id)
            assert await pilot_until(pilot, lambda: seen and seen[0] is not main)

    _run(drive())


def test_load_run_pane_error_lines(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    camp = S.campaign_dir(cfg, cid)
    shutil.copytree(camp, camp.parent / f"{cid}-err")
    cid2 = f"{cid}-err"
    runs = S.runs_dir(cfg, cid2)
    shutil.rmtree(runs / "001-implementer")
    bad_id = "098-bad"
    (runs / bad_id).mkdir()
    (runs / bad_id / "run.json").write_text("{", encoding="utf-8")
    (runs / "002-gate_triager" / "stderr.log").unlink(missing_ok=True)
    assert "no run directory" in AM.load_run_pane_detail(cfg, cid2, "001-implementer").text.plain
    assert "run.json malformed" in AM.load_run_pane_detail(cfg, cid2, bad_id).text.plain
    assert "no stderr" in AM.load_run_pane_detail(cfg, cid2, "002-gate_triager").text.plain


def test_runs_pane_no_writes(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _cid_with_runs(cfg)
    root = S.campaign_dir(cfg, cid)
    before = _hashes(root)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            await press(app, pilot, ["3", "j", "k", "down", "up"])
            await settle(app, pilot)

    _run(drive())
    assert _hashes(root) == before


def test_timeline_tab_does_not_tail(tmp_path, monkeypatch):
    tail_calls = []
    real = AM.stream_tail

    def spy(*a, **k):
        tail_calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(AM, "stream_tail", spy)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _cid_with_runs(cfg)
    hist = Hist.campaign_history(cfg, cid)
    run_id = next((r["run_id"] for r in hist["runs"] if r.get("has_stream")), None)
    if not run_id:
        pytest.skip("no stream run")
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            s = detail_screen(app)
            s.select_run(run_id)
            assert await pilot_until(pilot, lambda: runs_detail(app).strip())
            n = len(tail_calls)
            await press(app, pilot, ["2"])
            await settle(app, pilot)
            await pilot.pause(0.3)
            assert tail_calls == n

    _run(drive())


def test_live_follow_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["impl"]
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-live"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    runs = S.runs_dir(cfg, cid2)
    run_id = "002-implementer"
    run_dir = runs / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stream.jsonl").write_text("", encoding="utf-8")
    (run_dir / "request.json").write_text(json.dumps({"persona": "implementer"}), encoding="utf-8")
    st = json.loads((S.campaign_dir(cfg, cid2) / "state.json").read_text(encoding="utf-8"))
    st["state"] = "IMPLEMENTING"
    (S.campaign_dir(cfg, cid2) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    (run_dir / "exit.json").unlink(missing_ok=True)
    (run_dir / "run.json").unlink(missing_ok=True)
    ended = {"v": False}

    def fake_ended(rd):
        return ended["v"]

    monkeypatch.setattr(TL, "_ended", fake_ended)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            r0 = s._run_renderer
            line = json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "live-append-0476"}]}})
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
            assert await pilot_until(pilot, lambda: "live-append-0476" in runs_detail(app))
            assert s._run_renderer is r0
            ended["v"] = True
            frag = json.dumps({"type": "result", "result": "done"})
            with open(run_dir / "stream.jsonl", "ab") as f:
                f.write(frag.encode("utf-8"))
            await pilot_until(pilot, lambda: s._run_follow_done)
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "after-end"}]}}) + "\n")
            await settle(app, pilot)
            assert "after-end" not in runs_detail(app)

    _run(drive())
