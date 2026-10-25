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
from . import codewatch as CW
from . import lease as L
from . import store

SCHEMA = "wt.event.v1"

# type -> required data keys
EVENT_TYPES = {
    "admitted": (),
    "waiting": ("from",),
    "parked": ("reason", "detail", "resume_to"),
    "resumed": ("to",),
    "merged": ("merged_sha",),
    "handed_off": ("pr_url",),
    "abandoned": (),
    "run_failed": ("persona", "run_id", "transient", "error"),
    "cost_warning": ("total_usd", "cap_usd"),
    "backlog": ("ready", "blocked_by"),
    "rebase_resolved": ("conflicts",),
    "runner_dead": ("active", "queued"),
    "lease_stale": ("pid", "heartbeat_age_s"),
    "run_overdue": ("persona", "run_id", "age_s", "timeout_s"),
    "campaign_silent": ("state", "silent_s"),
    "message": ("msg_id", "from", "kind", "text"),
    # SPEC-0212
    "conductor_ping": ("blocked", "parked", "stuck", "waiting", "ready_not_admitted", "counts"),
    "backlog_blocked_long": ("blocked_for_s", "blocked_by"),
    "state_stuck": ("state", "stuck_s"),
    "ready_not_admitted": ("ready_for_s", "active", "slots"),
    # SPEC-0224
    "runner_draining": ("reason", "draining_s", "waiting_on", "ready"),
    # SPEC-0226
    "runner_config_invalid": ("scope", "path", "error"),
    # SPEC-0231: synthetic follower line (never appended to events.jsonl)
    "code_changed": ("from_sha", "to_sha", "paths", "skills_changed"),
    # SPEC-0222
    "park_undecided": ("reason", "parked_for_s", "owner"),
}

# transient run failures: account limits, overload, network-ish API errors
_TRANSIENT_MARKERS = ("usage_limit", "session limit", "rate limit", "429", "overloaded",
                      "529", "api_error", "timed out connecting")

DEFAULTS = {"poll_s": 2.0, "silent_after_s": 900, "overdue_grace_s": 60,
            "cost_warning_ratio": 0.8,
            # a GATING / CLOSING item runs project gates (and a rebase) in-process; the loop
            # cannot heartbeat meanwhile, so its lease is not stale within this budget
            "gate_budget_s": 1800,
            # SPEC-0212: conductor ping interval and the "too long" thresholds
            "ping_interval_s": 600, "blocked_after_s": 1200, "stuck_after_s": 900,
            "ready_grace_s": 60,
            # SPEC-0224: a drain (the runner waits for in-flight steps before a stop or exec)
            # older than this is reported as `runner_draining`
            "drain_grace_s": 60,
            # SPEC-0231: followers exit with `code_changed` when wt's code changes; 0 = off
            "code_check_s": 10,
            # SPEC-0222: a conductor-owned park without a decision note after this is overdue
            "park_decide_s": 300}


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
        if to == "WAITING":                                   # SPEC-0200
            return "waiting", {"from": frm}
        if frm == "WAITING":
            return "admitted", {"from": "WAITING", "to": to}
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
    if t == "rebase_resolved":                              # SPEC-0221
        return "rebase_resolved", {"conflicts": ev.get("conflicts") or [],
                                   "summary": ev.get("summary")}
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


# ---- ping digest and long-item checks (SPEC-0212) -----------------------------------------------

def _since_state(cfg, st: dict, now) -> float | None:
    """Seconds since the campaign entered its current state (last `transition` into it)."""
    for ev in reversed(store.read_ledger(cfg, st["id"], types=["transition"])):
        if ev.get("to") == st.get("state"):
            return _age_s(ev.get("ts"), now)
    return _age_s(st.get("updated_at") or st.get("created_at"), now)


def blocked_items(cfg, now, sett, *, after=None) -> list[dict]:
    """QUEUED items whose stored readiness is `blocked`, for longer than `after` seconds
    (default `blocked_after_s`)."""
    after = sett["blocked_after_s"] if after is None else after
    out = []
    for st in store.list_campaigns(cfg, state="QUEUED"):
        b = st.get("backlog") or {}
        if b.get("ready") is not False:
            continue
        age = _age_s(b.get("since") or st.get("created_at"), now) or 0
        if age > after:
            out.append({"campaign": st["id"], "spec_id": st.get("spec_id"),
                        "project": st.get("project"), "blocked_by": list(b.get("blocked_by") or []),
                        "blocked_for_s": round(age)})
    return sorted(out, key=lambda r: -r["blocked_for_s"])


