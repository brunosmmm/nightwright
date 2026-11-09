"""SPEC-0326: `nw queue watch` redesign — the journey track, the why line, waiting_for, readable
event sentences, the adaptive card density and the fixed-budget bottom sections."""
import datetime as dt
import io

from rich.console import Console

from test_agentic_dashboard import _campaign, _ev, _fixture
from test_agentic_e2e import impl_done, make_cfg, make_repo, run_cli, script
from nightwright.agentic import cli_queue as CQ
from nightwright.agentic import journey as JY
from nightwright.agentic import queue as Q
import pytest

pytestmark = pytest.mark.fast

TS = "2026-10-31T10:00:00-04:00"


def _t(frm, to, reason, ts=TS):
    return {"type": "transition", "from": frm, "to": to, "reason": reason, "ts": ts}


def _gate(ok, *failed, timed_out=False):
    return {"type": "gate", "ok": ok, "gate_seq": 1, "duration_s": 60, "results": [
        {"name": name, "ok": False, "timed_out": timed_out, "summary": {"failed": n}}
        for name, n in failed] + ([{"name": "lint", "ok": True}] if ok else [])}


REVIEW_LOOP = [
    _t("QUEUED", "OPENING", "admitted"), _t("OPENING", "IMPLEMENTING", "opened"),
    _t("IMPLEMENTING", "GATING", "implemented"), _gate(True), _t("GATING", "REVIEWING", "gates_green"),
    {"type": "finding", "severity": "must_fix", "persona": "conformance_reviewer", "title": "a"},
    {"type": "finding", "severity": "must_fix", "persona": "test_reviewer", "title": "b"},
    {"type": "finding", "severity": "major", "persona": "test_reviewer", "title": "c"},
    {"type": "note", "text": "review score 43/80 fix", "author": "queue"},
    _t("REVIEWING", "IMPLEMENTING", "review_fix"),
]


# ---- track ---------------------------------------------------------------------------------

def test_track_shows_the_fix_loop_and_outcomes():
    visits, elided = JY.track(REVIEW_LOOP, "IMPLEMENTING")
    assert elided == 0
    assert [(v["state"], v["ok"], v["outcome"]) for v in visits] == [
        ("IMPLEMENTING", True, "ok"), ("GATING", True, "ok"),
        ("REVIEWING", False, "43 · 2 must-fix · 1 major"), ("IMPLEMENTING", None, None)]
    assert visits[-1]["reason"] == "review_fix" and visits[-1]["at"] == TS
    text = CQ._track_text({"track": visits, "track_elided": 0, "state": "IMPLEMENTING"}).plain
    assert text == "impl ✓ → gates ✓ → review ✗ 43 · 2 must-fix · 1 major → impl ●"


def test_track_gate_failures_triage_and_timeout():
    led = [_t("OPENING", "IMPLEMENTING", "opened"), _t("IMPLEMENTING", "GATING", "implemented"),
           _gate(False, ("pytest", 2)), _t("GATING", "TRIAGING", "gates_failed"),
           _t("TRIAGING", "GATING", "retry_gate"), _gate(False, ("pytest", 0), timed_out=True),
           _t("GATING", "TRIAGING", "gates_failed"), _t("TRIAGING", "IMPLEMENTING", "triage_fix")]
    visits, _ = JY.track(led, "IMPLEMENTING")
    assert [(v["state"], v["outcome"]) for v in visits] == [
        ("IMPLEMENTING", "ok"), ("GATING", "2 failed"), ("TRIAGING", "retry"),
        ("GATING", "timeout"), ("TRIAGING", "fix"), ("IMPLEMENTING", None)]


