"""SPEC-0309: the host-wide gate-slot pool — flock slots shared across processes, the waiter
marker, the stop path, `settings`, and `gates.run_all` holding one slot per run."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from nightwright.agentic import gate_slots as GS
from nightwright.agentic import events as E
from nightwright.agentic import gates as G
from nightwright.agentic import store as S

from test_agentic_gates import CID, _cfg, _gate, _state
from waits import join, until

# a child process that holds a slot until `release` exists
HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from nightwright.agentic import gate_slots as GS
    cfg = {"data_dir": sys.argv[1], "agentic": {"gates": {"slots": int(sys.argv[2])}}}
    cid, release = sys.argv[3], Path(sys.argv[4])
    with GS.acquire(cfg, cid, poll_s=0.05) as hold:
        Path(sys.argv[4] + "." + cid + ".in").write_text(str(hold.slot))
        while not release.exists():
            time.sleep(0.05)
""")


def _spawn(cfg, slots, cid, release):
    return subprocess.Popen([sys.executable, "-c", HOLDER, cfg["data_dir"], str(slots), cid,
                             str(release)])


def _in(release, cid):
    return Path(str(release) + "." + cid + ".in")


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"gates": {"slots": 2}}}


def test_settings_falls_back_to_two():
    for bad in (0, "x", True, 99, 1.5, None, -1, 17):
        assert GS.settings({"agentic": {"gates": {"slots": bad}}}) == {"slots": 2}
    assert GS.settings({}) == {"slots": 2}
    assert GS.settings({"agentic": {"gates": {"slots": 1}}}) == {"slots": 1}
    assert GS.settings({"agentic": {"gates": {"slots": 16}}}) == {"slots": 16}


def test_at_most_slots_hold_across_processes(cfg, tmp_path):
    release = tmp_path / "release"
    procs = [_spawn(cfg, 2, f"c{i}", release) for i in range(2)]
    try:
        assert until(lambda: _in(release, "c0").exists() and _in(release, "c1").exists())
        assert {_in(release, "c0").read_text(), _in(release, "c1").read_text()} == {"0", "1"}
        third = _spawn(cfg, 2, "c2", release)
        procs.append(third)
        assert until(lambda: len(GS.occupancy(cfg)["waiting"]) == 1)
        occ = GS.occupancy(cfg)
        assert occ["slots"] == 2 and len(occ["held"]) == 2
        assert occ["waiting"][0]["campaign"] == "c2"
        assert GS.waiting(cfg, "c2")["pid"] == third.pid
        assert not _in(release, "c2").exists()          # the third has not entered
        release.write_text("go")
        assert until(lambda: _in(release, "c2").exists())
        for p in procs:
            assert p.wait(60) == 0
    finally:
        release.write_text("go")
        for p in procs:
            p.kill()
    assert GS.occupancy(cfg)["held"] == [] and GS.occupancy(cfg)["waiting"] == []
    assert GS.waiting(cfg, "c2") is None


def test_sigkilled_holder_frees_its_slot(cfg, tmp_path):
    release = tmp_path / "release"
    p = _spawn(cfg, 1, "c0", release)
    try:
        assert until(lambda: _in(release, "c0").exists())
        assert [h["campaign"] for h in GS.occupancy(cfg)["held"]] == ["c0"]
        os.kill(p.pid, signal.SIGKILL)
        p.wait(60)
        assert until(lambda: GS.occupancy(cfg)["held"] == [])
        with GS.acquire({**cfg, "agentic": {"gates": {"slots": 1}}}, "c1", poll_s=0.05) as hold:
            assert hold.slot == 0 and not hold.interrupted
    finally:
        p.kill()


