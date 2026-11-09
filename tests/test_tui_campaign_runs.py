"""SPEC-0476: campaign detail Runs pane."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import threading
import time
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
CID_STREAM = "SPEC-0394-20261102-ae5b"


def _cell_plain(row: dict, key: str) -> str:
    val = row.get(key)
    if val is None:
        return ""
    if key == "cost_usd" and isinstance(val, (int, float)):
        return f"{val:.4f}"
    return str(val)


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
    cid = CID_MULTI
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
            assert [c.label.plain for c in tbl.ordered_columns] == list(AM.RUNS_TABLE_COLUMNS)
            assert tbl.row_count == len(hist["runs"])
            keys = [str(tbl.ordered_rows[i].key.value) for i in range(tbl.row_count)]
            assert keys == [r["run_id"] for r in hist["runs"]]
            for i, row in enumerate(hist["runs"]):
                cells = [c.plain for c in tbl.get_row_at(i)]
                assert cells == [_cell_plain(row, k) for k in AM.RUNS_TABLE_COLUMNS]

    _run(drive())


def test_focus_select_run_and_unknown(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    hist = Hist.campaign_history(cfg, cid)
    run_id = "020-test_reviewer"
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
            assert await pilot_until(
                pilot, lambda: MAJOR_TITLE in runs_detail(app))
            s = detail_screen(app)
            assert s.query_one("#detail-body", TabbedContent).active == "tab-runs"
            assert runs_table(app).cursor_row == next(
                i for i, r in enumerate(hist["runs"]) if r["run_id"] == run_id)
            assert str(AM._verdict_status_line(json.loads(
                (S.runs_dir(cfg, cid) / run_id / "run.json").read_text(encoding="utf-8")
            ).get("verdict"))) in runs_detail(app)
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
            assert await pilot_until(
                pilot, lambda: MAJOR_TITLE in runs_detail(app))
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
            assert await pilot_until(pilot, lambda: f"must_fix: {GATE_TITLE}" in runs_detail(app))
            assert "  files: docs/product/reference/index.md" in runs_detail(app)
            assert mark in runs_detail(app)
            detail_gt = runs_detail(app)
            assert "e50" not in detail_gt.splitlines()
            assert "e51" in detail_gt.splitlines()
            s.select_run("020-test_reviewer")
            assert await pilot_until(pilot, lambda: f"major: {MAJOR_TITLE}" in runs_detail(app))
            rev = json.loads(
                (S.runs_dir(cfg, cid) / "020-test_reviewer" / "run.json").read_text(
                    encoding="utf-8"))
            for minor in (f["title"] for f in rev["verdict"]["findings"]
                          if f.get("severity") == "minor"):
                assert f"minor: {minor}" not in runs_detail(app)
            s.select_run("033-conformance_reviewer")
            conf = json.loads(
                (S.runs_dir(cfg, cid) / "033-conformance_reviewer" / "run.json").read_text(
                    encoding="utf-8"))
            vline = AM._verdict_status_line(conf.get("verdict"))
            assert await pilot_until(pilot, lambda: vline and vline in runs_detail(app))
            detail = runs_detail(app)
            for nit in (f["title"] for f in conf["verdict"]["findings"]
                        if f.get("severity") == "nit"):
                assert f"nit: {nit}" not in detail

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
    cid_stream = CID_STREAM
    hist_stream = Hist.campaign_history(cfg, cid_stream)
    stream_id = next((r["run_id"] for r in hist_stream["runs"] if r.get("has_stream")), None)
    assert stream_id is not None
    stream_path = S.runs_dir(cfg, cid_stream) / stream_id / "stream.jsonl"
    assert stream_path.is_file()
    snap = AM.load_run_pane_detail(cfg, cid_stream, stream_id)
    assert snap.offset == stream_path.stat().st_size
    assert "■ result:" in snap.text.plain
    raw = stream_path.read_text(encoding="utf-8")
    assert raw.splitlines()[-1] not in snap.text.plain or "{" not in snap.text.plain.split("\n")[-3:]

    cid_pre = CID_MULTI
    hist_pre = Hist.campaign_history(cfg, cid_pre)
    no_stream = next((r["run_id"] for r in hist_pre["runs"] if not r.get("has_stream")), None)
    assert no_stream is not None
    # 001-implementer: structured_output is an object → show file tail, not raw JSON keys only
    impl_result = (S.runs_dir(cfg, cid_pre) / no_stream / "result.json").read_text(encoding="utf-8")
    pre_snap = AM.load_run_pane_detail(cfg, cid_pre, no_stream)
    tail_lines = impl_result.splitlines()[-200:]
    for ln in tail_lines[-3:]:
        assert ln in pre_snap.text.plain

    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid_stream, "delivered", timeline=False)
            await press(app, pilot, ["3"])
            s = detail_screen(app)
            s.select_run(stream_id)
            assert await pilot_until(pilot, lambda: runs_detail(app).strip())
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            await open_screen(app, pilot, cid_pre, "delivered", timeline=False)
            await press(app, pilot, ["3"])
            s = detail_screen(app)
            s.select_run(no_stream)
            assert await pilot_until(pilot, lambda: runs_detail(app).strip())
            rd = S.runs_dir(cfg, cid_pre) / no_stream
            result = rd / "result.json"
            if result.is_file():
                result.unlink()
            s.select_run(no_stream)
            assert await pilot_until(pilot, lambda: "no result.json" in runs_detail(app))

    _run(drive())
    run_dir = S.runs_dir(cfg, cid_stream) / stream_id
    renderer = AM._run_renderer(cfg, run_dir)
    lines1, off1 = AM.stream_tail(cfg, cid_stream, stream_id, 0, renderer)
    assert lines1 and "■ result:" in "\n".join(lines1)
    assert off1 == stream_path.stat().st_size
    renderer2 = AM._run_renderer(cfg, run_dir)
    lines2, off2 = AM.stream_tail(cfg, cid_stream, stream_id, off1, renderer2)
    assert lines2 == []
    assert off2 == off1
    append = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "live-append-0476"}]}}) + "\n"
    with open(stream_path, "a", encoding="utf-8") as f:
        f.write(append)
    lines3, off3 = AM.stream_tail(cfg, cid_stream, stream_id, off1, renderer)
    assert any("live-append-0476" in ln for ln in lines3)
    assert off3 == stream_path.stat().st_size
    assert not any(ln.lstrip().startswith("{") and '"type":' in ln for ln in lines3)


def test_ended_stream_trailing_fragment(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_STREAM
    run_id = "001-implementer"
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-frag"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    run_dir = S.runs_dir(cfg, cid2) / run_id
    stream = run_dir / "stream.jsonl"
    frag = json.dumps({"type": "result", "subtype": "success", "result": "frag-0476"})
    stream.write_bytes(frag.encode("utf-8"))
    assert (run_dir / "run.json").is_file()
    snap = AM.load_run_pane_detail(cfg, cid2, run_id)
    assert "■ result:" in snap.text.plain
    assert snap.offset == stream.stat().st_size


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
    cache = Path(cfg["cache_dir"])
    before = _hashes(root)
    cache_before = _hashes(cache) if cache.is_dir() else {}
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            await press(app, pilot, ["3", "j", "k", "down", "up"])
            await settle(app, pilot)

    _run(drive())
    assert _hashes(root) == before
    assert _hashes(cache) == cache_before if cache.is_dir() else cache_before == {}


def test_timeline_tab_does_not_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    tail_calls = []
    real = AM.stream_tail

    def spy(*a, **k):
        tail_calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(AM, "stream_tail", spy)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_STREAM
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-tl"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    st = json.loads((S.campaign_dir(cfg, cid2) / "state.json").read_text(encoding="utf-8"))
    st["state"] = "IMPLEMENTING"
    (S.campaign_dir(cfg, cid2) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    hist = Hist.campaign_history(cfg, cid2)
    run_id = next((r["run_id"] for r in hist["runs"] if r.get("has_stream")), None)
    assert run_id
    run_dir = S.runs_dir(cfg, cid2) / run_id
    (run_dir / "exit.json").unlink(missing_ok=True)
    (run_dir / "run.json").unlink(missing_ok=True)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(run_id)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            await press(app, pilot, ["2"])
            await settle(app, pilot)
            off0 = s._run_offset
            n0 = len(tail_calls)
            line = json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "timeline-hidden-0476"}]}})
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
            await pilot.pause(0.35)
            assert len(tail_calls) == n0
            assert s._run_offset == off0
            assert "timeline-hidden-0476" not in runs_detail(app)

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
            assert await pilot_until(pilot, lambda: "■ result:" in runs_detail(app))
            assert s._run_follow_done
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "after-end"}]}}) + "\n")
            await settle(app, pilot)
            assert "after-end" not in runs_detail(app)

    _run(drive())


def test_non_first_run_focus_survives_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-reload"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    st = json.loads((S.campaign_dir(cfg, cid2) / "state.json").read_text(encoding="utf-8"))
    st["state"] = "REVIEWING"
    (S.campaign_dir(cfg, cid2) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    hist = Hist.campaign_history(cfg, cid2)
    run_id = "020-test_reviewer"
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            await settle(app, pilot)
            assert await pilot_until(pilot, lambda: MAJOR_TITLE in runs_detail(app))
            s = detail_screen(app)
            row = next(i for i, r in enumerate(hist["runs"]) if r["run_id"] == run_id)
            assert runs_table(app).cursor_row == row
            r0 = s._run_renderer
            sig0 = s._signature
            ledger = S.campaign_dir(cfg, cid2) / "ledger.jsonl"
            with open(ledger, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "schema": "nw.test.v1", "ts": "2026-11-09T12:00:00-05:00",
                    "kind": "note", "text": "reload-0476"}) + "\n")
            assert await pilot_until(pilot, lambda: detail_screen(app)._signature != sig0)
            s = detail_screen(app)
            assert runs_table(app).cursor_row == row
            assert str(runs_table(app).ordered_rows[row].key.value) == run_id
            assert s._run_renderer is r0

    _run(drive())


def test_follow_run_json_without_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_STREAM
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-rj"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    st = json.loads((S.campaign_dir(cfg, cid2) / "state.json").read_text(encoding="utf-8"))
    st["state"] = "IMPLEMENTING"
    (S.campaign_dir(cfg, cid2) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    hist = Hist.campaign_history(cfg, cid2)
    run_id = next((r["run_id"] for r in hist["runs"] if r.get("has_stream")), None)
    run_dir = S.runs_dir(cfg, cid2) / run_id
    (run_dir / "exit.json").unlink(missing_ok=True)
    assert (run_dir / "run.json").is_file()
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(run_id)
            assert await pilot_until(pilot, lambda: s._run_follow_done)
            before = runs_detail(app)
            line = json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "no-follow-rj-0476"}]}})
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
            await pilot.pause(0.35)
            assert runs_detail(app) == before

    _run(drive())


def test_select_run_replaces_renderer_and_offset(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_STREAM
    hist = Hist.campaign_history(cfg, cid)
    runs = [r["run_id"] for r in hist["runs"] if r.get("has_stream")]
    assert len(runs) >= 2
    a, b = runs[0], runs[1]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(a)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            r0 = s._run_renderer
            off0 = s._run_offset
            s.select_run(b)
            assert await pilot_until(
                pilot, lambda: s._run_renderer is not None and s._run_renderer is not r0)
            b_path = S.runs_dir(cfg, cid) / b / "stream.jsonl"
            assert s._run_offset == b_path.stat().st_size

    _run(drive())


def test_string_result_and_files_raw(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-raw"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    run_id = "001-implementer"
    run_dir = S.runs_dir(cfg, cid2) / run_id
    (run_dir / "stream.jsonl").unlink(missing_ok=True)
    body = "\n".join(f"line{i}" for i in range(250))
    (run_dir / "result.json").write_text(
        json.dumps({"result": body, "structured_output": "not-an-object"}), encoding="utf-8")
    snap = AM.load_run_pane_detail(cfg, cid2, run_id)
    assert "line49" not in snap.text.plain
    assert "line50" in snap.text.plain
    assert '"structured_output"' not in snap.text.plain
    (run_dir / "alt.txt").write_text("alt-raw-file-0476\n", encoding="utf-8")
    rj = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    rj.setdefault("files", {})["raw"] = "alt.txt"
    (run_dir / "run.json").write_text(json.dumps(rj), encoding="utf-8")
    assert "alt-raw-file-0476" in AM.load_run_pane_detail(cfg, cid2, run_id).text.plain
    rj["files"]["raw"] = "../../../outside"
    (run_dir / "run.json").write_text(json.dumps(rj), encoding="utf-8")
    assert "alt-raw-file-0476" in AM.load_run_pane_detail(cfg, cid2, run_id).text.plain


def test_runs_cursor_jk_and_arrows(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    hist = Hist.campaign_history(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            await press(app, pilot, ["3"])
            tbl = runs_table(app)
            assert await pilot_until(pilot, lambda: tbl.row_count > 1)
            row0 = tbl.cursor_row
            rid0 = str(tbl.ordered_rows[row0].key.value)
            await press(app, pilot, ["down"])
            assert await pilot_until(pilot, lambda: tbl.cursor_row == row0 + 1)
            await press(app, pilot, ["up"])
            assert await pilot_until(pilot, lambda: tbl.cursor_row == row0)
            await press(app, pilot, ["j"])
            assert await pilot_until(pilot, lambda: tbl.cursor_row == row0 + 1)
            rid1 = str(tbl.ordered_rows[tbl.cursor_row].key.value)
            assert rid1 == hist["runs"][row0 + 1]["run_id"]
            await press(app, pilot, ["k"])
            assert await pilot_until(pilot, lambda: tbl.cursor_row == row0)

    _run(drive())


def test_stream_appears_after_first_paint(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["impl"]
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-late-stream"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    run_id = "002-implementer"
    run_dir = S.runs_dir(cfg, cid2) / run_id
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
    late_line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "late-stream-0476"}]}}) + "\n"

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            (run_dir / "stream.jsonl").unlink(missing_ok=True)
            s._run_has_stream = False
            s._run_renderer = None
            s._run_follow_done = False
            (run_dir / "stream.jsonl").write_text(late_line, encoding="utf-8")
            assert await pilot_until(pilot, lambda: "late-stream-0476" in runs_detail(app))
            assert not s._run_follow_done

    _run(drive())


def test_poll_does_not_cancel_slow_detail_load(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["impl"]
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-slow-detail"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    runs = S.runs_dir(cfg, cid2)
    run_id = "002-implementer"
    run_dir = runs / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stream.jsonl").write_text("", encoding="utf-8")
    (run_dir / "request.json").write_text(json.dumps({"persona": "implementer"}), encoding="utf-8")
    slow_line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "slow-detail-0476"}]}}) + "\n"
    st = json.loads((S.campaign_dir(cfg, cid2) / "state.json").read_text(encoding="utf-8"))
    st["state"] = "IMPLEMENTING"
    (S.campaign_dir(cfg, cid2) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    (run_dir / "exit.json").unlink(missing_ok=True)
    (run_dir / "run.json").unlink(missing_ok=True)
    ended = {"v": False}

    def fake_ended(rd):
        return ended["v"]

    monkeypatch.setattr(TL, "_ended", fake_ended)
    real = AM.load_run_pane_detail

    def slow(cfg, campaign_id, rid):
        rd = S.runs_dir(cfg, campaign_id) / rid
        with open(rd / "stream.jsonl", "a", encoding="utf-8") as f:
            f.write(slow_line)
        time.sleep(max(CD.POLL_S * 3, 0.2))
        return real(cfg, campaign_id, rid)

    monkeypatch.setattr(AM, "load_run_pane_detail", slow)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            assert await pilot_until(pilot, lambda: "slow-detail-0476" in runs_detail(app))
            extra = json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "slow-append-once"}]}}) + "\n"
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(extra)
            assert await pilot_until(pilot, lambda: "slow-append-once" in runs_detail(app))
            assert runs_detail(app).count("slow-append-once") == 1

    _run(drive())


def test_poll_does_not_cancel_slow_stream_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["impl"]
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-slow-tail"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    run_id = "002-implementer"
    run_dir = S.runs_dir(cfg, cid2) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stream.jsonl").write_text("", encoding="utf-8")
    st = json.loads((S.campaign_dir(cfg, cid2) / "state.json").read_text(encoding="utf-8"))
    st["state"] = "IMPLEMENTING"
    (S.campaign_dir(cfg, cid2) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    (run_dir / "exit.json").unlink(missing_ok=True)
    (run_dir / "run.json").unlink(missing_ok=True)
    ended = {"v": False}
    monkeypatch.setattr(TL, "_ended", lambda rd: ended["v"])
    real = AM.stream_tail

    def slow_tail(*a, **k):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "slow-tail-0476"}]}})
        path = S.runs_dir(a[0], a[1]) / a[2] / "stream.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        time.sleep(max(CD.POLL_S * 3, 0.2))
        return real(*a, **k)

    monkeypatch.setattr(AM, "stream_tail", slow_tail)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            off0 = s._run_offset
            assert await pilot_until(pilot, lambda: "slow-tail-0476" in runs_detail(app))
            assert s._run_offset > off0
            assert runs_detail(app).count("slow-tail-0476") == 1

    _run(drive())
