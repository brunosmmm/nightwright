"""SPEC-0212: the periodic conductor ping, its digest, and the long-item watchdog conditions,
all with an injected clock."""
import datetime as dt

import pytest

from wt.agentic import events as E
from wt.agentic import store
from test_agentic_events import _cfg, _campaign, _ev, _set_state

T0 = dt.datetime(2026, 10, 25, 12, 0, 0).astimezone()


def at(s):
    return T0 + dt.timedelta(seconds=s)


def iso(s):
    return at(s).isoformat(timespec="seconds")


def _queued(cfg, cid, *, ready, since, blocked_by=()):
    _campaign(cfg, cid, state="QUEUED")
    _set_state(cfg, cid, backlog={"ready": ready, "blocked_by": list(blocked_by),
                                  "since": iso(since)}, created_at=iso(since))


def _sync(cfg, **kw):
    """sync() minus `runner_dead` (these fixtures have queued work and no runner)."""
    return [e for e in E.sync(cfg, **kw) if e["type"] != "runner_dead"]


def _pings(cfg):
    return E.read_since(cfg, 0, types=["conductor_ping"])


def _enter(cfg, cid, state, ts_s):
    """Ledger transition into `state` at T0+ts_s (the ledger stamps real time; patch it)."""
    rec = _ev(cfg, cid, "transition", {"from": "OPENING", "to": state, "reason": "t"})
    path = store._ledger_path(cfg, cid)
    lines = path.read_text().splitlines()
    import json
    r = json.loads(lines[-1])
    r["ts"] = iso(ts_s)
    lines[-1] = json.dumps(r)
    path.write_text("\n".join(lines) + "\n")
    _set_state(cfg, cid, state=state)
    return rec


def test_ping_seeds_then_fires_at_interval_with_digest(tmp_path):
    cfg = _cfg(tmp_path)
    _queued(cfg, "SPEC-0901-20261025-aaaa", ready=False, since=0,
            blocked_by=["waiting on SPEC-0100 (draft)"])
    assert _sync(cfg, now=at(0), ping=True) == []          # seeds the clock
    assert _sync(cfg, now=at(599), ping=True) == []
    out = [e for e in E.sync(cfg, now=at(600), ping=True) if e["type"] == "conductor_ping"]
    assert len(out) == 1
    d = out[0]["data"]
    assert d["blocked"] == []                               # 600s < blocked_after_s
    assert d["counts"]["blocked"] == 1 and d["counts"]["queued"] == 1
    out = [e for e in E.sync(cfg, now=at(1300), ping=True) if e["type"] == "conductor_ping"]
    assert out[0]["data"]["blocked"][0]["blocked_by"] == ["waiting on SPEC-0100 (draft)"]
    assert out[0]["data"]["blocked"][0]["blocked_for_s"] == 1300


def test_no_duplicate_across_syncs_followers_and_non_followers(tmp_path):
    cfg = _cfg(tmp_path)
    E.sync(cfg, now=at(0), ping=True)
    E.sync(cfg, now=at(700), ping=True)
    E.sync(cfg, now=at(701), ping=True)                     # a second follower, same interval
    E.sync(cfg, now=at(1500))                               # a non-follower never pings
    assert len(_pings(cfg)) == 1


def test_restart_resumes_interval_and_never_bursts(tmp_path):
    cfg = _cfg(tmp_path)
    E.sync(cfg, now=at(0), ping=True)
    E.sync(cfg, now=at(600), ping=True)
    # follower restarts shortly after: interval continues from the persisted last ping
    E.sync(cfg, now=at(650), ping=True)
    assert len(_pings(cfg)) == 1
    # long downtime: exactly one ping on return, not one per missed interval
    E.sync(cfg, now=at(10000), ping=True)
    E.sync(cfg, now=at(10001), ping=True)
    assert len(_pings(cfg)) == 2


