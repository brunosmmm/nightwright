"""SPEC-0309: the host-wide gate-slot pool — flock slots shared across processes, the waiter
marker, the stop path, `settings`, and `gates.run_all` holding one slot per run."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from wt.agentic import gate_slots as GS
from wt.agentic import gates as G
from wt.agentic import store as S

from test_agentic_gates import CID, _cfg, _gate, _state
from waits import join, until

# a child process that holds a slot until `release` exists
HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from wt.agentic import gate_slots as GS
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


def test_run_all_waits_for_the_slot_and_counts_budget_from_the_hold(world, monkeypatch):
    cfg, st, _ = world
    monkeypatch.setattr(GS, "WAIT_EVENT_S", 0.05)
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
    monkeypatch.setattr(GS, "POLL_S", 0.05)          # the waiter polls at the default
    t.start()
    assert until(lambda: any(e["type"] == "gate_waiting" for e in S.read_ledger(cfg, CID)))
    holder_release.set()
    join(h)
    join(t)
    run = box["run"]
    assert run.ok and run.slot_wait_s > 0
    assert run.duration_s < run.slot_wait_s + 5            # duration is gate time, not the wait
    waits = [e for e in S.read_ledger(cfg, CID) if e["type"] == "gate_waiting"]
    assert len(waits) == 1
    assert waits[0]["slots"] == 1 and waits[0]["held"][0]["campaign"] == "other"
    assert G.as_payload(run)["slot_wait_s"] == run.slot_wait_s


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
