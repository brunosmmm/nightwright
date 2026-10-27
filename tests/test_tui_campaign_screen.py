"""SPEC-0246: the TUI campaign detail screen (Textual pilot tests)."""
import json

import pytest

from test_agentic_dashboard import _campaign, _ev, _fixture
from test_agentic_e2e import run_cli
from test_tui_queue_tab import SIZE, _desk, _run, act, press, select, settle, snapshot, view
from wt import tui as T
from wt.agentic import events as EV
from wt.agentic import store as S
from wt.tui import campaign_screen as CS
from waits import pilot_until

needs_textual = pytest.mark.skipif(not T.textual_available(),
                                   reason="optional tui extra not installed")
pytestmark = needs_textual


def text_of(widget) -> str:
    return str(widget.content)


def screen(app) -> CS.CampaignScreen:
    assert isinstance(app.screen, CS.CampaignScreen), app.screen
    return app.screen


def status(app) -> str:
    return text_of(screen(app).query_one("#campaign-status"))


def detail(app) -> str:
    return text_of(screen(app).query_one("#campaign-detail-text"))


def seed(cfg, cid):
    """A ledger with a run, a gate, a finding and a note on top of the fixture's events."""
    _ev(cfg, cid, "transition", {"from": "QUEUED", "to": "IMPLEMENTING", "reason": "start"})
    _ev(cfg, cid, "run.start", {"persona": "implementer", "run_id": "001-implementer"})
    _ev(cfg, cid, "gate", {"gate_seq": 1, "ok": False, "results": [
        {"name": "pytest", "ok": False, "required": True, "exit_code": 1, "duration_s": 1.0,
         "log": "gates/missing.log"}]})
    _ev(cfg, cid, "finding", {"persona": "reviewer", "severity": "major", "title": "Broken",
                              "detail": "d" * 100})
    _ev(cfg, cid, "note", {"text": "hello there"})


async def open_screen(app, pilot, cid, section):
    await settle(app, pilot)
    select(app, section, cid)
    await press(app, pilot, ["enter"])
    await settle(app, pilot)


def test_enter_opens_the_screen_and_escape_returns_with_the_cursor(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "backlog")
            s = screen(app)
            assert s.campaign_id == cid
            assert text_of(s.query_one("#campaign-header")).startswith(cid)
            await press(app, pilot, ["escape"])
            await settle(app, pilot)
            assert len(app.screen_stack) == 1
            assert view(app).current_row().campaign == cid
            await open_screen(app, pilot, cid, "backlog")
            await press(app, pilot, ["q"])                  # q also pops, it does not quit
            await settle(app, pilot)
            assert len(app.screen_stack) == 1 and app.is_running

    _run(drive())


def test_cursor_starts_on_the_last_event_and_filters_cycle(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    total = len(S.read_ledger(cfg, cid))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "backlog")
            s = screen(app)
            assert len(s.shown) == total and s.table.cursor_row == total - 1
            assert status(app) == f"filter all · {total}/{total} events"
            assert "hello there" in detail(app)
            seen = []
            for _ in range(5):
                await press(app, pilot, ["f"])
                seen.append((s.filter, len(s.shown), {e.group for e in s.shown}))
                assert status(app) == f"filter {s.filter} · {len(s.shown)}/{total} events"
            assert [x[0] for x in seen] == ["transitions", "runs", "gates", "findings", "notes"]
            assert all(g == {f} for f, n, g in seen if n)
            assert sum(n for _, n, _ in seen) == total
            await press(app, pilot, ["f"])
            assert s.filter == "all" and len(s.shown) == total
            # g / G jump
            await press(app, pilot, ["g"])
            assert s.table.cursor_row == 0
            await press(app, pilot, ["G"])
            assert s.table.cursor_row == total - 1

    _run(drive())


def test_detail_pane_follows_the_cursor_and_v_toggles_the_review(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "backlog")
            s = screen(app)
            await press(app, pilot, ["f", "f", "f"])            # gates
            assert "pytest: FAIL" in detail(app) and "(missing:" in detail(app)
            await press(app, pilot, ["f"])                      # findings
            assert "d" * 100 in detail(app)
            await press(app, pilot, ["f", "f"])                 # all
            await press(app, pilot, ["f", "f"])                 # runs: run.start, no run dir
            assert "persona: implementer" in detail(app)
            await press(app, pilot, ["v"])
            assert s.review_mode and "Code review" in detail(app)
            assert "no code review yet" in detail(app)
            await press(app, pilot, ["j", "k"])                 # moving keeps review mode
            assert "Spec review" in detail(app)
            await press(app, pilot, ["v"])
            assert not s.review_mode and "persona: implementer" in detail(app)

    _run(drive())


def test_a_ledger_append_shows_within_a_poll_and_never_syncs(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "POLL_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)

    def boom(*a, **k):
        raise AssertionError("the campaign screen must stay read-only")
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "backlog")
            for name in ("sync", "cursor_set", "reader_lock"):
                monkeypatch.setattr(EV, name, boom, raising=False)
            s = screen(app)
            n = len(s.shown)
            await press(app, pilot, ["f", "f", "f"])            # gates, 1 row
            gate_total = len(s.shown)
            _ev(cfg, cid, "gate", {"gate_seq": 2, "ok": True, "results": []})
            assert await pilot_until(pilot, lambda: len(s.shown) == gate_total + 1)
            await settle(app, pilot)
            assert s.filter == "gates" and len(s.shown) == gate_total + 1
            assert s.table.cursor_row == len(s.shown) - 1           # followed the end
            # on an earlier row the cursor stays on its seq
            await press(app, pilot, ["g"])
            first_seq = s.shown[0].seq
            _ev(cfg, cid, "gate", {"gate_seq": 3, "ok": True, "results": []})
            assert await pilot_until(pilot, lambda: len(s.shown) == gate_total + 2)
            await settle(app, pilot)
            assert len(s.shown) == gate_total + 2
            assert s.shown[s.table.cursor_row].seq == first_seq
            assert status(app).endswith(f"/{n + 2} events")

    _run(drive())


