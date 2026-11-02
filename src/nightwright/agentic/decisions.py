"""The conductor's decision log (SPEC-0301): `<agentic_dir>/conductor/decisions.jsonl`, one
structured record per conductor or queue decision, so an audit can tell which rule of the
`/nw-conduct` decision table a decision applied and whether every park got one.

A decision on a campaign also lands as a ledger `note`, so `queue.park_view`, `nw campaign log`
and the TUI see it unchanged. Pure files; no clock except in `record` / `parse_since`."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
from pathlib import Path

from . import events as E
from . import store

SCHEMA = "nw.conductor.decision.v1"
KINDS = ("resume", "abandon", "escalate", "ask_steward", "dispatch", "restart_runner", "wait",
         "reconcile", "apply_answer", "merge", "no_action", "other")
# up to two qualifier segments: the landed nw-conduct row `message.request.human_order` (SPEC-0382)
RULE_RE = re.compile(r"^[a-z_]+(\.[a-z_]+){0,2}$")
BY_RE = re.compile(r"^(conductor|queue|steward:[A-Za-z0-9_.-]+)$")
_REL_RE = re.compile(r"^(\d+)([smhd])$")
_REL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def log_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "conductor" / "decisions.jsonl"


def parse_ts(value) -> dt.datetime | None:
    """An ISO stamp as an aware datetime (a naive one is read as UTC); None when unparseable."""
    try:
        ts = dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


def parse_since(text, now=None) -> dt.datetime:
    """`2h` / `1d` / `30m` / `90s` (relative to `now`) or an ISO date / datetime (a naive one is
    local time). ValueError otherwise."""
    text = (text or "").strip()
    m = _REL_RE.match(text)
    if m:
        now = now or dt.datetime.now().astimezone()
        return now - dt.timedelta(seconds=int(m.group(1)) * _REL_UNITS[m.group(2)])
    try:
        ts = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"bad --since {text!r}: expected an ISO date/datetime or 2h / 1d")
    return ts if ts.tzinfo else ts.astimezone()


def _read_all(cfg) -> list[dict]:
    path = log_path(cfg)
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("seq"), int):
            out.append(rec)
    return out


def read(cfg, *, campaign=None, since=None) -> list[dict]:
    """Records in file order; optionally of one campaign, and at or after `since` (datetime)."""
    out = _read_all(cfg)
    if campaign is not None:
        out = [r for r in out if r.get("campaign") == campaign]
    if since is not None:
        out = [r for r in out if (parse_ts(r.get("ts")) or since) >= since]
    return out


def needs_decision(by) -> bool:
    """SPEC-0318: a park, resume or abandon by anyone but a human carries a decision record."""
    return by != "human"


def validate_request(kind, rule, rationale, by) -> dict:
    """The checks of `record` that need no campaign; returns the request dict
    `{kind, rule, rationale, by}`. ValueError on any bad argument."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    if not isinstance(rule, str) or not RULE_RE.match(rule):
        raise ValueError(f"bad rule id {rule!r}: expected <event>[.<qualifier>] matching "
                         f"{RULE_RE.pattern}")
    if not isinstance(by, str) or not BY_RE.match(by):
        raise ValueError(f"bad --by {by!r}: conductor, queue or steward:<P>")
    rationale = (rationale or "").strip()
    if not rationale:
        raise ValueError("a decision needs a rationale")
    return {"kind": kind, "rule": rule, "rationale": rationale, "by": by}


def covering(cfg, campaign, park_ts) -> dict | None:
    """SPEC-0318: the first record of `campaign` with ts at or after `park_ts` (an ISO stamp);
    None when there is none (or the park's ts is unparseable)."""
    start = parse_ts(park_ts)
    if start is None:
        return None
    return next((r for r in read(cfg, campaign=campaign)
                 if (t := parse_ts(r.get("ts"))) is not None and t >= start), None)


