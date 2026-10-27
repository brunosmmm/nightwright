"""SPEC-0228: the watchdog gaps — `waiting_not_admitted`, `runner_journal_silent` (with the
loop state the runner publishes in its lease), and the drain info on `ready_not_admitted`.
Every test holds the real flock through `L.acquire` and sets lease fields through
`update_info`; `now` values are fixed offsets from T0, with no sleeps."""
import datetime as dt
import json

import pytest

from wt.agentic import events as E
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store
from test_agentic_events import _campaign, _cfg, _set_state

T0 = dt.datetime.now().astimezone().replace(microsecond=0)


def at(s):
    return T0 + dt.timedelta(seconds=s)


def iso(s):
    return at(s).isoformat(timespec="seconds")


@pytest.fixture
def held():
    """Acquire the real lease for `cfg` (huge ttl: the fixed `now` values run far ahead of the
    real heartbeat); released at teardown."""
    leases = []

    def make(cfg, **info):
        lease = L.acquire(cfg, ["test"], ttl_s=10 ** 6)
        if info:
            lease.update_info(**info)
        leases.append(lease)
        return lease

    yield make
    for lease in leases:
        lease.release()


def _waiting(cfg, cid, since=0, resume_to="IMPLEMENTING"):
    _campaign(cfg, cid, state="WAITING")
    _set_state(cfg, cid, waiting={"since": iso(since), "resume_to": resume_to})
    return cid


def _of(events, type_):
    return [e for e in events if e["type"] == type_]


A, B, C = ("SPEC-0901-20261025-aaaa", "SPEC-0902-20261025-bbbb", "SPEC-0903-20261025-cccc")


# ---- waiting_not_admitted -----------------------------------------------------------------

def test_waiting_not_admitted_after_grace(tmp_path, held):
    cfg = _cfg(tmp_path)
    held(cfg, slot_freed_at=iso(0), tick_at=iso(0), poll_interval_s=20)
    _campaign(cfg, A, state="IMPLEMENTING")
    _waiting(cfg, B, since=0)
    assert _of(E.sync(cfg, now=at(30)), "waiting_not_admitted") == []
    out = _of(E.sync(cfg, now=at(90)), "waiting_not_admitted")
    assert len(out) == 1 and out[0]["campaign"] == B
    d = out[0]["data"]
    assert d["waiting_for_s"] == 90 and d["slot_free_s"] == 90
    assert d["resume_to"] == "IMPLEMENTING" and d["active"] == 1 and d["slots"] == 2
    assert d["runner_pid"] and d["drain"] is None
    assert _of(E.sync(cfg, now=at(120)), "waiting_not_admitted") == []        # once per wait
    # resumed and waited again: a new `waiting.since` is a new episode
    _set_state(cfg, B, waiting={"since": iso(100), "resume_to": "GATING"})
    assert _of(E.sync(cfg, now=at(150)), "waiting_not_admitted") == []        # 50 s into it
    again = _of(E.sync(cfg, now=at(200)), "waiting_not_admitted")
    assert len(again) == 1 and again[0]["data"]["resume_to"] == "GATING"
    assert E.ping_digest(cfg, at(200))["counts"]["waiting_not_admitted"] == 1


def test_waiting_not_admitted_quiet_at_exactly_the_grace(tmp_path, held):
    cfg = _cfg(tmp_path)
    held(cfg, slot_freed_at=iso(0), tick_at=iso(0), poll_interval_s=20)
    _campaign(cfg, A, state="IMPLEMENTING")
    _waiting(cfg, B, since=0)
    grace = E.settings(cfg)["waiting_grace_s"]
    assert _of(E.sync(cfg, now=at(grace)), "waiting_not_admitted") == []
    assert len(_of(E.sync(cfg, now=at(grace + 1)), "waiting_not_admitted")) == 1


def test_waiting_not_admitted_counts_in_flight(tmp_path, held):
    cfg = _cfg(tmp_path)
    held(cfg, slot_freed_at=iso(0), in_flight=[C])        # C parked, its run still in flight
    _campaign(cfg, A, state="IMPLEMENTING")
    _campaign(cfg, C, state="PARKED")
    _waiting(cfg, B, since=0)
    assert E.waiting_not_admitted_items(cfg, at(300), E.settings(cfg)) == []
    assert E.ping_digest(cfg, at(300))["waiting_not_admitted"] == []