def test_waiter_returns_on_stop_without_a_slot_and_leaves_no_marker(cfg):
    one = {**cfg, "agentic": {"gates": {"slots": 1}}}
    stop = threading.Event()
    got = {}

    def waiter():
        with GS.acquire(one, "w", stop=stop, poll_s=0.05) as hold:
            got["hold"] = hold

    with GS.acquire(one, "holder", poll_s=0.05):
        t = threading.Thread(target=waiter)
        t.start()
        assert until(lambda: GS.waiting(one, "w") is not None)
        stop.set()
        join(t)
        assert not t.is_alive()
    assert got["hold"].interrupted and got["hold"].slot is None
    assert GS.waiting(one, "w") is None
    assert not (GS.slots_dir(one) / "waiting-w.json").exists()


def test_a_failing_on_wait_does_not_abort_the_wait(cfg, monkeypatch):
    one = {**cfg, "agentic": {"gates": {"slots": 1}}}
    monkeypatch.setattr(GS, "WAIT_EVENT_S", 0.0)
    calls = []

    def boom(occ, waited):
        calls.append(waited)
        raise RuntimeError("ledger write failed")

    got = {}

    def waiter():
        with GS.acquire(one, "w", poll_s=0.05, on_wait=boom) as hold:
            got["hold"] = hold

    with GS.acquire(one, "holder", poll_s=0.05):
        t = threading.Thread(target=waiter)
        t.start()
        assert until(lambda: calls)
        assert GS.waiting(one, "w") is not None          # still waiting after the callback raised
    join(t)
    assert len(calls) == 1
    assert got["hold"].slot == 0 and not got["hold"].interrupted
    assert GS.waiting(one, "w") is None


def test_holding_ignores_a_dead_pid_slot_file(cfg):
    d = GS.slots_dir(cfg)
    d.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (d / "slot-0.lock").write_text(json.dumps({"campaign": "x", "pid": dead.pid,
                                               "since": "2026-10-30T10:00:00-04:00",
                                               "waited_s": 2800.0}))
    assert GS.holding(cfg, "x") is None
    (d / "slot-0.lock").write_text(json.dumps({"campaign": "x", "pid": os.getpid(),
                                               "since": "2026-10-30T10:00:00-04:00",
                                               "waited_s": 2800.0}))
    assert GS.holding(cfg, "x")["waited_s"] == 2800.0


def test_dead_pid_marker_is_ignored(cfg):
    d = GS.slots_dir(cfg)
    d.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (d / "waiting-x.json").write_text(json.dumps({"campaign": "x", "pid": dead.pid,
                                                  "since": "2026-10-30T10:00:00-04:00"}))
    assert GS.waiting(cfg, "x") is None
    assert GS.occupancy(cfg)["waiting"] == []


def test_lowered_slots_files_drain_and_raised_slots_appear(cfg):
    two = cfg
    one = {**cfg, "agentic": {"gates": {"slots": 1}}}
    with GS.acquire(two, "a", poll_s=0.05) as a, GS.acquire(two, "b", poll_s=0.05) as b:
        assert {a.slot, b.slot} == {0, 1}
        occ = GS.occupancy(one)
        assert occ["slots"] == 1 and len(occ["held"]) == 2      # both still shown
    with GS.acquire(one, "c", poll_s=0.05) as c:
        assert c.slot == 0


# ---- pools (SPEC-0337) ----------------------------------------------------------------

def test_occupancy_without_workers_is_exactly_the_local_keys(cfg):
    assert GS.occupancy(cfg) == {"slots": 2, "held": [], "waiting": []}
    with GS.acquire(cfg, "a", poll_s=0.05) as hold:
        assert hold.pool is None
        occ = GS.occupancy(cfg)
        assert set(occ) == {"slots", "held", "waiting"}
        assert [(h["slot"], h["campaign"], h["pid"]) for h in occ["held"]] == [(0, "a", os.getpid())]
    # an empty registry changes nothing either
    from nightwright.agentic import workers as WK
    WK.path(cfg).write_text("")
    assert set(GS.occupancy(cfg)) == {"slots", "held", "waiting"}


def _register(cfg, *names, slots=1):
    from nightwright.agentic import workers as WK
    WK.update(cfg, lambda recs: recs.extend(
        {"name": n, "ssh": f"me@{n}", "slots": slots, "kinds": ["gate"], "enabled": True}
        for n in names))


