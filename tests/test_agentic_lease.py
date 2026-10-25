"""SPEC-0171: singleton runner lease — flock is the exclusion, the TTL is advisory."""
import datetime as dt
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
                                   "ttl_s": 120, "stale": False}
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
