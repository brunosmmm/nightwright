"""SPEC-0410: the queue tab's grouped row model (no Textual needed)."""
import random

import pytest

from nightwright.tui import agentic_model as AM


def _it(cid, state="IMPLEMENTING", **kw):
    return {"campaign": cid, "spec_id": "SPEC-0001", "spec_title": f"title {cid}", "state": state,
            "priority": 3, "created_at": "2026-11-03T10:00:00+00:00", "loops": "1/3",
            "cost_usd": 1.0, "cap_usd": 4.0, "project": "P", "why": f"why {cid}",
            "activity": {"kind": "idle"}, "reviews": [], **kw}


def _payload(**lists):
    base = {k: [] for k in ("needs_you", "conductor_handling", "active", "waiting", "backlog",
                            "delivered")}
    return {**base, **lists}


def _groups(rows):
    return [r.group for r in rows if r.kind == "header"]


def _ids(rows, group):
    return [r.campaign for r in rows if r.kind == "campaign" and r.group == group]


STATES = {"CLOSING": "closing", "REVIEWING": "reviewing", "GATING": "gating",
          "TRIAGING": "gating", "OPENING": "building", "SPEC_WRITING": "building",
          "SPEC_REVIEW": "building", "IMPLEMENTING": "building", "WEIRD": "building"}


def test_every_active_state_lands_in_its_group():
    payload = _payload(active=[_it(f"C-{s}", s) for s in STATES])
    rows = AM.rows_from_payload(payload)
    for state, group in STATES.items():
        assert f"C-{state}" in _ids(rows, group), state
    assert _groups(rows) == ["closing", "reviewing", "gating", "building"]


def test_group_order_headers_counts_and_no_empty_group():
    payload = _payload(
        needs_you=[_it("N", "PARKED", park={"parked_at": "2026-11-03T09:00:00+00:00"})],
        conductor_handling=[_it("K", "PARKED", park={"parked_at": "2026-11-03T09:00:00+00:00"})],
        active=[_it("A1", "CLOSING"), _it("A2", "CLOSING"), _it("A3", "GATING")],
        waiting=[_it("W", "WAITING", waiting={"since": "2026-11-03T09:00:00+00:00"})],
        backlog=[_it("Q", position=1)],
        delivered=[_it("D", "MERGED", finished_age_s=60, state_since="2026-11-03T09:30:00+00:00")])
    rows = AM.rows_from_payload(payload)
    assert _groups(rows) == ["needs you", "conductor", "closing", "gating", "waiting", "queued",
                             "delivered"]
    counts = {r.group: r.count for r in rows if r.kind == "header"}
    assert counts["closing"] == 2 and counts["delivered"] == 1
    for g, n in counts.items():
        assert len(_ids(rows, g)) == n
    assert all(r.campaign == "" and r.actions == () for r in rows if r.kind == "header")
    assert rows[0].kind == "header" and rows[1].kind == "campaign"      # header precedes its rows


def test_empty_queue_is_one_placeholder_and_collapsed_emits_header_only():
    rows = AM.rows_from_payload(_payload())
    assert [(r.kind, r.title, r.campaign) for r in rows] == [("placeholder", AM.EMPTY_META, "")]
    payload = _payload(delivered=[_it("D", "MERGED", finished_age_s=1, state_since="x")])
    rows = AM.rows_from_payload(payload, collapsed=frozenset({"delivered"}))
    assert [(r.kind, r.group, r.count) for r in rows] == [("header", "delivered", 1)]


def test_sort_keys_and_independence_from_payload_order():
    act = [_it("B", priority=2, created_at="2026-11-03T10:00:00+00:00"),
           _it("A", priority=2, created_at="2026-11-03T10:00:00+00:00"),
           _it("C", priority=1, created_at="2026-11-03T12:00:00+00:00"),
           _it("D", priority=None, created_at="2026-11-03T08:00:00+00:00"),   # default priority 3
           _it("E", priority=2, created_at="2026-11-03T09:00:00+00:00")]
    park = lambda at, **k: {"parked_at": at, **k}               # noqa: E731
    cond = [_it("K2", "PARKED", park=park("2026-11-03T09:00:00+00:00")),
            _it("K1", "PARKED", park=park("2026-11-03T09:00:00+00:00")),
            _it("KO", "PARKED", park=park("2026-11-03T10:00:00+00:00", overdue=True)),
            _it("KA", "PARKED", park=park("2026-11-03T08:00:00+00:00"))]
    wait = [_it("W3", "WAITING", priority=3, waiting={"since": "2026-11-03T07:00:00+00:00"}),
            _it("W2", "WAITING", priority=1, waiting={"since": "2026-11-03T09:00:00+00:00"}),
            _it("W1", "WAITING", priority=1, waiting={"since": "2026-11-03T08:00:00+00:00"})]
    back = [_it("Q3", position=3), _it("Q1", position=1), _it("Q2", position=2)]
    dele = [_it("D1", "MERGED", state_since="2026-11-03T09:00:00+00:00", finished_age_s=5),
            _it("D3", "MERGED", state_since="2026-11-03T11:00:00+00:00", finished_age_s=1),
            _it("D2a", "MERGED", state_since="2026-11-03T10:00:00+00:00", finished_age_s=2),
            _it("D2b", "MERGED", state_since="2026-11-03T10:00:00+00:00", finished_age_s=2)]
    payload = _payload(active=act, conductor_handling=cond, waiting=wait, backlog=back,
                       delivered=dele)
    rows = AM.rows_from_payload(payload)
    assert _ids(rows, "building") == ["C", "E", "A", "B", "D"]
    assert _ids(rows, "conductor") == ["KA", "K1", "K2", "KO"]         # overdue does not jump
    assert _ids(rows, "waiting") == ["W1", "W2", "W3"]
    assert _ids(rows, "queued") == ["Q1", "Q2", "Q3"]
    assert _ids(rows, "delivered") == ["D3", "D2a", "D2b", "D1"]
    rng = random.Random(7)
    for _ in range(5):
        shuffled = {k: rng.sample(v, len(v)) for k, v in payload.items()}
        assert AM.rows_from_payload(shuffled) == rows