def test_acquire_refuses_an_empty_pool_list(cfg):
    with pytest.raises(ValueError):
        with GS.acquire(cfg, "a", pools=[]):
            pass


def test_acquire_prefers_the_first_pool_then_falls_back_to_local(cfg):
    pools = [("box", 1), (None, 1)]
    with GS.acquire(cfg, "a", poll_s=0.05, pools=pools) as a:
        assert (a.pool, a.slot) == ("box", 0)
        assert (GS.slots_dir(cfg) / "w-box" / "slot-0.lock").exists()
        with GS.acquire(cfg, "b", poll_s=0.05, pools=pools) as b:      # remote full: local
            assert (b.pool, b.slot) == (None, 0)
    with GS.acquire(cfg, "c", poll_s=0.05, pools=pools) as c:          # both free again
        assert c.pool == "box"


def test_acquire_waits_for_whichever_pool_frees_first(cfg):
    pools = [("box", 1), (None, 1)]
    got = {}
    a_cm = GS.acquire(cfg, "a", poll_s=0.05, pools=pools)
    b_cm = GS.acquire(cfg, "b", poll_s=0.05, pools=pools)
    a = a_cm.__enter__()                                  # takes the box slot
    b = b_cm.__enter__()                                  # box is full -> local
    assert (a.pool, b.pool) == ("box", None)

    def waiter():
        with GS.acquire(cfg, "c", poll_s=0.05, pools=pools) as hold:
            got["pool"] = hold.pool
    t = threading.Thread(target=waiter)
    t.start()
    try:
        until(lambda: GS.waiting(cfg, "c") is not None)
        assert "pool" not in got
        a_cm.__exit__(None, None, None)                   # free only the box slot
        join(t)
        assert got["pool"] == "box"                       # the waiter takes whichever freed
    finally:
        b_cm.__exit__(None, None, None)
        join(t)
    assert GS.waiting(cfg, "c") is None


def test_acquire_rejects_a_bad_pool_name(cfg):
    for bad in ("../x", "A", "", "a/b"):
        with pytest.raises(ValueError):
            with GS.acquire(cfg, "a", pools=[(bad, 1)]):
                pass


def test_occupancy_and_holding_report_remote_holds(cfg):
    _register(cfg, "box", "idle", slots=2)
    with GS.acquire(cfg, "a", poll_s=0.05, pools=[("box", 2), (None, 2)]) as a:
        assert a.pool == "box"
        occ = GS.occupancy(cfg)
        assert occ["held"] == [] and occ["slots"] == 2        # local keys unchanged
        assert set(occ["workers"]) == {"box", "idle"}
        assert occ["workers"]["idle"] == {"slots": 2, "held": []}
        (h,) = occ["workers"]["box"]["held"]
        assert occ["workers"]["box"]["slots"] == 2
        assert (h["slot"], h["campaign"], h["pid"]) == (0, "a", os.getpid())
        hold = GS.holding(cfg, "a")
        assert hold["pool"] == "box" and hold["pid"] == os.getpid()
        from nightwright.agentic import workers as WK     # `rm` does not hide a held slot
        WK.update(cfg, lambda recs: recs.clear())
        assert GS.holding(cfg, "a")["pool"] == "box"
        assert "workers" not in GS.occupancy(cfg)
    assert GS.holding(cfg, "a") is None


def test_waiting_keeps_the_pool_list_it_started_with(cfg):
    """`rm`/`off` do not reach a waiter: it still gets a slot of the pool it was given."""
    from nightwright.agentic import workers as WK
    _register(cfg, "box")
    got = {}
    with GS.acquire(cfg, "a", poll_s=0.05, pools=[("box", 1)]):
        def waiter():
            with GS.acquire(cfg, "w", poll_s=0.05, pools=[("box", 1)]) as hold:
                got["pool"] = hold.pool
        t = threading.Thread(target=waiter)
        t.start()
        until(lambda: GS.waiting(cfg, "w") is not None)
        WK.update(cfg, lambda recs: recs.clear())
    join(t)
    assert got["pool"] == "box"


