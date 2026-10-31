"""SPEC-0171: singleton runner lease — flock is the exclusion, the TTL is advisory."""
import datetime as dt
import json
import os

import pytest

from wt.agentic import lease as L


def _cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
            "agentic": {"lease_ttl_s": 120, "profiles": {}}}


def test_acquire_release_and_status(tmp_path):
    cfg = _cfg(tmp_path)
    assert L.lease_status(cfg) == {"held": False, "pid": None, "host": None, "started_at": None,
                                   "heartbeat_at": None, "heartbeat_age_s": None,
                                   "ttl_s": 120, "stale": False, "draining": None,
                                   **{k: None for k in L.LOOP_KEYS}}
    lease = L.acquire(cfg, ["wt", "queue", "run"])
    st = L.lease_status(cfg)
    assert st["held"] and st["pid"] == os.getpid() and st["ttl_s"] == 120 and not st["stale"]
    assert L.read_lease(cfg)["argv"] == ["wt", "queue", "run"]
    lease.release()
    assert L.lease_status(cfg)["held"] is False
    assert L.read_lease(cfg) is None
    lease.release()  # idempotent


def test_second_acquire_names_holder(tmp_path):
    cfg = _cfg(tmp_path)
    with L.acquire(cfg):
        with pytest.raises(ValueError, match=rf"queue held by pid {os.getpid()} on .* \(heartbeat \d+s ago\)"):
            L.acquire(cfg)
    L.acquire(cfg).release()      # free again after the context exits


def test_stale_flag_follows_heartbeat_age(tmp_path):
    cfg = _cfg(tmp_path)
    t0 = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc)
    lease = L.acquire(cfg, now=t0)
    try:
        assert L.lease_status(cfg, now=t0 + dt.timedelta(seconds=60))["stale"] is False
        late = L.lease_status(cfg, now=t0 + dt.timedelta(seconds=121))
        assert late["stale"] is True and late["heartbeat_age_s"] == 121.0
        lease.heartbeat(now=t0 + dt.timedelta(seconds=121))
        assert L.lease_status(cfg, now=t0 + dt.timedelta(seconds=130))["stale"] is False
    finally:
        lease.release()


def test_ttl_from_config_and_override(tmp_path):
    cfg = _cfg(tmp_path)
    with L.acquire(cfg, ttl_s=5) as lease:
        assert lease.info["ttl_s"] == 5 and L.lease_status(cfg)["ttl_s"] == 5
    cfg["agentic"]["lease_ttl_s"] = 42
    with L.acquire(cfg):
        assert L.lease_status(cfg)["ttl_s"] == 42


def test_lease_file_without_lock_is_not_held(tmp_path):
    """A leftover lease.json from a crashed holder: the flock is gone, so it is not held."""
    cfg = _cfg(tmp_path)
    L.acquire(cfg).release()
    L.agentic_dir(cfg).mkdir(parents=True, exist_ok=True)
    L.lease_path(cfg).write_text('{"pid": 1, "host": "ghost", "heartbeat_at": "2026-01-01T00:00:00+00:00", "ttl_s": 120}')
    st = L.lease_status(cfg)
    assert st["held"] is False and st["pid"] is None and st["stale"] is False
    L.acquire(cfg).release()      # and a new holder can take it


