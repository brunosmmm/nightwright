"""Host-wide gate-slot pool (SPEC-0309): at most `agentic.gates.slots` `gates.run_all` calls run
at once on the host, so parallel pytest (`-n 4`) never oversubscribes the CPU. A slot is an
`fcntl.flock` on `<agentic_dir>/gate-slots/slot-<n>.lock` — it dies with the holder's process, so
a crashed runner never leaks one (the approach of `lease.py`) — and the file carries
`{campaign, pid, since}` for `occupancy()`. A campaign blocked for a slot publishes
`waiting-<campaign>.json`, the one on-disk signal the stuck and silent detectors read."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..export import _atomic_write
from .store import agentic_dir, now_iso

DEFAULT_SLOTS = 2
MAX_SLOTS = 16
POLL_S = 1.0
WAIT_EVENT_S = 30.0         # a wait longer than this appends one `gate_waiting` ledger entry


@dataclass
class SlotHold:
    slot: int | None        # None: no slot held (the wait was cut short by a stop)
    waited_s: float = 0.0
    interrupted: bool = False


def slots_dir(cfg) -> Path:
    return agentic_dir(cfg) / "gate-slots"


def settings(cfg) -> dict:
    """`{"slots": n}` from `agentic.gates.slots`; anything but an int from 1 to 16 (a bool, a
    string, a float, 0, 99) falls back to the default, as `events.settings` does."""
    raw = (((cfg.get("agentic") or {}).get("gates")) or {}).get("slots")
    ok = isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= MAX_SLOTS
    return {"slots": raw if ok else DEFAULT_SLOTS}


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:                         # EPERM: it exists, owned by someone else
        return True
    return True


def _read_json(path: Path) -> dict | None:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _waiter_path(cfg, campaign_id) -> Path:
    return slots_dir(cfg) / f"waiting-{campaign_id}.json"


def _try_take(cfg, n_slots: int, campaign_id):
    """Take the first free slot: `(slot, fd)`, else None."""
    d = slots_dir(cfg)
    for n in range(n_slots):
        fd = os.open(d / f"slot-{n}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        info = {"campaign": campaign_id, "pid": os.getpid(), "since": now_iso()}
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps(info) + "\n").encode("utf-8"))
        return n, fd
    return None


def _release(fd) -> None:
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def occupancy(cfg) -> dict:
    """`{slots, held: [{slot, campaign, pid, since}], waiting: [{campaign, pid, since}]}`. A
    slot is held when its flock is (a probe takes and drops it); a waiter whose pid is gone is
    left out."""
    d = slots_dir(cfg)
    held, waiting = [], []
    if d.is_dir():
        for p in sorted(d.glob("slot-*.lock"), key=lambda p: int(p.stem.split("-")[1])
                        if p.stem.split("-")[1].isdigit() else 0):
            fd = os.open(p, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                info = _read_json(p) or {}
                held.append({"slot": int(p.stem.split("-")[1]), "campaign": info.get("campaign"),
                             "pid": info.get("pid"), "since": info.get("since")})
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        for p in sorted(d.glob("waiting-*.json")):
            rec = _read_json(p)
            if rec and _pid_alive(rec.get("pid")):
                waiting.append({k: rec.get(k) for k in ("campaign", "pid", "since")})
    return {"slots": settings(cfg)["slots"], "held": held, "waiting": waiting}


def waiting(cfg, campaign_id, now=None) -> dict | None:
    """The campaign's live waiter marker, else None (absent, unreadable or a dead pid)."""
    rec = _read_json(_waiter_path(cfg, campaign_id))
    if rec and _pid_alive(rec.get("pid")):
        return rec
    return None


@contextmanager
def acquire(cfg, campaign_id, *, stop=None, poll_s=None, on_wait=None) -> Iterator[SlotHold]:
    """Hold one slot for the `with` body. While none is free, poll every `poll_s` with the
    waiter marker published; a stop (`stop` set, or the gates' sticky stop) ends the wait and
    yields a hold with `interrupted=True` and no slot. `on_wait(occupancy, waited_s)` is
    called once when the wait passes `WAIT_EVENT_S`."""
    from . import gates as G                    # lazy: gates imports this module
    poll_s = POLL_S if poll_s is None else poll_s
    slots_dir(cfg).mkdir(parents=True, exist_ok=True)
    n_slots = settings(cfg)["slots"]
    t0 = time.monotonic()
    taken = _try_take(cfg, n_slots, campaign_id)
    if taken is None:
        marker = _waiter_path(cfg, campaign_id)
        _atomic_write(marker, json.dumps({"campaign": campaign_id, "pid": os.getpid(),
                                          "since": now_iso()}) + "\n")
        announced = False
        try:
            while taken is None:
                if G._stop_requested(stop):
                    break
                waited = time.monotonic() - t0
                if on_wait is not None and not announced and waited > WAIT_EVENT_S:
                    announced = True
                    on_wait(occupancy(cfg), round(waited, 1))
                if stop is not None:
                    stop.wait(poll_s)
                else:
                    time.sleep(poll_s)
                taken = _try_take(cfg, n_slots, campaign_id)
        finally:
            marker.unlink(missing_ok=True)
    waited_s = round(time.monotonic() - t0, 1)
    if taken is None:
        yield SlotHold(None, waited_s, interrupted=True)
        return
    slot, fd = taken
    try:
        yield SlotHold(slot, waited_s)
    finally:
        _release(fd)
