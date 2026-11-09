"""SPEC-0204: the lease is heartbeated while a long tick runs, so an alive runner never looks
stale (`lease_status`, `nw queue list`, the dashboard, conduct-status)."""
import threading
import time

from test_agentic_e2e import make_cfg, make_repo, script
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q
import pytest

pytestmark = pytest.mark.fast


@pytest.mark.integration
def test_long_tick_keeps_the_lease_fresh(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    # The thread beats every max(1, ttl/4) s and `heartbeat_at` is truncated to whole seconds,
    # so a fresh lease can read about interval + 1 s old; a 5 s TTL leaves room for a loaded
    # host while the tick still runs 1.5x past it.
    ttl = 5
    cfg["agentic"] = {**(cfg.get("agentic") or {}), "lease_ttl_s": ttl}
    real_tick = Q.tick
    seen = []
    # The production beat is max(1s, ttl/4) = 1s against a 2s TTL: one starved heartbeat
    # thread on a loaded box and the age crosses the TTL. Beat every 0.1s so the margin is
    # 20x the interval, and the test asserts "beats keep happening", not scheduler luck.
    real_hb = Q._heartbeat_while
    monkeypatch.setattr(Q, "_heartbeat_while",
                        lambda lease, interval_s=None: real_hb(lease, interval_s=0.1))

    def slow_tick(*a, **k):
        t_end = time.monotonic() + ttl * 1.5              # well past the TTL
        while time.monotonic() < t_end:
            seen.append(L.lease_status(cfg))
            time.sleep(0.25)
        return real_tick(*a, **k)

    monkeypatch.setattr(Q, "tick", slow_tick)
    Q.run(cfg, once=True)
    assert seen and all(s["held"] for s in seen)
    assert not any(s["stale"] for s in seen), [s["heartbeat_age_s"] for s in seen]
    assert max(s["heartbeat_age_s"] for s in seen) < ttl


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
