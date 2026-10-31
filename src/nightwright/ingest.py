"""Load transcript streams from Claude Code and Cursor into normalized event lists.

Each transcript file = one stream (session or sidechain). Each event is a dict
{ts, cwd, branch, session, uuid, kind}."""
import datetime as dt
import glob
import json
import os
import re

from .topics import _decode_cursor_project_path

# Cursor agent transcript timestamp: "Friday, Jun 19, 2026, 3:46 AM (UTC-4)"
_CURSOR_TS_RE = re.compile(
    r"<timestamp>[^,]+,\s+"             # "Friday, "
    r"([A-Za-z]+ \d+, \d+, "           # "Jun 19, 2026, "
    r"\d+:\d+ (?:AM|PM))"              # "3:46 AM"
    r"\s+\(UTC([+-]\d+)\)</timestamp>"  # " (UTC-4)"
)


def _parse_cursor_timestamp(text):
    """Extract and parse the <timestamp> injected by Cursor into each user message.
    Returns an aware datetime or None if no timestamp is found."""
    m = _CURSOR_TS_RE.search(text)
    if not m:
        return None
    try:
        base = dt.datetime.strptime(m.group(1), "%b %d, %Y, %I:%M %p")
        offset = dt.timezone(dt.timedelta(hours=int(m.group(2))))
        return base.replace(tzinfo=offset)
    except Exception:
        return None


def load_streams(cfg):
    """Each transcript file = one stream (session or sidechain). Returns list of
    streams, each a sorted list of event dicts {ts, cwd, branch, session, uuid}."""
    streams = []
    sessions_seen = set()
    for path in glob.glob(os.path.join(cfg["_root"], "**", "*.jsonl"), recursive=True):
        evs = []
        try:
            with open(path) as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    ts = o.get("timestamp")
                    if not ts:
                        continue
                    try:
                        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    sid = o.get("sessionId")
                    if sid:
                        sessions_seen.add(sid)
                    # kind: classify for the Claude-gen vs your-turn diagnostic.
                    # tool_results are role=user but are mid-Claude-turn, so only a real
                    # typed prompt counts as "prompt".
                    typ = o.get("type"); msg = o.get("message") or {}
                    cont = msg.get("content")
                    is_tr = bool(o.get("toolUseResult")) or (
                        isinstance(cont, list) and any(isinstance(b, dict)
                        and b.get("type") == "tool_result" for b in cont))
                    kind = ("assistant" if typ == "assistant"
                            else "tool" if (typ == "user" and is_tr)
                            else "prompt" if typ == "user" else "other")
                    evs.append({"ts": t, "cwd": o.get("cwd"), "branch": o.get("gitBranch"),
                                "session": sid, "uuid": o.get("uuid"), "kind": kind})
        except Exception:
            continue
        if evs:
            evs.sort(key=lambda e: e["ts"])
            # carry cwd/branch across events that omit them (system/meta events have
            # null cwd but belong to the same session directory) — forward then back.
            last_cwd = last_branch = None
            for e in evs:
                last_cwd = e["cwd"] or last_cwd
                last_branch = e["branch"] or last_branch
                e["cwd"], e["branch"] = last_cwd, last_branch
            first_cwd = next((e["cwd"] for e in evs if e["cwd"]), None)
            first_branch = next((e["branch"] for e in evs if e["branch"]), None)
            for e in evs:
                e["cwd"] = e["cwd"] or first_cwd
                e["branch"] = e["branch"] or first_branch
            streams.append(evs)
    return streams, sessions_seen


def load_cursor_streams(cfg):
    """Load Cursor agent transcripts from ~/.cursor/projects/*/agent-transcripts/**/*.jsonl.

    Cursor transcripts lack top-level timestamp/cwd/branch fields.  We recover them:
      - timestamp : parsed from <timestamp>…</timestamp> in each user message text (~98% present)
      - cwd       : derived from the Cursor project folder name via filesystem walk
      - branch    : not available; left None (topic resolves to repo name or loose label)
      - session   : transcript file UUID (from filename)
      - kind      : always "prompt" (only user messages emit events; assistant turns have no ts)

    Returns (streams, sessions_seen) in the same shape as load_streams()."""
    root = cfg["_cursor_root"]
    streams = []
    sessions_seen = set()
    pattern = os.path.join(root, "*", "agent-transcripts", "**", "*.jsonl")
    for path in glob.glob(pattern, recursive=True):
        # Derive workspace path from the Cursor project folder name (2nd path component).
        # ~/.cursor/projects/<project-key>/agent-transcripts/<uuid>/<uuid>.jsonl
        rel = os.path.relpath(path, root)
        project_key = rel.split(os.sep)[0]
        cwd = _decode_cursor_project_path(project_key)

        # Session ID = transcript file UUID (basename without extension).
        session = os.path.splitext(os.path.basename(path))[0]

        evs = []
        try:
            with open(path) as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("role") != "user":
                        continue
                    # Extract timestamp from the user message text.
                    content = (o.get("message") or {}).get("content", [])
                    text = ""
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text = item.get("text", "")
                                break
                    elif isinstance(content, str):
                        text = content
                    ts = _parse_cursor_timestamp(text)
                    if ts is None:
                        continue
                    evs.append({"ts": ts, "cwd": cwd, "branch": None,
                                "session": session, "uuid": None, "kind": "prompt"})
        except Exception:
            continue

        if evs:
            evs.sort(key=lambda e: e["ts"])
            sessions_seen.add(session)
            streams.append(evs)

    return streams, sessions_seen


def load_all_streams(cfg):
    """Combine Claude Code and Cursor agent transcript streams."""
    streams, sessions = load_streams(cfg)
    if cfg.get("_cursor_root") and os.path.isdir(cfg["_cursor_root"]):
        c_streams, c_sessions = load_cursor_streams(cfg)
        streams += c_streams
        sessions |= c_sessions
    return streams, sessions