def test_sort_ignores_ages():
    a = _it("A", state_age_s=1)
    b = _it("B", state_age_s=99999, created_at="2026-11-03T09:00:00+00:00")
    assert _ids(AM.rows_from_payload(_payload(active=[a, b])), "building") == ["B", "A"]


def test_row_columns_and_time_in_state():
    rows = AM.rows_from_payload(_payload(
        active=[_it("R", activity={"kind": "run", "persona": "implementer", "elapsed_s": 125},
                    state_age_s=3700)],
        waiting=[_it("W", "WAITING", waiting_s=700, waiting_for={"kind": "campaign_slot"})],
        backlog=[_it("Q", position=1, age_s=90000, loops=None, waiting_for={
            "kind": "dependency", "on": ["SPEC-0009", "SPEC-0010"]})],
        needs_you=[_it("N", "PARKED", park_age_s=5000, park={"parked_at": "x"})]))
    by = {r.campaign: r for r in rows if r.kind == "campaign"}
    assert by["R"].activity == "implementer 2m05s" and by["R"].time_text == "1h"
    assert by["R"].pri == "3" and by["R"].loops == "1/3" and by["R"].why == "why R"
    assert by["W"].activity == "waiting for a campaign slot" and by["W"].time_text == "11m"
    assert by["Q"].activity == "SPEC-0009, SPEC-0010" and by["Q"].time_text == "1d"
    assert by["Q"].state == "QUEUED" and by["Q"].actions == ("park", "prioritize", "abandon")
    assert by["N"].time_text == "1h" and by["N"].actions == ("resume", "prioritize", "abandon")


def test_time_falls_back_to_state_since_then_blank():
    far = "2000-01-01T00:00:00+00:00"
    rows = AM.rows_from_payload(_payload(active=[_it("S", state_since=far),
                                                 _it("X", state_since=None)]))
    by = {r.campaign: r for r in rows if r.kind == "campaign"}
    assert by["S"].time_text.endswith("d") and by["X"].time_text == ""


def test_delivered_rows_show_finished_ago_and_no_priority():
    rows = AM.rows_from_payload(_payload(delivered=[
        _it("D", "MERGED", finished_age_s=7300, state_since="x", why="")]))
    d = rows[-1]
    assert d.time_text == "finished 2h ago" and d.pri == "" and d.loops == "" and d.actions == ()


def test_overdue_is_in_why_for_conductor_rows():
    rows = AM.rows_from_payload(_payload(conductor_handling=[
        _it("K", "PARKED", park={"parked_at": "a", "overdue": True}, why="base moved")]))
    assert rows[-1].why == "base moved · overdue"


@pytest.mark.parametrize("cost,cap,expect", [
    (0, 10, "░" * 8), (5, 10, "████░░░░"), (10, 10, "█" * 8), (50, 10, "█" * 8),
    (3, None, ""), (3, 0, ""), (None, 10, "░" * 8), (-1, 10, "░" * 8)])
def test_cost_bar(cost, cap, expect):
    assert AM.cost_bar(cost, cap) == expect


def test_activity_text_chips_gates_and_gate_slot():                          # SPEC-0277
    it = _it("R", "REVIEWING", reviews=[
        {"persona": "conformance_reviewer", "status": "pass"},
        {"persona": "adversarial_reviewer", "status": "fix"},
        {"persona": "test_reviewer", "status": "running"},
        {"persona": "extra_reviewer", "status": "not_run"}])
    assert AM.activity_text(it) == "✓conf ✗adve ▶test ·extr"
    assert AM.activity_text(_it("G", "GATING", activity={"kind": "gates"})) == "gates"
    named = _it("G", "GATING", activity={"kind": "gates"},
                gate_hold={"progress": {"gate": "pytest", "percent": 4}})
    assert AM.activity_text(named) == "pytest"
    slot = _it("G", "GATING", activity={"kind": "gates"}, waiting_for={"kind": "gate_slot"})
    assert AM.activity_text(slot) == "waiting for a gate slot"
    assert AM.activity_text(_it("I")) == ""


def test_group_of():
    assert AM.group_of("CLOSING", "active") == "closing"
    assert AM.group_of("anything", "active") == "building"
    assert AM.group_of("PARKED", "needs_you") == "needs you"
    assert AM.group_of("QUEUED", "backlog") == "queued"
    assert AM.QUEUE_GROUPS[0] == "needs you" and AM.QUEUE_GROUPS[-1] == "delivered"


@pytest.mark.parametrize("state,expect", [
    ("CLOSING", ("park",)), ("REVIEWING", ("park",)), ("GATING", ("park",)),
    ("WAITING", ("park", "prioritize", "abandon")), ("QUEUED", ("park", "prioritize", "abandon")),
    ("PARKED", ("resume", "prioritize", "abandon")), ("MERGED", ())])
def test_allowed_actions_per_group_row(state, expect):
    src = {"PARKED": "needs_you", "WAITING": "waiting", "QUEUED": "backlog",
           "MERGED": "delivered"}.get(state, "active")
    rows = AM.rows_from_payload(_payload(**{src: [_it("X", state, position=1,
                                                      finished_age_s=1, state_since="a")]}))
    assert rows[-1].actions == expect
