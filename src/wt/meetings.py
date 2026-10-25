"""Outlook/M365 meetings: read the cached meetings.jsonl, filter a raw calendar dump into
it, and refresh it via a headless `claude -p` + m365 MCP call."""
import json
import os
import subprocess

from rich.markup import escape

from .render import note, warn


def meetings_path(cfg):
    p = cfg["meetings_file"]
    return p if os.path.isabs(p) else os.path.join(cfg["data_dir"], p)


def read_meetings(cfg):
    p = meetings_path(cfg)
    out = []
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    return out


def _attended(ev, mf):
    if ev.get("isAllDay") or ev.get("isCancelled"):
        return False
    if ev.get("subject") in (mf.get("allowlist") or []):
        return True
    resp = (ev.get("responseStatus") or {}).get("response")
    return ev.get("showAs") in mf["status"] and resp in mf["response"]


def ingest_meetings(cfg, rawfile):
    """Filter a raw search_events JSON dump -> upsert meetings.jsonl (dedup by id)."""
    with open(rawfile) as f:
        data = json.load(f)
    events = data.get("value", data) if isinstance(data, dict) else data
    mf = cfg["meeting_filter"]
    existing = {m["id"]: m for m in read_meetings(cfg)}
    added = 0
    for ev in events:
        if not _attended(ev, mf):
            continue
        rec = {
            "id": ev["id"],
            "subject": ev.get("subject", ""),
            "start": ev["start"]["dateTime"], "end": ev["end"]["dateTime"],
            "showAs": ev.get("showAs"),
            "response": (ev.get("responseStatus") or {}).get("response"),
        }
        if rec["id"] not in existing:
            added += 1
        existing[rec["id"]] = rec
    with open(meetings_path(cfg), "w") as f:
        for m in sorted(existing.values(), key=lambda x: x["start"]):
            f.write(json.dumps(m) + "\n")
    from .console import console
    console.print(f"ingested {len(existing)} attended meetings ({added} new) -> "
                  f"{escape(str(meetings_path(cfg)))}", highlight=False)


def _harvest_tool_result(stdout, toolname):
    """Pull a tool's raw result out of `claude --output-format stream-json` output.
    The MCP payload rides verbatim in the stream, so the model never retypes it
    (avoids truncation). Returns the largest matching tool_result text, or None."""
    names, best = {}, None
    for ln in stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        msg = o.get("message", {})
        if not isinstance(msg, dict):
            continue
        for c in msg.get("content", []) or []:
            if c.get("type") == "tool_use":
                names[c["id"]] = c["name"]
            elif c.get("type") == "tool_result" and names.get(c.get("tool_use_id")) == toolname:
                cont = c.get("content")
                txt = ("".join(b.get("text", "") for b in cont if isinstance(b, dict))
                       if isinstance(cont, list) else cont if isinstance(cont, str) else "")
                if best is None or len(txt) > len(best):
                    best = txt
    return best


def refresh_meetings(cfg, start, end):
    """Headless `claude -p` pulls the calendar via the m365 MCP; we harvest the raw
    tool_result from the stream-json output, cache it, then deterministically ingest."""
    cache_dir = cfg["cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"meetings_{start[:10]}_{end[:10]}.json")
    prompt = (
        f"Use the mcp__m365__search_events tool to list calendar events with "
        f"start={start} end={end} count=250 timezone='Eastern Standard Time'. "
        f"If the tool isn't loaded yet, load it first via ToolSearch. Make exactly one "
        f"search_events call, then stop with no commentary."
    )
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", "claude-haiku-4-5-20251001",
           "--allowedTools", "mcp__m365__search_events", "ToolSearch", "--max-turns", "6"]
    note("running headless claude to refresh meetings cache ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    raw = _harvest_tool_result(r.stdout, "mcp__m365__search_events")
    if not raw:
        warn("  ! meeting refresh produced no search_events result (check m365 OAuth via "
              "/mcp). meetings.jsonl left unchanged.")
        return
    with open(cache, "w") as f:
        f.write(raw)
    ingest_meetings(cfg, cache)
