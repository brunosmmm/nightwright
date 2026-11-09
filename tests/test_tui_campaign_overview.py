"""SPEC-0496: the campaign detail Overview pane as dashboard cards."""
import copy

import pytest
from rich.console import Console

from test_agentic_dashboard import _fixture
from test_tui_campaign_detail import detail_screen, open_screen, seed
from test_tui_queue_tab import _desk, _run, settle
from nightwright.tui import campaign_overview as CO
from waits import pilot_until

GEN = "2026-11-09T12:00:00-05:00"


def _phases(**walls):
    order = ("queue", "spec", "implement", "gate", "review", "close", "parked", "waiting")
    return [{"phase": p, "visits": 2 if walls.get(p) else 0, "wall_s": walls.get(p, 0),
             "run_s": 0, "gate_s": 0, "gate_wait_s": 0, "cost_usd": 0, "runs": 0}
            for p in order]


def _run_row(persona, phase, verdict, *, loop=0, status="ok", dur=60.0, model="grok-4.7-high",
             started="2026-11-09T09:00:00-05:00", summary=None):
    return {"run_id": f"x-{persona}", "persona": persona, "phase": phase, "loop": loop,
            "model": model, "effort": "high", "started_at": started, "duration_s": dur,
            "cost_usd": None, "exit_code": 0 if status == "ok" else None, "status": status,
            "verdict_status": verdict, "summary": summary}


def _gate(seq, ok, *, failing="pytest-affected", started="2026-11-09T09:30:00-05:00",
          retries=0):
    return {"gate_seq": seq, "started_at": started, "duration_s": 1260.0, "ok": ok,
            "results": [{"name": "spec-lint", "ok": True, "retries": 0},
                        {"name": failing, "ok": ok, "retries": retries}]}


def _rec(kind, *, severity=None, title="", text="", ts="2026-11-09T09:00:00-05:00",
         agent="conductor"):
    return {"kind": kind, "severity": severity, "title": title, "text": text, "ts": ts,
            "agent": agent}


MERGED = {
    "schema": "nw.campaign.history.v1", "generated_at": GEN,
    "header": {"campaign": "IDEA-1041-20261109-c0c6", "spec_id": "SPEC-0488",
               "idea_id": "IDEA-1041", "title": "SPEC-0488", "project": "Meta-Tools",
               "profile": "cursor", "state": "MERGED", "outcome": "merged",
               "created_at": "2026-11-09T08:17:30-05:00",
               "closed_at": "2026-11-09T10:16:02-05:00", "wall_s": 7112, "active_s": 5400,
               "loops": 0, "loop_max": 3, "runs": 6, "cost_usd": 0.0, "cap_usd": 15.0,
               "conductor_usd": 0.37, "merged_sha": "31d23d9dbcae8c7792e2", "park": None},
    "phases": _phases(queue=1712, spec=1381, implement=43, gate=2679, review=695, close=602),
    "dominant": {"wall": "gate"},
    "timeline": [{"kind": "transition", "phase": "close", "ts": "2026-11-09T10:06:00-05:00"}],
    "runs": [_run_row("spec_writer", "spec", "written", summary="wrote the spec"),
             _run_row("implementer", "implement", "done"),
             _run_row("gate_triager", "gate", "retry_gate", model="grok-4.7-medium"),
             _run_row("conformance_reviewer", "review", "pass", model="grok-4.7-xhigh"),
             _run_row("test_reviewer", "review", "pass", model="grok-4.7-xhigh"),
             _run_row("adversarial_reviewer", "review", "pass", model="grok-4.7-xhigh",
                      dur=700.0)],
    "gates": [_gate(1, False, retries=2), _gate(2, True),
              _gate(3, True, started="2026-11-09T10:06:02-05:00")],
    "records": [_rec("finding", severity="minor"), _rec("finding", severity="minor"),
                _rec("finding", severity="nit"), _rec("finding", severity="info"),
                _rec("note", text="decision other (message.request): Queued on request."),
                _rec("decision", title="other", text="Queued on request."),
                _rec("close_check", text="merge 31d23d9d",
                     ts="2026-11-09T10:16:00-05:00")],
    "gaps": [],
}