def test_park_runs_end_to_end_through_the_screen(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "backlog")
            s = screen(app)
            await press(app, pilot, ["P", "escape"])             # priority picker, cancelled
            await press(app, pilot, ["p", *"hold", "enter", "enter"])
            await settle(app, pilot)
            assert S.read_state(cfg, cid)["state"] == "PARKED"
            assert isinstance(app.screen, CS.CampaignScreen)
            assert "PARKED" in text_of(s.query_one("#campaign-header"))
            assert "parked" in text_of(s.query_one("#campaign-header"))
            assert s.detail.actions == ("resume", "prioritize", "abandon")
            await press(app, pilot, ["p"])                       # no longer offered
            assert any("park not available" in m for m, _ in app.notes)
            await press(app, pilot, ["A", "enter"])              # abandon: stays open
            await settle(app, pilot)
            assert S.read_state(cfg, cid)["state"] == "ABANDONED"
            assert isinstance(app.screen, CS.CampaignScreen)
            assert s.detail.actions == ()

    _run(drive())


def test_idea_keys_are_inert_on_the_screen(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await open_screen(app, pilot, cid, "backlog")
            for key in ("x", "i", "X", "l", "c", "t", "m", "s", "a"):
                await press(app, pilot, [key])
                assert len(app.screen_stack) == 2 and isinstance(app.screen, CS.CampaignScreen)

    _run(drive())


def test_campaign_option_opens_the_screen_directly(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, campaign=cid)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert app.query_one("#tabs").active == "tab-queue"
            assert screen(app).campaign_id == cid
            await press(app, pilot, ["escape"])
            assert len(app.screen_stack) == 1

    _run(drive())


# --- `wt tui --campaign` ----------------------------------------------------------------------

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
    assert len(calls) == n                                  # the TUI never started
    plain = run_cli(cfg, monkeypatch, ["tui"])
    assert plain.exit_code == 0 and calls[-1]["campaign"] is None


# --- bad data and untrusted text (SPEC-0265) ---------------------------------------------

def _section_of(app, cid):
    return next(r.section for r in view(app).rows if r.campaign == cid)


def test_reload_error_keeps_the_app_alive(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            monkeypatch.setattr(CS.CampaignScreen, "paint",
                                lambda self, previous=None: (_ for _ in ()).throw(TypeError("x")))
            screen(app).reload()
            await settle(app, pilot)
            assert app.is_running and "campaign failed" in status(app)
            monkeypatch.undo()
            screen(app).reload()
            await settle(app, pilot)
            assert "campaign failed" not in status(app)

    _run(drive())


def test_a_failed_paint_is_retried_by_the_next_poll(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            monkeypatch.setattr(CS.CampaignScreen, "paint",
                                lambda self, previous=None: (_ for _ in ()).throw(TypeError("x")))
            screen(app).reload()
            await settle(app, pilot)
            assert "campaign failed" in status(app)
            monkeypatch.undo()                  # nothing changes on disk
            screen(app)._poll()
            await settle(app, pilot)
            assert app.is_running and "campaign failed" not in status(app)

    _run(drive())


def test_a_signature_error_in_poll_is_contained(tmp_path, monkeypatch):
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
            screen(app)._signature = "stale"
            monkeypatch.setattr(CS.AM, "campaign_signature", boom)
            screen(app)._poll()                 # falls back to a reload, which succeeds
            await settle(app, pilot)
            assert app.is_running and screen(app)._signature != "stale"
            monkeypatch.setattr(CS.AM, "load_campaign", lambda cfg, cid: {}["missing"])
            screen(app)._poll()
            await settle(app, pilot)
            assert app.is_running and "campaign failed" in status(app)

    _run(drive())


def test_timeline_text_renders_verbatim(tmp_path):
    from rich.text import Text
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    _ev(cfg, cid, "note", {"text": "see [/oops] and [x]"})
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await open_screen(app, pilot, cid, _section_of(app, cid))
            s = screen(app)
            assert app.is_running
            cells = s.table.get_row_at(len(s.shown) - 1)
            assert all(isinstance(c, Text) for c in cells)
            assert cells[3].plain.endswith("see [/oops] and [x]")      # after the "text=" label

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
            monkeypatch.setattr(CS.AM, "entry_detail",
                                lambda *a: (_ for _ in ()).throw(RuntimeError("no detail [/x]")))
            screen(app).show_detail()
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
            monkeypatch.setattr(CS.CampaignScreen, "paint",
                                lambda self, previous=None: (_ for _ in ()).throw(TypeError("x")))
            await press(app, pilot, "f")
            await settle(app, pilot)
            assert app.is_running and "campaign failed" in status(app)
            monkeypatch.undo()
            screen(app).reload()
            await settle(app, pilot)
            assert "campaign failed" not in status(app)
            monkeypatch.setattr(CS.CampaignScreen, "paint",
                                lambda self, previous=None: (_ for _ in ()).throw(TypeError("y")))
            screen(app).on_resize(None)
            await settle(app, pilot)
            assert app.is_running and "campaign failed" in status(app)

    _run(drive())
