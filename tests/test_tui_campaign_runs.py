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

pytestmark = pytest.mark.tui

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
    try:
        return str(detail_screen(app).query_one("#runs-detail-text").content)
    except Exception:  # popup still composing
        return ""


def runs_table(app) -> DataTable:
    return detail_screen(app).query_one("#campaign-runs", DataTable)


def run_row_keys(tbl: DataTable) -> set[str]:
    return {str(tbl.ordered_rows[i].key.value) for i in range(tbl.row_count)}


def _spy_polls(monkeypatch):
    """Counts completed follow polls so 'nothing appeared' is asserted after real ticks."""
    calls = []
    real = CD.CampaignDetail._runs_follow_poll

    async def spy(self, *a, **k):
        try:
            return await real(self, *a, **k)
        finally:
            calls.append(1)

    monkeypatch.setattr(CD.CampaignDetail, "_runs_follow_poll", spy)
    return calls


async def _after_ticks(pilot, calls, n=3):
    base = len(calls)
    assert await pilot_until(pilot, lambda: len(calls) >= base + n)


def _feed_recorder(monkeypatch):
    """Every Renderer.feed_line call records (renderer id, fed line) in `fed`."""
    fed = []
    real = TL.Renderer.feed_line

    def feed(self, line):
        fed.append((id(self), line))
        return real(self, line)

    monkeypatch.setattr(TL.Renderer, "feed_line", feed)
    return fed


def _asst(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}})


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
            assert [c.label.plain for c in tbl.ordered_columns] == [
                "run_id", "persona", "phase", "status", "verdict_status", "duration_s",
                "cost_usd"]
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
            vr = json.loads(
                (S.runs_dir(cfg, cid) / run_id / "run.json").read_text(encoding="utf-8")
            ).get("verdict") or {}
            assert vr.get("verdict") in runs_detail(app)
            assert str(vr.get("score")) in runs_detail(app)
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


def test_select_unknown_run_keeps_non_runs_tab(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    app = IdeaDesk(cfg, tab="queue")
    app.notes = []
    app.notify = lambda msg, **k: app.notes.append((msg, k))

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid)
            await settle(app, pilot)
            s = detail_screen(app)
            body = s.query_one("#detail-body", TabbedContent)
            assert body.active == "tab-overview"
            s.select_run("missing-id")
            await settle(app, pilot)
            assert any("not found: missing-id" in m for m, _ in app.notes)
            assert body.active == "tab-overview"
            await press(app, pilot, ["2"])
            await settle(app, pilot)
            start = body.active
            assert start != "tab-runs"
            app.notes.clear()
            s.select_run("missing-id-2")
            await settle(app, pilot)
            assert any("not found: missing-id-2" in m for m, _ in app.notes)
            assert body.active == start

    _run(drive())


def _live_run_copy(tmp_path, monkeypatch, tag: str, *, stream: bool):
    """A copied campaign whose run 002-implementer looks live: no exit.json or run.json,
    `TL._ended` reads the returned flag dict."""
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["impl"]
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-{tag}"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    run_id = "002-implementer"
    run_dir = S.runs_dir(cfg, cid2) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if stream:
        (run_dir / "stream.jsonl").write_text(_asst("seed-head") + "\n", encoding="utf-8")
    (run_dir / "request.json").write_text(json.dumps({"persona": "implementer"}), encoding="utf-8")
    st = json.loads((S.campaign_dir(cfg, cid2) / "state.json").read_text(encoding="utf-8"))
    st["state"] = "IMPLEMENTING"
    (S.campaign_dir(cfg, cid2) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    for name in ("exit.json", "run.json", "result.json", "stderr.log"):
        (run_dir / name).unlink(missing_ok=True)
    ended = {"v": False}
    monkeypatch.setattr(TL, "_ended", lambda rd: ended["v"])
    return cfg, cid2, run_id, run_dir, ended


_LATE_RUN_JSON = {"verdict": {"status": "late-verdict-0476", "blockers": ["late-blocker-0476"]}}


def test_head_refreshes_when_run_json_lands_after_exit(tmp_path, monkeypatch):
    polls = _spy_polls(monkeypatch)
    cfg, cid2, run_id, run_dir, ended = _live_run_copy(tmp_path, monkeypatch, "late-head", stream=True)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            ended["v"] = True  # exit.json first: the run is over, the verdict is not there yet
            assert await pilot_until(pilot, lambda: s._run_follow_done)
            assert "late-verdict-0476" not in runs_detail(app)
            assert not s._run_head_done
            (run_dir / "run.json").write_text(json.dumps(_LATE_RUN_JSON), encoding="utf-8")
            assert await pilot_until(pilot, lambda: "late-verdict-0476" in runs_detail(app))
            assert "major: late-blocker-0476" in runs_detail(app)
            assert await pilot_until(pilot, lambda: s._run_head_done)
            await _after_ticks(pilot, polls)
            assert "late-verdict-0476" in runs_detail(app)

    _run(drive())


def test_non_stream_run_head_and_result_refresh_when_it_finishes(tmp_path, monkeypatch):
    cfg, cid2, run_id, run_dir, ended = _live_run_copy(tmp_path, monkeypatch, "late-nostream", stream=False)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_loaded_id == run_id)
            assert "no result.json" in runs_detail(app)
            assert not s._run_head_done
            ended["v"] = True
            (run_dir / "result.json").write_text(
                json.dumps({"result": "final-result-0476"}), encoding="utf-8")
            (run_dir / "run.json").write_text(json.dumps(_LATE_RUN_JSON), encoding="utf-8")
            assert await pilot_until(pilot, lambda: "late-verdict-0476" in runs_detail(app))
            assert "major: late-blocker-0476" in runs_detail(app)
            assert await pilot_until(pilot, lambda: "final-result-0476" in runs_detail(app))
            assert "no result.json" not in runs_detail(app)
            assert s._run_head_done

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
            cv = conf.get("verdict") or {}
            assert await pilot_until(
                pilot, lambda: str(cv.get("score")) in runs_detail(app))
            assert cv.get("verdict") in runs_detail(app)
            detail = runs_detail(app)
            for nit in (f["title"] for f in conf["verdict"]["findings"]
                        if f.get("severity") == "nit"):
                assert f"nit: {nit}" not in detail

    _run(drive())


def test_verdict_line_lead_priority(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    run_id = "001-implementer"
    run_dir = S.runs_dir(cfg, cid) / run_id
    rj = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    rj["verdict"] = {
        "status": "status-lead-0476",
        "disposition": "disp-0476",
        "verdict": "verdict-0476",
        "score": 7,
        "summary": "summary-0476",
    }
    (run_dir / "run.json").write_text(json.dumps(rj), encoding="utf-8")
    plain = AM.load_run_pane_detail(cfg, cid, run_id).text.plain.splitlines()[0]
    assert plain.startswith("status-lead-0476")
    assert "disp-0476" not in plain
    assert "verdict-0476" not in plain
    assert "7" in plain
    assert "summary-0476" in plain


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

    assert not any(ln.lstrip().startswith("{") and '"type":' in ln
                   for ln in snap.text.plain.splitlines())

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
    real_feed = TL.Renderer.feed_line
    fed_here = []

    def rec(self, line):
        fed_here.append((self, line))
        return real_feed(self, line)

    TL.Renderer.feed_line = rec
    try:
        lines3, off3 = AM.stream_tail(cfg, cid_stream, stream_id, off1, renderer)
    finally:
        TL.Renderer.feed_line = real_feed
    assert [(r is renderer, ln) for r, ln in fed_here] == [(True, append.rstrip("\n"))]
    assert any("live-append-0476" in ln for ln in lines3)
    assert off3 == stream_path.stat().st_size
    assert not any(ln.lstrip().startswith("{") and '"type":' in ln for ln in lines3)


def _tool_result_event(content: str) -> str:
    return json.dumps({"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": "t1", "content": content}]}})


