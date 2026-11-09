"""Conductor cost measurement (SPEC-0299): which Claude Code session(s) are the conductor, what
their transcripts say they cost, and how that splits over the campaigns live at the time.

Pure functions over files. The registry (`<agentic_dir>/conductor/sessions.jsonl`) is filled by
`events.follow` (the follower inherits `CLAUDE_CODE_SESSION_ID`) or by hand. Prices are
configuration (`agentic.conductor.prices`); no built-in table ships."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import statistics
from pathlib import Path

from . import store

SCHEMA = "nw.conductor.cost.v1"
KINDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
MERGE_STATES = frozenset({"MERGED", "HANDED_OFF"})
TOP_CAMPAIGNS = 10


# ---- session registry ----------------------------------------------------------------------

def conductor_dir(cfg) -> Path:
    return store.agentic_dir(cfg) / "conductor"


def _sessions_path(cfg) -> Path:
    return conductor_dir(cfg) / "sessions.jsonl"


def sessions(cfg, reader="conductor") -> list[dict]:
    """Registered sessions of `reader`, first seen first. Unparseable lines are skipped."""
    path = _sessions_path(cfg)
    if not path.is_file():
        return []
    out = []
    try:
        with path.open("rb") as stream:
            while True:
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if isinstance(rec, dict) and rec.get("session_id") and rec.get("reader") == reader:
                    out.append(rec)
    except OSError:
        return []
    return out


def record_session(cfg, reader, env=os.environ, *, source="follower", session_id=None,
                   extra=None, refresh=False) -> dict | None:
    """Append `{ts, reader, session_id, claude_pid, cwd, source}` unless the `(reader,
    session_id)` pair is already recorded. Returns the record, or None when there is no
    session id (env var unset) or the pair is already there. `extra` adds fields to the record.
    With `refresh`, an already recorded pair gets `extra` merged into its row in place (a
    resumed launch re-records its effective argv, SPEC-0382) and the row is returned. Writes
    under a flock."""
    sid = session_id or env.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    d = conductor_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / "sessions.jsonl.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if any(r["session_id"] == sid for r in sessions(cfg, reader)):
            return _refresh_row(cfg, reader, sid, extra or {}) if refresh else None
        pid = env.get("CLAUDE_PID")
        rec = {"ts": store.now_iso(), "reader": reader, "session_id": sid,
               "claude_pid": int(pid) if str(pid or "").isdigit() else None,
               "cwd": os.getcwd(), "source": source, **(extra or {})}
        with open(_sessions_path(cfg), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _refresh_row(cfg, reader, sid, extra) -> dict | None:
    """Merge `extra` into the `(reader, sid)` rows of sessions.jsonl (caller holds the flock);
    the file is replaced atomically. Returns the last updated row."""
    path = _sessions_path(cfg)
    lines, out = [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            lines.append(raw)
            continue
        if isinstance(rec, dict) and rec.get("session_id") == sid and rec.get("reader") == reader:
            rec = out = {**rec, **extra}
            raw = json.dumps(rec, ensure_ascii=False)
        lines.append(raw)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return out


# ---- herdr placement (SPEC-0361) -----------------------------------------------------------

def _herdr_panes_path(cfg) -> Path:
    return conductor_dir(cfg) / "herdr_panes.json"


def _start_ticks(pid) -> int | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def record_herdr_pane(cfg, reader, env=os.environ) -> dict | None:
    """When this follower runs inside a herdr pane (`HERDR_ENV=1`), record where:
    `herdr_panes.json[reader] = {pane_id, workspace_id, tab_id, session_id, pid, start_ticks,
    ts}` (newest wins). Reads only the env; never calls herdr. Returns the record or None."""
    if env.get("HERDR_ENV") != "1" or not env.get("HERDR_PANE_ID"):
        return None
    pid = os.getpid()
    rec = {"pane_id": env.get("HERDR_PANE_ID"), "workspace_id": env.get("HERDR_WORKSPACE_ID"),
           "tab_id": env.get("HERDR_TAB_ID"), "session_id": env.get("CLAUDE_CODE_SESSION_ID"),
           "pid": pid, "start_ticks": _start_ticks(pid), "ts": store.now_iso()}
    d = conductor_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / "sessions.jsonl.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = herdr_panes(cfg)
        data[reader] = rec
        path = _herdr_panes_path(cfg)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return rec
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def herdr_panes(cfg) -> dict:
    """`{reader: placement}` as recorded (live or not); {} when absent or unreadable."""
    try:
        data = json.loads(_herdr_panes_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def placement_live(rec) -> bool:
    """The follower that recorded `rec` still runs (same pid, same /proc start time)."""
    pid = rec.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    ticks = _start_ticks(pid)
    return rec.get("start_ticks") is None or ticks is None or ticks == rec.get("start_ticks")


# ---- transcripts ---------------------------------------------------------------------------

def _safe_id(session_id) -> bool:
    """A session id is a file stem: no path separators or glob metacharacters."""
    sid = str(session_id or "")
    return bool(sid) and sid not in (".", "..") and not any(c in sid for c in "/\\*?[]")


def _project_dirs(cfg) -> list[Path]:
    """The direct children of the transcripts root that are directories, sorted."""
    root = cfg.get("_root")
    if not root:
        return []
    try:
        return sorted(p for p in Path(root).iterdir() if p.is_dir())
    except OSError:
        return []


def find_transcript(cfg, session_id) -> Path | None:
    """`<root>/<project>/<session_id>.jsonl` by exact lookup (no glob: the id is never a pattern)."""
    if not _safe_id(session_id):
        return None
    return next((f for d in _project_dirs(cfg) if (f := d / f"{session_id}.jsonl").is_file()), None)


def subagent_transcripts(cfg, session_id) -> list[Path]:
    if not _safe_id(session_id):
        return []
    out = []
    for d in _project_dirs(cfg):
        sub = d / str(session_id) / "subagents"
        if sub.is_dir():
            out.extend(sub.glob("**/*.jsonl"))
    return sorted(out)


def _parse_ts(value):
    try:
        ts = dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def usage_kinds(usage: dict) -> dict:
    """Token counts of one API `usage` object by price kind (`KINDS`) plus `context`."""
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        w5, w1 = _int(cc.get("ephemeral_5m_input_tokens")), _int(cc.get("ephemeral_1h_input_tokens"))
    else:
        w5, w1 = _int(usage.get("cache_creation_input_tokens")), 0
    turn = {"input": _int(usage.get("input_tokens")), "output": _int(usage.get("output_tokens")),
            "cache_read": _int(usage.get("cache_read_input_tokens")),
            "cache_write_5m": w5, "cache_write_1h": w1}
    turn["context"] = turn["input"] + turn["cache_read"] + w5 + w1
    return turn


def _lines(path, *needles):
    """The lines of a transcript that contain one of `needles`, read one at a time (a transcript
    is hundreds of MB; the tool results and user lines are never turns). An `OSError` mid-read
    ends the stream, so the caller keeps what it had."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                if any(n in raw for n in needles):
                    yield raw
    except OSError:
        return


