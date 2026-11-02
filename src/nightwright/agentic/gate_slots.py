"""Host-wide gate-slot pool (SPEC-0309): at most `agentic.gates.slots` `gates.run_all` calls run
at once on the host, so parallel pytest (`-n 4`) never oversubscribes the CPU. A slot is an
`fcntl.flock` on `<agentic_dir>/gate-slots/slot-<n>.lock` — it dies with the holder's process, so
a crashed runner never leaks one (the approach of `lease.py`) — and the file carries
`{campaign, pid, since, waited_s}` for `occupancy()` and `holding()` (the finished wait, which the
stuck detector must not count against the gate that follows it). A campaign blocked for a slot publishes
`waiting-<campaign>.json`, the one on-disk signal the stuck and silent detectors read.

SPEC-0337: `acquire` can wait on an ordered list of pools. The local pool is `slot-<n>.lock`
here; worker `W`'s is `w-<W>/slot-<n>.lock`. A pool is named by its worker, `None` for local."""
from __future__ import annotations

import fcntl
import json
import os
import re
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
_POOL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
WAIT_EVENT_S = 30.0         # a wait longer than this appends one `gate_waiting` ledger entry


@dataclass
class SlotHold:
    slot: int | None        # None: no slot held (the wait was cut short by a stop)
    waited_s: float = 0.0
    interrupted: bool = False
    pool: str | None = None     # the worker whose pool the slot is in; None: local (SPEC-0337)


def slots_dir(cfg) -> Path:
    return agentic_dir(cfg) / "gate-slots"


def pool_dir(cfg, pool: str | None) -> Path:
    """Where a pool's slot files live: the slots dir for local, `w-<worker>/` for a worker."""
    if pool is None:
        return slots_dir(cfg)
    if not isinstance(pool, str) or not _POOL_NAME.fullmatch(pool):
        raise ValueError(f"invalid gate-slot pool name {pool!r}")
    return slots_dir(cfg) / f"w-{pool}"


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


HOLDER_KEYS = ("job", "by", "project", "branch")      # SPEC-0381: a standalone gate job's holder


def _holder(holder) -> dict:
    """SPEC-0381: the `HOLDER_KEYS` of `holder` (string values only), for the hold file and the
    waiter marker."""
    return {k: str(holder[k]) for k in HOLDER_KEYS
            if isinstance(holder, dict) and holder.get(k) is not None}