def _tool_out(i: int) -> str:
    """The rendered line of `_tool_result_event(f"tool-out-{i:04d}")`."""
    return f"  ← 1 line: tool-out-{i:04d}"


def _multi_line_event(i: int) -> str:
    """One assistant event with three tool_use blocks: three rendered lines."""
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"u{i}{k}", "name": "Bash", "input": {"command": f"cmd-{i:03d}-{k}"}}
        for k in range(3)]}})


def _stream_copy(cfg, tag):
    cid = _copy_campaign(cfg, CID_STREAM, tag)
    rid = "001-implementer"
    return cid, rid, S.runs_dir(cfg, cid) / rid


def test_stream_initial_paint_caps_rendered_lines(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid3, rid, run_dir = _stream_copy(cfg, "stream200")
    stream = run_dir / "stream.jsonl"
    for n in (199, 200, 201, 250):
        stream.write_text("\n".join(_tool_result_event(f"tool-out-{i:04d}") for i in range(n))
                          + "\n", encoding="utf-8")
        snap = AM.load_run_pane_detail(cfg, cid3, rid)
        assert len(snap.body) == min(n, 200), n
        assert snap.body[0] == _tool_out(max(n - 200, 0)), n
        assert snap.body[-1] == _tool_out(n - 1), n
        assert snap.offset == stream.stat().st_size, n
        if n > 200:
            assert _tool_out(n - 201) not in snap.body
    # The cap counts rendered lines, not events: 100 three-line events keep the last 200 lines.
    events = [_multi_line_event(i) for i in range(100)]
    stream.write_text("\n".join(events) + "\n", encoding="utf-8")
    snap = AM.load_run_pane_detail(cfg, cid3, rid)
    assert len(snap.body) == 200
    assert snap.body[-1] == "→ Bash cmd-099-2"
    assert snap.body[0] == "→ Bash cmd-033-1"
    assert snap.offset == stream.stat().st_size


def test_stream_initial_paint_cap_in_popup(tmp_path, monkeypatch):
    cfg, cid, run_id, run_dir, ended = _live_run_copy(tmp_path, monkeypatch, "cap", stream=True)
    (run_dir / "stream.jsonl").write_text(
        "\n".join(_tool_result_event(f"tool-out-{i:04d}") for i in range(250)) + "\n",
        encoding="utf-8")
    (run_dir / "stderr.log").write_text(
        "\n".join(f"err-{i:04d}" for i in range(250)) + "\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({"verdict": {"status": "verdict-0564", "summary": "sum"}}), encoding="utf-8")
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: _tool_out(249) in runs_detail(app))
            lines = runs_detail(app).splitlines()
            assert lines[0] == "verdict-0564 sum"
            assert _tool_out(50) in lines and _tool_out(49) not in lines
            assert sum(ln.startswith("  ← 1 line: tool-out-") for ln in lines) == 200
            # The stderr tail is not capped by the stream bound.
            assert all(f"err-{i:04d}" in lines for i in range(50, 250))
            assert "err-0049" not in lines
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(_tool_result_event("tool-out-0250") + "\n")
            assert await pilot_until(pilot, lambda: _tool_out(250) in runs_detail(app))
            assert _tool_out(50) in runs_detail(app).splitlines()
            assert s._run_follow_done is False

    _run(drive())


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

    def tracked(cfg, campaign_id, run_id, *extra):
        seen.append(threading.current_thread())
        return real(cfg, campaign_id, run_id, *extra)

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
    polls = _spy_polls(monkeypatch)
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
            # Positive control: follow does tail on the visible Runs tab in this same setup.
            assert await pilot_until(pilot, lambda: len(tail_calls) > 0)
            await press(app, pilot, ["2"])
            await settle(app, pilot)
            off0 = s._run_offset
            n0 = len(tail_calls)
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(_asst("timeline-hidden-0476") + "\n")
            await _after_ticks(pilot, polls)
            assert len(tail_calls) == n0
            assert s._run_offset == off0
            assert "timeline-hidden-0476" not in runs_detail(app)

    _run(drive())


def test_live_follow_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    polls = _spy_polls(monkeypatch)
    fed = _feed_recorder(monkeypatch)
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
    (run_dir / "stream.jsonl").write_text(_asst("seed-0476") + "\n", encoding="utf-8")
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
            assert await pilot_until(pilot, lambda: s._run_renderer is not None), (
                runs_detail(app), s._run_loaded_id, s._selected_run_id, s._active_tab())
            r0 = s._run_renderer
            assert "seed-0476" in runs_detail(app)
            assert [ln for rid, ln in fed if rid == id(r0)] == [_asst("seed-0476")]
            line = _asst("live-append-0476")
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
            assert await pilot_until(pilot, lambda: "live-append-0476" in runs_detail(app))
            assert s._run_renderer is r0
            # The initial paint and the poll fed the one Renderer.
            assert [ln for rid, ln in fed if rid == id(r0)] == [
                _asst("seed-0476"), _asst("live-append-0476")]
            assert len({rid for rid, _ in fed}) == 1
            ended["v"] = True
            frag = json.dumps({"type": "result", "result": "done"})
            with open(run_dir / "stream.jsonl", "ab") as f:
                f.write(frag.encode("utf-8"))
            assert await pilot_until(pilot, lambda: "■ result:" in runs_detail(app))
            assert s._run_follow_done
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "after-end"}]}}) + "\n")
            await _after_ticks(pilot, polls)
            assert "after-end" not in runs_detail(app)

    _run(drive())


def test_follow_ended_latches_trailing_space(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    polls = _spy_polls(monkeypatch)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["impl"]
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-space-tail"
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
    monkeypatch.setattr(TL, "_ended", lambda rd: ended["v"])
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            ended["v"] = True
            frag = json.dumps({
                "type": "result", "subtype": "success", "result": "space-tail-0476"}) + " "
            with open(run_dir / "stream.jsonl", "ab") as f:
                f.write(frag.encode("utf-8"))
            assert await pilot_until(pilot, lambda: s._run_follow_done)
            assert "■ result:" in runs_detail(app)
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "blocked-space"}]}}) + "\n")
            await _after_ticks(pilot, polls)
            assert "blocked-space" not in runs_detail(app)

    _run(drive())


