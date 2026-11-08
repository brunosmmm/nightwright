"""SPEC-0407: the TUI campaign detail popup (Textual pilot tests)."""
import importlib

import pytest
from rich.text import Text

from test_agentic_dashboard import _ev, _fixture
from test_agentic_e2e import run_cli
from test_tui_queue_tab import SIZE, _desk, _run, press, select, settle, view
from nightwright import tui as T
from nightwright.agentic import events as EV
from nightwright.agentic import history as Hist
from nightwright.agentic import store as S
from nightwright.tui import agentic_model as AM
from nightwright.tui import campaign_detail as CD
from nightwright.tui.app import IdeaDesk
from waits import pilot_until


def text_of(widget) -> str:
    return str(widget.content)


def detail_screen(app) -> CD.CampaignDetail:
    assert isinstance(app.screen, CD.CampaignDetail), app.screen
    return app.screen


def status(app) -> str:
    return text_of(detail_screen(app).query_one("#campaign-status"))


def detail(app) -> str:
    return text_of(detail_screen(app).query_one("#campaign-detail-text"))


def overview(app) -> str:
    return text_of(detail_screen(app).query_one("#overview-text"))


def seed(cfg, cid):
    """Ledger rows with run, gate, finding, and note."""
    _ev(cfg, cid, "transition", {"from": "QUEUED", "to": "IMPLEMENTING", "reason": "start"})
    _ev(cfg, cid, "run.start", {"persona": "implementer", "run_id": "001-implementer"})
    _ev(cfg, cid, "gate", {"gate_seq": 1, "ok": False, "results": [
        {"name": "pytest", "ok": False, "required": True, "exit_code": 1, "duration_s": 1.0,
         "log": "gates/missing.log"}]})
    _ev(cfg, cid, "finding", {"persona": "reviewer", "severity": "major", "title": "Broken",
                              "detail": "d" * 100})
    _ev(cfg, cid, "note", {"text": "hello there"})


def _timeline_total(cfg, cid) -> int:
    return len(Hist.campaign_history(cfg, cid).get("timeline") or [])


async def open_screen(app, pilot, cid, section, *, timeline=True):
    """Enter opens the modal; press `2` for Timeline when tests need the event table."""
    await settle(app, pilot)
    select(app, section, cid)
    await press(app, pilot, ["enter"])
    await settle(app, pilot)
    assert isinstance(app.screen, CD.CampaignDetail)
    if timeline:
        await press(app, pilot, ["2"])
        await settle(app, pilot)


