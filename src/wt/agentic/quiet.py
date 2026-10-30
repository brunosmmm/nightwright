"""Quiet conductor (SPEC-0300): the conductor's event follower holds informational events,
folds them into a periodic `conductor_digest` line and counts every delivered line, so a wakeup
is spent on decisions. Nothing here changes what `events.sync` appends: `events.jsonl`, `wt queue
events` history, the TUI and steward followers are untouched. Unknown event types wake."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

from ..export import _atomic_write
from . import events as E
from . import lease as L
from . import store

DIGEST_SCHEMA = "wt.conductor.digest.v1"
WAKEUPS_SCHEMA = "wt.conductor.wakeups.v1"

# type -> "hold" | "wake" | "cond" (decided from facts at delivery time). The skill's *Quiet
# stream* paragraph mirrors the hold and cond lists; a test compares them. Any type missing
# here wakes.
RULES = {
    "admitted": "hold", "resumed": "hold", "waiting": "hold", "abandoned": "hold",
    "cost_warning": "hold", "escalation": "hold", "escalation_withdrawn": "hold",
    "rebase_resolved": "hold", "run_failed": "hold", "merged": "hold", "handed_off": "hold",
    "backlog": "cond", "conductor_ping": "cond", "runner_draining": "cond",
    "campaign_silent": "cond", "state_stuck": "cond",
    "parked": "wake", "park_undecided": "wake", "runner_dead": "wake", "lease_stale": "wake",
    "run_overdue": "wake", "message": "wake", "escalation_answered": "wake",
    "ready_not_admitted": "wake", "waiting_not_admitted": "wake",
    "runner_journal_silent": "wake", "temp_pressure": "wake", "runner_config_invalid": "wake",
    "backlog_blocked_long": "wake", "code_changed": "wake",
}

DEFAULTS = {"enabled": True, "readers": ["conductor"], "digest_interval_s": 1800}

# a `backlog` event is held when it only restates an ordinary dependency wait; any other
# `blocked_by` text (a missing spec, a missing branch ...) wakes, see `backlog_resolved`
_BENIGN_BLOCKED = ("waiting on ", "spec status is ")
_GRACE_S = 300                     # the table's own "wait" window past a drain's step timeout


def settings(cfg) -> dict:
    """`DEFAULTS` overlaid with `agentic.conductor.quiet`; a bad value falls back to its default."""
    raw = (((cfg.get("agentic") or {}).get("conductor") or {}).get("quiet")) or {}
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    readers = raw.get("readers")
    if isinstance(readers, list) and all(isinstance(r, str) for r in readers):
        out["readers"] = list(readers)
    v = raw.get("digest_interval_s")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
        out["digest_interval_s"] = v
    return out


def enabled_for(cfg, reader) -> bool:
    s = settings(cfg)
    return bool(s["enabled"] and reader in s["readers"])


# ---- classification -----------------------------------------------------------------------

def ping_fingerprint(data: dict) -> list:
    """Sorted `[category, campaign]` pairs of what in a `conductor_ping` needs action."""
    fp = set()
    for cat in ("blocked", "stuck", "ready_not_admitted", "waiting_not_admitted"):
        for it in data.get(cat) or []:
            fp.add((cat, str(it.get("campaign"))))
    for it in data.get("parked") or []:
        if it.get("owner") == "conductor" and not it.get("decided"):
            fp.add(("parked", str(it.get("campaign"))))
    runner = data.get("runner")
    if runner:
        fp.add(("runner", str(runner.get("pid"))))
    return [list(p) for p in sorted(fp)]


class Ctx:
    """Facts for one poll, read lazily: the runner lease and the digest file (whose
    `last_ping_fp` is the last delivered ping's fingerprint)."""

    def __init__(self, cfg, reader, *, lease=None, digest=None, now=None):
        self.cfg, self.reader = cfg, reader
        self.now = now or dt.datetime.now().astimezone()
        self._lease = lease
        self.digest = digest if digest is not None else Digest.load(cfg, reader)

    @property
    def lease(self) -> dict:
        if self._lease is None:
            self._lease = L.lease_status(self.cfg, now=self.now)
        return self._lease


def _drain_hold(cfg, data) -> bool:
    budget = E.settings(cfg)["gate_budget_s"]
    waits = [w.get("timeout_s") or budget for w in data.get("waiting_on") or []
             if isinstance(w, dict)]
    bound = max(waits, default=budget) + _GRACE_S
    return float(data.get("draining_s") or 0) <= bound


def _silent_hold(ev, ctx) -> bool:
    """A gate or close process for the campaign is alive in the runner."""
    lease, cid = ctx.lease, ev.get("campaign")
    if not lease.get("held") or not cid:
        return False
    if cid in (lease.get("in_flight") or ()):
        return True
    busy = lease.get("busy")
    if isinstance(busy, dict) and busy.get("campaign") == cid:
        age = E._age_s(busy.get("since"), ctx.now)
        return age is not None and age <= float(busy.get("budget_s") or 0)
    return False


def backlog_resolved(data) -> bool:
    """True for a `backlog` event that only restates a ready item or an ordinary dependency
    wait (`waiting on SPEC-X (...)`, `spec status is ...`)."""
    if data.get("ready"):
        return True
    blocked = data.get("blocked_by") or []
    return all(isinstance(b, str) and b.startswith(_BENIGN_BLOCKED) for b in blocked)


def classify(cfg, ev, ctx) -> str:
    """`"wake"` or `"hold"` for one event. Unknown or new types wake."""
    t, data = ev.get("type"), ev.get("data") or {}
    rule = RULES.get(t, "wake")
    if rule != "cond":
        return "hold" if rule == "hold" else "wake"
    if t == "conductor_ping":
        fp = ping_fingerprint(data)
        return "hold" if not fp or fp == ctx.digest.last_ping_fp else "wake"
    if t == "runner_draining":
        return "hold" if _drain_hold(cfg, data) else "wake"
    if t in ("campaign_silent", "state_stuck"):
        return "hold" if _silent_hold(ev, ctx) else "wake"
    if t == "backlog":
        return "hold" if backlog_resolved(data) else "wake"
    return "wake"


# ---- the pending digest ---------------------------------------------------------------------

def _fresh() -> dict:
    return {"first_id": None, "last_id": 0, "since": None, "until": None, "held": {},
            "campaigns": {}, "merged": [], "pings_held": 0, "ping_items": []}


class Digest:
    """Held events waiting for their digest line, persisted at `cursors/<reader>.digest.json`
    (atomically) so it survives the follower's exits. `last_ping_fp` and `last_printed_last_id`
    outlive `clear()`."""

    def __init__(self, path: Path, rec: dict | None = None):
        self.path = path
        rec = rec or {}
        self.pending = {**_fresh(), **{k: rec[k] for k in _fresh() if k in rec}}
        self.last_ping_fp = list(rec.get("last_ping_fp") or [])
        self.last_printed_last_id = int(rec.get("last_printed_last_id") or 0)

    @classmethod
    def load(cls, cfg, reader) -> "Digest":
        path = E.cursors_dir(cfg) / f"{E._safe(reader)}.digest.json"
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = None
        return cls(path, rec if isinstance(rec, dict) else None)

    def save(self) -> None:
        rec = {**self.pending, "last_ping_fp": self.last_ping_fp,
               "last_printed_last_id": self.last_printed_last_id}
        _atomic_write(self.path, json.dumps(rec, ensure_ascii=False) + "\n")

    @property
    def empty(self) -> bool:
        return self.pending["first_id"] is None

    def add(self, ev) -> bool:
        """Fold one held event in. False (a no-op) when its id is not past what the digest has
        seen or printed: a re-read after a crash between the digest and cursor writes."""
        p, eid = self.pending, ev["id"]
        if eid <= max(p["last_id"], self.last_printed_last_id):
            return False
        t = ev["type"]
        if p["first_id"] is None:
            p["first_id"], p["since"] = eid, ev.get("ts")
        p["last_id"], p["until"] = eid, ev.get("ts")
        p["held"][t] = p["held"].get(t, 0) + 1
        cid = ev.get("campaign")
        if cid and len(p["campaigns"].setdefault(t, [])) < 10 and cid not in p["campaigns"][t]:
            p["campaigns"][t].append(cid)
        if t in ("merged", "handed_off"):
            p["merged"].append({"campaign": cid, "spec_id": ev.get("spec_id"),
                                "label": ev.get("label") or ev.get("spec_id") or cid})
        if t == "conductor_ping":
            p["pings_held"] += 1
            p["ping_items"] = ping_fingerprint(ev.get("data") or {})
            if not p["ping_items"]:
                self.last_ping_fp = []          # all clear: the same items later are news
        return True

    def note_ping_wake(self, ev) -> None:
        self.last_ping_fp = ping_fingerprint(ev.get("data") or {})

    def due(self, now, interval) -> bool:
        if self.empty:
            return False
        age = E._age_s(self.pending["since"], now)
        return age is not None and age >= float(interval)

    def line(self, *, as_json=True) -> str:
        data = {k: v for k, v in self.pending.items()}
        E.validate("conductor_digest", data)
        ev = {"schema": E.SCHEMA, "id": None, "ts": store.now_iso(), "type": "conductor_digest",
              "campaign": None, "spec_id": None, "project": None, "by": "follower",
              "data": data}
        return (json.dumps(ev, ensure_ascii=False) if as_json else E.summary_line(ev)) + "\n"

    def clear(self) -> None:
        """Reset the pending fields only; remember the last id printed."""
        self.last_printed_last_id = max(self.last_printed_last_id, self.pending["last_id"])
        self.pending = _fresh()


# ---- counting -----------------------------------------------------------------------------

def deliveries_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "conductor" / "deliveries.jsonl"


def record_delivery(cfg, reader, ev, action) -> None:
    """Append one delivery record (`action`: wake | hold | digest) under a flock."""
    path = deliveries_path(cfg)
    rec = {"ts": store.now_iso(), "reader": reader, "event_id": ev.get("id"),
           "type": ev.get("type"), "campaign": ev.get("campaign"), "action": action}
    with E._locked(path.with_suffix(".lock")):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_deliveries(cfg, reader) -> list[dict]:
    path = deliveries_path(cfg)
    out = []
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("reader") == reader and rec.get("ts"):
                out.append(rec)
    return out


def _in_window(ts, since, until) -> bool:
    day = str(ts)[:10]
    return (since is None or day >= since) and (until is None or day <= until)


def wakeups_report(cfg, *, since=None, until=None, reader="conductor") -> dict:
    """`wt.conductor.wakeups.v1`: delivered lines (wake events, digests, `code_changed`),
    held events and wakeups per merge over a window of days (`YYYY-MM-DD`, inclusive)."""
    by_type: dict[str, dict] = {}
    by_day: dict[str, dict] = {}

    def day(ts):
        return by_day.setdefault(str(ts)[:10], {"day": str(ts)[:10], "delivered": 0, "held": 0,
                                                "merges": 0})
    delivered = held = digests = 0
    for rec in _read_deliveries(cfg, reader):
        if not _in_window(rec["ts"], since, until):
            continue
        row = by_type.setdefault(rec.get("type"), {"wake": 0, "hold": 0})
        if rec.get("action") == "hold":
            held += 1
            row["hold"] += 1
            day(rec["ts"])["held"] += 1
        else:
            delivered += 1
            row["wake"] += 1
            day(rec["ts"])["delivered"] += 1
            digests += rec.get("action") == "digest"
    merges = 0
    for ev in E.read_since(cfg, 0, types=["merged", "handed_off"]):
        if _in_window(ev.get("ts") or "", since, until):
            merges += 1
            day(ev["ts"])["merges"] += 1
    return {"schema": WAKEUPS_SCHEMA, "reader": reader, "since": since, "until": until,
            "delivered": delivered, "held": held, "digests": digests, "by_type": by_type,
            "merges": merges,
            "wakeups_per_merge": round(delivered / merges, 2) if merges else None,
            "by_day": [by_day[d] for d in sorted(by_day)]}
