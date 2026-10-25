"""Queue event stream (SPEC-0181): `<data_dir>/agentic/events.jsonl`, one `wt.event.v1` record
per attention-worthy event, read by followers with per-reader cursors.

Events are **projected from the campaign ledgers** rather than fired from inside the queue: a
follower calls `sync()` which, under `events.lock`, turns ledger entries the stream has not
seen yet into events (exactly once, whoever syncs) and runs the watchdog checks. So the stream
works with any loop version, and the watchdog runs outside the loop's process — a hung loop
cannot report its own hang. Other producers (the mailbox) call `append()` directly.

Watchdog conditions fire once when they appear and again only after they cleared."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from . import lease as L
from . import store

SCHEMA = "wt.event.v1"

# type -> required data keys
EVENT_TYPES = {
    "admitted": (),
    "parked": ("reason", "detail", "resume_to"),
    "resumed": ("to",),
    "merged": ("merged_sha",),
    "handed_off": ("pr_url",),
    "abandoned": (),
    "run_failed": ("persona", "run_id", "transient", "error"),
    "cost_warning": ("total_usd", "cap_usd"),
    "backlog": ("ready", "blocked_by"),
    "runner_dead": ("active", "queued"),
    "lease_stale": ("pid", "heartbeat_age_s"),
    "run_overdue": ("persona", "run_id", "age_s", "timeout_s"),
    "campaign_silent": ("state", "silent_s"),
    "message": ("msg_id", "from", "kind", "text"),
}

# transient run failures: account limits, overload, network-ish API errors
_TRANSIENT_MARKERS = ("usage_limit", "session limit", "rate limit", "429", "overloaded",
                      "529", "api_error", "timed out connecting")

DEFAULTS = {"poll_s": 2.0, "silent_after_s": 900, "overdue_grace_s": 60,
            "cost_warning_ratio": 0.8}


def settings(cfg) -> dict:
    return {**DEFAULTS, **(((cfg.get("agentic") or {}).get("events")) or {})}


# ---- paths ------------------------------------------------------------------------------

def events_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "events.jsonl"


def _lock_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "events.lock"


def _projector_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "events.projector.json"


def cursors_dir(cfg) -> Path:
    return store.agentic_dir(cfg) / "cursors"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---- append / read ------------------------------------------------------------------------

def _last_id(path: Path) -> int:
    if not path.is_file():
        return 0
    last = 0
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("id"), int):
                last = max(last, rec["id"])
    return last


def validate(type_: str, data: dict) -> None:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    missing = [k for k in EVENT_TYPES[type_] if k not in (data or {})]
    if missing:
        raise ValueError(f"event {type_!r} missing data keys: {', '.join(missing)}")


def _append_locked(cfg, type_, *, campaign=None, spec_id=None, project=None, by, data,
                   source=None) -> dict:
    validate(type_, data)
    path = events_path(cfg)
    rec = {"schema": SCHEMA, "id": _last_id(path) + 1, "ts": store.now_iso(), "type": type_,
           "campaign": campaign, "spec_id": spec_id, "project": project, "by": by,
           "data": dict(data or {})}
    if source:
        rec["source"] = source
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return rec


def append(cfg, type_, *, campaign=None, spec_id=None, project=None, by="wt", data=None,
           source=None) -> dict:
    """Validate and append one event (flock + O_APPEND + single write; monotonic `id`)."""
    with _locked(_lock_path(cfg)):
        return _append_locked(cfg, type_, campaign=campaign, spec_id=spec_id, project=project,
                              by=by, data=data or {}, source=source)


def read_since(cfg, after_id=0, *, types=None) -> list[dict]:
    path = events_path(cfg)
    if not path.is_file():
        return []
    wanted = set(types) if types else None
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("id"), int):
                continue
            if rec["id"] <= after_id:
                continue
            if wanted and rec.get("type") not in wanted:
                continue
            out.append(rec)
    return out


# ---- projection from ledgers -------------------------------------------------------------

def _is_transient(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _TRANSIENT_MARKERS)


def _project_ledger_event(ev: dict, st: dict, sett: dict, warned: bool):
    """Map one ledger event to (type, data) or None. `warned` = cost warning already sent."""
    t = ev.get("type")
    if t == "transition":
        to, frm = ev.get("to"), ev.get("from")
        if to == "OPENING" and frm == "QUEUED":
            return "admitted", {}
        if to == "MERGED":
            return "merged", {"merged_sha": (st.get("close") or {}).get("merged_sha")}
        if to == "HANDED_OFF":
            return "handed_off", {"pr_url": (st.get("close") or {}).get("pr_url")}
        if to == "ABANDONED":
            return "abandoned", {}
        return None
    if t == "park":
        return "parked", {"reason": ev.get("reason"), "detail": ev.get("detail"),
                          "resume_to": ev.get("resume_to")}
    if t == "resume":
        return "resumed", {"to": ev.get("to"), "note": ev.get("note")}
    if t == "run.end" and (ev.get("is_error") or ev.get("timed_out")):
        err = ev.get("result_tail") or ("timed out" if ev.get("timed_out") else "error")
        return "run_failed", {"persona": ev.get("persona"), "run_id": ev.get("run_id"),
                              "transient": _is_transient(err), "error": err[-300:],
                              "timed_out": bool(ev.get("timed_out"))}
    if t == "cost" and not warned:
        total, cap = ev.get("total_usd"), ev.get("cap_usd")
        if isinstance(total, (int, float)) and isinstance(cap, (int, float)) and cap > 0 \
                and total >= sett["cost_warning_ratio"] * cap:
            return "cost_warning", {"total_usd": total, "cap_usd": cap}
    if t == "backlog":
        return "backlog", {"ready": ev.get("ready"), "blocked_by": ev.get("blocked_by") or []}
    return None


def _load_projector(cfg) -> dict:
    p = _projector_path(cfg)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"seen": {}, "warned": [], "conditions": {}}


def _project(cfg, proj: dict, sett: dict) -> list[dict]:
    out = []
    root = store.campaigns_dir(cfg)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        cid = d.name
        if not (d / "state.json").is_file():
            continue
        seen = int(proj["seen"].get(cid, 0))
        new = [e for e in store.read_ledger(cfg, cid) if int(e.get("seq") or 0) > seen]
        if not new:
            continue
        try:
            st = store.read_state(cfg, cid)
        except ValueError:
            continue
        for ev in new:
            hit = _project_ledger_event(ev, st, sett, cid in proj["warned"])
            if hit:
                type_, data = hit
                if type_ == "cost_warning":
                    proj["warned"].append(cid)
                out.append(_append_locked(
                    cfg, type_, campaign=cid, spec_id=st.get("spec_id"),
                    project=st.get("project"), by=ev.get("by") or "tick", data=data,
                    source={"ledger_seq": ev.get("seq")}))
            proj["seen"][cid] = max(int(proj["seen"].get(cid, 0)), int(ev.get("seq") or 0))
    return out


# ---- watchdog ----------------------------------------------------------------------------

def _age_s(ts, now) -> float | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return max(0.0, (now - t).total_seconds())


def _open_run(cfg, cid) -> dict | None:
    """The last `run.start` with no later `run.end` for the same run id."""
    starts, ended = {}, set()
    for ev in store.read_ledger(cfg, cid, types=["run.start", "run.end"]):
        if ev["type"] == "run.start":
            starts[ev.get("run_id")] = ev
        else:
            ended.add(ev.get("run_id"))
    open_runs = [e for rid, e in starts.items() if rid not in ended]
    return open_runs[-1] if open_runs else None


def watchdog_checks(cfg, now=None) -> list[tuple]:
    """Current problem conditions as `(key, type, campaign, data)`. Pure over the store, the
    ledgers and the runner lease; no writes."""
    from .queue import ACTIVE_STATES  # local: queue imports a lot
    now = now or dt.datetime.now().astimezone()
    sett = settings(cfg)
    rows = store.list_campaigns(cfg)
    active = [r for r in rows if r.get("state") in ACTIVE_STATES]
    queued = [r for r in rows if r.get("state") == "QUEUED"]
    lease = L.lease_status(cfg, now=now)
    out = []
    running = {}
    for r in active:
        run = _open_run(cfg, r["id"])
        if run:
            running[r["id"]] = run
    if (active or queued) and not lease.get("held"):
        out.append(("runner_dead", "runner_dead", None,
                    {"active": len(active), "queued": len(queued)}))
    # A long persona run blocks the loop's between-tick heartbeat; only call the lease stale
    # when no run is in flight within its timeout.
    if lease.get("stale"):
        in_budget = any((_age_s(run.get("ts"), now) or 0) <= float(run.get("timeout_s") or 1800)
                        + sett["overdue_grace_s"] for run in running.values())
        if not in_budget:
            out.append((f"lease_stale:{lease.get('pid')}", "lease_stale", None,
                        {"pid": lease.get("pid"), "heartbeat_age_s": lease.get("heartbeat_age_s")}))
    for r in active:
        cid = r["id"]
        run = running.get(cid)
        if run:
            age = _age_s(run.get("ts"), now) or 0
            limit = float(run.get("timeout_s") or 1800) + sett["overdue_grace_s"]
            if age > limit:
                out.append((f"run_overdue:{cid}:{run.get('run_id')}", "run_overdue", cid,
                            {"persona": run.get("persona"), "run_id": run.get("run_id"),
                             "age_s": round(age), "timeout_s": run.get("timeout_s")}))
            continue
        tail = store.read_ledger(cfg, cid, tail=1)
        silent = _age_s(tail[-1].get("ts"), now) if tail else _age_s(r.get("updated_at"), now)
        if silent is not None and silent > sett["silent_after_s"]:
            out.append((f"campaign_silent:{cid}:{r.get('state')}", "campaign_silent", cid,
                        {"state": r.get("state"), "silent_s": round(silent)}))
    return out


def _watch(cfg, proj: dict, now=None) -> list[dict]:
    current = {key: (type_, cid, data) for key, type_, cid, data in watchdog_checks(cfg, now)}
    out = []
    for key, (type_, cid, data) in current.items():
        if key in proj["conditions"]:
            continue
        st = {}
        if cid:
            try:
                st = store.read_state(cfg, cid)
            except ValueError:
                st = {}
        out.append(_append_locked(cfg, type_, campaign=cid, spec_id=st.get("spec_id"),
                                  project=st.get("project"), by="watchdog", data=data))
    proj["conditions"] = {key: True for key in current}
    return out


def sync(cfg, now=None) -> list[dict]:
    """Project new ledger entries and run the watchdog; exactly once across concurrent
    callers (serialized by `events.lock`). Returns the events appended by this call."""
    sett = settings(cfg)
    with _locked(_lock_path(cfg)):
        proj = _load_projector(cfg)
        out = _project(cfg, proj, sett)
        out += _watch(cfg, proj, now)
        _atomic_write(_projector_path(cfg), json.dumps(proj, indent=2) + "\n")
    return out


# ---- cursors and following -----------------------------------------------------------------

def cursor_get(cfg, reader) -> int:
    p = cursors_dir(cfg) / f"{_safe(reader)}.json"
    if p.is_file():
        try:
            return int(json.loads(p.read_text(encoding="utf-8")).get("after", 0))
        except (ValueError, TypeError):
            return 0
    return 0


def cursor_set(cfg, reader, after: int) -> None:
    d = cursors_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / f"{_safe(reader)}.json",
                  json.dumps({"reader": reader, "after": after, "ts": store.now_iso()}) + "\n")


@contextmanager
def reader_lock(cfg, reader):
    """Exclusive, non-blocking lock per reader name: one live follower per reader, so one
    conductor (and one steward per project). Raises ValueError naming the holder."""
    d = cursors_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe(reader)}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        holder = (d / f"{_safe(reader)}.pid")
        pid = holder.read_text().strip() if holder.is_file() else "?"
        raise ValueError(f"reader {reader!r} is already followed by pid {pid}")
    try:
        (d / f"{_safe(reader)}.pid").write_text(str(os.getpid()))
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def reader_holder(cfg, reader) -> int | None:
    """Pid of the live follower for `reader`, or None."""
    d = cursors_dir(cfg)
    path = d / f"{_safe(reader)}.lock"
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pid_file = d / f"{_safe(reader)}.pid"
        try:
            return int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return -1
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def summary_line(ev: dict) -> str:
    """One-line human form (still one line per event, Monitor-friendly)."""
    bits = " ".join(f"{k}={v}" for k, v in (ev.get("data") or {}).items()
                    if v not in (None, "", []))
    who = ev.get("campaign") or "-"
    return f"#{ev['id']} {ev['type']} {who} {bits}".strip()


def follow(cfg, reader, *, as_json=True, poll_s=None, types=None, out=None, max_events=None,
           stop=None, from_start=False) -> int:
    """Stream events after the reader's cursor forever: sync, print, advance cursor, sleep.
    Holds the reader lock for its lifetime. Returns the number of events printed."""
    out = out or sys.stdout
    poll = float(poll_s if poll_s is not None else settings(cfg)["poll_s"])
    printed = 0
    with reader_lock(cfg, reader):
        sync(cfg)
        if not (cursors_dir(cfg) / f"{_safe(reader)}.json").is_file() and not from_start:
            # a brand-new reader starts at "now": history is for `wt queue events`, not alerts
            cursor_set(cfg, reader, _last_id(events_path(cfg)))
        while True:
            sync(cfg)
            after = cursor_get(cfg, reader)
            for ev in read_since(cfg, after, types=types):
                out.write((json.dumps(ev, ensure_ascii=False) if as_json else summary_line(ev))
                          + "\n")
                out.flush()
                cursor_set(cfg, reader, ev["id"])
                printed += 1
                if max_events and printed >= max_events:
                    return printed
            if stop is not None and stop():
                return printed
            time.sleep(poll)