def test_follow_ended_latches_non_json_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.08)
    polls = _spy_polls(monkeypatch)
    _, cfg, ids = _fixture(tmp_path)
    cid = ids["impl"]
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-bad-tail"
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
    monkeypatch.setattr(TL, "_ended", lambda rd: ended["v"])
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            ended["v"] = True
            with open(run_dir / "stream.jsonl", "ab") as f:
                f.write(b"{")
            assert await pilot_until(pilot, lambda: s._run_follow_done)
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "blocked-brace"}]}}) + "\n")
            await _after_ticks(pilot, polls)
            assert "blocked-brace" not in runs_detail(app)

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
    polls = _spy_polls(monkeypatch)
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
            await _after_ticks(pilot, polls)
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
    outside = run_dir.parent.parent.parent / "outside-sentinel-0476.txt"
    outside.write_text("outside-sentinel-0476\n", encoding="utf-8")
    rj["files"]["raw"] = "../../../outside-sentinel-0476.txt"
    (run_dir / "run.json").write_text(json.dumps(rj), encoding="utf-8")
    esc = AM.load_run_pane_detail(cfg, cid2, run_id).text.plain
    assert "line50" in esc
    assert "line49" not in esc
    assert "alt-raw-file-0476" not in esc
    assert "outside-sentinel-0476" not in esc


    # SPEC-0564: every shape of `files.raw` against a known result.json and two sentinels.
    (run_dir / "result.json").write_text("RESULT-JSON-0564\n", encoding="utf-8")
    (run_dir / "alt.txt").write_text("ALT-0564\n", encoding="utf-8")
    sentinel = run_dir.parent.parent.parent / "outside-0564.txt"
    sentinel.write_text("OUTSIDE-0564\n", encoding="utf-8")
    evil = run_dir.parent / f"{run_id}-evil"
    evil.mkdir()
    (evil / "x.txt").write_text("OUTSIDE-0564\n", encoding="utf-8")
    try:
        (run_dir / "link.txt").symlink_to(sentinel)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks")

    def pane(run_json_files):
        rj = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        rj["files"] = run_json_files
        (run_dir / "run.json").write_text(json.dumps(rj), encoding="utf-8")
        return AM.load_run_pane_detail(cfg, cid2, run_id).text.plain

    hostile = [
        "../../../outside-0564.txt",
        str(sentinel),
        str(run_dir / "alt.txt"),  # absolute, inside the run dir: rejected by is_absolute()
        "link.txt",
        f"../{run_id}-evil/x.txt",
        "   ",
        4,
        ["alt.txt"],
    ]
    for raw in hostile:
        text = pane({"raw": raw})
        assert "RESULT-JSON-0564" in text, raw
        assert "OUTSIDE-0564" not in text and "ALT-0564" not in text, raw
    text = pane(["alt.txt"])
    assert "RESULT-JSON-0564" in text and "ALT-0564" not in text
    for raw in ("alt.txt", "sub/../alt.txt"):
        text = pane({"raw": raw})
        assert "ALT-0564" in text and "RESULT-JSON-0564" not in text, raw
    text = pane({"raw": "missing.txt"})
    assert "no result.json" in text and "RESULT-JSON-0564" not in text
    (run_dir / "run.json").unlink()
    assert "RESULT-JSON-0564" in AM.load_run_pane_detail(cfg, cid2, run_id).text.plain


def test_runs_cursor_jk_and_arrows(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    # 13 runs: the whole table is walked within the TUI per-test budget.
    cid = _copy_campaign(cfg, CID_STREAM, "jk")
    run_ids = [r["run_id"] for r in Hist.campaign_history(cfg, cid)["runs"]]
    assert len(run_ids) >= 3
    for rid in run_ids:
        path = S.runs_dir(cfg, cid) / rid / "run.json"
        rj = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        verdict = rj.get("verdict") if isinstance(rj.get("verdict"), dict) else {}
        verdict["summary"] = f"row-marker-{rid}"
        rj["verdict"] = verdict
        path.write_text(json.dumps(rj), encoding="utf-8")
    markers = {rid: f"row-marker-{rid}" for rid in run_ids}
    assert not any(m in other for m in markers.values() for other in markers.values()
                   if m != other)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs"})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s.view is not None)
            tbl = runs_table(app)
            assert await pilot_until(pilot, lambda: tbl.row_count == len(run_ids))
            assert [str(tbl.ordered_rows[i].key.value) for i in range(tbl.row_count)] == run_ids
            s.select_run(run_ids[0])
            assert await pilot_until(
                pilot, lambda: tbl.cursor_row == 0 and markers[run_ids[0]] in runs_detail(app))

            def only_marker(rid):
                assert markers[rid] in runs_detail(app)
                for other, m in markers.items():
                    if other != rid:
                        assert m not in runs_detail(app), (other, rid)
                assert s._selected_run_id == rid
                assert str(tbl.ordered_rows[tbl.cursor_row].key.value) == rid

            async def step(key, row):
                await pilot.press(key)
                rid = run_ids[row]
                assert await pilot_until(
                    pilot, lambda: tbl.cursor_row == row and markers[rid] in runs_detail(app)), (
                    key, row, tbl.cursor_row, s._selected_run_id)
                only_marker(rid)

            async def boundary(key, row):
                await pilot.press(key)
                await settle(app, pilot)
                assert tbl.cursor_row == row
                only_marker(run_ids[row])

            only_marker(run_ids[0])
            last = len(run_ids) - 1
            for row in range(1, last + 1):
                await step(("down", "j")[(row - 1) % 2], row)
            await boundary("j", last)
            for row in range(last - 1, -1, -1):
                await step(("up", "k")[(last - 1 - row) % 2], row)
            await boundary("k", 0)

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
    (run_dir / "stream.jsonl").unlink(missing_ok=True)
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
            assert await pilot_until(pilot, lambda: s._run_loaded_id == run_id)
            assert s._run_renderer is None and not s._run_has_stream
            assert not s._run_follow_done
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

    def slow(cfg, campaign_id, rid, *extra):
        rd = S.runs_dir(cfg, campaign_id) / rid
        with open(rd / "stream.jsonl", "a", encoding="utf-8") as f:
            f.write(slow_line)
        time.sleep(max(CD.POLL_S * 3, 0.2))
        return real(cfg, campaign_id, rid, *extra)

    monkeypatch.setattr(AM, "load_run_pane_detail", slow)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(run_id)
            assert await pilot_until(pilot, lambda: "slow-detail-0476" in runs_detail(app))
            extra = json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "slow-append-once"}]}}) + "\n"
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(extra)
            assert await pilot_until(pilot, lambda: "slow-append-once" in runs_detail(app))
            assert runs_detail(app).count("slow-append-once") == 1

    _run(drive())


def test_stale_run_detail_dropped_on_switch(tmp_path, monkeypatch):
    """Pins the end state after run A's slow load finishes while run B is selected: stored
    run id, renderer, offset, head, body, and no repaint of A. A's worker is normally
    cancelled at the `to_thread` await, so A's late snapshot never reaches the token check or
    the selection check; this test does not show either guard is needed. That is
    `test_late_load_token_guard` and `test_late_load_selection_guard`."""
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    slow_id, other = "002-gate_triager", "020-test_reviewer"
    slow_marker = GATE_TITLE
    other_marker = MAJOR_TITLE
    real = AM.load_run_pane_detail
    paints = _spy_paints(monkeypatch)

    slow_done = threading.Event()
    slow_started = threading.Event()

    def slow(cfg, campaign_id, rid, *extra):
        if rid == slow_id:
            slow_started.set()
            time.sleep(max(CD.POLL_S * 3, 0.25))
            try:
                return real(cfg, campaign_id, rid, *extra)
            finally:
                slow_done.set()
        return real(cfg, campaign_id, rid, *extra)

    monkeypatch.setattr(AM, "load_run_pane_detail", slow)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(slow_id)
            assert await pilot_until(pilot, slow_started.is_set)
            s.select_run(other)
            assert await pilot_until(pilot, lambda: other_marker in runs_detail(app))
            assert await pilot_until(pilot, lambda: s._run_loaded_id == other)
            state = _run_state(s)
            n_paints = len(paints)
            assert await pilot_until(pilot, slow_done.is_set)
            await settle(app, pilot)
            assert other_marker in runs_detail(app)
            assert slow_marker not in runs_detail(app)
            # The widget's follow state still belongs to the selected run.
            assert s._run_loaded_id == other
            assert _run_state(s) == state
            assert s._run_renderer is state["renderer"]
            assert other_marker in s._run_head.plain and slow_marker not in s._run_head.plain
            assert slow_marker not in "".join(s._run_body)
            assert len(paints) == n_paints

    _run(drive())


