"""Singleton runner lease (SPEC-0171): `fcntl.flock` on `<data_dir>/agentic/queue.lock` is the
exclusion — it dies with the holder's process — and `queue.lease.json` carries who holds it
and a heartbeat. The TTL is advisory: `lease_status()` flags a hung holder as `stale`, it
never takes the lease over."""
from __future__ import annotations

import copy
import datetime as dt
import fcntl
import json
import os
import socket
import threading
from pathlib import Path

from ..export import _atomic_write
from .store import agentic_dir, now_iso


HANDOVER_ENV = "NW_RUNNER_LEASE_FD"
# SPEC-0228: the loop state the runner publishes in the lease info (read by the watchdog)
LOOP_KEYS = ("tick_at", "poll_interval_s", "in_flight", "slot_freed_at", "busy",
             "busy_ended_at", "slots_full_at", "progress_at", "last_tick_s", "ticks",
             "code_sha",                                         # SPEC-0375
             "credentials")                      # SPEC-0487: {VAR: status}, never a value


def lock_path(cfg) -> Path:
    return agentic_dir(cfg) / "queue.lock"


def lease_path(cfg) -> Path:
    return agentic_dir(cfg) / "queue.lease.json"


def read_lease(cfg) -> dict | None:
    p = lease_path(cfg)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):       # absent, or unlinked by a release between polls
        return None


class Lease:
    """A held lease. Keep the object alive for as long as the loop runs."""

    def __init__(self, cfg, fd, info):
        self.cfg = cfg
        self.fd = fd
        self.info = info
        # The heartbeat thread serialises `info` while the main thread changes it (SPEC-0224):
        # every change and every write happens under this lock, so an older snapshot can never
        # land after a newer one.
        self._lock = threading.Lock()

    def _write(self):
        snapshot = copy.deepcopy(self.info)
        _atomic_write(lease_path(self.cfg), json.dumps(snapshot, indent=2) + "\n")

    def heartbeat(self, now=None):
        with self._lock:
            self.info["heartbeat_at"] = now_iso(now)
            self._write()

    def update_info(self, **kv):
        """Set lease info keys (new values, never mutated in place) and write the file."""
        with self._lock:
            self.info.update(copy.deepcopy(kv))
            self._write()

    def pop_info(self, *keys):
        """Remove lease info keys and write the file."""
        with self._lock:
            for k in keys:
                self.info.pop(k, None)
            self._write()

    def end_busy(self, prev=None):
        """Leave a declared long step in one locked write: restore `prev` (an enclosing
        step) or drop `busy`, and stamp `busy_ended_at`, so the watchdog never sees `busy`
        gone without a fresh activity mark (SPEC-0228)."""
        with self._lock:
            if prev is None:
                self.info.pop("busy", None)
            else:
                self.info["busy"] = copy.deepcopy(prev)
            self.info["busy_ended_at"] = now_iso()
            self._write()

    def release(self):
        with self._lock:
            if self.fd is None:
                return
            try:
                lease_path(self.cfg).unlink(missing_ok=True)
            finally:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
                self.fd = None

    def handover(self) -> dict:
        """Keep the lease across `os.execv` (SPEC-0194): the flock lives on the open file
        description, so making the descriptor inheritable and naming it in the environment
        lets the re-executed loop adopt it — the lease is never free in between. Returns the
        env additions; after this, `release()` is a no-op in this image."""
        with self._lock:
            self.info.pop("draining", None)       # the drain ends with this process image
            self.info.pop("busy", None)           # SPEC-0228: so does a declared long step
            self._write()
            os.set_inheritable(self.fd, True)
            fd, self.fd = self.fd, None
        return {HANDOVER_ENV: str(fd)}

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
    """Take the runner lease or raise ValueError naming the current holder. A loop that
    re-executed itself adopts the descriptor it handed over (`Lease.handover`)."""
    d = agentic_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    adopted = _adopt_handover(cfg)
    if adopted is not None:
        info = dict(read_lease(cfg) or {})
        info.pop("draining", None)
        info.pop("busy", None)
        info.update({"pid": os.getpid(), "argv": list(argv or []),
                     "heartbeat_at": now_iso(now), "restarted_at": now_iso(now)})
        info.setdefault("host", socket.gethostname())
        info.setdefault("started_at", now_iso(now))
        info.setdefault("ttl_s", ttl_s or (cfg.get("agentic") or {}).get("lease_ttl_s") or 120)
        lease = Lease(cfg, adopted, info)
        _atomic_write(lease_path(cfg), json.dumps(info, indent=2) + "\n")
        return lease
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


def _adopt_handover(cfg):
    """The handed-over descriptor when it still holds this lease's flock, else None."""
    raw = os.environ.pop(HANDOVER_ENV, None)
    if not raw:
        return None
    try:
        fd = int(raw)
        if os.fstat(fd).st_ino != os.stat(lock_path(cfg)).st_ino:
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)     # we already hold it: succeeds
        os.set_inheritable(fd, False)
        return fd
    except (ValueError, OSError):
        return None


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
    """`{held, pid, host, started_at, heartbeat_at, heartbeat_age_s, ttl_s, stale}`, plus
    `draining` (the runner's drain info or None), `config_errors` (SPEC-0226: path -> broken
    config entry, `{}` when none), `log` / `detached` (SPEC-0291: set by `nw queue run --detach`,
    None otherwise) and the loop state of SPEC-0228 (`tick_at`, `poll_interval_s`,
    `in_flight`, `slot_freed_at`, `busy`, `busy_ended_at`, `slots_full_at`; each None when the runner has not written it) while
    held."""
    info = read_lease(cfg) or {}
    held = is_held(cfg)
    age = _heartbeat_age(info, now) if held else None
    ttl = info.get("ttl_s") or (cfg.get("agentic") or {}).get("lease_ttl_s") or 120
    out = {
        "held": held,
        "pid": info.get("pid") if held else None,
        "host": info.get("host") if held else None,
        "started_at": info.get("started_at") if held else None,
        "heartbeat_at": info.get("heartbeat_at") if held else None,
        "heartbeat_age_s": round(age, 1) if age is not None else None,
        "ttl_s": ttl,
        "stale": bool(held and age is not None and age > ttl),
    }
    out["draining"] = (info.get("draining") or None) if held else None     # SPEC-0224
    if held:
        out["config_errors"] = info.get("config_errors") or {}     # SPEC-0226
        out["log"] = info.get("log")                               # SPEC-0291: --detach
        out["detached"] = info.get("detached")
    for key in LOOP_KEYS:                                          # SPEC-0228
        out[key] = info.get(key) if held else None
    return out