def stuck_items(cfg, now, sett) -> list[dict]:
    """Active campaigns in one state longer than `stuck_after_s`, unless a persona run is in
    flight within its budget, or (GATING/CLOSING) the in-process gate is within `gate_budget_s`."""
    from .queue import ACTIVE_STATES
    out = []
    for st in store.list_campaigns(cfg):
        if st.get("state") not in ACTIVE_STATES:
            continue
        age = _since_state(cfg, st, now)
        if age is None or age <= sett["stuck_after_s"]:
            continue
        run = _open_run(cfg, st["id"])
        if run:
            if (_age_s(run.get("ts"), now) or 0) <= float(run.get("timeout_s") or 1800) \
                    + sett["overdue_grace_s"]:
                continue
        elif st["state"] in ("GATING", "CLOSING") and age <= sett["gate_budget_s"]:
            continue
        out.append({"campaign": st["id"], "spec_id": st.get("spec_id"), "state": st["state"],
                    "stuck_s": round(age)})
    return out


def ready_not_admitted_items(cfg, now, sett) -> list[dict]:
    """Ready backlog items that are not starting although a slot is free, for longer than
    `ready_grace_s`. Carries the runner pid and whether its tick has work in flight."""
    from .queue import ACTIVE_STATES, slots_for
    rows = store.list_campaigns(cfg)
    active = [r for r in rows if r.get("state") in ACTIVE_STATES]
    slots = slots_for(cfg)
    if len(active) >= slots:
        return []
    lease = L.lease_status(cfg, now=now)
    busy = any(_open_run(cfg, r["id"]) or r.get("state") in ("GATING", "CLOSING")
               for r in active)
    out = []
    for st in rows:
        b = st.get("backlog") or {}
        if st.get("state") != "QUEUED" or b.get("ready") is not True or st.get("park_requested"):
            continue
        age = _age_s(b.get("since"), now) or 0
        if age > sett["ready_grace_s"]:
            out.append({"campaign": st["id"], "spec_id": st.get("spec_id"),
                        "ready_for_s": round(age), "active": len(active), "slots": slots,
                        "runner_pid": lease.get("pid") if lease.get("held") else None,
                        "runner_draining": bool(lease.get("held") and lease.get("draining")),
                        "runner_busy": bool(lease.get("held") and busy)})
    return sorted(out, key=lambda r: -r["ready_for_s"])


def ping_digest(cfg, now=None) -> dict:
    """The `conductor_ping` payload: what needs the conductor's eyes right now."""
    from .queue import ACTIVE_STATES, park_view
    now = now or dt.datetime.now().astimezone()
    sett = settings(cfg)
    rows = store.list_campaigns(cfg)
    parked, waiting = [], []
    for st in rows:
        if st.get("state") == "PARKED":
            park = st.get("park") or {}
            since = park.get("since")
            view = park_view(cfg, st, now, sett)
            parked.append({"campaign": st["id"], "spec_id": st.get("spec_id"),
                           "reason": park.get("reason"), "detail": park.get("detail"),
                           "since": since, "parked_for_s": round(_age_s(since, now) or 0),
                           "decided": view["decided"], "owner": view["owner"],
                           "overdue": view["overdue"]})
        elif st.get("state") == "WAITING":
            w = st.get("waiting") or {}
            waiting.append({"campaign": st["id"], "spec_id": st.get("spec_id"),
                            "waiting_for_s": round(_age_s(w.get("since"), now) or 0),
                            "resume_to": w.get("resume_to")})
    blocked = blocked_items(cfg, now, sett)
    stuck = stuck_items(cfg, now, sett)
    rna = ready_not_admitted_items(cfg, now, sett)
    queued = [r for r in rows if r.get("state") == "QUEUED"]
    ready = sum(1 for r in queued if (r.get("backlog") or {}).get("ready") is True)
    counts = {"active": sum(1 for r in rows if r.get("state") in ACTIVE_STATES),
              "queued": len(queued), "ready": ready,
              "blocked": sum(1 for r in queued if (r.get("backlog") or {}).get("ready") is False),
              "parked": len(parked),
              "needs_you": sum(1 for p in parked if p["owner"] == "human"),
              "undecided_parks": sum(1 for p in parked
                                     if p["owner"] == "conductor" and not p["decided"]),
              "stuck": len(stuck), "waiting": len(waiting)}
    return {"blocked": blocked, "parked": parked, "stuck": stuck, "waiting": waiting,
            "ready_not_admitted": rna, "counts": counts}