def test_interval_config_and_disable(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["agentic"]["events"] = {"ping_interval_s": 60}
    E.sync(cfg, now=at(0), ping=True)
    E.sync(cfg, now=at(61), ping=True)
    assert len(_pings(cfg)) == 1
    cfg["agentic"]["events"] = {"ping_interval_s": 0}
    E.sync(cfg, now=at(1000), ping=True)
    assert len(_pings(cfg)) == 1


def test_blocked_threshold_and_watchdog_once(tmp_path):
    cfg = _cfg(tmp_path)
    cid = "SPEC-0902-20261025-aaaa"
    _queued(cfg, cid, ready=False, since=0, blocked_by=["x"])
    assert _sync(cfg, now=at(1200)) == []                  # not strictly longer yet
    out = _sync(cfg, now=at(1201))
    assert [e["type"] for e in out] == ["backlog_blocked_long"]
    assert out[0]["campaign"] == cid and out[0]["data"]["blocked_by"] == ["x"]
    assert _sync(cfg, now=at(1500)) == []                  # once per condition


def test_stuck_threshold_and_in_budget_exclusion(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0903-20261025-aaaa", state="IMPLEMENTING")
    _enter(cfg, cid, "IMPLEMENTING", 0)
    sett = E.settings(cfg)
    assert E.stuck_items(cfg, at(900), sett) == []
    assert [s["campaign"] for s in E.stuck_items(cfg, at(901), sett)] == [cid]
    # an in-flight run within its budget excludes the state
    _ev(cfg, cid, "run.start", {"persona": "implementer", "run_id": "001", "timeout_s": 3000})
    rpath = store._ledger_path(cfg, cid)
    import json
    lines = rpath.read_text().splitlines()
    r = json.loads(lines[-1]); r["ts"] = iso(100); lines[-1] = json.dumps(r)
    rpath.write_text("\n".join(lines) + "\n")
    assert E.stuck_items(cfg, at(2000), sett) == []
    # SPEC-0270: an open run never makes the state stuck, even overdue (`run_overdue` owns it)
    assert E.stuck_items(cfg, at(100 + 3000 + 61), sett) == []


def test_gating_in_process_gate_within_budget_is_not_stuck(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0904-20261025-aaaa", state="GATING")
    _enter(cfg, cid, "GATING", 0)
    sett = E.settings(cfg)
    assert E.stuck_items(cfg, at(1700), sett) == []
    assert E.stuck_items(cfg, at(1801), sett)


def test_state_stuck_fires_once_per_campaign_state(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0905-20261025-aaaa", state="IMPLEMENTING")
    _enter(cfg, cid, "IMPLEMENTING", 0)
    out = [e for e in E.sync(cfg, now=at(1000)) if e["type"] == "state_stuck"]
    assert len(out) == 1 and out[0]["data"]["state"] == "IMPLEMENTING"
    assert [e for e in E.sync(cfg, now=at(1100)) if e["type"] == "state_stuck"] == []


def test_parked_and_waiting_in_digest_with_decision_flag(tmp_path):
    cfg = _cfg(tmp_path)
    a = _campaign(cfg, "SPEC-0906-20261025-aaaa", state="PARKED")
    b = _campaign(cfg, "SPEC-0907-20261025-aaaa", state="PARKED")
    w = _campaign(cfg, "SPEC-0908-20261025-aaaa", state="WAITING")
    for cid in (a, b):
        park = {"reason": "cost_cap", "detail": "d", "resume_to": "IMPLEMENTING",
                "since": iso(0)}
        _ev(cfg, cid, "park", park)
        _set_state(cfg, cid, park=park)
    _ev(cfg, b, "note", {"text": "conductor: asked steward"})
    _set_state(cfg, w, waiting={"resume_to": "GATING", "since": iso(0), "reason": "r"})
    d = E.ping_digest(cfg, at(300))
    by = {p["campaign"]: p for p in d["parked"]}
    assert by[a]["decided"] is False and by[b]["decided"] is True
    assert by[a]["parked_for_s"] == 300 and by[a]["reason"] == "cost_cap"
    assert d["waiting"] == [{"campaign": w, "spec_id": "SPEC-0908", "waiting_for_s": 300,
                             "resume_to": "GATING"}]
    assert d["counts"]["undecided_parks"] == 1 and d["counts"]["waiting"] == 1


def test_ready_not_admitted_after_grace_with_free_slot(tmp_path):
    cfg = _cfg(tmp_path)
    cid = "SPEC-0909-20261025-aaaa"
    _queued(cfg, cid, ready=True, since=0)
    assert _sync(cfg, now=at(60)) == []
    out = [e for e in E.sync(cfg, now=at(61)) if e["type"] == "ready_not_admitted"]
    assert len(out) == 1
    d = out[0]["data"]
    assert d["active"] == 0 and d["slots"] >= 1 and d["runner_pid"] is None
    assert d["runner_busy"] is False
    assert [e for e in E.sync(cfg, now=at(200)) if e["type"] == "ready_not_admitted"] == []
    assert E.ping_digest(cfg, at(200))["ready_not_admitted"][0]["campaign"] == cid


def test_ready_not_admitted_silent_when_slots_full(tmp_path):
    cfg = _cfg(tmp_path)
    from wt.agentic.queue import slots_for
    for i in range(slots_for(cfg)):
        c = _campaign(cfg, f"SPEC-09{20 + i}-20261025-aaaa", state="IMPLEMENTING")
        _set_state(cfg, c, updated_at=iso(0))
    _queued(cfg, "SPEC-0910-20261025-aaaa", ready=True, since=0)
    assert E.ready_not_admitted_items(cfg, at(500), E.settings(cfg)) == []


def test_new_types_validate_and_summary_line(tmp_path):
    cfg = _cfg(tmp_path)
    for t, data in [("conductor_ping", {"blocked": []}), ("backlog_blocked_long", {}),
                    ("state_stuck", {"state": "x"}), ("ready_not_admitted", {})]:
        with pytest.raises(ValueError, match="missing data keys"):
            E.append(cfg, t, data=data)
    E.sync(cfg, now=at(0), ping=True)
    E.sync(cfg, now=at(600), ping=True)
    line = E.summary_line(_pings(cfg)[0])
    assert "\n" not in line and line.startswith("#1 conductor_ping blocked=0 parked=0")


def test_ping_digest_lists_waiting_and_runner(tmp_path):
    from wt.agentic import lease as L
    from test_agentic_watchdog_gaps import _waiting
    cfg = _cfg(tmp_path)
    real = dt.datetime.now().astimezone().replace(microsecond=0)
    lease = L.acquire(cfg, ["test"], ttl_s=10 ** 6)
    try:
        lease.update_info(slot_freed_at=real.isoformat(), tick_at=real.isoformat(),
                          poll_interval_s=20)
        _waiting(cfg, "SPEC-0950-20261025-aaaa", since=-3600)
        now = real + dt.timedelta(seconds=300)
        d = E.ping_digest(cfg, now)
        assert [w["campaign"] for w in d["waiting_not_admitted"]] == ["SPEC-0950-20261025-aaaa"]
        assert d["counts"]["waiting_not_admitted"] == 1
        assert d["runner"] == E.runner_silence(cfg, now, E.settings(cfg))
        assert d["runner"]["silent_s"] == 300
        assert E.ping_digest(cfg, real)["runner"] is None
    finally:
        lease.release()


def test_old_ping_event_validates(tmp_path):
    cfg = _cfg(tmp_path)
    old = {"blocked": [], "parked": [], "stuck": [], "waiting": [], "ready_not_admitted": [],
           "counts": {}}
    E.validate("conductor_ping", old)               # no waiting_not_admitted / runner keys
    assert E.append(cfg, "conductor_ping", data=old)["type"] == "conductor_ping"


def _ev_at(cfg, cid, etype, ts_s, payload=None):
    """Append a ledger event and stamp it at T0+ts_s."""
    import json
    _ev(cfg, cid, etype, payload or {})
    path = store._ledger_path(cfg, cid)
    lines = path.read_text().splitlines()
    r = json.loads(lines[-1]); r["ts"] = iso(ts_s); lines[-1] = json.dumps(r)
    path.write_text("\n".join(lines) + "\n")


def _run(cfg, cid, start_s, end_s=None, timeout_s=1800):
    _ev_at(cfg, cid, "run.start", start_s,
           {"persona": "implementer", "run_id": "001", "timeout_s": timeout_s})
    if end_s is not None:
        _ev_at(cfg, cid, "run.end", end_s, {"persona": "implementer", "run_id": "001"})


def test_stuck_ignores_persona_state_right_after_run_end(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0905-20261025-aaaa", state="IMPLEMENTING")
    _enter(cfg, cid, "IMPLEMENTING", 0)
    _run(cfg, cid, 10, end_s=1995)
    sett = E.settings(cfg)
    assert E.stuck_items(cfg, at(2000), sett) == []
    rows = E.stuck_items(cfg, at(2995), sett)
    assert [r["campaign"] for r in rows] == [cid]
    assert rows[0]["stuck_s"] == 1000 and rows[0]["in_state_s"] == 2995


def test_stuck_skips_open_run_even_when_overdue(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0906-20261025-aaaa", state="IMPLEMENTING")
    _enter(cfg, cid, "IMPLEMENTING", 0)
    _run(cfg, cid, 0, timeout_s=1800)
    sett = E.settings(cfg)
    assert E.stuck_items(cfg, at(3000), sett) == []
    keys = [k for k, *_ in E.watchdog_checks(cfg, now=at(3000))]
    assert any(k.startswith("run_overdue") for k in keys)
    assert not any(k.startswith("state_stuck") for k in keys)


def test_stuck_closing_measured_from_latest_gate_event(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0907-20261025-aaaa", state="CLOSING")
    _enter(cfg, cid, "CLOSING", 0)
    _ev_at(cfg, cid, "gate", 2200)
    sett = E.settings(cfg)
    assert E.stuck_items(cfg, at(2500), sett) == []          # entered 2500s ago, gate 300s ago
    assert E.stuck_items(cfg, at(4200), sett)                # gate 2000s old
    _set_state(cfg, cid, closing={"queued_behind": "SPEC-0001"})
    assert E.stuck_items(cfg, at(4200), sett) == []          # train waiter


def test_stuck_persona_state_without_runs_uses_state_age(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0908-20261025-aaaa", state="REVIEWING")
    _enter(cfg, cid, "REVIEWING", 0)
    rows = E.stuck_items(cfg, at(1000), E.settings(cfg))
    assert rows[0]["stuck_s"] == 1000 and rows[0]["in_state_s"] == 1000