def _parked():
    h = copy.deepcopy(MERGED)
    h["header"].update(state="PARKED", outcome=None, closed_at=None, merged_sha=None,
                       loops=4, park={"reason": "human_decision", "detail": "ESC-0051: resume "
                                      "spec writing or abandon?", "resume_to": "IMPLEMENTING",
                                      "escalated_from": "spec_review_exhausted"})
    h["phases"] = _phases(queue=5, spec=3000, gate=7000, review=7000, parked=150000)
    h["dominant"] = {"wall": "parked"}
    h["records"] += [_rec("finding", severity="must_fix"), _rec("finding", severity="major"),
                     _rec("escalation", text="spec review parked again")]
    return h


def _live_gate():
    h = copy.deepcopy(MERGED)
    h["header"].update(state="GATING", outcome=None, closed_at=None, merged_sha=None,
                       wall_s=4000)
    h["phases"] = _phases(queue=14, spec=374, implement=2023, gate=0)
    h["dominant"] = {"wall": "implement"}
    h["runs"].append(_run_row("implementer", "implement", None, loop=1, status="running",
                              dur=None, started="2026-11-09T11:50:00-05:00"))
    h["gates"] = [_gate(1, True), _gate(2, False)]
    h["records"].append(_rec("flaky", title="tests/test_x.py::test_flaky_one"))
    return h


def _abandoned():
    h = copy.deepcopy(MERGED)
    h["header"].update(state="ABANDONED", outcome="abandoned", merged_sha=None)
    h["records"].append(_rec("decision", title="abandon",
                             text="Divergence triager picked rework. Do not resume."))
    return h


NEW = {"header": {"campaign": "SPEC-0500-20261109-aaaa", "state": "QUEUED",
                  "created_at": GEN, "wall_s": 3, "loops": 0, "runs": 0},
       "phases": _phases(), "runs": [], "gates": [], "records": [], "gaps": ["no run.json"],
       "generated_at": GEN}


def text(history, width=120, **kw):
    return CO.Overview(history, **kw).plain(width)


def flat(out: str) -> str:
    """Card text with borders dropped and wrapped lines joined."""
    return " ".join(out.replace("│", " ").split())


def _lines(history, width):
    return text(history, width).rstrip("\n").split("\n")


def test_merged_clean_banner_and_cards():
    out = text(MERGED, title="Cursor launch passes a flat model id")
    first = out.splitlines()[0]
    assert "SPEC-0488 · Cursor launch passes a flat model id" in first
    assert first.rstrip().endswith("✓ MERGED ─╮")
    for want in ("IDEA-1041-20261109-c0c6 · Meta-Tools · IDEA-1041 · cursor", "opened 08:17",
                 "closed 10:16", "wall 1h58m", "active 1h30m", "loops 0/3", "runs 6",
                 "cost $0.00 / $15.00", "conductor $0.37", "merged 31d23d9d"):
        assert want in out, want
    assert "why " not in out and "attention:" not in out
    assert CO.badge(MERGED["header"]) == ("✓ MERGED", "green")
    assert CO.reason(MERGED) is None


def test_time_card_rows_markers_and_zero_phases_omitted():
    out = text(MERGED)
    rows = [ln for ln in out.splitlines() if "░" in ln or "█" in ln]
    phases = [p for p in ("queue", "spec", "implement", "gate", "review", "close", "parked",
                          "waiting") if any(f"│ {p} " in r for r in rows)]
    assert phases == ["queue", "spec", "implement", "gate", "review", "close"]
    gate_row = next(r for r in rows if "│ gate " in r)
    assert "◀" in gate_row and "44m" in gate_row and "×2" in gate_row
    assert "now" not in out


def test_runs_gates_findings_cards():
    out = text(MERGED)
    assert "3 reviewers" in out and "3×pass" in out
    assert "grok-xhigh" in out and "grok-med" in out
    triager = next(ln for ln in out.splitlines() if "gate_triager" in ln)
    assert "↻ retry_gate" in triager
    assert "runs · 6 · 1 retried" in out
    g1 = next(ln for ln in out.splitlines() if "#1 " in ln)
    assert "✗ pytest-affected" in g1 and "↻2" in g1 and "21m" in g1
    assert "close · 2 checks" in next(ln for ln in out.splitlines() if "#3 " in ln)
    assert "gates · 3 · 1 failed" in out
    assert "0 must-fix" in out and "2 minor" in out and "1 nit" in out
    assert "✓ nothing blocking" in out


def test_parked_shows_reason_badge_and_now_marker():
    h = _parked()
    out = text(h, width=110)
    assert "⏸ PARKED" in out.splitlines()[0]
    assert ("why parked: human_decision (from spec_review_exhausted): ESC-0051: resume spec "
            "writing or abandon? → resume to IMPLEMENTING") in flat(out)
    assert "elapsed" in out and "closed" not in out
    parked = next(ln for ln in out.splitlines() if "│ parked " in ln)
    assert "◀ now" in parked
    assert "1 must-fix" in out and "1 escalation" in out and "nothing blocking" not in out
    assert CO.badge(h["header"]) == ("⏸ PARKED", "yellow")