def test_ready_not_admitted_counts_in_flight(tmp_path, held):
    cfg = _cfg(tmp_path)
    lease = held(cfg, in_flight=[C])
    _campaign(cfg, A, state="IMPLEMENTING")
    _campaign(cfg, C, state="PARKED")
    _campaign(cfg, B, state="QUEUED")
    _set_state(cfg, B, backlog={"ready": True, "blocked_by": [], "since": iso(0)})
    assert E.ready_not_admitted_items(cfg, at(300), E.settings(cfg)) == []
    lease.update_info(in_flight=[])
    (row,) = E.ready_not_admitted_items(cfg, at(300), E.settings(cfg))
    assert row["active"] == 1 and row["campaign"] == B


def test_ready_not_admitted_waits_for_slot_age(tmp_path, held):
    """SPEC-0268: a long-ready item alarms only once a slot has also been free past the grace."""
    cfg = _cfg(tmp_path)
    lease = held(cfg, slot_freed_at=iso(295))
    _campaign(cfg, B, state="QUEUED")
    _set_state(cfg, B, backlog={"ready": True, "blocked_by": [], "since": iso(-700)})
    assert E.ready_not_admitted_items(cfg, at(300), E.settings(cfg)) == []     # free for 5 s
    lease.update_info(slot_freed_at=iso(180))
    (row,) = E.ready_not_admitted_items(cfg, at(300), E.settings(cfg))
    assert row["slot_free_s"] == 120 and row["ready_for_s"] == 1000


def test_ready_not_admitted_without_lease_uses_ready_age(tmp_path):
    cfg = _cfg(tmp_path)
    _campaign(cfg, B, state="QUEUED")
    _set_state(cfg, B, backlog={"ready": True, "blocked_by": [], "since": iso(0)})
    (row,) = E.ready_not_admitted_items(cfg, at(300), E.settings(cfg))
    assert row["campaign"] == B and row["slot_free_s"] is None and row["runner_pid"] is None


def test_waiting_not_admitted_quiet_when_full_or_park_requested(tmp_path, held):
    cfg = _cfg(tmp_path)
    held(cfg, slot_freed_at=iso(0))
    _campaign(cfg, A, state="IMPLEMENTING")
    _campaign(cfg, C, state="GATING")                      # two active: both slots taken
    _waiting(cfg, B, since=0)
    assert E.waiting_not_admitted_items(cfg, at(300), E.settings(cfg)) == []
    _set_state(cfg, C, state="MERGED")                     # a slot is free again...
    assert len(E.waiting_not_admitted_items(cfg, at(300), E.settings(cfg))) == 1
    _set_state(cfg, B, park_requested={"reason": "human"})     # ...but a park was requested
    assert E.waiting_not_admitted_items(cfg, at(300), E.settings(cfg)) == []


def test_waiting_not_admitted_waits_for_slot_age(tmp_path, held):
    cfg = _cfg(tmp_path)
    lease = held(cfg)                                      # a held lease without slot_freed_at
    _campaign(cfg, A, state="MERGED")
    _waiting(cfg, B, since=0)
    sett = E.settings(cfg)
    at_hour = 3600
    assert E.waiting_not_admitted_items(cfg, at(at_hour), sett) == []
    lease.update_info(slot_freed_at=iso(at_hour - 10))    # a campaign merged 10 s ago
    assert E.waiting_not_admitted_items(cfg, at(at_hour), sett) == []
    (row,) = E.waiting_not_admitted_items(cfg, at(at_hour + 80), sett)    # 90 s after the merge
    assert row["slot_free_s"] == 90 and row["waiting_for_s"] == at_hour + 80


def test_waiting_not_admitted_skipped_without_held_lease(tmp_path):
    cfg = _cfg(tmp_path)
    _waiting(cfg, B, since=0)
    assert E.waiting_not_admitted_items(cfg, at(300), E.settings(cfg)) == []


# ---- runner_journal_silent ----------------------------------------------------------------

def _journal(cfg, *recs):
    path = Q.runner_journal_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for rec in recs:
            f.write((rec if isinstance(rec, str) else json.dumps(rec)) + "\n")


