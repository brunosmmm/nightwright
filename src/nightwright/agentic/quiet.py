"""Quiet conductor (SPEC-0300): the conductor's event follower holds informational events,
folds them into a periodic `conductor_digest` line and counts every delivered line, so a wakeup
is spent on decisions. Nothing here changes what `events.sync` appends: `events.jsonl`, `nw queue
events` history, the TUI and steward followers are untouched. Unknown event types wake."""
from __future__ import annotations

import datetime as dt
import json
import math
import logging
import re
from pathlib import Path

from ..export import _atomic_write
from . import events as E
from . import gate_slots
from . import lease as L
from . import queue
from . import store

log = logging.getLogger(__name__)

DIGEST_SCHEMA = "nw.conductor.digest.v1"
WAKEUPS_SCHEMA = "nw.conductor.wakeups.v1"

# type -> "hold" | "wake" | "cond" (decided from facts at delivery time). The skill's *Quiet
# stream* paragraph mirrors the hold and cond lists; a test compares them. Any type missing
# here wakes.
RULES = {
    "admitted": "hold", "resumed": "hold", "waiting": "hold", "abandoned": "hold",
    "cost_warning": "hold", "escalation": "hold", "escalation_withdrawn": "hold",
    "rebase_resolved": "hold", "run_failed": "hold", "merged": "hold", "handed_off": "hold",
    # SPEC-0305: informational events of the idea-campaign spec phase (SPEC-0217)
    "spec_written": "hold", "spec_reviewed": "hold", "spec_accepted": "hold",
    # SPEC-0309: a gate waited over 30 s for a host-wide gate slot (informational)
    "gate_waiting": "hold",
    # SPEC-0338: a remote gate run fell back to local (the fallback already ran the gates)
    "worker_failed": "hold",
    # SPEC-0341: a remote gate job was given up on (the gate then ran locally)
    "worker_job_lost": "hold",
    # SPEC-0342: a lost job's next attempt (another worker, or local); a queue-wide note
    "worker_job_retried": "hold", "note": "hold",
    # SPEC-0343: a drained worker was disabled by the queue (informational)
    "worker_drained": "hold",
    # SPEC-0386: a scheduled worker update finished (held); one that failed / timed out wakes
    "worker_updated": "hold", "worker_update_failed": "wake",
    # SPEC-0381: a standalone gate job (no campaign) was dispatched or finished (informational)
    "gate_job": "hold",
    # SPEC-0422: the cost-cap policy raised a converging campaign's cap (informational)
    "cap_raised": "hold",
    # SPEC-0420: red-base episodes; the queue acts on them (an escalation arrives on its own)
    "red_main": "hold", "red_main_fix_opened": "hold", "red_main_cleared": "hold",
    "close_held": "hold",
    # SPEC-0493: a finished cadence job run (red_main carries any episode it opens)
    "cadence": "hold",
    # SPEC-0345: a rate-limit backoff episode began (dispatch resumes on its own at `until`)
    "rate_limited": "hold",
    "backlog": "cond", "backlog_blocked_long": "cond", "conductor_ping": "cond",
    "runner_draining": "cond", "campaign_silent": "cond", "state_stuck": "cond", "parked": "cond",
    "park_undecided": "wake", "runner_dead": "wake", "lease_stale": "wake",
    "run_overdue": "wake", "message": "wake", "escalation_answered": "wake",
    "escalation_timeout": "wake",       # SPEC-0425: the conductor auto-applies the recommendation
    "ready_not_admitted": "wake", "waiting_not_admitted": "wake",
    "runner_journal_silent": "wake", "temp_pressure": "wake", "runner_config_invalid": "wake",
    "code_changed": "wake", "auto_resume_failed": "wake",
    "launch_stale": "wake",                      # SPEC-0424: a relaunch request
}

DEFAULTS = {"enabled": True, "readers": ["conductor"], "digest_interval_s": 1800}

# a `backlog` event is held when it only restates an ordinary dependency wait; any other
# `blocked_by` text (a missing spec, a missing branch ...) wakes, see `backlog_resolved`.
# `backlog_blocked_long` and the ping also require a live campaign per dependency (SPEC-0306)
_BENIGN_BLOCKED = ("waiting on ", "mailed conductor: ", "spec status is draft",
                   "spec status is proposed", "spec status is in-progress")
