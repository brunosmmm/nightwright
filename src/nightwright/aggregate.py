"""Aggregation: walk streams into per-day/per-topic seconds, work segments, and the
Claude-vs-you actor split; union intervals for wall-clock; merge live + archived days."""
import datetime as dt
from collections import defaultdict

from .dates import _parse_iso
from .history import read_history
from .ingest import load_all_streams
from .meetings import read_meetings
from .topics import RepoResolver, resolve_topic


def build(cfg, overrides, rr):
    """Returns (day_topic, day_intervals, segments, day_actor) keyed by BASE topic.
    day_actor splits transcript time into 'claude' (gap ending at an assistant/tool
    event = Claude generating) vs 'you' (gap ending at a typed prompt). Diagnostic only."""
    gap = cfg["gap_minutes"] * 60
    minb = cfg["min_block_seconds"]
    tz = cfg["_tz"]
    streams, _ = load_all_streams(cfg)
    day_topic = defaultdict(lambda: defaultdict(float))
    day_intervals = defaultdict(list)
    day_actor = defaultdict(lambda: defaultdict(float))
    segments = []
    for evs in streams:
        n = len(evs)
        cur = None
        prev = None
        for i, ev in enumerate(evs):
            nxt = evs[i + 1]["ts"] if i + 1 < n else None
            delta = (nxt - ev["ts"]).total_seconds() if nxt else None
            contrib = delta if (delta is not None and delta <= gap) else minb
            topic, unc = resolve_topic(ev, overrides, rr)
            day = ev["ts"].astimezone(tz).date().isoformat()
            day_topic[day][topic] += contrib
            day_intervals[day].append((ev["ts"], ev["ts"] + dt.timedelta(seconds=contrib)))
            if i + 1 < n:
                actor = "you" if evs[i + 1]["kind"] == "prompt" else "claude"
            else:
                actor = "you" if ev["kind"] == "prompt" else "claude"
            day_actor[day][actor] += contrib
            gprev = (ev["ts"] - prev["ts"]).total_seconds() if prev else None
            cont = (cur and cur["session"] == ev["session"] and cur["topic"] == topic
                    and gprev is not None and gprev <= gap)
            if cont:
                cur["end"] = ev["ts"]; cur["secs"] += contrib; cur["unc"] = cur["unc"] or unc
            else:
                if cur:
                    segments.append(cur)
                cur = {"session": ev["session"], "topic": topic, "start": ev["ts"],
                       "end": ev["ts"], "secs": contrib, "unc": unc,
                       "cwd": ev["cwd"], "branch": ev["branch"]}
            prev = ev
        if cur:
            segments.append(cur)
    # meetings
    for m in read_meetings(cfg):
        s = _parse_iso(m["start"]); e = _parse_iso(m["end"])
        secs = max((e - s).total_seconds(), 0)
        day = s.astimezone(tz).date().isoformat()
        day_topic[day]["MEETINGS"] += secs
        day_intervals[day].append((s, e))
        segments.append({"session": "MEETINGS", "topic": "MEETINGS", "start": s, "end": e,
                         "secs": secs, "unc": False, "cwd": None, "branch": m.get("subject")})
    return day_topic, day_intervals, segments, day_actor


def union_secs(intervals):
    if not intervals:
        return 0.0
    iv = sorted(intervals); tot = 0.0; cs, ce = iv[0]
    for s, e in iv[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            tot += (ce - cs).total_seconds(); cs, ce = s, e
    tot += (ce - cs).total_seconds()
    return tot


def assemble(cfg):
    """Merge live transcript data (full fidelity) with archived days (transcripts gone).
    Live wins for any day present in both. Returns ({day: {topics, wall, actor, src}},
    live_segments)."""
    from .rules import load_overrides
    overrides = load_overrides(cfg); rr = RepoResolver(cfg)
    day_topic, day_intervals, segments, day_actor = build(cfg, overrides, rr)
    live = set(day_topic)
    data = {}
    for d in day_topic:
        data[d] = {"topics": dict(day_topic[d]), "wall": union_secs(day_intervals[d]),
                   "actor": dict(day_actor[d]), "src": "live"}
    for rec in read_history(cfg):         # archived days (transcripts gone) — already base topics
        d = rec["day"]
        if d in live:
            continue                      # live transcripts win
        data[d] = {"topics": dict(rec.get("topics", {})), "wall": rec.get("wall", 0.0),
                   "actor": rec.get("actor", {}), "src": "archive"}
    return data, segments