def test_track_merges_consecutive_duplicates_and_caps():
    # a re-entry into the same state right after skipped QUEUED/OPENING visits is one visit
    led = [_t("QUEUED", "OPENING", "admitted"), _t("OPENING", "IMPLEMENTING", "opened"),
           _t("IMPLEMENTING", "PARKED", "cost_cap"), _t("PARKED", "QUEUED", "resumed"),
           _t("QUEUED", "OPENING", "admitted"), _t("OPENING", "PARKED", "open_failed")]
    visits, _ = JY.track(led, "PARKED")
    assert [v["state"] for v in visits] == ["IMPLEMENTING", "PARKED"]
    loop = [_t("OPENING", "IMPLEMENTING", "opened")]
    for _ in range(6):
        loop += [_t("IMPLEMENTING", "GATING", "implemented"), _gate(False, ("pytest", 1)),
                 _t("GATING", "TRIAGING", "gates_failed"), _t("TRIAGING", "IMPLEMENTING", "triage_fix")]
    visits, elided = JY.track(loop, "IMPLEMENTING")
    assert len(visits) == JY.TRACK_CAP and elided == 19 - JY.TRACK_CAP
    assert visits[-1] == {"state": "IMPLEMENTING", "reason": "triage_fix", "at": TS,
                          "ok": None, "outcome": None}
    text = CQ._track_text({"track": visits, "track_elided": elided, "state": "IMPLEMENTING"},
                          width=40).plain
    assert text.startswith("+") and text.endswith("impl ●") and len(text) <= 40


def test_track_without_transitions_is_the_current_state():
    assert JY.track([], "GATING") == ([{"state": "GATING", "reason": None, "at": None,
                                        "ok": None, "outcome": None}], 0)


# ---- why / waiting_for ---------------------------------------------------------------------

def test_why_review_fix_loop():
    st = {"state": "IMPLEMENTING"}
    assert JY.why(st, REVIEW_LOOP) == \
        "back to implementing: review scored 43 < 80 (2 must-fix, 1 major)"


def test_why_gate_retry_after_triage():
    led = [_t("IMPLEMENTING", "GATING", "implemented"), _gate(False, ("pytest", 2)),
           _t("GATING", "TRIAGING", "gates_failed"), _t("TRIAGING", "GATING", "retry_gate")]
    assert JY.why({"state": "GATING"}, led) == \
        "re-running gates: triage says the failures are flaky"
    assert JY.why({"state": "TRIAGING"}, led[:3]) == "triaging: gates failed (2 failed)"


def test_why_parked_cost_cap_and_loop_limit():
    st = {"state": "PARKED", "park": {"reason": "cost_cap", "detail": "x"},
          "cost": {"cap_usd": 15}}
    assert JY.why(st, []) == "parked: cost cap $15 reached"
    st = {"state": "PARKED", "park": {"reason": "loop_exhausted"}}
    assert JY.why(st, [], loops="3/3") == "parked: loop limit reached (3/3)"
    st = {"state": "PARKED", "park": {"reason": "rebase_conflict", "detail": "docs/LEDGER.md"}}
    assert JY.why(st, []) == "parked: rebase conflict — docs/LEDGER.md"


def test_why_dependency_and_close_train_waits():
    w = JY.waiting_for({"state": "QUEUED", "backlog": {"blocked_by": ["SPEC-0302"]}}, [])
    assert w == {"kind": "dependency", "on": ["SPEC-0302"]}
    assert JY.why({"state": "QUEUED"}, [], w) == "waiting on SPEC-0302"
    st = {"state": "CLOSING", "closing": {"queued_behind": "IDEA-511-20261031-eda6"}}
    w = JY.waiting_for(st, [])
    assert JY.why(st, [], w) == "waiting to close behind IDEA-511"
    st = {"state": "WAITING", "waiting": {"resume_to": "GATING"}}
    w = JY.waiting_for(st, [], busy=5, total=3)
    assert JY.why(st, [], w) == "waiting for a campaign slot (5/3 busy), then gates"