def _ping(cfg, proj: dict, now) -> list[dict]:
    """Append a `conductor_ping` when the interval since the persisted `last_ping` has passed.
    The first ever call only seeds the clock."""
    interval = float(settings(cfg)["ping_interval_s"])
    if interval <= 0:
        return []
    now = now or dt.datetime.now().astimezone()
    last = _age_s(proj.get("last_ping"), now)
    if last is None:
        proj["last_ping"] = store.now_iso(now)
        return []
    if last < interval:
        return []
    proj["last_ping"] = store.now_iso(now)
    return [_append_locked(cfg, "conductor_ping", by="watchdog", data=ping_digest(cfg, now))]


def watchdog_checks(cfg, now=None) -> list[tuple]:
    """Current problem conditions as `(key, type, campaign, data)`. Pure over the store, the
    ledgers and the runner lease; no writes."""
    from .queue import ACTIVE_STATES, park_view, slots_for  # local: queue imports a lot
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
    # A long persona run or gate run blocks the loop's between-tick heartbeat; only call the
    # lease stale when nothing is in flight within its budget.
    if lease.get("stale"):
        in_budget = any((_age_s(run.get("ts"), now) or 0) <= float(run.get("timeout_s") or 1800)
                        + sett["overdue_grace_s"] for run in running.values())
        gating = any(r.get("state") in ("GATING", "CLOSING") and r["id"] not in running
                     and (_age_s(r.get("updated_at"), now) or 0) <= sett["gate_budget_s"]
                     for r in active)
        if not (in_budget or gating):
            out.append((f"lease_stale:{lease.get('pid')}", "lease_stale", None,
                        {"pid": lease.get("pid"), "heartbeat_age_s": lease.get("heartbeat_age_s")}))
    draining = lease.get("draining") if lease.get("held") else None
    if isinstance(draining, dict) and draining.get("since"):
        drained = _age_s(draining["since"], now) or 0
        if drained > float(sett["drain_grace_s"]):
            from .queue import backlog_readiness
            ready = sum(1 for q in queued if backlog_readiness(cfg, q)[0])
            out.append((f"runner_draining:{lease.get('pid')}:{draining['since']}",
                        "runner_draining", None,
                        {"reason": draining.get("reason"), "draining_s": round(drained),
                         "waiting_on": list(draining.get("waiting_on") or []),
                         "ready": ready, "slots": slots_for(cfg)}))
    for path, entry in sorted((lease.get("config_errors") or {}).items()):     # SPEC-0226
        out.append((f"runner_config_invalid:{path}:{entry.get('mtime_ns')}",
                    "runner_config_invalid", None,
                    {"scope": entry.get("scope"), "path": path, "error": entry.get("error"),
                     "since": entry.get("since")}))
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
    for b in blocked_items(cfg, now, sett):
        out.append((f"backlog_blocked_long:{b['campaign']}", "backlog_blocked_long",
                    b["campaign"], {k: b[k] for k in ("blocked_for_s", "blocked_by")}))
    for s_ in stuck_items(cfg, now, sett):
        out.append((f"state_stuck:{s_['campaign']}:{s_['state']}", "state_stuck", s_["campaign"],
                    {"state": s_["state"], "stuck_s": s_["stuck_s"]}))
    for r_ in ready_not_admitted_items(cfg, now, sett):
        out.append((f"ready_not_admitted:{r_['campaign']}", "ready_not_admitted",
                    r_["campaign"], {k: v for k, v in r_.items()
                                     if k not in ("campaign", "spec_id")}))
    for st in rows:
        if st.get("state") != "PARKED":
            continue
        view = park_view(cfg, st, now, sett)
        if view["overdue"]:
            out.append((f"park_undecided:{st['id']}:{view['park_seq']}", "park_undecided",
                        st["id"], {"reason": (st.get("park") or {}).get("reason"),
                                   "parked_for_s": round(view["parked_for_s"]),
                                   "owner": view["owner"]}))
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


def sync(cfg, now=None, ping=False) -> list[dict]:
    """Project new ledger entries and run the watchdog; exactly once across concurrent
    callers (serialized by `events.lock`). Returns the events appended by this call."""
    sett = settings(cfg)
    with _locked(_lock_path(cfg)):
        proj = _load_projector(cfg)
        out = _project(cfg, proj, sett)
        out += _watch(cfg, proj, now)
        if ping:                                  # SPEC-0212: only followers ping
            out += _ping(cfg, proj, now)
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