def _try_take(cfg, pools: list, campaign_id, t0: float, skip=None, holder=None):
    """Take the first free slot of the first pool that has one: `(slot, fd, waited_s, pool)`,
    else None. The hold file records the wait that ended in it (`waited_s`, seconds since
    `t0`). SPEC-0343: a non-local pool for which `skip(pool)` is true is passed over."""
    for pool, n_slots in pools:
        if pool is not None and skip is not None and skip(pool):
            continue
        d = pool_dir(cfg, pool)
        d.mkdir(parents=True, exist_ok=True)
        for n in range(n_slots):
            fd = os.open(d / f"slot-{n}.lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            waited_s = round(time.monotonic() - t0, 1)
            info = {"campaign": campaign_id, "pid": os.getpid(), "since": now_iso(),
                    "waited_s": waited_s}
            if pool is not None:
                info["pool"] = pool
            info.update(_holder(holder))
            try:
                os.ftruncate(fd, 0)
                os.write(fd, (json.dumps(info) + "\n").encode("utf-8"))
            except BaseException:               # never keep a lock the caller cannot release
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                raise
            return n, fd, waited_s, pool
    return None


def _release(fd) -> None:
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _probe_held(d: Path) -> list[dict]:
    """The slots of one pool dir whose flock is held (a probe takes and drops it)."""
    held = []
    for p in sorted(d.glob("slot-*.lock"), key=lambda p: int(p.stem.split("-")[1])
                    if p.stem.split("-")[1].isdigit() else 0):
        fd = os.open(p, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            info = _read_json(p) or {}
            held.append({"slot": int(p.stem.split("-")[1]), "campaign": info.get("campaign"),
                         "pid": info.get("pid"), "since": info.get("since"),
                         **{k: info[k] for k in HOLDER_KEYS if k in info}})
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return held


def held(cfg, pool) -> list[dict]:
    """SPEC-0343: the held slots of one pool (`pool_dir`); a missing dir means none."""
    d = pool_dir(cfg, pool)
    return _probe_held(d) if d.is_dir() else []


def occupancy(cfg) -> dict:
    """`{slots, held: [{slot, campaign, pid, since}], waiting: [{campaign, pid, since}]}`. A
    slot is held when its flock is (a probe takes and drops it); a waiter whose pid is gone is
    left out. SPEC-0337: with a worker registered it adds `workers: {W: {slots, held}}`; the
    other keys are the local pool's, as before."""
    from . import workers as WK
    d = slots_dir(cfg)
    held, waiting = [], []
    if d.is_dir():
        held = _probe_held(d)
        for p in sorted(d.glob("waiting-*.json")):
            rec = _read_json(p)
            if rec and _pid_alive(rec.get("pid")):
                waiting.append({**{k: rec.get(k) for k in ("campaign", "pid", "since")},
                                **{k: rec[k] for k in HOLDER_KEYS if k in rec}})
    out = {"slots": settings(cfg)["slots"], "held": held, "waiting": waiting}
    registered = WK.load(cfg)
    if registered:
        out["workers"] = {w["name"]: {"slots": w["slots"],
                                      "held": _probe_held(d / f"w-{w['name']}")
                                      if (d / f"w-{w['name']}").is_dir() else []}
                          for w in registered}
    return out


def waiting(cfg, campaign_id, now=None) -> dict | None:
    """The campaign's live waiter marker, else None (absent, unreadable or a dead pid)."""
    rec = _read_json(_waiter_path(cfg, campaign_id))
    if rec and _pid_alive(rec.get("pid")):
        return rec
    return None


def holding(cfg, campaign_id) -> dict | None:
    """The live hold of `campaign_id` (`{campaign, pid, since, waited_s}`, plus `pool` for a
    worker's), else None. The finished wait stays readable while the gate runs, after the
    waiter marker is gone. SPEC-0337: scans the local slots and every `w-*/` dir on disk,
    registered or not, so a hold on a worker just removed stays visible."""
    d = slots_dir(cfg)
    if not d.is_dir():
        return None
    for p in sorted(d.glob("slot-*.lock")) + sorted(d.glob("w-*/slot-*.lock")):
        rec = _read_json(p)
        if rec and rec.get("campaign") == campaign_id and _pid_alive(rec.get("pid")):
            return rec
    return None


def _drop_marker(marker: Path) -> None:
    """Remove our own waiter marker; one a second wait of the same campaign (another pid)
    wrote over is left to its writer."""
    rec = _read_json(marker)
    if rec is None or rec.get("pid") == os.getpid():
        marker.unlink(missing_ok=True)


def _repooled(cfg, repool, current: list) -> list:
    """SPEC-0374: `repool()`'s list when it is a usable one, else `current`."""
    try:
        new = list(repool() or [])
        for pool, _n in new:
            pool_dir(cfg, pool)
    except Exception:                           # a registry read failing mid-wait: keep waiting
        return current
    return new or current


@contextmanager
def acquire(cfg, campaign_id, *, stop=None, poll_s=None, on_wait=None,
            wait_event_s=None, pools=None, skip=None, repool=None,
            holder=None) -> Iterator[SlotHold]:
    """Hold one slot for the `with` body. `pools` is an ordered list of `(pool_name, n_slots)`
    (`pool_name` a worker name, `None` for local; default local only): each poll tries them in
    order and takes the first free slot, so earlier pools are preferred; the pool list is
    fixed for the wait unless `repool` is given. `SlotHold.pool` says which one it got. While none is free, poll every
    `poll_s` with the
    waiter marker published; a stop (`stop` set, or the gates' sticky stop) ends the wait and
    yields a hold with `interrupted=True` and no slot. `on_wait(occupancy, waited_s)` is
    called once when the wait passes `wait_event_s` (default `WAIT_EVENT_S`; SPEC-0336's worker
    reads it from its environment). SPEC-0343: `skip(pool) -> bool`, evaluated before each try of
    a non-local pool, passes over a pool that may no longer take gates (a draining worker).
    SPEC-0374: `repool() -> list`, called before each retry while waiting, replaces the pool
    list (a worker that went into cooldown, was disabled or drained leaves it; with none left
    the local pool comes in), so a waiter is never stranded on a pool that no longer takes
    gates. An exception, an empty list or a bad pool name keeps the current list. SPEC-0381:
    `holder` (`{job, by, project, branch}`) is written into the hold file and the waiter marker."""
    from . import gates as G                    # lazy: gates imports this module
    poll_s = POLL_S if poll_s is None else poll_s
    wait_event_s = WAIT_EVENT_S if wait_event_s is None else wait_event_s
    slots_dir(cfg).mkdir(parents=True, exist_ok=True)
    pools = [(None, settings(cfg)["slots"])] if pools is None else list(pools)
    if not pools:
        raise ValueError("pools must not be empty")
    for pool, _n in pools:
        pool_dir(cfg, pool)                     # reject a bad name before anything is held
    t0 = time.monotonic()
    taken = _try_take(cfg, pools, campaign_id, t0, skip, holder)
    if taken is None:
        marker = _waiter_path(cfg, campaign_id)
        _atomic_write(marker, json.dumps({"campaign": campaign_id, "pid": os.getpid(),
                                          "since": now_iso(), **_holder(holder)}) + "\n")
        announced = False
        try:
            while taken is None:
                if G._stop_requested(stop):
                    break
                waited = time.monotonic() - t0
                if on_wait is not None and not announced and waited > wait_event_s:
                    announced = True
                    try:
                        on_wait(occupancy(cfg), round(waited, 1))
                    except Exception:           # informational: never abort the gate run
                        pass
                if stop is not None:
                    stop.wait(poll_s)
                else:
                    time.sleep(poll_s)
                if repool is not None:
                    pools = _repooled(cfg, repool, pools)
                taken = _try_take(cfg, pools, campaign_id, t0, skip, holder)
        finally:
            _drop_marker(marker)
    if taken is None:
        yield SlotHold(None, round(time.monotonic() - t0, 1), interrupted=True)
        return
    slot, fd, waited_s, pool = taken
    try:
        yield SlotHold(slot, waited_s, pool=pool)
    finally:
        _release(fd)