_CAMPAIGN_WAIT_RE = re.compile(
    r"^waiting on (\S+) \(after: campaign \1 is (\S+)\)$")
_ID_RE = re.compile(r"[A-Za-z]+-\d+(?:-\d{8}-[0-9a-f]{4})?")   # + campaign ids (SPEC-0423)
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

def _benign(entry) -> bool:
    return isinstance(entry, str) and entry.startswith(_BENIGN_BLOCKED)


def waited_ids(entry: str) -> list[str]:
    """The dependency ids a `waiting on ...` entry waits for: the ids after its last `waiting
    on `, so a superseded spec names its replacements, which are what actually block."""
    return _ID_RE.findall(entry.rsplit("waiting on ", 1)[-1])


def dependency_wait(blocked_by, live_specs, project=None) -> bool:
    """True when `blocked_by` is a non-empty list of ordinary dependency waits and every
    `waiting on` entry names only dependencies with a live campaign in `project`
    (`live_specs` holds `(project, spec_id)` pairs; spec ids are per repository). A missing
    project has no live dependency."""
    if not isinstance(blocked_by, list) or not blocked_by:
        return False
    live = {s for p, s in live_specs if p == project} if project is not None else set()
    for b in blocked_by:
        if not _benign(b):
            return False
        if b.startswith("waiting on "):
            m = _CAMPAIGN_WAIT_RE.match(b)
            if m:
                cid, st = m.group(1), m.group(2)
                if st == "HANDED_OFF" or cid in live:
                    continue
            ids = waited_ids(b)
            if not ids or not set(ids) <= live:
                return False
    return True


def ping_fingerprint(data: dict, live_specs=frozenset()) -> list:
    """Sorted `[category, campaign]` pairs of what in a `conductor_ping` needs action. A
    blocked item that only waits on dependencies being built (`dependency_wait`) is not."""
    fp = set()
    for cat in ("blocked", "stuck", "ready_not_admitted", "waiting_not_admitted"):
        for it in data.get(cat) or []:
            if cat == "blocked" and dependency_wait(it.get("blocked_by"), live_specs,
                                                    it.get("project")):
                continue
            fp.add((cat, str(it.get("campaign"))))
    for it in data.get("revise_pending") or []:                       # SPEC-0497
        fp.add(("revise_pending", str(it.get("spec_id"))))
    for it in data.get("parked") or []:
        if it.get("owner") == "conductor" and not it.get("decided"):
            fp.add(("parked", str(it.get("campaign"))))
    runner = data.get("runner")
    if runner:
        fp.add(("runner", str(runner.get("pid"))))
    return [list(p) for p in sorted(fp)]


def _ping_fp(data, ctx) -> list:
    """`ping_fingerprint` with the live specs, scanned only when the ping lists blocked items."""
    return ping_fingerprint(data, ctx.live_specs if ctx and data.get("blocked") else frozenset())