def _spy_paints(monkeypatch):
    """Records every `_paint_runs_detail` call."""
    calls = []
    real = CD.CampaignDetail._paint_runs_detail

    def spy(self, scroll):
        calls.append(scroll)
        return real(self, scroll)

    monkeypatch.setattr(CD.CampaignDetail, "_paint_runs_detail", spy)
    return calls


def _run_state(s) -> dict:
    """The Runs pane's stored follow state (renderer by identity, the rest by value)."""
    return {"renderer": s._run_renderer, "offset": s._run_offset, "loaded": s._run_loaded_id,
            "head": s._run_head.plain, "body": list(s._run_body), "done": s._run_follow_done,
            "stale": s._run_detail_stale}


def _late_snapshots(monkeypatch):
    """While `mode["late"]` is set the loader returns the run's real snapshot with LATE-0564
    at the top of the head, so a late result that is wrongly accepted is visible."""
    mode = {"late": False}
    real = AM.load_run_pane_detail

    def load(cfg, campaign_id, rid, *extra):
        snap = real(cfg, campaign_id, rid, *extra)
        if mode["late"]:
            head = Text("LATE-0564\n")
            head.append_text(snap.head)
            snap.head = head
        return snap

    monkeypatch.setattr(AM, "load_run_pane_detail", load)
    return mode


def _stream_run_pair(cfg):
    runs = [r["run_id"] for r in Hist.campaign_history(cfg, CID_STREAM)["runs"]
            if r.get("has_stream")]
    assert len(runs) >= 2
    return runs[0], runs[1]


def test_late_load_token_guard(tmp_path, monkeypatch):
    """A superseded load for the still-selected run is dropped by the token check alone."""
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    a, _ = _stream_run_pair(cfg)
    mode = _late_snapshots(monkeypatch)
    paints = _spy_paints(monkeypatch)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, CID_STREAM, "delivered", timeline=False)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(a)
            assert await pilot_until(pilot, lambda: s._run_loaded_id == a)
            old = s._run_detail_token
            r_first = s._run_renderer
            n = len(paints)
            s._load_run_detail(a)
            assert s._run_detail_token == old + 1 and s._selected_run_id == a
            assert await pilot_until(
                pilot, lambda: len(paints) > n and s._run_renderer is not r_first)
            await settle(app, pilot)
            state, n = _run_state(s), len(paints)
            shown = runs_detail(app)
            mode["late"] = True
            await s._load_run_detail_async(a, old)
            await settle(app, pilot)
            assert len(paints) == n
            assert _run_state(s) == state and s._run_renderer is state["renderer"]
            assert runs_detail(app) == shown and "LATE-0564" not in shown
            assert "LATE-0564" not in s._run_head.plain

    _run(drive())


def test_late_load_selection_guard(tmp_path, monkeypatch):
    """A late load for run A whose token is current is dropped by the selection check alone:
    selecting B on the Timeline tab changes the selection without bumping the token."""
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    a, b = _stream_run_pair(cfg)
    mode = _late_snapshots(monkeypatch)
    paints = _spy_paints(monkeypatch)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, CID_STREAM, "delivered", timeline=False)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(a)
            assert await pilot_until(pilot, lambda: s._run_loaded_id == a)
            await press(app, pilot, ["2"])
            await settle(app, pilot)
            token, n = s._run_detail_token, len(paints)
            s._select_run_row(b)
            assert s._run_detail_token == token and s._selected_run_id == b
            assert s._run_loaded_id is None and s._run_renderer is None and s._run_offset == 0
            assert s._run_detail_stale is True
            before = _run_state(s)
            mode["late"] = True
            await s._load_run_detail_async(a, s._run_detail_token)
            await settle(app, pilot)
            assert s._selected_run_id == b and s._run_detail_token == token
            assert s._run_loaded_id is None and s._run_renderer is None
            assert s._run_offset == 0 and s._run_detail_stale is True
            assert _run_state(s) == before
            assert len(paints) == n and "LATE-0564" not in s._run_head.plain

    _run(drive())


def test_late_load_accepted_on_timeline_tab(tmp_path, monkeypatch):
    """Nothing checks the active tab: with token and selection current, a load that lands
    while the Timeline tab is showing is accepted and painted, only the scroll is skipped."""
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    a, _ = _stream_run_pair(cfg)
    mode = _late_snapshots(monkeypatch)
    paints = _spy_paints(monkeypatch)
    scrolls = []
    real_scroll = CD.CampaignDetail._scroll_runs_detail

    def scroll_spy(self, where):
        scrolls.append(where)
        return real_scroll(self, where)

    monkeypatch.setattr(CD.CampaignDetail, "_scroll_runs_detail", scroll_spy)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, CID_STREAM, "delivered", timeline=False)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(a)
            assert await pilot_until(pilot, lambda: s._run_loaded_id == a)
            await press(app, pilot, ["2"])
            await settle(app, pilot)
            n = len(paints)
            scrolls.clear()
            mode["late"] = True
            await s._load_run_detail_async(a, s._run_detail_token)
            await settle(app, pilot)
            assert s._run_loaded_id == a and s._run_detail_stale is False
            assert "LATE-0564" in s._run_head.plain
            assert len(paints) == n + 1
            assert scrolls == []

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


