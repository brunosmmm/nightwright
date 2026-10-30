"""The MVP scorecard (SPEC-0304): exit criteria E1-E6 of SPEC-0298 over the last N merged
campaigns, from the files SPEC-0299/0300/0301 and the campaign store already write.

Pure reads. The window is a set of campaigns, so every source is cut at the datetime bounds of
that set (`[start, end]`), not at whole days."""
from __future__ import annotations

import datetime as dt
import statistics

from . import conductor_cost as CC
from . import decisions as D
from . import escalations as ESC
from . import events as E
from . import project as P
from . import queue as Q
from . import quiet
from . import store

SCHEMA = "wt.conductor.mvp.v1"
MEASUREMENT_SPECS = ("SPEC-0299", "SPEC-0300", "SPEC-0301")
DEFAULT_WINDOW = 20
DEFAULT_TARGETS = {"e1": 1.0, "e2": 150_000, "e3": 5, "e4": 1.0, "e5": 1, "e6": 1}
NAMES = {"e1": "conductor USD per merge", "e2": "median conductor context (tokens)",
         "e3": "delivered lines per merge", "e4": "parks decided before resume",
         "e5": "PR-policy campaign merged", "e6": "re-orientations oriented and clean"}
OPS = {"e1": "<=", "e2": "<=", "e3": "<=", "e4": ">=", "e5": ">=", "e6": ">="}
ORIENT_TURNS = 10
CLEAN_AFTER = dt.timedelta(minutes=15)
PR_POLICIES = ("pr", "draft-pr")