def read_turns(path) -> list[dict]:
    """API turns of one transcript, one per `message.id` (the last line seen wins; one message
    is split over a line per content block). `context` is the prompt size of the turn."""
    by_id: dict[str, dict] = {}
    for raw in _lines(path, '"assistant"'):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict) or msg.get("model") == "<synthetic>":
            continue
        ts = _parse_ts(rec.get("timestamp"))
        if ts is None:
            continue
        turn = {"ts": ts, "model": msg.get("model"), "msg_id": msg.get("id"), **usage_kinds(usage)}
        by_id[msg.get("id") or f"line-{len(by_id)}-{raw[:40]}"] = turn
    return list(by_id.values())


# ---- prices --------------------------------------------------------------------------------

def _valid_price(entry) -> bool:
    """A price entry is a dict whose present kinds are all plain numbers; a typo in the hand-written
    config makes the model unpriced rather than breaking every report and export."""
    return isinstance(entry, dict) and all(
        isinstance(entry[k], (int, float)) and not isinstance(entry[k], bool)
        and math.isfinite(entry[k]) and entry[k] >= 0
        for k in KINDS if entry.get(k) is not None)


def _conductor_cfg(cfg) -> dict:
    """`agentic.conductor` when each level is a dict, else `{}`."""
    agentic = cfg.get("agentic") if isinstance(cfg, dict) else None
    cond = agentic.get("conductor") if isinstance(agentic, dict) else None
    return cond if isinstance(cond, dict) else {}


