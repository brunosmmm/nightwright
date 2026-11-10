"""SPEC-0507: an exclusive timeout re-gate (SPEC-0311) cannot starve: closes that re-gate
yield to it, its wait is noted on the campaign ledger, and it is bounded by
`agentic.gates.exclusive_wait_max_s`."""
import datetime as dt
import threading

import pytest
from test_agentic_queue import _gate_run, _state, opened  # noqa: F401  (opened is a fixture)
from waits import until

from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

pytestmark = pytest.mark.fast

NOW = dt.datetime(2026, 11, 9, 16, 0, 0).astimezone()


def _ago(seconds: float) -> str:
    return S.now_iso(dt.datetime.now().astimezone() - dt.timedelta(seconds=seconds))


def _gs(cid, entered=None):
    st = _state("GATING", id=cid)
    if entered:
        st["gate_exclusive"] = {"entered_at": entered}
    return st


def _notes(cfg, cid, since=0):
    return [e["text"] for e in S.read_ledger(cfg, cid)[since:] if e["type"] == "note"]


# ---- pure ------------------------------------------------------------------------------------

def test_exclusive_wait_max_setting():
    assert Q.exclusive_wait_max_s({}) == Q.EXCLUSIVE_WAIT_MAX_S
    for raw, want in ((600, 600), (90.5, 90), (0, Q.EXCLUSIVE_WAIT_MAX_S),
                      (-5, Q.EXCLUSIVE_WAIT_MAX_S), (True, Q.EXCLUSIVE_WAIT_MAX_S),
                      ("600", Q.EXCLUSIVE_WAIT_MAX_S), (None, Q.EXCLUSIVE_WAIT_MAX_S)):
        cfg = {"agentic": {"gates": {"exclusive_wait_max_s": raw}}}
        assert Q.exclusive_wait_max_s(cfg) == want, raw


@pytest.mark.parametrize("entered,expired", [
    (None, False),                                           # no marker: never expires
    ((NOW - dt.timedelta(seconds=100)).isoformat(), False),
    ((NOW - dt.timedelta(seconds=1200)).isoformat(), False),  # exactly the bound: still waits
    ((NOW - dt.timedelta(seconds=1201)).isoformat(), True),
    ("not-a-time", True),                                    # unreadable: waited forever
])
def test_expired_exclusives_table(entered, expired):
    assert Q.expired_exclusives([_gs("A", entered)], NOW, 1200) == (["A"] if expired else [])


def test_expired_exclusives_order_and_purity():
    old = (NOW - dt.timedelta(hours=2)).isoformat()
    older = (NOW - dt.timedelta(hours=3)).isoformat()
    a, b, c = _gs("A", old), _gs("B", older), _gs("C", NOW.isoformat())
    before = [dict(x) for x in (a, b, c)]
    assert Q.expired_exclusives([a, c, b], NOW, 1200) == ["B", "A"]
    assert [a, b, c] == before
    # SPEC-0311's selection itself is unchanged
    assert Q.select_gate_starts([a, c], 1, False) == []
    assert Q.select_gate_starts([c, a], 0, False) == ["A"]


# ---- the ca46 reproduction (tick level) ------------------------------------------------------