def test_waiting_for_gate_slot_from_occupancy_and_ledger():
    st = {"id": "C1", "state": "GATING"}
    led = [_t("IMPLEMENTING", "GATING", "implemented")]
    occ = {"slots": 2, "held": [{"slot": 0, "campaign": "IDEA-511-x-y"},
                                {"slot": 1, "campaign": "IDEA-509-x-y"}],
           "waiting": [{"campaign": "C1", "since": "2026-10-31T10:00:00-04:00"}]}
    now = dt.datetime.fromisoformat("2026-10-31T10:00:30-04:00")
    w = JY.waiting_for(st, led, occ, now=now)
    assert w == {"kind": "gate_slot", "slots": 2, "held": ["IDEA-511-x-y", "IDEA-509-x-y"],
                 "since": "2026-10-31T10:00:00-04:00", "waited_s": 30}
    assert JY.waiting_text(w) == "waiting for a gate slot (2/2 held: IDEA-511, IDEA-509)"
    # the gate-slot wait is the `now` line, not the why
    assert JY.why(st, led, w) == "gating the implementation"
    now_line = CQ._now_text({"state": "GATING", "activity": {"kind": "gates"},
                             "waiting_for": w}).plain
    assert now_line == "⏳ waiting for a gate slot (2/2 held: IDEA-511, IDEA-509) · 30s"
    # holding a slot / not listed: not waiting
    assert JY.waiting_for(st, led, {**occ, "waiting": []}) is None
    # no snapshot: the last gate_waiting entry after the transition, until the gate starts
    gw = {"type": "gate_waiting", "slots": 2, "held": occ["held"], "waited_s": 30.3}
    assert JY.waiting_for(st, led + [gw])["held"] == ["IDEA-511-x-y", "IDEA-509-x-y"]
    assert JY.waiting_for(st, led + [gw, _gate(True)]) is None
    assert JY.waiting_for({"id": "C1", "state": "IMPLEMENTING"}, led + [gw]) is None


# ---- readable events -----------------------------------------------------------------------

def test_event_sentences():
    hhmm = JY.hhmm(TS)
    cases = [
        (_t("GATING", "TRIAGING", "gates_failed"), "gates → triage: gates failed"),
        ({"type": "run.start", "persona": "implementer", "model": "opus", "effort": "high"},
         "started implementer (opus/high)"),
        ({"type": "run.end", "persona": "implementer", "duration_s": 125, "cost_usd": 1.5,
          "timed_out": True}, "implementer finished in 2m05s · $1.50 · timed out"),
        ({"type": "cost", "persona": "gate_triager", "delta_usd": 0.0928554, "total_usd": 2.516},
         "gate_triager cost $0.09, total $2.52"),
        ({"type": "commit", "sha": "63c7cff020e1c43ca0a95bd48149726ddb2665a2",
          "message": "nw(IDEA-508-x): implementer run 004 — fix it\n\nCo-Authored-By: x"},
         "commit 63c7cff implementer run 004 — fix it"),
        (_gate(False, ("pytest", 2)), "gates #1 failed: pytest (2 failed)"),
        ({"type": "gate_waiting", "slots": 2, "held": [{"campaign": "IDEA-511-a-b"}]},
         "waiting for a gate slot (1/2 held: IDEA-511)"),
        ({"type": "finding", "severity": "must_fix", "persona": "test_reviewer", "title": "T"},
         "must-fix from test: T"),
        ({"type": "note", "text": "review score 73/80 fix", "author": "queue"},
         "review score 73/80 fix"),
        ({"type": "note", "text": "decision wait", "author": "conductor"},
         "conductor: decision wait"),
        ({"type": "park", "reason": "cost_cap", "detail": "cap $15"}, "parked: cost cap — cap $15"),
        ({"type": "resume", "to": "IMPLEMENTING", "note": "go"}, "resumed → impl: go"),
        ({"type": "spec_written", "spec_id": "SPEC-0318", "mode": "revise"}, "spec revise: SPEC-0318"),
        ({"type": "spec_review", "round": 1, "verdict": "revise",
          "counts": {"must_fix": 0, "major": 2}}, "spec review r1: revise (2 major)"),
        ({"type": "spec_accepted", "spec_id": "SPEC-0318", "round": 1},
         "spec SPEC-0318 accepted (round 1)"),
        ({"type": "close", "mode": "merge", "merged_sha": "abcdef123456"}, "closed: merge abcdef1"),
    ]
    for ev, text in cases:
        assert JY.event_sentence({**ev, "ts": TS}) == f"{hhmm} {text}", ev