def test_runner_journal_silent_once_per_episode(tmp_path, held):
    cfg = _cfg(tmp_path)
    lease = held(cfg, poll_interval_s=20, tick_at=iso(0), slot_freed_at=iso(0))
    assert _of(E.sync(cfg, now=at(50)), "runner_journal_silent") == []
    out = _of(E.sync(cfg, now=at(70)), "runner_journal_silent")
    assert len(out) == 1
    d = out[0]["data"]
    assert d["pid"] == lease.info["pid"] and d["silent_s"] == 70 and d["interval_s"] == 20
    assert d["last_tick_at"] == iso(0) and d["last_journal_ts"] is None
    assert d["draining"] is None and d["busy"] is None and d["heartbeat_age_s"] is not None
    assert _of(E.sync(cfg, now=at(100)), "runner_journal_silent") == []        # same episode
    lease.update_info(tick_at=iso(110))                                      # the loop ticked
    assert _of(E.sync(cfg, now=at(120)), "runner_journal_silent") == []
    again = _of(E.sync(cfg, now=at(200)), "runner_journal_silent")
    assert len(again) == 1 and again[0]["data"]["last_tick_at"] == iso(110)


def test_runner_journal_silent_rearms_on_a_new_holder_record(tmp_path, held):
    cfg = _cfg(tmp_path)
    lease = held(cfg, poll_interval_s=20, tick_at=iso(0))
    assert len(_of(E.sync(cfg, now=at(70)), "runner_journal_silent")) == 1
    _journal(cfg, {"ts": iso(100), "kind": "tick", "pid": lease.info["pid"]})
    assert _of(E.sync(cfg, now=at(110)), "runner_journal_silent") == []       # cleared
    again = _of(E.sync(cfg, now=at(200)), "runner_journal_silent")
    assert len(again) == 1 and again[0]["data"]["last_journal_ts"] == iso(100)


def test_runner_journal_silent_skips_free_stale_or_legacy_lease(tmp_path, held):
    cfg = _cfg(tmp_path)
    sett = E.settings(cfg)
    assert E.runner_silence(cfg, at(500), sett) is None            # no lease at all
    lease = held(cfg, poll_interval_s=20)                          # legacy: no tick_at
    assert E.runner_silence(cfg, at(500), sett) is None
    lease.update_info(tick_at=iso(0))
    assert E.runner_silence(cfg, at(500), sett)["silent_s"] == 500
    lease.update_info(ttl_s=120, heartbeat_at=iso(-1000))                # stale: lease_stale covers it
    assert L.lease_status(cfg, now=at(500))["stale"] is True
    assert E.runner_silence(cfg, at(500), sett) is None


def test_runner_journal_silent_suppressed_during_close(tmp_path, held):
    cfg = _cfg(tmp_path)
    sett = E.settings(cfg)
    lease = held(cfg, poll_interval_s=20, tick_at=iso(0),
                 busy={"kind": "close", "campaign": A, "since": iso(0), "budget_s": 1800})
    lease.update_info(busy={"kind": "close", "campaign": A, "since": iso(300), "budget_s": 1800})
    assert E.runner_silence(cfg, at(600), sett) is None            # 5 min into a close
    lease.update_info(busy={"kind": "close", "campaign": A, "since": iso(-2100 + 600),
                            "budget_s": 1800})
    d = E.runner_silence(cfg, at(600), sett)                      # 35 min into it: over budget
    assert d["busy"]["kind"] == "close" and d["busy"]["campaign"] == A
    (ev,) = _of(E.sync(cfg, now=at(600)), "runner_journal_silent")
    assert ev["data"]["busy"]["kind"] == "close"


def test_journal_record_counts_as_activity(tmp_path, held):
    cfg = _cfg(tmp_path)
    sett = E.settings(cfg)
    lease = held(cfg, poll_interval_s=20, tick_at=iso(0))
    pid = lease.info["pid"]
    _journal(cfg, {"ts": iso(100), "kind": "tick", "pid": pid})
    assert E.runner_silence(cfg, at(120), sett) is None            # fresh holder record
    _journal(cfg, {"ts": iso(300), "kind": "tick", "pid": pid + 1})       # another pid
    d = E.runner_silence(cfg, at(200), sett)
    assert d["silent_s"] == 100 and d["last_journal_ts"] == iso(100)
    (ev,) = _of(E.sync(cfg, now=at(200)), "runner_journal_silent")
    assert ev["data"]["last_tick_at"] == iso(0)
    store_none = _cfg(tmp_path / "other")
    held(store_none, poll_interval_s=20, tick_at=iso(0))
    _journal(store_none, {"ts": iso(100), "kind": "tick", "pid": -1})
    d = E.runner_silence(store_none, at(200), sett)               # no holder record: tick_at alone
    assert d["last_journal_ts"] is None and d["silent_s"] == 200