# ---- gates.run_all --------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["agentic"] = {"profiles": {}, "gates": {"slots": 1}}
    wt = tmp_path / "worktree"
    wt.mkdir()
    st = _state(wt)
    S.create_campaign(cfg, st)
    return cfg, st, wt


def test_run_all_without_a_wait_records_zero(world):
    cfg, st, _ = world
    run = G.run_all({"gates": [_gate("a", "true")]}, st, cfg, seq=1)
    assert run.ok and run.slot_wait_s == 0.0
    assert G.as_payload(run)["slot_wait_s"] == 0.0
    assert GS.occupancy(cfg)["held"] == []                  # released after the run


def test_run_all_holds_one_slot_across_every_gate(world, monkeypatch):
    cfg, st, _ = world
    during = []
    real = G.run_gate_with_retries

    def spy(*a, **kw):                  # what the pool shows as each gate is about to start
        during.append([h["campaign"] for h in GS.occupancy(cfg)["held"]])
        return real(*a, **kw)

    monkeypatch.setattr(G, "run_gate_with_retries", spy)
    run = G.run_all({"gates": [_gate("a", "true"), _gate("b", "true"), _gate("c", "true")]},
                    st, cfg, seq=1)
    assert run.ok and len(run.results) == 3
    assert during == [[CID]] * 3                        # one hold, there for every gate
    assert GS.occupancy(cfg)["held"] == []


def test_a_short_wait_appends_no_gate_waiting(world, monkeypatch):
    cfg, st, _ = world
    monkeypatch.setattr(GS, "POLL_S", 0.05)             # WAIT_EVENT_S stays at 30 s
    with GS.acquire(cfg, "other", poll_s=0.05):
        box = {}
        t = threading.Thread(target=lambda: box.update(
            run=G.run_all({"gates": [_gate("a", "true")]}, st, cfg, seq=1)))
        t.start()
        assert until(lambda: GS.waiting(cfg, CID) is not None)
    join(t)
    assert box["run"].ok and box["run"].slot_wait_s > 0
    assert not [e for e in S.read_ledger(cfg, CID) if e["type"] == "gate_waiting"]


def test_run_and_log_stores_slot_wait_s_on_the_gate_entry(world, monkeypatch):
    cfg, st, _ = world
    monkeypatch.setattr(GS, "POLL_S", 0.05)
    with GS.acquire(cfg, "other", poll_s=0.05):
        box = {}
        t = threading.Thread(target=lambda: box.update(
            run=G.run_and_log(cfg, st, {"gates": [_gate("a", "true")]})))
        t.start()
        assert until(lambda: GS.waiting(cfg, CID) is not None)
    join(t)
    entries = [e for e in S.read_ledger(cfg, CID) if e["type"] == "gate"]
    assert len(entries) == 1
    assert entries[0]["slot_wait_s"] == box["run"].slot_wait_s > 0


def test_run_all_waits_for_the_slot_and_counts_budget_from_the_hold(world, monkeypatch):
    cfg, st, _ = world
    monkeypatch.setattr(GS, "WAIT_EVENT_S", 0.05)
    monkeypatch.setattr(GS, "POLL_S", 0.05)
    budget = 600.0
    real_settings = E.settings
    monkeypatch.setattr(E, "settings", lambda c: {**real_settings(c), "gate_budget_s": budget})
    seen = {}
    real = G.run_gate_with_retries

    def spy(*a, **kw):
        seen["deadline"], seen["called"] = kw["deadline"], time.monotonic()
        return real(*a, **kw)

    monkeypatch.setattr(G, "run_gate_with_retries", spy)
    holder_release = threading.Event()
    held = threading.Event()

    def hold():
        with GS.acquire(cfg, "other", poll_s=0.05):
            held.set()
            holder_release.wait(60)

    h = threading.Thread(target=hold)
    h.start()
    assert held.wait(60)
    box = {}
    t = threading.Thread(target=lambda: box.update(
        run=G.run_all({"gates": [_gate("a", "true")]}, st, cfg, seq=1)))
    t.start()
    assert until(lambda: any(e["type"] == "gate_waiting" for e in S.read_ledger(cfg, CID)))
    started = time.monotonic()
    assert until(lambda: time.monotonic() - started >= 0.6)    # a wait clearly longer than the gate
    released = time.monotonic()
    holder_release.set()
    join(h)
    join(t)
    run = box["run"]
    assert run.ok and run.slot_wait_s >= 0.5
    assert run.duration_s < run.slot_wait_s             # gate time, not the wait
    # the deadline counts from the hold: it cannot be earlier than release + the budget
    assert seen["deadline"] >= released + budget
    assert seen["deadline"] <= seen["called"] + budget
    waits = [e for e in S.read_ledger(cfg, CID) if e["type"] == "gate_waiting"]
    assert len(waits) == 1
    assert waits[0]["slots"] == 1 and waits[0]["held"][0]["campaign"] == "other"
    assert G.as_payload(run)["slot_wait_s"] == run.slot_wait_s