def test_event_sentence_fallback_is_truncated_key_values():
    ev = {"type": "loop_divergence", "ts": TS, "seq": 3, "by": "tick", "loop": 2,
          "signals": ["x" * 80], "rounds": None}
    out = JY.event_sentence(ev)
    assert out.startswith(f"{JY.hhmm(TS)} loop_divergence loop=2 signals=")
    assert "rounds" not in out and "seq" not in out and len(out) <= 106
    assert JY.event_sentence({"type": "odd"}) == "--:-- odd"


def test_recent_lines_skip_cost_rows():
    led = [{"type": "commit", "sha": "a" * 40, "message": "m", "ts": TS},
           {"type": "cost", "persona": "p", "delta_usd": 1, "total_usd": 1, "ts": TS},
           _t("IMPLEMENTING", "GATING", "implemented")]
    assert [r["type"] for r in JY.recent_lines(led)] == ["commit", "transition"]


# ---- layout --------------------------------------------------------------------------------

def _item(i, state="GATING"):
    return {"campaign": f"IDEA-{500 + i}-20261031-abcd", "spec_id": f"SPEC-{300 + i}",
            "spec_title": "A title", "project": "Meta-Tools", "state": state, "loops": "1/3",
            "runs": 3, "cost_usd": 2.5, "cap_usd": 15, "cost": {}, "age_s": 3600,
            "state_age_s": 120, "profile": "balanced", "activity": {"kind": "gates"},
            "why": "gating the fix", "waiting_for": None, "reviews": [],
            "track": [{"state": "IMPLEMENTING", "ok": True, "outcome": "ok"},
                      {"state": state, "ok": None, "outcome": None}], "track_elided": 0,
            "recent_lines": [{"text": "10:00 impl → gates: implemented"}]}


def test_layout_picks_the_densest_style_that_fits():
    items = [_item(i) for i in range(5)]
    full = CQ._lines(CQ._card_grid(items, 100, "full"), 100)
    compact = CQ._lines(CQ._card_grid(items, 100, "compact"), 100)
    assert compact == 20 and full > compact
    assert CQ.active_region(items, 100, None)[0] == "full"
    assert CQ.active_region(items, 100, full)[0] == "full"
    assert CQ.active_region(items, 100, full - 1)[0] == "compact"
    assert CQ.active_region(items, 100, 14)[0] == "row"
    density, rows = CQ.active_region(items, 100, 3)
    text = _plain(rows, 100)
    assert density == "row" and "+3 more" in text and len(text.splitlines()) == 3
    # columns: 1, 2 at >= 140, 3 at >= 210
    assert [CQ.card_columns(w, 5) for w in (139, 140, 209, 210)] == [1, 2, 2, 3]
    assert CQ.card_columns(300, 1) == 1


def _plain(renderable, width):
    c = Console(width=width, file=io.StringIO(), record=True, force_terminal=False)
    c.print(renderable)
    return c.export_text()


def test_title_is_inside_the_card_and_leads_the_row():
    """SPEC-0453: the border is the id and the state. The title gets the card body,
    the compact card's first line, and the front of the row's flexible column."""
    it = _item(0)
    border = CQ._card_title(it).plain
    assert it["campaign"] in border and it["state"] in border and "loop 1/3" in border
    assert "A title" not in border and "SPEC-300" not in border
    panel = _plain(CQ._active_panel(it), 120)
    assert "A title" in panel and "SPEC-300" in panel
    compact = [ln.strip() for ln in _plain(CQ._card_compact(it), 120).splitlines() if ln.strip()]
    assert "A title" in compact[0] and it["campaign"] in compact[1]
    row = _plain(CQ._cards_rows([it]), 160)
    assert "A title" in row and "gating the fix" in row
    assert row.find("A title") < row.find("gating the fix")