def test_enter_opens_the_screen_and_escape_returns_with_the_cursor(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued", timeline=False)
            s = detail_screen(app)
            assert s.campaign_id == cid
            assert overview(app).startswith(cid)
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            assert not isinstance(app.screen, CD.CampaignDetail)
            assert view(app).current_row().campaign == cid
            await open_screen(app, pilot, cid, "queued", timeline=False)
            await press(app, pilot, ["q"])
            await settle(app, pilot)
            assert not isinstance(app.screen, CD.CampaignDetail) and app.is_running

    _run(drive())


def test_cursor_starts_on_the_last_event_and_filters_cycle(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    total = _timeline_total(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            s = detail_screen(app)
            events = s._event_rows()
            data = s._data_indexes()
            assert len(events) == total and s.table.cursor_row == data[-1]
            loops = (s.view.history.get("header") or {}).get("loops", 0)
            loop_n = s.shown[s.table.cursor_row].get("loop", 0)
            assert status(app) == f"filter all · {total}/{total} events · loop {loop_n}/{loops}"
            assert "hello there" in detail(app)
            seen = []
            for _ in range(6):
                await press(app, pilot, ["f"])
                seen.append((s.filter, len(s._event_rows()), {AM.timeline_group(e) for e in s._event_rows()}))
                loops = (s.view.history.get("header") or {}).get("loops", 0)
                loop_n = 0
                i = s.table.cursor_row
                if 0 <= i < len(s.shown) and not s.shown[i].get("separator"):
                    loop_n = s.shown[i].get("loop", 0)
                assert status(app) == (
                    f"filter {s.filter} · {len(s._event_rows())}/{total} events · loop {loop_n}/{loops}")
            assert [x[0] for x in seen] == [
                "transitions", "runs", "gates", "findings", "notes", "decisions"]
            assert all(g == {f} for f, n, g in seen if n)
            assert sum(n for _, n, _ in seen) == total
            await press(app, pilot, ["f"])
            assert s.filter == "all" and len(s._event_rows()) == total
            await press(app, pilot, ["g"])
            assert s.table.cursor_row == s._data_indexes()[0]
            await press(app, pilot, ["G"])
            assert s.table.cursor_row == s._data_indexes()[-1]

    _run(drive())


def test_review_survives_a_slow_entry_detail(tmp_path, monkeypatch):
    import threading

    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    in_detail = threading.Event()
    release = threading.Event()
    real = AM.entry_detail

    def slow(*a, **k):
        in_detail.set()
        release.wait(timeout=5)
        return real(*a, **k)

    monkeypatch.setattr(AM, "entry_detail", slow)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            assert await pilot_until(pilot, lambda: in_detail.is_set())
            await press(app, pilot, ["v"])
            assert "Code review" in detail(app)
            release.set()
            await settle(app, pilot)
            s = detail_screen(app)
            assert s.review_mode and "Code review" in detail(app)
            await press(app, pilot, ["v"])
            await settle(app, pilot)
            assert not s.review_mode
            assert await pilot_until(pilot, lambda: "hello there" in detail(app))

    _run(drive())


def test_detail_pane_follows_the_cursor_and_v_toggles_the_review(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            s = detail_screen(app)
            await press(app, pilot, ["f", "f", "f"])
            assert "pytest: FAIL" in detail(app) and "(missing:" in detail(app)
            await press(app, pilot, ["f"])
            assert "d" * 100 in detail(app)
            await press(app, pilot, ["f", "f", "f", "f", "f"])   # notes→decisions→all→transitions→runs
            assert "persona: implementer" in detail(app)
            await press(app, pilot, ["v"])
            assert s.review_mode and "Code review" in detail(app)
            assert "no code review yet" in detail(app)
            await press(app, pilot, ["j", "k"])
            assert "Spec review" in detail(app)
            await press(app, pilot, ["v"])
            assert not s.review_mode and "persona: implementer" in detail(app)

    _run(drive())


def test_a_ledger_append_shows_within_a_poll_and_never_syncs(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)

    def boom(*a, **k):
        raise AssertionError("the campaign detail popup must stay read-only")
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            for name in ("sync", "cursor_set", "reader_lock"):
                monkeypatch.setattr(EV, name, boom, raising=False)
            s = detail_screen(app)
            n = len(s._event_rows())
            await press(app, pilot, ["f", "f", "f"])
            gate_total = len(s._event_rows())
            _ev(cfg, cid, "gate", {"gate_seq": 2, "ok": True, "results": []})
            assert await pilot_until(pilot, lambda: len(s._event_rows()) == gate_total + 1)
            await settle(app, pilot)
            assert s.filter == "gates" and len(s._event_rows()) == gate_total + 1
            assert s.table.cursor_row == s._data_indexes()[-1]
            await press(app, pilot, ["g"])
            first_seq = s._event_rows()[0].get("seq")
            _ev(cfg, cid, "gate", {"gate_seq": 3, "ok": True, "results": []})
            assert await pilot_until(pilot, lambda: len(s._event_rows()) == gate_total + 2)
            await settle(app, pilot)
            assert len(s._event_rows()) == gate_total + 2
            assert s.shown[s.table.cursor_row].get("seq") == first_seq
            assert f"/{n + 2} events" in status(app)

    _run(drive())


def test_decisions_append_triggers_poll_reload(tmp_path, monkeypatch):
    import json
    from nightwright.agentic import store as St

    monkeypatch.setattr(CD, "POLL_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    loads = []
    real = AM.load_detail

    def track(*a, **k):
        loads.append(1)
        return real(*a, **k)

    monkeypatch.setattr(AM, "load_detail", track)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            await settle(app, pilot)
            n = len(loads)
            path = St.agentic_dir(cfg) / "conductor" / "decisions.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {"event_id": f"poll-test-{n}", "text": "signature bump", "ts": "2026-11-07T00:00:00+00:00"}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            assert await pilot_until(pilot, lambda: len(loads) > n)

    _run(drive())


def test_poll_restore_cursor_by_ref_when_seq_is_null(tmp_path, monkeypatch):
    import tui_harness as TH
    from nightwright.agentic import store as St
    from test_tui_campaign_detail_ac import _fixture_timeline_record, open_via_queue

    monkeypatch.setattr(CD, "POLL_S", 0.1)
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid, row = _fixture_timeline_record(cfg)
    ref = row["ref"]
    state = St.read_state(cfg, cid)
    St.write_state(cfg, cid, {**state, "state": "IMPLEMENTING"})
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_via_queue(app, pilot, cid)
            s = detail_screen(app)
            idx = next(
                i for i, shown in enumerate(s.shown)
                if not shown.get("separator") and shown.get("ref") == ref)
            s.table.move_cursor(row=idx)
            await settle(app, pilot)
            assert s.shown[s.table.cursor_row].get("ref") == ref
            before_events = len(s._event_rows())
            _ev(cfg, cid, "note", {"text": "poll ref hold"})
            assert await pilot_until(
                pilot, lambda: len(s._event_rows()) > before_events)
            await settle(app, pilot)
            assert s.shown[s.table.cursor_row].get("ref") == ref

    _run(drive())


def test_timeline_summaries_match_history(tmp_path):
    import tui_harness as TH
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cid = "SPEC-0336-20261031-6558"
    hist = Hist.campaign_history(cfg, cid)
    summaries = [str(r.get("summary") or "") for r in hist.get("timeline") or []]
    from nightwright.tui.app import IdeaDesk
    from test_tui_campaign_detail_ac import open_via_queue, SIZE_160, detail_screen
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE_160) as pilot:
            await open_via_queue(app, pilot, cid)
            await press(app, pilot, ["2"])
            await settle(app, pilot)
            s = detail_screen(app)
            shown = [str(r.get("summary") or "") for r in s._event_rows()]
            assert len(shown) == len(summaries)
            for got, want in zip(shown, summaries):
                if want.strip():
                    assert got == want or want.startswith(got) or got in want

    _run(drive())


def test_resume_and_prioritize_update_header(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, ids["parked"], _section_of(app, ids["parked"]),
                             timeline=False)
            await press(app, pilot, ["u", "enter", "enter"])
            await settle(app, pilot)
            assert "IMPLEMENTING" in overview(app)
            await press(app, pilot, ["escape"])
            await open_screen(app, pilot, ids["ready"], "queued", timeline=False)
            await press(app, pilot, ["P", "enter", "enter"])
            await settle(app, pilot)
            assert S.read_state(cfg, ids["ready"]).get("priority") == 1

    _run(drive())


def test_park_runs_end_to_end_through_the_screen(tmp_path):
    import hashlib
    from pathlib import Path

    def _hashes(root):
        root = Path(root)
        out = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    data = Path(cfg["data_dir"])
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            s = detail_screen(app)
            await press(app, pilot, ["P", "escape"])
            await press(app, pilot, ["p", *"hold", "enter", "enter"])
            await settle(app, pilot)
            assert S.read_state(cfg, cid)["state"] == "PARKED"
            assert isinstance(app.screen, CD.CampaignDetail)
            assert "PARKED" in overview(app)
            assert "parked" in overview(app)
            assert s.view.actions == ("resume", "prioritize", "abandon")
            before = _hashes(data)
            await press(app, pilot, ["p"])
            await settle(app, pilot)
            assert any("park not available in" in m for m, _ in app.notes)
            assert _hashes(data) == before
            await press(app, pilot, ["A", "enter"])
            assert await pilot_until(
                pilot, lambda: S.read_state(cfg, cid)["state"] == "ABANDONED")
            await settle(app, pilot)
            assert isinstance(app.screen, CD.CampaignDetail)
            assert app.screen is s
            assert s._poll_timer is None
            assert s.view.actions == ()

    _run(drive())


def test_idea_keys_are_inert_on_the_screen(tmp_path):
    from nightwright.tui.app import TextPrompt
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            for key in ("x", "i", "X", "l", "c", "t", "m", "s", "a"):
                await press(app, pilot, [key])
                assert isinstance(app.screen, CD.CampaignDetail)
            await press(app, pilot, ["p"])
            await pilot.pause(0.3)
            assert isinstance(app.screen, TextPrompt)
            await press(app, pilot, ["x"])
            await pilot.pause(0.1)
            assert isinstance(app.screen, TextPrompt)
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            assert isinstance(app.screen, CD.CampaignDetail)

    _run(drive())


def test_campaign_option_opens_the_screen_directly(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, campaign=cid)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert app.query_one("#tabs").active == "tab-queue"
            assert detail_screen(app).campaign_id == cid
            await press(app, pilot, ["escape"])
            assert not isinstance(app.screen, CD.CampaignDetail)

    _run(drive())


def test_cli_campaign_resolves_ids_and_rejects_bad_ones(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    calls = []
    monkeypatch.setattr(T, "run", lambda cfg, **kw: calls.append(kw))
    ok = run_cli(cfg, monkeypatch, ["tui", "--campaign", ids["ready"]])
    assert ok.exit_code == 0 and calls[-1]["campaign"] == ids["ready"]
    pre = run_cli(cfg, monkeypatch, ["tui", "--campaign", "SPEC-0002-"])
    assert pre.exit_code == 0 and calls[-1]["campaign"] == ids["blocked"]
    n = len(calls)
    bad = run_cli(cfg, monkeypatch, ["tui", "--campaign", "SPEC-9999"])
    assert bad.exit_code != 0 and "unknown campaign" in bad.output
    amb = run_cli(cfg, monkeypatch, ["tui", "--campaign", "SPEC-0001-"])
    assert amb.exit_code != 0 and "ambiguous" in amb.output
    assert ids["ready"] in amb.output and ids["impl"] in amb.output
    assert len(calls) == n
    plain = run_cli(cfg, monkeypatch, ["tui"])
    assert plain.exit_code == 0 and calls[-1]["campaign"] is None


def _section_of(app, cid):
    return next(r.group for r in view(app).rows if r.campaign == cid)


def test_reload_error_keeps_the_app_alive(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            monkeypatch.setattr(CD.CampaignDetail, "paint",
                                lambda self, previous=None: (_ for _ in ()).throw(TypeError("x")))
            detail_screen(app)._safe_paint()
            await settle(app, pilot)
            assert app.is_running and "campaign failed" in status(app)
            monkeypatch.undo()
            detail_screen(app)._safe_paint()
            await settle(app, pilot)
            assert "campaign failed" not in status(app)

    _run(drive())


def test_a_failed_paint_is_retried_by_the_next_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            monkeypatch.setattr(CD.CampaignDetail, "paint",
                                lambda self, previous=None: (_ for _ in ()).throw(TypeError("x")))
            detail_screen(app)._safe_paint()
            await settle(app, pilot)
            assert "campaign failed" in status(app)
            monkeypatch.undo()
            detail_screen(app)._poll()
            await settle(app, pilot)
            assert app.is_running and "campaign failed" not in status(app)

    _run(drive())


def test_mailbox_append_triggers_poll_reload(tmp_path, monkeypatch):
    import json
    from nightwright.agentic import mailbox as MB

    monkeypatch.setattr(CD, "POLL_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    loads = []
    real = AM.load_detail

    def track(*a, **k):
        loads.append(1)
        return real(*a, **k)

    monkeypatch.setattr(AM, "load_detail", track)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "queued")
            await settle(app, pilot)
            n = len(loads)
            path = MB.mailbox_dir(cfg) / "poll-test.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {"id": f"m-{n}", "text": "signature bump"}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            assert await pilot_until(pilot, lambda: len(loads) > n)

    _run(drive())


def test_a_signature_error_in_poll_is_contained(tmp_path, monkeypatch):
    monkeypatch.setattr(CD, "POLL_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    def boom(cfg, campaign_id):
        raise RuntimeError("no signature")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            s = detail_screen(app)
            s._signature = "stale"
            monkeypatch.setattr(AM, "detail_signature", boom)
            s._poll()
            assert await pilot_until(pilot, lambda: s._signature != "stale")
            await settle(app, pilot)
            assert app.is_running
            monkeypatch.setattr(AM, "load_detail", lambda cfg, cid: {}["missing"])
            s._poll()
            assert await pilot_until(pilot, lambda: "campaign failed" in status(app))
            await settle(app, pilot)
            assert app.is_running and s.view is not None
            assert "campaign failed" in status(app)
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            assert not isinstance(app.screen, CD.CampaignDetail)

    _run(drive())


def test_timeline_text_renders_verbatim(tmp_path):
    from nightwright.agentic import store as St

    markup = "[bold red]x[/] and [link=file:///etc]y[/link]"
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    _ev(cfg, cid, "note", {"text": "see [/oops] and [x]"})
    _ev(cfg, cid, "note", {"text": markup})
    state = St.read_state(cfg, cid)
    state = {
        **state,
        "park": {
            "reason": "hold",
            "detail": markup,
            "resume_to": "IMPLEMENTING",
        },
    }
    St.write_state(cfg, cid, state)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            s = detail_screen(app)
            assert app.is_running
            last = s._data_indexes()[-1]
            cells = s.table.get_row_at(last)
            assert all(isinstance(c, Text) for c in cells)
            assert cells[5].plain.endswith(markup)
            ov = s.query_one("#overview-text").content
            assert isinstance(ov, Text)
            assert markup in ov.plain

    _run(drive())


def test_show_detail_error_is_shown_in_the_pane(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            monkeypatch.setattr(AM, "entry_detail",
                                lambda *a: (_ for _ in ()).throw(RuntimeError("no detail [/x]")))
            detail_screen(app).show_detail()
            await settle(app, pilot)
            assert app.is_running
            assert "(detail unavailable: no detail [/x])" in detail(app)

    _run(drive())


def test_filter_key_and_resize_paint_errors_are_contained(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            monkeypatch.setattr(CD.CampaignDetail, "paint",
                                lambda self, previous=None: (_ for _ in ()).throw(TypeError("x")))
            await press(app, pilot, "f")
            await settle(app, pilot)
            assert app.is_running and "campaign failed" in status(app)
            monkeypatch.undo()
            detail_screen(app).reload()
            await settle(app, pilot)
            assert "campaign failed" not in status(app)
            monkeypatch.setattr(CD.CampaignDetail, "paint",
                                lambda self, previous=None: (_ for _ in ()).throw(TypeError("y")))
            detail_screen(app).on_resize(None)
            await settle(app, pilot)
            assert app.is_running and "campaign failed" in status(app)

    _run(drive())


def test_terminal_campaign_does_not_start_a_poll_timer(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["merged"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "delivered", timeline=False)
            s = detail_screen(app)
            await settle(app, pilot)
            assert s.view.terminal
            assert s._poll_timer is None

    _run(drive())


def test_campaign_screen_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("nightwright.tui.campaign_screen")
    from nightwright.tui.campaign_detail import CampaignDetail
    assert CampaignDetail is not None