class Ctx:
    """Facts for one poll, read lazily: the runner lease and the digest file (whose
    `last_ping_fp` is the last delivered ping's fingerprint)."""

    def __init__(self, cfg, reader, *, lease=None, digest=None, now=None, live_specs=None,
                 closing_waiters=None, states=None):
        self.cfg, self.reader = cfg, reader
        self.now = now or dt.datetime.now().astimezone()
        self._lease = lease
        self._live_specs = live_specs
        self._closing_waiters = closing_waiters
        self._states = states
        self._rows = None
        self.digest = digest if digest is not None else Digest.load(cfg, reader)

    @property
    def lease(self) -> dict:
        if self._lease is None:
            self._lease = L.lease_status(self.cfg, now=self.now)
        return self._lease

    @property
    def rows(self) -> list:
        """One `store.list_campaigns` scan, shared by `states` and `closing_waiters`."""
        if self._rows is None:
            self._rows = store.list_campaigns(self.cfg)
        return self._rows

    @property
    def live_specs(self) -> frozenset:
        """`(project, spec_id)` of every campaign in a non-terminal state (something is
        building it)."""
        if self._live_specs is None:
            live = set()
            for c in store.list_campaigns(self.cfg):
                if not c.get("project"):
                    continue
                live.add((c.get("project"), c.get("spec_id")) if c.get("spec_id") else None)
                live.add((c.get("project"), c.get("id")))              # SPEC-0423: `after` refs
                if (c.get("idea") or {}).get("id"):
                    live.add((c.get("project"), c["idea"]["id"]))
            self._live_specs = frozenset(x for x in live if x)
        return self._live_specs

    @property
    def closing_waiters(self) -> dict:
        """Campaign id -> head id for every campaign waiting in the close train behind a live
        CLOSING head (SPEC-0314)."""
        if self._closing_waiters is None:
            rows = self.rows
            ids = {r["id"] for r in rows if r.get("state") == "CLOSING"}
            self._closing_waiters = {
                r["id"]: r["closing"]["queued_behind"] for r in rows
                if E.waits_in_close_train(r, ids)}
        return self._closing_waiters

    @property
    def states(self) -> dict:
        """Campaign id -> `{state, gate_exclusive, updated_at, created_at}` (SPEC-0398);
        `gate_exclusive` is whether the SPEC-0311 exclusive re-gate marker is set."""
        if self._states is None:
            self._states = {
                r["id"]: {"state": r.get("state"), "gate_exclusive": bool(queue.gate_exclusive(r)),
                          "updated_at": r.get("updated_at"), "created_at": r.get("created_at")}
                for r in self.rows}
        return self._states


_CLOCK_FIELD = {"campaign_silent": "silent_s", "state_stuck": "stuck_s"}


def _watch_age(ev, now) -> float:
    """Seconds a held silent/stuck campaign has been so: the event's own clock plus the time
    since it was raised."""
    clock = (ev.get("data") or {}).get(_CLOCK_FIELD.get(ev.get("type"), ""))
    return float(clock if _num(clock) else 0) + (E._age_s(ev.get("ts"), now) or 0)


def _drain_bound(cfg, data) -> float:
    """Seconds a drain may last before it needs action: its slowest wait plus the grace."""
    budget = E.settings(cfg)["gate_budget_s"]
    waits = [w.get("timeout_s") or budget for w in data.get("waiting_on") or []
             if isinstance(w, dict)]
    return max(waits, default=budget) + _GRACE_S


def _drain_hold(cfg, data) -> bool:
    return float(data.get("draining_s") or 0) <= _drain_bound(cfg, data)


def _silent_hold(ev, ctx) -> bool:
    """A gate or close process for the campaign is alive in the runner, or it waits in the
    close train behind a head for which that holds (SPEC-0314), or it waits for / holds a gate
    slot, or its gate is deferred behind a drain or another gate (SPEC-0398)."""
    lease, cid = ctx.lease, ev.get("campaign")
    if not lease.get("held") or not cid:
        return False
    return (_alive(lease, cid, ctx) or _alive(lease, ctx.closing_waiters.get(cid), ctx)
            or _gate_active(cid, ctx) or _gate_deferred(cid, lease, ctx))


def _gate_active(cid, ctx) -> bool:
    """SPEC-0398: a live gate-slot waiter marker, or a live slot hold within the gate budget
    plus grace (a missing or unparsable `since` does not hold). A retry that keeps the same
    hold is measured from that hold's `since`."""
    if gate_slots.waiting(ctx.cfg, cid):
        return True
    hold = gate_slots.holding(ctx.cfg, cid)
    if not hold:
        return False
    age = E._age_s(hold.get("since"), ctx.now)
    return age is not None and age <= E.settings(ctx.cfg)["gate_budget_s"] + _GRACE_S


def _gate_deferred(cid, lease, ctx) -> bool:
    """SPEC-0398: a GATING campaign outside `in_flight` whose gate step is deferred by a drain
    within its window, or behind another gate by an exclusive re-gate (`select_gate_starts`)
    within `2 x gate_budget_s` plus grace."""
    me = ctx.states.get(cid)
    in_flight = lease.get("in_flight") or ()
    if not me or me.get("state") != "GATING" or cid in in_flight:
        return False
    draining = lease.get("draining")
    if isinstance(draining, dict):
        age = E._age_s(draining.get("since"), ctx.now)
        if age is not None and _drain_hold(
                ctx.cfg, {"waiting_on": draining.get("waiting_on"), "draining_s": age}):
            return True
    states = ctx.states
    if not any(v.get("state") == "GATING" and v.get("gate_exclusive") for v in states.values()):
        return False
    if not any(i != cid and (states.get(i) or {}).get("state") in ("GATING", "CLOSING")
               for i in in_flight):
        return False
    age = E._since_state(ctx.cfg, {"id": cid, **me}, ctx.now)
    return age is not None and age <= 2 * E.settings(ctx.cfg)["gate_budget_s"] + _GRACE_S