def test_ca46_reproduction(opened, monkeypatch):
    """The campaign SPEC-0476-20261109-ca46 state of 2026-11-09 15:44: B's gate (SPEC-0490) is
    in flight; X (IDEA-1039) has waited exclusive since 14:54; CA (ca46) enters exclusive behind
    it; K is CLOSING with a close that re-gates. Before SPEC-0507, K's close started beside the
    pending exclusives (keeping the drain going) and CA waited with nothing on its ledger. Now:
    K yields, CA and X each get one note naming what they wait for, and the order is B, X, CA,
    then K."""
    cfg, base_id, _ctx = opened
    base = S.read_state(cfg, base_id)
    ids, started, events = {}, [], {k: threading.Event() for k in ("B", "X", "CA", "K")}

    def clone(name, state, **over):
        ids[name] = f"{base_id}-{name}"
        S.write_state(cfg, ids[name], dict(base, id=ids[name], state=state,
                                           counters=dict(base["counters"]), **over))

    def name_of(state):
        return next(k for k, v in ids.items() if v == state["id"])

    def fake_gate(ctx, state):
        name = name_of(state)
        started.append(name)
        events[name].wait(10)
        return Q.StepResult(state["id"], Q.Step("gate"), "GATING",
                            gate_run=_gate_run(1, ("test", True, True, False)))

    def fake_close(ctx, state):
        started.append(f"{name_of(state)}:close")
        events["K"].wait(10)
        return Q.StepResult(state["id"], Q.Step("close"), state["state"], error="faked")

    def finish(name):
        events[name].set()
        pool.flights[ids[name]].tasks[0][1].result(10)

    monkeypatch.setattr(Q, "_do_gate", fake_gate)
    monkeypatch.setattr(Q, "_do_prepare_close", fake_close)
    S.write_state(cfg, base_id, dict(base, state="PARKED", park={
        "reason": "x", "detail": "", "resume_to": "IMPLEMENTING"}))
    pool = Q.StepPool()
    try:
        clone("B", "GATING")
        Q.tick(cfg, pool=pool)
        assert until(lambda: started == ["B"])
        clone("X", "GATING", gate_exclusive={"entered_at": _ago(600)})
        clone("CA", "GATING", gate_exclusive={"entered_at": _ago(60)})
        clone("K", "CLOSING")
        for _ in range(3):                                   # the runner keeps ticking
            Q.tick(cfg, pool=pool)
        assert started == ["B"]                              # no close joins the drain
        ca_notes, x_notes = _notes(cfg, ids["CA"]), _notes(cfg, ids["X"])
        assert len(ca_notes) == 1 and len(x_notes) == 1      # once per marker, not per tick
        assert ids["B"] in ca_notes[0] and f"queued behind exclusive re-gate(s) {ids['X']}" \
            in ca_notes[0]
        assert ids["B"] in x_notes[0] and "queued behind" not in x_notes[0]
        assert any("close re-gate waits for exclusive" in t for t in _notes(cfg, ids["K"]))
        finish("B")
        Q.tick(cfg, pool=pool)                               # B drained: the oldest goes alone
        assert until(lambda: started == ["B", "X"])
        Q.tick(cfg, pool=pool)
        assert started == ["B", "X"]
        finish("X")
        Q.tick(cfg, pool=pool)
        assert until(lambda: started == ["B", "X", "CA"])
        Q.tick(cfg, pool=pool)
        assert started == ["B", "X", "CA"]                   # K still yields to the running CA
        finish("CA")
        Q.tick(cfg, pool=pool)
        assert until(lambda: started == ["B", "X", "CA", "K:close"])
    finally:
        for e in events.values():
            e.set()
        pool.shutdown()


# ---- expiry ----------------------------------------------------------------------------------

def _fly(pool, ctx, state, kind):
    from concurrent.futures import Future
    fl = Q._Flight(state, ctx)
    fl.tasks.append((Q.Step(kind), Future(), None))
    pool.flights[state["id"]] = fl