def price_for(cfg, model) -> dict | None:
    """The price entry for `model`: longest matching prefix of `agentic.conductor.prices`."""
    prices = _conductor_cfg(cfg).get("prices")
    if not isinstance(prices, dict):
        return None
    keys = [k for k in prices if _valid_price(prices[k]) and str(model or "").startswith(str(k))]
    return prices[max(keys, key=len)] if keys else None


def turn_usd(turn, price) -> float | None:
    """USD for one turn at `price` (per million tokens), or None when the model has no price."""
    if price is None:
        return None
    return sum(turn[k] * float(price.get(k) or 0) for k in KINDS) / 1_000_000


def _kind_usd(turn, price) -> dict:
    return {k: turn[k] * float(price.get(k) or 0) / 1_000_000 for k in KINDS}


# ---- live windows --------------------------------------------------------------------------

def live_windows(cfg, *, now=None) -> list[dict]:
    """One row per campaign: `{campaign, spec_id, start, end, live_end, outcome, persona_usd}`. A
    campaign is live from its `open` event (or `created_at`) until its terminal transition (`end`
    None while there is none); the same window `campaign_export.campaign_row` uses. `live_end` is
    what `_live_at` reads: `end` when there is one; for a campaign in a terminal state with no
    terminal transition (a legacy or hand-repaired one) `max(start, min(updated_at, last
    transition))`, so it stops absorbing spend instead of staying live forever (`end` stays None,
    so it is still not counted as ended or merged); None for a campaign still running."""
    out = []
    for s in store.list_campaigns(cfg, include_terminal=True):
        cid = s["id"]
        events = store.read_ledger(cfg, cid, types=["open", "transition"])
        start = next((_parse_ts(e.get("ts")) for e in events if e.get("type") == "open"), None) \
            or _parse_ts(s.get("created_at"))
        term = next((e for e in reversed(events) if e.get("type") == "transition"
                     and e.get("to") in store.TERMINAL_STATES), None)
        end = _parse_ts(term.get("ts")) if term else None
        live_end = end
        if end is None and s.get("state") in store.TERMINAL_STATES:
            last = next((_parse_ts(e.get("ts")) for e in reversed(events)
                         if e.get("type") == "transition"), None)
            bounds = [t for t in (_parse_ts(s.get("updated_at")), last) if t is not None]
            live_end = max(start, min(bounds)) if bounds and start else start
        out.append({"campaign": cid, "spec_id": s.get("spec_id"), "start": start, "end": end,
                    "live_end": live_end,
                    "outcome": term.get("to") if term else None,
                    "persona_usd": ((s.get("cost") or {}).get("total_usd")) or 0.0})
    return out


def _live_at(windows, t) -> list[dict]:
    return [w for w in windows if w["start"] is not None and w["start"] <= t
            and (w["live_end"] is None or t < w["live_end"])]


def _turns_of(cfg, paths, seen=None) -> list[dict]:
    """Priced turns of `paths`, sorted by time. A message id seen in an earlier transcript (a
    resumed session replays its history) is skipped; `seen` carries ids across calls."""
    out = []
    seen = set() if seen is None else seen
    for p in paths:
        for t in read_turns(p):
            if t["msg_id"] is not None:
                if t["msg_id"] in seen:
                    continue
                seen.add(t["msg_id"])
            price = price_for(cfg, t["model"])
            out.append({**t, "usd": turn_usd(t, price), "_price": price})
    return sorted(out, key=lambda t: t["ts"])