def test_bottom_sections_truncate_with_more_rows(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    for i in range(7):
        _campaign(cfg, repo, f"SPEC-0001-q{i}", "QUEUED")
    for i in range(6):
        _campaign(cfg, repo, f"SPEC-0001-p{i}", "PARKED",
                  park={"reason": "rate_limited", "detail": "429", "resume_to": "IMPLEMENTING"})
    for i in range(8):
        _campaign(cfg, repo, f"SPEC-0001-m{i}", "MERGED", updated_min=5 + i)
    for i in range(6):
        _campaign(cfg, repo, f"SPEC-0001-a{i}", "IMPLEMENTING")
    p = Q.dashboard_payload(cfg, recent=8)
    for width in (100, 160):
        out = _plain(CQ._dashboard(p, width, 24, height=30, fill=False), width)
        assert "Backlog (7)" in out and "+3 more" in out           # 4 of 7 shown
        assert "Recently delivered (8" in out                     # 5 of 8 shown
        assert "Conductor handling (6)" in out and "+2 more" in out
        assert "Active (6)  row view" in out
        lines = out.splitlines()
        assert lines[-1].strip(), "no trailing blank: the delivered rows end the frame"
    # Backlog and Recently delivered sit side by side from 120 columns
    wide = _plain(CQ._dashboard(p, 160, 24, height=30, fill=False), 160)
    assert any("Backlog (7)" in ln and "Recently delivered" in ln for ln in wide.splitlines())


def test_once_frame_at_small_height_keeps_bottom_sections(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    for i in range(4):
        _campaign(cfg, repo, f"SPEC-0001-x{i}", "IMPLEMENTING")
    r = run_cli(cfg, monkeypatch, ["queue", "watch", "--once", "--height", "30"])
    assert r.exit_code == 0, r.output
    assert "Backlog (2)" in r.output and "Recently delivered (2" in r.output
    assert "Active (6)" in r.output and "view" in r.output
    assert len(r.output.rstrip("\n").splitlines()) <= 30 + 2


def test_payload_adds_journey_fields(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    _ev(cfg, ids["gating"], "transition", {"from": "IMPLEMENTING", "to": "GATING",
                                           "reason": "implemented"})
    _ev(cfg, ids["gating"], "gate_waiting", {"slots": 2, "held": [
        {"slot": 0, "campaign": "X-1-a-b"}], "waited_s": 31.0}, by="gates")
    p = Q.dashboard_payload(cfg)
    assert p["slots"]["over"] == 0 and "gate_slots" in p
    it = {a["campaign"]: a for a in p["active"]}[ids["gating"]]
    assert it["track"][-1]["state"] == "GATING" and it["track_elided"] == 0
    assert it["why"] == "gating the implementation"
    assert it["state_age_s"] is not None
    # occupancy (on disk) shows no waiter, so no gate-slot wait is claimed
    assert it["waiting_for"] is None
    assert it["recent_lines"][-1]["text"].endswith("waiting for a gate slot (1/2 held: X-1)")
    back = {b["campaign"]: b for b in p["backlog"]}
    assert back[ids["blocked"]]["waiting_for"]["kind"] == "dependency"
    assert back[ids["blocked"]]["why"].startswith("waiting on ")
    assert back[ids["ready"]]["why"] == "ready: admitted at the next free slot"
    assert p["conductor_handling"][0]["why"].startswith("parked: rate limited")


def test_header_says_when_campaigns_exceed_the_slots():
    p = {"lease": {"held": False}, "slots": {"total": 3, "busy": 5, "over": 2},
         "gate_slots": {"total": 2, "held": ["a", "b"], "waiting": ["c"]},
         "totals": {"active": 5, "ready": 0, "blocked": 0, "delivered": 0,
                    "spend_today_usd": 1.0}, "generated_at": TS}
    out = _plain(CQ._dash_header(p, 160), 160)
    assert "campaigns" in out and "5/3 over by 2, no admissions until ≤3" in out
    assert "gates 2/2 +1 waiting" in out