def test_expired_exclusive_starts_non_exclusively(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st.update(state="GATING", gate_exclusive={"entered_at": _ago(Q.EXCLUSIVE_WAIT_MAX_S + 60)})
    S.write_state(cfg, cid, st)
    n0 = len(S.read_ledger(cfg, cid))
    pool = Q.StepPool()
    _fly(pool, ctx, _state("GATING", id="B"), "gate")       # a normal gate still running
    busy = [S.read_state(cfg, cid), _gs("C")]
    assert Q._gate_starts(ctx, pool, busy, set(), {"B"}) == {cid, "C"}
    assert not Q.gate_exclusive(busy[0]) and not Q.gate_exclusive(S.read_state(cfg, cid))
    notes = _notes(cfg, cid, n0)
    assert len(notes) == 1 and "starting non-exclusively" in notes[0] and "B" in notes[0]
    assert f"(max {Q.EXCLUSIVE_WAIT_MAX_S}s)" in notes[0]


def test_no_expiry_beside_a_running_exclusive_gate(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st.update(state="GATING", gate_exclusive={"entered_at": _ago(Q.EXCLUSIVE_WAIT_MAX_S + 60)})
    S.write_state(cfg, cid, st)
    before = (S.read_state(cfg, cid)["gate_exclusive"], len(S.read_ledger(cfg, cid)))
    pool = Q.StepPool()
    _fly(pool, ctx, _state("GATING", id="B", gate_exclusive={"entered_at": _ago(10)}), "gate")
    assert Q._gate_starts(ctx, pool, [S.read_state(cfg, cid)], set(), {"B"}) == set()
    assert (S.read_state(cfg, cid)["gate_exclusive"], len(S.read_ledger(cfg, cid))) == before


def test_configured_bound_applies(opened):
    cfg, cid, ctx = opened
    cfg = dict(cfg, agentic={**(cfg.get("agentic") or {}), "gates": {"exclusive_wait_max_s": 30}})
    ctx.cfg = cfg
    st = S.read_state(cfg, cid)
    st.update(state="GATING", gate_exclusive={"entered_at": _ago(90)})
    S.write_state(cfg, cid, st)
    pool = Q.StepPool()
    _fly(pool, ctx, _state("GATING", id="B"), "gate")
    assert Q._gate_starts(ctx, pool, [S.read_state(cfg, cid)], set(), {"B"}) == {cid}
    assert "(max 30s)" in _notes(cfg, cid)[-1]


# ---- notes once ------------------------------------------------------------------------------

def test_deferral_note_once_per_marker_across_runner_restarts(opened, monkeypatch):
    cfg, cid, ctx = opened
    journaled = []
    monkeypatch.setattr(Q, "journal", lambda cfg_, kind, **kw: journaled.append(kind))
    st = S.read_state(cfg, cid)
    st.update(state="GATING", gate_exclusive={"entered_at": _ago(5)})
    S.write_state(cfg, cid, st)
    n0 = len(S.read_ledger(cfg, cid))
    pool = Q.StepPool()
    _fly(pool, ctx, _state("GATING", id="B"), "gate")
    for _ in range(3):
        Q._note_gate_deferred(ctx, pool, S.read_state(cfg, cid), [])
    Q._note_gate_deferred(ctx, Q.StepPool(), S.read_state(cfg, cid), [])   # a new runner
    notes = _notes(cfg, cid, n0)
    assert notes == ["exclusive re-gate waiting: 1 gate step(s) in flight (B)"]
    assert S.read_state(cfg, cid)["gate_exclusive"]["noted"] is True
    assert journaled.count("gate_timeout_deferred") == 2      # runner memory: once per runner
    # a fresh marker (a resume re-sets it) is noted again
    st = S.read_state(cfg, cid)
    st["gate_exclusive"] = {"entered_at": _ago(1)}
    S.write_state(cfg, cid, st)
    Q._note_gate_deferred(ctx, pool, S.read_state(cfg, cid), [])
    assert len(_notes(cfg, cid, n0)) == 2


def test_close_deferral_note_once(opened, monkeypatch):
    cfg, cid, ctx = opened
    journaled = []
    monkeypatch.setattr(Q, "journal", lambda cfg_, kind, **kw: journaled.append((kind, kw)))
    n0 = len(S.read_ledger(cfg, cid))
    pool = Q.StepPool()
    st = S.read_state(cfg, cid)
    for _ in range(3):
        Q._note_close_deferred(ctx, pool, st, ["X", "Y"])
    assert _notes(cfg, cid, n0) == ["close re-gate waits for exclusive re-gate(s) X, Y"]
    assert journaled == [("close_deferred_for_exclusive", {"campaign": cid, "exclusive": ["X", "Y"]})]


def test_exclusive_held_counts_pending_and_running(opened):
    _cfg, _cid, ctx = opened
    pool = Q.StepPool()
    assert Q._exclusive_held(ctx, pool, [_gs("A"), _state("CLOSING", id="K")]) == []
    assert Q._exclusive_held(ctx, pool, [_gs("A", _ago(1))]) == ["A"]
    _fly(pool, ctx, _state("GATING", id="R", gate_exclusive={"entered_at": _ago(1)}), "gate")
    assert Q._exclusive_held(ctx, pool, [_gs("A")]) == ["R"]