def test_heartbeat_races_info_updates(tmp_path):
    """SPEC-0224: info changes and heartbeats serialise on the lease lock — no RuntimeError from
    a dict changing under json.dumps, no half-written file, no removed key written back.
    SPEC-0248: a fixed iteration count behind a barrier, and each thread must iterate while the
    main loop runs, so the race is exercised at any machine speed (no wall-clock deadline)."""
    import json
    import threading

    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["t"])
    errors, reads = [], []
    stop = threading.Event()
    start = threading.Barrier(3)
    counts = {"beat": 0, "read": 0}
    iterated = {k: threading.Event() for k in counts}

    def beat():
        start.wait(60)
        try:
            while not stop.is_set():
                lease.heartbeat()
                counts["beat"] += 1
                iterated["beat"].set()
        except BaseException as e:        # noqa: BLE001 - the test asserts nothing was raised
            errors.append(e)

    def read():
        start.wait(60)
        while not stop.is_set():
            try:
                reads.append(json.loads(L.lease_path(cfg).read_text()))
            except FileNotFoundError:
                pass
            except ValueError as e:
                errors.append(e)
            counts["read"] += 1
            iterated["read"].set()

    threads = [threading.Thread(target=beat), threading.Thread(target=read)]
    for t in threads:
        t.start()
    try:
        start.wait(60)
        for n in range(1, 501):
            if n == 500:                  # both threads are racing before the last pair
                for name, ev in iterated.items():
                    assert ev.wait(60), f"never saw: a {name} iteration"
            lease.update_info(draining={"reason": "code_changed", "since": "t",
                                        "waiting_on": [{"campaign": f"c{i}"} for i in range(n % 50)]})
            lease.pop_info("draining")
        lease.pop_info("draining")
    finally:
        stop.set()
        for t in threads:
            t.join(60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert counts["beat"] >= 1 and counts["read"] >= 1
    assert reads and all(isinstance(r, dict) for r in reads)
    lease.heartbeat()
    assert "draining" not in json.loads(L.lease_path(cfg).read_text())
    lease.release()


def test_lease_status_exposes_loop_state(tmp_path):
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["wt", "queue", "run"])
    st = L.lease_status(cfg)
    assert all(st[k] is None for k in (*L.LOOP_KEYS, "draining"))      # a legacy lease: no keys
    busy = {"kind": "close", "campaign": "c1", "since": "t", "budget_s": 1800}
    drain = {"reason": "code_changed", "since": "t", "waiting_on": []}
    lease.update_info(tick_at="t1", poll_interval_s=20, in_flight=["c1"], slot_freed_at="t0",
                      busy=busy, draining=drain)
    st = L.lease_status(cfg)
    assert (st["tick_at"], st["poll_interval_s"], st["in_flight"], st["slot_freed_at"]) \
        == ("t1", 20, ["c1"], "t0")
    assert st["busy"] == busy and st["draining"] == drain
    lease.release()
    st = L.lease_status(cfg)
    assert all(st[k] is None for k in (*L.LOOP_KEYS, "draining"))


def test_lease_status_exposes_progress_keys(tmp_path):
    cfg = _cfg(tmp_path)
    assert {"progress_at", "last_tick_s"} <= set(L.LOOP_KEYS)
    lease = L.acquire(cfg, ["wt", "queue", "run"])
    st = L.lease_status(cfg)
    assert st["progress_at"] is None and st["last_tick_s"] is None        # unwritten
    lease.update_info(progress_at="t2", last_tick_s=4.5)
    st = L.lease_status(cfg)
    assert (st["progress_at"], st["last_tick_s"]) == ("t2", 4.5)
    lease.release()
    st = L.lease_status(cfg)
    assert st["progress_at"] is None and st["last_tick_s"] is None


def test_handover_drops_busy(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["t"])
    lease.update_info(busy={"kind": "drain", "campaign": None, "since": "t", "budget_s": 1},
                      tick_at="t1", in_flight=["c1"])
    env = lease.handover()
    assert "busy" not in L.read_lease(cfg)                  # the old image already dropped it
    info = L.read_lease(cfg)                                # a crash in between left it behind
    info["busy"] = {"kind": "open", "campaign": "c1", "since": "t", "budget_s": 1}
    L._atomic_write(L.lease_path(cfg), json.dumps(info))
    monkeypatch.setenv(L.HANDOVER_ENV, env[L.HANDOVER_ENV])
    adopted = L.acquire(cfg, ["t"])
    try:
        assert "busy" not in adopted.info and "busy" not in L.read_lease(cfg)
        st = L.lease_status(cfg)
        assert st["tick_at"] == "t1" and st["in_flight"] == ["c1"] and st["busy"] is None
    finally:
        adopted.release()
