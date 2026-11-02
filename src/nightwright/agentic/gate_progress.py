"""Gate progress percent (SPEC-0373): how far a running gate is, for `nw queue watch` and
`nw queue gate-slots`. The source is the gate's own output: pytest prints `[ 42%]` at line ends,
and a local gate's log (`gates/NNN-name.log`) is written live, so the LAST such marker in its tail
is the percent. A remote (worker) gate's output reaches the controller only when the gate ends,
so it has no live percent; and a gate with no marker has none either. Both fall back to an
estimate, elapsed over the campaign's previous gate duration, shown as `~NN%`. Pure helpers plus
one stat-cached log read; nothing here is written to disk."""
from __future__ import annotations

import datetime as dt
import re

from . import store

_PCT = re.compile(r"\[\s*(\d{1,3})%\]")
TAIL_BYTES = 8192
_CACHE: dict = {}           # log path -> ((mtime_ns, size), percent or None)


def parse_percent(text: str) -> int | None:
    """The last `[ NN%]` marker in `text` (0-100), else None."""
    found = _PCT.findall(text or "")
    if not found:
        return None
    return min(int(found[-1]), 100)


def _log_percent(path) -> int | None:
    """Percent from the end of one log, re-read only when its (mtime, size) changed."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - TAIL_BYTES))
            pct = parse_percent(f.read().decode("utf-8", errors="replace"))
    except OSError:
        return None
    _CACHE[str(path)] = (key, pct)
    return pct


def live_percent(cfg, campaign_id, since: str | None) -> int | None:
    """The percent in the newest gate log written since the hold began (`since`, ISO); an
    older log belongs to a previous run and is ignored."""
    try:
        t0 = dt.datetime.fromisoformat(since).timestamp() if since else None
    except (TypeError, ValueError):
        t0 = None
    best, best_m = None, -1
    try:
        for p in store.gates_dir(cfg, campaign_id).glob("*.log"):
            m = p.stat().st_mtime
            if (t0 is None or m >= t0 - 1) and m > best_m:
                best, best_m = p, m
    except OSError:
        return None
    return _log_percent(best) if best else None


def previous_duration(events) -> float | None:
    """`duration_s` of the newest `gate` event in `events`, else None."""
    for e in reversed(events or []):
        if e.get("type") == "gate" and isinstance(e.get("duration_s"), (int, float)) \
                and e["duration_s"] > 0:
            return float(e["duration_s"])
    return None


def estimate(elapsed_s, previous_s) -> int | None:
    """Elapsed over the previous gate's duration, capped at 99 (a guess never says done)."""
    if not elapsed_s or not previous_s:
        return None
    return max(0, min(99, int(100 * elapsed_s / previous_s)))


def progress(cfg, campaign_id, *, since, elapsed_s, events, remote: bool = False) -> dict | None:
    """`{percent, estimated}` for a running gate, else None when nothing is known. `remote`
    gates have no live output here, so they only get the estimate."""
    pct = None if remote else live_percent(cfg, campaign_id, since)
    if pct is not None:
        return {"percent": pct, "estimated": False}
    est = estimate(elapsed_s, previous_duration(events))
    return None if est is None else {"percent": est, "estimated": True}


def bar(pct: int, estimated: bool = False, width: int = 8) -> str:
    """`▕████░░░░▏ 42%`; an estimate reads `~42%`."""
    pct = max(0, min(100, int(pct)))
    full = round(pct / 100 * width)
    return f"▕{'█' * full}{'░' * (width - full)}▏ {'~' if estimated else ''}{pct}%"


def label(prog: dict | None) -> str:
    return bar(prog["percent"], prog["estimated"]) if prog else ""