def test_a_hold_records_the_wait_it_ended_in(world):
    cfg, st, _ = world
    got = {}

    def waiter():
        with GS.acquire(cfg, CID, poll_s=0.05) as hold:
            got["rec"] = GS.holding(cfg, CID)
            got["waited"] = hold.waited_s

    with GS.acquire(cfg, "other", poll_s=0.05):
        t = threading.Thread(target=waiter)
        t.start()
        assert until(lambda: GS.waiting(cfg, CID) is not None)
        t0 = time.monotonic()
        assert until(lambda: time.monotonic() - t0 >= 0.3)
    join(t)
    assert got["waited"] >= 0.3 and got["rec"]["waited_s"] == got["waited"]
    assert GS.waiting(cfg, CID) is None and GS.holding(cfg, CID) is None   # released


def test_a_wait_ends_on_the_sticky_stop_without_a_stop_event(world):
    cfg, st, wt = world
    box = {}
    marker = wt / "ran"
    with GS.acquire(cfg, "other", poll_s=0.05):
        t = threading.Thread(target=lambda: box.update(run=G.run_all(
            {"gates": [_gate("a", f"touch {marker}")]}, st, cfg, seq=1)))
        try:
            t.start()
            assert until(lambda: GS.waiting(cfg, CID) is not None)
            G.terminate_all()
            join(t)
            assert not t.is_alive()
        finally:
            G.reset()
    run = box["run"]
    assert run.interrupted and not run.ok and run.results == () and not marker.exists()
    assert GS.waiting(cfg, CID) is None


def test_a_marker_is_removed_only_by_its_writer(world, monkeypatch):
    cfg, st, _ = world
    stop = threading.Event()
    box = {}

    def waiter():
        with GS.acquire(cfg, "w", stop=stop, poll_s=0.05) as hold:
            box["hold"] = hold

    with GS.acquire(cfg, "other", poll_s=0.05):
        t = threading.Thread(target=waiter)
        t.start()
        assert until(lambda: GS.waiting(cfg, "w") is not None)
        marker = GS.slots_dir(cfg) / "waiting-w.json"
        marker.write_text(json.dumps({"campaign": "w", "pid": os.getpid() + 1,
                                      "since": "2026-10-30T10:00:00-04:00"}))   # another wait's
        stop.set()
        join(t)
    assert marker.exists()                                  # not ours: left to its writer


def test_run_all_stopped_in_the_wait_starts_no_gate(world):
    cfg, st, wt = world
    stop = threading.Event()
    box = {}
    marker = wt / "ran"
    with GS.acquire(cfg, "other", poll_s=0.05):
        t = threading.Thread(target=lambda: box.update(run=G.run_all(
            {"gates": [_gate("a", f"touch {marker}")]}, st, cfg, seq=1, stop=stop)))
        t.start()
        assert until(lambda: GS.waiting(cfg, CID) is not None)
        stop.set()
        join(t)
        assert not t.is_alive()
    run = box["run"]
    assert run.interrupted and run.results == () and not marker.exists()
    assert GS.waiting(cfg, CID) is None