def settings(cfg) -> dict:
    """The targets: `agentic.conductor.mvp.targets` over `DEFAULT_TARGETS`; a value that is not
    a non-negative number falls back to its default."""
    given = ((((cfg.get("agentic") or {}).get("conductor") or {}).get("mvp") or {})
             .get("targets")) or {}
    out = dict(DEFAULT_TARGETS)
    for k in out:
        v = given.get(k) if isinstance(given, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = v
    return out


def _iso(ts) -> str | None:
    return ts.astimezone(dt.timezone.utc).isoformat() if ts else None


def _crit(cid, targets, value, status, detail="") -> dict:
    return {"id": cid.upper(), "name": NAMES[cid], "value": value, "target": targets[cid],
            "op": OPS[cid], "status": status, "detail": detail}


def _cmp(value, op, target) -> bool:
    return value <= target if op == "<=" else value >= target


# ---- the window ----------------------------------------------------------------------------

def merged_campaigns(cfg, *, project=None) -> list[dict]:
    """Campaigns whose terminal transition is MERGED or HANDED_OFF, by that transition's ts:
    `{campaign, spec_id, project, start, end, outcome}`."""
    states = {s["id"]: s for s in store.list_campaigns(cfg, include_terminal=True)}
    out = []
    for w in CC.live_windows(cfg):
        st = states.get(w["campaign"]) or {}
        if w["end"] is None or w["outcome"] not in CC.MERGE_STATES or w["start"] is None:
            continue
        if project and st.get("project") != project:
            continue
        out.append({"campaign": w["campaign"], "spec_id": w["spec_id"],
                    "project": st.get("project"), "start": w["start"], "end": w["end"],
                    "outcome": w["outcome"]})
    out.sort(key=lambda c: (c["end"], c["campaign"]))
    return out


def pick_window(merged, *, n, since=None) -> dict:
    """The last `n` of `merged` (opened at or after `since` when given) and its interval."""
    chosen = [c for c in merged if since is None or c["start"] >= since][-n:]
    return {"requested": n, "campaigns": chosen,
            "start": min((c["start"] for c in chosen), default=None),
            "end": max((c["end"] for c in chosen), default=None)}


def qualifying_point(all_merged):
    """`(latest terminal ts of the merged SPEC-0299/0300/0301 campaigns, [spec ids not merged])`."""
    ends, missing = [], []
    for sid in MEASUREMENT_SPECS:
        mine = [c["end"] for c in all_merged if c["spec_id"] == sid]
        if mine:
            ends.append(max(mine))
        else:
            missing.append(sid)
    return (max(ends) if ends else None), missing


def qualifying(window, all_merged) -> tuple[bool, str | None]:
    point, missing = qualifying_point(all_merged)
    why = []
    if missing:
        why.append("not merged yet: " + ", ".join(missing))
    if len(window["campaigns"]) < window["requested"]:
        why.append(f"{len(window['campaigns'])} of {window['requested']} merged campaigns")
    if not missing:
        early = [c["campaign"] for c in window["campaigns"] if c["start"] <= point]
        if early:
            why.append(f"opened before {_iso(point)}: " + ", ".join(early))
    return not why, "; ".join(why) or None


# ---- E1 - E3 -------------------------------------------------------------------------------

def e1_e2(cfg, start, end, merges, targets) -> tuple[dict, dict]:
    turns = CC.turns_between(cfg, start, end)
    if not turns:
        return (_crit("e1", targets, None, "no_data", "no conductor turns in the window"),
                _crit("e2", targets, None, "no_data", "no conductor turns in the window"))
    unpriced = [t for t in turns if t["usd"] is None]
    usd = sum(t["usd"] for t in turns if t["usd"] is not None)
    per_merge = usd / merges
    if unpriced:
        status = "incomplete"
        detail = f"{len(unpriced)} of {len(turns)} turns have no price"
    else:
        status = "pass" if _cmp(per_merge, "<=", targets["e1"]) else "fail"
        detail = f"${usd:,.2f} over {merges} merges"
    median = statistics.median(t["context"] for t in turns)
    return (_crit("e1", targets, round(per_merge, 6), status, detail),
            _crit("e2", targets, median, "pass" if _cmp(median, "<=", targets["e2"]) else "fail",
                  f"{len(turns)} turns"))


def e3(cfg, start, end, merges, targets) -> dict:
    stamped = [(CC._parse_ts(r["ts"]), r) for r in quiet.read_deliveries(cfg, "conductor")]
    stamped = [(t, r) for t, r in stamped if t is not None]
    if not any(t <= end for t, _ in stamped):
        return _crit("e3", targets, None, "no_data", "no delivery recorded before the window ends")
    delivered = sum(1 for t, r in stamped if start <= t <= end and r.get("action") != "hold")
    value = delivered / merges
    return _crit("e3", targets, round(value, 6),
                 "pass" if _cmp(value, "<=", targets["e3"]) else "fail",
                 f"{delivered} delivered over {merges} merges")


# ---- E4 ------------------------------------------------------------------------------------

def e4(cfg, campaigns, start, end, targets) -> tuple[dict, list[dict]]:
    by_campaign: dict = {}
    for r in D.read(cfg):
        t = D.parse_ts(r.get("ts"))
        if r.get("campaign") and t is not None:
            by_campaign.setdefault(r["campaign"], []).append((t, r))
    total, covered, bad = 0, 0, []
    for c in campaigns:
        cid = c["campaign"]
        led = store.read_ledger(cfg, cid, types=["park", "transition"])
        parks = [(D.parse_ts(e.get("ts")), e) for e in led if e.get("type") == "park"]
        parks = [(t, e) for t, e in parks if t is not None]
        for t, e, hit in D.match_parks(parks, by_campaign.get(cid, [])):
            if not start <= t <= end:
                continue
            total += 1
            status = None
            if hit is None:
                status = "missing"
            else:
                exit_ts = next((D.parse_ts(x.get("ts")) for x in led
                                if x.get("type") == "transition" and x.get("from") == "PARKED"
                                and (x.get("seq") or 0) > (e.get("seq") or 0)), None)
                if exit_ts is not None and D.parse_ts(hit.get("ts")) > exit_ts:
                    status = "late"
            if status:
                bad.append({"campaign": cid, "reason": e.get("reason"), "ts": e.get("ts"),
                            "status": status})
            else:
                covered += 1
    if not total:
        return _crit("e4", targets, None, "no_data", "no parks in the window"), bad
    value = covered / total
    return (_crit("e4", targets, round(value, 6),
                  "pass" if _cmp(value, ">=", targets["e4"]) else "fail",
                  f"{covered} of {total} parks decided before the campaign left PARKED"), bad)


# ---- E5 ------------------------------------------------------------------------------------

def _pr_policy(cfg, project, cache) -> str | None:
    if project not in cache:
        try:
            cache[project] = (P.project_config(cfg, project).get("merge") or {}).get("policy")
        except ValueError:
            cache[project] = None
    return cache[project]


def e5(cfg, point, targets) -> tuple[dict, list[dict]]:
    """Merged PR-policy campaigns opened at or after `point` (no lower bound when None)."""
    policies: dict = {}
    escs = ESC.list_all(cfg)
    rows = []
    for st in store.list_campaigns(cfg, include_terminal=True):
        close = st.get("close") or {}
        if st.get("state") not in CC.MERGE_STATES or close.get("pr_state") != "MERGED":
            continue
        if _pr_policy(cfg, st.get("project"), policies) not in PR_POLICIES:
            continue
        cid = st["id"]
        led = store.read_ledger(cfg, cid, types=["open", "park"])
        opened = next((CC._parse_ts(e.get("ts")) for e in led if e.get("type") == "open"), None) \
            or CC._parse_ts(st.get("created_at"))
        if point is not None and (opened is None or opened < point):
            continue
        touch = [{"kind": "park", "reason": e.get("reason"), "ts": e.get("ts")}
                 for e in led if e.get("type") == "park" and Q.park_owner(e.get("reason")) == "human"]
        touch += [{"kind": "escalation", "id": r.get("id"), "question": r.get("question")}
                  for r in escs if r.get("campaign") == cid]
        clean = not any(t["kind"] == "park" and t["reason"] != "human_decision" for t in touch)
        rows.append({"campaign": cid, "project": st.get("project"), "spec_id": st.get("spec_id"),
                     "pr_url": close.get("pr_url"), "outcome": st.get("state"),
                     "touchpoints": touch, "status": "pass" if clean else "fail"})
    rows.sort(key=lambda r: r["campaign"])
    passing = sum(1 for r in rows if r["status"] == "pass")
    detail = (f"{passing} of {len(rows)} merged PR campaigns without a human-owned park"
              if rows else "no merged PR-policy campaign")
    return _crit("e5", targets, passing, "pass" if _cmp(passing, ">=", targets["e5"]) else "fail",
                 detail), rows


# ---- E6 ------------------------------------------------------------------------------------

def e6(cfg, start, end, targets) -> tuple[dict, list[dict]]:
    sess = []
    for r in CC.sessions(cfg):
        path = CC.find_transcript(cfg, r["session_id"])
        turns = CC.read_turns(path) if path else []
        if turns:
            sess.append((min(t["ts"] for t in turns), r["session_id"], path))
    sess.sort(key=lambda s: s[0])
    undecided = [t for e in E.read_since(cfg, 0, types=["park_undecided"])
                 if (t := CC._parse_ts(e.get("ts"))) is not None]
    events = []
    for i, (first, sid, path) in enumerate(sess):
        marks = CC.transcript_marks(path)
        briefs = [idx for _, kind, idx in marks if kind == "brief"]
        found = [(ts, "compaction", idx) for ts, kind, idx in marks
                 if kind == "compact_boundary" and start <= ts <= end]
        if i > 0 and start <= first <= end:
            found.append((first, "restart", 0))
        for ts, kind, idx in found:
            events.append({
                "session_id": sid, "ts": _iso(ts), "kind": kind,
                "oriented": any(0 <= b - idx < ORIENT_TURNS for b in briefs),
                "clean": not any(ts < u <= ts + CLEAN_AFTER for u in undecided)})
    events.sort(key=lambda x: x["ts"])
    if not events:
        return _crit("e6", targets, 0, "no_data", "no compaction or restart in the window"), events
    good = sum(1 for x in events if x["oriented"] and x["clean"])
    status = "pass" if len(events) >= targets["e6"] and good == len(events) else "fail"
    return _crit("e6", targets, len(events), status,
                 f"{sum(x['oriented'] for x in events)} oriented, "
                 f"{sum(x['clean'] for x in events)} clean of {len(events)}"), events


# ---- the report ----------------------------------------------------------------------------

def report(cfg, *, n=DEFAULT_WINDOW, since=None, project=None) -> dict:
    """The `wt.conductor.mvp.v1` payload."""
    targets = settings(cfg)
    all_merged = merged_campaigns(cfg)
    pool = [c for c in all_merged if not project or c["project"] == project]
    win = pick_window(pool, n=n, since=since)
    start, end, camps = win["start"], win["end"], win["campaigns"]
    ok_q, why = qualifying(win, all_merged)
    point, _ = qualifying_point(all_merged)
    merges = sum(1 for c in all_merged if start is not None and start <= c["end"] <= end)
    if camps:
        c1, c2 = e1_e2(cfg, start, end, merges, targets)
        c3 = e3(cfg, start, end, merges, targets)
        c4, undecided = e4(cfg, camps, start, end, targets)
        c6, ev6 = e6(cfg, start, end, targets)
    else:
        c1, c2, c3, c4, c6 = (_crit(k, targets, None, "no_data", "no merged campaign in the window")
                              for k in ("e1", "e2", "e3", "e4", "e6"))
        undecided, ev6 = [], []
    c5, ev5 = e5(cfg, point, targets)
    criteria = [c1, c2, c3, c4, c5, c6]
    return {"schema": SCHEMA,
            "window": {"n": len(camps), "requested": n, "start": _iso(start), "end": _iso(end),
                       "merges": merges, "qualifying": ok_q, "qualifying_reason": why,
                       "campaigns": [c["campaign"] for c in camps]},
            "criteria": criteria, "parks_undecided": undecided, "e5_campaigns": ev5,
            "e6_events": ev6,
            "ok": ok_q and all(c["status"] == "pass" for c in criteria)}


def _fmt(v) -> str:
    if v is None:
        return "-"
    return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"


def render_text(rep) -> str:
    rows = [("id", "criterion", "value", "target", "status")] + [
        (c["id"], c["name"], _fmt(c["value"]), f"{c['op']} {_fmt(c['target'])}", c["status"])
        for c in rep["criteria"]]
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in rows]
    w = rep["window"]
    lines += ["", f"window: {w['n']} of {w['requested']} campaigns, {w['start'] or '-'} .. "
                  f"{w['end'] or '-'}, {w['merges']} merges, "
                  + ("qualifying" if w["qualifying"] else f"not qualifying ({w['qualifying_reason']})")]
    for c in rep["criteria"]:
        if c["detail"]:
            lines.append(f"{c['id']}: {c['detail']}")
    if rep["parks_undecided"]:
        lines += ["", "parks without a timely decision:"] + [
            f"  {p['ts']}  {p['campaign']}  {p['reason']}  {p['status']}"
            for p in rep["parks_undecided"]]
    lines.append("")
    lines.append("ok" if rep["ok"] else "not ok")
    return "\n".join(lines)
