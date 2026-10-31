"""SPEC-0204: the lease is heartbeated while a long tick runs, so an alive runner never looks
stale (`lease_status`, `nw queue list`, the dashboard, conduct-status)."""
import threading
import time

from test_agentic_e2e import make_cfg, make_repo, script
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q


def test_long_tick_keeps_the_lease_fresh(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cfg["agentic"] = {**(cfg.get("agentic") or {}), "lease_ttl_s": 2}
    real_tick = Q.tick
    seen = []

    def slow_tick(*a, **k):
        t_end = time.monotonic() + 3.5                    # well past the 2s TTL
        while time.monotonic() < t_end:
            seen.append(L.lease_status(cfg))
            time.sleep(0.25)
        return real_tick(*a, **k)

    monkeypatch.setattr(Q, "tick", slow_tick)
    Q.run(cfg, once=True)
    assert seen and all(s["held"] for s in seen)
    assert not any(s["stale"] for s in seen), [s["heartbeat_age_s"] for s in seen]
    assert max(s["heartbeat_age_s"] for s in seen) < 2


def test_heartbeat_thread_stops_with_the_tick(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    lease = L.acquire(cfg, ["t"])
    try:
        before = threading.active_count()
        with Q._heartbeat_while(lease, interval_s=0.05):
            time.sleep(0.2)
        time.sleep(0.1)
        assert threading.active_count() == before
        assert not any(t.name == "nw-lease-heartbeat" for t in threading.enumerate())
    finally:
        lease.release()