def _alive(lease, cid, ctx) -> bool:
    """The campaign has a step in the runner's pool, or is its fresh `busy` campaign."""
    if not cid:
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
    wait (`waiting on SPEC-X (...)`, or the queued spec still `draft` / `proposed` /
    `in-progress`). A spec that is missing, rejected, superseded or done can never become ready,
    so that text wakes."""
    if data.get("ready"):
        return True
    blocked = data.get("blocked_by") or []
    return all(_benign(b) for b in blocked)


def classify(cfg, ev, ctx) -> str:
    """`"wake"` or `"hold"` for one event. Unknown or new types wake."""
    t, data = ev.get("type"), ev.get("data") or {}
    rule = RULES.get(t, "wake")
    if rule != "cond":
        return "hold" if rule == "hold" else "wake"
    if t == "conductor_ping":
        fp = _ping_fp(data, ctx)
        return "hold" if not fp or fp == ctx.digest.last_ping_fp else "wake"
    if t == "runner_draining":
        return "hold" if _drain_hold(cfg, data) else "wake"
    if t in ("campaign_silent", "state_stuck"):
        return "hold" if _silent_hold(ev, ctx) else "wake"
    if t == "backlog":
        return "hold" if backlog_resolved(data) else "wake"
    if t == "backlog_blocked_long":
        return "hold" if dependency_wait(
            data.get("blocked_by"), ctx.live_specs, ev.get("project")) else "wake"
    if t == "parked":                       # SPEC-0302: a park the queue decided itself;
        # SPEC-0315: a park the conductor itself made (`nw queue park` -> reason human, decided;
        # `nw queue escalate` -> human_decision, human-owned): it already knows
        # SPEC-0420: a red-base fix's park (`red_main` set) is the queue's to decide
        return "hold" if data.get("auto") or data.get("red_main") \
            or ev.get("by") == "conductor" else "wake"
    return "wake"


# ---- the pending digest ---------------------------------------------------------------------

def _fresh() -> dict:
    return {"first_id": None, "last_id": 0, "since": None, "until": None, "held": {},
            "campaigns": {}, "merged": [], "pings_held": 0, "ping_items": []}


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str_or_none(v) -> bool:
    return v is None or isinstance(v, str)


def _pair_list(v) -> bool:
    return isinstance(v, list) and all(
        isinstance(x, list) and len(x) == 2 and all(isinstance(y, str) for y in x) for x in v)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _watch_ok(w) -> bool:
    ev = w.get("event") if isinstance(w, dict) else None
    return (isinstance(w, dict) and isinstance(w.get("key"), str)
            and isinstance(w.get("type"), str) and _num(w.get("bound_s"))
            and isinstance(ev, dict) and ev.get("type") in _CLOCK_FIELD
            and isinstance(ev.get("data", {}), dict))


_FIELD_OK = {"first_id": lambda v: v is None or _int(v), "last_id": _int, "since": _str_or_none,
             "until": _str_or_none,
             "held": lambda v: isinstance(v, dict) and all(
                 isinstance(k, str) and _int(n) for k, n in v.items()),
             "campaigns": lambda v: isinstance(v, dict) and all(
                 isinstance(k, str) and isinstance(c, list) and all(isinstance(x, str) for x in c)
                 for k, c in v.items()),
             "merged": lambda v: isinstance(v, list) and all(isinstance(m, dict) for m in v),
             "pings_held": _int, "ping_items": _pair_list, "last_ping_fp": _pair_list,
             "last_printed_last_id": _int,
             "drains": lambda v: isinstance(v, list) and all(
                 isinstance(d, dict) and isinstance(d.get("since"), str)
                 and isinstance(d.get("bound_s"), (int, float)) and isinstance(d.get("event"), dict)
                 for d in v),
             "watches": lambda v: isinstance(v, list) and all(_watch_ok(w) for w in v)}


def _well_typed(rec) -> bool:
    """False for a persisted digest with a wrong-typed field: that file is ignored."""
    return isinstance(rec, dict) and all(ok(rec[k]) for k, ok in _FIELD_OK.items() if k in rec)


class Digest:
    """Held events waiting for their digest line, persisted at `cursors/digests/<reader>.json`
    (atomically) so it survives the follower's exits. `last_ping_fp`, `last_printed_last_id`,
    `drains` (held `runner_draining` events still being watched, see `track_drain`) and
    `watches` (held `campaign_silent` / `state_stuck`, see `track_watch`) outlive `clear()`.
    A wrong-typed file is treated as absent."""

    def __init__(self, path: Path, rec: dict | None = None, legacy: Path | None = None):
        self.path = path
        self._legacy = legacy                   # a migrated `<reader>.digest.json` to remove
        self._unsaved = False
        rec = rec if _well_typed(rec) else {}
        self.drains = [dict(d) for d in rec.get("drains") or []]
        self.watches = [dict(w) for w in rec.get("watches") or []]
        self.pending = {**_fresh(), **{k: rec[k] for k in _fresh() if k in rec}}
        self.last_ping_fp = [list(p) for p in rec.get("last_ping_fp") or []]
        self.last_printed_last_id = rec.get("last_printed_last_id") or 0

    @classmethod
    def load(cls, cfg, reader) -> "Digest":
        base = E.cursors_dir(cfg)
        path = base / "digests" / f"{E._safe(reader)}.json"
        legacy = None
        rec = cls._read(path)
        if rec is None:                         # SPEC-0324: migrate the pre-`digests/` file
            old = base / f"{E._safe(reader)}.digest.json"
            rec = cls._read(old)
            if rec is not None and "after" not in rec:
                legacy = old
            else:
                rec = None                      # absent, or a reader's cursor: leave it alone
        return cls(path, rec, legacy)

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):           # unreadable, non-UTF-8, a directory, bad JSON
            return None
        return rec if isinstance(rec, dict) else None

    def retry_save(self) -> None:
        """Write again a state whose last `save` failed (the follower keeps one Digest in
        memory across polls, so nothing is lost meanwhile)."""
        if self._unsaved:
            self.save()

    def save(self) -> None:
        rec = {**self.pending, "last_ping_fp": self.last_ping_fp,
               "last_printed_last_id": self.last_printed_last_id, "drains": self.drains,
               "watches": self.watches}
        try:
            _atomic_write(self.path, json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:                    # e.g. a directory at the path: keep running
            if not self._unsaved:
                log.warning("quiet digest not saved to %s: %s", self.path, e)
            self._unsaved = True                # `follow` keeps this object; see `retry_save`
            return
        self._unsaved = False
        if self._legacy is not None:
            old, self._legacy = self._legacy, None
            if "after" not in (self._read(old) or {"after": 1}):
                old.unlink(missing_ok=True)

    def reset(self) -> None:
        """Forget everything seen: a follower replaying from cursor 0 must fold in every
        re-read hold."""
        self.pending, self.last_printed_last_id = _fresh(), 0
        self.last_ping_fp, self.drains, self.watches = [], [], []

    @property
    def empty(self) -> bool:
        return self.pending["first_id"] is None

    @property
    def worth_printing(self) -> bool:
        """SPEC-0317: a digest holding only `conductor_ping` events with no `ping_items` has
        nothing to act on; `follow` discards it instead of printing it."""
        held, p = self.pending["held"], self.pending
        return bool(p["ping_items"]) or any(t != "conductor_ping" for t in held)

    def add(self, ev, ctx=None) -> bool:
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
            p["ping_items"] = _ping_fp(ev.get("data") or {}, ctx)
            if not p["ping_items"]:
                self.last_ping_fp = []          # all clear: the same items later are news
        return True

    def track_drain(self, cfg, ev, lease, now=None) -> None:
        """Keep a held `runner_draining` so `drain_wakes` can wake once the drain outlives its
        window: the watchdog raises it once per drain, with `draining_s` just past the grace."""
        drain = lease.get("draining") if lease.get("held") else None
        since = drain.get("since") if isinstance(drain, dict) else None
        if not isinstance(since, str):
            return                              # the drain is already over: nothing to watch
        bound = _drain_bound(cfg, ev.get("data") or {})
        if now is not None:                     # a replay pass: an already-old event is history
            clock = (ev.get("data") or {}).get("draining_s")
            if (float(clock) if _num(clock) else 0) + (E._age_s(ev.get("ts"), now) or 0) > bound:
                return
        self.drains = [d for d in self.drains if d["since"] != since]
        self.drains.append({"since": since, "bound_s": bound, "event": ev})

    def drain_wakes(self, ctx) -> list[dict]:
        """Held drains that are over their window now: returned as `runner_draining` events
        (`draining_s` refreshed) and dropped, as are drains that ended. Reads the lease only
        when a drain is being watched."""
        if not self.drains:
            return []
        drain = ctx.lease.get("draining") if ctx.lease.get("held") else None
        cur = drain.get("since") if isinstance(drain, dict) else None
        woken, keep = [], []
        for d in self.drains:
            if d["since"] != cur:
                continue
            age = E._age_s(cur, ctx.now)
            if age is not None and age > d["bound_s"]:
                ev = dict(d["event"])
                ev["data"] = {**(ev.get("data") or {}), "draining_s": round(age)}
                woken.append(ev)
            else:
                keep.append(d)
        if woken or len(keep) != len(self.drains):
            self.drains = keep
            self.save()
        return woken

    def track_watch(self, cfg, ev, now=None) -> None:
        """Keep a held `campaign_silent` / `state_stuck` so `watch_wakes` can wake once the
        campaign has stayed in flight past the gate budget: the watchdog raises each once.
        With `now` (a replay pass) an event already past its bound is not kept: it is history."""
        key = f"{ev.get('type')}:{ev.get('campaign')}:{(ev.get('data') or {}).get('state')}"
        bound = E.settings(cfg)["gate_budget_s"] + _GRACE_S
        if now is not None and _watch_age(ev, now) > bound:
            return
        self.watches = [w for w in self.watches if w["key"] != key]
        self.watches.append({"key": key, "type": ev.get("type"), "bound_s": bound, "event": ev})

    def watch_wakes(self, ctx) -> list[dict]:
        """Held silent/stuck events whose campaign stayed in flight past the bound: returned
        (clock field refreshed) and dropped, as are watches whose campaign left flight. A
        watch kept only because the campaign waits behind a live close-train head is kept
        without waking. Reads the lease only when a watch exists."""
        if not self.watches:
            return []
        woken, keep = [], []
        for w in self.watches:
            ev = w["event"]
            if not _silent_hold(ev, ctx):
                continue
            field = _CLOCK_FIELD[ev.get("type")]
            data = ev.get("data") or {}
            age = _watch_age(ev, ctx.now)
            if age > w["bound_s"] and _alive(ctx.lease, ev.get("campaign"), ctx):
                woken.append({**ev, "data": {**data, field: round(age)}})
            else:
                keep.append(w)
        if woken or len(keep) != len(self.watches):
            self.watches = keep
            self.save()
        return woken

    def note_ping_wake(self, ev, ctx=None) -> None:
        self.last_ping_fp = _ping_fp(ev.get("data") or {}, ctx)

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


def read_deliveries(cfg, reader) -> list[dict]:
    """Delivery records of `reader` (with a `ts`), in file order."""
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


_read_deliveries = read_deliveries


def check_day(value, name="day") -> str | None:
    """`value` unchanged when it is `None` or a `YYYY-MM-DD` date; else `ValueError`."""
    if value is None:
        return None
    try:
        if len(str(value)) != 10:
            raise ValueError
        dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from None
    return str(value)


def _in_window(ts, since, until) -> bool:
    day = str(ts)[:10]
    return (since is None or day >= since) and (until is None or day <= until)


def wakeups_report(cfg, *, since=None, until=None, reader="conductor") -> dict:
    """`nw.conductor.wakeups.v1`: delivered lines (wake events, digests, `code_changed`),
    held events and wakeups per merge over a window of days (`YYYY-MM-DD`, inclusive)."""
    since, until = check_day(since, "--since"), check_day(until, "--until")
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