def turns_between(cfg, start, end) -> list[dict]:
    """Every turn of the registered conductor sessions (main transcripts only, message ids
    de-duplicated as `_turns_of` does) with ts in `[start, end]`, time ordered. Each carries
    `usd`, None when its model has no price."""
    paths = [p for r in sessions(cfg) if (p := find_transcript(cfg, r["session_id"]))]
    return [t for t in _turns_of(cfg, paths) if start <= t["ts"] <= end]


BRIEF_CMD = "nw queue conductor-brief"


def transcript_marks(path, replayed=()):
    """`(ts, kind, assistant_turn_index)` for the `compact_boundary` lines of one transcript
    (kind `compact_boundary`; the index is that of the next assistant turn) and for the Bash
    tool calls whose command contains `nw queue conductor-brief` (kind `brief`; the index is
    that of the turn making the call). Assistant turns are counted by distinct `message.id`.
    Turns whose id is in `replayed` (a resumed session's copy of earlier history) are not
    counted, nor are the marks in that replayed stretch (before the first new turn, which a
    resumed transcript may open with a copied `compact_boundary`)."""
    out, seen = [], {}
    for raw in _lines(path, '"assistant"', "compact_boundary"):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_ts(rec.get("timestamp"))
        if ts is None:
            continue
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            if not (replayed and not seen):
                out.append((ts, "compact_boundary", len(seen)))
        elif rec.get("type") == "assistant" and isinstance(rec.get("message"), dict):
            msg = rec["message"]
            if msg.get("model") == "<synthetic>":
                continue
            if msg.get("id") in replayed:
                continue
            key = msg.get("id") or f"line-{len(seen)}"
            idx = seen.setdefault(key, len(seen))
            content = msg.get("content")
            for block in content if isinstance(content, list) else ():
                cmd = (block.get("input") or {}).get("command") if isinstance(block, dict) \
                    and block.get("type") == "tool_use" and block.get("name") == "Bash" \
                    and isinstance(block.get("input"), dict) else None
                if isinstance(cmd, str) and BRIEF_CMD in cmd:
                    out.append((ts, "brief", idx))
    return out


def _split(turns, windows):
    """Equal split of each turn's USD over the campaigns live at its time:
    `({campaign: usd}, idle_usd)`. Unpriced turns contribute nothing."""
    shares: dict[str, float] = {}
    idle = 0.0
    for t in turns:
        if t["usd"] is None:
            continue
        live = _live_at(windows, t["ts"])
        if not live:
            idle += t["usd"]
            continue
        for w in live:
            shares[w["campaign"]] = shares.get(w["campaign"], 0.0) + t["usd"] / len(live)
    return shares, idle


def attribute(cfg, now=None) -> dict | None:
    """Campaign id → conductor USD over every registered conductor session, for the campaigns
    export. None when no session is registered or no turn is priced; otherwise a campaign
    with no attributed turn is simply absent (the caller reads it as 0.0)."""
    reg = sessions(cfg)
    if not reg:
        return None
    paths = [p for r in reg if (p := find_transcript(cfg, r["session_id"]))]
    turns = _turns_of(cfg, paths)
    if not any(t["usd"] is not None for t in turns):
        return None
    shares, _ = _split(turns, live_windows(cfg, now=now))
    return shares


# ---- the report ----------------------------------------------------------------------------