def test_stale_stream_tail_dropped_on_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    polls = _spy_polls(monkeypatch)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_STREAM
    hist = Hist.campaign_history(cfg, cid)
    stream_runs = [r["run_id"] for r in hist["runs"] if r.get("has_stream")]
    assert len(stream_runs) >= 2
    slow_id, other = stream_runs[0], stream_runs[1]
    other_marker = AM.load_run_pane_detail(cfg, cid, other).text.plain.splitlines()[0]
    real = AM.stream_tail
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-stale-tail"
    shutil.copytree(camp, dst)
    cid2 = dst.name
    st = json.loads((S.campaign_dir(cfg, cid2) / "state.json").read_text(encoding="utf-8"))
    st["state"] = "IMPLEMENTING"
    (S.campaign_dir(cfg, cid2) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    slow_dir = S.runs_dir(cfg, cid2) / slow_id
    (slow_dir / "exit.json").unlink(missing_ok=True)
    (slow_dir / "run.json").unlink(missing_ok=True)
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "stale-tail-0476"}]}}) + "\n"

    def slow_tail2(cfg, campaign_id, rid, offset, renderer):
        if rid == slow_id:
            time.sleep(max(CD.POLL_S * 3, 0.25))
            with open(slow_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(line)
        return real(cfg, campaign_id, rid, offset, renderer)

    monkeypatch.setattr(AM, "stream_tail", slow_tail2)
    monkeypatch.setattr(TL, "_ended", lambda rd: False)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(slow_id)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            s.select_run(other)
            assert await pilot_until(pilot, lambda: other_marker in runs_detail(app))
            await _after_ticks(pilot, polls, 8)
            assert other_marker in runs_detail(app)
            assert "stale-tail-0476" not in runs_detail(app)

    _run(drive())


def _copy_campaign(cfg, cid: str, suffix: str) -> str:
    camp = S.campaign_dir(cfg, cid)
    dst = camp.parent / f"{cid}-{suffix}"
    shutil.copytree(camp, dst)
    return dst.name


def _edit_run_json(cfg, cid, run_id, fn):
    path = S.runs_dir(cfg, cid) / run_id / "run.json"
    rj = json.loads(path.read_text(encoding="utf-8"))
    fn(rj)
    path.write_text(json.dumps(rj), encoding="utf-8")


def _live_copy(cfg, cid, suffix):
    """Copy of the stream campaign made non-terminal, its stream run un-ended on disk."""
    cid2 = _copy_campaign(cfg, cid, suffix)
    state_path = S.campaign_dir(cfg, cid2) / "state.json"
    st = json.loads(state_path.read_text(encoding="utf-8"))
    original = st["state"]
    st["state"] = "IMPLEMENTING"
    state_path.write_text(json.dumps(st), encoding="utf-8")
    run_id = next(r["run_id"] for r in Hist.campaign_history(cfg, cid2)["runs"]
                  if r.get("has_stream"))
    run_dir = S.runs_dir(cfg, cid2) / run_id
    (run_dir / "exit.json").unlink(missing_ok=True)
    (run_dir / "run.json").unlink(missing_ok=True)
    return cid2, run_id, run_dir, original


def test_verdict_line_lead_fallbacks(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _copy_campaign(cfg, CID_MULTI, "lead")
    run_id = "001-implementer"
    (S.runs_dir(cfg, cid) / run_id / "stderr.log").write_text("stderr-first-0564\n", encoding="utf-8")

    def only(verdict):
        def fn(rj):
            rj["verdict"] = verdict
        _edit_run_json(cfg, cid, run_id, fn)
        return AM.load_run_pane_detail(cfg, cid, run_id).head.plain.splitlines()[0]

    def hist_status():
        row = next(r for r in Hist.campaign_history(cfg, cid)["runs"] if r["run_id"] == run_id)
        return row["verdict_status"]

    assert only({"disposition": "disp-0476", "score": 3, "summary": "sum-a"}) == "disp-0476 3 sum-a"
    assert only({"verdict": "verd-0476", "summary": "sum-b"}) == "verd-0476 sum-b"
    # Lead fallbacks: a null key falls through to the next key; the table agrees with the pane.
    for verdict, lead in (
            ({"status": None, "disposition": "d", "verdict": "v"}, "d"),
            ({"status": None, "disposition": None, "verdict": "v"}, "v"),
            ({"status": "s", "disposition": "d"}, "s"),
            ({"disposition": "d", "verdict": "v"}, "d")):
        assert only(verdict) == lead
        assert hist_status() == lead
    # No lead key at all: the first line is the score (a null lead is not a lead).
    assert only({"score": 4}) == "4"
    assert hist_status() is None
    # No verdict line: the first line is the first blocking row, else the stderr first line.
    for verdict in ({}, "text", [1]):
        assert only(verdict) == "stderr-first-0564"
    assert only({"blockers": ["blk-0564"]}) == "major: blk-0564"
    (S.runs_dir(cfg, cid) / run_id / "stderr.log").unlink()
    assert only({}) == "no stderr"
    # The shipped fixtures: gate_triager leads with its disposition, the reviewer with its verdict.
    for rid in ("002-gate_triager", "020-test_reviewer"):
        v = json.loads((S.runs_dir(cfg, cid) / rid / "run.json").read_text(
            encoding="utf-8"))["verdict"]
        lead = next(v[k] for k in ("status", "disposition", "verdict") if v.get(k) is not None)
        first = AM.load_run_pane_detail(cfg, cid, rid).text.plain.splitlines()[0]
        assert first.startswith(str(lead))
        row = next(r for r in Hist.campaign_history(cfg, cid)["runs"] if r["run_id"] == rid)
        assert row["verdict_status"] == lead


def test_detail_first_line_falls_back_to_body(tmp_path):
    """Last step of the first-line chain: verdict line, blocking row, stderr line, body line.
    The body line leads only when stderr.log exists and is empty (a missing one adds `no stderr`)."""
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)

    def first(cid, rid):
        return AM.load_run_pane_detail(cfg, cid, rid)

    def set_verdict(cid, rid, verdict):
        def fn(rj):
            rj["verdict"] = verdict
        _edit_run_json(cfg, cid, rid, fn)

    # Stream run: the first body line is the first rendered stream line.
    cid, rid, run_dir = _stream_copy(cfg, "body0584")
    (run_dir / "stderr.log").write_text("", encoding="utf-8")
    set_verdict(cid, rid, {})
    (run_dir / "stream.jsonl").write_text(
        "".join(_tool_result_event(f"tool-out-{i:04d}") + "\n" for i in range(3)),
        encoding="utf-8")
    snap = first(cid, rid)
    assert snap.head.plain == ""
    assert snap.body and snap.body[0] == _tool_out(0)
    assert snap.text.plain.splitlines()[0] == _tool_out(0) == snap.body[0]

    # Earlier steps beat the body line, in order.
    set_verdict(cid, rid, {"blockers": ["blk-0584"]})
    assert first(cid, rid).text.plain.splitlines()[0] == "major: blk-0584"
    set_verdict(cid, rid, {})
    (run_dir / "stderr.log").write_text("stderr-line-0584\n", encoding="utf-8")
    assert first(cid, rid).text.plain.splitlines()[0] == "stderr-line-0584"
    (run_dir / "stderr.log").unlink()
    assert first(cid, rid).text.plain.splitlines()[0] == "no stderr"

    # Result-fallback run (no stream.jsonl): the first line of the pre-stream result.
    cid = _copy_campaign(cfg, CID_MULTI, "body0584")
    rid = "001-implementer"
    run_dir = S.runs_dir(cfg, cid) / rid
    assert not (run_dir / "stream.jsonl").exists()
    (run_dir / "stderr.log").write_text("", encoding="utf-8")
    set_verdict(cid, rid, {})
    snap = first(cid, rid)
    assert snap.head.plain == ""
    assert not snap.has_stream
    assert snap.body and snap.body[0].strip()
    assert snap.text.plain.splitlines()[0] == snap.body[0]


def test_verdict_line_composition(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _copy_campaign(cfg, CID_MULTI, "compose")
    run_id = "001-implementer"
    (S.runs_dir(cfg, cid) / run_id / "stderr.log").write_text("err-0564\n", encoding="utf-8")

    def head(verdict):
        def fn(rj):
            rj["verdict"] = verdict
        _edit_run_json(cfg, cid, run_id, fn)
        return AM.load_run_pane_detail(cfg, cid, run_id).head.plain

    for verdict, line in (
            ({"status": "s", "score": 7, "summary": "m"}, "s 7 m"),
            ({"status": "s", "score": 0, "summary": "m"}, "s 0 m"),
            ({"status": "s", "score": 7, "summary": ""}, "s 7"),
            ({"status": "s", "summary": "m"}, "s m"),
            ({"status": "s", "score": 7}, "s 7"),
            ({"status": "s"}, "s"),
            ({"score": 7, "summary": "m"}, "7 m"),
            ({"score": 0}, "0"),
            ({"summary": "m"}, "m")):
        assert head(verdict).splitlines()[0] == line, verdict
    # Rich markup stays literal; control characters are stripped (printable remainder kept).
    summary = "[b]x[/b] \x1b[31mred\x85end"
    shown = "s 7 [b]x[/b] [31mredend"
    plain = head({"status": "s", "score": 7, "summary": summary})
    assert plain.splitlines()[0] == shown
    assert "\x1b" not in plain and "\x85" not in plain
    # Line order: verdict line, blocking rows, stderr.
    plain = head({"status": "s", "score": 7, "summary": "m", "blockers": ["blk-0564"]})
    assert plain.splitlines() == ["s 7 m", "major: blk-0564", "err-0564"]

    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs"})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s.view is not None)
            head({"status": "s", "score": 7, "summary": summary})
            s.select_run(run_id)
            assert await pilot_until(pilot, lambda: shown in runs_detail(app).splitlines())
            assert "\x1b" not in runs_detail(app) and "\x85" not in runs_detail(app)
            assert runs_detail(app).splitlines()[0] == shown

    _run(drive())


def test_findings_must_fix_severity_and_excluded_keys(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _copy_campaign(cfg, CID_MULTI, "sev")
    run_id = "001-implementer"
    (S.runs_dir(cfg, cid) / run_id / "stderr.log").write_text("stderr-0564\n", encoding="utf-8")

    def fn(rj):
        rj["changed_files"] = ["cf-top-0476.py"]
        rj["tests_run"] = ["tr-top-0476"]
        rj["verdict"] = {
            "status": "fix",
            "changed_files": ["cf-verdict-0476.py"],
            "tests_run": ["tr-verdict-0476"],
            "findings": [
                {"severity": "must_fix", "title": "[b]sev-must-0476[/b]", "files": ["a.py", "b.py"]},
                {"severity": "major", "title": "sev-major-0564"},
                {"severity": "minor", "title": "MARK-minor-0564"},
                {"severity": "nit", "title": "MARK-nit-0564"},
                {"severity": "MUST_FIX", "title": "MARK-upper-0564"},
                {"severity": "blocker", "title": "MARK-blocker-0564"},
                "MARK-str-finding-0564",
            ],
            "must_fix": [
                {"title": "mf-obj-0564", "files": ["c.py"]},
                {"title": "mf-nofiles-0564"},
                "MARK-str-mustfix-0564",
            ],
            "blockers": ["blk-str-0564", 918273645],
        }

    _edit_run_json(cfg, cid, run_id, fn)
    head = AM.load_run_pane_detail(cfg, cid, run_id).head.plain
    assert head.splitlines() == [
        "fix",
        "must_fix: [b]sev-must-0476[/b]", "  files: a.py, b.py",
        "major: sev-major-0564",
        "must_fix: mf-obj-0564", "  files: c.py",
        "must_fix: mf-nofiles-0564",
        "major: blk-str-0564",
        "stderr-0564",
    ]
    text = AM.load_run_pane_detail(cfg, cid, run_id).text.plain
    for absent in ("cf-top-0476", "tr-top-0476", "cf-verdict-0476", "tr-verdict-0476",
                   "MARK-minor-0564", "MARK-nit-0564", "MARK-upper-0564", "MARK-blocker-0564",
                   "MARK-str-finding-0564", "MARK-str-mustfix-0564", "918273645"):
        assert absent not in text, absent
    # The same rows reach the widget text of the open popup.
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs"})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s.view is not None)
            s.select_run(run_id)
            assert await pilot_until(
                pilot, lambda: "must_fix: [b]sev-must-0476[/b]" in runs_detail(app).splitlines())
            lines = runs_detail(app).splitlines()
            assert lines[:3] == ["fix", "must_fix: [b]sev-must-0476[/b]", "  files: a.py, b.py"]
            assert "must_fix: mf-obj-0564" in lines and "MARK-minor-0564" not in runs_detail(app)

    _run(drive())


def test_malformed_shapes_and_control_chars(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _copy_campaign(cfg, CID_MULTI, "shape")
    run_id = "001-implementer"
    rdir = S.runs_dir(cfg, cid) / run_id
    for verdict, files in (
            ({"must_fix": 3, "blockers": 2, "findings": "x"}, "x"),
            ({"must_fix": "fix it", "blockers": True, "findings": 7}, ["x"]),
            ({"findings": [None, 5, {"severity": "major", "files": "nope"}]}, {"raw": 4})):
        def fn(rj, verdict=verdict, files=files):
            rj["verdict"] = verdict
            rj["files"] = files
        _edit_run_json(cfg, cid, run_id, fn)
        text = AM.load_run_pane_detail(cfg, cid, run_id).text.plain
        assert "must_fix: f" not in text and "fix it" not in text
    (rdir / "outside.json").write_text('{"result": "abs-raw-0476"}', encoding="utf-8")
    _edit_run_json(cfg, cid, run_id, lambda rj: rj.update(
        files={"raw": str(rdir / "outside.json")}, verdict={}))
    assert "abs-raw-0476" not in AM.load_run_pane_detail(cfg, cid, run_id).text.plain
    # Terminal control characters from files never reach the pane text.
    (rdir / "stderr.log").write_text("err\x1b[2Jline\x07\n", encoding="utf-8")
    _edit_run_json(cfg, cid, run_id, lambda rj: rj.update(
        verdict={"status": "ok\x1b]0;x\x07", "blockers": ["bl\x1bk"]}))
    plain = AM.load_run_pane_detail(cfg, cid, run_id).text.plain
    assert "\x1b" not in plain and "\x07" not in plain
    assert "err[2Jline" in plain and "major: blk" in plain


def test_error_lines_in_open_popup(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _copy_campaign(cfg, CID_MULTI, "err-ui")
    runs = S.runs_dir(cfg, cid)
    rows = [r["run_id"] for r in Hist.campaign_history(cfg, cid)["runs"]]
    gone, bad, no_err = "001-implementer", "020-test_reviewer", "002-gate_triager"
    assert {gone, bad, no_err} <= set(rows)
    shutil.rmtree(runs / gone)
    (runs / bad / "run.json").write_text("{", encoding="utf-8")
    (runs / no_err / "stderr.log").unlink(missing_ok=True)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs"})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s.view is not None)
            for rid, line in ((gone, "no run directory"), (bad, "run.json malformed"),
                              (no_err, "no stderr")):
                s.select_run(rid)
                assert await pilot_until(pilot, lambda: line in runs_detail(app))
                assert detail_screen(app) is s
                assert str(runs_table(app).ordered_rows[runs_table(app).cursor_row].key.value) == rid

    _run(drive())


def test_verdict_visible_after_select_with_long_stream(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = _copy_campaign(cfg, CID_STREAM, "long")
    run_id = next(r["run_id"] for r in Hist.campaign_history(cfg, cid)["runs"]
                  if r.get("has_stream"))
    path = S.runs_dir(cfg, cid) / run_id / "stream.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for i in range(400):
            f.write(_asst(f"long-line-{i:04d}") + "\n")
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid, focus={"tab": "runs"})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s.view is not None)
            s.select_run(run_id)
            assert await pilot_until(pilot, lambda: "long-line-0399" in runs_detail(app))
            pane = s.query_one("#runs-detail")
            assert await pilot_until(pilot, lambda: pane.max_scroll_y > 0)
            assert await pilot_until(pilot, lambda: pane.scroll_y == 0)

    _run(drive())


def test_aba_selection_drops_stale_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid2, a, run_dir, _ = _live_copy(cfg, CID_STREAM, "aba")
    others = [r["run_id"] for r in Hist.campaign_history(cfg, cid2)["runs"] if r["run_id"] != a]
    b = others[0]
    monkeypatch.setattr(TL, "_ended", lambda rd: False)
    gate = threading.Event()
    in_tail = threading.Event()
    real = AM.stream_tail

    def blocked(*args, **kw):
        if gate.is_set():
            return real(*args, **kw)
        lines, _ = real(*args, **kw)
        in_tail.set()
        gate.wait(5)
        return lines, 987654

    monkeypatch.setattr(AM, "stream_tail", blocked)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": a})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            r_old = s._run_renderer
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(_asst("aba-line-0476") + "\n")
            assert await pilot_until(pilot, lambda: in_tail.is_set())
            s.select_run(b)
            s.select_run(a)
            assert await pilot_until(
                pilot, lambda: s._run_renderer is not None and s._run_renderer is not r_old)
            r_new = s._run_renderer
            gate.set()
            await _after_ticks(pilot, _spy_polls(monkeypatch), 3)
            assert s._run_renderer is r_new
            assert s._run_offset != 987654
            assert runs_detail(app).count("aba-line-0476") <= 1

    _run(drive())


def test_ended_checks_run_off_main_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid2, run_id, run_dir, _ = _live_copy(cfg, CID_STREAM, "thread")
    seen = []

    def spy(rd):
        seen.append(threading.current_thread())
        return False

    monkeypatch.setattr(TL, "_ended", spy)
    polls = _spy_polls(monkeypatch)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            await _after_ticks(pilot, polls)
            assert seen and all(t is not threading.main_thread() for t in seen)

    _run(drive())


def test_terminal_reload_drains_followed_run_and_refreshes_head(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid2, run_id, run_dir, original = _live_copy(cfg, CID_STREAM, "final")
    monkeypatch.setattr(TL, "_ended", lambda rd: False)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            assert not s._run_follow_done
            with open(run_dir / "stream.jsonl", "ab") as f:
                f.write(_asst("final-0476").encode())
            (run_dir / "run.json").write_text(
                json.dumps({"verdict": {"status": "head-refresh-0476"}}), encoding="utf-8")
            state_path = S.campaign_dir(cfg, cid2) / "state.json"
            st = json.loads(state_path.read_text(encoding="utf-8"))
            st["state"] = original
            state_path.write_text(json.dumps(st), encoding="utf-8")
            assert await pilot_until(pilot, lambda: s.view is not None and s.view.terminal)
            assert await pilot_until(pilot, lambda: "final-0476" in runs_detail(app)), (
                runs_detail(app), s._run_follow_done, s._run_offset, s._run_loaded_id,
                s._active_tab(), s._run_renderer)
            assert s._run_follow_done
            assert runs_detail(app).startswith("head-refresh-0476")

    _run(drive())


def _frag_stream(run_dir: Path, text: str) -> None:
    """Two complete lines then an object with no trailing newline."""
    frag = json.dumps({"type": "result", "subtype": "success", "result": text})
    (run_dir / "stream.jsonl").write_text(
        _asst("complete-a-0476") + "\n" + _asst("complete-b-0476") + "\n" + frag, encoding="utf-8")


def test_ended_stream_no_trailing_newline_opens_popup(tmp_path, monkeypatch):
    """EOF on a finished run is the end: the partial last line is flushed, the worker returns."""
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid2 = _copy_campaign(cfg, CID_STREAM, "ended-frag")
    run_id = next(r["run_id"] for r in Hist.campaign_history(cfg, cid2)["runs"]
                  if r.get("has_stream"))
    run_dir = S.runs_dir(cfg, cid2) / run_id
    _frag_stream(run_dir, "ended-frag-0476")
    assert (run_dir / "run.json").is_file()
    snap = AM.load_run_pane_detail(cfg, cid2, run_id)
    assert snap.ended and snap.offset == (run_dir / "stream.jsonl").stat().st_size
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: "■ result:" in runs_detail(app)), (
                runs_detail(app))
            assert "complete-b-0476" in runs_detail(app)
            assert s._run_loaded_id == run_id and s._run_follow_done
            assert not s.workers or all(
                w.is_finished for w in s.workers if w.name == "campaign-run-detail")

    _run(drive())


def test_terminal_campaign_unended_run_is_settled(tmp_path, monkeypatch):
    """A terminal campaign whose run never wrote exit.json/run.json is settled on open:
    the fragment is flushed, follow latches done, and nothing waits for a tick that never
    comes."""
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid2 = _copy_campaign(cfg, CID_STREAM, "term-unended")
    run_id = next(r["run_id"] for r in Hist.campaign_history(cfg, cid2)["runs"]
                  if r.get("has_stream"))
    run_dir = S.runs_dir(cfg, cid2) / run_id
    (run_dir / "exit.json").unlink(missing_ok=True)
    (run_dir / "run.json").unlink(missing_ok=True)
    _frag_stream(run_dir, "term-frag-0476")
    monkeypatch.setattr(TL, "_ended", lambda rd: False)
    # Un-settled the fragment stays unread (the run looks live)...
    live = AM.load_run_pane_detail(cfg, cid2, run_id)
    assert "■ result:" not in live.text.plain and not live.ended
    # ...settled it is drained and the offset is at EOF.
    done = AM.load_run_pane_detail(cfg, cid2, run_id, True)
    assert "■ result:" in done.text.plain
    assert done.offset == (run_dir / "stream.jsonl").stat().st_size
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s.view is not None and s.view.terminal)
            assert await pilot_until(pilot, lambda: "■ result:" in runs_detail(app)), (
                runs_detail(app))
            assert s._run_follow_done and s._run_head_done
            before = s._run_offset
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(_asst("term-late-0476") + "\n")
            # A terminal campaign has no follow timer: nothing to wait on, only to settle.
            await pilot.pause(0.3)
            assert "term-late-0476" not in runs_detail(app)
            assert s._run_offset == before

    _run(drive())


def runs_detail_content(app):
    return detail_screen(app).query_one("#runs-detail-text").content


def test_widget_paints_rich_text_not_markup(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    mark = "[bold]tagged-widget[/bold]"
    (S.runs_dir(cfg, cid) / "002-gate_triager" / "stderr.log").write_text(
        "x\n" + mark, encoding="utf-8")
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run("002-gate_triager")
            assert await pilot_until(pilot, lambda: mark in runs_detail(app))
            content = runs_detail_content(app)
            assert isinstance(content, Text)
            assert mark in content.plain

    _run(drive())


def test_runs_tab_has_no_placeholder_widget(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = CID_MULTI
    run_id = Hist.campaign_history(cfg, cid)["runs"][0]["run_id"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            s = detail_screen(app)
            await press(app, pilot, ["3"])
            s.select_run(run_id)
            assert await pilot_until(pilot, lambda: runs_detail(app).strip())
            pane = s.query_one("#tab-runs")
            for w in pane.query("Static"):
                text = str(w.content)
                assert "placeholder: runs" not in text
            assert not list(pane.query("#placeholder-text"))
            assert not list(pane.query("#placeholder-runs"))
            assert not list(s.query("#placeholder-text"))
            assert "placeholder: runs" not in str(s.query_one("#overview-text").content)

    _run(drive())


def _pre_stream_copy(cfg, tag):
    cid2 = _copy_campaign(cfg, CID_MULTI, tag)
    run_id = "001-implementer"
    run_dir = S.runs_dir(cfg, cid2) / run_id
    (run_dir / "stream.jsonl").unlink(missing_ok=True)
    rj = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    (rj.get("files") or {}).pop("raw", None)
    (run_dir / "run.json").write_text(json.dumps(rj), encoding="utf-8")
    return cid2, run_id, run_dir


def test_pre_stream_result_fallback_branches(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid, run_id, run_dir = _pre_stream_copy(cfg, "fallback")
    result = run_dir / "result.json"

    # Object without a usable `result` string: the file's last 200 lines, not the whole file.
    obj = {f"key{i:03d}": i for i in range(250)}
    obj["structured_output"] = {"ok": True}
    result.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    file_lines = result.read_text(encoding="utf-8").splitlines()
    assert len(file_lines) > 250
    plain = AM.load_run_pane_detail(cfg, cid, run_id).text.plain
    assert file_lines[-200] in plain.splitlines()
    assert file_lines[-201] not in plain.splitlines()
    assert "key000" not in plain
    assert file_lines[-1] in plain.splitlines()

    # `result` string beside an object `structured_output`: still the file's lines.
    result.write_text(json.dumps({"result": "str-result-0476", "structured_output": {"a": 1}},
                                 indent=1), encoding="utf-8")
    plain = AM.load_run_pane_detail(cfg, cid, run_id).text.plain
    assert '"result": "str-result-0476"' in plain

    # Not JSON: the file's last 200 lines.
    result.write_text("\n".join(f"raw{i}" for i in range(300)), encoding="utf-8")
    plain = AM.load_run_pane_detail(cfg, cid, run_id).text.plain
    assert "raw99" not in plain.splitlines()
    assert "raw100" in plain.splitlines()
    assert "raw299" in plain.splitlines()

    # Empty file: the missing-result line.
    result.write_text("", encoding="utf-8")
    assert "no result.json" in AM.load_run_pane_detail(cfg, cid, run_id).text.plain
    result.write_text("  \n", encoding="utf-8")
    assert "no result.json" in AM.load_run_pane_detail(cfg, cid, run_id).text.plain


def test_follow_reads_run_off_main_thread(tmp_path, monkeypatch):
    cfg, cid2, run_id, run_dir, ended = _live_run_copy(tmp_path, monkeypatch, "threads", stream=True)
    main = threading.main_thread()
    seen: dict[str, list] = {"stream_tail": [], "stream_tail_drain": [], "load_run_head": []}
    for name in seen:
        real = getattr(AM, name)

        def wrap(*a, _real=real, _name=name, **k):
            seen[_name].append(threading.current_thread())
            return _real(*a, **k)

        monkeypatch.setattr(AM, name, wrap)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(_asst("thread-append-0476") + "\n")
            assert await pilot_until(pilot, lambda: "thread-append-0476" in runs_detail(app))
            ended["v"] = True
            assert await pilot_until(pilot, lambda: s._run_follow_done)
            assert await pilot_until(pilot, lambda: seen["load_run_head"])
            for name, threads in seen.items():
                assert threads, name
                assert all(t is not main for t in threads), name

    _run(drive())


def test_ended_run_without_run_json_does_not_repaint(tmp_path, monkeypatch):
    cfg, cid2, run_id, run_dir, ended = _live_run_copy(tmp_path, monkeypatch, "norj", stream=True)
    ended["v"] = True  # exit.json present, run.json never written (a crashed run)
    heads = []
    real_head = AM.load_run_head

    def head_spy(*a, **k):
        heads.append(1)
        return real_head(*a, **k)

    monkeypatch.setattr(AM, "load_run_head", head_spy)
    paints = []
    real_paint = CD.CampaignDetail._paint_runs_detail

    def paint_spy(self, scroll):
        paints.append(1)
        return real_paint(self, scroll)

    monkeypatch.setattr(CD.CampaignDetail, "_paint_runs_detail", paint_spy)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_follow_done)
            # The head is re-read every tick (run.json may still land) ...
            assert await pilot_until(pilot, lambda: len(heads) >= 2)
            await settle(app, pilot)
            n_paints = len(paints)
            n_heads = len(heads)
            assert not s._run_head_done
            assert await pilot_until(pilot, lambda: len(heads) >= n_heads + 3)
            # ... but an unchanged head is not repainted.
            assert len(paints) == n_paints

    _run(drive())


def test_follow_state_failure_does_not_kill_popup(tmp_path, monkeypatch):
    polls = _spy_polls(monkeypatch)
    cfg, cid2, run_id, run_dir, ended = _live_run_copy(tmp_path, monkeypatch, "fsfail", stream=True)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise PermissionError("run dir unreadable")

    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            monkeypatch.setattr(AM, "run_follow_state", boom)  # after the initial paint
            assert await pilot_until(pilot, lambda: len(calls) >= 2)
            await _after_ticks(pilot, polls)
            assert "seed-head" in runs_detail(app)
            assert detail_screen(app) is s

    _run(drive())


def test_follow_resumes_after_returning_to_runs_tab(tmp_path, monkeypatch):
    polls = _spy_polls(monkeypatch)
    cfg, cid2, run_id, run_dir, ended = _live_run_copy(tmp_path, monkeypatch, "resume", stream=True)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            await press(app, pilot, ["2"])
            await settle(app, pilot)
            off0 = s._run_offset
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(_asst("hidden-append-0476") + "\n")
            await _after_ticks(pilot, polls)
            assert s._run_offset == off0
            assert "hidden-append-0476" not in runs_detail(app)
            await press(app, pilot, ["3"])
            assert await pilot_until(pilot, lambda: "hidden-append-0476" in runs_detail(app))
            assert not s._run_follow_done
            # Follow is armed again: a later append shows up on a poll.
            with open(run_dir / "stream.jsonl", "a", encoding="utf-8") as f:
                f.write(_asst("after-resume-0476") + "\n")
            assert await pilot_until(pilot, lambda: "after-resume-0476" in runs_detail(app))
            assert s._run_offset == (run_dir / "stream.jsonl").stat().st_size

    _run(drive())


def test_runs_pane_no_writes_live_follow_and_drain(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    polls = _spy_polls(monkeypatch)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid2, run_id, run_dir, _ = _live_copy(cfg, CID_STREAM, "nowrite")
    ended = {"v": False}
    monkeypatch.setattr(TL, "_ended", lambda rd: ended["v"])
    frag = json.dumps({"type": "result", "result": "frag-0476"})
    with open(run_dir / "stream.jsonl", "ab") as f:
        f.write(b"\n" + _asst("nw-extra").encode() + b"\n" + frag.encode())
    root = S.campaign_dir(cfg, cid2)
    before = _hashes(root)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            await _after_ticks(pilot, polls)
            await press(app, pilot, ["j", "k", "down", "up"])
            n_results = runs_detail(app).count("■ result:")
            assert "nw-extra" in runs_detail(app)
            ended["v"] = True
            assert await pilot_until(pilot, lambda: s._run_follow_done)
            # The trailing no-newline fragment is drained once the run ends.
            assert await pilot_until(
                pilot, lambda: runs_detail(app).count("■ result:") > n_results)
            await _after_ticks(pilot, polls)

    _run(drive())
    # cache_dir is covered by test_runs_pane_no_writes: a live campaign's history index is
    # refreshed by the history layer itself, not by this pane.
    assert _hashes(root) == before


def test_runs_pane_no_writes_terminal_reload_drain(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.05)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid2, run_id, run_dir, original = _live_copy(cfg, CID_STREAM, "nowrite-final")
    monkeypatch.setattr(TL, "_ended", lambda rd: False)
    root = S.campaign_dir(cfg, cid2)
    app = _desk(cfg, tab="queue")
    box = {}

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(cid2, focus={"tab": "runs", "run": run_id})
            s = detail_screen(app)
            assert await pilot_until(pilot, lambda: s._run_renderer is not None)
            with open(run_dir / "stream.jsonl", "ab") as f:
                f.write(_asst("nw-final-0476").encode())
            state_path = root / "state.json"
            st = json.loads(state_path.read_text(encoding="utf-8"))
            st["state"] = original
            state_path.write_text(json.dumps(st), encoding="utf-8")
            # Hash straight after the test's own writes, before any await.
            box["before"] = _hashes(root)
            assert await pilot_until(pilot, lambda: s.view is not None and s.view.terminal)
            assert await pilot_until(pilot, lambda: "nw-final-0476" in runs_detail(app))
            assert s._run_follow_done

    _run(drive())
    # cache_dir is not asserted here: the state change itself makes the history layer re-index.
    assert _hashes(root) == box["before"]