def test_live_in_gate_highlights_phase_running_run_and_failed_gate():
    h = _live_gate()
    out = text(h, width=160)
    assert "● GATING" in out.splitlines()[0]
    assert "attention: gate #2 failed: pytest-affected" in out
    gate = next(ln for ln in out.splitlines() if "│ gate " in ln)
    assert "● now" in gate
    running = next(ln for ln in out.splitlines() if "● running" in ln)
    assert "10m…" in running
    assert "1 flaky" in out and "test_x.py::test_flaky_one" in out


def test_abandoned_reason_from_abandon_decision():
    out = text(_abandoned())
    assert "✗ ABANDONED" in out.splitlines()[0]
    assert "why abandoned: Divergence triager picked rework. Do not resume." in flat(out)


@pytest.mark.parametrize("history", [{}, NEW, {"header": None, "runs": None, "gates": [None]}])
def test_empty_and_new_campaign_render_placeholders(history):
    out = text(history, width=90)
    assert "no runs yet" in out and "no gates yet" in out
    if history is NEW:
        assert "● QUEUED" in out and "no time recorded yet" not in out   # current phase row
        assert "⚠ 1 history gap" in out
    else:
        assert "no time recorded yet" in out


def test_what_happened_dedupes_decision_notes():
    out = text(MERGED)
    assert out.count("Queued on request.") == 1
    assert "decision other (message.request)" not in out
    assert "wrote the spec" in out and "close: merge 31d23d9d" in out


@pytest.mark.parametrize("width", [40, 80, 110, 160, 200])
@pytest.mark.parametrize("make", [lambda: MERGED, _parked, _live_gate, _abandoned,
                                  lambda: NEW, lambda: {}])
def test_every_line_is_exactly_the_width(make, width):
    lines = _lines(make(), width)
    console = Console(width=width)
    assert all(console.measure(ln).maximum <= width for ln in lines)
    assert all(len(ln) == width for ln in lines), [ln for ln in lines if len(ln) != width]


def _row_with(lines, needle):
    return next(ln for ln in lines if needle in ln)


@pytest.mark.parametrize("width,cols", [(200, 3), (150, 3), (149, 2), (96, 2), (95, 1),
                                        (60, 1)])
def test_layout_by_width(width, cols):
    lines = _lines(MERGED, width)
    titles = ("where the time went", "runs · ", "gates · ")
    row = _row_with(lines, "where the time went")
    on_row = [t for t in titles if t in row]
    assert len(on_row) == {3: 3, 2: 2, 1: 1}[cols]
    if cols == 2:
        assert "gates · " in row and "runs · " not in row
    if cols == 1 and width >= CO.SIDE_BY_SIDE_MIN:
        assert "findings" in _row_with(lines, "gates · ")


def test_helpers():
    assert CO.fmt_s(42) == "42s" and CO.fmt_s(1380) == "23m" and CO.fmt_s(7112) == "1h58m"
    assert CO.fmt_s(179679) == "2d01h" and CO.fmt_s(None) == "-"
    assert CO.short_model("grok-4.7-high") == "grok-high"
    assert CO.short_model("composer-2.5") == "composer"
    assert CO.short_model("grok-4.7-medium") == "grok-med"
    assert CO.run_glyph({"status": "timeout"})[0] == "✗"
    assert CO.run_glyph({"status": "ok", "exit_code": 0, "verdict_status": "fix"})[0] == "↻"
    assert CO.run_glyph({"status": "running"})[0] == "●"


def test_popup_overview_shows_banner_and_cards_and_scrolls(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    seed(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await open_screen(app, pilot, cid, "queued", timeline=False)
            s = detail_screen(app)
            widget = s.query_one("#overview-text")
            assert await pilot_until(pilot, lambda: isinstance(widget.content, CO.Overview))
            out = str(widget.content)
            assert cid in out
            for want in ("where the time went", "runs", "gates", "findings"):
                assert want in out, want
            pane = s.query_one("#tab-overview")
            assert await pilot_until(pilot, lambda: pane.max_scroll_y > 0)
            pane.scroll_end(animate=False)
            await settle(app, pilot)
            assert pane.scroll_y > 0

    _run(drive())