def _pct(values, q):
    s = sorted(values)
    return s[min(len(s) - 1, max(0, -(-len(s) * q // 100) - 1))]


def _date_range(since, until):
    def ok(d):
        return (since is None or d >= since) and (until is None or d <= until)
    return ok


def _totals(turns) -> dict:
    priced = [t for t in turns if t["usd"] is not None]
    by_kind = {k: 0.0 for k in KINDS}
    for t in priced:
        for k, v in _kind_usd(t, t["_price"]).items():
            by_kind[k] += v
    return {"turns": len(turns), **{k: sum(t[k] for t in turns) for k in KINDS},
            "usd": sum(t["usd"] for t in priced), "usd_complete": len(priced) == len(turns),
            "unpriced_turns": len(turns) - len(priced),
            "usd_by_kind": by_kind}


def _round(obj, nd=6):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v, nd) for v in obj]
    return obj


def cost_report(cfg, *, since=None, until=None, session_ids=None, include_subagents=False,
                now=None) -> dict:
    """The `nw.conductor.cost.v1` payload. `since` / `until` are inclusive `datetime.date`s,
    compared with local dates in `cfg["_tz"]`; omitted bounds are open. `session_ids` overrides
    the registry."""
    now = now or dt.datetime.now().astimezone()
    tz = cfg.get("_tz") or dt.timezone.utc
    in_window = _date_range(since, until)

    def local_day(ts):
        return ts.astimezone(tz).date()

    ids = list(dict.fromkeys(session_ids if session_ids else
                             [r["session_id"] for r in sessions(cfg)]))
    rows, main_paths, sub_paths = [], [], []
    for sid in ids:
        path = find_transcript(cfg, sid)
        rows.append({"session_id": sid, "path": str(path) if path else None,
                     "found": path is not None, "turns": 0})
        if path:
            main_paths.append((rows[-1], path))
        if include_subagents:
            sub_paths.extend(subagent_transcripts(cfg, sid))
    turns = []
    seen: set = set()
    for row, path in main_paths:
        got = [t for t in _turns_of(cfg, [path], seen) if in_window(local_day(t["ts"]))]
        row["turns"] = len(got)
        turns.extend(got)
    turns.sort(key=lambda t: t["ts"])

    windows = live_windows(cfg, now=now)
    shares, idle = _split(turns, windows)
    ended = [w for w in windows if w["end"] is not None and in_window(local_day(w["end"]))]
    merged = [w for w in ended if w["outcome"] in MERGE_STATES]
    totals = _totals(turns)

    by_day: dict[str, dict] = {}
    for t in turns:
        d = by_day.setdefault(local_day(t["ts"]).isoformat(),
                              {"turns": 0, "usd": 0.0, "cache_read": 0, "merges": 0})
        d["turns"] += 1
        d["usd"] += t["usd"] or 0.0
        d["cache_read"] += t["cache_read"]
    for w in merged:
        by_day.setdefault(local_day(w["end"]).isoformat(),
                          {"turns": 0, "usd": 0.0, "cache_read": 0, "merges": 0})["merges"] += 1

    ctx = [t["context"] for t in turns]
    camps = [{"campaign": w["campaign"], "spec_id": w["spec_id"], "outcome": w["outcome"],
              "conductor_usd": shares.get(w["campaign"], 0.0),
              "persona_usd": w["persona_usd"]}
             for w in windows if w["campaign"] in shares or w in ended]
    camps.sort(key=lambda c: (-c["conductor_usd"], c["campaign"]))

    sub = None
    if include_subagents:
        sub_turns = [t for t in _turns_of(cfg, sub_paths) if in_window(local_day(t["ts"]))]
        sub = {"transcripts": len(sub_paths), **_totals(sub_turns)}

    return _round({
        "schema": SCHEMA,
        "sessions": rows,
        "window": {"since": since.isoformat() if since else None,
                   "until": until.isoformat() if until else None},
        "totals": totals,
        "unpriced_models": sorted({t["model"] or "?" for t in turns if t["usd"] is None}),
        "context": {"median": statistics.median(ctx) if ctx else None,
                    "p90": _pct(ctx, 90) if ctx else None, "max": max(ctx) if ctx else None,
                    "last": ctx[-1] if ctx else None},
        "by_day": [{"date": d, **v} for d, v in sorted(by_day.items())],
        "merges": len(merged),
        "usd_per_merge": totals["usd"] / len(merged) if merged else None,
        "idle_usd": idle,
        "persona_usd": sum(w["persona_usd"] for w in ended),
        "campaigns": camps,
        "subagents": sub,
    })