def followers(cfg, now=None) -> list[dict]:
    """One row per reader with a cursor file: kind, live pid (None when its lock is not
    held), cursor, lag (events or inbox messages after the cursor) and cursor age."""
    from . import mailbox as M
    now = now or dt.datetime.now().astimezone()
    d = cursors_dir(cfg)
    rows = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        reader = rec.get("reader") or p.stem
        try:
            after = int(rec.get("after", 0))
        except (ValueError, TypeError):
            after = 0
        mailbox = reader.startswith("mailbox:")
        lag = len(M.read_inbox(cfg, reader[len("mailbox:"):], after)) if mailbox \
            else len(read_since(cfg, after))
        pid = reader_holder(cfg, reader)
        age = _age_s(rec.get("ts"), now)
        rows.append({"reader": reader, "kind": "mailbox" if mailbox else "events",
                     "pid": pid, "held": pid is not None, "after": after,
                     "cursor_ts": rec.get("ts"), "cursor_age_s": None if age is None
                     else int(age), "lag": lag})
    return sorted(rows, key=lambda r: (r["kind"], r["reader"]))


def summary_line(ev: dict) -> str:
    """One-line human form (still one line per event, Monitor-friendly)."""
    bits = " ".join(f"{k}={v}" for k, v in (ev.get("data") or {}).items()
                    if v not in (None, "", []))
    who = ev.get("campaign") or "-"
    evid = "-" if ev.get("id") is None else ev["id"]
    if ev.get("type") == "code_changed":
        d = ev.get("data") or {}
        return (f"code_changed {str(d.get('from_sha'))[:7]}..{str(d.get('to_sha'))[:7]} "
                f"skills={','.join(d.get('skills_changed') or []) or '-'} (re-arm)")
    if ev.get("type") == "conductor_ping":
        d, c = ev.get("data") or {}, (ev.get("data") or {}).get("counts") or {}
        return (f"#{evid} conductor_ping blocked={len(d.get('blocked') or [])} "
                f"parked={c.get('parked', 0)}(undecided {c.get('undecided_parks', 0)}) "
                f"stuck={len(d.get('stuck') or [])} waiting={c.get('waiting', 0)} "
                f"ready_not_admitted={len(d.get('ready_not_admitted') or [])} "
                f"active={c.get('active', 0)} queued={c.get('queued', 0)}")
    return f"#{evid} {ev['type']} {who} {bits}".strip()


def code_changed_line(reader, change: dict, *, as_json=True) -> str:
    """The synthetic `code_changed` line a follower prints before exiting (SPEC-0231). Not an
    event of the stream: `id` is null, nothing is appended to events.jsonl."""
    data = {"reader": reader, **change}
    validate("code_changed", data)
    ev = {"schema": SCHEMA, "id": None, "ts": store.now_iso(), "type": "code_changed",
          "campaign": None, "spec_id": None, "project": None, "by": "follower", "data": data}
    return (json.dumps(ev, ensure_ascii=False) if as_json else summary_line(ev)) + "\n"


def follow(cfg, reader, *, as_json=True, poll_s=None, types=None, out=None, max_events=None,
           stop=None, from_start=False, code_root=CW.AUTO, code_check_s=None) -> int:
    """Stream events after the reader's cursor forever: sync, print, advance cursor, sleep.
    Holds the reader lock for its lifetime. Returns the number of events printed. When wt's
    code changes (SPEC-0231) it prints one `code_changed` line and returns; `code_root=None`
    turns that watch off."""
    out = out or sys.stdout
    poll = float(poll_s if poll_s is not None else settings(cfg)["poll_s"])
    printed = 0
    with reader_lock(cfg, reader):
        watcher = CW.Watcher(CW.code_checkout() if code_root is CW.AUTO else code_root,
                             settings(cfg)["code_check_s"] if code_check_s is None
                             else code_check_s)
        sync(cfg, ping=True)
        if not (cursors_dir(cfg) / f"{_safe(reader)}.json").is_file() and not from_start:
            # a brand-new reader starts at "now": history is for `wt queue events`, not alerts
            cursor_set(cfg, reader, _last_id(events_path(cfg)))
        while True:
            sync(cfg, ping=True)
            after = cursor_get(cfg, reader)
            for ev in read_since(cfg, after, types=types):
                out.write((json.dumps(ev, ensure_ascii=False) if as_json else summary_line(ev))
                          + "\n")
                out.flush()
                cursor_set(cfg, reader, ev["id"])
                printed += 1
                if max_events and printed >= max_events:
                    return printed
            change = watcher.poll(time.monotonic())
            if change:
                out.write(code_changed_line(reader, change, as_json=as_json))
                out.flush()
                return printed
            if stop is not None and stop():
                return printed
            time.sleep(poll)
