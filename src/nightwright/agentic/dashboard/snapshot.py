"""`nw.dashboard.v1` (SPEC-0401): one document with everything the remote dashboard shows,
built only from the existing read-only payload builders, plus the stat-only `watermark` and the
lazy shared `SnapshotCache` the server (a later sibling spec) serves from.

Never calls `events.sync`, `events.follow`, `read_since(cfg, 0)`, `conduct_status`,
`store.write_state`, a mailbox write or any queue mutation (SPEC-0400 invariant 1)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time

from .. import escalations as ESC
from .. import events as E
from .. import lease as L
from .. import store
from . import settings

SCHEMA = "nw.dashboard.v1"
TOP_KEYS = ("schema", "generated_at", "rev", "health", "totals", "slots", "gate_pools",
            "active", "needs_you", "conductor_handling", "waiting", "backlog", "delivered",
            "recent_events", "inbox")
RECENT_TYPES = ("merged", "handed_off", "abandoned", "run_failed", "parked", "resumed",
                "escalation", "escalation_answered", "escalation_withdrawn", "runner_dead",
                "lease_stale", "cost_warning", "worker_failed")
RECENT_N = 20
RECENT_WINDOW = 256 * 1024
BACKLOG_TOP = 5
HUMAN_PARKS = ("main_dirty", "main_not_on_base")
# Keys `dashboard_payload` derives from "now" without the `_age_s` suffix: they move with the
# clock alone, so `rev` ignores them like the `*_age_s` keys (As shipped, SPEC-0401).
NOW_RELATIVE_KEYS = frozenset({"age_s", "elapsed_s", "held_s", "waiting_s", "waited_s"})
_CAMPAIGN_FILES = ("state.json", "ledger.jsonl")


# ---- watermark ---------------------------------------------------------------------------

def _stat(path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def campaigns_signature(cfg) -> tuple:
    """Sorted (campaign_id, file_name, size, mtime_ns) of each campaign's `state.json` and
    `ledger.jsonl`; `stat()` calls only. A missing file counts as None."""
    d = store.campaigns_dir(cfg)
    out = []
    try:
        dirs = [p for p in d.iterdir() if p.is_dir()] if d.is_dir() else []
    except OSError:
        dirs = []
    for c in dirs:
        for name in _CAMPAIGN_FILES:
            s = _stat(c / name)
            out.append((c.name, name, *(s or (None, None))))
    return tuple(sorted(out, key=lambda r: (r[0], r[1])))


def watermark(cfg) -> tuple:
    """Cheap change signal (Decision 3): the event projection, the escalation records, the
    lease file and the campaign records. A ledger append moves it before any follower syncs
    `events.jsonl`. A live runner's heartbeat moves the lease mtime, so the cache then rebuilds
    once per `refresh_s`; `rev` stays stable when nothing else changed."""
    lease = _stat(L.lease_path(cfg))
    return (_stat(E.events_path(cfg)), ESC.signature(cfg),
            lease[1] if lease else None, campaigns_signature(cfg))


# ---- rev ---------------------------------------------------------------------------------

def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items()
                if not (isinstance(k, str) and (k.endswith("_age_s") or k in NOW_RELATIVE_KEYS))}
    if isinstance(obj, (list, tuple)):
        return [_strip(v) for v in obj]
    return obj


def compute_rev(doc: dict) -> str:
    """First 16 hex chars of the sha256 of the canonical JSON of `doc` without `generated_at`,
    `rev` and every age key, at any depth: an age-only rebuild keeps the same rev."""
    body = _strip({k: v for k, v in doc.items() if k not in ("generated_at", "rev")})
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---- recent events -----------------------------------------------------------------------

def _summary(ev: dict) -> str:
    label = ev.get("label") or ev.get("spec_id") or ev.get("campaign") or "-"
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    tail = data.get("reason") or data.get("state")
    return f"{ev.get('type')} {label}" + (f" {tail}" if tail else "")


def recent_events(cfg, n: int = RECENT_N) -> list[dict]:
    """The last `n` events of `RECENT_TYPES` from the tail (last 256 KiB) of `events.jsonl`,
    in file order. Never reads the whole log."""
    path = E.events_path(cfg)
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            start = max(0, size - RECENT_WINDOW)
            f.seek(start)
            raw = f.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if start > 0:
        lines = lines[1:]                          # the first line is (maybe) partial
    keep = set(RECENT_TYPES)
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") not in keep:
            continue
        out.append({"id": ev.get("id"), "ts": ev.get("ts"), "type": ev.get("type"),
                    "campaign": ev.get("campaign"), "spec_id": ev.get("spec_id"),
                    "project": ev.get("project"), "by": ev.get("by"), "summary": _summary(ev)})
    return out[-n:] if n > 0 else []


# ---- build -------------------------------------------------------------------------------

def _error(e: BaseException) -> dict:
    return {"error": f"{type(e).__name__}: {e}"}


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:                          # noqa: BLE001 - one section, not the doc
        return _error(e)


def _failed(v) -> bool:
    return isinstance(v, dict) and set(v) == {"error"}


def _conductor(cfg, rows) -> dict:
    row = next((r for r in rows if r.get("reader") == "conductor"), None) \
        if not _failed(rows) else None
    return {"pid": E.reader_holder(cfg, "conductor"),
            "lag": (row or {}).get("lag"), "cursor_age_s": (row or {}).get("cursor_age_s")}


def _stewards(rows) -> list[dict]:
    if _failed(rows):
        return rows
    return [{"address": r["reader"][len("mailbox:"):], "pid": r.get("pid"), "lag": r.get("lag")}
            for r in rows if r.get("kind") == "mailbox" and r.get("held")]


def _herdr_watch(cfg):
    from ..cli_conduct import _herdr_watch_status
    return _herdr_watch_status(cfg)


def _first_line(text) -> str:
    return str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""


def _escalations(cfg) -> list[dict]:
    return [{"id": r.get("id"), "created": r.get("created"), "by": r.get("by"),
             "kind": r.get("kind"), "question": _first_line(r.get("question")),
             "campaign": r.get("campaign"), "spec_id": r.get("spec_id"),
             "project": r.get("project")}
            for r in ESC.list_all(cfg, "open")]


def _dp_section(dp, fn):
    """A section derived from `dashboard_payload`: its error when the payload failed."""
    return dp if _failed(dp) else _safe(fn, dp)


def build(cfg, now=None) -> dict:
    """One `nw.dashboard.v1` document. A builder that raises turns its section into
    `{"error": "<Type>: <message>"}`; the rest still builds."""
    from .. import queue as Q
    now = now or dt.datetime.now().astimezone()
    dp = _safe(Q.dashboard_payload, cfg, now=now)
    rows = _safe(E.followers, cfg, now=now)

    def runner(dp):
        lease = dp.get("lease")
        return {"lease": lease, "runner_code": dp.get("runner_code"),
                "draining": (lease or {}).get("draining")}

    def backlog(dp):
        rows_ = dp.get("backlog") or []
        return {"count": len(rows_), "top": rows_[:BACKLOG_TOP]}

    def parks(dp):
        return [r for r in dp.get("needs_you") or []
                if ((r.get("park") or {}).get("reason")) in HUMAN_PARKS]

    doc = {
        "schema": SCHEMA, "generated_at": now.astimezone(dt.timezone.utc).isoformat(
            timespec="seconds"), "rev": None,
        "health": {"runner": _dp_section(dp, runner),
                   "conductor": _safe(_conductor, cfg, rows),
                   "stewards": _stewards(rows),
                   "herdr_watch": _safe(_herdr_watch, cfg)},
        "totals": _dp_section(dp, lambda d: d["totals"]),
        "slots": _dp_section(dp, lambda d: d["slots"]),
        "gate_pools": _dp_section(dp, lambda d: d["gate_pools"]),
        "active": _dp_section(dp, lambda d: d["active"]),
        "needs_you": _dp_section(dp, lambda d: d["needs_you"]),
        "conductor_handling": _dp_section(dp, lambda d: d["conductor_handling"]),
        "waiting": _dp_section(dp, lambda d: d["waiting"]),
        "backlog": _dp_section(dp, backlog),
        "delivered": _dp_section(dp, lambda d: d["delivered"]),
        "recent_events": _safe(recent_events, cfg),
        "inbox": {"escalations": _safe(_escalations, cfg), "parks": _dp_section(dp, parks)},
    }
    doc["rev"] = compute_rev(doc)
    return doc


# ---- cache -------------------------------------------------------------------------------

class SnapshotCache:
    """Lazy and shared (Decision 4): no thread, no work without `get()`. A `get()` checks the
    watermark at most once per `refresh_s` and rebuilds only when it moved or the snapshot is
    older than `max_stale_s`. One lock: concurrent callers wait for a single build. Callers
    share the returned dict and must not mutate it."""

    def __init__(self, cfg, clock=time.monotonic, builder=build, *, refresh_s=None,
                 max_stale_s=None, watermark_fn=watermark):
        sett = settings(cfg)
        self.cfg = cfg
        self.clock = clock
        self.builder = builder
        self.watermark_fn = watermark_fn
        self.refresh_s = sett["refresh_s"] if refresh_s is None else refresh_s
        self.max_stale_s = sett["max_stale_s"] if max_stale_s is None else max_stale_s
        self.builds = 0
        self.watermark_checks = 0
        self._lock = threading.Lock()
        self._cached = None
        self._wm = None
        self._built_at = None
        self._last_check = None

    def _build(self, t, wm=None) -> None:
        # the watermark is taken before the build: a change during it is seen at the next check
        self._wm = self.watermark_fn(self.cfg) if wm is None else wm
        self._cached = self.builder(self.cfg)
        self.builds += 1
        self._built_at = self._last_check = t

    def get(self) -> dict:
        with self._lock:
            t = self.clock()
            if self._cached is None:
                self.watermark_checks += 1
                self._build(t)
            elif t - self._built_at >= self.max_stale_s:
                self._build(t)
            elif t - self._last_check >= self.refresh_s:
                self._last_check = t
                self.watermark_checks += 1
                wm = self.watermark_fn(self.cfg)
                if wm != self._wm:
                    self._build(t, wm)
            return self._cached