def record(cfg, *, kind, rule, rationale, campaign=None, event_id=None, action=None,
           by="conductor") -> dict:
    """Validate, then append one decision. With a campaign the ledger note is appended first;
    the `decisions.jsonl` line follows only if that succeeded, so a failure leaves at most a plain
    note. The next `seq` is reserved under the log's flock before the note is written (the note
    carries it). ValueError on any bad argument, with nothing written."""
    validate_request(kind, rule, rationale, by)
    rationale = rationale.strip()
    spec_id = None
    if campaign is not None:
        state = store.read_state(cfg, campaign)          # ValueError: no such campaign
        spec_id = state.get("spec_id")
    if event_id is not None:
        nxt = E.read_since(cfg, event_id - 1)[:1] if event_id >= 1 else []
        if not nxt or nxt[0].get("id") != event_id:
            raise ValueError(f"no queue event {event_id}")
    path = log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name("decisions.jsonl.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        seq = max((r["seq"] for r in _read_all(cfg)), default=0) + 1
        if campaign is not None:
            store.append_event(cfg, campaign, "note", {
                "text": f"decision {kind} ({rule}): {rationale}", "author": by,
                "decision": {"seq": seq, "kind": kind, "rule": rule}}, by=f"agent:{by}")
        rec = {"schema": SCHEMA, "seq": seq, "ts": store.now_iso(), "by": by,
               "campaign": campaign, "spec_id": spec_id, "event_id": event_id, "rule": rule,
               "kind": kind, "action": action, "rationale": rationale}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return rec


def open_dispatches(records) -> list[dict]:
    """`dispatch` decisions with no later decision on the same campaign (or, for a dispatch
    without a campaign, no later campaign-less decision)."""
    latest: dict = {}
    for r in records:
        latest[r.get("campaign")] = max(latest.get(r.get("campaign"), 0), r["seq"])
    return [r for r in records if r.get("kind") == "dispatch" and latest[r.get("campaign")] == r["seq"]]


def match_parks(parks, records) -> list[tuple]:
    """Pair each park with its covering decision. `parks` is `[(ts, event)]` and `records`
    `[(ts, decision)]`, both of one campaign. A park is covered by the first decision with ts >=
    the park's and < the next park's, so one decision covers at most one park. Returns `[(ts,
    event, decision | None)]` in park order."""
    parks = sorted(parks, key=lambda p: p[0])
    out = []
    for i, (t, e) in enumerate(parks):
        upper = parks[i + 1][0] if i + 1 < len(parks) else None
        hit = next((r for dt_, r in records if dt_ >= t and (upper is None or dt_ < upper)), None)
        out.append((t, e, hit))
    return out


def park_coverage(cfg, *, since=None) -> dict:
    """`{parks, covered, missing, ratio}` over every `park` ledger event in the window. A park is
    covered by the first decision of its campaign with ts >= the park's and < the next park's, so
    one decision covers at most one park. Without `since` the window starts at the first
    decision record (nothing before the log existed can be missing); an empty log has no parks."""
    records = _read_all(cfg)
    start = since
    if start is None:
        stamps = [t for t in (parse_ts(r.get("ts")) for r in records) if t is not None]
        if not stamps:
            return {"parks": 0, "covered": 0, "missing": [], "ratio": None, "items": []}
        start = stamps[0]
    by_campaign: dict = {}
    for r in records:
        t = parse_ts(r.get("ts"))
        if r.get("campaign") and t is not None:
            by_campaign.setdefault(r["campaign"], []).append((t, r))
    items = []
    for st in store.list_campaigns(cfg, include_terminal=True):
        cid = st["id"]
        parks = [(parse_ts(e.get("ts")), e) for e in store.read_ledger(cfg, cid, types=["park"])]
        parks = [(t, e) for t, e in parks if t is not None]
        for t, e, hit in match_parks(parks, by_campaign.get(cid, [])):
            if t < start:
                continue
            items.append({"campaign": cid, "ts": e.get("ts"), "reason": e.get("reason"),
                          "decision": hit})
    items.sort(key=lambda x: (x["ts"] or "", x["campaign"]))
    covered = sum(1 for x in items if x["decision"])
    return {"parks": len(items), "covered": covered,
            "missing": [{"campaign": x["campaign"], "ts": x["ts"], "reason": x["reason"]}
                        for x in items if not x["decision"]],
            "ratio": covered / len(items) if items else None, "items": items}
