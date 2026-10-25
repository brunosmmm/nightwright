"""Singleton runner lease (SPEC-0171): `fcntl.flock` on `<data_dir>/agentic/queue.lock` is the
exclusion — it dies with the holder's process — and `queue.lease.json` carries who holds it
and a heartbeat. The TTL is advisory: `lease_status()` flags a hung holder as `stale`, it
never takes the lease over."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import socket
from pathlib import Path

from ..export import _atomic_write
from .store import agentic_dir, now_iso


def lock_path(cfg) -> Path:
    return agentic_dir(cfg) / "queue.lock"


def lease_path(cfg) -> Path:
    return agentic_dir(cfg) / "queue.lease.json"


def read_lease(cfg) -> dict | None:
    p = lease_path(cfg)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


class Lease:
    """A held lease. Keep the object alive for as long as the loop runs."""

    def __init__(self, cfg, fd, info):
        self.cfg = cfg
        self.fd = fd
        self.info = info

    def heartbeat(self, now=None):
        self.info["heartbeat_at"] = now_iso(now)
        _atomic_write(lease_path(self.cfg), json.dumps(self.info, indent=2) + "\n")

    def release(self):
        if self.fd is None:
            return
        try:
            lease_path(self.cfg).unlink(missing_ok=True)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def _heartbeat_age(info, now=None) -> float | None:
    raw = (info or {}).get("heartbeat_at")
    if not raw:
        return None
    try:
        hb = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    now = now or dt.datetime.now().astimezone()
    if hb.tzinfo is None:
        hb = hb.astimezone()
    return max(0.0, (now - hb).total_seconds())


def acquire(cfg, argv=None, *, ttl_s=None, now=None) -> Lease:
    """Take the runner lease or raise ValueError naming the current holder."""
    d = agentic_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(cfg), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        holder = read_lease(cfg) or {}
        age = _heartbeat_age(holder, now)
        age_txt = f"heartbeat {age:.0f}s ago" if age is not None else "no heartbeat"
        raise ValueError(f"queue held by pid {holder.get('pid', '?')} on "
                         f"{holder.get('host', '?')} ({age_txt})")
    ttl = ttl_s if ttl_s is not None else ((cfg.get("agentic") or {}).get("lease_ttl_s") or 120)
    info = {"pid": os.getpid(), "host": socket.gethostname(), "argv": list(argv or []),
            "started_at": now_iso(now), "heartbeat_at": now_iso(now), "ttl_s": ttl}
    lease = Lease(cfg, fd, info)
    _atomic_write(lease_path(cfg), json.dumps(info, indent=2) + "\n")
    return lease


def is_held(cfg) -> bool:
    """Probe the flock with a fresh descriptor (released at once)."""
    p = lock_path(cfg)
    if not p.exists():
        return False
    fd = os.open(p, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def lease_status(cfg, now=None) -> dict:
    """`{held, pid, host, started_at, heartbeat_at, heartbeat_age_s, ttl_s, stale}`."""
    info = read_lease(cfg) or {}
    held = is_held(cfg)
    age = _heartbeat_age(info, now) if held else None
    ttl = info.get("ttl_s") or (cfg.get("agentic") or {}).get("lease_ttl_s") or 120
    return {
        "held": held,
        "pid": info.get("pid") if held else None,
        "host": info.get("host") if held else None,
        "started_at": info.get("started_at") if held else None,
        "heartbeat_at": info.get("heartbeat_at") if held else None,
        "heartbeat_age_s": round(age, 1) if age is not None else None,
        "ttl_s": ttl,
        "stale": bool(held and age is not None and age > ttl),
    }