def test_journal_tail_for_pid_bounded(tmp_path):
    cfg = _cfg(tmp_path)
    assert Q.journal_tail_for_pid(cfg, 7) is None                  # no journal
    pad = {"ts": iso(0), "kind": "tick", "pid": 99, "pad": "x" * 1000}
    _journal(cfg, {"ts": iso(1), "kind": "old", "pid": 7})
    _journal(cfg, *[pad] * 400)                                   # > 256 KiB after it
    assert Q.journal_tail_for_pid(cfg, 7) is None                  # the holder record is past the window
    _journal(cfg, {"ts": iso(2), "kind": "mid", "pid": 7}, "garbage {", {"ts": iso(3), "kind": "x",
                                                                         "pid": 8})
    _journal(cfg, {"ts": iso(4), "kind": "new", "pid": 7}, "{partial")
    assert Q.journal_tail_for_pid(cfg, 7)["kind"] == "new"
    assert Q.journal_tail_for_pid(cfg, 8)["kind"] == "x"
    assert Q.journal_tail_for_pid(cfg, 6) is None


# ---- drain info ---------------------------------------------------------------------------

def test_drain_info_on_rows(tmp_path, held):
    cfg = _cfg(tmp_path)
    lease = held(cfg, poll_interval_s=20, tick_at=iso(0), slot_freed_at=iso(0))
    _campaign(cfg, A, state="IMPLEMENTING")
    _waiting(cfg, B, since=0)
    _campaign(cfg, C, state="QUEUED")
    _set_state(cfg, C, backlog={"ready": True, "blocked_by": [], "since": iso(0)})
    sett = E.settings(cfg)

    def rows():
        return (E.ready_not_admitted_items(cfg, at(300), sett)[0]["drain"],
                E.waiting_not_admitted_items(cfg, at(300), sett)[0]["drain"],
                E.runner_silence(cfg, at(300), sett)["draining"])

    assert rows() == (None, None, None)
    waiting_on = [{"campaign": A, "kind": "implement", "run_ids": ["001"], "timeout_s": 900}]
    lease.update_info(draining={"reason": "code_changed", "since": iso(100),
                                "waiting_on": waiting_on})
    for drain in rows():
        assert drain == {"reason": "code_changed", "since": iso(100), "draining_s": 200,
                         "waiting_on": waiting_on}
    assert E.ready_not_admitted_items(cfg, at(300), sett)[0]["runner_draining"] is True


def test_silence_threshold_uses_the_lease_interval_then_cfg(tmp_path, held):
    cfg = _cfg(tmp_path)                                   # no agentic.poll_interval_s: default 20
    lease = held(cfg, poll_interval_s=5, tick_at=iso(0))
    sett = E.settings(cfg)
    assert E.runner_silence(cfg, at(14), sett) is None     # 3 x 5 = 15
    assert E.runner_silence(cfg, at(16), sett)["interval_s"] == 5
    lease.pop_info("poll_interval_s")                      # lease lacks the key: cfg interval
    cfg["agentic"]["poll_interval_s"] = 10
    assert E.runner_silence(cfg, at(29), sett) is None
    assert E.runner_silence(cfg, at(31), sett)["interval_s"] == 10


def test_active_counts_in_flight_ids_that_are_not_active_campaigns(tmp_path, held):
    cfg = _cfg(tmp_path)
    cfg["agentic"]["profiles"] = {"balanced": {"slots": 3}}
    held(cfg, slot_freed_at=iso(0), in_flight=[C])         # C parked, its run still in flight
    _campaign(cfg, A, state="IMPLEMENTING")
    _campaign(cfg, C, state="PARKED")
    _waiting(cfg, B, since=0)
    (row,) = E.waiting_not_admitted_items(cfg, at(300), E.settings(cfg))
    assert row["active"] == 2 and row["slots"] == 3
    _set_state(cfg, B, state="QUEUED", waiting=None)
    _set_state(cfg, B, backlog={"ready": True, "blocked_by": [], "since": iso(0)})
    (row,) = E.ready_not_admitted_items(cfg, at(300), E.settings(cfg))
    assert row["active"] == 2 and row["campaign"] == B
